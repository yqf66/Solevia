"""D-batch contract regressions; transport doubles are not live model evidence."""

from copy import deepcopy
import json

import pytest
from sqlalchemy import select

from planner_api.config import Settings
from planner_api.context_batches import build_advisory_reads, encoded
from planner_api.context_compiler import deduplicate_context_snapshots
from planner_api.generation_orchestration import (
    GenerationOrchestrator,
    ModelSpecialistProvider,
    SPECIALIST_READ_CACHE,
)
from planner_api.models import AgentReport, GenerationRun, Goal
from planner_api.specialist_context import evidence_identities, resolve_snapshot_references
from planner_api.ai import ProviderUnavailableError


def scenario():
    return {
        "generation_scope": {"weeks": [4]},
        "requirements": {"capacity": [40, 60, 180, 240], "sleep": "不得削减睡眠"},
        "precedence_rules": ["最新确认优先，旧值仅作为历史"],
        "requirement_memory": {
            "effective_facts": [
                {"id": "capacity-fact", "field_key": "weekly_capacity", "value": [40, 60, 180, 240]}
            ],
            "change_history": [
                {
                    "field_key": "capacity",
                    "old_value": 180,
                    "new_value": 40,
                    "operation": "REPLACE",
                    "reason": "前松后紧" * 40,
                }
                for _ in range(4)
            ],
        },
        "current_plan": {
            "phases": [
                {
                    "stage_order": 1,
                    "start_week": 1,
                    "end_week": 4,
                    "tasks": [
                        {
                            "id": f"t{i}",
                            "description": "不可省略的任务说明" * 70,
                            "completed_minutes": 0 if i == 0 else 10,
                            "depends_on": [],
                        }
                        for i in range(10)
                    ],
                }
            ],
            "standing_work_item_registry": [
                {"item_key": "A", "constraints": {"deadline": "2035-10-18"}, "body": "前置" * 200},
                {"item_key": "B", "constraints": {"window": "第4周"}, "body": "后置" * 200},
            ],
            "existing_dependency_registry": [
                {
                    "predecessor_key": "A",
                    "successor_key": "B",
                    "dependency_type": "FINISH_TO_START",
                    "minimum_lag_days": 2,
                    "maximum_lag_days": 4,
                    "condition": {"approval": True},
                }
            ],
        },
        "unknown": {"records": [{"details": "未知字段保持原文"}]},
    }


def config(**changes):
    return Settings(
        _env_file=None,
        llm_provider="deepseek",
        llm_api_key="unit-only",
        specialist_context_max_input_chars=8000,
        specialist_context_max_batches=8,
        specialist_batch_max_input_multiplier=8,
        specialist_agent_max_retries=0,
        multi_agent_max_roles=1,
        **changes,
    )


def reports(calls, *, fail_index=None):
    def transport(_endpoint, _headers, payload, _timeout):
        batch = json.loads(payload["input"])["context"]
        calls.append(batch)
        if len(calls) == fail_index:
            raise TimeoutError("isolated test failure")
        return {
            "output_text": json.dumps(
                {"summary": "完整保留当前批次的只读证据", "evidence_ids": ["capacity-fact"]}
            ),
            "usage": {"input_tokens": 100, "output_tokens": 20},
        }

    return transport


def test_typed_nested_phase_graph_and_atomic_history_have_complete_transport():
    context = scenario()
    original = deepcopy(context)
    reads = build_advisory_reads(context, max_chars=4500, max_batches=8)
    assert reads.inputs and reads.coverage["input_coverage_complete"]
    assert reads.coverage["typed_contract"].startswith("specialist-context")
    task_ids = []
    ledgers = []
    for batch in reads.inputs:
        assert len(encoded(batch)) <= 4500
        assert batch["requirements"] == context["requirements"]
        assert (
            batch["requirement_memory"]["effective_facts"]
            == context["requirement_memory"]["effective_facts"]
        )
        assert batch["precedence_rules"] == context["precedence_rules"]
        assert batch["unknown"] == context["unknown"]
        phase = batch["current_plan"]["phases"][0]
        assert (phase["start_week"], phase["end_week"]) == (1, 4)
        task_ids.extend(t["id"] for t in phase["tasks"])
        if batch["requirement_memory"]["change_history"]:
            ledgers.append(batch["requirement_memory"]["change_history"])
        graph = batch["current_plan"]["existing_dependency_registry"]
        assert graph == context["current_plan"]["existing_dependency_registry"]
        items = batch["current_plan"]["standing_work_item_registry"]
        if items:
            assert {item["item_key"] for item in items} == {"A", "B"}
    assert sorted(task_ids) == [f"t{i}" for i in range(10)]
    assert ledgers == [context["requirement_memory"]["change_history"]]
    assert context == original


@pytest.mark.parametrize("problem", ["missing", "ambiguous", "shape"])
def test_invalid_graph_cannot_claim_complete_read(problem):
    context = scenario()
    graph = context["current_plan"]["existing_dependency_registry"]
    if problem == "missing":
        graph[0]["predecessor_key"] = "unknown-item"
    elif problem == "ambiguous":
        context["current_plan"]["standing_work_item_registry"].append(
            {"work_item_key": "A", "different": True}
        )
    else:
        graph[0].pop("successor_key")
    reads = build_advisory_reads(context, max_chars=4500, max_batches=8)
    assert not reads.inputs and not reads.coverage["input_coverage_complete"]
    assert "WORK_ITEM_EDGE" in reads.coverage["reason"]


def test_unknown_lists_are_not_split_even_if_they_embed_task_named_fields():
    context = {"unknown_records": [{"tasks": [{"id": "hidden", "text": "x" * 9000}]}]}
    reads = build_advisory_reads(context, max_chars=4000)
    assert not reads.inputs
    assert reads.coverage["reason"] == "NO_STRUCTURED_COLLECTION"


def test_cost_guard_does_not_call_model_or_claim_success():
    context = scenario()
    calls = []
    provider = ModelSpecialistProvider(
        config().model_copy(update={"specialist_batch_max_input_multiplier": 1}),
        transport=reports(calls),
    )
    with pytest.raises(ProviderUnavailableError) as error:
        provider.analyze("CONSTRAINT_ANALYST", "WORK_ITEM_COMPILATION", context)
    assert error.value.coverage["reason"] == "BATCH_COST_LIMIT_EXCEEDED"
    assert not calls


def test_same_request_exact_references_resolve_but_progress_variants_remain_distinct():
    snapshot = {"tasks": [{"id": "t1", "text": "x" * 300, "completed_minutes": 0}]}
    context = {"plan": snapshot, "current_plan": deepcopy(snapshot)}
    referenced = deduplicate_context_snapshots(context)
    assert len(encoded(referenced)) < len(encoded(context))
    assert resolve_snapshot_references(referenced) == context
    changed = deepcopy(context)
    changed["current_plan"]["tasks"][0]["completed_minutes"] = 10
    assert "_context_references" not in deduplicate_context_snapshots(changed)
    referenced["_context_references"]["references"][0]["target"] = "$.missing"
    reads = build_advisory_reads(referenced, max_chars=3000)
    assert not reads.inputs and "REFERENCE" in reads.coverage["reason"]


def test_unread_identity_index_is_not_citable_evidence():
    context = {"tasks": [{"id": "visible"}], "_advisory_task_index": [["/tasks/42", ["unread"]]]}
    assert "visible" in evidence_identities(context)
    assert "unread" not in evidence_identities(context)


def test_failed_batch_is_retried_without_recalling_successful_batches():
    SPECIALIST_READ_CACHE.clear()
    calls = []
    provider = ModelSpecialistProvider(config(), transport=reports(calls, fail_index=2))
    context = scenario()
    with pytest.raises(ProviderUnavailableError) as error:
        provider.analyze_scoped(
            "CONSTRAINT_ANALYST",
            "WORK_ITEM_COMPILATION",
            context,
            cache_scope={"goal_id": "isolated-test"},
        )
    coverage = error.value.coverage
    assert coverage["completed_batches"] == 1
    assert coverage["consumed_input_units"] == 100 and coverage["provider_calls"] == 2
    assert len(coverage["completed_reports"]) == 1
    successful_body = calls[0]
    result = provider.analyze_scoped(
        "CONSTRAINT_ANALYST",
        "WORK_ITEM_COMPILATION",
        context,
        cache_scope={"goal_id": "isolated-test"},
    )
    assert result.coverage["cached_batches"] == 1
    assert not any(batch == successful_body for batch in calls[2:])
    count = len(calls)
    repeated = provider.analyze_scoped(
        "CONSTRAINT_ANALYST",
        "WORK_ITEM_COMPILATION",
        context,
        cache_scope={"goal_id": "isolated-test"},
    )
    assert len(calls) == count
    assert repeated.coverage["cached_batches"] == repeated.coverage["batch_count"]
    assert repeated.input_units == repeated.output_units == repeated.coverage["provider_calls"] == 0
    assert repeated.batch_reports == result.batch_reports


@pytest.mark.parametrize(
    "change",
    [
        "goal",
        "database",
        "scope",
        "capacity",
        "progress",
        "source",
        "edge",
        "output",
        "reasoning",
        "role",
        "credentials",
        "proxy",
    ],
)
def test_batch_cache_invalidates_for_every_relevant_contract_change(change):
    SPECIALIST_READ_CACHE.clear()
    context = {
        "generation_scope": {"weeks": [2]},
        "requirements": {"capacity": 40},
        "tasks": [{"id": "t1", "completed_minutes": 0}],
        "sources": [{"id": "source1", "revision": 1}],
        "dependency_contract": {"minimum_lag_days": 2},
    }
    calls = []

    def transport(*_args):
        calls.append(1)
        return {"output_text": '{"summary":"完整核对本批合同与证据"}'}

    provider = ModelSpecialistProvider(config(), transport=transport)
    scope = {"goal_id": "G1", "database": "D1"}
    role = "CONSTRAINT_ANALYST"
    provider.analyze_scoped(role, "WORK_ITEM_COMPILATION", context, cache_scope=scope)
    provider.analyze_scoped(role, "WORK_ITEM_COMPILATION", context, cache_scope=scope)
    assert len(calls) == 1
    if change in {"goal", "database"}:
        scope["goal_id" if change == "goal" else "database"] += "changed"
    elif change == "scope":
        context["generation_scope"]["weeks"] = [3]
    elif change == "capacity":
        context["requirements"]["capacity"] = 60
    elif change == "progress":
        context["tasks"][0]["completed_minutes"] = 10
    elif change == "source":
        context["sources"][0]["revision"] = 2
    elif change == "edge":
        context["dependency_contract"]["minimum_lag_days"] = 3
    elif change == "output":
        provider.settings = provider.settings.model_copy(
            update={"specialist_agent_max_output_tokens": 1000}
        )
    elif change == "reasoning":
        provider.settings = provider.settings.model_copy(
            update={"specialist_agent_reasoning_effort": "low"}
        )
    elif change == "role":
        role = "LOAD_BALANCER"
    elif change == "credentials":
        provider.settings = provider.settings.model_copy(
            update={"llm_api_key": config().llm_api_key.__class__("other-test-key")}
        )
    else:
        provider.settings = provider.settings.model_copy(
            update={"llm_proxy_url": "http://localhost:99"}
        )
    provider.analyze_scoped(role, "WORK_ITEM_COMPILATION", context, cache_scope=scope)
    assert len(calls) == 2


@pytest.mark.parametrize("failure", ["truncated", "invented_evidence", "malformed"])
def test_invalid_reports_are_never_cached_even_when_partial_json_parses(failure):
    calls = []

    def transport(*_args):
        calls.append(1)
        if failure == "truncated":
            return {"output_text": '{"summary":"合法JSON也不代表完整完成"}', "status": "incomplete"}
        if failure == "invented_evidence":
            return {
                "output_text": '{"summary":"引用对象并未提供在本批里","evidence_ids":["invented"]}'
            }
        return {"output_text": '{"summary":'}

    provider = ModelSpecialistProvider(config(), transport=transport)
    for _ in range(2):
        with pytest.raises(ProviderUnavailableError):
            provider.analyze_scoped(
                "CONSTRAINT_ANALYST",
                "FULL_PLAN",
                {"tasks": [{"id": "t1"}]},
                cache_scope={"goal_id": "invalid-report-test"},
            )
    assert len(calls) == 2


def test_failed_whole_report_preserves_successful_cost_and_unknown_fields(session_factory):
    context = scenario()
    context["new_unknown_contract"] = {"must_preserve": "未知约束也保留"}
    calls = []
    settings = config()
    provider = ModelSpecialistProvider(settings, transport=reports(calls, fail_index=2))
    with session_factory() as session:
        goal = Goal(title="独立分批失败", raw_input="保留全部任务")
        session.add(goal)
        session.commit()
        result = GenerationOrchestrator(session, provider, settings).prepare(
            goal.id, "WORK_ITEM_COMPILATION", context, minimum_tier="L2"
        )
        assert all(c["new_unknown_contract"] == context["new_unknown_contract"] for c in calls)
        report = session.scalar(
            select(AgentReport).where(AgentReport.generation_run_id == result["run_id"])
        )
        run = session.get(GenerationRun, result["run_id"])
        assert report.status == "FAILED" and run.status == "DEGRADED"
        assert report.input_units == run.input_units == 100
        assert report.report_json["input_coverage"]["input_coverage_complete"] is False
        assert len(report.report_json["input_coverage"]["completed_reports"]) == 1


def test_roles_share_one_deadline_instead_of_resetting_budget(session_factory, monkeypatch):
    import planner_api.generation_orchestration as module

    clock, timeouts = [0.0], []
    monkeypatch.setattr(module.time, "perf_counter", lambda: clock[0])

    def transport(_endpoint, _headers, _payload, timeout):
        timeouts.append(timeout)
        clock[0] += min(35, timeout)
        if timeout < 35:
            raise TimeoutError()
        return {"output_text": '{"summary":"已完整读完本角色的证据"}'}

    settings = config().model_copy(
        update={"specialist_total_timeout_seconds": 60, "multi_agent_max_roles": 3}
    )
    with session_factory() as session:
        goal = Goal(title="共享等待预算", raw_input="恢复规划")
        session.add(goal)
        session.commit()
        result = GenerationOrchestrator(
            session, ModelSpecialistProvider(settings, transport=transport), settings
        ).prepare(
            goal.id,
            "RECOVERY_PLANNING",
            {"tasks": [{"id": "t1"}], "requirements": {"capacity": 85}},
            minimum_tier="L3",
        )
        assert timeouts == [60, 25]
        assert len(result["decision"]["degraded_roles"]) == 2
        failed = [report for report in result["specialist_reports"] if report.get("degraded")]
        assert all(not report["input_coverage"]["input_coverage_complete"] for report in failed)


def test_evidence_hint_uses_only_full_body_and_does_not_turn_index_into_evidence():
    context = {
        "tasks": [{"id": "present-task", "completed_minutes": 22}],
        "_advisory_task_index": [["/tasks/99", ["unread-task"]]],
        "requirements": {"remaining_minutes": 0, "target_value": 80},
    }
    hint = ModelSpecialistProvider._evidence_hint(context)
    assert hint["allowed_id_examples"] == ["present-task"]
    assert hint["unread_index_is_evidence"] is False
    # Callers cannot inject reserved advisory metadata; it is generated by the reader.
    context.pop("_advisory_task_index")
    calls = []

    def transport(_endpoint, _headers, payload, _timeout):
        body = json.loads(payload["input"])
        calls.append(body)
        return {"output_text": '{"summary":"已安排不等于执行完成","evidence_ids":["present-task"]}'}

    provider = ModelSpecialistProvider(config(), transport=transport)
    provider.analyze("CONSTRAINT_ANALYST", "FULL_PLAN", context)
    assert calls[0]["evidence_contract"] == hint
    assert calls[0]["context"]["requirements"] == context["requirements"]
    assert calls[0]["context"]["tasks"][0]["completed_minutes"] == 22


def test_arithmetic_hints_detect_old_buffer_error_without_changing_source():
    from planner_api.specialist_context import arithmetic_observations

    context = {
        "current_plan": {
            "summary": "旧助手写后期缓冲240分钟",
            "commitments": [
                {
                    "cadence": "TOTAL",
                    "minimum_total_minutes": minutes,
                    "start_week": start,
                    "end_week": end,
                }
                for minutes, start, end in [(80, 1, 2), (400, 3, 4), (120, 4, 4)]
            ],
            "temporal_segments": [
                {
                    "start_week": 3,
                    "end_week": 4,
                    "available_days_per_week": 4,
                    "suggested_daily_minutes": 120,
                }
            ],
        }
    }
    original = deepcopy(context)
    hints = arithmetic_observations(context)
    assert "80+400+120=600" in hints[0]
    assert "960" in hints[1] and "demand=520" in hints[1] and "difference=440" in hints[1]
    assert context == original
    context["current_plan"]["temporal_segments"][0]["weekly_capacity_overrides"] = [60]
    assert len(arithmetic_observations(context)) == 1
    context["current_plan"]["commitments"][0]["cadence"] = "PER_WEEK"
    assert arithmetic_observations(context) == []


def test_derived_buffer_conflict_must_be_reported_not_cached_silently():
    from planner_api.specialist_context import derived_buffer_conflicts
    from planner_api.generation_orchestration import SpecialistReportResult

    context = {
        "current_plan": {
            "commitments": [
                {"cadence": "TOTAL", "minimum_total_minutes": 520, "start_week": 3, "end_week": 4}
            ],
            "temporal_segments": [
                {
                    "start_week": 3,
                    "end_week": 4,
                    "available_days_per_week": 4,
                    "suggested_daily_minutes": 120,
                    "capacity_summary": "两周共960分钟；约240分钟仅作缓冲不排任务",
                }
            ],
        }
    }
    assert derived_buffer_conflicts(context)[0]["arithmetic_difference"] == 440
    with pytest.raises(ValueError, match="ARITHMETIC_SOURCE_CONFLICT"):
        ModelSpecialistProvider._validate_evidence(
            SpecialistReportResult(summary="照搬旧缓冲数", constraints=["富余240分钟"]), context
        )
    ModelSpecialistProvider._validate_evidence(
        SpecialistReportResult(
            summary="只读算术核对", risks=["旧缓冲240与算术差440口径有差异，非可用时间授权"]
        ),
        context,
    )
    # An explicit reserve preference alone is not a derived arithmetic claim.
    context["current_plan"]["temporal_segments"][0]["capacity_summary"] = "希望保留240分钟缓冲"
    assert derived_buffer_conflicts(context) == []
    context["current_plan"]["temporal_segments"] = None
    assert derived_buffer_conflicts(context) == []
