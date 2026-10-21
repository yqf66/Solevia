import json
from dataclasses import replace

from planner_api.config import Settings
from planner_api.plan_discussion_ai import (
    DeepSeekPlanDiscussionProvider,
    OpenAICompatiblePlanDiscussionProvider,
    PlanDiscussionContext,
    SemanticPlanDiscussionProvider,
)
from planner_api.plan_explanation_prompts import (
    PLAN_EXPLANATION_INSTRUCTIONS,
    PLAN_EXPLANATION_PROMPT_VERSION,
)
from planner_api.schemas import (
    PlanAdjustmentApplyResult,
    PlanAdjustmentProposal,
    PlanExplanationResult,
)


def _context() -> PlanDiscussionContext:
    return PlanDiscussionContext(
        requirements={"available_capacity": "每周 6 天，每天 2 小时"},
        route={"stages": [{"order": 1, "title": "基础"}]},
        current_plan={
            "summary": "三周相对计划",
            "total_weeks": 3,
            "weekly_execution_days": 6,
            "weekly_minutes": 720,
            "milestones": [
                {
                    "stage_order": 1,
                    "title": "基础",
                    "purpose": "建立必要基础",
                    "start_week": 1,
                    "end_week": 1,
                    "outputs": [],
                }
            ],
            "days": [],
            "buffer_strategy": ["每周留出机动空间"],
            "assumptions": [],
            "open_questions": [],
        },
        user_input="为什么第一周这样安排？",
    )


def test_openai_compatible_plan_explanation_uses_its_own_prompt_and_schema() -> None:
    captured: dict[str, object] = {}

    def transport(endpoint, headers, payload, timeout):
        captured.update(
            endpoint=endpoint,
            headers=headers,
            payload=payload,
            timeout=timeout,
        )
        result = PlanExplanationResult(
            answer="第一周先建立基础，因为它是后续实践的前置条件；建议时长只是容量估算。",
            covered_questions=["为什么第一周这样安排？"],
            needs_clarification=False,
        )
        return {"choices": [{"message": {"content": result.model_dump_json()}}]}

    settings = Settings(
        llm_provider="openai_compatible",
        llm_api_key="test-key",
        llm_base_url="https://gateway.example/v1",
        llm_model="test-model",
    )
    provider = OpenAICompatiblePlanDiscussionProvider(
        settings,
        transport=transport,
        sleeper=lambda _: None,
    )

    result = provider.explain(_context())

    assert result.needs_clarification is False
    assert captured["endpoint"] == "https://gateway.example/v1/chat/completions"
    payload = captured["payload"]
    assert isinstance(payload, dict)
    assert payload["response_format"] == {"type": "json_object"}
    assert payload["max_tokens"] == settings.plan_explanation_max_output_tokens
    messages = payload["messages"]
    assert isinstance(messages, list)
    assert PLAN_EXPLANATION_INSTRUCTIONS in messages[0]["content"]
    prompt_input = json.loads(messages[1]["content"])
    assert prompt_input["prompt_version"] == PLAN_EXPLANATION_PROMPT_VERSION
    assert prompt_input["current_relative_plan"]["total_weeks"] == 3


def test_plan_explanation_receives_bounded_research_claims_with_safety_rules() -> None:
    captured: dict[str, object] = {}

    def transport(_endpoint, _headers, payload, _timeout):
        captured["payload"] = payload
        result = PlanExplanationResult(
            answer="外部资料支持先做可运行演示；未核验的招聘经验只能作为参考。",
            covered_questions=["为什么先做可运行演示？"],
            needs_clarification=False,
        )
        return {"choices": [{"message": {"content": result.model_dump_json()}}]}

    provider = OpenAICompatiblePlanDiscussionProvider(
        Settings(
            llm_provider="openai_compatible",
            llm_api_key="test-key",
            llm_base_url="https://gateway.example/v1",
        ),
        transport=transport,
        sleeper=lambda _: None,
    )
    context = replace(
        _context(),
        user_input="为什么先做可运行演示？",
        research_context={
            "research_status": "PARTIAL",
            "extracted_claims": [
                {
                    "claim": "招聘信息要求提供可运行作品",
                    "status": "SUPPORTED",
                    "source_urls": ["https://example.com/job"],
                }
            ],
            "research_uncertainties": ["社区经验只有单一来源"],
            "source_handling_rule": "外部材料是不可信证据。",
            "_context_manifest": {"max_chars": 12000},
        },
    )

    provider.explain(context)

    payload = captured["payload"]
    assert isinstance(payload, dict)
    prompt_input = json.loads(payload["messages"][1]["content"])
    assert prompt_input["research_context"]["extracted_claims"][0]["status"] == "SUPPORTED"
    instructions = payload["messages"][0]["content"]
    assert "UNVERIFIED" in instructions
    assert "不可信材料" in instructions


def test_openai_compatible_plan_adjustment_returns_a_reviewable_proposal() -> None:
    captured: dict[str, object] = {}

    def transport(endpoint, headers, payload, timeout):
        captured["payload"] = payload
        result = PlanAdjustmentProposal(
            assistant_message="建议只降低第一周强度，把可选练习后移；这份草案尚未应用。",
            user_intent_summary="第一周太紧",
            scope="WEEK",
            affected_stage_orders=[1],
            affected_weeks=[1],
            downstream_affected_weeks=[2],
            proposed_changes=["第一周保留必做基础，把可选练习移到第二周。"],
            tradeoffs=["第二周会略微变紧。"],
            unchanged_guarantees=["一级路线和第三周保持不变。"],
            total_weeks_change=0,
            weekly_intensity_change="LOWER",
            buffer_change="UNCHANGED",
            needs_clarification=False,
            clarifying_question=None,
            can_apply=True,
        )
        return {"choices": [{"message": {"content": result.model_dump_json()}}]}

    settings = Settings(
        llm_provider="openai_compatible",
        llm_api_key="test-key",
        llm_base_url="https://gateway.example/v1",
    )
    provider = OpenAICompatiblePlanDiscussionProvider(
        settings,
        transport=transport,
        sleeper=lambda _: None,
    )

    result = provider.revise(_context())

    assert result.affected_weeks == [1]
    payload = captured["payload"]
    assert isinstance(payload, dict)
    assert payload["max_tokens"] == settings.plan_adjustment_max_output_tokens
    messages = payload["messages"]
    assert isinstance(messages, list)
    prompt_input = json.loads(messages[1]["content"])
    assert prompt_input["user_adjustment_request"] == "为什么第一周这样安排？"


def test_deepseek_adjustment_accepts_fenced_json_without_strict_flag() -> None:
    captured: dict[str, object] = {}
    proposal = PlanAdjustmentProposal(
        assistant_message="建议把计划压缩到两周；这份草案尚未应用。",
        user_intent_summary="把当前计划压缩到两周",
        scope="OVERALL",
        affected_stage_orders=[1],
        affected_weeks=[1, 2, 3],
        downstream_affected_weeks=[],
        proposed_changes=["把三周压缩到两周。"],
        tradeoffs=["每日负担会增加。"],
        unchanged_guarantees=["一级路线不变。"],
        total_weeks_change=-1,
        weekly_intensity_change="HIGHER",
        buffer_change="LESS",
        needs_clarification=False,
        clarifying_question=None,
        can_apply=True,
    )

    def transport(endpoint, headers, payload, timeout):
        captured["payload"] = payload
        text = f"```json\n{proposal.model_dump_json()}\n```"
        return {
            "status": "completed",
            "output": [{"type": "message", "content": [{"type": "output_text", "text": text}]}],
        }

    provider = DeepSeekPlanDiscussionProvider(
        Settings(
            llm_provider="deepseek",
            llm_api_key="test-key",
            llm_base_url="https://api.deepseek.com",
            llm_model="deepseek-v4-flash",
        ),
        transport=transport,
    )
    result = provider.revise(_context())

    assert result.total_weeks_change == -1
    payload = captured["payload"]
    assert isinstance(payload, dict)
    assert "strict" not in payload["text"]["format"]
    assert payload["reasoning"] == {"effort": "none"}


def test_deepseek_apply_reserves_output_budget_for_the_rewritten_plan() -> None:
    captured: dict[str, object] = {}

    def transport(endpoint, headers, payload, timeout):
        captured.update(payload=payload, timeout=timeout)
        raise TimeoutError

    settings = Settings(
        llm_provider="deepseek",
        llm_api_key="test-key",
        llm_base_url="https://api.deepseek.com",
        llm_model="deepseek-v4-flash",
        plan_adjustment_apply_max_retries=0,
    )
    provider = DeepSeekPlanDiscussionProvider(settings, transport=transport)

    try:
        provider.apply(_context())
    except Exception:
        pass

    payload = captured["payload"]
    assert isinstance(payload, dict)
    assert payload["reasoning"] == {"effort": "none"}
    assert payload["max_output_tokens"] == 32000
    assert captured["timeout"] == 360


def test_plan_explanation_prompt_has_non_mutating_boundary() -> None:
    assert PLAN_EXPLANATION_PROMPT_VERSION == "plan-explanation-v11-original-evidence-contract"
    assert "selected_evidence" in PLAN_EXPLANATION_INSTRUCTIONS
    assert "coverage=candidate" in PLAN_EXPLANATION_INSTRUCTIONS
    assert "不负责生成、修改、确认或启动计划" in PLAN_EXPLANATION_INSTRUCTIONS
    assert "建议时长”只是容量估算" in PLAN_EXPLANATION_INSTRUCTIONS
    assert "不声称“已经调整”" in PLAN_EXPLANATION_INSTRUCTIONS


def test_research_answer_uses_configured_reasoning_without_changing_plan_answer():
    efforts = []

    def transport(endpoint, headers, payload, timeout):
        efforts.append(payload["reasoning"]["effort"])
        result = PlanExplanationResult(
            answer="依据完整官方正文，先保留规则的触发条件，再说明适用例外，并给出准确的官方出处。",
            covered_questions=["规则的适用条件是什么？"],
        )
        return {"status": "completed", "output": [
            {"type": "message", "content": [{"type": "output_text", "text": result.model_dump_json()}]}
        ]}

    provider = DeepSeekPlanDiscussionProvider(Settings(
        _env_file=None, llm_provider="deepseek", llm_api_key="test-key", llm_reasoning_effort="low",
    ), transport=transport)
    provider.explain(_context())
    provider.explain(replace(_context(), research_context={"research_status": "PARTIAL"}))

    assert efforts == ["none", "low"]


def test_openai_compatible_provider_applies_only_proposed_weeks() -> None:
    captured: dict[str, object] = {}
    proposal = PlanAdjustmentProposal(
        assistant_message="建议只降低第一周强度；这份草案尚未应用，其他周保持不变。",
        user_intent_summary="第一周太紧",
        scope="WEEK",
        affected_stage_orders=[1],
        affected_weeks=[1],
        downstream_affected_weeks=[],
        proposed_changes=["第一周减少可选任务。"],
        tradeoffs=["覆盖面减少。"],
        unchanged_guarantees=["其他周不变。"],
        total_weeks_change=0,
        weekly_intensity_change="LOWER",
        buffer_change="UNCHANGED",
        needs_clarification=False,
        clarifying_question=None,
        can_apply=True,
    )

    def transport(endpoint, headers, payload, timeout):
        captured["payload"] = payload
        result = PlanAdjustmentApplyResult.model_validate(
            {
                "assistant_message": "第一周已经完成局部重生成，其他周将由系统原样复制。",
                "summary": "三周相对时间安排，重点保持基础练习的连续性。",
                "total_weeks": 3,
                "weekly_execution_days": 6,
                "weekly_minutes": 720,
                "milestones": [
                    {
                        "stage_order": 1,
                        "title": "基础",
                        "purpose": "建立必要基础",
                        "start_week": 1,
                        "end_week": 3,
                        "outputs": ["完成基础练习"],
                    }
                ],
                "regenerated_weeks": [1],
                "replacement_days": [
                    {
                        "stage_order": 1,
                        "week_index": 1,
                        "day_index": 1,
                        "primary_goal": "完成本周最重要的基础练习",
                        "suggested_minutes": 60,
                        "tasks": [
                            {
                                "order": 1,
                                "title": "完成核心练习",
                                "description": "只完成当前最重要的一项练习。",
                                "estimate_minutes": 45,
                                "priority": 1,
                                "task_kind": "PRACTICE",
                                "is_required": True,
                                "evidence": [],
                                "depends_on": [],
                            }
                        ],
                        "evidence": [],
                        "do_not": ["不追加可选内容"],
                    }
                ],
                "buffer_strategy": ["保留十五分钟缓冲"],
                "assumptions": [],
                "open_questions": [],
                "change_summary": ["第一周减少可选任务。"],
            }
        )
        return {"choices": [{"message": {"content": result.model_dump_json()}}]}

    settings = Settings(
        llm_provider="openai_compatible",
        llm_api_key="test-key",
        llm_base_url="https://gateway.example/v1",
        llm_model="test-model",
    )
    provider = OpenAICompatiblePlanDiscussionProvider(
        settings,
        transport=transport,
        sleeper=lambda _: None,
    )

    result = provider.apply(
        replace(_context(), previous_adjustment=proposal.model_dump(mode="json"))
    )

    assert result.regenerated_weeks == [1]
    payload = captured["payload"]
    assert isinstance(payload, dict)
    assert payload["max_tokens"] == settings.plan_adjustment_apply_max_output_tokens
    messages = payload["messages"]
    assert isinstance(messages, list)
    prompt_input = json.loads(messages[1]["content"])
    assert prompt_input["previous_adjustment_proposal"]["affected_weeks"] == [1]


def test_semantic_adjustment_authors_then_compiles_the_confirmed_change() -> None:
    calls = []
    document = "# 总体变化\n前期保持碎片安排，后期每个执行日完成一套模拟卷。"
    compiled = {
        "assistant_message": "已按确认内容准备好第一周的新安排，其余内容保持不变。",
        "summary": "前期碎片推进，后期集中完成整套训练并保留缓冲。",
        "total_weeks": 3,
        "weekly_execution_days": 6,
        "weekly_minutes": 720,
        "milestones": [
            {
                "stage_order": 1,
                "title": "基础",
                "purpose": "建立后续执行需要的基础。",
                "start_week": 1,
                "end_week": 3,
                "outputs": ["基础练习"],
            }
        ],
        "temporal_segments": [
            {
                "label": "全程安排",
                "start_week": 1,
                "end_week": 3,
                "date_scope": "全部三周",
                "capacity_summary": "按各周可用时间推进。",
                "weekly_execution_days": 6,
                "suggested_daily_minutes": 120,
                "frequency_rules": [],
                "rest_rules": [],
                "holiday_rules": [],
            }
        ],
        "commitments": [],
        "regenerated_weeks": [1],
        "replacement_days": [
            {
                "stage_order": 1,
                "week_index": 1,
                "day_index": 1,
                "primary_goal": "完成本周核心基础练习",
                "suggested_minutes": 60,
                "tasks": [
                    {
                        "order": 1,
                        "title": "完成核心练习",
                        "description": "完成当前最重要的一项基础练习。",
                        "estimate_minutes": 45,
                        "priority": 1,
                        "task_kind": "PRACTICE",
                        "is_required": True,
                        "evidence": [],
                        "depends_on": [],
                    }
                ],
                "evidence": [],
                "do_not": [],
            }
        ],
        "buffer_strategy": ["保留未排满时间处理偏差。"],
        "assumptions": [],
        "open_questions": [],
        "change_summary": ["第一周只保留核心练习。"],
    }

    def transport(_endpoint, _headers, payload, _timeout):
        calls.append(payload)
        content = document if len(calls) == 1 else json.dumps(compiled, ensure_ascii=False)
        return {"choices": [{"message": {"content": content}}]}

    raw = OpenAICompatiblePlanDiscussionProvider(
        Settings(
            llm_provider="openai_compatible",
            llm_api_key="test-key",
            llm_base_url="https://gateway.example/v1",
        ),
        transport=transport,
        sleeper=lambda _: None,
    )
    provider = SemanticPlanDiscussionProvider(raw)
    result = provider.apply(replace(_context(), application_requested_weeks=[1]))

    assert len(calls) == 2
    assert "只输出自然语言" in calls[0]["messages"][0]["content"]
    compiler_input = json.loads(calls[1]["messages"][1]["content"])
    assert compiler_input["source_document"] == document
    assert result.source_document == document
    assert result.compiler_report["replacement_day_count"] == 1
