"""Unify quick arrangements and add exploratory planning."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0035_quick_plans_and_rolling"
down_revision: str | None = "0034_light_arrangements"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "goals",
        sa.Column("planning_mode", sa.String(16), nullable=False, server_default="COMMITTED"),
    )
    op.add_column(
        "goals", sa.Column("planning_mode_confirmed_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.execute("UPDATE goals SET planning_mode_confirmed_at = created_at")
    op.add_column(
        "route_versions",
        sa.Column("planning_mode", sa.String(16), nullable=False, server_default="COMMITTED"),
    )
    op.add_column(
        "plan_versions",
        sa.Column("planning_mode", sa.String(16), nullable=False, server_default="COMMITTED"),
    )
    op.add_column(
        "plan_versions",
        sa.Column("exploration_window_weeks", sa.Integer(), nullable=False, server_default="1"),
    )
    op.add_column(
        "plan_versions",
        sa.Column("next_unplanned_week", sa.Integer(), nullable=False, server_default="1"),
    )
    op.add_column(
        "plan_generation_jobs",
        sa.Column("planning_mode", sa.String(16), nullable=False, server_default="COMMITTED"),
    )
    op.create_table(
        "plan_windows",
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
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("start_week", sa.Integer(), nullable=False),
        sa.Column("end_week", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="DRAFT"),
        sa.Column("trigger", sa.String(24), nullable=False, server_default="INITIAL"),
        sa.Column("basis_json", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("plan_version_id", "sequence", name="uq_plan_window_sequence"),
    )
    op.create_index("ix_plan_windows_goal_id", "plan_windows", ["goal_id"])
    op.create_index("ix_plan_windows_plan_version_id", "plan_windows", ["plan_version_id"])
    op.create_index("ix_plan_windows_status", "plan_windows", ["status"])
    # Both previous entry points now belong to one quick-planning product.
    op.execute("UPDATE manual_plans SET kind = 'QUICK' WHERE kind IN ('LIGHT', 'MANUAL')")
    op.rename_table("manual_plans", "quick_plans")
    op.rename_table("manual_tasks", "quick_plan_tasks")
    op.rename_table("manual_task_date_overrides", "quick_plan_task_date_overrides")
    op.rename_table("light_arrangement_drafts", "quick_plan_drafts")
    op.alter_column("quick_plan_tasks", "manual_plan_id", new_column_name="quick_plan_id")
    op.alter_column(
        "quick_plan_task_date_overrides",
        "manual_task_id",
        new_column_name="quick_plan_task_id",
    )
    op.alter_column("quick_plan_drafts", "manual_plan_id", new_column_name="quick_plan_id")


def downgrade() -> None:
    op.alter_column("quick_plan_drafts", "quick_plan_id", new_column_name="manual_plan_id")
    op.alter_column(
        "quick_plan_task_date_overrides",
        "quick_plan_task_id",
        new_column_name="manual_task_id",
    )
    op.alter_column("quick_plan_tasks", "quick_plan_id", new_column_name="manual_plan_id")
    op.rename_table("quick_plan_drafts", "light_arrangement_drafts")
    op.rename_table("quick_plan_task_date_overrides", "manual_task_date_overrides")
    op.rename_table("quick_plan_tasks", "manual_tasks")
    op.rename_table("quick_plans", "manual_plans")
    op.drop_index("ix_plan_windows_status", table_name="plan_windows")
    op.drop_index("ix_plan_windows_plan_version_id", table_name="plan_windows")
    op.drop_index("ix_plan_windows_goal_id", table_name="plan_windows")
    op.drop_table("plan_windows")
    op.drop_column("plan_generation_jobs", "planning_mode")
    op.drop_column("plan_versions", "next_unplanned_week")
    op.drop_column("plan_versions", "exploration_window_weeks")
    op.drop_column("plan_versions", "planning_mode")
    op.drop_column("route_versions", "planning_mode")
    op.drop_column("goals", "planning_mode_confirmed_at")
    op.drop_column("goals", "planning_mode")
