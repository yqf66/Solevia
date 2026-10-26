"""add plan activation, calendar mapping and progress events

Revision ID: 0006_execution_start
Revises: 0005_plan_discussion
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006_execution_start"
down_revision: str | None = "0005_plan_discussion"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "plan_activations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "goal_id", sa.String(36), sa.ForeignKey("goals.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column(
            "plan_version_id",
            sa.String(36),
            sa.ForeignKey("plan_versions.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("requested_start_date", sa.Date(), nullable=False),
        sa.Column("projected_end_date", sa.Date(), nullable=False),
        sa.Column("execution_weekdays_json", sa.JSON(), nullable=False),
        sa.Column("excluded_dates_json", sa.JSON(), nullable=False),
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_plan_activations_goal_id", "plan_activations", ["goal_id"])
    op.create_index(
        "ix_plan_activations_plan_version_id", "plan_activations", ["plan_version_id"], unique=True
    )
    op.create_table(
        "plan_day_schedules",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "activation_id",
            sa.String(36),
            sa.ForeignKey("plan_activations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "plan_day_id",
            sa.String(36),
            sa.ForeignKey("plan_days.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("scheduled_date", sa.Date(), nullable=False),
    )
    op.create_index("ix_plan_day_schedules_activation_id", "plan_day_schedules", ["activation_id"])
    op.create_index(
        "ix_plan_day_schedules_plan_day_id", "plan_day_schedules", ["plan_day_id"], unique=True
    )
    op.create_index(
        "ix_plan_day_schedules_scheduled_date", "plan_day_schedules", ["scheduled_date"]
    )
    op.create_table(
        "progress_events",
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
        sa.Column("event_type", sa.String(24), nullable=False),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
    )
    for column in ("goal_id", "plan_version_id", "plan_day_id", "task_id", "event_type"):
        op.create_index(f"ix_progress_events_{column}", "progress_events", [column])


def downgrade() -> None:
    op.drop_table("progress_events")
    op.drop_table("plan_day_schedules")
    op.drop_table("plan_activations")
