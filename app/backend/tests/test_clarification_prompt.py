from planner_api.prompts import (
    CLARIFICATION_INSTRUCTIONS,
    CLARIFICATION_PROMPT_VERSION,
    REQUIREMENT_ANALYSIS_INSTRUCTIONS,
)
from planner_api.schemas import REQUIREMENT_FIELD_KEYS


def test_prompt_contract_names_every_core_field_and_version() -> None:
    assert CLARIFICATION_PROMPT_VERSION == "clarification-v5-natural-memory"
    assert "真实讨论，不是问卷" in CLARIFICATION_INSTRUCTIONS
    assert "用户改变想法时" in CLARIFICATION_INSTRUCTIONS
    assert "滚动摘要" in CLARIFICATION_INSTRUCTIONS


def test_assistant_introduces_its_official_identity() -> None:
    assert "Solevia" in CLARIFICATION_INSTRUCTIONS
    assert "足下" in CLARIFICATION_INSTRUCTIONS
    assert "千里之行，始于足下" in CLARIFICATION_INSTRUCTIONS


def test_prompt_forbids_previous_free_form_keys() -> None:
    assert "不要提字段、Schema" in CLARIFICATION_INSTRUCTIONS
    assert "duration_weeks" not in REQUIREMENT_FIELD_KEYS


def test_prompt_preserves_user_confirmation_boundary() -> None:
    from planner_api.prompts import REQUIREMENT_ANALYSIS_INSTRUCTIONS

    assert "不能代替用户确认" in REQUIREMENT_ANALYSIS_INSTRUCTIONS
    assert "最终由用户主动点击决定" in REQUIREMENT_ANALYSIS_INSTRUCTIONS


def test_prompt_requires_one_field_per_question() -> None:
    assert "通常最多问一个" in CLARIFICATION_INSTRUCTIONS


def test_prompt_distinguishes_outputs_success_and_soft_deadline() -> None:
    from planner_api.prompts import REQUIREMENT_ANALYSIS_INSTRUCTIONS

    assert "success_criteria" in REQUIREMENT_ANALYSIS_INSTRUCTIONS
    assert "明确无期限可写 NONE" in REQUIREMENT_ANALYSIS_INSTRUCTIONS
    assert "不要机械要求六个固定字段全部填写" in REQUIREMENT_ANALYSIS_INSTRUCTIONS


def test_prompt_supports_low_domain_knowledge_across_goal_types() -> None:
    assert "比较选择" in CLARIFICATION_INSTRUCTIONS
    assert "用户不知道时" in CLARIFICATION_INSTRUCTIONS
    assert "低风险默认建议" in CLARIFICATION_INSTRUCTIONS
    assert "医疗、法律、财务及安全事项" in CLARIFICATION_INSTRUCTIONS
    assert "REQUEST_CONFIRMATION" in REQUIREMENT_ANALYSIS_INSTRUCTIONS


def test_analysis_prompt_extracts_multiple_facts_and_preserves_execution_boundaries() -> None:
    assert "一轮可以同时更新多个字段" in REQUIREMENT_ANALYSIS_INSTRUCTIONS
    assert "健身 19:00-20:00 不能动" in REQUIREMENT_ANALYSIS_INSTRUCTIONS
    assert "自己安排" in REQUIREMENT_ANALYSIS_INSTRUCTIONS
    assert "尽量保留用户原词" in REQUIREMENT_ANALYSIS_INSTRUCTIONS
    assert "定性的 available_capacity" in REQUIREMENT_ANALYSIS_INSTRUCTIONS
