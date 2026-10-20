from sqlalchemy import select
from sqlalchemy.orm import Session

from planner_api.models import Goal, ManualPlan, PlanActivation, utc_now


def handoff_quick_plans(session: Session, goal: Goal) -> None:
    """Transfer execution ownership without deleting source tasks or their history."""
    activation = session.scalar(
        select(PlanActivation).where(PlanActivation.plan_version_id == goal.active_plan_version_id)
    )
    if activation is None:
        return
    for source in session.scalars(select(ManualPlan)):
        arrangement = source.arrangement_json or {}
        if arrangement.get("upgraded_goal_id") != goal.id or arrangement.get("handoff_at"):
            continue
        source.arrangement_json = {
            **arrangement,
            "handoff_at": utc_now().isoformat(),
            "handoff_start_date": activation.requested_start_date.isoformat(),
            "handoff_previous_status": source.status,
        }
        source.status = "ARCHIVED"

