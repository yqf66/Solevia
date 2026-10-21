import json

import pytest
from urllib.error import HTTPError
from pydantic import SecretStr

from planner_api.ai import (
    ClarificationContext,
    DeepSeekClarificationProvider,
    OpenAICompatibleClarificationProvider,
    ProviderConfigurationError,
    ProviderUnavailableError,
    _urllib_json_transport,
    build_clarification_provider,
    configured_json_transport,
)
from planner_api.config import Settings


def test_balance_rejection_does_not_enter_format_fallback():
    calls = []

    def rejected(url, headers, payload, timeout):
        calls.append(url)
        raise HTTPError(url, 402, "Payment required", {}, None)

    provider = DeepSeekClarificationProvider(deepseek_settings(), transport=rejected, sleeper=lambda _: None)
    with pytest.raises(ProviderUnavailableError) as caught:
        provider.respond(ClarificationContext(raw_input="合成需求", active_facts={},
                                             latest_user_message="继续", answer_for_keys=[]))
    assert len(calls) == 1
    assert caught.value.diagnostic == "HTTP 402"
    assert "额度不足" in str(caught.value)


def test_truncated_structured_response_retries_with_bounded_larger_budget():
    from planner_api.schemas import RequirementAnalysis

    budgets = []
    result = RequirementAnalysis(summary="需要确认目标", proposed_facts=[], conflicts=[], open_questions=[],
                                 readiness={"ready_for_review": False, "blocking_fields": ["objective"], "reason": "需确认"})

    def transport(url, headers, payload, timeout):
        budgets.append(payload['max_output_tokens'])
        if len(budgets) == 1:
            return {"status": "incomplete", "incomplete_details": {"reason": "max_output_tokens"}}
        return response_with_text(result.model_dump(mode='json'))

    provider = DeepSeekClarificationProvider(deepseek_settings(llm_max_output_tokens=3000),
                                             transport=transport, sleeper=lambda _: None)
    parsed = provider.analyze(ClarificationContext(raw_input="合成", active_facts={},
                                                  latest_user_message="继续", answer_for_keys=[]), "已保存")
    assert parsed.readiness.ready_for_review is False
    # The last available retry uses the bounded ceiling rather than risking
    # another output-length failure at only twice the original allowance.
    assert budgets == [3000, 16000]


def test_incomplete_http_body_enters_provider_network_retry_path(monkeypatch):
    from http.client import IncompleteRead
    from urllib.error import URLError
    from unittest.mock import MagicMock

    response = MagicMock()
    response.__enter__.return_value = response
    response.read.side_effect = IncompleteRead(b"partial model response")
    monkeypatch.setattr("planner_api.ai.urlopen", lambda *args, **kwargs: response)
    with pytest.raises(URLError, match="未收到完整响应"):
        _urllib_json_transport("https://example.com/responses", {}, {}, 10)
    response.__exit__.assert_called_once()


def valid_model_result() -> dict:
    return {
        "response_type": "CLARIFY",
        "assistant_message": "我先分析这个目标，再确认一个关键问题：你目前进行到哪一步？",
        "proposed_facts": [
            {
                "category": "OBJECTIVE",
                "field_key": "objective",
                "value": "完成一个项目",
                "source": "USER",
                "status": "CONFIRMED",
                "confidence": 1,
            }
        ],
        "conflicts": [],
        "questions": ["你目前进行到哪一步？"],
        "question_keys": ["current_state"],
        "open_questions": ["你目前进行到哪一步？"],
        "readiness": {
            "ready_for_review": False,
            "blocking_fields": ["current_state"],
            "reason": "当前基础尚未明确。",
        },
    }


def response_with_text(value: dict) -> dict[str, object]:
    return {
        "status": "completed",
        "output": [
            {
                "type": "message",
                "content": [{"type": "output_text", "text": f"```json\n{json.dumps(value)}\n```"}],
            }
        ],
    }


def response_with_plain_text(value: str) -> dict[str, object]:
    return {
        "status": "completed",
        "output": [
            {
                "type": "message",
                "content": [{"type": "output_text", "text": value}],
            }
        ],
    }


def deepseek_settings(**overrides) -> Settings:
    values = {
        "llm_provider": "deepseek",
        "llm_api_key": SecretStr("test-secret-key"),
        "llm_base_url": "https://api.deepseek.com",
        "llm_model": "deepseek-v4-flash",
        "llm_max_retries": 1,
    }
    values.update(overrides)
    return Settings(**values)


def context() -> ClarificationContext:
    return ClarificationContext(
        raw_input="完成一个项目",
        active_facts={},
        latest_user_message=None,
        answer_for_keys=[],
    )


def test_real_provider_builds_responses_api_request_and_parses_schema() -> None:
    captured = {}

    def transport(url, headers, payload, timeout):
        captured.update(url=url, headers=headers, payload=payload, timeout=timeout)
        return response_with_text(valid_model_result())

    provider = DeepSeekClarificationProvider(deepseek_settings(), transport=transport)
    result = provider.clarify(context())

    assert captured["url"] == "https://api.deepseek.com/responses"
    assert captured["payload"]["model"] == "deepseek-v4-flash"
    assert captured["payload"]["text"]["format"]["type"] == "json_schema"
    assert "strict" not in captured["payload"]["text"]["format"]
    input_context = json.loads(captured["payload"]["input"])
    assert input_context["turn_kind"] == "INITIAL"
    assert input_context["confirmed_facts"] == {}
    assert input_context["proposed_facts"] == {}
    assert input_context["conversation_summary"] == ""
    assert "test-secret-key" not in json.dumps(captured["payload"])
    assert captured["headers"]["Authorization"] == "Bearer test-secret-key"
    assert result.question_keys == ["current_state"]


def test_provider_exposes_separate_conversation_and_analysis_requests() -> None:
    payloads = []

    def transport(_url, _headers, payload, _timeout):
        payloads.append(payload)
        if len(payloads) == 1:
            return response_with_plain_text("我们先聊聊你想解决的问题。")
        return response_with_text(
            {
                "proposed_facts": [],
                "conflicts": [],
                "open_questions": ["想解决的问题"],
                "current_topic": "objective",
                "summary": "正在讨论目标。",
                "entity_refs": [],
                "readiness": {
                    "ready_for_review": False,
                    "blocking_fields": ["objective"],
                    "reason": "目标还需要讨论。",
                },
            }
        )

    provider = DeepSeekClarificationProvider(deepseek_settings(), transport=transport)
    reply = provider.respond(context())
    analysis = provider.analyze(context(), reply.assistant_message)
    assert reply.assistant_message.startswith("我们先聊聊")
    assert analysis.current_topic == "objective"
    assert len(payloads) == 2
    assert payloads[0]["text"]["format"]["type"] == "text"
    assert payloads[1]["text"]["format"]["name"] == "requirement_analysis"


def test_provider_retries_malformed_output_then_succeeds() -> None:
    attempts = []

    def transport(_url, _headers, _payload, _timeout):
        attempts.append(1)
        if len(attempts) == 1:
            return {"status": "completed", "output": []}
        return response_with_text(valid_model_result())

    provider = DeepSeekClarificationProvider(
        deepseek_settings(), transport=transport, sleeper=lambda _seconds: None
    )

    assert provider.clarify(context()).response_type == "CLARIFY"
    assert len(attempts) == 2


def test_provider_retries_visible_reply_that_contains_internal_planning_notes() -> None:
    attempts = []

    def transport(_url, _headers, _payload, _timeout):
        attempts.append(1)
        if len(attempts) == 1:
            return response_with_plain_text(
                "用户明确了目标。我应该保持不排期，需要避免追问，留一个问题即可。"
            )
        return response_with_plain_text("明白了，我们先沿着你确认的目标继续讨论。")

    provider = DeepSeekClarificationProvider(
        deepseek_settings(), transport=transport, sleeper=lambda _seconds: None
    )

    reply = provider.respond(context())

    assert reply.assistant_message.startswith("明白了")
    assert len(attempts) == 2


def test_deepseek_conversation_falls_back_to_json_schema_when_plain_text_is_empty() -> None:
    payloads = []

    def transport(_url, _headers, payload, _timeout):
        payloads.append(payload)
        if len(payloads) <= 2:
            return {"status": "completed", "output": []}
        return response_with_text({"assistant_message": "结构化兼容回复"})

    provider = DeepSeekClarificationProvider(
        deepseek_settings(), transport=transport, sleeper=lambda _seconds: None
    )

    reply = provider.respond(context())

    assert reply.assistant_message == "结构化兼容回复"
    assert payloads[0]["text"]["format"]["type"] == "text"
    assert payloads[-1]["text"]["format"]["type"] == "json_schema"


def test_deepseek_output_text_variants_are_accepted() -> None:
    assert DeepSeekClarificationProvider._extract_output_text(
        {"status": "completed", "output_text": "直接文本"}
    ) == "直接文本"
    assert DeepSeekClarificationProvider._extract_output_text(
        {"status": "completed", "output": "字符串输出"}
    ) == "字符串输出"
    assert DeepSeekClarificationProvider._extract_output_text(
        {
            "status": "completed",
            "output": [{"type": "output_text", "text": "消息文本"}],
        }
    ) == "消息文本"


def test_compatible_retry_receives_safe_schema_feedback() -> None:
    payloads = []

    def transport(_url, _headers, payload, _timeout):
        payloads.append(payload)
        if len(payloads) == 1:
            invalid = valid_model_result()
            invalid["proposed_facts"][0]["field_key"] = "topic"
            return {"choices": [{"message": {"content": json.dumps(invalid)}}]}
        return {"choices": [{"message": {"content": json.dumps(valid_model_result())}}]}

    provider = OpenAICompatibleClarificationProvider(
        deepseek_settings(llm_provider="openai_compatible"),
        transport=transport,
        sleeper=lambda _seconds: None,
    )

    assert provider.clarify(context()).response_type == "CLARIFY"
    retry_message = payloads[1]["messages"][-1]["content"]
    assert "proposed_facts.0.field_key" in retry_message
    assert "test-secret-key" not in retry_message


def test_provider_failure_is_safe_and_does_not_expose_key() -> None:
    def transport(_url, _headers, _payload, _timeout):
        return {"status": "completed", "output": []}

    provider = DeepSeekClarificationProvider(
        deepseek_settings(), transport=transport, sleeper=lambda _seconds: None
    )

    with pytest.raises(ProviderUnavailableError) as raised:
        provider.clarify(context())

    assert "test-secret-key" not in str(raised.value)
    assert "内容已经保存" in str(raised.value)


def test_compatible_timeout_is_classified_and_retried_only_once() -> None:
    attempts = []

    def transport(*_args):
        attempts.append(1)
        raise TimeoutError("gateway timed out")

    provider = OpenAICompatibleClarificationProvider(
        deepseek_settings(llm_provider="openai_compatible", llm_max_retries=5),
        transport=transport,
        sleeper=lambda _seconds: None,
    )

    with pytest.raises(ProviderUnavailableError) as raised:
        provider.clarify(context())

    assert len(attempts) == 2
    assert raised.value.code == "MODEL_TIMEOUT"
    assert "等待得有些久" in str(raised.value)


def test_deepseek_provider_requires_key_but_fake_does_not() -> None:
    with pytest.raises(ProviderConfigurationError):
        build_clarification_provider(Settings(llm_provider="deepseek", llm_api_key=SecretStr("")))

    provider = build_clarification_provider(Settings(llm_provider="fake"))
    assert type(provider).__name__ == "FakeClarificationProvider"


def test_openai_compatible_provider_uses_chat_completions() -> None:
    captured = {}

    def transport(url, headers, payload, timeout):
        captured.update(url=url, headers=headers, payload=payload, timeout=timeout)
        return {
            "choices": [
                {"message": {"content": f"```json\n{json.dumps(valid_model_result())}\n```"}}
            ]
        }

    settings = deepseek_settings(
        llm_provider="openai_compatible",
        llm_base_url="https://gateway.example/v1",
        llm_model="provider-model-id",
    )
    provider = OpenAICompatibleClarificationProvider(settings, transport=transport)
    result = provider.clarify(context())

    assert captured["url"] == "https://gateway.example/v1/chat/completions"
    assert captured["payload"]["model"] == "provider-model-id"
    assert captured["payload"]["response_format"] == {"type": "json_object"}
    assert result.assistant_message.startswith("我先分析")


def test_app_proxy_wraps_only_the_builtin_transport() -> None:
    settings = deepseek_settings(llm_proxy_url="http://127.0.0.1:18888")
    custom_transport = lambda *_args: {}  # noqa: E731

    proxied = configured_json_transport(settings, _urllib_json_transport)

    assert proxied is not _urllib_json_transport
    assert configured_json_transport(settings, custom_transport) is custom_transport
    assert (
        configured_json_transport(
            deepseek_settings(llm_proxy_url=""),
            _urllib_json_transport,
        )
        is _urllib_json_transport
    )


def test_schema_rejects_free_form_fact_keys() -> None:
    invalid = valid_model_result()
    invalid["proposed_facts"][0]["field_key"] = "duration_weeks"

    with pytest.raises(ProviderUnavailableError):
        DeepSeekClarificationProvider(
            deepseek_settings(),
            transport=lambda *_args: response_with_text(invalid),
            sleeper=lambda _seconds: None,
        ).clarify(context())


def test_schema_requires_questions_to_match_visible_message() -> None:
    invalid = valid_model_result()
    invalid["assistant_message"] = "我已经理解了。"

    with pytest.raises(ProviderUnavailableError):
        DeepSeekClarificationProvider(
            deepseek_settings(llm_max_retries=0),
            transport=lambda *_args: response_with_text(invalid),
        ).clarify(context())
