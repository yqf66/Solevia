"""add relative plan versions, milestones, days and tasks

Revision ID: 0004_relative_plans
Revises: 0003_route_options
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004_relative_plans"
down_revision: str | None = "0003_route_options"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("goals", sa.Column("active_plan_version_id", sa.String(36), nullable=True))
    op.create_table(
        "plan_versions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "goal_id", sa.String(36), sa.ForeignKey("goals.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column(
            "route_version_id",
            sa.String(36),
            sa.ForeignKey("route_versions.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("total_weeks", sa.Integer(), nullable=False),
        sa.Column("weekly_execution_days", sa.Integer(), nullable=False),
        sa.Column("weekly_minutes", sa.Integer(), nullable=False),
        sa.Column("total_estimated_minutes", sa.Integer(), nullable=False),
        sa.Column("buffer_strategy_json", sa.JSON(), nullable=False),
        sa.Column("assumptions_json", sa.JSON(), nullable=False),
        sa.Column("open_questions_json", sa.JSON(), nullable=False),
        sa.Column("prompt_version", sa.String(40), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_plan_versions_goal_id", "plan_versions", ["goal_id"])
    op.create_index("ix_plan_versions_route_version_id", "plan_versions", ["route_version_id"])
    op.create_index("ix_plan_versions_status", "plan_versions", ["status"])
    op.create_table(
        "plan_milestones",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "plan_version_id",
            sa.String(36),
            sa.ForeignKey("plan_versions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "route_stage_id",
            sa.String(36),
            sa.ForeignKey("route_stages.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("stage_order", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(160), nullable=False),
        sa.Column("purpose", sa.Text(), nullable=False),
        sa.Column("start_week", sa.Integer(), nullable=False),
        sa.Column("end_week", sa.Integer(), nullable=False),
        sa.Column("outputs_json", sa.JSON(), nullable=False),
    )
    op.create_index("ix_plan_milestones_plan_version_id", "plan_milestones", ["plan_version_id"])
    op.create_index("ix_plan_milestones_route_stage_id", "plan_milestones", ["route_stage_id"])
    op.create_table(
        "plan_days",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "plan_version_id",
            sa.String(36),
            sa.ForeignKey("plan_versions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "milestone_id",
            sa.String(36),
            sa.ForeignKey("plan_milestones.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("stage_order", sa.Integer(), nullable=False),
        sa.Column("week_index", sa.Integer(), nullable=False),
        sa.Column("day_index", sa.Integer(), nullable=False),
        sa.Column("primary_goal", sa.Text(), nullable=False),
        sa.Column("suggested_minutes", sa.Integer(), nullable=False),
        sa.Column("evidence_json", sa.JSON(), nullable=False),
        sa.Column("do_not_json", sa.JSON(), nullable=False),
    )
    op.create_index("ix_plan_days_plan_version_id", "plan_days", ["plan_version_id"])
    op.create_index("ix_plan_days_milestone_id", "plan_days", ["milestone_id"])
    op.create_index(
        "idx_plan_days_plan_week_day",
        "plan_days",
        ["plan_version_id", "week_index", "day_index"],
        unique=True,
    )
    op.create_table(
        "plan_tasks",
        sa.Column("id", sa.String(36), primary_key=True),
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
        sa.Column("order_in_day", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(180), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("estimate_minutes", sa.Integer(), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column("task_kind", sa.String(24), nullable=False),
        sa.Column("is_required", sa.Boolean(), nullable=False),
        sa.Column("evidence_json", sa.JSON(), nullable=False),
        sa.Column("depends_on_json", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
    )
    op.create_index("ix_plan_tasks_plan_version_id", "plan_tasks", ["plan_version_id"])
    op.create_index("ix_plan_tasks_plan_day_id", "plan_tasks", ["plan_day_id"])
    op.create_index("ix_plan_tasks_status", "plan_tasks", ["status"])


def downgrade() -> None:
    op.drop_table("plan_tasks")
    op.drop_table("plan_days")
    op.drop_table("plan_milestones")
    op.drop_table("plan_versions")
    op.drop_column("goals", "active_plan_version_id")
