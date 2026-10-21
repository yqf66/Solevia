import json

from pydantic import SecretStr

from planner_api.config import Settings
from planner_api.route_explanation_ai import (
    DeepSeekRouteExplanationProvider,
    OpenAICompatibleRouteExplanationProvider,
    RouteExplanationContext,
)
from planner_api.route_explanation_prompts import (
    ROUTE_EXPLANATION_INSTRUCTIONS,
    ROUTE_EXPLANATION_PROMPT_VERSION,
)


def settings() -> Settings:
    return Settings(
        llm_provider="openai_compatible",
        llm_api_key=SecretStr("explanation-test-key"),
        llm_base_url="https://gateway.example/v1",
        llm_model="explanation-model",
        route_explanation_max_output_tokens=2400,
        route_explanation_timeout_seconds=75,
        route_explanation_max_retries=1,
    )


def context() -> RouteExplanationContext:
    return RouteExplanationContext(
        requirements={"objective": "准备后端面试"},
        current_route={
            "summary": "先学习基础，再综合复习。",
            "stages": [
                {
                    "order": 1,
                    "title": "学习基础",
                    "purpose": "建立后续需要的知识基础。",
                    "topics": ["数据结构"],
                    "outputs": ["知识笔记"],
                    "depends_on": [],
                    "options": [],
                },
                {
                    "order": 2,
                    "title": "综合复习与模拟面试",
                    "purpose": "整合知识并练习表达。",
                    "topics": ["模拟面试"],
                    "outputs": ["模拟面试记录"],
                    "depends_on": [1],
                    "options": [],
                },
            ],
        },
        question="模拟面试是什么、怎么做、需要什么环境？",
    )


def test_explanation_provider_uses_dedicated_prompt_schema_and_budget() -> None:
    captured = {}

    def transport(url, headers, payload, timeout):
        captured.update(url=url, headers=headers, payload=payload, timeout=timeout)
        return {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {
                                "answer": (
                                    "模拟面试就是按真实面试流程练习回答。准备题目清单和录音工具，"
                                    "完成后回放并记录改进点。"
                                ),
                                "covered_questions": [
                                    "模拟面试是什么",
                                    "具体怎么做",
                                    "需要什么环境",
                                ],
                                "needs_clarification": False,
                            },
                            ensure_ascii=False,
                        )
                    }
                }
            ]
        }

    provider = OpenAICompatibleRouteExplanationProvider(settings(), transport=transport)
    result = provider.explain(context())

    assert captured["url"] == "https://gateway.example/v1/chat/completions"
    assert captured["timeout"] == 75
    assert captured["payload"]["max_tokens"] == 2400
    assert "route_draft" not in json.dumps(captured["payload"], ensure_ascii=False)
    assert "RouteExplanationResult" not in result.answer
    request_context = json.loads(captured["payload"]["messages"][1]["content"])
    assert request_context["prompt_version"] == "route-explanation-v1"
    assert request_context["user_question"].startswith("模拟面试是什么")
    assert len(result.covered_questions) == 3


def test_deepseek_explanation_accepts_fenced_json_without_strict_flag() -> None:
    captured = {}
    answer = {
        "answer": "模拟面试按真实流程练习回答，再复盘表达和内容。",
        "covered_questions": ["模拟面试是什么", "怎么做", "需要什么环境"],
        "needs_clarification": False,
    }

    def transport(url, headers, payload, timeout):
        captured["payload"] = payload
        text = f"```json\n{json.dumps(answer, ensure_ascii=False)}\n```"
        return {
            "status": "completed",
            "output": [{"type": "message", "content": [{"type": "output_text", "text": text}]}],
        }

    provider = DeepSeekRouteExplanationProvider(
        Settings(
            llm_provider="deepseek",
            llm_api_key=SecretStr("test-key"),
            llm_base_url="https://api.deepseek.com",
            llm_model="deepseek-v4-flash",
        ),
        transport=transport,
    )
    result = provider.explain(context())

    assert result.needs_clarification is False
    assert "strict" not in captured["payload"]["text"]["format"]


def test_deepseek_explanation_accepts_responses_text_variants() -> None:
    answer = {
        "answer": "模拟面试按真实流程练习回答，再复盘表达和内容。",
        "covered_questions": ["模拟面试是什么"],
        "needs_clarification": False,
    }
    value = json.dumps(answer, ensure_ascii=False)
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
        provider = DeepSeekRouteExplanationProvider(
            Settings(
                llm_provider="deepseek",
                llm_api_key=SecretStr("test-key"),
                llm_base_url="https://api.deepseek.com",
                llm_model="deepseek-v4-flash",
            ),
            transport=lambda *_args, response=response: response,
        )
        result = provider.explain(context())
        assert result.needs_clarification is False


def test_explanation_provider_retries_incomplete_schema_without_route_prompt() -> None:
    payloads = []

    def transport(_url, _headers, payload, _timeout):
        payloads.append(payload)
        content = (
            {"answer": "太短", "covered_questions": [], "needs_clarification": False}
            if len(payloads) == 1
            else {
                "answer": (
                    "模拟面试用于练习真实回答过程。使用题目清单和录音工具完成一轮问答，"
                    "再回放检查内容与表达。"
                ),
                "covered_questions": ["模拟面试是什么", "怎么做", "需要什么环境"],
                "needs_clarification": False,
            }
        )
        return {"choices": [{"message": {"content": json.dumps(content, ensure_ascii=False)}}]}

    provider = OpenAICompatibleRouteExplanationProvider(
        settings(), transport=transport, sleeper=lambda _seconds: None
    )
    result = provider.explain(context())

    assert len(payloads) == 2
    assert "完整、自包含" in payloads[1]["messages"][-1]["content"]
    assert result.answer.endswith("检查内容与表达。")


def test_explanation_prompt_is_isolated_and_requires_complete_answers() -> None:
    assert ROUTE_EXPLANATION_PROMPT_VERSION == "route-explanation-v1"
    assert "不负责生成、修订或确认路线" in ROUTE_EXPLANATION_INSTRUCTIONS
    assert "不能以“后面再说”" in ROUTE_EXPLANATION_INSTRUCTIONS
    assert "最小可行步骤" in ROUTE_EXPLANATION_INSTRUCTIONS
    assert "不改变阶段、顺序、依赖" in ROUTE_EXPLANATION_INSTRUCTIONS
