from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from planner_api.models import (
    Goal,
    PlanActivation,
    PlanDay,
    PlanDaySchedule,
    PlanTask,
    ScheduleChange,
    TaskTimeBlock,
)

SCHEDULE_PLANNED = "PLANNED"
SCHEDULE_REQUIRES_REVIEW = "REQUIRES_REVIEW"
SCHEDULE_CANCELLED = "CANCELLED"
SCHEDULE_COMPLETED = "COMPLETED"


def changed_schedule_dates(session: Session, source_id: str, result_id: str) -> set[date]:
    """Compare task content by execution date, not cloned row identities."""
    def layout(plan_id: str) -> dict[date, list[tuple]]:
        dates = dict(session.execute(
            select(PlanDaySchedule.plan_day_id, PlanDaySchedule.scheduled_date)
            .join(PlanActivation, PlanActivation.id == PlanDaySchedule.activation_id)
            .where(PlanActivation.plan_version_id == plan_id)
        ).all())
        result: dict[date, list[tuple]] = {}
        for task in session.scalars(select(PlanTask).where(
            PlanTask.plan_version_id == plan_id,
        ).order_by(PlanTask.order_in_day, PlanTask.title)):
            day = dates.get(task.plan_day_id)
            if day is not None and task.status not in {"COMPLETED", "CANCELLED", "CARRIED_OVER"}:
                result.setdefault(day, []).append((
                    task.title, task.description, task.estimate_minutes, task.order_in_day,
                    task.priority, task.ai_schedule_requested, task.depends_on_json,
                ))
        return result

    session.flush()
    before, after = layout(source_id), layout(result_id)
    return {day for day in before.keys() | after.keys() if before.get(day) != after.get(day)}


def mark_plan_schedule_review(
    session: Session,
    goal: Goal,
    source_plan_version_id: str,
    result_plan_version_id: str,
    reason: str,
    *,
    today: date | None = None,
) -> int:
    """Invalidate future slots from a superseded plan without rewriting history.

    A plan revision changes task/day identity, so old future slots cannot be
    treated as authoritative. They remain as audit history and become
    actionable again only after the user schedules the task in the new plan.
    """
    local_today = today or datetime.now(ZoneInfo(goal.timezone)).date()
    session.flush()
    blocks = list(
        session.scalars(
            select(TaskTimeBlock).where(
                TaskTimeBlock.goal_id == goal.id,
                TaskTimeBlock.plan_version_id == source_plan_version_id,
                TaskTimeBlock.scheduled_date >= local_today,
                TaskTimeBlock.status.in_([SCHEDULE_PLANNED, SCHEDULE_REQUIRES_REVIEW]),
            )
        )
    )
    old_tasks = {
        item.id: item
        for item in session.scalars(
            select(PlanTask).where(PlanTask.plan_version_id == source_plan_version_id)
        )
    }
    new_tasks = list(
        session.scalars(
            select(PlanTask).where(PlanTask.plan_version_id == result_plan_version_id)
        )
    )
    new_days = {
        item.id: item
        for item in session.scalars(
            select(PlanDay).where(PlanDay.plan_version_id == result_plan_version_id)
        )
    }
    new_dates = {
        item.plan_day_id: item.scheduled_date
        for activation in session.scalars(
            select(PlanActivation).where(
                PlanActivation.plan_version_id == result_plan_version_id
            )
        )
        for item in session.scalars(
            select(PlanDaySchedule).where(PlanDaySchedule.activation_id == activation.id)
        )
    }
    new_tasks = [t for t in new_tasks if t.status not in {"CARRIED_OVER", "CANCELLED"}]
    def unique_match(items):
        return items[0] if len(items) == 1 else None

    by_coordinate_order = {
        (
            new_days[item.plan_day_id].week_index,
            new_days[item.plan_day_id].day_index,
            item.order_in_day,
        ): item
        for item in new_tasks
        if item.plan_day_id in new_days
    }
    for block in blocks:
        old_task = old_tasks.get(block.task_id)
        if old_task is None or old_task.status in {"COMPLETED", "IN_PROGRESS"}:
            continue
        replacement = unique_match([t for t in new_tasks if t.source_task_id == block.task_id])
        if replacement is None:
            replacement = unique_match([
                t for t in new_tasks
                if t.lineage_task_id == (old_task.lineage_task_id or old_task.id)
            ])
        if replacement is None and old_task:
            old_day = session.get(PlanDay, old_task.plan_day_id)
            if old_day:
                replacement = by_coordinate_order.get(
                    (old_day.week_index, old_day.day_index, old_task.order_in_day)
                )
                if replacement and (
                    replacement.title, replacement.description, replacement.estimate_minutes
                ) != (old_task.title, old_task.description, old_task.estimate_minutes):
                    replacement = None
        before = {
            "status": block.status,
            "scheduled_date": block.scheduled_date.isoformat(),
            "start_local": block.start_local,
            "end_local": block.end_local,
            "revision": block.revision,
            "task_id": block.task_id,
            "plan_day_id": block.plan_day_id,
            "plan_version_id": block.plan_version_id,
        }
        unchanged = bool(replacement and block.status == SCHEDULE_PLANNED
                         and new_dates.get(replacement.plan_day_id) == block.scheduled_date
                         and replacement.title == old_task.title
                         and replacement.description == old_task.description
                         and replacement.estimate_minutes == old_task.estimate_minutes)
        if replacement is not None:
            block.plan_version_id = result_plan_version_id
            block.plan_day_id = replacement.plan_day_id
            block.task_id = replacement.id
            replacement_date = new_dates.get(replacement.plan_day_id)
            if replacement_date is not None:
                block.scheduled_date = replacement_date
        block.status = SCHEDULE_PLANNED if unchanged else SCHEDULE_REQUIRES_REVIEW
        block.parallel_confirmed = False
        block.parallel_reason = None
        block.revision += 1
        block.change_reason = reason
        session.add(
            ScheduleChange(
                goal_id=goal.id,
                entity_type="TASK_TIME_BLOCK",
                entity_id=block.id,
                change_kind="PLAN_REVISION_REVIEW",
                before_json=before,
                after_json={
                    "status": block.status,
                    "revision": block.revision,
                    "plan_version_id": block.plan_version_id,
                    "plan_day_id": block.plan_day_id,
                    "task_id": block.task_id,
                    "scheduled_date": block.scheduled_date.isoformat(),
                    "reason": reason,
                },
                reason=reason,
            )
        )
    session.flush()
    return len(blocks)


def allocate_plan_schedule(
    session: Session,
    goal: Goal,
    plan_id: str,
    *,
    schedule_plan: list[dict[str, object]] | None = None,
    affected_dates: set[date] | None = None,
    use_ai: bool = True,
    orchestration_context: dict[str, object] | None = None,
) -> list[dict[str, object]]:
    """Inherit the goal's scheduling preference when an execution revision is published."""
    from planner_api.day_schedule_service import DayScheduleService
    from planner_api.models import PlanSchedulePreference
    from planner_api.schemas import DayAllocationRequest

    preference = session.scalar(select(PlanSchedulePreference).where(
        PlanSchedulePreference.goal_id == goal.id,
    ))
    if preference is None or preference.mode == "FLEXIBLE":
        return []
    session.flush()
    activation = session.scalar(select(PlanActivation).where(
        PlanActivation.plan_version_id == plan_id,
    ))
    if activation is None:
        return []
    dates = sorted(set(session.scalars(select(PlanDaySchedule.scheduled_date).where(
        PlanDaySchedule.activation_id == activation.id,
        PlanDaySchedule.scheduled_date >= datetime.now(ZoneInfo(goal.timezone)).date(),
    ))))
    if affected_dates is not None:
        dates = [day for day in dates if day in affected_dates]
    # Rebuild future blocks from the current shared calendar constraints.  A
    # previous allocation is not a fixed user commitment: leaving it in place
    # would make the allocator treat every task as already occupied and would
    # prevent newly discovered course/lesson blocks from taking effect.
    existing_blocks = session.scalars(select(TaskTimeBlock).where(
        TaskTimeBlock.plan_version_id == plan_id,
        TaskTimeBlock.scheduled_date >= datetime.now(ZoneInfo(goal.timezone)).date(),
        TaskTimeBlock.status.in_((SCHEDULE_PLANNED, SCHEDULE_REQUIRES_REVIEW)),
        TaskTimeBlock.scheduled_date.in_(dates),
    ))
    for block in existing_blocks:
        block.status = SCHEDULE_CANCELLED
        block.change_reason = "按最新课程与共享日历约束重新排课"
        block.revision += 1
    session.flush()
    scheduler = DayScheduleService(session, shared_context=orchestration_context)
    requests = [
        DayAllocationRequest(
            scheduled_date=day, include_flexible=False, goal_id=goal.id, use_ai=use_ai,
        )
        for day in dates
    ]
    if schedule_plan is not None:
        by_date = {
            str(item.get("scheduled_date")): item
            for item in schedule_plan
            if isinstance(item, dict)
        }
        adjusted: list[DayAllocationRequest] = []
        for request in requests:
            record = by_date.get(request.scheduled_date.isoformat(), {})
            baseline = scheduler.allocator.preview(request)
            by_title = {}
            for task in baseline.scheduling_context.get("tasks", []):
                if isinstance(task, dict) and isinstance(task.get("title"), str):
                    by_title.setdefault(task["title"], []).append(task["task_id"])
            order: list[str] = []
            starts: dict[str, str] = {}
            for assignment in record.get("assignments", []):
                if not isinstance(assignment, dict):
                    continue
                matches = by_title.get(assignment.get("title"), [])
                if not matches:
                    continue
                task_id = matches.pop(0)
                order.append(task_id)
                if assignment.get("start_local"):
                    starts[task_id] = assignment["start_local"]
            adjusted.append(request.model_copy(update={
                "model_task_order": order,
                "model_preferred_starts": starts,
                "model_notes": ["使用用户在草案中修改的时间安排。"],
            }))
        requests = adjusted
    records: list[dict[str, object]] = []
    for preview in scheduler.preview_many(requests):
        titles = {
            item["task_id"]: item.get("title", "")
            for item in preview.scheduling_context.get("tasks", [])
            if isinstance(item, dict) and isinstance(item.get("task_id"), str)
        }
        records.append({
            "scheduled_date": preview.request.scheduled_date.isoformat(),
            "strategy": preview.strategy,
            # Keep the persisted draft stable across cloned revisions. Task IDs
            # are intentionally omitted because recovery creates new task IDs.
            "assignments": [
                {
                    "title": titles.get(item.task_id, ""),
                    "start_local": item.start_local,
                    "end_local": item.end_local,
                }
                for item in preview.assignments
            ],
            "unallocated_titles": [item.title for item in preview.unallocated],
            "unallocated": [
                {"task_id": item.id,
                 "reason_code": preview.unallocated_reasons.get(item.id, "CAPACITY_INSUFFICIENT")}
                for item in preview.unallocated
            ],
        })
        # Publishing one plan must not change another plan's pending tasks.
        for assignment in preview.assignments:
            task = session.get(PlanTask, assignment.task_id)
            if task is not None and task.plan_version_id == plan_id:
                scheduler.allocator.today.schedule_task(assignment, commit=False)
                session.flush()
    return records
