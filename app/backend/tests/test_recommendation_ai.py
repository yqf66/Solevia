import json

import pytest
from pydantic import SecretStr
from planner_api.ai import ProviderUnavailableError, ProviderConfigurationError

from planner_api.config import Settings
from planner_api.recommendation_ai import RecommendationProvider, recommendation_policy_fingerprint


def settings(provider="openai_compatible"):
    return Settings(
        llm_provider=provider,
        llm_api_key=SecretStr("real-test-key"),
        llm_base_url="https://gateway.example/v1",
        llm_model="planner-model",
    )


def test_provider_restricts_transport_to_structured_selection():
    calls = []

    def transport(url, headers, payload, timeout):
        calls.append((url, headers, payload, timeout))
        return {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {
                                "selections": [{"task_id": "task-2", "rationale": "期限更近"}],
                                "summary": "先处理期限更近的任务",
                            },
                            ensure_ascii=False,
                        )
                    }
                }
            ]
        }

    result = RecommendationProvider(settings(), transport=transport).rank(
        [{"task_id": "task-1", "title": "A"}, {"task_id": "task-2", "title": "B"}],
        "今天优先紧急项",
        3,
        capacity={"spare_minutes": 60},
    )
    assert result.selections[0].task_id == "task-2"
    assert calls[0][0].endswith("/chat/completions")
    assert calls[0][2]["response_format"] == {"type": "json_object"}
    assert calls[0][3] <= 45


def test_provider_rejects_extra_fields_and_invalid_shape():
    def transport(*_args):
        return {
            "choices": [
                {
                    "message": {
                        "content": '{"selections":[{"task_id":"x","rationale":"ok","extra":true}]}'
                    }
                }
            ]
        }

    with pytest.raises(ProviderUnavailableError):
        RecommendationProvider(settings(), transport=transport).rank(
            [{"task_id": "x"}], "", 1, capacity={"spare_minutes": 1}
        )


def test_fake_provider_is_never_used_for_ai_rank():
    with pytest.raises(ProviderConfigurationError, match="真实模型未配置"):
        RecommendationProvider(settings("fake"))


@pytest.mark.parametrize(
    "response",
    [
        {"status": "incomplete", "output_text": '{"selections":[]}'},
        {"status": "failed", "output_text": '{"selections":[]}'},
        {"choices": [{"finish_reason": "length", "message": {"content": '{"selections":[]}'}}]},
        {
            "choices": [
                {"finish_reason": "content_filter", "message": {"content": '{"selections":[]}'}}
            ]
        },
    ],
)
def test_valid_json_from_unfinished_response_is_not_a_success(response):
    with pytest.raises(ProviderUnavailableError):
        RecommendationProvider(settings(), transport=lambda *_: response).rank(
            [{"task_id": "x"}], "", 1, capacity={"spare_minutes": 30}
        )


@pytest.mark.parametrize(
    "update",
    [
        {"llm_api_key": SecretStr("changed-key")},
        {"llm_proxy_url": "http://127.0.0.1:9876"},
        {"llm_max_output_tokens": 1000},
        {"llm_timeout_seconds": 20},
    ],
)
def test_effective_policy_change_invalidates_reuse(update):
    original = settings()
    assert recommendation_policy_fingerprint(original) != recommendation_policy_fingerprint(
        original.model_copy(update=update)
    )


def test_ineffective_output_budget_change_keeps_reuse():
    original = settings()
    assert recommendation_policy_fingerprint(original) == recommendation_policy_fingerprint(
        original.model_copy(update={"llm_max_output_tokens": 6000})
    )


def test_one_format_repair_keeps_candidates_and_capacity():
    calls = []

    def transport(_url, _headers, payload, timeout):
        calls.append((json.loads(json.dumps(payload)), timeout))
        text = (
            '{"selections":[{"task_id":"x","rationale":"即将到期","summary":""}]}'
            if len(calls) == 1
            else '{"selections":[{"task_id":"x","rationale":"即将到期"}],"summary":"建议先做一项"}'
        )
        return {"choices": [{"finish_reason": "stop", "message": {"content": text}}]}

    result = RecommendationProvider(settings(), transport=transport).rank(
        [{"task_id": "x", "estimate_minutes": 20}],
        "最多20分钟",
        1,
        capacity={"spare_minutes": 20},
    )
    assert result.selections[0].task_id == "x"
    assert len(calls) == 2 and calls[1][1] <= calls[0][1]
    assert calls[0][0]["messages"][1] == calls[1][0]["messages"][1]
    assert "selections.0.summary" in calls[1][0]["messages"][0]["content"]
