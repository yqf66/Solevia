import json
from copy import deepcopy

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from planner_api.ai import ProviderUnavailableError
from planner_api.config import Settings
from planner_api.context_compiler import ContextCompiler
from planner_api.database import Base
from planner_api.generation_orchestration import (
    GenerationOrchestrator, ModelSpecialistProvider, SpecialistCall, SpecialistReportResult,
)
from planner_api.materials.analysis_ai import RealMaterialAnalysisProvider, MaterialQueryPlan
from planner_api.models import AgentReport, GenerationRun, Goal
from planner_api.plan_ai import PlanContext, _plan_input
from planner_api.structured_facts import closed_calendar_facts, structural_facts


@pytest.mark.parametrize("count", [45, 100])
def test_all_fixed_rules_and_exceptions_reach_model(count):
    payload = {"schedule_context": {
        "fixed_occupancy_rules": [{"id": f"r{i}", "minutes": i} for i in range(count)],
        "fixed_occupancy_overrides": [{"id": f"o{i}", "minutes": 5} for i in range(75)],
        "date_exceptions": [{"id": f"e{i}", "minutes": 20} for i in range(75)],
    }}
    result = ContextCompiler(max_chars=1000).compile(payload, task_type="EXECUTION_CALENDAR")
    assert result.payload["schedule_context"] == payload["schedule_context"]
    assert result.manifest["lossy"] is False
    assert result.manifest["budget_exceeded"] is True


def test_middle_facts_user_conditions_tasks_and_dependency_evidence_survive_budget():
    raw = "背景。" * 5000 + "仅周日训练，前两周60分钟，后三周480分钟。" + "背景。" * 5000
    payload = {
        "latest_user_message": raw,
        "requirement_memory": {"effective_facts": [
            {"id": f"f{i}", "value": "约束" * 40, "source_refs": [f"m{i}"]}
            for i in range(1200)
        ]},
        "tasks": [{"id": f"t{i}", "estimate_minutes": 10,
                   "dependencies": [f"t{i-1}"] if i else [],
                   "description": "必须完成独立成果" * 500, "evidence": [f"m{i}"]}
                  for i in range(8)],
    }
    before = deepcopy(payload)
    result = ContextCompiler(max_chars=1000, max_user_text_chars=500).compile(
        payload, task_type="PLAN_ADJUSTMENT"
    )
    assert payload == before
    assert {k: v for k, v in result.payload.items() if k != "_context_manifest"} == before
    assert result.payload["tasks"][4]["dependencies"] == ["t3"]
    assert result.manifest["omissions"] == []


@pytest.mark.parametrize("user_text", ["第1周到第35周全部核对", "W1-W35", "第35周至第1周", "第1–35周", "第1至第35周"])
def test_entire_week_interval_and_cross_interval_dependencies_are_preserved(user_text):
    payload = {"planning_context": {"user_request": user_text}, "days": [
        {"week_index": week, "tasks": [{"id": f"t{week}", "dependencies": ["t40"]}]}
        for week in range(1, 41)
    ]}
    result = ContextCompiler(max_chars=1000).compile(payload, task_type="PLAN_ADJUSTMENT")
    assert result.manifest["requested_weeks"] == list(range(1, 36))
    assert result.payload["days"] == payload["days"]


def test_recompilation_keeps_upstream_provenance_without_recursive_manifests():
    first = ContextCompiler().compile({"user_request": "全部保留"}, task_type="A")
    second = ContextCompiler().compile(first.payload, task_type="B")
    assert second.manifest["upstream_source_hash"] == first.manifest["source_hash"]
    assert second.manifest["upstream_lossy"] is False
    assert json.dumps(second.manifest).count("compiler_version") == 1


def test_recompilation_does_not_claim_old_lossy_input_is_complete():
    payload = {"facts": ["already incomplete"],
               "_context_manifest": {"compiler_version": "context-v1", "source_hash": "old"}}
    result = ContextCompiler().compile(payload, task_type="SECOND_STAGE")
    assert result.manifest["upstream_lossy"] is True
    assert result.manifest["lossy"] is True


def test_plan_provider_input_keeps_phased_capacity_and_complete_task_contract():
    requirements = {"phase_capacity": [60, 60, 480, 480], "deadline": "2026-12-31",
                    "facts": [{"id": f"f{i}", "value": "必须保留" * 40} for i in range(500)]}
    tasks = [{"id": f"t{i}", "minutes": 50, "dependencies": ["t0"] if i else [],
              "source_refs": [f"m{i}"], "description": "必须逐项验收" * 500} for i in range(8)]
    result = json.loads(_plan_input(
        PlanContext(requirements, {"type": "HYBRID"}, {"tasks": tasks}),
        requested_weeks={"start": 1, "end": 35},
    ))
    assert result["confirmed_requirements"] == requirements
    assert result["planning_context"]["tasks"] == tasks
    assert result["_context_manifest"]["requested_weeks"] == list(range(1, 36))
    assert result["_context_manifest"]["lossy"] is False


def test_fact_inventory_counts_all_rows_and_invalidates_middle_change():
    payload = {"requirements": {"phases": [60, 60, 480]}, "days": [
        {"tasks": [{"id": f"t{i}", "minutes": 20} for i in range(8)]}
    ]}
    result = structural_facts(payload)
    assert result["counts"]["tasks"] == 8
    assert result["semantic_inference"] is False
    payload["days"][0]["tasks"][4]["minutes"] = 30
    assert structural_facts(payload)["sections"]["days"]["sha256"] != result["sections"]["days"]["sha256"]


@pytest.mark.parametrize("extra", [
    {"user_request": "不是改期限，只解释"},
    {"requirements": {"deadline": "2026-10-31"}},
    {"tasks": [{"dependencies": ["outside"]}]},
    {"unknown_field": "硬约束"},
])
def test_numeric_calendar_gate_never_skips_free_text_or_unknown_contract(extra):
    context = {"calendar_summary": {"task_count": 300}, **extra}
    assert not closed_calendar_facts(context, "CALENDAR_SPECIALIST")
    assert not closed_calendar_facts(context, "CONTEXT_CURATOR")


class ObservingSpecialist:
    def __init__(self, callback=lambda: None):
        self.calls = 0
        self.callback = callback

    def analyze(self, *_):
        self.calls += 1
        self.callback()
        return SpecialistCall(SpecialistReportResult(summary="完整合同只读分析完成"), 10, 5, 1)


def test_provider_wait_holds_no_transaction_and_does_not_overwrite_concurrent_change(tmp_path):
    engine = create_engine(f"sqlite:///{(tmp_path / 'concurrent.db').as_posix()}",
                          connect_args={"timeout": 0.2})
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine)
    with factory() as session:
        goal = Goal(title="原始标题", raw_input="完整规划")
        session.add(goal)
        session.commit()
        goal_id = goal.id

        def write_during_wait():
            assert not session.in_transaction()
            with factory() as other:
                other.get(Goal, goal_id).title = "另一操作已保存"
                other.commit()

        provider = ObservingSpecialist(write_during_wait)
        result = GenerationOrchestrator(session, provider, Settings(_env_file=None)).prepare(
            goal_id, "FULL_PLAN", {"user_request": "保留已完成内容"}, minimum_tier="L3"
        )
        assert provider.calls == 2
        assert session.get(Goal, goal_id).title == "另一操作已保存"
        assert session.get(GenerationRun, result["run_id"]).status == "CONTEXT_READY"
    engine.dispose()


def test_late_specialist_response_cannot_resurrect_invalidated_run(session_factory):
    with session_factory() as session:
        goal = Goal(title="失效测试", raw_input="规划")
        session.add(goal)
        session.commit()
        goal_id = goal.id

        def invalidate():
            assert not session.in_transaction()
            with session_factory() as other:
                run = other.scalar(select(GenerationRun).where(GenerationRun.goal_id == goal_id))
                run.status = "FAILED"
                other.commit()

        with pytest.raises(ProviderUnavailableError) as error:
            GenerationOrchestrator(session, ObservingSpecialist(invalidate),
                                   Settings(_env_file=None)).prepare(
                goal_id, "FULL_PLAN", {"user_request": "规划"}, minimum_tier="L2"
            )
        assert error.value.code == "SPECIALIST_RUN_SUPERSEDED"
        assert session.scalar(select(GenerationRun)).status == "FAILED"
        assert session.scalar(select(AgentReport)).status == "DISCARDED"


def test_endpoint_change_invalidates_cached_semantic_report(session_factory):
    with session_factory() as session:
        goal = Goal(title="缓存身份", raw_input="规划")
        session.add(goal)
        session.commit()
        provider = ObservingSpecialist()
        for endpoint in ["https://a.example/v1", "https://a.example/v1/", "https://b.example/v1"]:
            settings = Settings(_env_file=None, llm_base_url=endpoint)
            GenerationOrchestrator(session, provider, settings).prepare(
                goal.id, "FULL_PLAN", {"user_request": "不改变任务"}, minimum_tier="L2"
            )
        assert provider.calls == 2


def test_closed_numeric_report_skips_remote_call_but_toggle_invalidates_cache(session_factory):
    with session_factory() as session:
        goal = Goal(title="数值日历", raw_input="规划")
        session.add(goal)
        session.commit()
        context = {"calendar_summary": {"task_count": 300, "execution_day_count": 92}}
        settings = Settings(_env_file=None, llm_provider="deepseek", llm_api_key="test")
        provider = ModelSpecialistProvider(settings)
        observer = ObservingSpecialist()
        provider.analyze = observer.analyze
        first = GenerationOrchestrator(session, provider, settings).prepare(
            goal.id, "DATE_SCHEDULING", context, minimum_tier="L2"
        )
        assert observer.calls == 0
        assert first["specialist_reports"][0]["origin"] == "PROGRAM_CLOSED_CALENDAR_FACTS"
        settings = settings.model_copy(update={"specialist_program_facts_enabled": False})
        GenerationOrchestrator(session, provider, settings).prepare(
            goal.id, "DATE_SCHEDULING", context, minimum_tier="L2"
        )
        assert observer.calls == 1


@pytest.mark.parametrize("query", ["flash_attention", "Section 4.2", "表 3"])
def test_literal_material_query_needs_no_model_rewrite(query):
    provider = RealMaterialAnalysisProvider(Settings(_env_file=None, llm_api_key="test"))
    provider._request = lambda *_: pytest.fail("literal locator unnecessarily called model")
    assert provider.plan_queries(query) == [query]


@pytest.mark.parametrize("query", ["不只看速度，还要比较显存和反例", "flash_attention 有什么限制？", "Section 4.2 与第5节有冲突吗"])
def test_semantic_material_query_keeps_model_decomposition(query):
    provider = RealMaterialAnalysisProvider(Settings(_env_file=None, llm_api_key="test"))
    calls = []
    def request(*args, **kwargs):
        assert kwargs == {'query_planning': True}
        calls.append(args)
        return MaterialQueryPlan(queries=["first", "second"])
    provider._request = request
    assert provider.plan_queries(query) == ["first", "second"]
    assert len(calls) == 1
