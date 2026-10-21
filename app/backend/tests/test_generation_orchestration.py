from sqlalchemy import select

from planner_api.ai import ProviderUnavailableError
from planner_api.config import Settings
from planner_api.generation_orchestration import (
    GenerationOrchestrator,
    SpecialistCall,
    SpecialistReportResult,
)
from planner_api.models import AgentReport, GenerationRun, Goal
from planner_api.schemas import PlanTaskDraft


class CountingSpecialist:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def analyze(self, role, task_type, context):
        self.calls.append(role)
        return SpecialistCall(
            result=SpecialistReportResult(
                summary=f"{role} 完成了只读整理",
                facts=[f"任务类型是 {task_type}"],
                constraints=["已完成内容不得改写"],
                evidence_ids=["message-1"],
                confidence=0.9,
            ),
            input_units=100,
            output_units=40,
            latency_ms=12,
        )


def test_high_complexity_run_uses_specialists_and_reuses_cached_reports(session_factory):
    provider = CountingSpecialist()
    settings = Settings(_env_file=None, llm_provider="fake", multi_agent_max_roles=3)
    with session_factory() as session:
        goal = Goal(title="测试规划", raw_input="需要保留历史并处理冲突")
        session.add(goal)
        session.commit()
        context = {
            "requirement_memory": {
                "effective_facts": [{"id": "fact-1", "field_key": "deadline", "value": "十月"}]
            },
            "recovery_context": {"backlog_tasks": [{"id": f"task-{i}"} for i in range(20)]},
            "locked_execution": {"day_ids": ["day-1"], "task_ids": ["task-1"]},
            "notes": "冲突 " + ("上下文" * 9000),
        }
        orchestrator = GenerationOrchestrator(session, provider=provider, settings=settings)
        first = orchestrator.prepare(
            goal.id,
            "RECOVERY_PLANNING",
            context,
            minimum_tier="L3",
        )
        assert first["tier"] == "L3"
        assert len(first["specialist_reports"]) == 3
        assert len(provider.calls) == 3
        orchestrator.complete(first["run_id"], {"status": "PASSED"})

        second = orchestrator.prepare(
            goal.id,
            "RECOVERY_PLANNING",
            context,
            minimum_tier="L3",
        )
        assert len(provider.calls) == 3
        assert all(report["cache_hit"] for report in second["specialist_reports"])
        records = list(
            session.scalars(
                select(AgentReport).where(AgentReport.generation_run_id == second["run_id"])
            )
        )
        assert {record.status for record in records} == {"CACHED"}
        assert len(list(session.scalars(select(GenerationRun)))) == 2


def test_flexible_task_policy_is_structured_and_defaults_to_exact_day():
    exact = PlanTaskDraft(
        order=1,
        title="完成基础动作",
        description="完成一组可以独立检查的基础动作",
        estimate_minutes=20,
        priority=3,
        task_kind="PRACTICE",
        evidence=[],
    )
    assert exact.scheduling_policy == "EXACT_DAY"

    flexible = exact.model_copy(
        update={
            "scheduling_policy": "DATE_WINDOW",
            "scheduling_policy_data": {"window": "本周内", "selection_rule": "天气合适时"},
        }
    )
    assert flexible.scheduling_policy == "DATE_WINDOW"
    assert flexible.scheduling_policy_data["window"] == "本周内"


def test_intent_report_reuses_unchanged_facts_but_invalidates_on_user_change(session_factory):
    provider = CountingSpecialist()
    settings = Settings(_env_file=None, llm_provider="fake", multi_agent_max_roles=1)
    with session_factory() as session:
        goal = Goal(title="分阶段规划", raw_input="前松后紧")
        session.add(goal)
        session.commit()
        orchestrator = GenerationOrchestrator(session, provider=provider, settings=settings)
        facts = {"requirements": {"capacity": [60,60,480,480]}, "user_request": "前松后紧"}
        orchestrator.prepare(goal.id, "FULL_PLAN", {**facts, "current_plan": {"generated_weeks": 1}}, minimum_tier="L2")
        next_week = orchestrator.prepare(goal.id, "FULL_PLAN", {**facts, "current_plan": {"generated_weeks": 2}}, minimum_tier="L2")
        assert provider.calls == ["CONTEXT_CURATOR"]
        assert next_week["specialist_reports"][0]["cache_hit"]
        orchestrator.prepare(goal.id, "FULL_PLAN", {**facts, "user_request": "前期改为40分钟"}, minimum_tier="L2")
        assert provider.calls == ["CONTEXT_CURATOR", "CONTEXT_CURATOR"]


def test_failed_specialist_records_diagnostic_and_supplies_local_fallback(session_factory):
    class FailedSpecialist:
        def analyze(self, *_args):
            raise ProviderUnavailableError(
                "辅助整理失败",
                code="SPECIALIST_AGENT_FAILED",
                diagnostic="invalid structured response",
            )

    settings = Settings(_env_file=None, llm_provider="fake", multi_agent_max_roles=1)
    with session_factory() as session:
        goal = Goal(title="测试规划", raw_input="安排执行日期")
        session.add(goal)
        session.commit()
        result = GenerationOrchestrator(
            session,
            provider=FailedSpecialist(),
            settings=settings,
        ).prepare(
            goal.id,
            "DATE_SCHEDULING",
            {"instructions": "周一开始"},
            minimum_tier="L2",
        )
        assert result["decision"]["degraded_roles"] == ["CALENDAR_SPECIALIST"]
        assert result["specialist_reports"][0]["degraded"] is True
        record = session.scalar(
            select(AgentReport).where(AgentReport.generation_run_id == result["run_id"])
        )
        assert record.status == "FAILED"
        assert record.error_message == "invalid structured response"
        assert record.report_json["failure_code"] == "SPECIALIST_AGENT_FAILED"


def test_calendar_complexity_only_adds_specialist_for_structurally_complex_input(
    session_factory,
):
    provider = CountingSpecialist()
    settings = Settings(_env_file=None, llm_provider="fake", multi_agent_max_roles=1)
    with session_factory() as session:
        goal = Goal(title="日期安排", raw_input="按现有计划安排日期")
        session.add(goal)
        session.commit()
        orchestrator = GenerationOrchestrator(session, provider=provider, settings=settings)
        simple = orchestrator.prepare(
            goal.id,
            "DATE_SCHEDULING",
            {
                "calendar_summary": {
                    "execution_day_count": 92,
                    "task_count": 92,
                    "policy_counts": {"DATE_WINDOW": 77, "CADENCE": 15},
                }
            },
            minimum_tier="L1",
        )
        assert simple["tier"] == "L1"
        assert not simple["specialist_reports"]

        complex_run = orchestrator.prepare(
            goal.id,
            "DATE_SCHEDULING",
            {
                "calendar_summary": {
                    "execution_day_count": 92,
                    "task_count": 306,
                    "policy_counts": {
                        "DATE_WINDOW": 132,
                        "FLOATING": 85,
                        "EXACT_DAY": 47,
                        "CHECKPOINT": 35,
                    },
                }
            },
            minimum_tier="L1",
        )
        assert complex_run["tier"] == "L2"
        assert [item["role"] for item in complex_run["specialist_reports"]] == [
            "CALENDAR_SPECIALIST"
        ]
