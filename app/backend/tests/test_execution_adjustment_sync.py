from copy import deepcopy
from datetime import timedelta

import pytest
from sqlalchemy import select

from planner_api.ai import ProviderUnavailableError
from planner_api.api import get_plan_service
from planner_api.execution_discussion_service import ExecutionDiscussionService
from planner_api.execution_service import ExecutionService
from planner_api.models import Goal, Message, PlanActivation, PlanDay, PlanDaySchedule, PlanTask, ProgressEvent, RequirementVersion, RouteVersion
from planner_api.plan_discussion_ai import FakePlanDiscussionProvider
from planner_api.schemas import PlanRevisionResult
from test_execution_discussion import _started_goal


@pytest.mark.parametrize("scenario", ["insert", "repair_old_mapping", "week_full", "deadline"])
def test_hybrid_calendar_revision_stays_in_its_week_and_before_deadline(client, session_factory, scenario):
    goal_id = _started_goal(client)
    with session_factory() as session:
        service = get_plan_service(session)
        goal = session.get(Goal, goal_id)
        source = service._current_plan(goal_id)
        source.execution_model_version = "HYBRID_WORK_ITEM"
        source.schedule_type = "CALENDAR"
        source.compiler_report_json = {**source.compiler_report_json, "week_basis": "ROLLING_START"}
        activation = session.scalar(select(PlanActivation).where(PlanActivation.plan_version_id == source.id))
        anchor = activation.requested_start_date
        activation.execution_weekdays_json = list(range(7))
        source_days = list(session.scalars(select(PlanDay).where(PlanDay.plan_version_id == source.id)))
        by_id = {day.id: day for day in source_days}
        schedules = list(session.scalars(select(PlanDaySchedule).where(PlanDaySchedule.activation_id == activation.id)))
        for schedule in schedules:
            day = by_id[schedule.plan_day_id]
            schedule.scheduled_date = anchor + timedelta(days=(day.week_index - 1) * 7 + day.day_index - 1)
        activation.projected_end_date = max(row.scheduled_date for row in schedules)
        first = min(source_days, key=lambda day: (day.week_index, day.day_index))
        recorded = session.scalar(select(PlanTask).where(PlanTask.plan_day_id == first.id))
        recorded.status = "COMPLETED"
        requirement = session.get(RequirementVersion, goal.current_requirement_version_id)
        requirement.snapshot_json = {**requirement.snapshot_json, "deadline": str(anchor + timedelta(days=source.total_weeks * 7 - 1)), "temporal_context": {}}
        target_week = 2
        week_days = [day for day in source_days if day.week_index == target_week]
        extra_index = max(day.day_index for day in week_days) + 1
        assert extra_index <= 7
        expected = anchor + timedelta(days=7 + extra_index - 1)
        payload = deepcopy(service._plan_payload(source))
        extra = deepcopy(next(day for day in payload["days"] if day["week_index"] == target_week))
        extra.update(day_index=extra_index, primary_goal="拆分后的独立交付")
        payload["days"].append(extra)
        payload["days"].sort(key=lambda day: (day["week_index"], day["day_index"]))
        payload["weekly_execution_days"] = max(source.weekly_execution_days, extra_index)
        payload["change_summary"] = ["拆分第二周的未执行任务"]
        if scenario == "week_full":
            activation.excluded_dates_json = [str(expected + timedelta(days=i)) for i in range(7 - extra_index + 1)]
        elif scenario == "deadline":
            # A date inside the old three-day grace must still respect a hard deadline.
            requirement.snapshot_json = {**requirement.snapshot_json, "deadline": str(expected - timedelta(days=1))}
        if scenario == "repair_old_mapping":
            original_extra = PlanDay(plan_version_id=source.id, milestone_id=week_days[0].milestone_id,
                stage_order=week_days[0].stage_order, week_index=target_week, day_index=extra_index,
                primary_goal=extra["primary_goal"], suggested_minutes=extra["suggested_minutes"])
            session.add(original_extra)
            session.flush()
            session.add(PlanDaySchedule(activation_id=activation.id, plan_day_id=original_extra.id,
                scheduled_date=activation.projected_end_date + timedelta(days=1)))
            source.weekly_execution_days = payload["weekly_execution_days"]
        session.flush()
        route = session.get(RouteVersion, source.route_version_id)
        revised = service._persist_revision(goal, route, service._route_stages(route.id), source,
            PlanRevisionResult.model_validate(payload), "calendar regression")
        if scenario in {"week_full", "deadline"}:
            with pytest.raises(ProviderUnavailableError) as caught:
                service._migrate_active_execution(goal, source, revised)
            assert caught.value.code == ("ADJUSTMENT_WEEK_DATES_EXHAUSTED" if scenario == "week_full" else "ADJUSTMENT_DEADLINE_EXCEEDED")
            assert session.scalar(select(PlanActivation).where(PlanActivation.plan_version_id == revised.id)) is None
        else:
            service._migrate_active_execution(goal, source, revised)
            new_activation = session.scalar(select(PlanActivation).where(PlanActivation.plan_version_id == revised.id))
            new_day = session.scalar(select(PlanDay).where(PlanDay.plan_version_id == revised.id,
                PlanDay.week_index == target_week, PlanDay.day_index == extra_index))
            scheduled = session.scalar(select(PlanDaySchedule).where(PlanDaySchedule.activation_id == new_activation.id,
                PlanDaySchedule.plan_day_id == new_day.id))
            assert scheduled.scheduled_date == expected
            migrated = session.scalar(select(PlanTask).where(PlanTask.plan_version_id == revised.id,
                PlanTask.source_task_id == recorded.id))
            assert migrated.status == "COMPLETED"
        session.rollback()


@pytest.mark.parametrize("change_recorded_action", [False, True])
def test_legacy_duplicate_positions_preserve_task_identity_and_still_protect_actions(
    client, session_factory, change_recorded_action,
):
    goal_id = _started_goal(client)
    with session_factory() as session:
        service = get_plan_service(session)
        goal = session.get(Goal, goal_id)
        source = service._current_plan(goal_id)
        tasks = list(session.scalars(select(PlanTask).join(PlanDay, PlanDay.id == PlanTask.plan_day_id)
            .where(PlanTask.plan_version_id == source.id).order_by(PlanDay.week_index, PlanDay.day_index, PlanTask.order_in_day)))
        day_tasks = [task for task in tasks if task.plan_day_id == tasks[0].plan_day_id]
        duplicate = PlanTask(plan_version_id=source.id, plan_day_id=tasks[0].plan_day_id,
            order_in_day=1, title="另一个已完成动作", description="保留这个动作的独立执行记录",
            estimate_minutes=5, priority=1, task_kind="REVIEW", status="COMPLETED")
        session.add(duplicate)
        session.flush()
        day_tasks.append(duplicate)
        day = session.get(PlanDay, tasks[0].plan_day_id)
        day.suggested_minutes += 5
        for task in day_tasks:
            task.order_in_day = 1
            task.status = "COMPLETED"
        event = ProgressEvent(goal_id=goal_id, plan_version_id=source.id,
            plan_day_id=day_tasks[-1].plan_day_id, task_id=day_tasks[-1].id, event_type="TASK_COMPLETED")
        session.add(event)
        session.flush()
        payload = deepcopy(service._plan_payload(source))
        payload["change_summary"] = ["仅整理任务显示序号，执行动作保持不变"]
        for day in payload["days"]:
            for index, task in enumerate(day["tasks"], 1):
                task["order"] = index
        if change_recorded_action:
            payload["days"][0]["tasks"][0]["description"] = "擅自改写已完成动作"
        result = PlanRevisionResult.model_validate(payload)
        route = session.get(RouteVersion, source.route_version_id)
        revised = service._persist_revision(goal, route, service._route_stages(route.id), source, result, "test")
        if change_recorded_action:
            with pytest.raises(ProviderUnavailableError) as caught:
                service._migrate_active_execution(goal, source, revised)
            assert caught.value.code == "EXECUTION_HISTORY_CHANGED"
        else:
            service._migrate_active_execution(goal, source, revised)
            migrated = session.get(PlanTask, event.task_id)
            assert migrated.source_task_id == day_tasks[-1].id
            assert migrated.title == day_tasks[-1].title
            assert migrated.status == "COMPLETED"
            assert migrated.plan_version_id == revised.id
        session.rollback()


def test_execution_context_uses_published_plan_state_and_tasks_outside_recent_window(client, session_factory):
    goal_id = _started_goal(client)
    with session_factory() as session:
        goal = session.get(Goal, goal_id)
        service = get_plan_service(session)
        plan = service._current_plan(goal_id)
        activation = session.scalar(select(PlanActivation).where(PlanActivation.plan_version_id == plan.id))
        schedules = list(session.scalars(select(PlanDaySchedule).where(PlanDaySchedule.activation_id == activation.id)))
        schedules[-1].scheduled_date = service._today_for_goal_id(goal_id) + timedelta(days=30)
        far_task = session.scalar(select(PlanTask).where(PlanTask.plan_day_id == schedules[-1].plan_day_id))
        conversation = service._ensure_plan_conversation(goal_id)
        session.add(Message(conversation_id=conversation.id, sequence=1, role="ASSISTANT",
            content="旧建议仍待确认", metadata_json={"intent": "REVISE"}))
        session.add(Message(conversation_id=conversation.id, sequence=2, role="ASSISTANT",
            content="调整已经发布", metadata_json={"intent": "APPLY", "adjustment_applied": True,
                "plan_revision": plan.revision}))
        session.flush()
        discussion = ExecutionDiscussionService(session, FakePlanDiscussionProvider())
        execution_conversation = discussion._ensure_conversation(goal_id)
        context = discussion._context(goal, plan, execution_conversation, "现在是否已应用？")
        state = context.current_plan["adjustment_state"]
        assert state["active_plan_id"] == plan.id
        assert state["has_pending_adjustment"] is False
        assert state["last_published_adjustment"]["content"] == "调整已经发布"
        assert any(task["id"] == far_task.id and task["estimate_minutes"] == far_task.estimate_minutes
                   for task in context.current_plan["upcoming_task_index"])
        session.rollback()


def test_revised_calendar_uses_current_tasks_even_without_lineage(client, session_factory):
    goal_id = _started_goal(client)
    with session_factory() as session:
        plan = get_plan_service(session)._current_plan(goal_id)
        plan.revision = 2
        day = session.scalar(select(PlanDay).where(PlanDay.plan_version_id == plan.id))
        day.primary_goal = "旧安排：今天完成第一套真题"
        task = session.scalar(select(PlanTask).where(PlanTask.plan_day_id == day.id))
        task.title = "当前安排：完成阅读练习"
        task.source_task_id = None
        day_id = day.id
        session.commit()
    response = client.get(f"/api/goals/{goal_id}/execution/calendar")
    assert response.status_code == 200
    displayed = next(day for day in response.json()["days"] if day["plan_day_id"] == day_id)
    assert displayed["primary_goal"] == "当前安排：完成阅读练习"
    assert "第一套真题" not in displayed["primary_goal"]


@pytest.mark.parametrize("explicit_origin", [False, True])
def test_calendar_distinguishes_position_and_task_stages_after_recovery(client, session_factory, explicit_origin):
    goal_id = _started_goal(client)
    with session_factory() as session:
        plan = get_plan_service(session)._current_plan(goal_id)
        days = list(session.scalars(select(PlanDay).where(PlanDay.plan_version_id == plan.id)))
        origin, destination = days[:2]
        origin.stage_order = 2
        destination.stage_order = 3
        source = session.scalar(select(PlanTask).where(PlanTask.plan_day_id == origin.id))
        source.origin_stage_order = None
        for task in session.scalars(select(PlanTask).where(PlanTask.plan_day_id == destination.id)):
            task.origin_stage_order = 3
        recovered = PlanTask(plan_version_id=plan.id, plan_day_id=destination.id,
            source_task_id=source.id, origin_stage_order=2 if explicit_origin else None,
            order_in_day=50, title="从第二阶段补回的任务", description="阶段归属不随日期改变",
            estimate_minutes=5, priority=1, task_kind="REVIEW", status="NOT_STARTED")
        archived = PlanTask(plan_version_id=plan.id, plan_day_id=destination.id,
            origin_stage_order=9, order_in_day=51, title="已移走的旧任务", description="仅保留历史",
            estimate_minutes=5, priority=1, task_kind="REVIEW", status="CARRIED_OVER")
        session.add_all([recovered, archived])
        session.flush()
        destination_id, recovered_id = destination.id, recovered.id
        session.commit()
    response = client.get(f"/api/goals/{goal_id}/execution/calendar")
    assert response.status_code == 200
    day = next(day for day in response.json()["days"] if day["plan_day_id"] == destination_id)
    assert day["code"].startswith("M3-")
    assert day["task_stage_orders"] == [2, 3]
    assert next(task for task in day["tasks"] if task["id"] == recovered_id)["origin_stage_order"] == 2


def test_committed_goal_finishes_tasks_without_automatically_planning_more(client, session_factory):
    goal_id = _started_goal(client)
    with session_factory() as session:
        service = ExecutionService(session)
        plan = get_plan_service(session)._current_plan(goal_id)
        goal = session.get(Goal, goal_id)
        assert goal.planning_mode == plan.planning_mode == "COMMITTED"
        versions_before = list(session.scalars(select(type(plan).id).where(type(plan).goal_id == goal_id)))
        from planner_api.models import PlanAdjustmentPreview
        session.add(PlanAdjustmentPreview(goal_id=goal_id, source_plan_version_id=plan.id,
            status="APPLIED", fingerprint="completion-summary-regression", preview_json={}))
        tasks = list(session.scalars(select(PlanTask).where(PlanTask.plan_version_id == plan.id)))
        # A passed final date must not silently complete unfinished actions.
        activation = session.scalar(select(PlanActivation).where(PlanActivation.plan_version_id == plan.id))
        for schedule in session.scalars(select(PlanDaySchedule).where(PlanDaySchedule.activation_id == activation.id)):
            schedule.scheduled_date = service._today_for(goal) - timedelta(days=1)
        for task in tasks[:-1]:
            task.status = "COMPLETED"
        day = session.get(PlanDay, tasks[-1].plan_day_id)
        session.flush()
        service._apply_automatic_completions(goal, plan, day)
        assert goal.status == "ACTIVE"
        tasks[-1].status = "COMPLETED"
        session.flush()
        service._apply_automatic_completions(goal, plan, day)
        assert goal.status == "COMPLETED"
        assert goal.completed_at is not None
        assert "采用 1 次调整" in goal.completion_summary
        original_summary = goal.completion_summary.replace("采用 1 次调整", "采用 0 次调整")
        goal.completion_summary = original_summary
        assert "采用 1 次调整" in service._lifecycle_read(goal).completion_summary
        assert goal.completion_summary == original_summary
        goal.completion_summary = "我选择保留这段个人收获，不改写。"
        assert service._lifecycle_read(goal).completion_summary == goal.completion_summary
        assert goal.active_plan_version_id == plan.id
        assert list(session.scalars(select(type(plan).id).where(type(plan).goal_id == goal_id))) == versions_before
        session.rollback()


def test_several_backlog_clones_on_one_day_receive_distinct_positions(client, session_factory):
    goal_id = _started_goal(client)
    with session_factory() as session:
        service = get_plan_service(session)
        goal = session.get(Goal, goal_id)
        source = service._current_plan(goal_id)
        activation = session.scalar(select(PlanActivation).where(PlanActivation.plan_version_id == source.id))
        schedules = list(session.scalars(select(PlanDaySchedule).where(
            PlanDaySchedule.activation_id == activation.id).order_by(PlanDaySchedule.scheduled_date)))
        today = service._today_for_goal_id(goal_id)
        for index, schedule in enumerate(schedules):
            schedule.scheduled_date = today + timedelta(days=index - 1)
        day = session.get(PlanDay, schedules[0].plan_day_id)
        completed = session.scalar(select(PlanTask).where(PlanTask.plan_day_id == day.id))
        completed.status = "COMPLETED"
        completed.order_in_day = 1
        first = PlanTask(plan_version_id=source.id, plan_day_id=day.id,
            order_in_day=1, title="第一项待承接任务", description="保留独立动作",
            estimate_minutes=5, priority=1, task_kind="REVIEW", status="NOT_STARTED")
        extra = PlanTask(plan_version_id=source.id, plan_day_id=day.id,
            order_in_day=1, title="另一项待承接任务", description="保留独立动作",
            estimate_minutes=5, priority=1, task_kind="REVIEW", status="NOT_STARTED")
        session.add_all([first, extra])
        day.suggested_minutes += 10
        session.flush()
        target = session.get(PlanDay, schedules[-1].plan_day_id)
        payload = deepcopy(service._plan_payload(source))
        payload["change_summary"] = ["把两项遗留任务承接到同一天"]
        for payload_day in payload["days"]:
            for index, task in enumerate(payload_day["tasks"], 1):
                task["order"] = index
        route = session.get(RouteVersion, source.route_version_id)
        revised = service._persist_revision(goal, route, service._route_stages(route.id), source,
            PlanRevisionResult.model_validate(payload), "test")
        service._migrate_active_execution(goal, source, revised, recovery_context={
            "proposed_assignments": [{"backlog_task_id": task.id,
                "target_week": target.week_index, "target_day": target.day_index} for task in (first, extra)],
        })
        clones = list(session.scalars(select(PlanTask).where(PlanTask.plan_version_id == revised.id,
            PlanTask.source_task_id.in_([first.id, extra.id]), PlanTask.status != "CARRIED_OVER")))
        assert len(clones) == 2
        assert len({task.plan_day_id for task in clones}) == 1
        positions = list(session.scalars(select(PlanTask.order_in_day).where(PlanTask.plan_day_id == clones[0].plan_day_id)))
        assert len(positions) == len(set(positions))
        preserved = session.scalar(select(PlanTask).where(PlanTask.plan_version_id == revised.id,
            PlanTask.source_task_id == completed.id))
        assert preserved.status == "COMPLETED"
        session.rollback()
