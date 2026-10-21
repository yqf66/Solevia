import json

import pytest
from pydantic import SecretStr

from planner_api.ai import (ClarificationContext, DeepSeekClarificationProvider,
                            OpenAICompatibleClarificationProvider, ProviderUnavailableError,
                            _research_reply_instructions)
from planner_api.config import Settings
from planner_api.research.scope_guard import (AnswerScopeReview, guard_answer_scope,
    scope_author_instructions, scope_units)


PACK = {"evidence_mode": "SPANS", "research_question": "哪个合适？", "research_status": "PARTIAL",
        "selected_evidence": [{"evidence_id": "e1", "evidence_level": "FULL_TEXT",
        "quote": "测评者报告：这一款机器在这个房间干衣约6小时。", "source_url": "https://example.com/test"}]}


def test_rejection_only_removes_verbatim_units_without_reauthoring_remaining_evidence():
    answer = "测评者说这款约6小时。[测评](https://example.com/test)\n\n所以除湿机都能6小时干衣。\n\n缺少多人使用经验。"
    audit = []

    def review(text):
        payload = json.loads(text)
        units = payload["answer_units"]
        assert payload["selected_evidence"] == PACK["selected_evidence"]
        return AnswerScopeReview(checked_unit_ids=[u["unit_id"] for u in units],
            rejected=[{"unit_id": u["unit_id"], "reason": "单款扩大为品类"}
                      for u in units if "所以" in u["text"]])

    result = guard_answer_scope("哪个合适？", PACK, answer, review, audit=audit)
    assert "所以除湿机" not in result
    assert "[测评](https://example.com/test)" in result and "缺少多人使用经验。" in result
    assert "仍需核实" in result and len(audit) == 1


@pytest.mark.parametrize("kind", ["missing", "duplicate", "unknown", "failure"])
def test_incomplete_or_failed_review_never_releases_unchecked_draft(kind):
    def review(_):
        if kind == "failure":
            raise TimeoutError()
        return AnswerScopeReview(checked_unit_ids=[] if kind == "missing" else ["u1", "u1"] if kind == "duplicate" else ["u1"],
            rejected=[{"unit_id": "unknown", "reason": "bad"}] if kind == "unknown" else [])
    with pytest.raises(ProviderUnavailableError) as error:
        guard_answer_scope("q", PACK, "确定有效。", review, audit=[])
    assert error.value.code == "RESEARCH_SCOPE_UNVERIFIED"


@pytest.mark.parametrize("provider_class", [DeepSeekClarificationProvider, OpenAICompatibleClarificationProvider])
@pytest.mark.parametrize("enabled", [False, True])
def test_provider_integration_is_independent_and_switchable(provider_class, enabled):
    calls = []

    def transport(_url, _headers, payload, _timeout):
        calls.append(payload)
        is_review = "checked_unit_ids" in json.dumps(payload)
        if is_review:
            body = json.loads(payload.get("input") or payload["messages"][-1]["content"])
            output = json.dumps({"checked_unit_ids": [u["unit_id"] for u in body["answer_units"]],
                "rejected": [{"unit_id": "u1", "reason": "单款扩大为普遍保证"}]})
        else:
            output = json.dumps({"assistant_message": "所有除湿机都能6小时干衣。"})
        if "messages" in payload:
            return {"choices": [{"message": {"content": output}, "finish_reason": "stop"}]}
        return {"status": "completed", "output_text": output}

    settings = Settings(_env_file=None, llm_api_key=SecretStr("test"),
                        research_answer_scope_guard_enabled=enabled, research_answer_verification_mode="OFF")
    provider = provider_class(settings, transport=transport)
    reply = provider.respond(ClarificationContext(raw_input="哪个合适？", latest_user_message="哪个合适？",
        active_facts={}, answer_for_keys=[], research_context=PACK))
    assert len(calls) == (2 if enabled else 1)
    assert ("所有除湿机" not in reply.assistant_message) == enabled


def test_supported_answer_and_nonresearch_are_unchanged():
    answer = "这款测评报告约6小时，其他环境未核实。"
    def review(_):
        return AnswerScopeReview(checked_unit_ids=[u["unit_id"] for u in scope_units(answer)], rejected=[])
    assert guard_answer_scope("q", PACK, answer, review, audit=[]) == answer
    assert guard_answer_scope("q", None, answer, lambda _: pytest.fail("unnecessary check"), audit=[]) == answer


def test_paragraphs_preserve_markdown_links_and_multiline_lists():
    answer = "**测评个例**\n- 型号 A：6小时。\n- [原文](https://example.com/test)\n\n**缺口**\n缺少其他环境数据。"
    assert "".join(u["text"] for u in scope_units(answer)) == answer
    assert len(scope_units(answer)) == 2
    assert scope_author_instructions("base", None) == "base"
    assert "单一用户/单款" in scope_author_instructions("base", PACK)


def test_original_evidence_answer_contract_applies_even_when_extra_review_is_off():
    from dataclasses import replace
    from planner_api.prompts import CLARIFICATION_INSTRUCTIONS
    context = ClarificationContext(raw_input="q", latest_user_message="q", active_facts={},
        answer_for_keys=[], research_context=PACK)
    prompt = _research_reply_instructions(None, context)
    assert "单一用户/单款" in prompt and "此前助手说法" in prompt
    assert _research_reply_instructions(None, replace(context, research_context=None)) == CLARIFICATION_INSTRUCTIONS


@pytest.mark.parametrize("name", ["DeepSeekPlanDiscussionProvider", "OpenAICompatiblePlanDiscussionProvider"])
@pytest.mark.parametrize("enabled", [False, True])
def test_plan_explanation_scope_guard_preserves_other_fields_and_full_verifier_off(name, enabled):
    from dataclasses import replace
    from planner_api import plan_discussion_ai
    from planner_api.schemas import PlanExplanationResult
    from test_plan_explanation_provider import _context
    provider = getattr(plan_discussion_ai, name)(Settings(_env_file=None, llm_api_key="test",
        research_answer_scope_guard_enabled=enabled, research_answer_verification_mode="OFF"))
    calls = []
    def generate(schema, *args):
        calls.append((schema, args))
        if schema is AnswerScopeReview:
            return AnswerScopeReview(checked_unit_ids=["u1"],
                rejected=[{"unit_id": "u1", "reason": "个例扩大"}])
        return PlanExplanationResult(answer="所有型号都只需6小时，并且在各种环境都保证有效。", covered_questions=["多久？"])
    provider._generate = generate
    result = provider.explain(replace(_context(), research_context=PACK))
    assert len(calls) == (2 if enabled else 1)
    assert ("所有型号" not in result.answer) == enabled
    assert result.covered_questions == ["多久？"]
    assert result.needs_clarification is False
