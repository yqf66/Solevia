"""Meaningful safety boundaries for mechanical compiler patches."""
from copy import deepcopy
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from planner_api.plan_ai import normalize_compiled_plan_days, normalize_hybrid_contract
from planner_api.plan_compiler_repair import (
    CompilerFieldPatch, apply_bookkeeping_patches, propose_bookkeeping_patches, repair_bookkeeping,
)
from planner_api.plan_service import PlanService
from planner_api.schemas import HybridPlanDetailResult, PlanDayDraft, PlanTaskDraft
from planner_api.work_item_domain import WorkItemDependencyDraft
from planner_api.work_item_validation import WorkItemValidationError, validate_work_item_batch


def scenario(allocation=None):
    task = PlanTaskDraft(order=1, title="形成报告", description="完成报告并留下产物",
        estimate_minutes=30, priority=1, task_kind="BUILD", evidence=["报告"],
        work_item_keys=["REPORT_ITEM"], satisfies_commitments=[],
        scheduling_policy="FLOATING", scheduling_policy_data={"workload_minutes": allocation or {"REPORT_ITEM": 30}})
    result = HybridPlanDetailResult(days=[PlanDayDraft(stage_order=1, week_index=1, day_index=1,
        primary_goal="完成本周报告产物", suggested_minutes=30, tasks=[task], evidence=["报告"])],
        source_document="已确认设计：报告30分钟；不删任务，不改日期和睡眠。")
    outline = {"commitments": [{"label": "完成报告", "workload_key": "REPORT_WORK",
        "minimum_total_minutes": 30, "start_week": 1, "end_week": 1,
        "cadence": "TOTAL", "minimum_occurrences": 1}],
        "standing_work_item_registry": [{"item_key": "REPORT_ITEM", "commitment_refs": ["完成报告"]}],
        "confirmed_capacity": [60, 60, 480, 480]}
    return result, outline


def test_unique_owner_patch_is_atomic_idempotent_and_conserves_authored_work():
    result, outline = scenario()
    original = result.model_dump(mode="json")
    contract = deepcopy(outline)
    repaired = repair_bookkeeping(result, outline)
    task = repaired.days[0].tasks[0]
    assert task.scheduling_policy_data["workload_minutes"] == {"REPORT_WORK": 30}
    assert task.satisfies_commitments == ["完成报告"]
    assert task.estimate_minutes == 30
    assert repaired.source_document == result.source_document
    assert repaired.days[0].stage_order == result.days[0].stage_order
    assert repaired.days[0].suggested_minutes == 30
    assert task.depends_on == result.days[0].tasks[0].depends_on
    assert result.model_dump(mode="json") == original
    assert outline == contract
    assert repaired.compiler_report["bookkeeping_repair"]["model_calls"] == 0
    assert "REQUIRES_FULL" in repaired.compiler_report["bookkeeping_repair"]["status"]
    assert repair_bookkeeping(repaired, outline) is repaired
    assert repair_bookkeeping(result, outline, enabled=False) is result


@pytest.mark.parametrize("value", [20, 30])
def test_saved_failure_numeric_allocation_maps_only_to_single_explicit_workload(value):
    result, outline = scenario()
    result.days[0].tasks[0].scheduling_policy_data["workload_minutes"] = value
    repaired = repair_bookkeeping(result, outline)
    assert repaired.days[0].tasks[0].scheduling_policy_data["workload_minutes"] == {"REPORT_WORK": value}
    assert repaired.days[0].tasks[0].estimate_minutes == 30
    outline["commitments"].append({"label": "另一成果", "workload_key": "OTHER"})
    outline["standing_work_item_registry"][0]["commitment_refs"].append("另一成果")
    assert not propose_bookkeeping_patches(result, outline).patches


@pytest.mark.parametrize("defect", ["ambiguous_owner", "unknown_key", "collision", "over_minutes", "bool_minutes", "unowned_label"])
def test_uncertain_or_semantic_workload_errors_are_never_patched(defect):
    result, outline = scenario()
    task = result.days[0].tasks[0]
    if defect == "ambiguous_owner":
        outline["commitments"].append({"label": "另一成果", "workload_key": "OTHER"})
        outline["standing_work_item_registry"][0]["commitment_refs"].append("另一成果")
    elif defect == "unknown_key":
        task.scheduling_policy_data["workload_minutes"] = {"REPORT_WRONG": 30}
    elif defect == "collision":
        task.scheduling_policy_data["workload_minutes"] = {"REPORT_ITEM": 10, "REPORT_WORK": 20}
    elif defect == "over_minutes":
        task.scheduling_policy_data["workload_minutes"] = {"REPORT_ITEM": 31}
    elif defect == "bool_minutes":
        task.scheduling_policy_data["workload_minutes"] = {"REPORT_ITEM": True}
    else:
        outline["commitments"].append({"label": "他人的报告", "workload_key": "UNOWNED"})
        task.scheduling_policy_data["workload_minutes"] = {"他人的报告": 30}
    original = result.model_dump(mode="json")
    assert not propose_bookkeeping_patches(result, outline).patches
    assert repair_bookkeeping(result, outline).model_dump(mode="json") == original


@pytest.mark.parametrize("defect", ["stale_result", "changed_author", "changed_capacity", "changed_value", "wrong_old_hash", "partial", "new_task", "wrong_proof"])
def test_patch_contract_rejects_stale_and_tampered_requests_without_mutation(defect):
    result, outline = scenario()
    patch = propose_bookkeeping_patches(result, outline)
    if defect == "stale_result":
        result.days[0].tasks[0].estimate_minutes = 25
    elif defect == "changed_author":
        result.source_document = "新设计稿"
    elif defect == "changed_capacity":
        outline["confirmed_capacity"][0] = 40
    elif defect == "changed_value":
        patch.patches[0].value = {"REPORT_WORK": 1}
    elif defect == "wrong_old_hash":
        patch.patches[0].old_value_hash = "0" * 64
    elif defect == "partial":
        patch.patches.pop()
    elif defect == "new_task":
        patch.patches[0].task_reference = "M1-W1-D1-T99"
    else:
        patch.patches[0].proof = "EXPLICIT_MINUTE_OWNERSHIP"
    original = result.model_dump(mode="json")
    with pytest.raises(ValueError, match="CONTRACT_MISMATCH"):
        apply_bookkeeping_patches(result, outline, patch)
    assert result.model_dump(mode="json") == original


def test_patch_schema_disallows_date_estimate_and_arbitrary_json_paths():
    for field in ["estimate_minutes", "day_index", "/days/0/tasks/0/title"]:
        with pytest.raises(ValidationError):
            CompilerFieldPatch(task_reference="M1-W1-D1-T1", field=field,
                old_value_hash="a" * 64, value=1, proof="UNIQUE_REGISTERED_IDENTITY")


def test_work_item_key_case_mapping_requires_unique_registered_identity():
    result, outline = scenario()
    result.days[0].tasks[0].work_item_keys = ["report_item"]
    repaired = repair_bookkeeping(result, outline)
    assert repaired.days[0].tasks[0].work_item_keys == ["REPORT_ITEM"]
    outline["standing_work_item_registry"].append({"item_key": "Report_Item", "commitment_refs": []})
    assert not propose_bookkeeping_patches(result, outline).patches


def test_repaired_bookkeeping_still_rejects_real_capacity_overflow():
    result, outline = scenario()
    repaired = repair_bookkeeping(result, outline)
    plan = SimpleNamespace(schedule_type="OUTCOME", weekly_execution_days=1,
        temporal_segments_json=[{"start_week": 1, "end_week": 1, "weekly_minutes": 20,
                                 "weekly_execution_days": 1, "suggested_daily_minutes": 20}],
        commitments_json=outline["commitments"])
    with pytest.raises(HTTPException) as error:
        PlanService._validate_detail_result(plan, {1: SimpleNamespace(start_week=1, end_week=1)}, [1], repaired.days)
    assert error.value.detail["code"] == "PLAN_TEMPORAL_SEGMENT_MINUTES_INVALID"
    assert repaired.days[0].tasks[0].estimate_minutes == 30
    assert repaired.compiler_report["bookkeeping_repair"]["status"] != "FULL_BUSINESS_VALIDATION_PASSED"


def test_dependency_titles_require_exact_unique_matches_and_keep_ambiguous_evidence():
    result, _ = scenario()
    task = result.days[0].tasks[0]
    task.depends_on = ["阅读", "完成阅读"]
    raw = result.model_dump(mode="json")
    registry = [{"title": "完成阅读报告", "reference": "M1-W1-D1-T2"},
                {"title": "完成阅读", "reference": "M1-W1-D1-T3"},
                {"title": "完成阅读", "reference": "M1-W1-D1-T4"}]
    normalize_compiled_plan_days(raw, "days", registry)
    assert raw["days"][0]["tasks"][0]["depends_on"] == ["阅读", "完成阅读"]
    with pytest.raises(ValidationError, match="canonical"):
        PlanService._validate_week_dependency_targets(HybridPlanDetailResult.model_validate(raw),
            {"generated_task_registry": registry})


def test_canonical_reference_never_redirected_when_exact_target_exists():
    result, _ = scenario()
    result.days[0].tasks[0].depends_on = ["M2-W1-D1-T2"]
    raw = result.model_dump(mode="json")
    normalize_compiled_plan_days(raw, "days", [
        {"title": "另一阶段阅读", "reference": "M1-W1-D1-T2"},
        {"title": "真实阅读", "reference": "M2-W1-D1-T2"}])
    assert raw["days"][0]["tasks"][0]["depends_on"] == ["M2-W1-D1-T2"]


def test_partial_coordinate_does_not_guess_stage_when_registry_is_ambiguous():
    result, _ = scenario()
    result.days[0].tasks[0].depends_on = ["W1-D2-T1"]
    raw = result.model_dump(mode="json")
    normalize_compiled_plan_days(raw, "days", [
        {"title": "阅读", "reference": "M1-W1-D2-T1"},
        {"title": "实验", "reference": "M2-W1-D2-T1"}])
    assert raw["days"][0]["tasks"][0]["depends_on"] == ["W1-D2-T1"]


def test_two_independent_outcomes_keep_explicit_minutes_instead_of_double_counting():
    result, outline = scenario({"完成报告": 20, "另一成果": 10})
    outline["commitments"].append({"label": "另一成果", "workload_key": "OTHER",
        "minimum_total_minutes": 10, "start_week": 1, "end_week": 1, "cadence": "TOTAL"})
    outline["commitments"][0]["minimum_total_minutes"] = 20
    outline["standing_work_item_registry"][0]["commitment_refs"].append("另一成果")
    repaired = repair_bookkeeping(result, outline)
    task = repaired.days[0].tasks[0]
    assert task.scheduling_policy_data["workload_minutes"] == {"REPORT_WORK": 20, "OTHER": 10}
    assert sum(task.scheduling_policy_data["workload_minutes"].values()) == task.estimate_minutes
    PlanService._validate_minimum_commitment_minutes(outline["commitments"], repaired.days)


def test_dependency_edge_dedup_preserves_conditions_lags_sources_and_self_cycles():
    edge = WorkItemDependencyDraft(predecessor_key="SOURCE", successor_key="TARGET",
        minimum_lag_days=1, condition={"approved": True}, source_refs=["确认一"]).model_dump(mode="json")
    duplicate = {**edge, "source_refs": ["确认二"]}
    changed = {**edge, "minimum_lag_days": 2}
    self_edge = {**edge, "successor_key": "SOURCE"}
    raw = {"work_item_dependencies": [edge, duplicate, changed, self_edge]}
    normalize_hybrid_contract(raw)
    assert len(raw["work_item_dependencies"]) == 3
    assert raw["work_item_dependencies"][0]["source_refs"] == ["确认一", "确认二"]
    assert raw["work_item_dependencies"][1]["minimum_lag_days"] == 2
    with pytest.raises(ValidationError, match="itself"):
        WorkItemDependencyDraft.model_validate(raw["work_item_dependencies"][-1])
    with pytest.raises(WorkItemValidationError):
        validate_work_item_batch(work_items=[], groups=[],
            dependencies=[WorkItemDependencyDraft.model_validate(e) for e in raw["work_item_dependencies"][:2]],
            milestone_weeks={}, commitment_labels=set(), existing_item_keys=["SOURCE", "TARGET"])
