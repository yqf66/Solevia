"""Contract regressions; transport doubles here are not live acceptance evidence."""
import json
from copy import deepcopy
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from planner_api.config import Settings
from planner_api.context_batches import build_advisory_reads
from planner_api.generation_orchestration import GenerationOrchestrator, ModelSpecialistProvider
from planner_api.materials.analysis_ai import RealMaterialAnalysisProvider
from planner_api.materials.analysis_service import MaterialAnalysisService
from planner_api.materials.schemas import MaterialSearchHitRead
from planner_api.models import AgentReport, GenerationRun, Goal, GoalMaterialLink, MaterialRevision, MaterialSource
from planner_api.plan_ai import PlanContext
from planner_api.plan_service import PlanService
from planner_api.work_item_validation import WorkItemValidationError


def settings(**updates):
    return Settings(_env_file=None, llm_provider="deepseek", llm_api_key="unit-only",
                    multi_agent_max_roles=1, llm_max_retries=0,
                    specialist_agent_max_retries=0, **updates)


@pytest.mark.parametrize("change,reason", [
    ("scope", "SCOPE_CHANGED"), ("legacy_scope", "SCOPE_CHANGED"),
    ("capacity", "CONSTRAINTS_CHANGED"), ("progress", "CONTENT_CHANGED"),
    ("source", "CONSTRAINTS_CHANGED"), ("output_budget", "POLICY_CHANGED"),
])
def test_specialist_contract_cache_invalidates_and_diagnostics_do_not_pollute_input(session_factory, change, reason):
    calls = []

    def transport(endpoint, headers, payload, timeout):
        calls.append(json.loads(payload["input"]))
        return {"output_text": '{"summary":"只读核对当前范围与阶段容量"}'}

    config = settings()
    context = {"generation_scope": {"weeks": [1]}, "generated_scope": {"legacy": "keep"},
               "requirements": {"capacity": [60, 60, 480, 480]},
               "requirement_memory": {"source_version": 1},
               "tasks": [{"id": "t1", "completed_minutes": 0}]}
    original = deepcopy(context)
    with session_factory() as session:
        goal = Goal(title="合同验收", raw_input="前松后紧")
        session.add(goal)
        session.commit()
        service = GenerationOrchestrator(session, ModelSpecialistProvider(config, transport=transport), config)
        first = service.prepare(goal.id, "WORK_ITEM_COMPILATION", context, minimum_tier="L2")
        second = service.prepare(goal.id, "WORK_ITEM_COMPILATION", context, minimum_tier="L2")
        assert len(calls) == 1
        assert calls[0]["context"]["generation_scope"] == {"weeks": [1]}
        assert calls[0]["context"]["generated_scope"] == {"legacy": "keep"}
        assert second["specialist_reports"][0]["cache_hit"]
        assert "cache_audit" not in second["decision"]
        assert "_cache_contract" not in second["specialist_reports"][0]
        run = session.get(GenerationRun, second["run_id"])
        assert run.decision_json["cache_audit"]["CONSTRAINT_ANALYST"]["status"] == "HIT"
        changed = deepcopy(context)
        if change == "scope":
            changed["generation_scope"]["weeks"] = [2]
        elif change == "legacy_scope":
            changed["generated_scope"]["legacy"] = "changed"
        elif change == "capacity":
            changed["requirements"]["capacity"][0] = 40
        elif change == "progress":
            changed["tasks"][0]["completed_minutes"] = 10
        elif change == "source":
            changed["requirement_memory"]["source_version"] = 2
        else:
            config = config.model_copy(update={"specialist_agent_max_output_tokens": 1000})
        result = GenerationOrchestrator(session, ModelSpecialistProvider(config, transport=transport), config).prepare(
            goal.id, "WORK_ITEM_COMPILATION", changed, minimum_tier="L2")
        assert len(calls) == 2
        audit = session.get(GenerationRun, result["run_id"]).decision_json["cache_audit"]["CONSTRAINT_ANALYST"]
        assert audit["status"] == "MISS"
        assert reason in audit["reasons"]
        assert context == original
        assert first["specialist_reports"][0]["input_coverage"]["input_coverage_complete"]


def test_unsplit_is_diagnosable_never_cached_and_keeps_original_contract(session_factory):
    config = settings(specialist_context_max_input_chars=12000)
    context = {"generation_scope": {"weeks": [3]}, "requirements": {"original": "完整硬约束" * 4000},
               "tasks": [{"id": "t1", "description": "唯一任务"}]}
    calls = []
    provider = ModelSpecialistProvider(config, transport=lambda *args: calls.append(args))
    with session_factory() as session:
        goal = Goal(title="不可安全分批", raw_input="保留约束")
        session.add(goal)
        session.commit()
        service = GenerationOrchestrator(session, provider, config)
        first = service.prepare(goal.id, "WORK_ITEM_COMPILATION", context, minimum_tier="L2")
        second = service.prepare(goal.id, "WORK_ITEM_COMPILATION", context, minimum_tier="L2")
        assert not calls
        for result in (first, second):
            record = session.scalar(select(AgentReport).where(AgentReport.generation_run_id == result["run_id"]))
            assert record.status == "FAILED"
            coverage = record.report_json["input_coverage"]
            assert not coverage["input_coverage_complete"]
            assert coverage["diagnostics"]["global_over_budget"]
            assert coverage["diagnostics"]["unit_path"] == "/tasks/0"
            assert coverage["diagnostics"]["candidate_chars"] > coverage["max_chars"]
            assert len(coverage["diagnostics"]["unit_hash"]) == 64
            assert "完整硬约束" not in json.dumps(coverage, ensure_ascii=False)
        audit = session.get(GenerationRun, second["run_id"]).decision_json["cache_audit"]["CONSTRAINT_ANALYST"]
        assert "PREVIOUS_REPORT_INCOMPLETE" in audit["reasons"]


def test_atomic_overflow_is_distinguishable_from_global_overflow():
    context = {"requirements": "不得删任务", "tasks": [{"id": "t1", "text": "x" * 8000}]}
    result = build_advisory_reads(context, max_chars=3000)
    assert not result.inputs
    assert not result.coverage["diagnostics"]["global_over_budget"]
    assert result.coverage["diagnostics"]["atomic_unit_chars"] > 8000


def material_fixture(session):
    goal = Goal(title="长问题", raw_input="按资料回答")
    source = MaterialSource(canonical_uri="file:///synthetic-report.md", title="技术报告")
    session.add_all([goal, source])
    session.flush()
    revision = MaterialRevision(source_id=source.id, revision=1, content_hash="synthetic-hash", file_size=100, status="READY")
    session.add_all([revision, GoalMaterialLink(goal_id=goal.id, source_id=source.id)])
    session.commit()
    return goal, source, revision


@pytest.mark.parametrize("unavailable", [False, True])
def test_long_conversation_tail_reaches_query_planner_or_explicitly_reports_no_evidence(session_factory, unavailable):
    question = "以下仅是背景，与最终问题无关。" * 180 + "最后只问测试环境导出配额；排除生产环境，不要把次数当成容量。"
    assert len(question) > 2000
    requests, searches = [], []

    def transport(endpoint, headers, payload, timeout):
        requests.append(json.loads(payload["messages"][1]["content"]))
        if unavailable:
            raise TimeoutError()
        return {"choices": [{"message": {"content": '{"queries":["测试环境 导出配额 排除生产"]}'}}]}

    config = settings(material_rerank_mode="OFF")
    with session_factory() as session:
        goal, source, revision = material_fixture(session)

        def search(goal_id, request):
            searches.append(request.query)
            return [MaterialSearchHitRead(source_id=source.id, revision_id=revision.id, revision=1,
                sequence=0, text="测试环境配额1536 MiB，生产2048 MiB。", score=1, locator="L1")]

        processing = SimpleNamespace(settings=config, semantic_status={"status": "OFF"}, search_goal_materials=search)
        result = MaterialAnalysisService(session, RealMaterialAnalysisProvider(config, transport=transport), processing).prepare_conversation_context(
            goal.id, question, [source.id])
        assert requests[0]["query"] == question
        assert result["query"] == question
        if unavailable:
            assert not searches
            assert not result["passages"]
            assert result["retrieval_failure"]["code"] == "MATERIAL_QUERY_PLAN_INCOMPLETE"
            assert "不能" in result["note"]
        else:
            assert searches == ["测试环境 导出配额 排除生产"]
            assert result["passages"][0]["text"].startswith("测试环境")
            assert result["retrieval_failure"] is None
        link = session.scalar(select(GoalMaterialLink))
        link.status = "REVOKED"
        session.commit()
        before = len(requests)
        with pytest.raises(HTTPException) as error:
            MaterialAnalysisService(session, RealMaterialAnalysisProvider(config, transport=transport), processing).prepare_conversation_context(goal.id, question, [source.id])
        assert error.value.status_code == 422
        assert len(requests) == before


def test_workload_feedback_keeps_structured_expected_actual_and_never_authorizes_capacity_bypass():
    context = PlanContext(requirements={"capacity": [60, 60, 480, 480]}, route={"confirmed": True}, planning_context={})
    detail = {"code": "PLAN_COMMITMENT_MINUTES_INVALID", "json_path": "/scheduling_policy_data/workload_minutes",
              "expected": {"maximum_allocated_minutes": 30}, "actual": {"allocation": {"A": 31}},
              "diagnostic": ["分配超过任务30分钟"]}
    error = WorkItemValidationError(detail["code"], ("分配超限",), detail=detail)
    result = PlanService._business_retry_context(context, "DETAILS", 1, error, "不可改变的用户成果")
    feedback = result.planning_context["generation_validation_feedback"]
    assert feedback["validation_detail"] == detail
    assert len(feedback["invariant_hash"]) == len(feedback["author_hash"]) == 64
    assert not feedback["compile_only_repair"]
    assert result.requirements == context.requirements
    assert result.route == context.route
    assert result.source_document is None


def test_failure_artifact_retains_scope_hashes_and_each_repair_attempt():
    service = PlanService.__new__(PlanService)
    service._generation_artifact_context_hash = lambda batch: "c" * 64
    saved = []

    def checkpoint(batch, phase, **data):
        saved.append(data)
        batch.staged_result_json = {"validation": data["validation"]}

    service._checkpoint_hybrid_artifact = checkpoint
    batch = SimpleNamespace(id="batch-id", plan_version_id="version-id", scope_json={"weeks": [2]},
                            attempt_count=1, staged_result_json={})
    error = HTTPException(422, {"code": "PLAN_WEEK_CAPACITY_INVALID", "diagnostic": ["135 > 85"],
                               "expected": 85, "actual": 135})
    for attempt in range(2):
        service._record_hybrid_validation_failure(batch, attempt, error, "保留全部任务")
    failures = saved[-1]["validation"]["automatic_repair_failures"]
    assert len(failures) == 2
    assert [f["attempt"] for f in failures] == [1, 2]
    assert failures[-1]["scope"] == {"weeks": [2]}
    assert failures[-1]["failure_class"] == "CAPACITY"
    assert failures[-1]["context_hash"] == "c" * 64
    assert failures[-1]["author_hash"] == failures[0]["author_hash"]
    assert failures[-1]["validation_detail"]["actual"] == 135
