from datetime import date

from sqlalchemy import select

from planner_api.models import (
    Goal,
    PlanActivation,
    PlanDay,
    PlanDaySchedule,
    PlanMilestone,
    PlanTask,
    PlanVersion,
    RequirementVersion,
    RouteStage,
    RouteVersion,
    ScheduleChange,
    TaskTimeBlock,
)
from planner_api.schedule_reconciliation import mark_plan_schedule_review


def test_plan_revision_moves_future_block_to_replacement_task_for_review(
    session_factory,
) -> None:
    session = session_factory()
    goal = Goal(title="复核时间块", raw_input="复核时间块", timezone="Asia/Shanghai")
    session.add(goal)
    session.flush()
    requirement = RequirementVersion(
        goal_id=goal.id,
        version=1,
        status="CONFIRMED",
        snapshot_json={},
    )
    session.add(requirement)
    session.flush()
    route = RouteVersion(
        goal_id=goal.id,
        requirement_version_id=requirement.id,
        version=1,
        status="CONFIRMED",
        summary="测试路线",
        prompt_version="test",
    )
    session.add(route)
    session.flush()
    stage = RouteStage(
        route_version_id=route.id,
        order=1,
        title="测试阶段",
        purpose="测试",
    )
    session.add(stage)
    session.flush()
    source = _plan(goal.id, route.id, "旧计划")
    revised = _plan(goal.id, route.id, "新计划")
    session.add_all([source, revised])
    session.flush()
    old_milestone = _milestone(source.id, stage.id, "旧阶段")
    new_milestone = _milestone(revised.id, stage.id, "新阶段")
    session.add_all([old_milestone, new_milestone])
    session.flush()
    old_day = _day(source.id, old_milestone.id, "旧任务")
    new_day = _day(revised.id, new_milestone.id, "新任务")
    session.add_all([old_day, new_day])
    session.flush()
    old_task = _task(source.id, old_day.id, "旧任务")
    session.add(old_task)
    session.flush()
    new_task = _task(revised.id, new_day.id, "新任务", source_task_id=old_task.id)
    session.add(new_task)
    activation = PlanActivation(
        goal_id=goal.id,
        plan_version_id=revised.id,
        requested_start_date=date(2026, 9, 9),
        projected_end_date=date(2026, 9, 11),
        execution_weekdays_json=[4],
    )
    session.add(activation)
    session.flush()
    session.add(
        PlanDaySchedule(
            activation_id=activation.id,
            plan_day_id=new_day.id,
            scheduled_date=date(2026, 9, 11),
        )
    )
    block = TaskTimeBlock(
        goal_id=goal.id,
        plan_version_id=source.id,
        plan_day_id=old_day.id,
        task_id=old_task.id,
        scheduled_date=date(2026, 9, 10),
        start_local="09:00",
        end_local="09:30",
    )
    session.add(block)
    session.flush()

    changed = mark_plan_schedule_review(
        session,
        goal,
        source.id,
        revised.id,
        "计划调整后需要复核",
        today=date(2026, 9, 9),
    )
    session.commit()

    assert changed == 1
    assert block.task_id == new_task.id
    assert block.plan_version_id == revised.id
    assert block.scheduled_date == date(2026, 9, 11)
    assert block.status == "REQUIRES_REVIEW"
    assert session.scalar(select(ScheduleChange).where(ScheduleChange.entity_id == block.id))


def _plan(goal_id: str, route_id: str, summary: str) -> PlanVersion:
    return PlanVersion(
        goal_id=goal_id,
        route_version_id=route_id,
        summary=summary,
        total_weeks=1,
        weekly_execution_days=1,
        weekly_minutes=60,
        total_estimated_minutes=30,
        prompt_version="test",
    )


def _milestone(plan_id: str, stage_id: str, title: str) -> PlanMilestone:
    return PlanMilestone(
        plan_version_id=plan_id,
        route_stage_id=stage_id,
        stage_order=1,
        title=title,
        purpose=title,
        start_week=1,
        end_week=1,
    )


def _day(plan_id: str, milestone_id: str, title: str) -> PlanDay:
    return PlanDay(
        plan_version_id=plan_id,
        milestone_id=milestone_id,
        stage_order=1,
        week_index=1,
        day_index=1,
        primary_goal=title,
        suggested_minutes=30,
    )


def _task(
    plan_id: str,
    day_id: str,
    title: str,
    *,
    source_task_id: str | None = None,
) -> PlanTask:
    return PlanTask(
        plan_version_id=plan_id,
        plan_day_id=day_id,
        source_task_id=source_task_id,
        order_in_day=1,
        title=title,
        description="",
        estimate_minutes=30,
        priority=3,
        task_kind="TASK",
    )
