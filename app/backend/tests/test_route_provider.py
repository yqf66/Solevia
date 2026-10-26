import json

import pytest
from urllib.error import HTTPError

from pydantic import SecretStr

from planner_api.config import Settings
from planner_api.ai import ProviderUnavailableError
from planner_api.route_ai import (
    DeepSeekRouteProvider,
    NaturalRouteAuthor,
    OpenAICompatibleRouteProvider,
    RouteContext,
    SemanticRouteProvider,
)
from planner_api.route_prompts import ROUTE_INSTRUCTIONS, ROUTE_PROMPT_VERSION


def route_result() -> dict:
    return {
        "assistant_message": "我根据需求设计了三个一级阶段。",
        "summary": "先确认起点，再完成核心工作，最后验证成果。",
        "stages": [
            {
                "order": 1,
                "title": "确认起点",
                "purpose": "明确当前条件以及后续推进需要遵守的边界。",
                "topics": ["当前基线", "关键约束"],
                "outputs": ["起点清单"],
                "depends_on": [],
            },
            {
                "order": 2,
                "title": "推进核心工作",
                "purpose": "围绕最终目标形成一个可以检查的核心成果。",
                "topics": ["核心模块", "中间验证"],
                "outputs": ["核心成果"],
                "depends_on": [1],
            },
            {
                "order": 3,
                "title": "验证并交付",
                "purpose": "按照成功标准检查结果并完成最终交付。",
                "topics": ["成果检查", "最终交付"],
                "outputs": ["通过检查的最终成果"],
                "depends_on": [2],
            },
        ],
        "tradeoffs": ["暂不展开每日安排。"],
        "assumptions": [],
        "open_questions": [],
        "change_summary": [],
    }


def settings() -> Settings:
    return Settings(
        llm_provider="openai_compatible",
        llm_api_key=SecretStr("route-test-key"),
        llm_base_url="https://gateway.example/v1",
        llm_model="route-model",
        llm_max_retries=1,
        route_timeout_seconds=90,
        route_max_retries=1,
    )


@pytest.mark.parametrize("provider_class", [DeepSeekRouteProvider, OpenAICompatibleRouteProvider, NaturalRouteAuthor])
def test_route_balance_error_is_explicit_and_does_not_retry(provider_class):
    calls = []

    def rejected(url, headers, payload, timeout):
        calls.append(url)
        raise HTTPError(url, 402, "Payment required", {}, None)

    provider = provider_class(settings(), transport=rejected, sleeper=lambda _: None)
    method = provider.draft_document if isinstance(provider, NaturalRouteAuthor) else provider.draft_route
    with pytest.raises(ProviderUnavailableError) as caught:
        method(RouteContext(requirements={"objective": "合成验收"}))
    assert len(calls) == 1
    assert caught.value.diagnostic == "HTTP 402"
    assert caught.value.code == "MODEL_REQUEST_REJECTED"
    assert "额度不足" in str(caught.value)
    assert "已保存" in str(caught.value)
    assert "route-test-key" not in str(caught.value)


def test_compatible_route_provider_uses_confirmed_requirement() -> None:
    captured = {}

    def transport(url, headers, payload, timeout):
        captured.update(url=url, headers=headers, payload=payload, timeout=timeout)
        return {"choices": [{"message": {"content": json.dumps(route_result())}}]}

    provider = OpenAICompatibleRouteProvider(settings(), transport=transport)
    result = provider.draft_route(
        RouteContext(
            requirements={"objective": "建立一个容易维护的阳台花园"},
            requirement_memory={
                "change_history": [
                    {"field_key": "available_capacity", "operation": "REPLACE"}
                ]
            },
        )
    )

    assert captured["url"] == "https://gateway.example/v1/chat/completions"
    assert captured["payload"]["model"] == "route-model"
    assert captured["timeout"] == 90
    request_context = json.loads(captured["payload"]["messages"][1]["content"])
    assert request_context["confirmed_requirements"]["objective"] == ("建立一个容易维护的阳台花园")
    assert request_context["mode"] == "CREATE"
    assert request_context["requirement_memory"]["change_history"][0]["operation"] == (
        "REPLACE"
    )
    assert "route-test-key" not in json.dumps(captured["payload"], ensure_ascii=False)
    assert len(result.stages) == 3


def test_semantic_route_provider_authors_then_compiles_the_same_document() -> None:
    calls = []
    document = "# 路线概览\n先准备，再实践，最后验收。"

    def transport(_url, _headers, payload, _timeout):
        calls.append(payload)
        if len(calls) == 1:
            return {"choices": [{"message": {"content": document}}]}
        return {"choices": [{"message": {"content": json.dumps(route_result())}}]}

    author = NaturalRouteAuthor(settings(), transport=transport)
    compiler = OpenAICompatibleRouteProvider(settings(), transport=transport)
    result = SemanticRouteProvider(author, compiler).draft_route(
        RouteContext(requirements={"objective": "完成目标"})
    )

    assert len(calls) == 2
    assert "只输出路线正文" in calls[0]["messages"][0]["content"]
    compiler_input = json.loads(calls[1]["messages"][1]["content"])
    assert compiler_input["source_document"] == document
    assert result.source_document == document
    assert result.compiler_report["status"] == "VALIDATED"


def test_deepseek_route_accepts_fenced_json_without_unsupported_strict_flag() -> None:
    captured = {}

    def transport(url, headers, payload, timeout):
        captured["payload"] = payload
        content = f"```json\n{json.dumps(route_result(), ensure_ascii=False)}\n```"
        return {
            "status": "completed",
            "output": [{"type": "message", "content": [{"type": "output_text", "text": content}]}],
        }

    provider = DeepSeekRouteProvider(
        Settings(
            llm_provider="deepseek",
            llm_api_key=SecretStr("route-test-key"),
            llm_base_url="https://api.deepseek.com",
            llm_model="deepseek-v4-flash",
        ),
        transport=transport,
    )
    result = provider.draft_route(RouteContext(requirements={"objective": "完成目标"}))

    assert len(result.stages) == 3
    assert "strict" not in captured["payload"]["text"]["format"]


def test_deepseek_route_accepts_responses_text_variants() -> None:
    value = json.dumps(route_result(), ensure_ascii=False)
    variants = [
        {"status": "completed", "output_text": value},
        {"status": "completed", "output": value},
        {"status": "completed", "output": [{"type": "output_text", "text": value}]},
        {"status": "completed", "output": [{"type": "message", "content": value}]},
        {
            "status": "completed",
            "output": [{"type": "message", "content": [{"type": "text", "text": value}]}],
        },
    ]

    for response in variants:
        provider = DeepSeekRouteProvider(
            Settings(
                llm_provider="deepseek",
                llm_api_key=SecretStr("route-test-key"),
                llm_base_url="https://api.deepseek.com",
                llm_model="deepseek-v4-flash",
            ),
            transport=lambda *_args, response=response: response,
        )
        result = provider.draft_route(RouteContext(requirements={"objective": "完成目标"}))
        assert len(result.stages) == 3


def test_route_provider_retries_invalid_dependency_with_feedback() -> None:
    payloads = []

    def transport(_url, _headers, payload, _timeout):
        payloads.append(payload)
        result = route_result()
        if len(payloads) == 1:
            result["stages"][0]["depends_on"] = [2]
        return {"choices": [{"message": {"content": json.dumps(result)}}]}

    provider = OpenAICompatibleRouteProvider(
        settings(), transport=transport, sleeper=lambda _seconds: None
    )
    result = provider.draft_route(RouteContext(requirements={"objective": "完成目标"}))

    assert result.stages[0].depends_on == []
    assert "Schema 校验" in payloads[1]["messages"][-1]["content"]


def test_route_prompt_stays_above_daily_schedule_layer() -> None:
    assert ROUTE_PROMPT_VERSION == "route-v4-schedule-types"
    assert "3–7 个一级阶段" in ROUTE_INSTRUCTIONS
    assert "不生成每日任务" in ROUTE_INSTRUCTIONS
    assert "生活、运动、育儿或园艺" in ROUTE_INSTRUCTIONS
    assert "不要计算总小时数" in ROUTE_INSTRUCTIONS
    assert "可选增强项不得阻塞" in ROUTE_INSTRUCTIONS
    assert "2–4 个大白话选项" in ROUTE_INSTRUCTIONS
    assert "非阻塞脚手架" in ROUTE_INSTRUCTIONS
    assert "EXPLAIN" not in ROUTE_INSTRUCTIONS
