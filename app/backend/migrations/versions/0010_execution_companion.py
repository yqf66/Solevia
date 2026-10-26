"""add focus sessions and quick capture inbox

Revision ID: 0010_execution_companion
Revises: 0009_feedback_adjustments
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010_execution_companion"
down_revision: str | None = "0009_feedback_adjustments"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "focus_sessions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "goal_id",
            sa.String(36),
            sa.ForeignKey("goals.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "plan_version_id",
            sa.String(36),
            sa.ForeignKey("plan_versions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "plan_day_id",
            sa.String(36),
            sa.ForeignKey("plan_days.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "task_id",
            sa.String(36),
            sa.ForeignKey("plan_tasks.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("task_title", sa.String(180), nullable=True),
        sa.Column("planned_minutes", sa.Integer(), nullable=False),
        sa.Column("actual_seconds", sa.Integer(), nullable=False),
        sa.Column("interruptions", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=False),
    )
    for column in ("goal_id", "plan_version_id", "plan_day_id", "task_id", "status"):
        op.create_index(f"ix_focus_sessions_{column}", "focus_sessions", [column])

    op.create_table(
        "quick_captures",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "goal_id",
            sa.String(36),
            sa.ForeignKey("goals.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("content", sa.String(500), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_quick_captures_goal_id", "quick_captures", ["goal_id"])
    op.create_index("ix_quick_captures_status", "quick_captures", ["status"])


def downgrade() -> None:
    op.drop_table("quick_captures")
    op.drop_table("focus_sessions")
