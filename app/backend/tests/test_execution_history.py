from datetime import date, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from test_execution_discussion import _started_goal

from planner_api.ai import ProviderUnavailableError
from planner_api.execution_discussion_service import ExecutionDiscussionService
from planner_api.models import (
    ExecutionDayLog,
    Feedback,
    FocusSession,
    Goal,
    PlanActivation,
    PlanDay,
    PlanDaySchedule,
    PlanTask,
    PlanVersion,
    ProgressEvent,
    RequirementVersion,
    RouteVersion,
)
from planner_api.plan_ai import FakePlanProvider
from planner_api.plan_discussion_ai import (
    FakePlanDiscussionProvider,
    SemanticPlanDiscussionProvider,
)
from planner_api.plan_service import PlanService
from planner_api.schemas import PlanRevisionResult


def _fixture(client, session_factory):
    goal_id = _started_goal(client)
    with session_factory() as session:
        goal = session.get(Goal, goal_id)
        plan = session.get(PlanVersion, goal.active_plan_version_id)
        days = list(
            session.scalars(
                select(PlanDay)
                .where(PlanDay.plan_version_id == plan.id)
                .order_by(PlanDay.week_index, PlanDay.day_index)
            )
        )
        activation = session.scalar(
            select(PlanActivation).where(PlanActivation.plan_version_id == plan.id)
        )
        for index, day in enumerate(days):
            schedule = session.scalar(
                select(PlanDaySchedule).where(PlanDaySchedule.plan_day_id == day.id)
            )
            schedule.scheduled_date = date.today() + timedelta(days=index - 1)
        activation.projected_end_date = date.today() + timedelta(days=len(days) - 2)
        session.commit()
        return goal_id, plan.id, [day.id for day in days]


def _service(session):
    return PlanService(session, FakePlanProvider(), FakePlanDiscussionProvider())


def _revision(service, source):
    payload = service._plan_payload(source)
    payload["change_summary"] = ["测试调整，保留执行历史"]
    return PlanRevisionResult.model_validate(payload)


@pytest.mark.parametrize("status", ["COMPLETED", "IN_PROGRESS", "CANCELLED", "CARRIED_OVER"])
def test_future_execution_status_is_locked_and_migrated(client, session_factory, status):
    goal_id, plan_id, day_ids = _fixture(client, session_factory)
    with session_factory() as session:
        source = session.get(PlanVersion, plan_id)
        task = session.scalar(select(PlanTask).where(PlanTask.plan_day_id == day_ids[-1]))
        task.status = status
        session.flush()
        service = _service(session)
        day = session.get(PlanDay, task.plan_day_id)
        assert (day.week_index, day.day_index) in service._locked_execution_coordinates(source)
        result = _revision(service, source)
        route = session.get(RouteVersion, source.route_version_id)
        revised = service._persist_revision(
            session.get(Goal, goal_id),
            route,
            service._route_stages(route.id),
            source,
            result,
            "test",
        )
        service._migrate_active_execution(session.get(Goal, goal_id), source, revised)
        migrated_day = session.scalar(
            select(PlanDay).where(
                PlanDay.plan_version_id == revised.id,
                PlanDay.week_index == day.week_index,
                PlanDay.day_index == day.day_index,
            )
        )
        migrated = session.scalar(
            select(PlanTask).where(
                PlanTask.plan_day_id == migrated_day.id,
                PlanTask.order_in_day == task.order_in_day,
            )
        )
        assert migrated.status == status
        assert migrated.lineage_task_id == task.id
        assert migrated.title == task.title


@pytest.mark.parametrize("record", ["log", "event", "feedback", "focus"])
def test_recorded_future_day_is_protected_even_without_status(client, session_factory, record):
    goal_id, plan_id, day_ids = _fixture(client, session_factory)
    with session_factory() as session:
        task = session.scalar(select(PlanTask).where(PlanTask.plan_day_id == day_ids[-1]))
        common = dict(goal_id=goal_id, plan_version_id=plan_id, plan_day_id=task.plan_day_id)
        if record == "log":
            row = ExecutionDayLog(
                **common, scheduled_date=date.today() + timedelta(days=2), summary="保留日志"
            )
        elif record == "event":
            row = ProgressEvent(**common, task_id=task.id, event_type="STARTED")
        elif record == "feedback":
            row = Feedback(**common, task_id=task.id, feedback_kind="TASK", reason_code="CUSTOM")
        else:
            row = FocusSession(
                **common,
                task_id=task.id,
                planned_minutes=20,
                actual_seconds=300,
                status="INTERRUPTED",
            )
        session.add(row)
        session.flush()
        day = session.get(PlanDay, task.plan_day_id)
        service = _service(session)
        source = session.get(PlanVersion, plan_id)
        assert (day.week_index, day.day_index) in service._locked_execution_coordinates(source)
        route = session.get(RouteVersion, source.route_version_id)
        revised = service._persist_revision(
            session.get(Goal, goal_id),
            route,
            service._route_stages(route.id),
            source,
            _revision(service, source),
            "test",
        )
        service._migrate_active_execution(session.get(Goal, goal_id), source, revised)
        assert row.plan_version_id == revised.id
        assert row.plan_day_id != task.plan_day_id
        if record != "log":
            assert row.task_id != task.id
            assert session.get(PlanTask, row.task_id).title == task.title


@pytest.mark.parametrize("weeks,days", [(1, 2), (2, 1)])
def test_shortening_cannot_delete_executed_coordinates(client, session_factory, weeks, days):
    _, plan_id, day_ids = _fixture(client, session_factory)
    with session_factory() as session:
        task = session.scalar(select(PlanTask).where(PlanTask.plan_day_id == day_ids[-1]))
        task.status = "COMPLETED"
        session.flush()
        with pytest.raises(ProviderUnavailableError) as caught:
            _service(session)._validate_execution_bounds(
                session.get(PlanVersion, plan_id), weeks, days
            )
        assert caught.value.code == "EXECUTION_HISTORY_OUT_OF_BOUNDS"
        with pytest.raises(ProviderUnavailableError) as merged_error:
            _service(session)._merge_adjustment_result(
                session.get(PlanVersion, plan_id),
                SimpleNamespace(total_weeks=weeks, weekly_execution_days=days),
            )
        assert merged_error.value.code == "EXECUTION_HISTORY_OUT_OF_BOUNDS"
        assert len(list(session.scalars(select(PlanVersion)))) == 1


def test_shortening_unexecuted_future_does_not_trigger_history_guard(client, session_factory):
    _, plan_id, day_ids = _fixture(client, session_factory)
    with session_factory() as session:
        source = session.get(PlanVersion, plan_id)
        for model in (ExecutionDayLog, ProgressEvent, Feedback, FocusSession):
            for row in session.scalars(select(model)):
                session.delete(row)
        for task in session.scalars(select(PlanTask).where(PlanTask.plan_version_id == plan_id)):
            task.status = "NOT_STARTED"
        first = session.scalar(select(PlanTask).where(PlanTask.plan_day_id == day_ids[0]))
        first.status = "COMPLETED"
        session.flush()
        _service(session)._validate_execution_bounds(source, 1, 1)


def test_migration_rejects_rewriting_executed_content(client, session_factory):
    goal_id, plan_id, day_ids = _fixture(client, session_factory)
    with session_factory() as session:
        task = session.scalar(select(PlanTask).where(PlanTask.plan_day_id == day_ids[-1]))
        task.status = "COMPLETED"
        session.flush()
        service = _service(session)
        source = session.get(PlanVersion, plan_id)
        result = _revision(service, source)
        result.days[-1].tasks[0].title = "错误改写已完成内容"
        route = session.get(RouteVersion, source.route_version_id)
        revised = service._persist_revision(
            session.get(Goal, goal_id),
            route,
            service._route_stages(route.id),
            source,
            result,
            "test",
        )
        with pytest.raises(ProviderUnavailableError) as caught:
            service._migrate_active_execution(session.get(Goal, goal_id), source, revised)
        assert caught.value.code == "EXECUTION_HISTORY_CHANGED"
        assert session.get(Goal, goal_id).active_plan_version_id == source.id


def test_archived_backlog_is_not_cloned_again_on_second_revision(client, session_factory):
    goal_id, plan_id, _ = _fixture(client, session_factory)
    with session_factory() as session:
        service = _service(session)
        source = session.get(PlanVersion, plan_id)
        route = session.get(RouteVersion, source.route_version_id)

        def migrate(current):
            revised = service._persist_revision(
                session.get(Goal, goal_id),
                route,
                service._route_stages(route.id),
                current,
                _revision(service, current),
                "test",
            )
            service._migrate_active_execution(session.get(Goal, goal_id), current, revised)
            return revised

        revised = migrate(source)
        count = len(
            list(
                session.scalars(
                    select(PlanTask).where(
                        PlanTask.plan_version_id == revised.id, PlanTask.title.like("补回：%")
                    )
                )
            )
        )
        assert count > 0
        next_revision = migrate(revised)
        assert (
            len(
                list(
                    session.scalars(
                        select(PlanTask).where(
                            PlanTask.plan_version_id == next_revision.id,
                            PlanTask.title.like("补回：%"),
                        )
                    )
                )
            )
            == count
        )


def test_preview_retries_past_day_rewrite_instead_of_accepting_it(
    client, session_factory, monkeypatch
):
    goal_id, plan_id, _ = _fixture(client, session_factory)
    attempts = []

    class RepairingProvider(FakePlanDiscussionProvider):
        def apply(self, context):
            attempts.append(context.user_input)
            result = super().apply(context)
            if len(attempts) > 1:
                past = {
                    (row["week_index"], row["day_index"])
                    for row in context.current_plan["execution_state"]
                    if row["scheduled_date"] < date.today().isoformat()
                }
                result = result.model_copy(
                    update={
                        "replacement_days": [
                            day
                            for day in result.replacement_days
                            if (day.week_index, day.day_index) not in past
                        ]
                    }
                )
            return result

    with session_factory() as session:
        current = session.get(PlanVersion, plan_id)
        route = session.get(RouteVersion, current.route_version_id)
        requirement = session.get(RequirementVersion, route.requirement_version_id)
        service = PlanService(session, FakePlanProvider(), RepairingProvider())
        monkeypatch.setattr(service, "_locked_execution_coordinates", lambda plan: set())
        context = service._discussion_context(requirement, route, current, "调整第 1 周")
        proposal = service.discussion_provider.revise(context)
        result = service._generate_adjustment_preview_batch(
            requirement, route, current, proposal, [1], current.total_weeks, set()
        )
        assert len(attempts) == 2
        assert "past_replacement_coordinates" in attempts[1]
        assert [(day.week_index, day.day_index) for day in result.replacement_days] == [(1, 2)]


@pytest.mark.parametrize(
    "code,succeeds,expected_attempts",
    [
        ("INVALID_MODEL_RESPONSE", True, 2),
        ("INVALID_MODEL_RESPONSE", False, 2),
        ("PROVIDER_ERROR", False, 1),
    ],
)
def test_adjustment_author_repair_is_bounded_and_skips_transport_failures(
    client,
    session_factory,
    monkeypatch,
    code,
    succeeds,
    expected_attempts,
):
    _, plan_id, _ = _fixture(client, session_factory)
    authors, compilations = [], []

    class Compiler(FakePlanDiscussionProvider):
        def apply(self, context):
            compilations.append(context)
            if not succeeds or len(compilations) == 1:
                raise ProviderUnavailableError(
                    "测试编译失败", code=code, diagnostic="temporal segments overlap"
                )
            return super().apply(context)

    with session_factory() as session:
        service = _service(session)
        current = session.get(PlanVersion, plan_id)
        route = session.get(RouteVersion, current.route_version_id)
        requirement = session.get(RequirementVersion, route.requirement_version_id)
        proposal = service.discussion_provider.revise(
            service._discussion_context(
                requirement,
                route,
                current,
                "调整第 2 周",
            )
        )
        context = service._discussion_context(
            requirement, route, current, "生成预览", proposal, application_requested_weeks=[2]
        )
        provider = SemanticPlanDiscussionProvider(Compiler())

        def author(attempt_context):
            authors.append(attempt_context)
            return f"设计稿 {len(authors)}"

        monkeypatch.setattr(provider, "_draft_apply_document", author)
        if succeeds:
            result = provider.apply(context)
            assert result.compiler_report["author_repair_attempts"] == 1
        else:
            with pytest.raises(ProviderUnavailableError):
                provider.apply(context)
        assert len(authors) == len(compilations) == expected_attempts
        if code == "INVALID_MODEL_RESPONSE":
            feedback = authors[1].planning_context["generation_validation_feedback"]
            assert feedback["previous_source_document"] == "设计稿 1"
            assert feedback["diagnostic"] == "temporal segments overlap"
        assert context.planning_context.get("generation_validation_feedback") is None


def test_execution_discussion_receives_actual_today_and_future_task_states(client, session_factory):
    goal_id, plan_id, day_ids = _fixture(client, session_factory)

    class CapturingProvider(FakePlanDiscussionProvider):
        context = None

        def discuss_execution(self, context):
            self.context = context
            return super().discuss_execution(context)

    provider = CapturingProvider()
    with session_factory() as session:
        today_task = session.scalar(select(PlanTask).where(PlanTask.plan_day_id == day_ids[1]))
        future_task = session.scalar(select(PlanTask).where(PlanTask.plan_day_id == day_ids[-1]))
        today_task.status, future_task.status = "IN_PROGRESS", "COMPLETED"
        session.flush()
        ExecutionDiscussionService(session, provider).discuss(goal_id, "接着执行进度讨论")
        plan_service = _service(session)
        plan = session.get(PlanVersion, plan_id)
        route = session.get(RouteVersion, plan.route_version_id)
        requirement = session.get(RequirementVersion, route.requirement_version_id)
        adjustment_context = plan_service._discussion_context(requirement, route, plan, "整理调整")
        execution_memory = adjustment_context.planning_context["requirement_memory"][
            "execution_review"
        ]
        assert any(
            message["role"] == "USER" and message["content"] == "接着执行进度讨论"
            for message in execution_memory["recent_messages"]
        )
    days = provider.context.current_plan["execution_days"]
    today_entry = next(row for row in days if row["scheduled_date"] == date.today().isoformat())
    assert any(task["status"] == "IN_PROGRESS" for task in today_entry["tasks"])
    assert any(
        task["status"] == "COMPLETED"
        for row in days
        if row["scheduled_date"] > date.today().isoformat()
        for task in row["tasks"]
    )
