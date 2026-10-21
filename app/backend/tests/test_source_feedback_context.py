"""Lossless author-feedback contracts, including distinct historical drafts."""
from copy import deepcopy
import json

import pytest

from planner_api.context_compiler import deduplicate_context_snapshots, deduplicate_source_feedback
from planner_api.plan_ai import PlanContext, _plan_input
from planner_api.plan_discussion_ai import PlanDiscussionContext, _discussion_input
from planner_api.specialist_context import resolve_snapshot_references


def scenario():
    source = ("准备80分钟、实现400分钟、验收120分钟；前松后紧；固定周日，不得移动已完成任务。\n" * 40)
    return {
        "confirmed_requirements": {"capacity": [60, 60, 480, 480]},
        "confirmed_route": {"schedule_type": "OUTCOME"},
        "planning_context": {
            "generation_validation_feedback": {
                "code": "PLAN_WEEK_DEPENDENCY_INVALID", "diagnostic": "前置引用无法定位",
                "previous_source_document": source, "compile_only_repair": True,
            },
            "recent_messages": [
                {"role": "user", "content": "可以加时间"},
                {"role": "user", "content": "纠正：不要增加时间，不要延长期限"},
            ],
            "tasks": [{"id": "a", "progress": 10}, {"id": "b", "progress": 0}],
            "dependencies": [{"predecessor": "a", "successor": "b"}],
        },
        "source_document": source,
    }


@pytest.mark.parametrize("normalizer", [deduplicate_context_snapshots, deduplicate_source_feedback])
def test_same_draft_restores_all_constraints_history_progress_and_dependencies(normalizer):
    original = scenario()
    saved = deepcopy(original)
    wire = normalizer(original)
    assert resolve_snapshot_references(wire) == original == saved
    assert wire["source_document"] == original["source_document"]
    feedback = wire["planning_context"]["generation_validation_feedback"]
    assert feedback["previous_source_document"] == {"$context_ref": "$.source_document"}
    assert feedback["diagnostic"] == "前置引用无法定位"
    assert list(wire).index("source_document") < list(wire).index("planning_context")
    assert list(wire)[:2] == list(original)[:2]  # Keep stable supplier-cache prefix.
    assert len(json.dumps(wire, ensure_ascii=False)) < len(json.dumps(original, ensure_ascii=False))
    assert normalizer(wire) == wire


@pytest.mark.parametrize("change", ["\n", " ", "不许增加任务", "不同版本"])
def test_different_draft_even_whitespace_is_preserved(change):
    payload = scenario()
    payload["planning_context"]["generation_validation_feedback"]["previous_source_document"] += change
    assert deduplicate_context_snapshots(payload) == payload


@pytest.mark.parametrize("value", [None, "", "短稿"])
def test_regeneration_and_small_drafts_keep_full_previous_document(value):
    payload = scenario()
    payload["source_document"] = value
    assert deduplicate_context_snapshots(payload) == payload


def test_only_known_feedback_paths_are_transformed_and_multiple_targets_stay_complete():
    payload = scenario()
    payload["existing_context"] = deepcopy(payload["planning_context"])
    payload["unknown"] = deepcopy(payload["planning_context"])
    payload["planning_context"]["other_feedback"] = deepcopy(
        payload["planning_context"]["generation_validation_feedback"]
    )
    wire = deduplicate_source_feedback(payload)
    assert len(wire["_context_references"]["references"]) == 2
    assert wire["unknown"] == payload["unknown"]
    assert wire["planning_context"]["other_feedback"] == payload["planning_context"]["other_feedback"]
    assert resolve_snapshot_references(wire) == payload


def test_disabled_policy_matches_original_wire_and_does_not_drop_feedback():
    payload = scenario()
    assert deduplicate_source_feedback(payload, enabled=False) == payload
    assert deduplicate_context_snapshots(payload, include_source_feedback=False) == payload


def test_author_anchor_cannot_become_an_unresolved_reference_chain():
    payload = scenario()
    payload["confirmed_requirements"] = payload["source_document"]
    wire = deduplicate_context_snapshots(payload)
    assert wire["source_document"] == payload["source_document"]
    assert resolve_snapshot_references(wire) == payload


@pytest.mark.parametrize("builder", ["plan", "discussion"])
def test_actual_input_builders_use_independent_switch_and_restore_same_contract(builder):
    p = scenario()
    if builder == "plan":
        context = PlanContext(requirements=p["confirmed_requirements"], route=p["confirmed_route"],
                              planning_context=p["planning_context"], source_document=p["source_document"])
        def build(enabled):
            return _plan_input(context, source_feedback_enabled=enabled, generation_step="HYBRID_DETAILS_COMPILER")
    else:
        context = PlanDiscussionContext(requirements=p["confirmed_requirements"], route=p["confirmed_route"],
                                        current_plan={}, user_input="只修正依赖，不改变容量或任务",
                                        planning_context=p["planning_context"], source_document=p["source_document"])
        def build(enabled):
            return _discussion_input(context, "test-version", "apply_request", source_feedback_enabled=enabled)
    off, on = (json.loads(build(enabled)) for enabled in (False, True))
    assert resolve_snapshot_references(on) == resolve_snapshot_references(off)
    assert on["_context_manifest"]["lossy"] is False
    assert on["planning_context"]["recent_messages"][-1]["content"] == "纠正：不要增加时间，不要延长期限"
