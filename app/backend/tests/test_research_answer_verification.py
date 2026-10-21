import json
from dataclasses import replace

import pytest

from planner_api.ai import ProviderUnavailableError
from planner_api.config import Settings
from planner_api.plan_discussion_ai import (
    DeepSeekPlanDiscussionProvider, OpenAICompatiblePlanDiscussionProvider, PlanDiscussionContext,
    _explanation_instructions,
)
from planner_api.plan_explanation_prompts import PLAN_EXPLANATION_INSTRUCTIONS
from planner_api.research.answer_verification import (
    AnswerVerification, CHECK_BATCH_UNITS, answer_units, verification_issues, verify_answer,
)
from planner_api.schemas import PlanExplanationResult


QUESTION = "KeyboardInterrupt 是否被分组？"
ORIGINAL = "KeyboardInterrupt is re-raised instead of an exception group."
EVIDENCE = [{"evidence_id": "e1", "quote": ORIGINAL, "context_before": "", "context_after": "",
             "evidence_level": "FULL_TEXT", "requested_version_matches": True,
             "source_url": "https://docs.python.org/3.14/library/asyncio-task.html"}]
GOOD = PlanExplanationResult(answer="KeyboardInterrupt 会原样抛出，不进入异常组。", covered_questions=[QUESTION])
BAD = PlanExplanationResult(answer="包括 KeyboardInterrupt 在内的所有失败都会进入异常组。", covered_questions=[QUESTION])


def check_for(answer, *, verdict="SUPPORTED", evidence_id="e1"):
    return AnswerVerification(units=[{"unit_id": u["unit_id"], "verdict": verdict,
        "supports": [evidence_id] if verdict == "SUPPORTED" else [],
        "reason": "核对原文"} for u in answer_units(answer)], missing_question_parts=[])


def test_unit_segmentation_keeps_all_verbatim_characters_and_links():
    text = "例外不能省略。\n来源：[官方](https://docs.python.org/3.14/library/asyncio-task.html)"
    units = answer_units(text)
    assert "".join(u["text"] for u in units).replace("\n", "") == text.replace("\n", "")
    assert all(text[u["char_start"]:u["char_end"]] == u["text"] for u in units)
    assert len(units) == 2


def test_unknown_evidence_addresses_are_rejected():
    check = check_for(GOOD.answer, evidence_id="unknown")
    assert verification_issues(check, answer_units(GOOD.answer), EVIDENCE)


@pytest.mark.parametrize("verdict", ["UNSUPPORTED", "CONTRADICTED", "INCOMPLETE", "OUT_OF_SCOPE"])
def test_semantic_failures_never_pass_as_related_material(verdict):
    assert verification_issues(check_for(BAD.answer, verdict=verdict), answer_units(BAD.answer), EVIDENCE)


@pytest.mark.parametrize("field,value", [("evidence_level", "SEARCH_SNIPPET"), ("requested_version_matches", False)])
def test_snippets_and_wrong_versions_cannot_confirm_a_claim(field, value):
    rows = [{**EVIDENCE[0], field: value}]
    assert verification_issues(check_for(GOOD.answer), answer_units(GOOD.answer), rows)


def test_missing_or_duplicate_units_and_unanswered_parts_fail():
    check = check_for(GOOD.answer)
    check.units = []
    assert verification_issues(check, answer_units(GOOD.answer), EVIDENCE)
    check = check_for(GOOD.answer)
    check.units += check.units
    assert verification_issues(check, answer_units(GOOD.answer), EVIDENCE)
    check = check_for(GOOD.answer)
    check.missing_question_parts = [QUESTION]
    assert verification_issues(check, answer_units(GOOD.answer), EVIDENCE)


def test_non_fact_classification_cannot_hide_fabricated_source_links():
    answer = "来源：[冒充官方](https://fake.example.com/rule)"
    assert verification_issues(check_for(answer, verdict="NON_FACT"), answer_units(answer), EVIDENCE)


@pytest.mark.parametrize("answer", [
    "出处（https://docs.python.org/3.14/library/asyncio-task.html）：",
    "出处：https://docs.python.org/3.14/library/asyncio-task.html（Task groups）。",
    "出处：https://docs.python.org/3.14/library/asyncio-task.html。",
])
def test_chinese_punctuation_is_not_mistaken_for_part_of_source_url(answer):
    assert not verification_issues(check_for(answer, verdict="NON_FACT"), answer_units(answer), EVIDENCE)


def test_batches_keep_all_evidence_and_exact_units_without_rewriting():
    result = PlanExplanationResult(answer=GOOD.answer * (CHECK_BATCH_UNITS + 2), covered_questions=[QUESTION])
    seen, coverage = [], []
    def generate(schema, stage, instructions, text):
        assert schema is AnswerVerification
        payload = json.loads(text)
        assert payload["selected_evidence"] == EVIDENCE
        assert payload["full_answer"] == result.answer
        assert len(payload["answer_units"]) <= CHECK_BATCH_UNITS
        coverage.append(payload["coverage_check"])
        seen.extend(payload["answer_units"])
        return AnswerVerification(units=[{"unit_id": u["unit_id"], "verdict": "SUPPORTED",
            "supports": ["e1"], "reason": "原文支持"} for u in payload["answer_units"]], missing_question_parts=[])
    audit = []
    assert verify_answer(QUESTION, {"selected_evidence": EVIDENCE}, result, generate, audit=audit) == result
    assert seen == answer_units(result.answer)
    assert coverage == [True, False]
    assert len(audit[0]["batches"]) == 2


@pytest.mark.parametrize("verdict", ["DECLARED_GAP", "OUT_OF_SCOPE"])
def test_honest_gaps_and_supported_redundancy_warn_without_repair(verdict):
    result = GOOD if verdict == "OUT_OF_SCOPE" else PlanExplanationResult(
        answer="当前材料尚未核实 SQLITE_BUSY 的触发条件，需要补充相应原文。", covered_questions=[QUESTION])
    def generate(schema, stage, instructions, text):
        assert stage == "research_answer_verify"
        check = check_for(result.answer, verdict=verdict)
        if verdict == "OUT_OF_SCOPE":
            check.units[0].supports = ["e1"]
        return check
    audit = []
    assert verify_answer(QUESTION, {"selected_evidence": EVIDENCE}, result, generate, audit=audit) == result
    assert not audit[0]["issues"] and audit[0]["warnings"]


def test_many_units_cannot_inflate_the_original_verification_capacity():
    result = PlanExplanationResult(answer="短句。" * 101, covered_questions=[QUESTION])
    def generate(*args):
        pytest.fail("Oversized answer must be rejected before making model calls")
    with pytest.raises(ProviderUnavailableError):
        verify_answer(QUESTION, {"selected_evidence": EVIDENCE}, result, generate, audit=[])


def test_one_evidence_aware_repair_is_rechecked_before_return():
    calls = []
    def generate(schema, stage, instructions, text):
        calls.append(stage)
        payload = json.loads(text)
        assert payload["selected_evidence"] == EVIDENCE
        if stage == "research_answer_repair":
            assert payload["issues"]
            return GOOD
        answer = "".join(u["text"] for u in payload["answer_units"])
        return check_for(answer, verdict="CONTRADICTED" if answer == BAD.answer else "SUPPORTED")
    audit = []
    result = verify_answer(QUESTION, {"selected_evidence": EVIDENCE}, BAD, generate, audit=audit)
    assert result == GOOD
    assert calls == ["research_answer_verify", "research_answer_repair", "research_answer_verify"]
    assert audit[0]["issues"] and not audit[1]["issues"]


def test_failed_repair_is_bounded_and_draft_is_not_returned():
    calls = []
    def generate(schema, stage, *_args):
        calls.append(stage)
        return BAD if schema is PlanExplanationResult else check_for(BAD.answer, verdict="CONTRADICTED")
    with pytest.raises(ProviderUnavailableError, match="尚未通过") as error:
        verify_answer(QUESTION, {"selected_evidence": EVIDENCE}, BAD, generate, audit=[])
    assert error.value.code == "RESEARCH_ANSWER_UNVERIFIED"
    assert len(calls) == 3


def test_checker_unavailable_never_silently_releases_draft():
    def unavailable(*args):
        raise ProviderUnavailableError("unavailable")
    with pytest.raises(ProviderUnavailableError):
        verify_answer(QUESTION, {"selected_evidence": EVIDENCE}, GOOD, unavailable, audit=[])
    with pytest.raises(ProviderUnavailableError):
        verify_answer(QUESTION, {"selected_evidence": []}, GOOD, unavailable, audit=[])


@pytest.mark.parametrize("provider_cls", [DeepSeekPlanDiscussionProvider, OpenAICompatiblePlanDiscussionProvider])
def test_real_provider_routes_span_answers_through_verifier_and_leaves_plan_flow(provider_cls):
    calls = []
    def transport(endpoint, headers, payload, timeout):
        if "messages" in payload:
            data = json.loads(payload["messages"][1]["content"])
        else:
            data = json.loads(payload["input"])
        calls.append(data)
        output = check_for(GOOD.answer).model_dump_json() if "answer_units" in data else GOOD.model_dump_json()
        if "messages" in payload:
            return {"choices": [{"message": {"content": output}}]}
        return {"status": "completed", "output": [{"type": "message", "content": [{"type": "output_text", "text": output}]}]}
    settings = Settings(llm_api_key="test", llm_base_url="https://example.com/v1", llm_model="test",
                        plan_explanation_max_retries=0, research_answer_verification_mode="ENFORCE")
    provider = provider_cls(settings, transport=transport, sleeper=lambda _: None)
    context = PlanDiscussionContext(requirements={}, route={}, current_plan={}, user_input=QUESTION,
        research_context={"evidence_mode": "SPANS", "selected_evidence": EVIDENCE})
    assert provider.explain(context) == GOOD
    assert len(calls) == 2 and not provider.last_research_verification[-1]["issues"]
    assert provider.explain(replace(context, research_context=None)) == GOOD
    assert len(calls) == 3


def test_experimental_gate_defaults_off_until_real_quality_acceptance():
    assert Settings(_env_file=None).research_answer_verification_mode == "OFF"
    context = PlanDiscussionContext(requirements={}, route={}, current_plan={}, user_input=QUESTION,
        research_context={"evidence_mode": "SPANS", "selected_evidence": EVIDENCE})
    assert _explanation_instructions(context) == PLAN_EXPLANATION_INSTRUCTIONS
    assert _explanation_instructions(context, checked_research=True) != PLAN_EXPLANATION_INSTRUCTIONS

