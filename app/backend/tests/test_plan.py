from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
import pytest

from fastapi import HTTPException
from sqlalchemy import delete, func, select

from planner_api.ai import ProviderUnavailableError
from planner_api.api import get_plan_service
from planner_api.execution_service import ExecutionService
from planner_api.models import (
    ContextPack,
    ExecutionDayLog,
    Feedback,
    FeedbackAdjustment,
    FocusSession,
    GenerationBatch,
    Goal,
    GoalErrorMemory,
    ManualPlan,
    ManualTask,
    PlanActivation,
    PlanAdjustmentPreview,
    PlanDay,
    PlanDaySchedule,
    PlanMilestone,
    PlanningBrief,
    PlanTask,
    PlanVersion,
    ProgressEvent,
    QuickCapture,
    RecoveryOperation,
    RequirementVersion,
    ResearchRun,
    TaskTimeBlock,
)
from planner_api.plan_ai import FakePlanProvider
from planner_api.plan_discussion_ai import FakePlanDiscussionProvider
from planner_api.plan_service import PlanService
from planner_api.planning_context import build_requirement_memory, derive_planning_horizon_contract
from planner_api.schemas import (
    PlanAdjustmentProposal,
    PlanDayDraft,
    PlanDetailResult,
    PlanOutlineResult,
    PlanTaskDraft,
    RecoveryAllocationResult,
)


def test_schedule_overrides_update_daily_plan_and_visible_preview() -> None:
    plan = [
        {
            "scheduled_date": "2026-09-10",
            "assignments": [{"title": "阅读", "start_local": "09:00", "end_local": "09:30"}],
        }
    ]
    overrides = [
        {
            "scheduled_date": "2026-09-10",
            "title": "阅读",
            "start_local": "10:00",
            "end_local": "10:30",
        }
    ]
    changed = PlanService._with_schedule_plan_overrides(plan, overrides)
    assert changed[0]["assignments"][0]["start_local"] == "10:00"
    visible = [
        {
            "date": "2026-09-10",
            "title": "阅读",
            "start": "09:00",
            "end": "09:30",
            "state": "SCHEDULED",
        }
    ]
    visible_changed = PlanService._with_schedule_overrides(visible, overrides)
    assert visible_changed[0]["start"] == "10:00"


def test_adjustment_schedule_fingerprint_supports_renamed_orm_columns(
    session_factory,
) -> None:
    with session_factory() as session:
        quick_plan = ManualPlan(title="今晚安排", timezone="Asia/Shanghai")
        session.add(quick_plan)
        session.flush()
        session.add(
            ManualTask(
                manual_plan_id=quick_plan.id,
                order_in_plan=1,
                title="整理训练装备",
                estimate_minutes=15,
            )
        )
        session.commit()

    with session_factory() as session:
        service = PlanService(session, FakePlanProvider(), FakePlanDiscussionProvider())
        before = service._adjustment_schedule_fingerprint()
        task = session.scalar(select(ManualTask))
        assert task is not None
        task.title = "整理训练装备和水杯"
        session.flush()
        after = service._adjustment_schedule_fingerprint()

    assert before != after


def _confirmed_route(client) -> dict:
    from test_route import _confirmed_requirement

    requirement = _confirmed_requirement(client)
    goal_id = requirement["goal_id"]
    client.post(f"/api/goals/{goal_id}/route/draft")
    response = client.post(f"/api/goals/{goal_id}/route/confirm")
    assert response.status_code == 200
    return response.json()


def _completed_plan(client) -> dict:
    route_workspace = _confirmed_route(client)
    goal_id = route_workspace["goal_id"]
    workspace = client.post(f"/api/goals/{goal_id}/plan/draft").json()
    while workspace["generation_phase"] == "DETAILS":
        workspace = client.post(f"/api/goals/{goal_id}/plan/details/next").json()
    return workspace


def test_explicit_plan_criticism_becomes_goal_scoped_error_memory(client, session_factory) -> None:
    workspace = _completed_plan(client)
    goal_id = workspace["goal_id"]
    feedback = "第 1 周这个排布不合理，以后不要把三个高强度任务连续放在一起。"

    response = client.post(
        f"/api/goals/{goal_id}/plan/revise",
        json={"content": feedback},
    )

    assert response.status_code == 200, response.text
    with session_factory() as session:
        memory = session.scalar(select(GoalErrorMemory).where(GoalErrorMemory.goal_id == goal_id))
        assert memory is not None
        assert memory.status == "CONFIRMED"
        assert memory.evidence == feedback
        requirement_memory = build_requirement_memory(session, goal_id)
        assert requirement_memory["error_avoidance_memory"][0]["rule"] == memory.rule


def test_repeated_plan_criticism_upserts_one_error_memory(client, session_factory) -> None:
    workspace = _completed_plan(client)
    goal_id = workspace["goal_id"]
    feedback = "这个排布不合理，以后不要连续安排多个高负荷日。"

    for _ in range(2):
        response = client.post(
            f"/api/goals/{goal_id}/plan/revise",
            json={"content": feedback},
        )
        assert response.status_code == 200, response.text

    with session_factory() as session:
        memories = list(
            session.scalars(select(GoalErrorMemory).where(GoalErrorMemory.goal_id == goal_id))
        )
        assert len(memories) == 1


def test_discussion_horizon_contract_compiles_seasonal_window_to_stable_weeks() -> None:
    memory = {
        "clarification": {
            "recent_messages": [{"role": "USER", "content": "从现在开始准备，寒假开始进入投递季。"}]
        }
    }
    contract = derive_planning_horizon_contract(
        {"deadline": "从现在到寒假开始，之前是准备阶段。"},
        memory,
        date(2026, 9, 20),
    )

    assert contract is not None
    assert contract["target_weeks"] == 16
    assert contract["basis"] == "SEASONAL_DISCUSSION_ANCHOR"
    assert contract["enforcement"] == "EXACT_TARGET"


def test_discussion_horizon_contract_uses_upper_bound_for_explicit_range() -> None:
    contract = derive_planning_horizon_contract(
        {"deadline": "准备周期约 3-4 个月。"},
        {},
        date(2026, 9, 20),
    )

    assert contract is not None
    assert contract["target_weeks"] == 16
    assert contract["minimum_weeks"] == 12
    assert contract["basis"] == "EXPLICIT_DISCUSSION_DURATION"


def test_outline_horizon_mismatch_is_repaired_before_persisting(client, session_factory) -> None:
    route_workspace = _confirmed_route(client)
    goal_id = route_workspace["goal_id"]
    with session_factory() as session:
        requirement = session.scalar(
            select(RequirementVersion)
            .where(RequirementVersion.goal_id == goal_id, RequirementVersion.status == "CONFIRMED")
            .order_by(RequirementVersion.version.desc())
        )
        assert requirement is not None
        requirement.snapshot_json = {
            **requirement.snapshot_json,
            "deadline": "从现在到寒假开始，之前约 3-4 个月。",
        }
        session.commit()

    class HorizonRepairProvider(FakePlanProvider):
        def __init__(self) -> None:
            self.calls = 0
            self.feedback = None

        @staticmethod
        def resize(result, total_weeks: int):
            stage_count = len(result.milestones)
            milestones = []
            for index, item in enumerate(result.milestones, start=1):
                start = min(total_weeks, ((index - 1) * total_weeks) // stage_count + 1)
                end = min(total_weeks, max(start, (index * total_weeks) // stage_count))
                milestones.append(item.model_copy(update={"start_week": start, "end_week": end}))
            segment = result.temporal_segments[0].model_copy(update={"end_week": total_weeks})
            commitments = [
                item.model_copy(update={"end_week": total_weeks}) for item in result.commitments
            ]
            return result.model_copy(
                update={
                    "total_weeks": total_weeks,
                    "milestones": milestones,
                    "temporal_segments": [segment],
                    "commitments": commitments,
                }
            )

        def draft_outline(self, context, generation_mode="STANDARD"):
            self.calls += 1
            target = int(context.planning_context["planning_horizon_contract"]["target_weeks"])
            if self.calls > 1:
                self.feedback = context.planning_context["generation_validation_feedback"]
            result = super().draft_outline(context, generation_mode)
            return self.resize(result, min(24, target + 1) if self.calls == 1 else target)

    provider = HorizonRepairProvider()

    def override_plan_service():
        with session_factory() as session:
            yield PlanService(session, provider, FakePlanDiscussionProvider())

    client.app.dependency_overrides[get_plan_service] = override_plan_service
    response = client.post(f"/api/goals/{goal_id}/plan/draft")

    assert response.status_code == 200
    assert response.json()["plan"]["total_weeks"] == 16
    assert provider.calls == 2
    assert provider.feedback["code"] == "PLAN_HORIZON_CONTRACT_INVALID"


def test_outline_missing_route_stage_is_repaired_before_persisting(client, session_factory) -> None:
    route_workspace = _confirmed_route(client)
    goal_id = route_workspace["goal_id"]

    class StageRepairProvider(FakePlanProvider):
        def __init__(self) -> None:
            self.calls = 0
            self.feedback = None

        def draft_outline(self, context, generation_mode="STANDARD"):
            self.calls += 1
            result = super().draft_outline(context, generation_mode)
            if self.calls == 1:
                milestones = result.milestones[:-1]
                milestones[-1] = milestones[-1].model_copy(update={"end_week": result.total_weeks})
                return result.model_copy(update={"milestones": milestones})
            self.feedback = context.planning_context["generation_validation_feedback"]
            return result

    provider = StageRepairProvider()

    def override_plan_service():
        with session_factory() as session:
            yield PlanService(session, provider, FakePlanDiscussionProvider())

    client.app.dependency_overrides[get_plan_service] = override_plan_service
    response = client.post(f"/api/goals/{goal_id}/plan/draft")

    assert response.status_code == 200
    assert [item["stage_order"] for item in response.json()["plan"]["milestones"]] == [
        1,
        2,
        3,
    ]
    assert provider.calls == 2
    assert provider.feedback["code"] == "PLAN_OUTLINE_STAGE_INVALID"


def test_confirmed_route_generates_relative_plan(client, session_factory) -> None:
    route_workspace = _confirmed_route(client)
    goal_id = route_workspace["goal_id"]

    response = client.post(f"/api/goals/{goal_id}/plan/draft")

    assert response.status_code == 200
    workspace = response.json()
    plan = workspace["plan"]
    assert workspace["status"] == "PLAN_DRAFTING"
    assert workspace["generation_phase"] == "DETAILS"
    assert workspace["generated_weeks"] == 0
    assert plan["status"] == "GENERATING"
    assert plan["total_weeks"] == 3
    assert len(plan["milestones"]) == 3
    assert plan["temporal_segments"] == [
        {
            "label": "默认推进期",
            "start_week": 1,
            "end_week": 3,
            "date_scope": "从实际开始日期起的全部计划周期",
            "effective_from": None,
            "effective_to": None,
            "capacity_summary": "每周安排 2 个执行日，每个执行日建议 90 分钟。",
            "weekly_execution_days": 2,
            "available_days_per_week": None,
            "suggested_daily_minutes": 90,
            "exception_days_per_week": 0,
            "exception_daily_minutes": None,
            "exception_weekdays": [],
            "weekly_capacity_overrides": [],
            "frequency_rules": ["每周完成两个可检查的小成果。"],
            "rest_rules": ["其余日期保留为休息或机动时间。"],
            "holiday_rules": [],
        }
    ]
    assert plan["source_document"].startswith("# 总体节奏")
    assert plan["compiler_report"]["status"] == "VALIDATED"
    assert plan["days"] == []

    first_details = client.post(f"/api/goals/{goal_id}/plan/details/next")
    assert first_details.status_code == 200
    workspace = first_details.json()
    plan = workspace["plan"]
    assert workspace["status"] == "PLAN_REVIEW"
    assert workspace["generation_phase"] == "COMPLETE"
    assert workspace["generated_weeks"] == 3
    assert plan["status"] == "DRAFT"
    assert len(plan["days"]) == 6
    assert plan["days"][0]["week_index"] == 1
    assert plan["days"][0]["day_index"] == 1
    relative_plan_text = " ".join(
        [
            plan["source_document"],
            *[day["primary_goal"] for day in plan["days"]],
            *[task["description"] for day in plan["days"] for task in day["tasks"]],
        ]
    )
    assert "2026" not in relative_plan_text

    with session_factory() as session:
        versions = list(session.scalars(select(PlanVersion)))
        milestones = list(session.scalars(select(PlanMilestone)))
        days = list(session.scalars(select(PlanDay)))
        tasks = list(session.scalars(select(PlanTask)))
    assert len(versions) == 1
    assert len(milestones) == 3
    assert len(days) == 6
    assert len(tasks) == 6


def test_exploratory_plan_stops_at_first_observable_route_outcome(client, session_factory) -> None:
    from test_clarification import answer_three_rounds, create_goal

    clarification = answer_three_rounds(client, create_goal(client))
    goal_id = clarification["goal_id"]
    assert (
        client.put(
            f"/api/goals/{goal_id}/planning-approach",
            json={"planning_mode": "EXPLORATORY"},
        ).status_code
        == 200
    )
    assert (
        client.put(
            f"/api/goals/{goal_id}/schedule-type",
            json={"schedule_type": "OUTCOME"},
        ).status_code
        == 200
    )
    assert client.post(f"/api/goals/{goal_id}/requirements/confirm").status_code == 200
    assert client.post(f"/api/goals/{goal_id}/route/draft").status_code == 200
    assert client.post(f"/api/goals/{goal_id}/route/confirm").status_code == 200

    workspace = client.post(f"/api/goals/{goal_id}/plan/draft").json()

    assert workspace["planning_mode"] == "EXPLORATORY"
    assert workspace["schedule_type"] == "OUTCOME"
    assert workspace["generation_target_weeks"] == 2
    assert workspace["plan"]["next_unplanned_week"] == 3
    assert workspace["exploration_windows"][0]["end_week"] == 2
    assert (
        workspace["exploration_windows"][0]["basis"]["horizon_basis"]
        == "FIRST_OBSERVABLE_ROUTE_OUTCOME"
    )
    while workspace["generation_phase"] == "DETAILS":
        workspace = client.post(f"/api/goals/{goal_id}/plan/details/next").json()
    assert workspace["generated_weeks"] == 2
    assert all(day["day_index"] == 1 for day in workspace["plan"]["days"])
    assert client.post(f"/api/goals/{goal_id}/plan/confirm").status_code == 200
    today = date.today()
    start = client.post(
        f"/api/goals/{goal_id}/execution/start",
        json={
            "requested_start_date": today.isoformat(),
            "execution_weekdays": [today.weekday()],
            "excluded_dates": [],
        },
    )
    assert start.status_code == 200

    with session_factory() as session:
        schedules = list(
            session.scalars(
                select(PlanDaySchedule)
                .where(PlanDaySchedule.activation_id.is_not(None))
                .order_by(PlanDaySchedule.scheduled_date)
            )
        )
        assert len(schedules) == 2
        first_day_id = schedules[0].plan_day_id
        schedules[0].scheduled_date = today - timedelta(days=1)
        schedules[1].scheduled_date = today
        session.commit()

    current = client.get(f"/api/goals/{goal_id}/today")
    assert current.status_code == 200
    assert current.json()["schedule_type"] == "OUTCOME"
    assert current.json()["day"] is not None
    assert current.json()["day"]["id"] == first_day_id
    assert current.json()["inactivity"]["level"] == "NONE"

    next_stage = client.post(
        f"/api/goals/{goal_id}/plan/exploration/draft",
        json={"mode": "STANDARD"},
    )

    assert next_stage.status_code == 200
    pending = next_stage.json()["pending_exploration_window"]
    assert (pending["start_week"], pending["end_week"]) == (3, 3)


def test_planning_compiler_brief_and_generation_batch_are_idempotent(
    client, session_factory
) -> None:
    workspace = _completed_plan(client)
    goal_id = workspace["goal_id"]

    first = client.get(f"/api/goals/{goal_id}/plan/compiler/brief")
    second = client.get(f"/api/goals/{goal_id}/plan/compiler/brief")
    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["id"] == second.json()["id"]
    assert first.json()["brief"]["capacity"]["total_weeks"] == 3
    assert first.json()["context"]["facts"]["precedence_rules"]
    assert first.json()["context"]["facts"]["requirement_memory"]["effective_facts"]
    assert first.json()["context"]["facts"]["schedule_context"]["timezone"] == ("Asia/Shanghai")

    payload = {
        "idempotency_key": "compiler-test-week-1",
        "scope_type": "WEEK",
        "scope": {"week_index": 1},
    }
    created = client.post(f"/api/goals/{goal_id}/plan/compiler/batches", json=payload)
    repeated = client.post(f"/api/goals/{goal_id}/plan/compiler/batches", json=payload)
    assert created.status_code == 201
    assert repeated.status_code == 201
    assert created.json()["id"] == repeated.json()["id"]
    assert client.get(f"/api/goals/{goal_id}/plan/compiler/batches").json()[0]["scope"] == {
        "week_index": 1
    }

    with session_factory() as session:
        assert len(list(session.scalars(select(ContextPack)))) == 3
        assert len(list(session.scalars(select(PlanningBrief)))) == 3
        batches = list(session.scalars(select(GenerationBatch)))
        assert (
            len([item for item in batches if item.idempotency_key == payload["idempotency_key"]])
            == 1
        )
        automatic = [item for item in batches if item.idempotency_key != payload["idempotency_key"]]
        assert len(automatic) == 4
        assert all(item.status == "COMPLETED" for item in automatic)
        assert {item.scope_type for item in automatic} == {"OUTLINE", "WEEK"}


def test_planning_context_carries_shared_schedule_fixed_tasks_and_holidays(client) -> None:
    assert (
        client.patch(
            "/api/schedule/profile",
            json={
                "timezone": "Asia/Shanghai",
                "holiday_country_code": "CN",
                "holiday_source": "USER",
                "holiday_confirmed": True,
            },
        ).status_code
        == 200
    )
    assert (
        client.put(
            "/api/schedule/profile/availability",
            json={
                "windows": [
                    {
                        "weekday": 6,
                        "start_local": "09:00",
                        "end_local": "18:00",
                        "valid_from": "2026-09-01",
                        "valid_to": "2026-09-30",
                    }
                ]
            },
        ).status_code
        == 200
    )
    fixed = client.post(
        "/api/manual-plans",
        json={
            "title": "固定生活安排",
            "tasks": [
                {
                    "title": "健身",
                    "estimate_minutes": 60,
                    "scheduled_date": "2026-09-13",
                    "start_local": "14:00",
                    "end_local": "15:00",
                    "recurrence": "WEEKLY",
                    "recurrence_weekdays": [6],
                }
            ],
        },
    )
    assert fixed.status_code == 201

    route_workspace = _confirmed_route(client)
    goal_id = route_workspace["goal_id"]
    brief = client.get(f"/api/goals/{goal_id}/plan/compiler/brief").json()
    context = brief["context"]["facts"]

    assert context["schedule_context"]["workspace_availability"][0] == {
        "weekday": 6,
        "start_local": "09:00",
        "end_local": "18:00",
        "valid_from": "2026-09-01",
        "valid_to": "2026-09-30",
    }
    assert context["schedule_context"]["fixed_occupancy_rules"][0]["title"] == "健身"
    assert context["schedule_context"]["holiday_region"]["country_code"] == "CN"
    assert context["requirement_memory"]["clarification"]["recent_messages"]


def test_plan_draft_endpoint_is_idempotent(client) -> None:
    route_workspace = _confirmed_route(client)
    goal_id = route_workspace["goal_id"]

    first = client.post(f"/api/goals/{goal_id}/plan/draft").json()
    second = client.post(f"/api/goals/{goal_id}/plan/draft").json()

    assert second["plan"]["id"] == first["plan"]["id"]
    while second["generation_phase"] == "DETAILS":
        second = client.post(f"/api/goals/{goal_id}/plan/details/next").json()
    completed = client.post(f"/api/goals/{goal_id}/plan/details/next").json()
    assert completed["plan"]["id"] == first["plan"]["id"]
    assert completed["generation_phase"] == "COMPLETE"


def test_failed_outline_reuses_batch_and_increments_attempt_count(client, session_factory) -> None:
    route_workspace = _confirmed_route(client)
    goal_id = route_workspace["goal_id"]

    class FailingOutlineProvider(FakePlanProvider):
        def draft_outline(self, context, generation_mode="STANDARD"):
            raise ProviderUnavailableError(
                "总体框架暂时失败",
                diagnostic="open_questions_note: Extra inputs are not permitted",
                code="INVALID_MODEL_RESPONSE",
            )

    def override_plan_service():
        with session_factory() as session:
            yield PlanService(
                session,
                FailingOutlineProvider(),
                FakePlanDiscussionProvider(),
            )

    client.app.dependency_overrides[get_plan_service] = override_plan_service
    first = client.post(f"/api/goals/{goal_id}/plan/draft")
    second = client.post(f"/api/goals/{goal_id}/plan/draft")

    assert first.status_code == 503
    assert second.status_code == 503
    with session_factory() as session:
        batches = list(
            session.scalars(select(GenerationBatch).where(GenerationBatch.goal_id == goal_id))
        )
    assert len(batches) == 1
    assert batches[0].status == "FAILED"
    assert batches[0].attempt_count == 2
    assert batches[0].failure_code == "INVALID_MODEL_RESPONSE"


def test_week_generation_retries_business_validation_with_feedback(client, session_factory) -> None:
    route_workspace = _confirmed_route(client)
    goal_id = route_workspace["goal_id"]

    class InvalidDayIndexThenValidProvider(FakePlanProvider):
        def __init__(self) -> None:
            self.calls: dict[int, int] = {}
            self.retry_feedback: dict[int, dict[str, object]] = {}

        def draft_details(
            self,
            context,
            outline,
            start_week,
            end_week,
            generation_mode="STANDARD",
        ):
            self.calls[start_week] = self.calls.get(start_week, 0) + 1
            result = super().draft_details(
                context,
                outline,
                start_week,
                end_week,
                generation_mode,
            )
            if self.calls[start_week] == 1:
                return result.model_copy(
                    update={
                        "days": [
                            day.model_copy(update={"day_index": day.day_index + 1})
                            for day in result.days
                        ]
                    }
                )
            self.retry_feedback[start_week] = context.planning_context[
                "generation_validation_feedback"
            ]
            return result

    provider = InvalidDayIndexThenValidProvider()

    def override_plan_service():
        with session_factory() as session:
            yield PlanService(session, provider, FakePlanDiscussionProvider())

    client.app.dependency_overrides[get_plan_service] = override_plan_service
    draft = client.post(f"/api/goals/{goal_id}/plan/draft")
    details = client.post(f"/api/goals/{goal_id}/plan/details/next")

    assert draft.status_code == 200
    assert details.status_code == 200
    assert details.json()["generation_phase"] == "COMPLETE"
    assert provider.calls == {1: 2, 2: 2, 3: 2}
    assert all(
        feedback["code"] == "PLAN_DETAIL_DAY_INDEX_INVALID"
        and "received=" in str(feedback["diagnostic"])
        and bool(feedback["previous_source_document"])
        for feedback in provider.retry_feedback.values()
    )


def test_confirm_plan_then_map_dates_and_start(client, session_factory) -> None:
    workspace = _completed_plan(client)
    goal_id = workspace["goal_id"]

    confirmed_response = client.post(f"/api/goals/{goal_id}/plan/confirm")

    assert confirmed_response.status_code == 200
    confirmed = confirmed_response.json()
    assert confirmed["status"] == "READY_TO_START"
    assert confirmed["plan"]["status"] == "CONFIRMED"

    setup = client.get(f"/api/goals/{goal_id}/execution/setup").json()
    assert setup["weekly_execution_days"] == 2
    assert len(setup["suggested_weekdays"]) == 2

    today = date.today()
    weekdays = sorted({today.weekday(), (today.weekday() + 1) % 7})
    payload = {
        "requested_start_date": today.isoformat(),
        "execution_weekdays": weekdays,
        "excluded_dates": [],
    }
    preview = client.post(f"/api/goals/{goal_id}/execution/preview", json=payload).json()
    assert preview["first_execution_date"] == today.isoformat()
    assert preview["total_execution_days"] == 6

    started_response = client.post(f"/api/goals/{goal_id}/execution/start", json=payload)
    assert started_response.status_code == 200
    today_workspace = started_response.json()
    assert today_workspace["status"] == "ACTIVE"
    assert today_workspace["is_execution_day"] is True
    assert today_workspace["scheduled_day"]["code"] == "M1-W1-D1"
    assert today_workspace["day"]["primary_goal"]

    with session_factory() as session:
        assert len(list(session.scalars(select(PlanActivation)))) == 1
        assert len(list(session.scalars(select(PlanDaySchedule)))) == 6

    calendar_response = client.get(f"/api/goals/{goal_id}/execution/calendar")
    assert calendar_response.status_code == 200
    calendar = calendar_response.json()
    assert len(calendar["days"]) == 6
    assert calendar["days"][0]["scheduled_date"] == today.isoformat()
    assert calendar["days"][0]["code"] == "M1-W1-D1"
    assert calendar["days"][0]["status"] == "PLANNED"
    assert calendar["days"][0]["tasks"][0]["title"]

    selected = client.get(
        f"/api/goals/{goal_id}/today",
        params={"on_date": calendar["days"][1]["scheduled_date"]},
    ).json()
    assert selected["today"] == calendar["days"][1]["scheduled_date"]
    assert selected["scheduled_day"]["code"] == calendar["days"][1]["code"]


def test_execution_mapping_uses_holidays_and_makeup_workdays(client) -> None:
    workspace = _completed_plan(client)
    goal_id = workspace["goal_id"]
    assert client.post(f"/api/goals/{goal_id}/plan/confirm").status_code == 200
    assert (
        client.patch(
            "/api/schedule/profile",
            json={"holiday_country_code": "CN", "holiday_source": "USER"},
        ).status_code
        == 200
    )

    makeup_preview = client.post(
        f"/api/goals/{goal_id}/execution/preview",
        json={
            "requested_start_date": "2026-09-20",
            "execution_weekdays": [0, 1],
            "excluded_dates": [],
        },
    ).json()
    assert makeup_preview["first_execution_date"] == "2026-09-20"

    holiday_preview = client.post(
        f"/api/goals/{goal_id}/execution/preview",
        json={
            "requested_start_date": "2026-09-25",
            "execution_weekdays": [0, 4],
            "excluded_dates": [],
        },
    ).json()
    assert holiday_preview["first_execution_date"] == "2026-09-28"


def test_execution_mapping_preserves_pick_one_holiday_rest_rule(client, session_factory) -> None:
    workspace = _completed_plan(client)
    goal_id = workspace["goal_id"]
    plan_id = workspace["plan"]["id"]
    with session_factory() as session:
        plan = session.get(PlanVersion, plan_id)
        plan.source_document = "中秋假期9月25日至27日，其中只挑一天休息，其余日期照常执行。"
        session.commit()
    assert client.post(f"/api/goals/{goal_id}/plan/confirm").status_code == 200
    client.patch(
        "/api/schedule/profile",
        json={"holiday_country_code": "CN", "holiday_source": "USER"},
    )
    preview = client.post(
        f"/api/goals/{goal_id}/execution/preview",
        json={
            "requested_start_date": "2026-09-25",
            "execution_weekdays": [4, 5],
            "excluded_dates": [],
        },
    ).json()
    assert preview["first_execution_date"] == "2026-09-26"


def test_today_can_pull_a_partial_future_task_forward(client) -> None:
    workspace = _completed_plan(client)
    goal_id = workspace["goal_id"]
    client.post(f"/api/goals/{goal_id}/plan/confirm")
    today = date.today()
    weekdays = sorted({today.weekday(), (today.weekday() + 1) % 7})
    started = client.post(
        f"/api/goals/{goal_id}/execution/start",
        json={
            "requested_start_date": today.isoformat(),
            "execution_weekdays": weekdays,
            "excluded_dates": [],
        },
    ).json()
    next_date = started["next_execution_date"]
    future = client.get(f"/api/goals/{goal_id}/today", params={"on_date": next_date}).json()
    future_task = future["day"]["tasks"][0]
    minutes = max(1, future_task["estimate_minutes"] - 1)
    response = client.post(
        f"/api/goals/{goal_id}/execution/carry-forward",
        json={"target_date": today.isoformat(), "task_id": future_task["id"], "minutes": minutes},
    )
    assert response.status_code == 200
    pulled = response.json()
    assert any(task["title"].startswith("提前承接：") for task in pulled["day"]["tasks"])


def test_today_can_arrange_a_floating_task_without_duplicate_materialization(client, session_factory) -> None:
    workspace = _completed_plan(client)
    goal_id = workspace["goal_id"]
    assert client.post(f"/api/goals/{goal_id}/plan/confirm").status_code == 200
    today = date.today()
    started = client.post(
        f"/api/goals/{goal_id}/execution/start",
        json={
            "requested_start_date": today.isoformat(),
            "execution_weekdays": sorted({today.weekday(), (today.weekday() + 1) % 7}),
            "excluded_dates": [],
        },
    ).json()
    future_date = started["next_execution_date"]
    assert future_date and future_date > today.isoformat()
    future = client.get(f"/api/goals/{goal_id}/today", params={"on_date": future_date}).json()
    task_id = future["day"]["tasks"][0]["id"]
    with session_factory() as session:
        task = session.get(PlanTask, task_id)
        task.scheduling_policy = "FLOATING"
        task.scheduling_policy_json = {}
        session.commit()

    calendar = client.get(f"/api/goals/{goal_id}/execution/calendar").json()
    calendar_task = next(
        item
        for day in calendar["days"]
        for item in day["tasks"]
        if item["id"] == task_id
    )
    assert calendar_task["can_arrange_today"] is True

    arranged = client.post(
        f"/api/goals/{goal_id}/execution/flexible-tasks/arrange",
        json={"task_id": task_id},
    )
    assert arranged.status_code == 200, arranged.text
    today_task_ids = {item["id"] for item in arranged.json()["day"]["tasks"]}
    assert task_id in today_task_ids

    repeated = client.post(
        f"/api/goals/{goal_id}/execution/flexible-tasks/arrange",
        json={"task_id": task_id},
    )
    assert repeated.status_code == 200, repeated.text
    with session_factory() as session:
        events = list(session.scalars(select(ProgressEvent).where(ProgressEvent.task_id == task_id)))
        assert [event.event_type for event in events] == ["CARRY_FORWARD"]
        moved = session.get(PlanTask, task_id)
        assert moved.plan_day_id == arranged.json()["day"]["id"]


def test_today_can_reschedule_flexible_task_with_a_reason(client, session_factory) -> None:
    workspace = _completed_plan(client)
    goal_id = workspace["goal_id"]
    plan_id = workspace["plan"]["id"]
    assert client.post(f"/api/goals/{goal_id}/plan/confirm").status_code == 200
    today = date.today()
    started = client.post(
        f"/api/goals/{goal_id}/execution/start",
        json={
            "requested_start_date": today.isoformat(),
            "execution_weekdays": sorted({today.weekday(), (today.weekday() + 1) % 7}),
            "excluded_dates": [],
        },
    ).json()
    future = client.get(
        f"/api/goals/{goal_id}/today", params={"on_date": started["next_execution_date"]}
    ).json()
    task_id = future["day"]["tasks"][0]["id"]
    with session_factory() as session:
        task = session.get(PlanTask, task_id)
        task.scheduling_policy = "FLOATING"
        task.scheduling_policy_json = {}
        session.commit()

    target_date = today + timedelta(days=2)
    moved = client.post(
        f"/api/goals/{goal_id}/execution/flexible-tasks/reschedule",
        json={"task_id": task_id, "target_date": target_date.isoformat(), "reason": "临时会议改到明天"},
    )
    assert moved.status_code == 200, moved.text
    assert moved.json()["today"] == target_date.isoformat()
    assert any(task["id"] == task_id for task in moved.json()["day"]["tasks"])
    with session_factory() as session:
        events = list(
            session.scalars(
                select(ProgressEvent).where(
                    ProgressEvent.goal_id == goal_id,
                    ProgressEvent.task_id == task_id,
                )
            )
        )
        assert [event.event_type for event in events] == ["RESCHEDULED"]
        assert events[0].metadata_json["reason"] == "临时会议改到明天"
        assert session.get(PlanTask, task_id).plan_version_id == plan_id

    past = client.post(
        f"/api/goals/{goal_id}/execution/flexible-tasks/reschedule",
        json={"task_id": task_id, "target_date": (today - timedelta(days=1)).isoformat(), "reason": "日期无效"},
    )
    assert past.status_code == 422


def test_flexible_decision_preview_requires_explicit_apply(client, session_factory) -> None:
    workspace = _completed_plan(client)
    goal_id = workspace["goal_id"]
    assert client.post(f"/api/goals/{goal_id}/plan/confirm").status_code == 200
    today = date.today()
    started = client.post(
        f"/api/goals/{goal_id}/execution/start",
        json={
            "requested_start_date": today.isoformat(),
            "execution_weekdays": sorted({today.weekday(), (today.weekday() + 1) % 7}),
            "excluded_dates": [],
        },
    ).json()
    future = client.get(
        f"/api/goals/{goal_id}/today", params={"on_date": started["next_execution_date"]}
    ).json()
    task_id = future["day"]["tasks"][0]["id"]
    with session_factory() as session:
        task = session.get(PlanTask, task_id)
        task.scheduling_policy = "FLOATING"
        task.scheduling_policy_json = {}
        session.commit()

    target_date = today + timedelta(days=2)
    with session_factory() as session:
        source_task = session.get(PlanTask, task_id)
        source_day = session.get(PlanDay, source_task.plan_day_id)
        activation = session.scalar(select(PlanActivation).where(PlanActivation.goal_id == goal_id))
        destination = PlanDay(
            plan_version_id=source_day.plan_version_id,
            milestone_id=source_day.milestone_id,
            stage_order=source_day.stage_order,
            week_index=source_day.week_index,
            day_index=100,
            primary_goal="容量竞争验收",
            suggested_minutes=60,
        )
        session.add(destination)
        session.flush()
        session.add(PlanDaySchedule(
            activation_id=activation.id, plan_day_id=destination.id, scheduled_date=target_date,
        ))
        competing = PlanTask(
            plan_version_id=source_day.plan_version_id, plan_day_id=destination.id,
            order_in_day=1, title="同日已有练习", description="不可重复计算",
            estimate_minutes=55, priority=1, task_kind="PRACTICE", status="NOT_STARTED",
        )
        session.add(competing)
        session.commit()
        competing_id = competing.id
        incoming_minutes = source_task.estimate_minutes
    preview = client.post(
        f"/api/goals/{goal_id}/execution/flexible-tasks/decision-preview",
        json={
            "action": "RESCHEDULE",
            "task_id": task_id,
            "target_date": target_date.isoformat(),
            "reason": "需要参加临时会议",
        },
    )
    assert preview.status_code == 200, preview.text
    preview_json = preview.json()
    assert preview_json["can_apply"] is True
    assert preview_json["source_date"] == started["next_execution_date"]
    assert preview_json["target_date"] == target_date.isoformat()
    assert preview_json["planned_minutes"] == 55 + incoming_minutes
    assert preview_json["capacity_minutes"] == 60
    assert preview_json["over_capacity_minutes"] == max(0, 55 + incoming_minutes - 60)
    assert preview_json["competing_tasks"] == ["同日已有练习"]
    with session_factory() as session:
        before_apply = session.get(PlanTask, task_id)
        assert before_apply is not None
        original_day_id = before_apply.plan_day_id
        session.get(PlanTask, competing_id).estimate_minutes = 75
        session.commit()

    stale = client.post(
        f"/api/goals/{goal_id}/execution/flexible-tasks/decision-apply",
        json={"preview_id": preview_json["id"]},
    )
    assert stale.status_code == 409
    with session_factory() as session:
        assert session.get(PlanTask, task_id).plan_day_id == original_day_id
    refreshed = client.post(
        f"/api/goals/{goal_id}/execution/flexible-tasks/decision-preview",
        json={"action": "RESCHEDULE", "task_id": task_id,
              "target_date": target_date.isoformat(), "reason": "需要参加临时会议"},
    )
    assert refreshed.status_code == 200, refreshed.text
    preview_json = refreshed.json()
    assert preview_json["planned_minutes"] == 75 + incoming_minutes

    applied = client.post(
        f"/api/goals/{goal_id}/execution/flexible-tasks/decision-apply",
        json={"preview_id": preview_json["id"]},
    )
    assert applied.status_code == 200, applied.text
    assert any(item["id"] == task_id for item in applied.json()["day"]["tasks"])
    with session_factory() as session:
        assert session.get(PlanTask, task_id).plan_day_id != original_day_id
    repeated = client.post(
        f"/api/goals/{goal_id}/execution/flexible-tasks/decision-apply",
        json={"preview_id": preview_json["id"]},
    )
    assert repeated.status_code == 200


def test_today_explains_when_a_floating_task_is_blocked_by_a_dependency(client, session_factory) -> None:
    workspace = _completed_plan(client)
    goal_id = workspace["goal_id"]
    assert client.post(f"/api/goals/{goal_id}/plan/confirm").status_code == 200
    today = date.today()
    started = client.post(
        f"/api/goals/{goal_id}/execution/start",
        json={
            "requested_start_date": today.isoformat(),
            "execution_weekdays": sorted({today.weekday(), (today.weekday() + 1) % 7}),
            "excluded_dates": [],
        },
    ).json()
    future = client.get(f"/api/goals/{goal_id}/today", params={"on_date": started["next_execution_date"]}).json()
    future_task = future["day"]["tasks"][0]
    today_task = client.get(f"/api/goals/{goal_id}/today").json()["day"]["tasks"][0]
    with session_factory() as session:
        task = session.get(PlanTask, future_task["id"])
        task.scheduling_policy = "FLOATING"
        task.depends_on_json = [today_task["title"]]
        session.commit()

    calendar = client.get(f"/api/goals/{goal_id}/execution/calendar").json()
    calendar_task = next(
        item
        for day in calendar["days"]
        for item in day["tasks"]
        if item["id"] == future_task["id"]
    )
    assert calendar_task["can_arrange_today"] is False
    assert "需先完成" in calendar_task["arrange_blocked_reason"]
    response = client.post(
        f"/api/goals/{goal_id}/execution/flexible-tasks/arrange",
        json={"task_id": future_task["id"]},
    )
    assert response.status_code == 409
    assert "需先完成" in response.json()["detail"]


def test_today_rejects_partial_carry_for_atomic_exam_task(client, session_factory) -> None:
    workspace = _completed_plan(client)
    goal_id = workspace["goal_id"]
    client.post(f"/api/goals/{goal_id}/plan/confirm")
    today = date.today()
    weekdays = sorted({today.weekday(), (today.weekday() + 1) % 7})
    started = client.post(
        f"/api/goals/{goal_id}/execution/start",
        json={
            "requested_start_date": today.isoformat(),
            "execution_weekdays": weekdays,
            "excluded_dates": [],
        },
    ).json()
    candidate = started["carry_forward_tasks"][0]
    with session_factory() as session:
        task = session.get(PlanTask, candidate["task_id"])
        task.title = "限时完成一整套模拟卷"
        session.commit()
    refreshed = client.get(f"/api/goals/{goal_id}/today").json()
    candidate = refreshed["carry_forward_tasks"][0]
    assert candidate["can_split"] is False
    response = client.post(
        f"/api/goals/{goal_id}/execution/carry-forward",
        json={
            "target_date": today.isoformat(),
            "task_id": candidate["task_id"],
            "minutes": max(1, candidate["estimate_minutes"] - 1),
        },
    )
    assert response.status_code == 422
    assert "不能拆" in response.json()["detail"]
    whole = client.post(
        f"/api/goals/{goal_id}/execution/carry-forward",
        json={
            "target_date": today.isoformat(),
            "task_id": candidate["task_id"],
            "minutes": None,
        },
    )
    assert whole.status_code == 200
    assert any(task["id"] == candidate["task_id"] for task in whole.json()["day"]["tasks"])


def test_today_task_events_are_idempotent_and_persist(client, session_factory) -> None:
    workspace = _completed_plan(client)
    goal_id = workspace["goal_id"]
    client.post(f"/api/goals/{goal_id}/plan/confirm")
    today = date.today()
    weekdays = sorted({today.weekday(), (today.weekday() + 1) % 7})
    payload = {
        "requested_start_date": today.isoformat(),
        "execution_weekdays": weekdays,
        "excluded_dates": [],
    }
    today_workspace = client.post(f"/api/goals/{goal_id}/execution/start", json=payload).json()
    task_id = today_workspace["day"]["tasks"][0]["id"]

    started = client.post(f"/api/goals/{goal_id}/tasks/{task_id}/start").json()
    assert started["day"]["tasks"][0]["status"] == "IN_PROGRESS"
    completed = client.post(f"/api/goals/{goal_id}/tasks/{task_id}/complete").json()
    assert completed["day"]["tasks"][0]["status"] == "COMPLETED"
    assert completed["day_completed"] is True
    repeated = client.post(f"/api/goals/{goal_id}/tasks/{task_id}/complete").json()
    assert repeated["day_progress"]["completed_tasks"] == 1

    refreshed = client.get(f"/api/goals/{goal_id}/today").json()
    assert refreshed["day"]["tasks"][0]["status"] == "COMPLETED"
    ended = client.post(f"/api/goals/{goal_id}/today/complete").json()
    assert ended["day_completed"] is True
    client.post(f"/api/goals/{goal_id}/today/complete")
    with session_factory() as session:
        events = list(session.scalars(select(ProgressEvent)))
    assert [event.event_type for event in events] == [
        "STARTED",
        "COMPLETED",
        "DAY_COMPLETED",
    ]


def test_last_plan_task_automatically_completes_goal(client, session_factory) -> None:
    workspace = _completed_plan(client)
    goal_id = workspace["goal_id"]
    plan_id = workspace["plan"]["id"]
    client.post(f"/api/goals/{goal_id}/plan/confirm")
    today = date.today()
    weekdays = sorted({today.weekday(), (today.weekday() + 1) % 7})
    started = client.post(
        f"/api/goals/{goal_id}/execution/start",
        json={
            "requested_start_date": today.isoformat(),
            "execution_weekdays": weekdays,
            "excluded_dates": [],
        },
    ).json()
    final_task_id = started["day"]["tasks"][0]["id"]

    with session_factory() as session:
        tasks = list(session.scalars(select(PlanTask).where(PlanTask.plan_version_id == plan_id)))
        for task in tasks:
            if task.id != final_task_id:
                task.status = "COMPLETED"
        session.commit()

    completed = client.post(f"/api/goals/{goal_id}/tasks/{final_task_id}/complete")
    assert completed.status_code == 200
    assert completed.json()["status"] == "COMPLETED"
    assert completed.json()["day_completed"] is True
    lifecycle = client.get(f"/api/goals/{goal_id}/lifecycle").json()
    assert lifecycle["status"] == "COMPLETED"
    assert lifecycle["completed_at"] is not None
    assert "6 项任务已全部完成" in lifecycle["completion_summary"]


def test_inactivity_distinguishes_short_and_long_pauses(client, session_factory) -> None:
    workspace = _completed_plan(client)
    goal_id = workspace["goal_id"]
    client.post(f"/api/goals/{goal_id}/plan/confirm")
    today = date.today()
    weekdays = sorted({today.weekday(), (today.weekday() + 1) % 7})
    started = client.post(
        f"/api/goals/{goal_id}/execution/start",
        json={
            "requested_start_date": today.isoformat(),
            "execution_weekdays": weekdays,
            "excluded_dates": [],
        },
    ).json()
    assert started["inactivity"]["level"] == "NONE"

    with session_factory() as session:
        activation = session.scalar(select(PlanActivation).where(PlanActivation.goal_id == goal_id))
        first_schedule = session.scalar(
            select(PlanDaySchedule)
            .where(PlanDaySchedule.activation_id == activation.id)
            .order_by(PlanDaySchedule.scheduled_date)
        )
        first_schedule.scheduled_date = today - timedelta(days=1)
        activation.activated_at = datetime.now(UTC) - timedelta(days=1)
        session.commit()

    missed_once = client.get(f"/api/goals/{goal_id}/today").json()["inactivity"]
    assert missed_once["level"] == "MISSED_ONCE"
    assert missed_once["missed_execution_days"] == 1
    assert missed_once["latest_missed_date"] == (today - timedelta(days=1)).isoformat()
    calendar = client.get(f"/api/goals/{goal_id}/execution/calendar").json()
    missed_day = next(
        item
        for item in calendar["days"]
        if item["scheduled_date"] == (today - timedelta(days=1)).isoformat()
    )
    assert missed_day["status"] == "MISSED"

    with session_factory() as session:
        activation = session.scalar(select(PlanActivation).where(PlanActivation.goal_id == goal_id))
        activation.activated_at = datetime.now(UTC) - timedelta(days=3)
        session.commit()

    paused_three_days = client.get(f"/api/goals/{goal_id}/execution/progress").json()["inactivity"]
    assert paused_three_days["level"] == "PAUSED_3_DAYS"
    assert paused_three_days["inactive_days"] == 3

    with session_factory() as session:
        activation = session.scalar(select(PlanActivation).where(PlanActivation.goal_id == goal_id))
        activation.activated_at = datetime.now(UTC) - timedelta(days=6)
        session.commit()

    assert (
        client.get(f"/api/goals/{goal_id}/today").json()["inactivity"]["level"] == "PAUSED_3_DAYS"
    )

    with session_factory() as session:
        activation = session.scalar(select(PlanActivation).where(PlanActivation.goal_id == goal_id))
        activation.activated_at = datetime.now(UTC) - timedelta(days=7)
        session.commit()

    client.post(
        f"/api/goals/{goal_id}/quick-captures",
        json={"content": "回来后先记下一个想法"},
    )
    paused_week = client.get(f"/api/goals/{goal_id}/today").json()["inactivity"]
    assert paused_week["level"] == "PAUSED_7_DAYS"
    assert paused_week["inactive_days"] == 7

    with session_factory() as session:
        plan_id = started["day"]["id"]
        plan_day = session.get(PlanDay, plan_id)
        session.add(
            ProgressEvent(
                goal_id=goal_id,
                plan_version_id=plan_day.plan_version_id,
                plan_day_id=plan_day.id,
                event_type="STARTED",
            )
        )
        session.commit()

    assert client.get(f"/api/goals/{goal_id}/today").json()["inactivity"]["level"] == "NONE"


def test_missed_task_can_be_completed_retrospectively(client, session_factory) -> None:
    workspace = _completed_plan(client)
    goal_id = workspace["goal_id"]
    client.post(f"/api/goals/{goal_id}/plan/confirm")
    today = date.today()
    weekdays = sorted({today.weekday(), (today.weekday() + 1) % 7})
    started = client.post(
        f"/api/goals/{goal_id}/execution/start",
        json={
            "requested_start_date": today.isoformat(),
            "execution_weekdays": weekdays,
            "excluded_dates": [],
        },
    ).json()
    task_id = started["day"]["tasks"][0]["id"]

    with session_factory() as session:
        activation = session.scalar(select(PlanActivation).where(PlanActivation.goal_id == goal_id))
        schedule = session.scalar(
            select(PlanDaySchedule).where(
                PlanDaySchedule.activation_id == activation.id,
                PlanDaySchedule.plan_day_id == started["day"]["id"],
            )
        )
        schedule.scheduled_date = today - timedelta(days=1)
        activation.activated_at = datetime.now(UTC) - timedelta(days=1)
        session.get(Goal, goal_id).status = "RECOVERY_REQUIRED"
        session.commit()

    before = client.get(f"/api/goals/{goal_id}/execution/progress").json()
    assert before["inactivity"]["missed_execution_days"] == 1

    response = client.post(
        f"/api/goals/{goal_id}/execution/recovery/complete-missed",
        json={"task_ids": [task_id]},
    )
    assert response.status_code == 200, response.text
    assert response.json()["inactivity"]["missed_execution_days"] == 0
    assert client.get(f"/api/goals/{goal_id}/lifecycle").json()["status"] == "ACTIVE"

    with session_factory() as session:
        task = session.get(PlanTask, task_id)
        event = session.scalar(
            select(ProgressEvent).where(
                ProgressEvent.task_id == task_id,
                ProgressEvent.event_type == "COMPLETED",
            )
        )
        assert task.status == "COMPLETED"
        assert event.metadata_json["recording_mode"] == "RETROACTIVE_COMPLETION"


def test_partial_day_is_distinct_and_optional_tasks_share_completion_boundary(
    client, session_factory
) -> None:
    workspace = _completed_plan(client)
    goal_id = workspace["goal_id"]
    client.post(f"/api/goals/{goal_id}/plan/confirm")
    today = date.today()
    started = client.post(
        f"/api/goals/{goal_id}/execution/start",
        json={
            "requested_start_date": today.isoformat(),
            "execution_weekdays": sorted({today.weekday(), (today.weekday() + 1) % 7}),
            "excluded_dates": [],
        },
    )
    assert started.status_code == 200, started.text

    with session_factory() as session:
        goal = session.get(Goal, goal_id)
        plan = session.get(PlanVersion, goal.active_plan_version_id)
        activation = session.scalar(select(PlanActivation).where(PlanActivation.goal_id == goal_id))
        schedules = list(
            session.scalars(
                select(PlanDaySchedule)
                .where(PlanDaySchedule.activation_id == activation.id)
                .order_by(PlanDaySchedule.scheduled_date)
            )
        )
        first_day = session.get(PlanDay, schedules[0].plan_day_id)
        first_task = session.scalar(select(PlanTask).where(PlanTask.plan_day_id == first_day.id))
        first_task.status = "COMPLETED"
        optional_task = PlanTask(
            plan_version_id=plan.id,
            plan_day_id=first_day.id,
            order_in_day=2,
            title="可选复盘",
            description="当天的可选复盘任务",
            estimate_minutes=15,
            priority="LOW",
            task_kind="OPTIONAL",
            is_required=False,
            evidence_json=[],
            depends_on_json=[],
            status="NOT_STARTED",
        )
        session.add(optional_task)
        schedules[0].scheduled_date = today - timedelta(days=2)
        schedules[1].scheduled_date = today - timedelta(days=1)
        activation.activated_at = datetime.now(UTC) - timedelta(days=2)
        session.flush()

        service = ExecutionService(session)
        service._apply_automatic_completions(goal, plan, first_day)
        day_completed = session.scalar(
            select(ProgressEvent.id).where(
                ProgressEvent.goal_id == goal_id,
                ProgressEvent.plan_day_id == first_day.id,
                ProgressEvent.event_type == "DAY_COMPLETED",
            )
        )
        assert day_completed is None
        session.commit()

    inactivity = client.get(f"/api/goals/{goal_id}/today").json()["inactivity"]
    assert inactivity["missed_execution_days"] == 2
    assert inactivity["partial_execution_days"] == 1
    assert inactivity["partial_dates"] == [(today - timedelta(days=2)).isoformat()]
    assert inactivity["interrupted_execution_days"] == 1
    assert inactivity["interrupted_dates"] == [(today - timedelta(days=1)).isoformat()]

    calendar = client.get(f"/api/goals/{goal_id}/execution/calendar").json()["days"]
    statuses = {item["scheduled_date"]: item["status"] for item in calendar}
    assert statuses[(today - timedelta(days=2)).isoformat()] == "PARTIAL"
    assert statuses[(today - timedelta(days=1)).isoformat()] == "MISSED"


def test_status_check_persists_recovery_required_and_defer_reactivates(
    client, session_factory
) -> None:
    workspace = _completed_plan(client)
    goal_id = workspace["goal_id"]
    client.post(f"/api/goals/{goal_id}/plan/confirm")
    today = date.today()
    weekdays = sorted({today.weekday(), (today.weekday() + 1) % 7})
    started = client.post(
        f"/api/goals/{goal_id}/execution/start",
        json={
            "requested_start_date": today.isoformat(),
            "execution_weekdays": weekdays,
            "excluded_dates": [],
        },
    ).json()

    with session_factory() as session:
        activation = session.scalar(select(PlanActivation).where(PlanActivation.goal_id == goal_id))
        first_schedule = session.scalar(
            select(PlanDaySchedule)
            .where(PlanDaySchedule.activation_id == activation.id)
            .order_by(PlanDaySchedule.scheduled_date)
        )
        first_schedule.scheduled_date = today - timedelta(days=7)
        activation.activated_at = datetime.now(UTC) - timedelta(days=7)
        session.commit()

    checked = client.post("/api/goals/status-check")
    assert checked.status_code == 200
    assert next(item for item in checked.json() if item["id"] == goal_id)["status"] == (
        "RECOVERY_REQUIRED"
    )
    assert client.get(f"/api/goals/{goal_id}/today").status_code == 200
    task_id = started["day"]["tasks"][0]["id"]
    assert client.post(f"/api/goals/{goal_id}/tasks/{task_id}/start").status_code == 409

    inactivity = client.get(f"/api/goals/{goal_id}/today").json()["inactivity"]
    deferred = client.post(
        f"/api/goals/{goal_id}/execution/recovery/defer",
        json={"missed_dates": inactivity["missed_dates"]},
    )
    assert deferred.status_code == 200
    assert client.get(f"/api/goals/{goal_id}/lifecycle").json()["status"] == "ACTIVE"


def test_goal_pause_resume_timezone_completion_archive_and_delete(client, session_factory) -> None:
    workspace = _completed_plan(client)
    goal_id = workspace["goal_id"]
    client.post(f"/api/goals/{goal_id}/plan/confirm")
    today = date.today()
    weekdays = sorted({today.weekday(), (today.weekday() + 1) % 7})
    client.post(
        f"/api/goals/{goal_id}/execution/start",
        json={
            "requested_start_date": today.isoformat(),
            "execution_weekdays": weekdays,
            "excluded_dates": [],
        },
    )

    settings = client.patch(f"/api/goals/{goal_id}/settings", json={"timezone": "Europe/London"})
    assert settings.status_code == 200
    assert settings.json()["timezone"] == "Europe/London"
    assert (
        client.patch(
            f"/api/goals/{goal_id}/settings", json={"timezone": "Mars/Olympus"}
        ).status_code
        == 422
    )

    paused = client.post(
        f"/api/goals/{goal_id}/pause",
        json={"pause_until": (today + timedelta(days=2)).isoformat()},
    )
    assert paused.status_code == 200
    assert paused.json()["status"] == "PAUSED"
    resumed = client.post(f"/api/goals/{goal_id}/resume")
    assert resumed.status_code == 200
    assert resumed.json()["status"] == "ACTIVE"
    indefinite = client.post(f"/api/goals/{goal_id}/pause", json={"pause_until": None})
    assert indefinite.status_code == 200
    assert indefinite.json()["pause_until"] is None
    assert client.post(f"/api/goals/{goal_id}/resume").json()["status"] == "ACTIVE"
    assert client.post(f"/api/goals/{goal_id}/complete", json={}).status_code == 409

    with session_factory() as session:
        tasks = session.scalars(
            select(PlanTask).where(PlanTask.plan_version_id == workspace["plan"]["id"])
        )
        for task in tasks:
            task.status = "COMPLETED"
        session.commit()

    completed = client.post(f"/api/goals/{goal_id}/complete", json={"note": "完成了整个计划。"})
    assert completed.status_code == 200
    assert completed.json()["status"] == "COMPLETED"
    assert completed.json()["completion_summary"] == "完成了整个计划。"
    assert client.post(f"/api/goals/{goal_id}/archive").json()["status"] == "ARCHIVED"
    archived_item = next(item for item in client.get("/api/goals").json() if item["id"] == goal_id)
    assert archived_item["status"] == "ARCHIVED"
    assert archived_item["archived_at"] is not None

    disposable = client.post("/api/goals", json={"raw_input": "这是一个待删除目标"}).json()
    disposable_id = disposable["goal_id"]
    renamed = client.patch(f"/api/goals/{disposable_id}", json={"title": "重命名后的目标"})
    assert renamed.status_code == 200
    assert renamed.json()["title"] == "重命名后的目标"
    assert client.post(f"/api/goals/{disposable_id}/archive").json()["status"] == "ARCHIVED"
    assert (
        client.request(
            "DELETE",
            f"/api/goals/{disposable_id}",
            json={"confirmation": "wrong"},
        ).status_code
        == 422
    )
    assert (
        client.request(
            "DELETE",
            f"/api/goals/{disposable_id}",
            json={"confirmation": disposable_id},
        ).status_code
        == 204
    )
    assert client.get(f"/api/goals/{disposable_id}/clarification").status_code == 404


def test_pause_until_date_resumes_from_today_and_shifts_missed_work(
    client, session_factory
) -> None:
    workspace = _completed_plan(client)
    goal_id = workspace["goal_id"]
    original_plan_id = workspace["plan"]["id"]
    client.post(f"/api/goals/{goal_id}/plan/confirm")
    today = date.today()
    weekdays = sorted({today.weekday(), (today.weekday() + 1) % 7})
    client.post(
        f"/api/goals/{goal_id}/execution/start",
        json={
            "requested_start_date": today.isoformat(),
            "execution_weekdays": weekdays,
            "excluded_dates": [],
        },
    )
    client.post(
        f"/api/goals/{goal_id}/pause",
        json={"pause_until": (today + timedelta(days=1)).isoformat()},
    )

    with session_factory() as session:
        goal = session.get(Goal, goal_id)
        activation = session.scalar(select(PlanActivation).where(PlanActivation.goal_id == goal_id))
        first_schedule = session.scalar(
            select(PlanDaySchedule)
            .where(PlanDaySchedule.activation_id == activation.id)
            .order_by(PlanDaySchedule.scheduled_date)
        )
        goal.pause_until = today
        activation.activated_at = datetime.now(UTC) - timedelta(days=1)
        first_schedule.scheduled_date = today - timedelta(days=1)
        session.commit()

    resumed_today = client.get(f"/api/goals/{goal_id}/today")
    assert resumed_today.status_code == 200
    assert resumed_today.json()["status"] == "ACTIVE"
    with session_factory() as session:
        goal = session.get(Goal, goal_id)
        operation = session.scalar(
            select(RecoveryOperation)
            .where(RecoveryOperation.goal_id == goal_id)
            .order_by(RecoveryOperation.created_at.desc())
        )
        assert goal.active_plan_version_id != original_plan_id
        assert goal.pause_until is None
        assert operation.status == "APPLIED"
        assert operation.mode == "SHIFT_FORWARD"


def test_goal_timezone_controls_its_calendar_day(monkeypatch) -> None:
    import planner_api.execution_service as execution_service_module
    import planner_api.plan_service as plan_service_module

    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            moment = cls(2026, 9, 7, 16, 30, tzinfo=UTC)
            return moment if tz is None else moment.astimezone(tz)

    monkeypatch.setattr(execution_service_module, "datetime", FrozenDateTime)
    monkeypatch.setattr(plan_service_module, "datetime", FrozenDateTime)
    shanghai_goal = Goal(raw_input="时区测试", title="时区测试", timezone="Asia/Shanghai")
    los_angeles_goal = Goal(
        raw_input="时区测试",
        title="时区测试",
        timezone="America/Los_Angeles",
    )

    assert ExecutionService._today_for(shanghai_goal) == date(2026, 9, 8)
    assert PlanService._today_for(shanghai_goal) == date(2026, 9, 8)
    assert ExecutionService._today_for(los_angeles_goal) == date(2026, 9, 7)
    assert PlanService._today_for(los_angeles_goal) == date(2026, 9, 7)


def test_recovery_shift_preview_is_read_only_and_apply_is_idempotent(
    client, session_factory
) -> None:
    workspace = _completed_plan(client)
    goal_id = workspace["goal_id"]
    source_plan_id = workspace["plan"]["id"]
    original_titles = [task["title"] for day in workspace["plan"]["days"] for task in day["tasks"]]
    client.post(f"/api/goals/{goal_id}/plan/confirm")
    today = date.today()
    weekdays = sorted({today.weekday(), (today.weekday() + 1) % 7})
    started_response = client.post(
        f"/api/goals/{goal_id}/execution/start",
        json={
            "requested_start_date": (today - timedelta(days=21)).isoformat(),
            "execution_weekdays": weekdays,
            "excluded_dates": [],
        },
    )
    assert started_response.status_code == 200, started_response.text

    first = client.get(f"/api/goals/{goal_id}/execution/recovery/shift-preview")
    second = client.get(f"/api/goals/{goal_id}/execution/recovery/shift-preview")
    assert first.status_code == 200
    assert second.json()["id"] == first.json()["id"]
    preview = first.json()
    assert preview["model_called"] is False
    assert preview["content_changed"] is False
    assert preview["changes"]
    assert preview["projected_end_date"] > preview["original_end_date"]
    assert "MISSED_EXECUTION" in preview["reason_codes"]
    with session_factory() as session:
        goal = session.get(Goal, goal_id)
        assert goal.active_plan_version_id == source_plan_id

    applied = client.post(
        f"/api/goals/{goal_id}/execution/recovery/shift",
        json={"preview_id": preview["id"]},
    )
    repeated = client.post(
        f"/api/goals/{goal_id}/execution/recovery/shift",
        json={"preview_id": preview["id"]},
    )
    assert applied.status_code == 200
    assert repeated.json()["result_plan_version_id"] == applied.json()["result_plan_version_id"]
    calendar = client.get(f"/api/goals/{goal_id}/execution/calendar").json()
    assert all(item["scheduled_date"] >= today.isoformat() for item in calendar["days"])
    plan = client.get(f"/api/goals/{goal_id}/plan").json()["plan"]
    assert plan["revision"] == 2
    assert [task["title"] for day in plan["days"] for task in day["tasks"]] == original_titles
    with session_factory() as session:
        versions = list(session.scalars(select(PlanVersion).where(PlanVersion.goal_id == goal_id)))
        operations = list(
            session.scalars(select(RecoveryOperation).where(RecoveryOperation.goal_id == goal_id))
        )
    assert len(versions) == 2
    assert len(operations) == 1
    assert operations[0].status == "APPLIED"


def test_recovery_defer_persists_and_catchup_keeps_original_end_date(
    client, session_factory
) -> None:
    workspace = _completed_plan(client)
    goal_id = workspace["goal_id"]
    client.post(f"/api/goals/{goal_id}/plan/confirm")
    today = date.today()
    started_response = client.post(
        f"/api/goals/{goal_id}/execution/start",
        json={
            "requested_start_date": (today - timedelta(days=1)).isoformat(),
            "execution_weekdays": sorted({(today - timedelta(days=1)).weekday(), today.weekday()}),
            "excluded_dates": [],
        },
    )
    assert started_response.status_code == 200, started_response.text
    with session_factory() as session:
        activation = session.scalar(select(PlanActivation).where(PlanActivation.goal_id == goal_id))
        activation.activated_at = datetime.now(UTC) - timedelta(days=3)
        session.commit()

    inactivity = client.get(f"/api/goals/{goal_id}/today").json()["inactivity"]
    assert inactivity["missed_dates"] == [(today - timedelta(days=1)).isoformat()]
    deferred = client.post(
        f"/api/goals/{goal_id}/execution/recovery/defer",
        json={"missed_dates": inactivity["missed_dates"]},
    )
    assert deferred.status_code == 200
    assert deferred.json()["recovery_deferred"] is True

    with session_factory() as session:
        plan = session.scalar(select(PlanVersion).where(PlanVersion.goal_id == goal_id))
        first_day = session.scalar(select(PlanDay).where(PlanDay.plan_version_id == plan.id))
        session.add(
            ProgressEvent(
                goal_id=goal_id,
                plan_version_id=plan.id,
                plan_day_id=first_day.id,
                event_type="STARTED",
            )
        )
        session.commit()
    active_again = client.get(f"/api/goals/{goal_id}/today").json()["inactivity"]
    assert active_again["level"] == "NONE"
    assert active_again["recovery_deferred"] is True
    assert active_again["missed_dates"] == inactivity["missed_dates"]

    preview_response = client.get(f"/api/goals/{goal_id}/execution/recovery/catchup-preview")
    assert preview_response.status_code == 200
    preview = preview_response.json()
    assert preview["include_non_execution_days"] is False
    assert preview["programmatic_feasible"] is False
    assert preview["model_required"] is True
    assert preview["required_extra_minutes_per_day"] > 0
    assert {
        "MISSED_EXECUTION",
        "DEADLINE_PROTECTION",
        "CAPACITY_INSUFFICIENT",
    } <= set(preview["reason_codes"])
    with_rest_days = client.get(
        f"/api/goals/{goal_id}/execution/recovery/catchup-preview",
        params={"include_non_execution_days": True},
    ).json()
    assert with_rest_days["include_non_execution_days"] is True
    assert with_rest_days["programmatic_feasible"] is True
    assert any(item["day_type"] == "NON_EXECUTION" for item in with_rest_days["allocation"])
    assert with_rest_days["id"] != preview["id"]
    applied = client.post(
        f"/api/goals/{goal_id}/execution/recovery/catchup",
        json={"preview_id": with_rest_days["id"]},
    )
    assert applied.status_code == 200
    assert applied.json()["result_plan_version_id"]
    calendar = client.get(f"/api/goals/{goal_id}/execution/calendar").json()
    assert calendar["last_date"] == with_rest_days["original_end_date"]
    plan = client.get(f"/api/goals/{goal_id}/plan").json()["plan"]
    assert plan["revision"] == 2
    with session_factory() as session:
        revised = session.scalar(
            select(PlanVersion).where(
                PlanVersion.goal_id == goal_id,
                PlanVersion.status == "CONFIRMED",
            )
        )
        revised_tasks = list(
            session.scalars(select(PlanTask).where(PlanTask.plan_version_id == revised.id))
        )
    assert any(task.status == "CARRIED_OVER" for task in revised_tasks)
    assert any(task.source_task_id is not None for task in revised_tasks)


def test_recovery_ai_creates_structured_brief_without_week_rewrite_batches(
    client, session_factory
) -> None:
    workspace = _completed_plan(client)
    goal_id = workspace["goal_id"]
    assert client.post(f"/api/goals/{goal_id}/plan/confirm").status_code == 200
    response = client.post(
        f"/api/goals/{goal_id}/execution/recovery/ai",
        json={
            "missed_dates": ["2026-09-02", "2026-09-03"],
            "backlog_minutes": 180,
            "original_end_date": "2026-10-11",
            "include_non_execution_days": True,
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["pending_adjustment"] is not None
    with session_factory() as session:
        brief = session.scalar(
            select(PlanningBrief).where(
                PlanningBrief.goal_id == goal_id,
                PlanningBrief.brief_kind == "RECOVERY",
            )
        )
        batches = list(
            session.scalars(
                select(GenerationBatch).where(
                    GenerationBatch.goal_id == goal_id,
                    GenerationBatch.scope_type == "RECOVERY_WEEK",
                )
            )
        )
    assert brief is not None
    assert brief.brief_json["include_non_execution_days"] is True
    assert "recovery_context" in brief.brief_json
    assert batches == []
    preview_response = client.post(f"/api/goals/{goal_id}/plan/adjustment/preview")
    assert preview_response.status_code == 200, preview_response.text
    preview = preview_response.json()
    assert preview["unallocated_count"] >= 0
    repeated = client.post(f"/api/goals/{goal_id}/plan/adjustment/preview").json()
    assert repeated["id"] == preview["id"]
    with session_factory() as session:
        operation = session.get(PlanAdjustmentPreview, preview["id"])
        assert operation is not None
        assert operation.status == "PREVIEW"
        assert (
            session.scalar(
                select(func.count()).select_from(PlanVersion).where(PlanVersion.goal_id == goal_id)
            )
            == 1
        )
    applied = client.post(
        f"/api/goals/{goal_id}/plan/adjustment/apply",
        json={"preview_id": preview["id"], "accept_unallocated": True},
    )
    assert applied.status_code == 200, applied.text
    assert applied.json()["pending_adjustment"] is None
    with session_factory() as session:
        assert session.get(PlanAdjustmentPreview, preview["id"]).status == "APPLIED"


def test_recovery_ai_retries_incomplete_task_ledger(client, session_factory) -> None:
    class IncompleteThenValidProvider(FakePlanDiscussionProvider):
        allocation_calls = 0

        def allocate_recovery(self, context) -> RecoveryAllocationResult:
            self.allocation_calls += 1
            result = super().allocate_recovery(context)
            if self.allocation_calls == 1 and result.assignments:
                return result.model_copy(update={"assignments": result.assignments[:-1]})
            return result

    provider = IncompleteThenValidProvider()

    def override_plan_service():
        with session_factory() as session:
            yield PlanService(session, FakePlanProvider(), provider)

    client.app.dependency_overrides[get_plan_service] = override_plan_service
    workspace = _completed_plan(client)
    goal_id = workspace["goal_id"]
    assert client.post(f"/api/goals/{goal_id}/plan/confirm").status_code == 200
    today = date.today()
    started = client.post(
        f"/api/goals/{goal_id}/execution/start",
        json={
            "requested_start_date": (today - timedelta(days=8)).isoformat(),
            "execution_weekdays": sorted({today.weekday(), (today.weekday() + 1) % 7}),
            "excluded_dates": [],
        },
    )
    assert started.status_code == 200, started.text
    calendar = client.get(f"/api/goals/{goal_id}/execution/calendar").json()
    missed_days = [day for day in calendar["days"] if day["status"] == "MISSED"]
    assert missed_days
    backlog_minutes = sum(
        task["estimate_minutes"]
        for day in missed_days
        for task in day["tasks"]
        if task["status"] != "COMPLETED"
    )

    response = client.post(
        f"/api/goals/{goal_id}/execution/recovery/ai",
        json={
            "missed_dates": [day["scheduled_date"] for day in missed_days],
            "backlog_minutes": backlog_minutes,
            "original_end_date": calendar["last_date"],
            "include_non_execution_days": True,
        },
    )

    assert response.status_code == 200, response.text
    assert response.json()["pending_adjustment"] is not None
    assert provider.allocation_calls == 2


@pytest.mark.parametrize("answer", ["允许顺延，最多三天。", "允许。", "关于这份调整建议，我补充说明：请确认是否允许这次小幅顺延。 允许", "不允许顺延", "BUTTON"])
def test_recovery_can_add_up_to_three_explicit_extension_days(client, session_factory, monkeypatch, answer) -> None:
    workspace = _completed_plan(client)
    goal_id = workspace["goal_id"]
    client.post(f"/api/goals/{goal_id}/plan/confirm")
    today = date.today()
    weekdays = sorted({today.weekday(), (today.weekday() + 1) % 7})
    client.post(
        f"/api/goals/{goal_id}/execution/start",
        json={
            "requested_start_date": today.isoformat(),
            "execution_weekdays": weekdays,
            "excluded_dates": [],
        },
    )
    with session_factory() as session:
        activation = session.scalar(select(PlanActivation).where(PlanActivation.goal_id == goal_id))
        schedules = list(
            session.scalars(
                select(PlanDaySchedule)
                .join(PlanDay, PlanDay.id == PlanDaySchedule.plan_day_id)
                .where(PlanDaySchedule.activation_id == activation.id)
                .order_by(PlanDay.week_index, PlanDay.day_index)
            )
        )
        assigned_dates = [
            today - timedelta(days=4),
            today - timedelta(days=3),
            today - timedelta(days=2),
            today - timedelta(days=1),
            today + timedelta(days=1),
            today + timedelta(days=2),
        ]
        for schedule, assigned_date in zip(schedules, assigned_dates, strict=True):
            schedule.scheduled_date = assigned_date
        activation.projected_end_date = assigned_dates[-1]
        backlog_minutes = 0
        for schedule in schedules[:4]:
            day = session.get(PlanDay, schedule.plan_day_id)
            backlog_minutes += sum(
                task.estimate_minutes
                for task in session.scalars(select(PlanTask).where(PlanTask.plan_day_id == day.id))
            )
            extra = PlanTask(
                plan_version_id=day.plan_version_id,
                plan_day_id=day.id,
                order_in_day=2,
                title=f"额外积压 {day.week_index}-{day.day_index}",
                description="用于验证小幅顺延的积压任务。",
                estimate_minutes=150,
                priority=4,
                task_kind="BUILD",
                is_required=True,
                evidence_json=[],
                depends_on_json=[],
            )
            session.add(extra)
            backlog_minutes += 150
        session.commit()

    proposed = client.post(
        f"/api/goals/{goal_id}/execution/recovery/ai",
        json={
            "missed_dates": [item.isoformat() for item in assigned_dates[:4]],
            "backlog_minutes": backlog_minutes,
            "original_end_date": assigned_dates[-1].isoformat(),
            "include_non_execution_days": False,
        },
    )
    assert proposed.status_code == 200, proposed.text
    assert proposed.json()["pending_adjustment"]["needs_clarification"] is True
    review = proposed.json()["recovery_review"]
    def forbidden(*args, **kwargs):
        raise AssertionError("Saved recovery decisions must not call research or a model")
    monkeypatch.setattr(PlanService, "_prepare_plan_research", forbidden)
    if answer == "BUTTON":
        stale = client.post(f"/api/goals/{goal_id}/plan/recovery-decision", json={"decision": "ALLOW_EXTENSION", "token": "stale"})
        assert stale.status_code == 409
        confirmed = client.post(f"/api/goals/{goal_id}/plan/recovery-decision", json={"decision": "ALLOW_EXTENSION", "token": review["token"]})
    else:
        confirmed = client.post(f"/api/goals/{goal_id}/plan/revise", json={"content": answer})
    if answer == "不允许顺延":
        assert confirmed.status_code == 200, confirmed.text
        assert not confirmed.json()["pending_adjustment"]["can_apply"]
        assert "保持原期限" in confirmed.json()["pending_adjustment"]["clarifying_question"]
        assert client.post(f"/api/goals/{goal_id}/plan/adjustment/preview").status_code == 409
        review = confirmed.json()["recovery_review"]
        confirmed = client.post(f"/api/goals/{goal_id}/plan/recovery-decision", json={"decision": "ALLOW_EXTENSION", "token": review["token"]})
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["pending_adjustment"]["can_apply"] is True

    preview = client.post(f"/api/goals/{goal_id}/plan/adjustment/preview")
    assert preview.status_code == 200, preview.text
    reloaded = client.get(f"/api/goals/{goal_id}/plan").json()
    assert reloaded["pending_adjustment_preview"]["id"] == preview.json()["id"]
    applied = client.post(
        f"/api/goals/{goal_id}/plan/adjustment/apply",
        json={"preview_id": preview.json()["id"], "accept_unallocated": True},
    )

    assert applied.status_code == 200, applied.text
    assert applied.json()["pending_adjustment"] is None
    calendar = client.get(f"/api/goals/{goal_id}/execution/calendar").json()
    assert date.fromisoformat(calendar["last_date"]) <= assigned_dates[-1] + timedelta(days=3)
    assert date.fromisoformat(calendar["last_date"]) > assigned_dates[-1]
    assert applied.json()["plan"]["total_weeks"] > workspace["plan"]["total_weeks"]


def test_recovery_shift_preserves_partial_day_and_creates_carryover_lineage(
    client, session_factory
) -> None:
    workspace = _completed_plan(client)
    goal_id = workspace["goal_id"]
    client.post(f"/api/goals/{goal_id}/plan/confirm")
    today = date.today()
    weekdays = sorted({today.weekday(), (today.weekday() + 1) % 7})
    started = client.post(
        f"/api/goals/{goal_id}/execution/start",
        json={
            "requested_start_date": today.isoformat(),
            "execution_weekdays": weekdays,
            "excluded_dates": [],
        },
    ).json()
    day_id = started["day"]["id"]
    completed_task_id = started["day"]["tasks"][0]["id"]
    with session_factory() as session:
        plan_day = session.get(PlanDay, day_id)
        second = PlanTask(
            plan_version_id=plan_day.plan_version_id,
            plan_day_id=day_id,
            order_in_day=2,
            title="整理剩余错题",
            description="完成当天尚未处理的错题整理。",
            estimate_minutes=30,
            priority=2,
            task_kind="REVIEW",
            is_required=True,
            evidence_json=[],
            depends_on_json=[],
        )
        session.add(second)
        session.commit()
        carry_source_id = second.id
    client.post(f"/api/goals/{goal_id}/tasks/{completed_task_id}/complete")
    with session_factory() as session:
        activation = session.scalar(select(PlanActivation).where(PlanActivation.goal_id == goal_id))
        schedule = session.scalar(
            select(PlanDaySchedule).where(PlanDaySchedule.plan_day_id == day_id)
        )
        schedule.scheduled_date = today - timedelta(days=1)
        activation.activated_at = datetime.now(UTC) - timedelta(days=3)
        session.commit()

    preview = client.get(f"/api/goals/{goal_id}/execution/recovery/shift-preview").json()
    assert day_id in preview["locked_day_ids"]
    assert carry_source_id in preview["carryover_task_ids"]
    applied = client.post(
        f"/api/goals/{goal_id}/execution/recovery/shift",
        json={"preview_id": preview["id"]},
    )
    assert applied.status_code == 200

    calendar = client.get(f"/api/goals/{goal_id}/execution/calendar").json()
    historical_day = next(
        item
        for item in calendar["days"]
        if item["scheduled_date"] == (today - timedelta(days=1)).isoformat()
    )
    assert historical_day["status"] == "PARTIAL"
    assert any(task["status"] == "CARRIED_OVER" for task in historical_day["tasks"])
    assert any(
        item["scheduled_date"] >= today.isoformat() and item["primary_goal"].startswith("接续完成")
        for item in calendar["days"]
    )
    with session_factory() as session:
        goal = session.get(Goal, goal_id)
        carried = session.scalar(
            select(PlanTask).where(
                PlanTask.plan_version_id == goal.active_plan_version_id,
                PlanTask.source_task_id == carry_source_id,
            )
        )
        historical = session.scalar(
            select(PlanTask).where(
                PlanTask.plan_version_id == goal.active_plan_version_id,
                PlanTask.title == "整理剩余错题",
                PlanTask.source_task_id.is_(None),
            )
        )
    assert carried is not None
    assert carried.status == "NOT_STARTED"
    assert historical.status == "CARRIED_OVER"


def test_blocked_task_feedback_is_persisted_without_replanning(client, session_factory) -> None:
    workspace = _completed_plan(client)
    goal_id = workspace["goal_id"]
    client.post(f"/api/goals/{goal_id}/plan/confirm")
    today = date.today()
    weekdays = sorted({today.weekday(), (today.weekday() + 1) % 7})
    started = client.post(
        f"/api/goals/{goal_id}/execution/start",
        json={
            "requested_start_date": today.isoformat(),
            "execution_weekdays": weekdays,
            "excluded_dates": [],
        },
    ).json()
    task_id = started["day"]["tasks"][0]["id"]
    plan_id = started["day"]["id"]

    response = client.post(
        f"/api/goals/{goal_id}/tasks/{task_id}/feedback",
        json={
            "reason_code": "TOO_HARD",
            "intent_code": "SPLIT_TASK",
            "detail_text": "递归部分还不理解",
        },
    )

    assert response.status_code == 200
    result = response.json()
    assert result["day"]["id"] == plan_id
    assert result["day"]["tasks"][0]["status"] == "BLOCKED"
    assert result["feedbacks"][-1]["reason_code"] == "TOO_HARD"
    assert result["feedbacks"][-1]["intent_code"] == "SPLIT_TASK"
    assert result["feedbacks"][-1]["status"] == "PENDING_ADJUSTMENT"

    refreshed = client.get(f"/api/goals/{goal_id}/today").json()
    assert refreshed["feedbacks"][-1]["detail_text"] == "递归部分还不理解"
    with session_factory() as session:
        feedbacks = list(session.scalars(select(Feedback)))
        events = list(session.scalars(select(ProgressEvent)))
    assert len(feedbacks) == 1
    assert [event.event_type for event in events] == [
        "TASK_BLOCKED",
        "FEEDBACK_SUBMITTED",
    ]


def test_feedback_record_can_be_deleted_and_pending_block_is_cleared(client) -> None:
    workspace = _completed_plan(client)
    goal_id = workspace["goal_id"]
    client.post(f"/api/goals/{goal_id}/plan/confirm")
    today = date.today()
    weekdays = sorted({today.weekday(), (today.weekday() + 1) % 7})
    started = client.post(
        f"/api/goals/{goal_id}/execution/start",
        json={
            "requested_start_date": today.isoformat(),
            "execution_weekdays": weekdays,
            "excluded_dates": [],
        },
    ).json()
    task_id = started["day"]["tasks"][0]["id"]
    feedback_workspace = client.post(
        f"/api/goals/{goal_id}/tasks/{task_id}/feedback",
        json={"reason_code": "TOO_HARD", "intent_code": "SPLIT_TASK"},
    ).json()
    feedback_id = feedback_workspace["feedbacks"][0]["id"]
    assert feedback_workspace["adjustments"][0]["status"] == "PENDING_REVIEW"

    response = client.delete(f"/api/goals/{goal_id}/feedback/{feedback_id}")

    assert response.status_code == 200
    result = response.json()
    assert result["feedbacks"] == []
    assert result["adjustments"] == []
    assert result["day"]["tasks"][0]["status"] == "NOT_STARTED"


def test_task_feedback_creates_preview_then_applies_local_split(
    client,
    session_factory,
) -> None:
    workspace = _completed_plan(client)
    goal_id = workspace["goal_id"]
    client.post(f"/api/goals/{goal_id}/plan/confirm")
    today = date.today()
    weekdays = sorted({today.weekday(), (today.weekday() + 1) % 7})
    started = client.post(
        f"/api/goals/{goal_id}/execution/start",
        json={
            "requested_start_date": today.isoformat(),
            "execution_weekdays": weekdays,
            "excluded_dates": [],
        },
    ).json()
    task_id = started["day"]["tasks"][0]["id"]
    original_title = started["day"]["tasks"][0]["title"]
    with session_factory() as session:
        original_task_count = len(list(session.scalars(select(PlanTask))))

    preview = client.post(
        f"/api/goals/{goal_id}/tasks/{task_id}/feedback",
        json={"reason_code": "TOO_HARD", "intent_code": "SPLIT_TASK"},
    ).json()

    assert preview["adjustments"][0]["status"] == "PENDING_REVIEW"
    assert preview["adjustments"][0]["impact"]
    assert preview["day"]["tasks"][0]["title"] == original_title
    adjustment_id = preview["adjustments"][0]["id"]

    applied = client.post(f"/api/goals/{goal_id}/feedback-adjustments/{adjustment_id}/apply").json()

    assert applied["adjustments"][0]["status"] == "APPLIED"
    assert "基础部分" in applied["day"]["tasks"][0]["title"]
    assert applied["day_log"]["adjustments"]
    calendar = client.get(f"/api/goals/{goal_id}/execution/calendar").json()
    changes = [change for day in calendar["days"] for change in day["changes"]]
    assert {change["direction"] for change in changes} == {"SPLIT_FROM", "SPLIT_TO"}
    assert all(change["rationale"] for change in changes)
    with session_factory() as session:
        candidate = session.get(FeedbackAdjustment, adjustment_id)
        tasks = list(session.scalars(select(PlanTask)))
        logs = list(session.scalars(select(ExecutionDayLog)))
    assert candidate.status == "APPLIED"
    assert len(tasks) == original_task_count + 1
    assert len(logs) == 1


def test_focus_history_capacity_and_quick_capture_persist(client, session_factory) -> None:
    workspace = _completed_plan(client)
    goal_id = workspace["goal_id"]
    client.post(f"/api/goals/{goal_id}/plan/confirm")
    today = date.today()
    weekdays = sorted({today.weekday(), (today.weekday() + 1) % 7})
    started = client.post(
        f"/api/goals/{goal_id}/execution/start",
        json={
            "requested_start_date": today.isoformat(),
            "execution_weekdays": weekdays,
            "excluded_dates": [],
        },
    ).json()
    task = started["day"]["tasks"][0]

    assert started["capacity"]["planned_minutes"] > 0
    assert started["capacity"]["remaining_tasks"] == len(started["day"]["tasks"])

    recorded = client.post(
        f"/api/goals/{goal_id}/focus-sessions",
        json={
            "task_id": task["id"],
            "planned_minutes": task["estimate_minutes"],
            "actual_seconds": 901,
            "interruptions": 2,
        },
    )
    assert recorded.status_code == 201
    assert recorded.json()["task_title"] == task["title"]
    assert recorded.json()["status"] == "ENDED_EARLY"
    history = client.get(f"/api/goals/{goal_id}/focus-sessions").json()
    assert history[0]["actual_seconds"] == 901
    assert history[0]["interruptions"] == 2
    refreshed = client.get(f"/api/goals/{goal_id}/today").json()
    assert refreshed["day_log"]["metrics"]["focus_seconds"] == 901
    progress = client.get(f"/api/goals/{goal_id}/execution/progress")
    assert progress.status_code == 200
    progress_data = progress.json()
    assert progress_data["goal_id"] == goal_id
    assert progress_data["trend"]["window_days"] == 1
    assert progress_data["trend"]["focus_minutes"] == 15
    assert progress_data["recent_days"][0]["code"] == "M1-W1-D1"
    assert progress_data["stage_summary"].startswith("当前阶段已完成")
    assert progress_data["overall_summary"].startswith("整个目标已完成")

    created = client.post(
        f"/api/goals/{goal_id}/quick-captures",
        json={"content": "  明天确认接口限流策略  "},
    )
    assert created.status_code == 201
    capture_id = created.json()["id"]
    assert created.json()["content"] == "明天确认接口限流策略"
    assert client.get(f"/api/goals/{goal_id}/quick-captures").json()[0]["id"] == capture_id
    deleted = client.delete(f"/api/goals/{goal_id}/quick-captures/{capture_id}")
    assert deleted.status_code == 200
    assert deleted.json() == []

    with session_factory() as session:
        assert len(list(session.scalars(select(FocusSession)))) == 1
        assert list(session.scalars(select(QuickCapture))) == []


def test_adjustment_can_be_discarded_without_changing_task(client) -> None:
    workspace = _completed_plan(client)
    goal_id = workspace["goal_id"]
    client.post(f"/api/goals/{goal_id}/plan/confirm")
    today = date.today()
    weekdays = sorted({today.weekday(), (today.weekday() + 1) % 7})
    started = client.post(
        f"/api/goals/{goal_id}/execution/start",
        json={
            "requested_start_date": today.isoformat(),
            "execution_weekdays": weekdays,
            "excluded_dates": [],
        },
    ).json()
    task = started["day"]["tasks"][0]
    preview = client.post(
        f"/api/goals/{goal_id}/tasks/{task['id']}/feedback",
        json={"reason_code": "TEMPORARY_CONFLICT", "intent_code": "RESCHEDULE_ONLY"},
    ).json()
    adjustment_id = preview["adjustments"][0]["id"]

    discarded = client.post(
        f"/api/goals/{goal_id}/feedback-adjustments/{adjustment_id}/discard"
    ).json()

    assert discarded["adjustments"][0]["status"] == "DISMISSED"
    assert discarded["day"]["tasks"][0]["id"] == task["id"]


def test_in_progress_task_only_reschedules_the_reported_remainder(client) -> None:
    workspace = _completed_plan(client)
    goal_id = workspace["goal_id"]
    client.post(f"/api/goals/{goal_id}/plan/confirm")
    today = date.today()
    weekdays = sorted({today.weekday(), (today.weekday() + 1) % 7})
    started = client.post(
        f"/api/goals/{goal_id}/execution/start",
        json={
            "requested_start_date": today.isoformat(),
            "execution_weekdays": weekdays,
            "excluded_dates": [],
        },
    ).json()
    task = started["day"]["tasks"][0]
    client.post(f"/api/goals/{goal_id}/tasks/{task['id']}/start")
    feedback = client.post(
        f"/api/goals/{goal_id}/tasks/{task['id']}/feedback",
        json={
            "reason_code": "TEMPORARY_CONFLICT",
            "intent_code": "RESCHEDULE_ONLY",
            "remaining_minutes": 10,
        },
    ).json()

    assert "未来七个自然日" in feedback["adjustments"][0]["rationale"]
    applied = client.post(
        f"/api/goals/{goal_id}/feedback-adjustments/{feedback['adjustments'][0]['id']}/apply"
    ).json()
    assert applied["day"]["tasks"][0]["status"] == "COMPLETED"
    assert applied["day"]["tasks"][0]["estimate_minutes"] == task["estimate_minutes"] - 10
    calendar = client.get(f"/api/goals/{goal_id}/execution/calendar").json()
    remainder = [
        item for day in calendar["days"] for item in day["tasks"] if "剩余部分" in item["title"]
    ]
    assert remainder[0]["estimate_minutes"] == 10


def test_reschedule_uses_the_nearest_execution_day_within_seven_days(client) -> None:
    workspace = _completed_plan(client)
    goal_id = workspace["goal_id"]
    client.post(f"/api/goals/{goal_id}/plan/confirm")
    today = date.today()
    later_weekday = (today.weekday() + 4) % 7
    started = client.post(
        f"/api/goals/{goal_id}/execution/start",
        json={
            "requested_start_date": today.isoformat(),
            "execution_weekdays": sorted({today.weekday(), later_weekday}),
            "excluded_dates": [],
        },
    ).json()
    task = started["day"]["tasks"][0]
    result = client.post(
        f"/api/goals/{goal_id}/tasks/{task['id']}/feedback",
        json={"reason_code": "TEMPORARY_CONFLICT", "intent_code": "RESCHEDULE_ONLY"},
    ).json()

    assert result["adjustments"][-1]["status"] == "PENDING_REVIEW"
    assert "未来七个自然日" in result["adjustments"][-1]["rationale"]
    assert "不会移到七个自然日以外" in result["adjustments"][-1]["impact"]


def test_temporary_conflict_can_create_a_nearby_non_execution_recovery_day(client) -> None:
    workspace = _completed_plan(client)
    goal_id = workspace["goal_id"]
    client.post(f"/api/goals/{goal_id}/plan/confirm")
    today = date.today()
    later_weekday = (today.weekday() + 4) % 7
    started = client.post(
        f"/api/goals/{goal_id}/execution/start",
        json={
            "requested_start_date": today.isoformat(),
            "execution_weekdays": sorted({today.weekday(), later_weekday}),
            "excluded_dates": [],
        },
    ).json()
    task = started["day"]["tasks"][0]
    client.post(f"/api/goals/{goal_id}/tasks/{task['id']}/start")
    preview = client.post(
        f"/api/goals/{goal_id}/tasks/{task['id']}/feedback",
        json={
            "reason_code": "TEMPORARY_CONFLICT",
            "intent_code": "RECOVER_ON_REST_DAY",
            "remaining_minutes": 10,
        },
    ).json()

    adjustment = preview["adjustments"][-1]
    assert adjustment["status"] == "PENDING_REVIEW"
    assert "最近的非执行日" in adjustment["title"]
    applied = client.post(
        f"/api/goals/{goal_id}/feedback-adjustments/{adjustment['id']}/apply"
    ).json()
    assert applied["day"]["tasks"][0]["status"] == "COMPLETED"
    calendar = client.get(f"/api/goals/{goal_id}/execution/calendar").json()
    tomorrow = (today + timedelta(days=1)).isoformat()
    recovery_days = [item for item in calendar["days"] if item["scheduled_date"] == tomorrow]
    assert len(recovery_days) == 1
    assert recovery_days[0]["tasks"][0]["estimate_minutes"] == 10
    assert "剩余部分" in recovery_days[0]["tasks"][0]["title"]


def test_three_consecutive_difficult_days_offer_review_without_applying_it(
    client,
    session_factory,
) -> None:
    workspace = _completed_plan(client)
    goal_id = workspace["goal_id"]
    client.post(f"/api/goals/{goal_id}/plan/confirm")
    today = date.today()
    weekdays = sorted({today.weekday(), (today.weekday() + 1) % 7})
    client.post(
        f"/api/goals/{goal_id}/execution/start",
        json={
            "requested_start_date": today.isoformat(),
            "execution_weekdays": weekdays,
            "excluded_dates": [],
        },
    )
    with session_factory() as session:
        plan = session.scalar(
            select(PlanVersion).where(
                PlanVersion.goal_id == goal_id,
                PlanVersion.status == "CONFIRMED",
            )
        )
        activation = session.scalar(
            select(PlanActivation).where(PlanActivation.plan_version_id == plan.id)
        )
        schedules = list(
            session.scalars(
                select(PlanDaySchedule)
                .where(PlanDaySchedule.activation_id == activation.id)
                .order_by(PlanDaySchedule.scheduled_date)
            )
        )[:3]
        for index, schedule in enumerate(schedules):
            schedule.scheduled_date = today - timedelta(days=2 - index)
            session.add(
                Feedback(
                    goal_id=goal_id,
                    plan_version_id=plan.id,
                    plan_day_id=schedule.plan_day_id,
                    feedback_kind="DAY_PACE",
                    reason_code="TOO_HEAVY",
                    intent_code="OBSERVE",
                    status="RECORDED",
                )
            )
        session.commit()

    progress = client.get(f"/api/goals/{goal_id}/execution/progress").json()
    assert progress["trend"]["status"] == "NEEDS_REVIEW"
    assert progress["trend"]["should_offer_adjustment"] is True
    assert progress["trend"]["summary"] == (
        "最近几次执行有些吃力，可以看看接下来几天是否需要放松一点。"
    )
    assert progress["trend"]["recommendation_title"] == "看看接下来几天怎么调"
    assert progress["trend"]["recommendation_request"]
    assert progress["plan_revision"] == 1


def test_positive_day_pace_feedback_does_not_change_completed_tasks(
    client,
    session_factory,
) -> None:
    workspace = _completed_plan(client)
    goal_id = workspace["goal_id"]
    client.post(f"/api/goals/{goal_id}/plan/confirm")
    today = date.today()
    weekdays = sorted({today.weekday(), (today.weekday() + 1) % 7})
    started = client.post(
        f"/api/goals/{goal_id}/execution/start",
        json={
            "requested_start_date": today.isoformat(),
            "execution_weekdays": weekdays,
            "excluded_dates": [],
        },
    ).json()
    task_id = started["day"]["tasks"][0]["id"]
    client.post(f"/api/goals/{goal_id}/tasks/{task_id}/complete")

    response = client.post(
        f"/api/goals/{goal_id}/today/feedback",
        json={
            "reason_code": "TOO_EASY",
            "intent_code": "OBSERVE",
            "detail_text": "入门部分以前接触过",
        },
    )

    assert response.status_code == 200
    result = response.json()
    assert result["day"]["tasks"][0]["status"] == "COMPLETED"
    assert result["feedbacks"][-1]["feedback_kind"] == "DAY_PACE"
    assert result["feedbacks"][-1]["reason_code"] == "TOO_EASY"
    assert result["feedbacks"][-1]["intent_code"] == "OBSERVE"
    with session_factory() as session:
        feedback = session.scalar(select(Feedback))
    assert feedback.task_id is None


def test_overall_heavy_feedback_proposes_a_visible_near_term_adjustment(client) -> None:
    workspace = _completed_plan(client)
    goal_id = workspace["goal_id"]
    client.post(f"/api/goals/{goal_id}/plan/confirm")
    today = date.today()
    weekdays = sorted({today.weekday(), (today.weekday() + 1) % 7})
    client.post(
        f"/api/goals/{goal_id}/execution/start",
        json={
            "requested_start_date": today.isoformat(),
            "execution_weekdays": weekdays,
            "excluded_dates": [],
        },
    )

    result = client.post(
        f"/api/goals/{goal_id}/today/feedback",
        json={
            "reason_code": "TOO_HARD_OVERALL",
            "intent_code": "LOWER_NEAR_TERM",
            "detail_text": "基础阶段需要更多消化时间",
        },
    ).json()

    assert result["feedbacks"][-1]["status"] == "PENDING_ADJUSTMENT"
    assert result["adjustments"][-1]["status"] == "PENDING_REVIEW"
    assert "下一个执行日" in result["adjustments"][-1]["title"]


def test_custom_day_adjustment_requires_detail(client) -> None:
    workspace = _completed_plan(client)
    goal_id = workspace["goal_id"]

    response = client.post(
        f"/api/goals/{goal_id}/today/feedback",
        json={"reason_code": "CUSTOM_ADJUSTMENT", "intent_code": "LOWER_NEAR_TERM"},
    )

    assert response.status_code == 422


def test_active_plan_can_be_partially_regenerated_without_losing_progress(client) -> None:
    workspace = _completed_plan(client)
    goal_id = workspace["goal_id"]
    client.post(f"/api/goals/{goal_id}/plan/confirm")
    today = date.today()
    weekdays = sorted({today.weekday(), (today.weekday() + 1) % 7})
    started = client.post(
        f"/api/goals/{goal_id}/execution/start",
        json={
            "requested_start_date": today.isoformat(),
            "execution_weekdays": weekdays,
            "excluded_dates": [],
        },
    ).json()
    locked_coordinate = (
        started["scheduled_day"]["week_index"],
        started["scheduled_day"]["day_index"],
    )
    original_primary_goal = started["day"]["primary_goal"]
    original_task_titles = [task["title"] for task in started["day"]["tasks"]]
    first_task_id = started["day"]["tasks"][0]["id"]
    client.post(f"/api/goals/{goal_id}/tasks/{first_task_id}/complete")
    client.post(
        f"/api/goals/{goal_id}/focus-sessions",
        json={
            "task_id": first_task_id,
            "planned_minutes": 10,
            "actual_seconds": 600,
            "interruptions": 0,
        },
    )

    proposed = client.post(
        f"/api/goals/{goal_id}/plan/revise",
        json={"content": "第一阶段多留一些学习时间，减少最后的整理任务。"},
    )

    assert proposed.status_code == 200
    assert proposed.json()["pending_adjustment"]["scope"] == "WEEK"
    applied = client.post(f"/api/goals/{goal_id}/plan/adjustment/apply")
    assert applied.status_code == 200
    result = applied.json()
    assert result["status"] == "ACTIVE"
    assert result["plan"]["status"] == "CONFIRMED"
    assert result["plan"]["revision"] == 2
    locked_day = next(
        day
        for day in result["plan"]["days"]
        if (day["week_index"], day["day_index"]) == locked_coordinate
    )
    assert locked_day["primary_goal"] == original_primary_goal
    assert [task["title"] for task in locked_day["tasks"]] == original_task_titles
    assert any(
        "已按反馈调整" in day["primary_goal"]
        for day in result["plan"]["days"]
        if day["week_index"] == locked_coordinate[0] and day["day_index"] != locked_coordinate[1]
    )

    resumed = client.get(f"/api/goals/{goal_id}/today")
    assert resumed.status_code == 200
    assert resumed.json()["day"]["tasks"][0]["status"] == "COMPLETED"
    focus_history = client.get(f"/api/goals/{goal_id}/focus-sessions").json()
    assert len(focus_history) == 1
    assert focus_history[0]["task_id"] == resumed.json()["day"]["tasks"][0]["id"]
    assert (
        client.get(f"/api/goals/{goal_id}/execution/progress").json()["trend"]["focus_minutes"]
        == 10
    )


def test_other_task_feedback_requires_a_short_detail(client) -> None:
    workspace = _completed_plan(client)
    goal_id = workspace["goal_id"]
    task_id = workspace["plan"]["days"][0]["tasks"][0]["id"]

    response = client.post(
        f"/api/goals/{goal_id}/tasks/{task_id}/feedback",
        json={"reason_code": "OTHER", "detail_text": ""},
    )

    assert response.status_code == 422


def test_plan_cannot_be_confirmed_with_pending_adjustment(client) -> None:
    workspace = _completed_plan(client)
    goal_id = workspace["goal_id"]
    client.post(
        f"/api/goals/{goal_id}/plan/revise",
        json={"content": "请把第 2 周调松一些。"},
    )

    response = client.post(f"/api/goals/{goal_id}/plan/confirm")

    assert response.status_code == 409


def test_plan_requires_a_confirmed_route(client) -> None:
    from test_clarification import create_goal

    workspace = create_goal(client)
    response = client.post(f"/api/goals/{workspace['goal_id']}/plan/draft")
    assert response.status_code == 409


def test_behavior_task_does_not_require_an_artificial_output() -> None:
    day = PlanDayDraft(
        stage_order=1,
        week_index=1,
        day_index=1,
        primary_goal="完成今天安排的有氧训练",
        suggested_minutes=30,
        tasks=[
            PlanTaskDraft(
                order=1,
                title="完成慢跑",
                description="按适合自己的强度完成训练。",
                estimate_minutes=30,
                priority=1,
                task_kind="PRACTICE",
                is_required=True,
                evidence=[],
                depends_on=[],
            )
        ],
        evidence=[],
        do_not=[],
    )

    assert day.evidence == []
    assert day.tasks[0].evidence == []


def test_plan_explanation_is_saved_without_changing_the_plan(client) -> None:
    workspace = _completed_plan(client)
    goal_id = workspace["goal_id"]
    original_plan_id = workspace["plan"]["id"]

    response = client.post(
        f"/api/goals/{goal_id}/plan/explain",
        json={"content": "请解释第一周为什么这样安排。"},
    )

    assert response.status_code == 200
    explained = response.json()
    assert explained["plan"]["id"] == original_plan_id
    assert explained["plan"]["revision"] == 1
    assert [message["role"] for message in explained["messages"]] == ["USER", "ASSISTANT"]
    assert explained["messages"][-1]["metadata"]["intent"] == "EXPLAIN"
    assert len(explained["revision_history"]) == 1


def test_plan_adjustment_proposal_preserves_the_current_version(client, session_factory) -> None:
    workspace = _completed_plan(client)
    goal_id = workspace["goal_id"]
    original_plan_id = workspace["plan"]["id"]

    response = client.post(
        f"/api/goals/{goal_id}/plan/revise",
        json={"content": "请把第 2 周调松一些。"},
    )

    assert response.status_code == 200
    proposed = response.json()
    assert proposed["plan"]["id"] == original_plan_id
    assert proposed["plan"]["revision"] == 1
    assert proposed["plan"]["status"] == "DRAFT"
    assert proposed["pending_adjustment"]["scope"] == "WEEK"
    assert proposed["pending_adjustment"]["affected_weeks"] == [2]
    assert proposed["pending_adjustment"]["can_apply"] is True
    assert len(proposed["revision_history"]) == 1
    assert proposed["messages"][-1]["metadata"]["intent"] == "REVISE"
    assert "adjustment_proposal" in proposed["messages"][-1]["metadata"]

    with session_factory() as session:
        versions = list(session.scalars(select(PlanVersion).where(PlanVersion.goal_id == goal_id)))
    assert len(versions) == 1
    assert versions[0].status == "DRAFT"


def test_recovery_scope_contracts_when_local_window_can_carry_backlog() -> None:
    proposal = PlanAdjustmentProposal(
        assistant_message="原建议扩大了范围，现在需要按结构化容量事实重新检查。",
        user_intent_summary="优先在近期补回积压，并保持后续安排不变。",
        scope="OVERALL",
        affected_stage_orders=[1, 2, 3],
        affected_weeks=[1, 2, 3],
        downstream_affected_weeks=[],
        proposed_changes=["重新安排积压任务"],
        tradeoffs=[],
        unchanged_guarantees=["最终目标不变"],
        total_weeks_change=0,
        weekly_intensity_change="HIGHER",
        buffer_change="LESS",
        needs_clarification=False,
        clarifying_question=None,
        can_apply=True,
    )
    context = {
        "backlog_task_minutes": 60,
        "soft_load_reference_minutes": 150,
        "recommended_local_weeks": [2],
        "recommended_local_days": [{"week_index": 2, "existing_minutes": 90}],
        "candidate_days": [{"week_index": 2, "stage_order": 2}],
    }

    result = PlanService._constrain_recovery_proposal(proposal, context)

    assert result.scope == "WEEK"
    assert result.affected_weeks == [2]
    assert result.downstream_affected_weeks == []
    assert result.affected_stage_orders == [2]


def test_exploratory_adjustment_defers_unmaterialized_weeks(
    client,
    session_factory,
) -> None:
    workspace = _completed_plan(client)
    plan_id = workspace["plan"]["id"]
    proposal = PlanAdjustmentProposal(
        assistant_message="把已经展开的安排改成固定星期结构，未来阶段继续沿用这套规则。",
        user_intent_summary="改成固定星期结构并延续到后续阶段。",
        scope="STAGE",
        affected_stage_orders=[1, 2, 3],
        affected_weeks=[1, 2, 3],
        downstream_affected_weeks=[],
        proposed_changes=["训练改成固定星期结构。"],
        tradeoffs=[],
        unchanged_guarantees=["已完成内容保持不变。"],
        total_weeks_change=0,
        weekly_intensity_change="UNCHANGED",
        buffer_change="UNCHANGED",
        needs_clarification=False,
        clarifying_question=None,
        can_apply=True,
    )

    with session_factory() as session:
        plan = session.get(PlanVersion, plan_id)
        assert plan is not None
        plan.planning_mode = "EXPLORATORY"
        week_three_day_ids = list(
            session.scalars(
                select(PlanDay.id).where(
                    PlanDay.plan_version_id == plan_id,
                    PlanDay.week_index == 3,
                )
            )
        )
        if week_three_day_ids:
            session.execute(delete(PlanTask).where(PlanTask.plan_day_id.in_(week_three_day_ids)))
            session.execute(delete(PlanDay).where(PlanDay.id.in_(week_three_day_ids)))
        session.flush()
        scoped = PlanService(
            session,
            FakePlanProvider(),
            FakePlanDiscussionProvider(),
        )._scope_exploratory_adjustment(plan, proposal)

    assert scoped is not None
    assert scoped.affected_stage_orders == [1, 2]
    assert scoped.affected_weeks == [1, 2]
    assert scoped.downstream_affected_weeks == [3]


def test_recovery_local_window_scales_with_remaining_cycle_and_is_capped() -> None:
    assert PlanService._recovery_local_window_days(0) == 0
    assert PlanService._recovery_local_window_days(1) == 1
    assert PlanService._recovery_local_window_days(3) == 2
    assert PlanService._recovery_local_window_days(10) == 3
    assert PlanService._recovery_local_window_days(16) == 5
    assert PlanService._recovery_local_window_days(40) == 10
    assert PlanService._recovery_local_window_days(100) == 10


def test_recovery_compiler_preserves_task_order_and_minimizes_peak() -> None:
    tasks = [
        {
            "task_id": "a",
            "source_date": "2026-09-01",
            "source_week": 1,
            "source_day": 1,
            "order": 1,
            "estimate_minutes": 60,
        },
        {
            "task_id": "b",
            "source_date": "2026-09-01",
            "source_week": 1,
            "source_day": 1,
            "order": 2,
            "estimate_minutes": 60,
        },
        {
            "task_id": "c",
            "source_date": "2026-09-02",
            "source_week": 1,
            "source_day": 2,
            "order": 1,
            "estimate_minutes": 30,
        },
    ]
    candidates = [
        {
            "plan_day_id": "d1",
            "date": "2026-09-07",
            "week_index": 2,
            "day_index": 1,
            "stage_order": 1,
            "existing_minutes": 90,
        },
        {
            "plan_day_id": "d2",
            "date": "2026-09-08",
            "week_index": 2,
            "day_index": 2,
            "stage_order": 1,
            "existing_minutes": 90,
        },
    ]

    result = PlanService._ordered_balanced_recovery_assignments(tasks, candidates)

    assert [item["backlog_task_id"] for item in result] == ["a", "b", "c"]
    assert [item["target_date"] for item in result] == [
        "2026-09-07",
        "2026-09-08",
        "2026-09-08",
    ]
    loads = {"2026-09-07": 90, "2026-09-08": 90}
    for item in result:
        loads[item["target_date"]] += item["estimate_minutes"]
    assert max(loads.values()) == 180


def test_plan_detail_rejects_noncanonical_or_forward_dependency() -> None:
    base = {
        "stage_order": 1,
        "week_index": 1,
        "day_index": 1,
        "primary_goal": "完成一个可检查的小成果",
        "suggested_minutes": 60,
        "evidence": [],
        "do_not": [],
    }
    first = PlanTaskDraft(
        order=1,
        title="准备资料",
        description="整理后续任务需要的资料。",
        estimate_minutes=20,
        priority=5,
        task_kind="PREPARE",
        is_required=True,
        evidence=[],
        depends_on=[],
    )
    valid = PlanTaskDraft(
        order=2,
        title="使用资料",
        description="使用已整理资料完成处理。",
        estimate_minutes=20,
        priority=4,
        task_kind="BUILD",
        is_required=True,
        evidence=[],
        depends_on=["M1-W1-D1-T1"],
    )
    PlanDetailResult(days=[PlanDayDraft(**base, tasks=[first, valid])])

    invalid = valid.model_copy(update={"depends_on": ["准备资料"]})
    try:
        PlanDetailResult(days=[PlanDayDraft(**base, tasks=[first, invalid])])
        raise AssertionError("Natural-language dependencies must be rejected")
    except ValueError as error:
        assert "canonical" in str(error)

    forward = first.model_copy(update={"depends_on": ["M1-W1-D1-T2"]})
    try:
        PlanDetailResult(days=[PlanDayDraft(**base, tasks=[forward, valid])])
        raise AssertionError("Forward dependencies must be rejected")
    except ValueError as error:
        assert "earlier task" in str(error)


def test_plan_detail_enforces_period_capacity_instead_of_global_average() -> None:
    plan = SimpleNamespace(
        weekly_execution_days=6,
        temporal_segments_json=[
            {
                "start_week": 1,
                "end_week": 1,
                "weekly_execution_days": 3,
                "suggested_daily_minutes": 120,
            },
            {
                "start_week": 2,
                "end_week": 2,
                "weekly_execution_days": 6,
                "suggested_daily_minutes": 480,
            },
        ],
        commitments_json=[],
    )
    milestone = SimpleNamespace(start_week=1, end_week=2)
    task = PlanTaskDraft(
        order=1,
        title="完成一套练习",
        description="按当前阶段要求完成练习并复盘。",
        estimate_minutes=180,
        priority=1,
        task_kind="PRACTICE",
        is_required=True,
        evidence=["练习记录"],
        depends_on=[],
    )
    day = PlanDayDraft(
        stage_order=1,
        week_index=1,
        day_index=1,
        primary_goal="完成今日练习",
        suggested_minutes=240,
        tasks=[task],
        evidence=[],
        do_not=[],
    )

    try:
        PlanService._validate_detail_result(plan, {1: milestone}, [1], [day])
        raise AssertionError("The early-period daily capacity must be enforced")
    except HTTPException as error:
        assert error.detail["code"] == "PLAN_TEMPORAL_SEGMENT_MINUTES_INVALID"


def test_plan_outline_rejects_capacity_below_daily_commitments() -> None:
    payload = {
        "assistant_message": "已按两个时期整理总体规划，并保留每日刚性训练。",
        "summary": "先在课程期积累基础，再在全日投入期完成整套训练闭环。",
        "total_weeks": 1,
        "weekly_execution_days": 7,
        "weekly_minutes": 1260,
        "milestones": [
            {
                "stage_order": 1,
                "title": "完成备考训练",
                "purpose": "按确认节奏完成可检查的训练成果。",
                "start_week": 1,
                "end_week": 1,
                "outputs": ["训练记录"],
            }
        ],
        "temporal_segments": [
            {
                "label": "课程期",
                "start_week": 1,
                "end_week": 1,
                "date_scope": "第一周",
                "capacity_summary": "普通执行日可投入三小时。",
                "weekly_execution_days": 7,
                "suggested_daily_minutes": 180,
                "frequency_rules": [],
                "rest_rules": [],
                "holiday_rules": [],
            }
        ],
        "commitments": [
            {
                "label": "每日错题复盘",
                "start_week": 1,
                "end_week": 1,
                "cadence": "PER_EXECUTION_DAY",
                "minimum_occurrences": 1,
                "minimum_minutes_per_occurrence": 60,
                "match_terms": ["错题复盘"],
                "source_text": "每天复盘错题一小时。",
            },
            {
                "label": "每日素材积累",
                "start_week": 1,
                "end_week": 1,
                "cadence": "PER_EXECUTION_DAY",
                "minimum_occurrences": 1,
                "minimum_minutes_per_occurrence": 150,
                "match_terms": ["素材积累"],
                "source_text": "每天积累素材两个半小时。",
            },
        ],
        "buffer_strategy": ["保留未使用时间处理波动。"],
    }

    from planner_api.plan_capacity import capacity_conflicts
    result = PlanOutlineResult.model_validate(payload)
    plan = SimpleNamespace(total_weeks=1,
                           weekly_execution_days=result.weekly_execution_days,
                           temporal_segments_json=[item.model_dump() for item in result.temporal_segments],
                           commitments_json=[item.model_dump() for item in result.commitments])
    # Keep the conflicting demand and supply for negotiation, never normalize
    # the 180-minute capacity upward or discard a confirmed promise.
    assert capacity_conflicts(plan)[0]["minimum_minutes"] == 210
    assert capacity_conflicts(plan)[0]["available_minutes"] == 180


def test_plan_detail_allows_only_declared_high_capacity_days() -> None:
    plan = SimpleNamespace(
        weekly_execution_days=7,
        temporal_segments_json=[
            {
                "start_week": 1,
                "end_week": 1,
                "weekly_execution_days": 7,
                "suggested_daily_minutes": 210,
                "exception_days_per_week": 1,
                "exception_daily_minutes": 420,
            }
        ],
        commitments_json=[],
    )
    milestone = SimpleNamespace(start_week=1, end_week=1)

    def day(index: int, minutes: int) -> PlanDayDraft:
        return PlanDayDraft(
            stage_order=1,
            week_index=1,
            day_index=index,
            primary_goal="完成当日可检查训练成果",
            suggested_minutes=minutes,
            tasks=[
                PlanTaskDraft(
                    order=1,
                    title="完成当日训练",
                    description="根据当日容量完成训练并留下记录。",
                    estimate_minutes=min(minutes, 240),
                    priority=1,
                    task_kind="PRACTICE",
                    is_required=True,
                    evidence=["训练记录"],
                    depends_on=[],
                )
            ],
            evidence=["训练记录"],
            do_not=[],
        )

    PlanService._validate_detail_result(
        plan,
        {1: milestone},
        [1],
        [day(1, 210), day(2, 420)],
    )
    try:
        PlanService._validate_detail_result(
            plan,
            {1: milestone},
            [1],
            [day(1, 300), day(2, 420)],
        )
        raise AssertionError("Only one declared high-capacity day may exceed the normal limit")
    except HTTPException as error:
        assert error.detail["code"] == "PLAN_TEMPORAL_SEGMENT_MINUTES_INVALID"


def test_plan_detail_allows_low_capacity_holiday_exception() -> None:
    plan = SimpleNamespace(
        weekly_execution_days=7,
        temporal_segments_json=[
            {
                "start_week": 1,
                "end_week": 1,
                "weekly_execution_days": 7,
                "suggested_daily_minutes": 480,
                "exception_days_per_week": 1,
                "exception_daily_minutes": 180,
            }
        ],
        commitments_json=[],
    )
    milestone = SimpleNamespace(start_week=1, end_week=1)

    def day(index: int, minutes: int) -> PlanDayDraft:
        return PlanDayDraft(
            stage_order=1,
            week_index=1,
            day_index=index,
            primary_goal="完成当日可检查训练成果",
            suggested_minutes=minutes,
            tasks=[
                PlanTaskDraft(
                    order=1,
                    title="完成当日训练",
                    description="按当天可用容量完成训练并保留记录。",
                    estimate_minutes=min(minutes, 240),
                    priority=1,
                    task_kind="PRACTICE",
                    is_required=True,
                    evidence=["训练记录"],
                    depends_on=[],
                )
            ],
            evidence=["训练记录"],
            do_not=[],
        )

    PlanService._validate_detail_result(
        plan,
        {1: milestone},
        [1],
        [
            day(1, 180),
            day(2, 480),
            day(3, 480),
            day(4, 480),
            day(5, 480),
            day(6, 480),
            day(7, 480),
        ],
    )


def test_plan_detail_requires_a_task_for_single_week_milestone() -> None:
    plan = SimpleNamespace(
        weekly_execution_days=2,
        temporal_segments_json=[
            {
                "start_week": 1,
                "end_week": 2,
                "weekly_execution_days": 2,
                "suggested_daily_minutes": 180,
            }
        ],
        commitments_json=[],
    )
    milestones = {
        2: SimpleNamespace(stage_order=2, start_week=1, end_week=2),
        3: SimpleNamespace(stage_order=3, start_week=2, end_week=2),
    }
    day = PlanDayDraft(
        stage_order=2,
        week_index=2,
        day_index=1,
        primary_goal="完成过渡阶段任务",
        suggested_minutes=60,
        tasks=[
            PlanTaskDraft(
                order=1,
                title="完成过渡练习",
                description="留下本周练习记录。",
                estimate_minutes=60,
                priority=1,
                task_kind="PRACTICE",
                is_required=True,
                evidence=["练习记录"],
                depends_on=[],
            )
        ],
        evidence=["练习记录"],
        do_not=[],
    )

    try:
        PlanService._validate_detail_result(plan, milestones, [2], [day])
        raise AssertionError("A milestone that only covers this week must be represented")
    except HTTPException as error:
        assert error.detail["code"] == "PLAN_STAGE_COVERAGE_INVALID"

    # Hybrid can cover a triggered stage with a validated work item, without
    # inventing a fixed execution day before its prerequisites are complete.
    PlanService._validate_detail_result(
        plan, milestones, [2], [day], covered_work_item_stages={3},
    )


def test_holiday_name_cannot_authorize_capacity_but_explicit_override_can() -> None:
    plan = SimpleNamespace(
        weekly_execution_days=7,
        temporal_segments_json=[
            {
                "label": "课程期",
                "start_week": 1,
                "end_week": 3,
                "weekly_execution_days": 7,
                "suggested_daily_minutes": 195,
                "exception_days_per_week": 1,
                "exception_daily_minutes": 240,
                "weekly_capacity_overrides": [],
                "holiday_rules": ["第三周包含中秋假期，可使用连续整块时间。"],
            }
        ],
        commitments_json=[],
    )
    milestone = SimpleNamespace(start_week=1, end_week=3)
    loads = [240, 195, 225, 225, 240, 420, 300]
    days = [
        PlanDayDraft(
            stage_order=1,
            week_index=3,
            day_index=index,
            primary_goal="完成中秋假期本日训练",
            suggested_minutes=minutes,
            tasks=[
                PlanTaskDraft(
                    order=1,
                    title="完成本日训练",
                    description="按假期容量完成任务并保留记录。",
                    estimate_minutes=min(minutes, 240),
                    priority=1,
                    task_kind="PRACTICE",
                    is_required=True,
                    evidence=["训练记录"],
                    depends_on=[],
                )
            ],
            evidence=["训练记录"],
            do_not=[],
        )
        for index, minutes in enumerate(loads, start=1)
    ]

    import pytest
    with pytest.raises(HTTPException):
        PlanService._validate_detail_result(
            plan, {1: milestone}, [3], days,
            "本周利用中秋假期的整块时间完成集中训练。",
        )
    plan.temporal_segments_json[0]["weekly_capacity_overrides"] = [{
        "week_index": 3, "reason": "用户确认第三周假期时间",
        "weekly_execution_days": 7, "suggested_daily_minutes": 195,
        "exception_days_per_week": 6, "exception_daily_minutes": 420,
    }]
    PlanService._validate_detail_result(plan, {1: milestone}, [3], days)
    result = PlanDetailResult(days=days, source_document="中秋假期集中训练")
    PlanService._record_week_capacity_override(plan, 3, result)

    override = plan.temporal_segments_json[0]["weekly_capacity_overrides"][0]
    assert override["week_index"] == 3
    assert override["exception_days_per_week"] == 6
    assert override["exception_daily_minutes"] == 420


def test_plan_detail_enforces_per_execution_day_commitment() -> None:
    plan = SimpleNamespace(
        weekly_execution_days=6,
        temporal_segments_json=[
            {
                "start_week": 2,
                "end_week": 2,
                "weekly_execution_days": 6,
                "suggested_daily_minutes": 480,
            }
        ],
        commitments_json=[
            {
                "label": "每天完成一套卷",
                "start_week": 2,
                "end_week": 2,
                "cadence": "PER_EXECUTION_DAY",
                "minimum_occurrences": 1,
                "maximum_occurrences": 1,
                "match_terms": ["整套试卷", "模拟卷"],
                "source_text": "后期每天完成一套卷。",
            }
        ],
    )
    milestone = SimpleNamespace(start_week=1, end_week=2)
    generic_task = PlanTaskDraft(
        order=1,
        title="专项练习",
        description="完成若干专项题并整理错题。",
        estimate_minutes=180,
        priority=1,
        task_kind="PRACTICE",
        is_required=True,
        evidence=["错题记录"],
        depends_on=[],
    )
    day = PlanDayDraft(
        stage_order=1,
        week_index=2,
        day_index=1,
        primary_goal="完成今日练习",
        suggested_minutes=240,
        tasks=[generic_task],
        evidence=[],
        do_not=[],
    )

    try:
        PlanService._validate_detail_result(plan, {1: milestone}, [2], [day])
        raise AssertionError("A generic practice task must not satisfy the full-paper promise")
    except HTTPException as error:
        assert error.detail["code"] == "PLAN_COMMITMENT_INVALID"

    explicitly_bound = generic_task.model_copy(update={"satisfies_commitments": ["每天完成一套卷"]})
    PlanService._validate_detail_result(
        plan,
        {1: milestone},
        [2],
        [day.model_copy(update={"tasks": [explicitly_bound]})],
    )

    full_paper = generic_task.model_copy(
        update={
            "title": "完成一整套模拟卷",
            "description": "限时完成一整套模拟卷并订正。",
        }
    )
    compliant = day.model_copy(update={"tasks": [full_paper]})
    PlanService._validate_detail_result(plan, {1: milestone}, [2], [compliant])
    paper_review = full_paper.model_copy(
        update={
            "order": 2,
            "title": "复盘整套模拟卷",
            "estimate_minutes": 30,
        }
    )
    multi_step_paper = day.model_copy(
        update={"suggested_minutes": 240, "tasks": [full_paper, paper_review]}
    )
    PlanService._validate_detail_result(plan, {1: milestone}, [2], [multi_step_paper])


def test_separate_commitment_day_is_exempt_from_ordinary_daily_commitments() -> None:
    daily_label = "普通执行日完成小训练"
    separate_label = "每周单独一天完成整套试卷"
    plan = SimpleNamespace(
        weekly_execution_days=2,
        temporal_segments_json=[
            {
                "start_week": 1,
                "end_week": 1,
                "weekly_execution_days": 2,
                "suggested_daily_minutes": 180,
                "exception_days_per_week": 1,
                "exception_daily_minutes": 240,
            }
        ],
        commitments_json=[
            {
                "label": daily_label,
                "start_week": 1,
                "end_week": 1,
                "cadence": "PER_EXECUTION_DAY",
                "minimum_occurrences": 1,
                "maximum_occurrences": None,
                "requires_separate_execution_day": False,
                "match_terms": ["小训练"],
            },
            {
                "label": separate_label,
                "start_week": 1,
                "end_week": 1,
                "cadence": "PER_WEEK",
                "minimum_occurrences": 1,
                "maximum_occurrences": 1,
                "requires_separate_execution_day": True,
                "match_terms": ["整套试卷"],
            },
        ],
    )
    milestone = SimpleNamespace(start_week=1, end_week=1)
    paper = PlanTaskDraft(
        order=1,
        title="完成整套试卷",
        description="在独占执行日限时完成一整套试卷。",
        estimate_minutes=240,
        priority=1,
        task_kind="PRACTICE",
        is_required=True,
        evidence=["答题记录"],
        depends_on=[],
        satisfies_commitments=[separate_label],
    )
    training = PlanTaskDraft(
        order=1,
        title="完成逻辑小训练",
        description="普通执行日完成一组逻辑小训练。",
        estimate_minutes=60,
        priority=1,
        task_kind="PRACTICE",
        is_required=True,
        evidence=["训练记录"],
        depends_on=[],
        satisfies_commitments=[daily_label],
    )
    paper_day = PlanDayDraft(
        stage_order=1,
        week_index=1,
        day_index=1,
        primary_goal="独立完成本周整套试卷",
        suggested_minutes=240,
        tasks=[paper],
        evidence=["答题记录"],
        do_not=["不混排普通日任务"],
    )
    ordinary_day = PlanDayDraft(
        stage_order=1,
        week_index=1,
        day_index=2,
        primary_goal="完成普通日训练",
        suggested_minutes=60,
        tasks=[training],
        evidence=["训练记录"],
        do_not=[],
    )

    PlanService._validate_detail_result(plan, {1: milestone}, [1], [paper_day, ordinary_day])

    ordinary_without_training = ordinary_day.model_copy(
        update={
            "tasks": [
                training.model_copy(
                    update={
                        "title": "整理学习材料",
                        "description": "整理下一次训练需要使用的材料。",
                        "satisfies_commitments": [],
                    }
                )
            ]
        }
    )
    try:
        PlanService._validate_detail_result(
            plan, {1: milestone}, [1], [paper_day, ordinary_without_training]
        )
        raise AssertionError("An ordinary execution day may not omit its daily commitment")
    except HTTPException as error:
        assert error.detail["code"] == "PLAN_COMMITMENT_INVALID"


def test_commitment_fallback_matches_short_chinese_semantic_phrases() -> None:
    task = SimpleNamespace(
        title="整理行测错题",
        description="归纳错误原因并留下订正记录。",
        evidence_json=["错题清单"],
        satisfies_commitments_json=[],
    )

    assert PlanService._task_matches_commitment(
        task,
        ["错题复盘", "行测复盘"],
        "每日行测错题复盘",
    )
    assert not PlanService._task_matches_commitment(
        task,
        ["完整套卷", "模拟试卷"],
        "每日完成一套卷",
    )


def test_explicit_commitment_bindings_take_priority_over_fuzzy_mentions() -> None:
    label = "每周完成一套完整套卷"
    completed = SimpleNamespace(
        title="完成首套摸底卷",
        description="限时完成并提交答题记录。",
        evidence_json=["答题记录"],
        satisfies_commitments_json=[label],
    )
    preparation = SimpleNamespace(
        title="整理套卷准备材料",
        description="为周日完整套卷准备文具和答题卡。",
        evidence_json=["材料清单"],
        satisfies_commitments_json=[],
    )

    count = PlanService._count_commitment_days(
        [(1, [completed]), (2, [preparation])],
        label,
        ["完整套卷", "限时套卷"],
    )

    assert count == 1


def test_total_commitment_allows_multiple_days_to_build_one_outcome(
    client, session_factory
) -> None:
    workspace = _completed_plan(client)
    with session_factory() as session:
        plan = session.scalar(
            select(PlanVersion).where(PlanVersion.goal_id == workspace["goal_id"])
        )
        label = "完成一个跨多天推进的阶段成果"
        plan.commitments_json = [
            {
                "label": label,
                "start_week": 1,
                "end_week": 3,
                "cadence": "TOTAL",
                "minimum_occurrences": 1,
                "maximum_occurrences": 1,
                "match_terms": ["跨多天阶段成果"],
            }
        ]
        tasks = list(
            session.scalars(
                select(PlanTask).where(PlanTask.plan_version_id == plan.id).order_by(PlanTask.id)
            )
        )
        assert len(tasks) > 1
        for task in tasks[:3]:
            task.satisfies_commitments_json = [label]
        session.commit()

        service = PlanService(session, FakePlanProvider(), FakePlanDiscussionProvider())
        service._reject_invalid_plan_commitments(plan)

        assert service._generated_weeks(plan.id) == {1, 2, 3}


def test_missing_total_commitment_retracts_only_its_target_week(client, session_factory) -> None:
    workspace = _completed_plan(client)
    with session_factory() as session:
        plan = session.scalar(
            select(PlanVersion).where(PlanVersion.goal_id == workspace["goal_id"])
        )
        label = "不存在的唯一校验词 zzzz"
        plan.commitments_json = [
            {
                "label": label,
                "start_week": 1,
                "end_week": 3,
                "cadence": "TOTAL",
                "minimum_occurrences": 1,
                "maximum_occurrences": 1,
                "match_terms": ["zzzz"],
            }
        ]
        session.commit()
        service = PlanService(session, FakePlanProvider(), FakePlanDiscussionProvider())

        try:
            service._reject_invalid_plan_commitments(plan)
            raise AssertionError("A missing stage outcome must request a targeted repair")
        except HTTPException as error:
            assert error.detail["code"] == "PLAN_GLOBAL_COMMITMENT_INVALID"
            assert error.detail["weeks"] == [3]

        assert service._generated_weeks(plan.id) == {1, 2}


def test_repeated_global_commitment_failure_preserves_generated_weeks(
    client, session_factory
) -> None:
    workspace = _completed_plan(client)
    with session_factory() as session:
        plan = session.scalar(
            select(PlanVersion).where(PlanVersion.goal_id == workspace["goal_id"])
        )
        label = "不存在的唯一校验词 zzzz"
        repair_key = f"{label}:TOTAL:[1, 2, 3]"
        plan.commitments_json = [
            {
                "label": label,
                "start_week": 1,
                "end_week": 3,
                "cadence": "TOTAL",
                "minimum_occurrences": 1,
                "maximum_occurrences": 1,
                "match_terms": ["zzzz"],
            }
        ]
        plan.compiler_report_json = {
            **(plan.compiler_report_json or {}),
            "automatic_repairs": [
                {
                    "code": "PLAN_GLOBAL_COMMITMENT_INVALID",
                    "repair_key": repair_key,
                }
            ],
        }
        session.commit()
        service = PlanService(session, FakePlanProvider(), FakePlanDiscussionProvider())

        try:
            service._reject_invalid_plan_commitments(plan)
            raise AssertionError("The same global repair must not delete weeks twice")
        except HTTPException as error:
            assert error.detail["code"] == "PLAN_GLOBAL_COMMITMENT_REPAIR_REPEATED"
            assert error.detail["retryable"] is False

        assert service._generated_weeks(plan.id) == {1, 2, 3}


def test_missing_week_reuses_previously_validated_batch_without_model_call(
    client, session_factory
) -> None:
    workspace = _completed_plan(client)

    class CacheOnlyProvider(FakePlanProvider):
        calls = 0

        def draft_details(self, *args, **kwargs):
            self.calls += 1
            raise AssertionError("A validated cached week must not call the model")

    with session_factory() as session:
        plan = session.scalar(
            select(PlanVersion).where(PlanVersion.goal_id == workspace["goal_id"])
        )
        goal = session.get(Goal, workspace["goal_id"])
        batch = session.scalar(
            select(GenerationBatch).where(
                GenerationBatch.plan_version_id == plan.id,
                GenerationBatch.scope_type == "WEEK",
                GenerationBatch.scope_json["week_index"].as_integer() == 3,
            )
        )
        assert batch is not None
        assert batch.staged_result_json
        for day in session.scalars(
            select(PlanDay).where(
                PlanDay.plan_version_id == plan.id,
                PlanDay.week_index == 3,
            )
        ):
            session.delete(day)
        batch.status = "FAILED"
        batch.failure_code = "MODEL_REQUEST_REJECTED"
        batch.failure_message = "HTTP 402"
        plan.status = "GENERATING"
        goal.status = "PLAN_DRAFTING"
        session.commit()

        provider = CacheOnlyProvider()
        service = PlanService(session, provider, FakePlanDiscussionProvider())
        result = service.draft_next_details(workspace["goal_id"])

        assert result.generation_phase == "COMPLETE"
        assert result.generated_weeks == 3
        assert provider.calls == 0
        assert batch.status == "COMPLETED"
        assert batch.failure_code is None


def test_http_402_generation_failure_has_actionable_feedback() -> None:
    provider_error = ProviderUnavailableError(
        "Solevia 暂时无法完成这次时间安排，请稍后再试。",
        diagnostic="HTTP 402",
        code="MODEL_REQUEST_REJECTED",
    )

    try:
        PlanService._raise_generation_failure(provider_error, "WEEK")
        raise AssertionError("Provider errors must be converted to an API failure")
    except HTTPException as error:
        assert "余额或额度不足" in error.detail["message"]
        assert "只会补齐缺失部分" in error.detail["message"]
        assert error.detail["provider_code"] == "MODEL_REQUEST_REJECTED"


def test_existing_http_402_job_gets_actionable_feedback() -> None:
    now = datetime.now(UTC)
    job = SimpleNamespace(
        id="job-402",
        mode="STANDARD",
        planning_mode="COMMITTED",
        schedule_type="RHYTHM",
        status="FAILED",
        phase="DETAILS",
        generated_weeks=15,
        total_weeks=16,
        error_code="PLAN_WEEK_GENERATION_FAILED",
        error_message="这一批周计划暂时生成失败。",
        diagnostic="W12: HTTP 402",
        created_at=now,
        updated_at=now,
    )

    result = PlanService._generation_job_read(job)

    assert "余额或额度不足" in result.error_message
    assert "只会补齐缺失部分" in result.error_message


def test_recovery_balancer_places_prerequisite_before_dependent_across_dates() -> None:
    tasks = [
        {
            "task_id": "dependent",
            "source_date": "2026-09-01",
            "source_week": 1,
            "source_day": 1,
            "order": 1,
            "estimate_minutes": 30,
            "priority": 5,
            "is_required": True,
            "dependency_constraints": [{"task_id": "prerequisite", "status": "NOT_STARTED"}],
        },
        {
            "task_id": "independent",
            "source_date": "2026-09-01",
            "source_week": 1,
            "source_day": 1,
            "order": 2,
            "estimate_minutes": 30,
            "priority": 5,
            "is_required": True,
            "dependency_constraints": [],
        },
        {
            "task_id": "prerequisite",
            "source_date": "2026-09-02",
            "source_week": 1,
            "source_day": 2,
            "order": 1,
            "estimate_minutes": 60,
            "priority": 1,
            "is_required": True,
            "dependency_constraints": [],
        },
    ]
    candidates = [
        {
            "plan_day_id": "d1",
            "date": "2026-09-10",
            "week_index": 2,
            "day_index": 1,
            "stage_order": 1,
            "existing_minutes": 50,
        },
        {
            "plan_day_id": "d2",
            "date": "2026-09-11",
            "week_index": 2,
            "day_index": 2,
            "stage_order": 1,
            "existing_minutes": 0,
        },
    ]

    result = PlanService._ordered_balanced_recovery_assignments(tasks, candidates)
    positions = {
        item["backlog_task_id"]: (item["target_date"], index) for index, item in enumerate(result)
    }

    assert positions["prerequisite"] < positions["dependent"]
    assert [item["backlog_task_id"] for item in result][0] == "prerequisite"


def test_ai_recovery_blocks_same_day_reverse_dependency_order() -> None:
    context = {
        "backlog_tasks": [
            {"task_id": "prerequisite", "estimate_minutes": 30, "dependency_constraints": []},
            {
                "task_id": "dependent",
                "estimate_minutes": 30,
                "dependency_constraints": [{"task_id": "prerequisite", "status": "NOT_STARTED"}],
            },
        ],
        "allocation_candidate_days": [
            {
                "plan_day_id": "d1",
                "date": "2026-09-10",
                "week_index": 2,
                "day_index": 1,
                "stage_order": 1,
                "existing_minutes": 0,
            }
        ],
        "candidate_days": [
            {
                "plan_day_id": "d1",
                "date": "2026-09-10",
                "week_index": 2,
                "day_index": 1,
                "stage_order": 1,
                "existing_minutes": 0,
            }
        ],
        "soft_load_reference_minutes": 120,
    }
    allocation = RecoveryAllocationResult(
        assistant_message="已生成依赖顺序测试所需的追赶任务安排，请确认。",
        assignments=[
            {
                "backlog_task_id": "dependent",
                "target_plan_day_id": "d1",
                "rationale": "错误地先安排后置任务",
            },
            {
                "backlog_task_id": "prerequisite",
                "target_plan_day_id": "d1",
                "rationale": "后安排前置任务",
            },
        ],
        unassigned_task_ids=[],
        tradeoffs=[],
        needs_clarification=False,
    )

    result = PlanService._normalize_ai_recovery_allocation(context, allocation)

    assert result["dependency_blocked_task_ids"] == ["dependent"]
    assert result["unassigned_task_ids"] == ["dependent"]


def test_invalid_persisted_dependency_retracts_week_for_retry(client, session_factory) -> None:
    workspace = _completed_plan(client)
    goal_id = workspace["goal_id"]
    with session_factory() as session:
        plan = session.scalar(select(PlanVersion).where(PlanVersion.goal_id == goal_id))
        first_day = session.scalar(
            select(PlanDay)
            .where(PlanDay.plan_version_id == plan.id)
            .order_by(PlanDay.week_index, PlanDay.day_index)
        )
        task = session.scalar(
            select(PlanTask)
            .where(PlanTask.plan_day_id == first_day.id)
            .order_by(PlanTask.order_in_day)
        )
        task.depends_on_json = [
            f"M{first_day.stage_order}-W{first_day.week_index}-D{first_day.day_index}-T6"
        ]
        session.commit()
        service = PlanService(session, FakePlanProvider(), FakePlanDiscussionProvider())

        try:
            service._reject_invalid_plan_dependency_weeks(plan)
            raise AssertionError("Invalid dependency coordinates must retract their week")
        except HTTPException as error:
            assert error.status_code == 503
            assert error.detail["code"] == "PLAN_DEPENDENCY_INVALID"

        remaining_weeks = service._generated_weeks(plan.id)
        assert first_day.week_index not in remaining_weeks


def test_week_dependency_error_repairs_without_user_retry(client, session_factory) -> None:
    workspace = _completed_plan(client)
    goal_id = workspace["goal_id"]

    class RepairingProvider(FakePlanProvider):
        calls = 0
        feedback = None

        def draft_details(self, context, outline, start_week, end_week, generation_mode="STANDARD"):
            self.calls += 1
            result = super().draft_details(context, outline, start_week, end_week, generation_mode)
            if self.calls == 1:
                first = result.days[0]
                result.days[1].tasks[0].depends_on = [f"M{first.stage_order}-W{start_week}-D1-T99"]
            else:
                self.feedback = context.planning_context["generation_validation_feedback"]
            return result

    with session_factory() as session:
        plan = session.scalar(select(PlanVersion).where(PlanVersion.goal_id == goal_id))
        days = list(session.scalars(select(PlanDay).where(PlanDay.plan_version_id == plan.id)))
        for day in days:
            if day.week_index == 1:
                session.delete(day)
        plan.status = "GENERATING"
        session.get(Goal, goal_id).status = "PLAN_DRAFTING"
        session.commit()
        provider = RepairingProvider()
        service = PlanService(session, provider, FakePlanDiscussionProvider())
        result = service.draft_next_details(goal_id)
        assert result.generation_phase == "COMPLETE"
        assert provider.calls == 2
        assert provider.feedback["code"] == "PLAN_WEEK_DEPENDENCY_INVALID"


def test_cross_week_repair_expands_to_prerequisite_and_consumers(client, session_factory) -> None:
    workspace = _completed_plan(client)
    goal_id = workspace["goal_id"]

    class ExpandingProvider(FakePlanProvider):
        prerequisite_rebuilt = False
        seen_scopes = []

        def draft_details(self, context, outline, start_week, end_week, generation_mode="STANDARD"):
            feedback = (context.planning_context or {}).get("generation_validation_feedback", {})
            self.seen_scopes.append(feedback.get("repair_scope"))
            if start_week == 1:
                self.prerequisite_rebuilt = True
            result = super().draft_details(context, outline, start_week, end_week, generation_mode)
            if start_week == 2 and not self.prerequisite_rebuilt:
                result.days[0].tasks[0].depends_on = ["M1-W1-D1-T99"]
            return result

    with session_factory() as session:
        plan = session.scalar(select(PlanVersion).where(PlanVersion.goal_id == goal_id))
        day = session.scalar(
            select(PlanDay).where(PlanDay.plan_version_id == plan.id, PlanDay.week_index == 2)
        )
        task = session.scalar(select(PlanTask).where(PlanTask.plan_day_id == day.id))
        task.depends_on_json = ["M1-W1-D1-T99"]
        plan.status = "GENERATING"
        session.get(Goal, goal_id).status = "PLAN_DRAFTING"
        session.commit()
        provider = ExpandingProvider()
        service = PlanService(session, provider, FakePlanDiscussionProvider())
        result = service.draft_next_details(goal_id)
        assert result.generation_phase == "COMPLETE"
        assert provider.prerequisite_rebuilt
        assert "RELATED_WEEKS" in provider.seen_scopes
        assert len(plan.compiler_report_json["automatic_repairs"]) == 3


def test_generation_repair_never_retracts_completed_tasks(client, session_factory) -> None:
    workspace = _completed_plan(client)
    with session_factory() as session:
        plan = session.scalar(
            select(PlanVersion).where(PlanVersion.goal_id == workspace["goal_id"])
        )
        task = session.scalar(select(PlanTask).where(PlanTask.plan_version_id == plan.id))
        task.depends_on_json = ["M1-W1-D1-T99"]
        task.status = "COMPLETED"
        session.commit()
        original_ids = set(
            session.scalars(select(PlanTask.id).where(PlanTask.plan_version_id == plan.id))
        )
        service = PlanService(session, FakePlanProvider(), FakePlanDiscussionProvider())
        try:
            service._reject_invalid_plan_dependency_weeks(plan, expand=True)
            raise AssertionError("Completed tasks must be protected")
        except HTTPException as error:
            assert error.detail["code"] == "PLAN_REPAIR_PROTECTED_EXECUTION"
        assert (
            set(session.scalars(select(PlanTask.id).where(PlanTask.plan_version_id == plan.id)))
            == original_ids
        )


def test_automatic_repair_stops_at_round_limit(client, session_factory, monkeypatch) -> None:
    workspace = _completed_plan(client)
    with session_factory() as session:
        service = PlanService(session, FakePlanProvider(), FakePlanDiscussionProvider())
        calls = []

        def failing(*args, **kwargs):
            calls.append(kwargs)
            raise HTTPException(
                503, {"code": "PLAN_DEPENDENCY_INVALID", "diagnostic": "still invalid"}
            )

        monkeypatch.setattr(service, "_draft_next_details_once", failing)
        try:
            service.draft_next_details(workspace["goal_id"])
            raise AssertionError("Repair must have a finite budget")
        except HTTPException as error:
            assert error.detail["automatic_repair_rounds"] == 3
        assert len(calls) == 4


def test_automatic_repair_does_not_repeat_rejected_api_request(
    client, session_factory, monkeypatch
) -> None:
    workspace = _completed_plan(client)
    with session_factory() as session:
        service = PlanService(session, FakePlanProvider(), FakePlanDiscussionProvider())
        calls = []

        def failing(*args, **kwargs):
            calls.append(1)
            raise HTTPException(
                503,
                {
                    "code": "PLAN_WEEK_GENERATION_FAILED",
                    "retryable": False,
                },
            )

        monkeypatch.setattr(service, "_draft_next_details_once", failing)
        try:
            service.draft_next_details(workspace["goal_id"])
            raise AssertionError("Rejected requests must not be retried")
        except HTTPException as error:
            assert error.detail["retryable"] is False
        assert len(calls) == 1


def test_dependency_expansion_has_finite_scope(client, session_factory, monkeypatch) -> None:
    workspace = _completed_plan(client)
    with session_factory() as session:
        plan = session.scalar(
            select(PlanVersion).where(PlanVersion.goal_id == workspace["goal_id"])
        )
        days = list(session.scalars(select(PlanDay).where(PlanDay.plan_version_id == plan.id)))
        day_by_id = {day.id: day for day in days}
        tasks = list(session.scalars(select(PlanTask).where(PlanTask.plan_version_id == plan.id)))
        for task in tasks:
            day = day_by_id[task.plan_day_id]
            if day.week_index == 2:
                task.depends_on_json = ["M1-W1-D1-T99"]
            elif day.week_index == 3:
                previous = next(item for item in days if item.week_index == 2)
                task.depends_on_json = [f"M{previous.stage_order}-W2-D{previous.day_index}-T1"]
        session.commit()
        monkeypatch.setattr("planner_api.plan_service.PLAN_DEPENDENCY_EXPANSION_WEEK_LIMIT", 2)
        service = PlanService(session, FakePlanProvider(), FakePlanDiscussionProvider())
        try:
            service._reject_invalid_plan_dependency_weeks(plan, expand=True)
            raise AssertionError("Oversized expansion must not delete weeks")
        except HTTPException as error:
            assert error.detail["code"] == "PLAN_DEPENDENCY_REPAIR_SCOPE_LIMIT"
        assert len(service._generated_weeks(plan.id)) == 3


def test_sequential_provider_repair_budget_counts_actual_week_calls(
    client, session_factory, monkeypatch
) -> None:
    workspace = _completed_plan(client)
    with session_factory() as session:
        provider = FakePlanProvider()
        provider.requires_sequential_details = True
        service = PlanService(session, provider, FakePlanDiscussionProvider())
        complete = service.get_workspace(workspace["goal_id"])
        calls = []

        def attempt(*args, **kwargs):
            calls.append(1)
            if len(calls) == 1:
                raise HTTPException(503, {"code": "PLAN_WEEK_GENERATION_FAILED"})
            return complete

        monkeypatch.setattr(service, "_draft_next_details_once", attempt)
        monkeypatch.setattr(service, "_generated_weeks", lambda _: set())
        monkeypatch.setattr("planner_api.plan_service.PLAN_AUTOMATIC_REPAIR_WEEK_BUDGET", 1)
        assert service.draft_next_details(workspace["goal_id"]).generation_phase == "COMPLETE"
        assert len(calls) == 2


def test_recovery_displacement_prioritizes_backlog_and_pushes_original_work() -> None:
    context = {
        "candidate_days": [
            {
                "plan_day_id": "d1",
                "date": "2026-09-08",
                "week_index": 2,
                "day_index": 1,
                "original_tasks": [
                    {"task_id": "a", "title": "A", "estimate_minutes": 60, "movable": True},
                    {"task_id": "b", "title": "B", "estimate_minutes": 30, "movable": True},
                ],
            },
            {
                "plan_day_id": "d2",
                "date": "2026-09-09",
                "week_index": 2,
                "day_index": 2,
                "original_tasks": [
                    {"task_id": "c", "title": "C", "estimate_minutes": 60, "movable": True},
                ],
            },
            {
                "plan_day_id": "d3",
                "date": "2026-09-10",
                "week_index": 2,
                "day_index": 3,
                "original_tasks": [],
            },
        ]
    }
    assignments = [
        {"backlog_task_id": "late", "target_date": "2026-09-08", "estimate_minutes": 120}
    ]

    result = PlanService._plan_recovery_displacement(context, assignments, 150)

    assert result["projected_daily_minutes_after_displacement"] == {
        "2026-09-08": 120,
        "2026-09-09": 150,
        "2026-09-10": 0,
    }
    assert [item["source_task_id"] for item in result["original_task_moves"]] == ["a", "b"]
    assert result["requires_direct_extension"] is False


def test_recovery_displacement_reports_required_direct_extension() -> None:
    context = {
        "candidate_days": [
            {
                "plan_day_id": "last",
                "date": "2026-09-10",
                "week_index": 2,
                "day_index": 3,
                "original_tasks": [
                    {"task_id": "a", "title": "A", "estimate_minutes": 60, "movable": True},
                    {"task_id": "b", "title": "B", "estimate_minutes": 30, "movable": True},
                ],
            }
        ]
    }
    assignments = [
        {"backlog_task_id": "late", "target_date": "2026-09-10", "estimate_minutes": 120}
    ]

    result = PlanService._plan_recovery_displacement(context, assignments, 150)

    assert result["requires_direct_extension"] is True
    assert result["direct_extension_execution_days"] == 1
    assert result["direct_extension_task_groups"] == [["a", "b"]]
    assert result["direct_extension_minutes"] == 90
    assert result["direct_extension_dates"] == ["2026-09-11"]
    assert result["slight_extension_possible"] is True


def test_recovery_displacement_refuses_to_hide_more_than_three_extension_days() -> None:
    context = {
        "original_end_date": "2026-09-10",
        "candidate_days": [
            {
                "plan_day_id": "last",
                "date": "2026-09-10",
                "week_index": 2,
                "day_index": 3,
                "original_tasks": [
                    {
                        "task_id": f"task-{index}",
                        "title": f"Task {index}",
                        "estimate_minutes": 150,
                        "movable": True,
                    }
                    for index in range(4)
                ],
            }
        ],
    }
    assignments = [
        {"backlog_task_id": "late", "target_date": "2026-09-10", "estimate_minutes": 120}
    ]

    result = PlanService._plan_recovery_displacement(context, assignments, 150)

    assert result["direct_extension_execution_days"] == 4
    assert result["direct_extension_dates"][-1] == "2026-09-14"
    assert result["slight_extension_possible"] is False


def test_non_applicable_adjustment_gets_one_semantic_retry(client, session_factory) -> None:
    workspace = _completed_plan(client)
    goal_id = workspace["goal_id"]
    calls = 0

    class RetryDiscussionProvider(FakePlanDiscussionProvider):
        def revise(self, context):
            nonlocal calls
            calls += 1
            result = super().revise(context)
            if calls == 1:
                return result.model_copy(update={"can_apply": False, "needs_clarification": False})
            return result

    def override_plan_service():
        with session_factory() as session:
            yield PlanService(
                session,
                FakePlanProvider(),
                RetryDiscussionProvider(),
            )

    client.app.dependency_overrides[get_plan_service] = override_plan_service
    response = client.post(
        f"/api/goals/{goal_id}/plan/revise",
        json={"content": "请全局调整时间规划，增加前期训练并压缩最后整理"},
    )

    assert response.status_code == 200
    assert calls == 2
    assert response.json()["pending_adjustment"]["can_apply"] is True


def test_pending_plan_adjustment_can_be_discarded(client) -> None:
    workspace = _completed_plan(client)
    goal_id = workspace["goal_id"]
    original_plan_id = workspace["plan"]["id"]
    proposed = client.post(
        f"/api/goals/{goal_id}/plan/revise",
        json={"content": "请只调整第 1 周。"},
    ).json()
    assert proposed["pending_adjustment"] is not None

    response = client.post(f"/api/goals/{goal_id}/plan/adjustment/discard")

    assert response.status_code == 200
    discarded = response.json()
    assert discarded["pending_adjustment"] is None
    assert discarded["plan"]["id"] == original_plan_id
    assert discarded["plan"]["revision"] == 1
    assert discarded["messages"][-1]["metadata"]["adjustment_dismissed"] is True


def test_follow_up_adjustment_receives_the_previous_proposal(
    client,
    session_factory,
) -> None:
    workspace = _completed_plan(client)
    goal_id = workspace["goal_id"]
    first = client.post(
        f"/api/goals/{goal_id}/plan/revise",
        json={"content": "请只调整第 1 周。"},
    ).json()
    assert first["pending_adjustment"] is not None
    captured: dict[str, object] = {}

    class RefiningDiscussionProvider(FakePlanDiscussionProvider):
        def revise(self, context):
            captured["previous_adjustment"] = context.previous_adjustment
            return super().revise(context)

    def override_plan_service():
        with session_factory() as session:
            yield PlanService(
                session,
                FakePlanProvider(),
                RefiningDiscussionProvider(),
            )

    client.app.dependency_overrides[get_plan_service] = override_plan_service
    response = client.post(
        f"/api/goals/{goal_id}/plan/revise",
        json={"content": "继续补充：第 1 周最多学习三天。"},
    )

    assert response.status_code == 200
    previous = captured["previous_adjustment"]
    assert isinstance(previous, dict)
    assert previous["affected_weeks"] == [1]


def test_applying_adjustment_creates_revision_and_preserves_unaffected_weeks(
    client,
    session_factory,
) -> None:
    workspace = _completed_plan(client)
    goal_id = workspace["goal_id"]
    original = workspace["plan"]
    original_week_one = [day["primary_goal"] for day in original["days"] if day["week_index"] == 1]
    client.post(
        f"/api/goals/{goal_id}/plan/revise",
        json={"content": "请把第 2 周调松一些。"},
    )

    response = client.post(f"/api/goals/{goal_id}/plan/adjustment/apply")

    assert response.status_code == 200
    applied = response.json()
    assert applied["plan"]["id"] != original["id"]
    assert applied["plan"]["revision"] == 2
    assert applied["pending_adjustment"] is None
    assert [
        day["primary_goal"] for day in applied["plan"]["days"] if day["week_index"] == 1
    ] == original_week_one
    assert all(
        "已按反馈调整" in day["primary_goal"]
        for day in applied["plan"]["days"]
        if day["week_index"] == 2
    )
    assert applied["messages"][-1]["metadata"]["adjustment_applied"] is True
    assert applied["messages"][-1]["metadata"]["regenerated_weeks"] == [2]
    assert [item["status"] for item in applied["revision_history"]] == [
        "DRAFT",
        "SUPERSEDED",
    ]
    with session_factory() as session:
        versions = list(session.scalars(select(PlanVersion).where(PlanVersion.goal_id == goal_id)))
    assert len(versions) == 2


def test_adjustment_preview_is_readable_and_does_not_publish(client, session_factory) -> None:
    workspace = _completed_plan(client)
    goal_id = workspace["goal_id"]
    original = workspace["plan"]
    client.post(
        f"/api/goals/{goal_id}/plan/revise",
        json={"content": "请把第 2 周调松一些。"},
    )

    response = client.post(f"/api/goals/{goal_id}/plan/adjustment/preview")

    assert response.status_code == 200, response.text
    preview = response.json()
    assert preview["source_plan_version_id"] == original["id"]
    assert preview["status"] == "PREVIEW"
    assert preview["plan"]["revision"] if "revision" in preview["plan"] else True
    with session_factory() as session:
        assert len(list(session.scalars(select(PlanVersion)))) == 1


def test_adjustment_preview_generates_multi_week_changes_in_repairable_batches(
    client,
    session_factory,
) -> None:
    class MultiWeekDiscussionProvider(FakePlanDiscussionProvider):
        requested_batches: list[list[int]] = []

        def revise(self, context):
            proposal = super().revise(context)
            return proposal.model_copy(
                update={
                    "scope": "STAGE",
                    "affected_weeks": [1, 2],
                    "downstream_affected_weeks": [],
                }
            )

        def apply(self, context):
            requested = list(context.application_requested_weeks or [])
            self.requested_batches.append(requested)
            if len(requested) != 1:
                raise ProviderUnavailableError("预览必须逐周生成。")
            result = super().apply(context)
            if requested == [1]:
                result = result.model_copy(
                    update={
                        "replacement_days": [
                            day.model_copy(update={"stage_order": 3})
                            for day in result.replacement_days
                        ]
                    }
                )
            return result

    provider = MultiWeekDiscussionProvider()

    def override_plan_service():
        with session_factory() as session:
            yield PlanService(session, FakePlanProvider(), provider)

    client.app.dependency_overrides[get_plan_service] = override_plan_service
    workspace = _completed_plan(client)
    goal_id = workspace["goal_id"]
    response = client.post(
        f"/api/goals/{goal_id}/plan/revise",
        json={"content": "调整前两周的执行结构。"},
    )
    assert response.status_code == 200, response.text

    preview = client.post(f"/api/goals/{goal_id}/plan/adjustment/preview")

    assert preview.status_code == 200, preview.text
    assert provider.requested_batches == [[1], [2]]
    preview_days = preview.json()["plan"]["days"]
    original_stage_by_coordinate = {
        (day["week_index"], day["day_index"]): day["stage_order"]
        for day in workspace["plan"]["days"]
    }
    assert all(
        day["stage_order"] == original_stage_by_coordinate[(day["week_index"], day["day_index"])]
        for day in preview_days
    )
    assert all(
        "已按反馈调整" in day["primary_goal"] for day in preview_days if day["week_index"] in {1, 2}
    )
    assert all(
        "已按反馈调整" not in day["primary_goal"] for day in preview_days if day["week_index"] == 3
    )
    repeated = client.post(f"/api/goals/{goal_id}/plan/adjustment/preview")
    assert repeated.status_code == 200, repeated.text
    assert repeated.json()["id"] == preview.json()["id"]
    assert provider.requested_batches == [[1], [2]]


def test_adjustment_preview_can_be_applied_without_regenerating(client, session_factory) -> None:
    workspace = _completed_plan(client)
    goal_id = workspace["goal_id"]
    client.post(
        f"/api/goals/{goal_id}/plan/revise",
        json={"content": "请把第 2 周调松一些。"},
    )
    preview = client.post(f"/api/goals/{goal_id}/plan/adjustment/preview").json()

    applied = client.post(
        f"/api/goals/{goal_id}/plan/adjustment/apply",
        json={"preview_id": preview["id"]},
    )

    assert applied.status_code == 200, applied.text
    assert applied.json()["plan"]["revision"] == 2
    with session_factory() as session:
        assert len(list(session.scalars(select(PlanVersion)))) == 2


def test_active_adjustment_preview_rolls_back_time_blocks_until_confirmed(
    client, session_factory
) -> None:
    workspace = _completed_plan(client)
    goal_id = workspace["goal_id"]
    client.post(f"/api/goals/{goal_id}/plan/confirm")
    started = client.post(
        f"/api/goals/{goal_id}/execution/start",
        json={
            "requested_start_date": date.today().isoformat(),
            "execution_weekdays": [0, 1],
            "excluded_dates": [],
        },
    )
    assert started.status_code == 200, started.text
    client.patch(
        f"/api/goals/{goal_id}/execution/schedule/preferences",
        json={"mode": "BLOCKS"},
    )
    client.put(
        f"/api/goals/{goal_id}/execution/schedule/availability",
        json={
            "windows": [
                {"weekday": weekday, "start_local": "00:00", "end_local": "23:59"}
                for weekday in range(7)
            ]
        },
    )
    client.post(
        f"/api/goals/{goal_id}/plan/revise",
        json={"content": "请把第 2 周调松一些。"},
    )

    preview = client.post(f"/api/goals/{goal_id}/plan/adjustment/preview")
    assert preview.status_code == 200, preview.text
    assert preview.json()["schedule"]
    with session_factory() as session:
        assert len(list(session.scalars(select(PlanVersion)))) == 1
        assert len(list(session.scalars(select(TaskTimeBlock)))) == 0

    applied = client.post(
        f"/api/goals/{goal_id}/plan/adjustment/apply",
        json={"preview_id": preview.json()["id"], "accept_unallocated": True},
    )
    assert applied.status_code == 200, applied.text
    with session_factory() as session:
        assert len(list(session.scalars(select(PlanVersion)))) == 2
        assert len(list(session.scalars(select(TaskTimeBlock)))) > 0


def test_restoring_old_plan_creates_another_revision(client) -> None:
    workspace = _completed_plan(client)
    goal_id = workspace["goal_id"]
    original = workspace["plan"]
    original_goals = [day["primary_goal"] for day in original["days"]]
    client.post(
        f"/api/goals/{goal_id}/plan/revise",
        json={"content": "请把第 2 周调松一些。"},
    )
    applied = client.post(f"/api/goals/{goal_id}/plan/adjustment/apply").json()
    assert applied["plan"]["revision"] == 2

    response = client.post(f"/api/goals/{goal_id}/plan/versions/{original['id']}/restore")

    assert response.status_code == 200
    restored = response.json()
    assert restored["plan"]["revision"] == 3
    assert restored["plan"]["id"] not in {original["id"], applied["plan"]["id"]}
    assert [day["primary_goal"] for day in restored["plan"]["days"]] == original_goals
    assert len(restored["revision_history"]) == 3


def test_failed_adjustment_application_keeps_current_plan(client, session_factory) -> None:
    workspace = _completed_plan(client)
    goal_id = workspace["goal_id"]
    original_plan_id = workspace["plan"]["id"]
    client.post(
        f"/api/goals/{goal_id}/plan/revise",
        json={"content": "请把第 1 周调松一些。"},
    )

    class FailingApplyProvider(FakePlanDiscussionProvider):
        def apply(self, _context):
            raise ProviderUnavailableError("局部重生成暂时失败")

    def override_plan_service():
        with session_factory() as session:
            yield PlanService(session, FakePlanProvider(), FailingApplyProvider())

    client.app.dependency_overrides[get_plan_service] = override_plan_service
    response = client.post(f"/api/goals/{goal_id}/plan/adjustment/apply")

    assert response.status_code == 200
    failed = response.json()
    assert failed["plan"]["id"] == original_plan_id
    assert failed["plan"]["revision"] == 1
    assert failed["pending_adjustment"] is not None
    assert failed["messages"][-1]["generation_status"] == "FAILED"
    assert failed["messages"][-1]["metadata"]["intent"] == "APPLY"


def test_plan_discussion_requires_a_complete_plan(client) -> None:
    route_workspace = _confirmed_route(client)
    response = client.post(
        f"/api/goals/{route_workspace['goal_id']}/plan/explain",
        json={"content": "解释一下。"},
    )
    assert response.status_code == 409


def test_plan_discussion_research_switch_persists_a_bounded_run(client, session_factory) -> None:
    workspace = _completed_plan(client)
    goal_id = workspace["goal_id"]

    response = client.post(
        f"/api/goals/{goal_id}/plan/explain",
        json={"content": "结合外部资料解释当前安排。", "web_research_enabled": True},
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["latest_research"]["status"] in {"SKIPPED", "FAILED"}
    assert payload["latest_research"]["source_count"] == 0
    with session_factory() as session:
        rows = list(session.scalars(select(ResearchRun).where(ResearchRun.goal_id == goal_id)))
        assert len(rows) == 1
        assert rows[0].enabled is True


def test_failed_plan_revision_keeps_the_current_version(client, session_factory) -> None:
    workspace = _completed_plan(client)
    goal_id = workspace["goal_id"]
    original_plan_id = workspace["plan"]["id"]

    class FailingDiscussionProvider:
        def explain(self, _context):
            raise ProviderUnavailableError("解释暂时失败")

        def revise(self, _context):
            raise ProviderUnavailableError("调整暂时失败")

    def override_plan_service():
        with session_factory() as session:
            yield PlanService(session, FakePlanProvider(), FailingDiscussionProvider())

    client.app.dependency_overrides[get_plan_service] = override_plan_service
    response = client.post(
        f"/api/goals/{goal_id}/plan/revise",
        json={"content": "请把第一周调松一些。"},
    )

    assert response.status_code == 200
    failed = response.json()
    assert failed["plan"]["id"] == original_plan_id
    assert failed["plan"]["status"] == "DRAFT"
    assert len(failed["revision_history"]) == 1
    assert failed["messages"][-1]["generation_status"] == "FAILED"


def test_incomplete_draft_resumes_missing_weeks(client, session_factory) -> None:
    route_workspace = _confirmed_route(client)
    goal_id = route_workspace["goal_id"]
    outlined = client.post(f"/api/goals/{goal_id}/plan/draft").json()
    plan_id = outlined["plan"]["id"]
    with session_factory() as session:
        plan = session.get(PlanVersion, plan_id)
        plan.status = "DRAFT"
        session.commit()

    resumed = client.post(f"/api/goals/{goal_id}/plan/details/next")

    assert resumed.status_code == 200
    workspace = resumed.json()
    assert workspace["generation_phase"] == "COMPLETE"
    assert workspace["generated_weeks"] == workspace["total_weeks"]


def test_missing_outline_is_rebuilt_without_deleting_the_parent(client, session_factory) -> None:
    route_workspace = _confirmed_route(client)
    goal_id = route_workspace["goal_id"]
    outlined = client.post(f"/api/goals/{goal_id}/plan/draft").json()
    damaged_id = outlined["plan"]["id"]
    with session_factory() as session:
        session.execute(delete(PlanMilestone).where(PlanMilestone.plan_version_id == damaged_id))
        plan = session.get(PlanVersion, damaged_id)
        plan.status = "DRAFT"
        session.commit()

    rebuilt = client.post(f"/api/goals/{goal_id}/plan/draft")

    assert rebuilt.status_code == 200
    workspace = rebuilt.json()
    assert workspace["plan"]["id"] != damaged_id
    with session_factory() as session:
        damaged = session.get(PlanVersion, damaged_id)
    assert damaged.status == "RECOVERY_SOURCE"
