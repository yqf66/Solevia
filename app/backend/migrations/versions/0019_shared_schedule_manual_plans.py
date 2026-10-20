"""add workspace shared schedule and manual plans"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0019_shared_schedule_manual_plans"
down_revision: str | None = "0018_execution_scheduling_foundation"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "workspace_schedule_profiles",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("timezone", sa.String(64), nullable=False, server_default="Asia/Shanghai"),
        sa.Column("default_mode", sa.String(16), nullable=False, server_default="FLEXIBLE"),
        sa.Column("break_minutes", sa.Integer(), nullable=False, server_default="10"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "workspace_availability_windows",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("profile_id", sa.String(36), sa.ForeignKey("workspace_schedule_profiles.id", ondelete="CASCADE"), nullable=False),
        sa.Column("weekday", sa.Integer(), nullable=False),
        sa.Column("start_local", sa.String(5), nullable=False),
        sa.Column("end_local", sa.String(5), nullable=False),
        sa.Column("valid_from", sa.Date(), nullable=True),
        sa.Column("valid_to", sa.Date(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_workspace_availability_windows_profile_id", "workspace_availability_windows", ["profile_id"])
    op.create_index("idx_workspace_availability_weekday", "workspace_availability_windows", ["profile_id", "weekday"])

    op.create_table(
        "manual_plans",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("title", sa.String(160), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("status", sa.String(24), nullable=False, server_default="ACTIVE"),
        sa.Column("timezone", sa.String(64), nullable=False, server_default="Asia/Shanghai"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_manual_plans_status", "manual_plans", ["status"])

    op.create_table(
        "manual_tasks",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("manual_plan_id", sa.String(36), sa.ForeignKey("manual_plans.id", ondelete="CASCADE"), nullable=False),
        sa.Column("order_in_plan", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("title", sa.String(180), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("estimate_minutes", sa.Integer(), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False, server_default="3"),
        sa.Column("scheduled_date", sa.Date(), nullable=True),
        sa.Column("start_local", sa.String(5), nullable=True),
        sa.Column("end_local", sa.String(5), nullable=True),
        sa.Column("status", sa.String(24), nullable=False, server_default="NOT_STARTED"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_manual_tasks_manual_plan_id", "manual_tasks", ["manual_plan_id"])
    op.create_index("ix_manual_tasks_scheduled_date", "manual_tasks", ["scheduled_date"])
    op.create_index("ix_manual_tasks_status", "manual_tasks", ["status"])
    op.create_index("idx_manual_tasks_plan_order", "manual_tasks", ["manual_plan_id", "order_in_plan"])
    op.create_index("idx_manual_tasks_scheduled_date", "manual_tasks", ["scheduled_date"])


def downgrade() -> None:
    for name in ("idx_manual_tasks_scheduled_date", "idx_manual_tasks_plan_order", "ix_manual_tasks_status", "ix_manual_tasks_scheduled_date", "ix_manual_tasks_manual_plan_id"):
        op.drop_index(name, table_name="manual_tasks")
    op.drop_table("manual_tasks")
    op.drop_index("ix_manual_plans_status", table_name="manual_plans")
    op.drop_table("manual_plans")
    op.drop_index("idx_workspace_availability_weekday", table_name="workspace_availability_windows")
    op.drop_index("ix_workspace_availability_windows_profile_id", table_name="workspace_availability_windows")
    op.drop_table("workspace_availability_windows")
    op.drop_table("workspace_schedule_profiles")
