"""Simulate recovery inside a savepoint and retain only its reviewable result."""

from __future__ import annotations

import hashlib
import json
from datetime import date

from sqlalchemy import select

from planner_api.models import (
    AvailabilityException,
    AvailabilityWindow,
    PlanDaySchedule,
    PlanSchedulePreference,
    PlanTask,
    WorkspaceAvailabilityWindow,
    WorkspaceScheduleProfile,
)
from planner_api.schedule_reconciliation import allocate_plan_schedule, mark_plan_schedule_review
from planner_api.today_service import TodayService
from planner_api.recovery_diagnostics import (
    backlog_diagnostics,
    explanation,
    source_for_task,
    source_items,
)


def catchup_clone(preview):
    return {
        **preview,
        "mode": "KEEP_DEADLINE",
        "projected_end_date": preview["original_end_date"],
        "changes": [],
        "carryover_task_ids": preview["backlog_task_ids"],
        "carryovers": preview["non_execution_assignments"],
        "redistributions": preview["execution_assignments"],
    }


def catchup_affected_dates(preview: dict) -> set[date]:
    return {date.fromisoformat(item["scheduled_date"]) for item in preview["allocation"]}


def preview_fingerprint(preview: dict[str, object]) -> str:
    """Hash reviewable state while ignoring transient IDs from schedule simulation."""
    canonical = dict(preview)
    time_preview = canonical.get("time_preview")
    if isinstance(time_preview, dict):
        canonical["time_preview"] = {"fingerprint": time_preview.get("fingerprint")}
    return hashlib.sha256(
        json.dumps(canonical, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()[:24]


def snapshot(service, goal, plan, anchor, sources=None, diagnostics=None):
    session = service.session
    sources = sources or {}
    activation = service._activation(plan.id)
    days = (
        sorted(
            set(
                session.scalars(
                    select(PlanDaySchedule.scheduled_date).where(
                        PlanDaySchedule.activation_id == activation.id,
                        PlanDaySchedule.scheduled_date >= anchor,
                    )
                )
            )
        )
        if activation
        else []
    )
    preference = session.scalar(
        select(PlanSchedulePreference).where(
            PlanSchedulePreference.goal_id == goal.id,
        )
    )
    mode = preference.mode if preference else "FLEXIBLE"
    boards = [TodayService(session).get_today(day) for day in days]
    items = []
    for board in boards:
        for task in [*board.fixed_tasks, *board.flexible_tasks]:
            if task.plan_id != goal.id or task.status not in {"NOT_STARTED", "IN_PROGRESS"}:
                continue
            raw = session.get(PlanTask, task.id)
            source = source_for_task(service, raw, sources) if sources else {}
            state = (
                "SCHEDULED"
                if task.is_fixed
                else "FLEXIBLE"
                if mode == "FLEXIBLE"
                else "UNALLOCATED"
            )
            detail = {}
            if state == "UNALLOCATED":
                code = (diagnostics or {}).get(task.id, "CAPACITY_INSUFFICIENT")
                if task.status == "IN_PROGRESS":
                    code = "PROGRESS_REVIEW_REQUIRED"
                if code == "DEPENDENCY_BLOCKED" and any(
                    d.status == "UNKNOWN" for d in task.dependencies
                ):
                    code = "UNRESOLVED_DEPENDENCY"
                detail = explanation(code)
            items.append(
                {
                    "title": task.title,
                    "date": str(task.scheduled_date),
                    "start": task.start_local,
                    "end": task.end_local,
                    "minutes": task.estimate_minutes,
                    "state": state,
                    **source,
                    **detail,
                }
            )
    return (
        sorted(
            items,
            key=lambda t: (
                t["date"],
                t["start"] or "99",
                t["title"],
                t["minutes"],
                t.get("source_task_id") or "",
            ),
        ),
        boards,
        mode,
    )


def preview_time(
    service,
    goal,
    plan,
    preview,
    *,
    catchup=False,
    schedule_plan: list[dict[str, object]] | None = None,
):
    session = service.session
    anchor = max(date.fromisoformat(preview["anchor_date"]), service._today_for(goal))
    sources = source_items(service, plan)
    before, _, mode = snapshot(service, goal, plan, anchor, sources)
    if catchup and not preview["programmatic_feasible"]:
        after = backlog_diagnostics(service, plan, preview, sources)
        fingerprint = hashlib.sha256(
            json.dumps(
                {"before": before, "after": after, "today": str(service._today_for(goal))},
                sort_keys=True,
            ).encode()
        ).hexdigest()
        return {
            "mode": mode,
            "timezone": goal.timezone,
            "before": before,
            "after": after,
            "fingerprint": fingerprint,
            "unallocated_count": len(after),
            "diagnostics_only": True,
            "day_schedule": [],
        }
    transaction = session.begin_nested()
    try:
        revised = service._clone_plan_for_recovery(
            goal,
            plan,
            catchup_clone(preview) if catchup else preview,
        )
        mark_plan_schedule_review(session, goal, plan.id, revised.id, "恢复时间预览")
        goal.active_plan_version_id = revised.id
        goal.status = "ACTIVE"
        session.flush()
        day_schedule = allocate_plan_schedule(
            session,
            goal,
            revised.id,
            schedule_plan=schedule_plan,
            affected_dates=catchup_affected_dates(preview) if catchup else None,
            use_ai=not catchup,
        )
        diagnostics = {
            item["task_id"]: item["reason_code"]
            for day in day_schedule
            for item in day.get("unallocated", [])
        }
        after, boards, _ = snapshot(service, goal, revised, anchor, sources, diagnostics)
        external = [
            t.model_dump(mode="json")
            for board in boards
            for t in [*board.fixed_tasks, *board.flexible_tasks]
            if t.plan_id != goal.id
        ]
        settings = {}
        for model in (
            PlanSchedulePreference,
            AvailabilityWindow,
            AvailabilityException,
            WorkspaceAvailabilityWindow,
            WorkspaceScheduleProfile,
        ):
            settings[model.__tablename__] = sorted(
                [
                    {c.name: str(getattr(row, c.name)) for c in model.__table__.columns}
                    for row in session.scalars(select(model))
                ],
                key=lambda row: row["id"],
            )
        state = {
            "before": before,
            "after": after,
            "settings": settings,
            "external": sorted(external, key=lambda t: (t["scheduled_date"], t["id"])),
            "today": str(service._today_for(goal)),
        }
        fingerprint = hashlib.sha256(json.dumps(state, sort_keys=True).encode()).hexdigest()
        result = {
            "mode": mode,
            "timezone": goal.timezone,
            "before": before,
            "after": after,
            "fingerprint": fingerprint,
            "unallocated_count": sum(t["state"] == "UNALLOCATED" for t in after),
            "day_schedule": day_schedule,
        }
    finally:
        transaction.rollback()
    return result
