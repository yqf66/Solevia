"""Stable source identities and explanations for read-only recovery reviews."""

from sqlalchemy import select
from datetime import date

from planner_api.dependency_resolver import PlanDependencyResolver
from planner_api.models import PlanDay, PlanDaySchedule, PlanTask, ProgressEvent


REASONS = {
    "CAPACITY_INSUFFICIENT": "可用时间不足以承接这项任务。",
    "CONTINUOUS_SLOT_REQUIRED": "空闲总量可能足够，但没有能放下整项任务的连续时段。",
    "DEPENDENCY_BLOCKED": "前置任务尚未完成或尚未安排，需先处理前置内容。",
    "UNRESOLVED_DEPENDENCY": "前置要求无法对应到明确任务，需要先确认前置关系。",
    "FIXED_CONSTRAINT": "固定任务或课程占用了可用时段。",
    "TIMEZONE_CONFLICT": "任务或可用时间的时区与共享日历不同，需要核对时区。",
    "PROGRESS_REVIEW_REQUIRED": "任务已开始，需核对剩余工作后再安排具体时段。",
    "COMPLEXITY_REVIEW_REQUIRED": "积压规模或单项估时超出直接追赶范围，需要规划讨论辅助调整。",
    "RECOVERY_INCOMPLETE": "部分任务能初步分配，但整份追赶方案尚未完整承接，需要整体核对后再应用。",
    "WINDOW_NOT_OPEN": "任务的执行窗口尚未开始。",
    "WINDOW_EXPIRED": "任务原执行窗口已结束，需要讨论调整窗口或目标。",
    "SPACING_REQUIRED": "与上次完成的间隔尚未满足，请调整恢复日期。",
    "QUOTA_SATISFIED": "当前周期的完成要求已达标，需要核对是否还应恢复此项。",
    "DAILY_LIMIT": "当天该系列完成次数已达到上限。",
    "DATE_EXCLUDED": "恢复日期不符合允许的星期或排除日期。",
    "INVALID_RULE": "任务规则或窗口资料不完整，需要先核对规划。",
    "TASK_NOT_READY": "原工作项已结束或取消，需要核对是否仍需恢复。",
    "SOURCE_IDENTITY_AMBIGUOUS": "同一来源有多项剩余任务，需要先确认源任务映射，暂不提供直接操作身份。",
}


def explanation(code):
    actions = ["REVIEW_PLAN", "KEEP_PENDING"]
    if code in {"DEPENDENCY_BLOCKED", "UNRESOLVED_DEPENDENCY"}:
        actions.insert(0, "REVIEW_DEPENDENCY")
    elif code in {"CAPACITY_INSUFFICIENT", "CONTINUOUS_SLOT_REQUIRED", "FIXED_CONSTRAINT"}:
        actions = ["ADJUST_AVAILABILITY", "CHANGE_RECOVERY_DATE", *actions]
    elif code in {"WINDOW_NOT_OPEN", "SPACING_REQUIRED", "DAILY_LIMIT", "DATE_EXCLUDED"}:
        actions.insert(0, "CHANGE_RECOVERY_DATE")
    return {
        "reason_code": code,
        "reason_detail": REASONS.get(code, "需要核对任务规则后重新预览。"),
        "allowed_actions": actions,
    }


def source_items(service, plan):
    session = service.session
    activation = service._activation(plan.id)
    dates = (
        dict(
            session.execute(
                select(
                    PlanDaySchedule.plan_day_id,
                    PlanDaySchedule.scheduled_date,
                ).where(PlanDaySchedule.activation_id == activation.id)
            ).all()
        )
        if activation
        else {}
    )
    tasks = list(session.scalars(select(PlanTask).where(PlanTask.plan_version_id == plan.id)))
    # Compute before the simulation migrates events to transient clone IDs.
    progress = {}
    by_id = {task.id: service._task_lineage_key(task) for task in tasks}
    for event in session.scalars(
        select(ProgressEvent).where(
            ProgressEvent.plan_version_id == plan.id,
            ProgressEvent.event_type == "PARTIAL_COMPLETION",
        )
    ):
        if event.task_id not in by_id or event.metadata_json.get("unit", "MINUTES") != "MINUTES":
            continue
        try:
            amount = max(0, int(event.metadata_json.get("amount") or 0))
        except (ValueError, TypeError):
            continue
        progress[event.task_id] = progress.get(event.task_id, 0) + amount
    result = {}
    for task in tasks:
        if task.status in {"COMPLETED", "CANCELLED", "CARRIED_OVER"}:
            continue
        key = by_id[task.id]
        peers = [
            item
            for item in tasks
            if by_id[item.id] == key
            and item.status not in {"COMPLETED", "CANCELLED", "CARRIED_OVER"}
        ]
        invested = (
            sum(progress.get(item.id, 0) for item in tasks if by_id[item.id] == key)
            if len(peers) == 1
            else progress.get(task.id, 0)
        )
        result[task.id] = {
            "source_plan_version_id": plan.id,
            "source_task_id": task.id,
            "lineage_task_id": key,
            "source_work_item_id": task.source_work_item_id,
            "work_item_window_id": task.work_item_window_id,
            "source_date": str(dates[task.plan_day_id]) if task.plan_day_id in dates else None,
            "remaining_minutes": max(0, task.estimate_minutes - invested),
        }
    return result


def source_for_task(service, task, sources):
    if task.id in sources:
        return sources[task.id]
    if task.source_task_id in sources:
        return sources[task.source_task_id]
    lineage = service._task_lineage_key(task)
    matches = [item for item in sources.values() if item["lineage_task_id"] == lineage]
    if len(matches) == 1:
        return matches[0]
    return {"lineage_task_id": lineage, **explanation("SOURCE_IDENTITY_AMBIGUOUS")}


def backlog_diagnostics(service, plan, preview, sources):
    # Reuse the existing eligibility ledger; never infer rules from task titles.
    from planner_api.recommendation_service import RecommendationService
    from planner_api.models import Goal

    resolver = PlanDependencyResolver(service.session)
    indexes = resolver.indexes(plan.id)
    eligibility = RecommendationService(service.session)
    goal = service.session.get(Goal, plan.goal_id)
    activation = service._activation(plan.id)
    tasks = list(indexes["tasks"])
    days = {
        day.id: day
        for day in service.session.scalars(
            select(PlanDay).where(PlanDay.plan_version_id == plan.id)
        )
    }
    dates = dict(
        service.session.execute(
            select(PlanDaySchedule.plan_day_id, PlanDaySchedule.scheduled_date).where(
                PlanDaySchedule.activation_id == activation.id
            )
        ).all()
    )
    events = list(
        service.session.scalars(
            select(ProgressEvent).where(ProgressEvent.plan_version_id == plan.id)
        )
    )
    unallocated = set(preview.get("unallocated_task_ids", []))
    items = []
    for task_id in preview["backlog_task_ids"]:
        task = service.session.get(PlanTask, task_id)
        code = (
            "CAPACITY_INSUFFICIENT"
            if task_id in unallocated
            else (
                "COMPLEXITY_REVIEW_REQUIRED"
                if preview.get("complexity_requires_model")
                else "RECOVERY_INCOMPLETE"
            )
        )
        dependencies = [resolver.resolve(raw, task, indexes) for raw in task.depends_on_json or []]
        if any(parent is None for parent in dependencies):
            code = "UNRESOLVED_DEPENDENCY"
        elif task_id in unallocated and any(
            parent.status not in {"COMPLETED", "CARRIED_OVER"}
            and (parent.id in unallocated or parent.id not in preview["backlog_task_ids"])
            for parent in dependencies
        ):
            code = "DEPENDENCY_BLOCKED"
        rule = eligibility._eligibility(
            goal,
            plan,
            activation,
            task,
            date.fromisoformat(preview["anchor_date"]),
            tasks,
            dates,
            days,
            events,
            None,
            {},
        ).get("blocked")
        detail = explanation(code)
        if rule and code not in {"UNRESOLVED_DEPENDENCY", "DEPENDENCY_BLOCKED"}:
            detail = {**explanation(rule[0]), "reason_detail": rule[1]}
        items.append(
            {
                "title": task.title,
                "date": preview["anchor_date"],
                "start": None,
                "end": None,
                "minutes": task.estimate_minutes,
                "state": "UNALLOCATED",
                **source_for_task(service, task, sources),
                **detail,
            }
        )
    return items
