import json
from copy import deepcopy

import pytest

from planner_api.ai import ProviderUnavailableError
from planner_api.config import Settings
from planner_api.context_batches import build_advisory_reads, encoded, fingerprint
from planner_api.generation_orchestration import GenerationOrchestrator, ModelSpecialistProvider


def scenario():
    return {
        "user_request": "前2周少且分散，后2周多且集中；不得删任务",
        "requirements": {
            "phase_capacity": [40, 60, 180, 240],
            "fixed_rules": ["睡眠不得缩短", "周日才可实验"],
        },
        "history": "原始历史证据和例外" * 30,
        "tasks": [
            {
                "id": f"t{i}",
                "title": f"任务{i}",
                "description": "完整说明" * 120,
                "depends_on": ["t0"] if i > 0 else [],
                "estimate_minutes": i + 1,
            }
            for i in range(12)
        ],
    }


def test_every_unit_and_cross_batch_dependency_is_read_with_complete_constraints():
    context = scenario()
    original = deepcopy(context)
    result = build_advisory_reads(context, max_chars=6000, max_batches=8)
    assert result.coverage["status"] == "BATCHED"
    assert result.coverage["input_coverage_complete"] is True
    assert result.coverage["semantic_completeness"] is False
    tasks = []
    for batch in result.inputs:
        assert len(encoded(batch)) <= 6000
        assert batch["requirements"] == context["requirements"]
        assert batch["user_request"] == context["user_request"]
        assert batch["history"] == context["history"]
        tasks.extend(batch["tasks"])
        if any(t["depends_on"] for t in batch["tasks"]):
            assert context["tasks"][0] in batch["tasks"] or any(
                d["value"] == context["tasks"][0] for d in batch["_advisory_dependency_context"]
            )
    assert tasks == context["tasks"]
    assert context == original
    assert result.coverage["batch_hashes"] == [fingerprint(b) for b in result.inputs]


@pytest.mark.parametrize(
    "defect,reason",
    [
        ("missing", "UNRESOLVED_OR_AMBIGUOUS_DEPENDENCY"),
        ("ambiguous", "UNRESOLVED_OR_AMBIGUOUS_DEPENDENCY"),
        ("shape", "UNSUPPORTED_DEPENDENCY_SHAPE"),
        ("hard", "GLOBAL_CONTRACT_OR_ATOMIC_ROW_EXCEEDS_BUDGET"),
        ("limit", "BATCH_LIMIT_EXCEEDED"),
    ],
)
def test_unsafe_splitting_returns_no_partial_coverage(defect, reason):
    context = scenario()
    batches = 8
    if defect == "missing":
        context["tasks"][1]["depends_on"] = ["missing"]
    elif defect == "ambiguous":
        context["tasks"][2]["id"] = "t0"
    elif defect == "shape":
        context["tasks"][1]["depends_on"] = [{"id": "t0"}]
    elif defect == "hard":
        context["requirements"]["long_constraint"] = "不可丢约束" * 4000
    elif defect == "limit":
        batches = 1
    result = build_advisory_reads(context, max_chars=6000, max_batches=batches)
    assert not result.inputs
    assert result.coverage["reason"] == reason
    assert result.coverage["input_coverage_complete"] is False


def test_nested_collections_preserve_parent_phase_metadata_and_cyclic_dependency():
    context = scenario()
    context["tasks"][0]["depends_on"] = ["t1"]
    tasks = context.pop("tasks")
    context["plan"] = {"phase": {"capacity": 77, "tasks": tasks}}
    result = build_advisory_reads(context, max_chars=6500, max_batches=8)
    assert result.inputs
    assert all(b["plan"]["phase"]["capacity"] == 77 for b in result.inputs)
    assert sum(len(b["plan"]["phase"]["tasks"]) for b in result.inputs) == 12


def test_contract_key_collision_does_not_overwrite_original_evidence():
    context = scenario()
    context["_advisory_task_index"] = "用户原文，不得覆盖"
    result = build_advisory_reads(context, max_chars=6000)
    assert not result.inputs
    assert result.coverage["reason"] == "RESERVED_CONTRACT_KEY_COLLISION"


def test_provider_keeps_every_batch_report_and_never_claims_global_feasibility():
    calls = []

    def transport(endpoint, headers, payload, timeout):
        data = json.loads(payload["input"])
        calls.append(data)
        return {
            "output_text": json.dumps(
                {
                    "summary": f"第{len(calls)}批完整原文整理",
                    "risks": [f"独立风险{len(calls)}"],
                    "evidence_ids": [data["context"]["tasks"][0]["id"]],
                }
            )
        }

    settings = Settings(
        _env_file=None,
        llm_provider="deepseek",
        llm_api_key="test",
        specialist_context_max_input_chars=12000,
        specialist_context_max_batches=8,
    )
    context = scenario()
    for t in context["tasks"]:
        t["description"] *= 4
    call = ModelSpecialistProvider(settings, transport=transport).analyze(
        "CONSTRAINT_ANALYST", "FULL_PLAN", context
    )
    assert len(calls) > 1
    assert len(call.batch_reports) == len(calls)
    assert call.coverage["input_coverage_complete"] is True
    assert [r["evidence_ids"] for r in call.batch_reports] == [
        [data["context"]["tasks"][0]["id"]] for data in calls
    ]
    assert sum(len(c["context"]["tasks"]) for c in calls) == 12


def test_failed_batch_is_not_returned_as_complete_and_does_not_retry_whole_huge_input():
    calls = []

    def transport(endpoint, headers, payload, timeout):
        calls.append(payload)
        if len(calls) == 2:
            raise TimeoutError()
        return {"output_text": '{"summary":"第一批已完成只读整理"}'}

    settings = Settings(
        _env_file=None,
        llm_provider="deepseek",
        llm_api_key="test",
        specialist_context_max_input_chars=12000,
        specialist_context_max_batches=8,
        specialist_agent_max_retries=0,
    )
    context = scenario()
    for t in context["tasks"]:
        t["description"] *= 4
    with pytest.raises(ProviderUnavailableError) as failure:
        ModelSpecialistProvider(settings, transport=transport).analyze(
            "CONSTRAINT_ANALYST", "FULL_PLAN", context
        )
    assert len(calls) == 2
    assert failure.value.coverage["completed_batches"] == 1
    assert failure.value.coverage["failed_batch_index"] == 2
    assert failure.value.coverage["input_coverage_complete"] is False


def test_orchestrator_persists_all_batch_reports_and_reuses_only_same_contract(session_factory):
    from planner_api.models import Goal

    calls = []

    def transport(endpoint, headers, payload, timeout):
        calls.append(json.loads(payload["input"]))
        return {"output_text": '{"summary":"已完成该批只读约束整理","risks":["独立跨批风险"]}'}

    settings = Settings(
        _env_file=None,
        llm_provider="deepseek",
        llm_api_key="test",
        specialist_context_max_input_chars=12000,
        specialist_context_max_batches=8,
        multi_agent_max_roles=1,
    )
    context = scenario()
    for t in context["tasks"]:
        t["description"] *= 4
    provider = ModelSpecialistProvider(settings, transport=transport)
    with session_factory() as session:
        goal = Goal(title="前松后紧测试", raw_input=context["user_request"])
        session.add(goal)
        session.commit()
        orchestrator = GenerationOrchestrator(session, provider, settings)
        first = orchestrator.prepare(goal.id, "WORK_ITEM_COMPILATION", context, minimum_tier="L2")
        count = len(calls)
        assert count > 1
        report = first["specialist_reports"][0]
        assert len(report["batch_reports"]) == count
        assert report["input_coverage"]["input_coverage_complete"]
        second = orchestrator.prepare(goal.id, "WORK_ITEM_COMPILATION", context, minimum_tier="L2")
        assert len(calls) == count
        assert second["specialist_reports"][0]["batch_reports"] == report["batch_reports"]
        changed = settings.model_copy(update={"specialist_context_max_batches": 7})
        GenerationOrchestrator(
            session, ModelSpecialistProvider(changed, transport=transport), changed
        ).prepare(goal.id, "WORK_ITEM_COMPILATION", context, minimum_tier="L2")
        assert len(calls) > count


def test_batches_share_total_deadline_instead_of_resetting_timeout(monkeypatch):
    import planner_api.generation_orchestration as module

    clock = [0.0]
    timeouts = []
    monkeypatch.setattr(module.time, "perf_counter", lambda: clock[0])

    def transport(endpoint, headers, payload, timeout):
        timeouts.append(timeout)
        clock[0] += 6
        return {"output_text": '{"summary":"完整保留该批的只读证据"}'}

    settings = Settings(
        _env_file=None,
        llm_provider="deepseek",
        llm_api_key="test",
        specialist_context_max_input_chars=12000,
        specialist_context_max_batches=8,
        specialist_total_timeout_seconds=10,
        specialist_agent_max_retries=0,
    )
    context = scenario()
    for task in context["tasks"]:
        task["description"] *= 4
    with pytest.raises(ProviderUnavailableError) as error:
        ModelSpecialistProvider(settings, transport=transport).analyze(
            "CONSTRAINT_ANALYST", "FULL_PLAN", context
        )
    assert error.value.code == "SPECIALIST_BATCH_DEADLINE"
    assert error.value.coverage["completed_batches"] == 2
    assert error.value.coverage["input_coverage_complete"] is False
    assert timeouts == [10, 4]
