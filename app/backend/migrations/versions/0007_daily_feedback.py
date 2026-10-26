"""add persistent daily feedback

Revision ID: 0007_daily_feedback
Revises: 0006_execution_start
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007_daily_feedback"
down_revision: str | None = "0006_execution_start"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "feedbacks",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "goal_id", sa.String(36), sa.ForeignKey("goals.id", ondelete="CASCADE"), nullable=False
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
            sa.ForeignKey("plan_tasks.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("feedback_kind", sa.String(24), nullable=False),
        sa.Column("reason_code", sa.String(40), nullable=False),
        sa.Column("intent_code", sa.String(40), nullable=True),
        sa.Column("detail_text", sa.Text(), nullable=True),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    for column in (
        "goal_id",
        "plan_version_id",
        "plan_day_id",
        "task_id",
        "feedback_kind",
        "reason_code",
        "status",
    ):
        op.create_index(f"ix_feedbacks_{column}", "feedbacks", [column])
    op.create_index(
        "idx_feedbacks_goal_day_created", "feedbacks", ["goal_id", "plan_day_id", "created_at"]
    )


def downgrade() -> None:
    op.drop_table("feedbacks")
