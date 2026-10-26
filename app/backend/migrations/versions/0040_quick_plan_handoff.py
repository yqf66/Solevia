"""Archive source arrangements only when their successor has actually started."""

from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

revision = "0040_quick_plan_handoff"
down_revision = "0039_continuous_execution_memory"
branch_labels = None
depends_on = None


def upgrade() -> None:
    connection = op.get_bind()
    sources = sa.table(
        "quick_plans", sa.column("id", sa.String), sa.column("status", sa.String),
        sa.column("title", sa.String), sa.column("arrangement_json", sa.JSON),
    )
    goals = sa.table(
        "goals", sa.column("id", sa.String), sa.column("title", sa.String),
        sa.column("status", sa.String), sa.column("active_plan_version_id", sa.String),
    )
    activations = sa.table(
        "plan_activations", sa.column("plan_version_id", sa.String),
        sa.column("requested_start_date", sa.Date),
    )
    for source in connection.execute(sa.select(sources)).mappings().all():
        arrangement = source["arrangement_json"] or {}
        successor_id = arrangement.get("upgraded_goal_id")
        if not successor_id or arrangement.get("handoff_at"):
            continue
        successor = connection.execute(
            sa.select(goals).where(goals.c.id == successor_id)
        ).mappings().first()
        if successor is None:
            continue
        if successor["title"].startswith("将这份近期安排升级为完整规划："):
            connection.execute(goals.update().where(goals.c.id == successor_id).values(
                title=source["title"],
            ))
        if successor["status"] not in {
            "ACTIVE", "PAUSED", "RECOVERY_REQUIRED", "COMPLETED", "ARCHIVED",
        }:
            continue
        start = connection.scalar(sa.select(activations.c.requested_start_date).where(
            activations.c.plan_version_id == successor["active_plan_version_id"],
        ))
        if start is None:
            continue
        connection.execute(sources.update().where(sources.c.id == source["id"]).values(
            status="ARCHIVED", arrangement_json={
                **arrangement,
                "handoff_at": datetime.now(UTC).isoformat(),
                "handoff_start_date": start.isoformat(),
                "handoff_previous_status": source["status"],
            },
        ))


def downgrade() -> None:
    # Reopening the source could duplicate live execution; history was never deleted.
    pass
