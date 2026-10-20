from __future__ import annotations

from planner_api.models import PlanTask
from planner_api.schemas import PlanTaskRead


def plan_task_read(task: PlanTask) -> PlanTaskRead:
    """Expose one stable execution DTO for legacy and materialized tasks."""

    policy = task.scheduling_policy or "EXACT_DAY"
    guidance = {
        "TASK_POOL": ["补选其他候选任务", "也可改到其他日期"],
        "TRIGGERED": ["保留触发条件，满足条件后重新安排"],
        "CADENCE": ["保留本周期剩余配额", "转入弹性补做"],
        "QUOTA": ["保留本周期剩余配额", "转入弹性补做"],
        "DATE_WINDOW": ["加入弹性待安排", "改到允许日期"],
        "FLOATING": ["加入弹性待安排", "改到其他日期"],
        "EXACT_DAY": ["记录跳过", "需要时通过恢复安排补做"],
    }.get(policy, ["记录跳过", "需要时通过恢复安排补做"])
    return PlanTaskRead(
        id=task.id,
        order=task.order_in_day,
        title=task.title,
        description=task.description,
        estimate_minutes=task.estimate_minutes,
        priority=task.priority,
        task_kind=task.task_kind,
        is_required=task.is_required,
        evidence=task.evidence_json,
        depends_on=task.depends_on_json,
        satisfies_commitments=task.satisfies_commitments_json,
        ai_schedule_requested=task.ai_schedule_requested,
        is_splittable=task.is_splittable,
        scheduling_policy=policy,
        scheduling_policy_data=task.scheduling_policy_json,
        status=task.status,
        source_kind=(
            "WORK_ITEM_OCCURRENCE" if task.source_work_item_id else "LEGACY_PLAN_TASK"
        ),
        source_work_item_id=task.source_work_item_id,
        work_item_window_id=task.work_item_window_id,
        occurrence_index=task.occurrence_index,
        materialization_key=task.materialization_key,
        skip_guidance=guidance,
    )
