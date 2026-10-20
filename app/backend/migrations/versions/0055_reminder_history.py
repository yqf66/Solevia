"""Persist commitment reminder history and acknowledgement state."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "0055_reminder_history"
down_revision: str | None = "0054_reminder_governance"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "reminder_events",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("goal_id", sa.String(length=36), nullable=False),
        sa.Column("plan_version_id", sa.String(length=36), nullable=True),
        sa.Column("dedupe_key", sa.String(length=255), nullable=False),
        sa.Column("label", sa.String(length=160), nullable=False),
        sa.Column("risk", sa.String(length=24), nullable=False),
        sa.Column("severity", sa.String(length=16), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("action", sa.String(length=32), nullable=False),
        sa.Column("period_end", sa.Date(), nullable=True),
        sa.Column("remaining_occurrences", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="OPEN"),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("read_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("dismissed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["goal_id"], ["goals.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["plan_version_id"], ["plan_versions.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("goal_id", "dedupe_key", name="uq_reminder_events_goal_dedupe"),
    )
    op.create_index("ix_reminder_events_goal_id", "reminder_events", ["goal_id"])
    op.create_index("ix_reminder_events_plan_version_id", "reminder_events", ["plan_version_id"])
    op.create_index("ix_reminder_events_status", "reminder_events", ["status"])
    op.create_index(
        "idx_reminder_events_goal_status", "reminder_events", ["goal_id", "status"]
    )


def downgrade() -> None:
    op.drop_index("idx_reminder_events_goal_status", table_name="reminder_events")
    op.drop_index("ix_reminder_events_status", table_name="reminder_events")
    op.drop_index("ix_reminder_events_plan_version_id", table_name="reminder_events")
    op.drop_index("ix_reminder_events_goal_id", table_name="reminder_events")
    op.drop_table("reminder_events")
