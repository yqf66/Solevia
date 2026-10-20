"""User-selected backlog scope; omitted tasks remain in the execution ledger."""

import hashlib
import json

from fastapi import HTTPException
from sqlalchemy import select

from planner_api.dependency_resolver import PlanDependencyResolver
from planner_api.models import PlanDaySchedule, PlanTask, ProgressEvent
from planner_api.recovery_diagnostics import source_items


def build_scope(service, plan, activation, anchor, selected_task_ids=None):
    session = service.session
    dates = dict(
        session.execute(
            select(
                PlanDaySchedule.plan_day_id,
                PlanDaySchedule.scheduled_date,
            ).where(PlanDaySchedule.activation_id == activation.id)
        ).all()
    )
    tasks = sorted(
        [
            task
            for task in session.scalars(select(PlanTask).where(PlanTask.plan_version_id == plan.id))
            if task.status not in {"COMPLETED", "CANCELLED", "CARRIED_OVER"}
            and dates.get(task.plan_day_id, anchor) < anchor
        ],
        key=lambda task: (dates[task.plan_day_id], task.order_in_day, task.id),
    )
    ids = {task.id for task in tasks}
    if not ids:
        raise HTTPException(409, "No missed schedule needs recovery")
    selected = ids if selected_task_ids is None else set(selected_task_ids)
    if (
        not selected
        or not selected <= ids
        or (selected_task_ids is not None and len(selected) != len(selected_task_ids))
    ):
        raise HTTPException(422, "请至少选择一项当前规划的未完成积压任务，任务范围可能已变化。")
    sources = source_items(service, plan)
    resolver = PlanDependencyResolver(session)
    indexes = resolver.indexes(plan.id)
    rows = []
    from planner_api.recovery_split import split_policy

    for task in tasks:
        can_split, split_reason = split_policy(service, plan, task)
        rows.append(
            {
                "source_task_id": task.id,
                "lineage_task_id": service._task_lineage_key(task),
                "title": task.title,
                "source_date": str(dates[task.plan_day_id]),
                "minutes": task.estimate_minutes,
                "remaining_minutes": sources[task.id]["remaining_minutes"],
                "selected": task.id in selected,
                "can_split": can_split,
                "split_reason": split_reason,
                "prerequisite_task_ids": sorted(
                    {
                        parent.id
                        for dependency in task.depends_on_json or []
                        if (parent := resolver.resolve(dependency, task, indexes)) is not None
                        and parent.id in ids
                    }
                ),
            }
        )
    events = [
        {
            "id": e.id,
            "task_id": e.task_id,
            "event_type": e.event_type,
            "metadata": e.metadata_json,
            "occurred_at": str(e.occurred_at),
        }
        for e in session.scalars(
            select(ProgressEvent).where(ProgressEvent.plan_version_id == plan.id)
        )
    ]
    state = {
        "items": rows,
        "events": sorted(events, key=lambda e: e["id"]),
        "tasks": [
            {
                "id": task.id,
                "status": task.status,
                "depends_on": task.depends_on_json,
                "policy": task.scheduling_policy,
                "rules": task.scheduling_policy_json,
            }
            for task in tasks
        ],
    }
    return {
        "source_plan_version_id": plan.id,
        "items": rows,
        "selected_task_ids": sorted(selected),
        "deferred_task_ids": sorted(ids - selected),
        "selected_minutes": sum(row["minutes"] for row in rows if row["selected"]),
        "deferred_minutes": sum(row["minutes"] for row in rows if not row["selected"]),
        "fingerprint": hashlib.sha256(json.dumps(state, sort_keys=True).encode()).hexdigest(),
    }
