"""add feedback adjustment candidates and automatic day logs

Revision ID: 0009_feedback_adjustments
Revises: 0008_plan_generation_jobs
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009_feedback_adjustments"
down_revision: str | None = "0008_plan_generation_jobs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "feedback_adjustments",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "feedback_id",
            sa.String(36),
            sa.ForeignKey("feedbacks.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "goal_id", sa.String(36), sa.ForeignKey("goals.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column(
            "plan_version_id",
            sa.String(36),
            sa.ForeignKey("plan_versions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("adjustment_kind", sa.String(32), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("title", sa.String(180), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("rationale", sa.Text(), nullable=False),
        sa.Column("impact_json", sa.JSON(), nullable=False),
        sa.Column("operation_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("feedback_id", name="uq_feedback_adjustments_feedback_id"),
    )
    for column in ("feedback_id", "goal_id", "plan_version_id", "adjustment_kind", "status"):
        op.create_index(f"ix_feedback_adjustments_{column}", "feedback_adjustments", [column])

    op.create_table(
        "execution_day_logs",
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
        sa.Column("scheduled_date", sa.Date(), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("metrics_json", sa.JSON(), nullable=False),
        sa.Column("adjustments_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("plan_day_id", name="uq_execution_day_logs_plan_day_id"),
    )
    for column in ("goal_id", "plan_version_id", "plan_day_id", "scheduled_date", "status"):
        op.create_index(f"ix_execution_day_logs_{column}", "execution_day_logs", [column])


def downgrade() -> None:
    op.drop_table("execution_day_logs")
    op.drop_table("feedback_adjustments")
