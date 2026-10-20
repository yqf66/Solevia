"""add execution scheduling foundation tables"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0018_execution_scheduling_foundation"
down_revision: str | None = "0017_clarification_analysis_jobs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "plan_schedule_preferences",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("goal_id", sa.String(36), sa.ForeignKey("goals.id", ondelete="CASCADE"), nullable=False),
        sa.Column("mode", sa.String(16), nullable=False, server_default="FLEXIBLE"),
        sa.Column("timezone", sa.String(64), nullable=False, server_default="Asia/Shanghai"),
        sa.Column("break_minutes", sa.Integer(), nullable=False, server_default="10"),
        sa.Column("allow_cross_window", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("goal_id", name="uq_plan_schedule_preferences_goal_id"),
    )
    op.create_index("ix_plan_schedule_preferences_goal_id", "plan_schedule_preferences", ["goal_id"])

    op.create_table(
        "availability_windows",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("goal_id", sa.String(36), sa.ForeignKey("goals.id", ondelete="CASCADE"), nullable=False),
        sa.Column("weekday", sa.Integer(), nullable=False),
        sa.Column("start_local", sa.String(5), nullable=False),
        sa.Column("end_local", sa.String(5), nullable=False),
        sa.Column("timezone", sa.String(64), nullable=False, server_default="Asia/Shanghai"),
        sa.Column("valid_from", sa.Date(), nullable=True),
        sa.Column("valid_to", sa.Date(), nullable=True),
        sa.Column("source", sa.String(16), nullable=False, server_default="USER"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_availability_windows_goal_id", "availability_windows", ["goal_id"])
    op.create_index("idx_availability_windows_goal_weekday", "availability_windows", ["goal_id", "weekday"])

    op.create_table(
        "availability_exceptions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("goal_id", sa.String(36), sa.ForeignKey("goals.id", ondelete="CASCADE"), nullable=False),
        sa.Column("exception_date", sa.Date(), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False, server_default="UNAVAILABLE"),
        sa.Column("start_local", sa.String(5), nullable=True),
        sa.Column("end_local", sa.String(5), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("source", sa.String(16), nullable=False, server_default="USER"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_availability_exceptions_goal_id", "availability_exceptions", ["goal_id"])
    op.create_index("idx_availability_exceptions_goal_date", "availability_exceptions", ["goal_id", "exception_date"])

    op.create_table(
        "task_time_blocks",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("goal_id", sa.String(36), sa.ForeignKey("goals.id", ondelete="CASCADE"), nullable=False),
        sa.Column("plan_version_id", sa.String(36), sa.ForeignKey("plan_versions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("plan_day_id", sa.String(36), sa.ForeignKey("plan_days.id", ondelete="CASCADE"), nullable=False),
        sa.Column("task_id", sa.String(36), sa.ForeignKey("plan_tasks.id", ondelete="CASCADE"), nullable=False),
        sa.Column("scheduled_date", sa.Date(), nullable=False),
        sa.Column("start_local", sa.String(5), nullable=False),
        sa.Column("end_local", sa.String(5), nullable=False),
        sa.Column("timezone", sa.String(64), nullable=False, server_default="Asia/Shanghai"),
        sa.Column("source", sa.String(16), nullable=False, server_default="SYSTEM"),
        sa.Column("status", sa.String(16), nullable=False, server_default="PLANNED"),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("change_reason", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_task_time_blocks_goal_id", "task_time_blocks", ["goal_id"])
    op.create_index("ix_task_time_blocks_plan_version_id", "task_time_blocks", ["plan_version_id"])
    op.create_index("ix_task_time_blocks_plan_day_id", "task_time_blocks", ["plan_day_id"])
    op.create_index("ix_task_time_blocks_task_id", "task_time_blocks", ["task_id"])
    op.create_index("ix_task_time_blocks_scheduled_date", "task_time_blocks", ["scheduled_date"])
    op.create_index("idx_task_time_blocks_goal_date", "task_time_blocks", ["goal_id", "scheduled_date"])
    op.create_index("idx_task_time_blocks_plan_day", "task_time_blocks", ["plan_day_id"])

    op.create_table(
        "schedule_changes",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("goal_id", sa.String(36), sa.ForeignKey("goals.id", ondelete="CASCADE"), nullable=False),
        sa.Column("entity_type", sa.String(24), nullable=False),
        sa.Column("entity_id", sa.String(36), nullable=True),
        sa.Column("change_kind", sa.String(24), nullable=False),
        sa.Column("before_json", sa.JSON(), nullable=False),
        sa.Column("after_json", sa.JSON(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_schedule_changes_goal_id", "schedule_changes", ["goal_id"])
    op.create_index("ix_schedule_changes_entity_id", "schedule_changes", ["entity_id"])


def downgrade() -> None:
    for name, table in (
        ("ix_schedule_changes_entity_id", "schedule_changes"),
        ("ix_schedule_changes_goal_id", "schedule_changes"),
    ):
        op.drop_index(name, table_name=table)
    op.drop_table("schedule_changes")
    for name in (
        "idx_task_time_blocks_plan_day",
        "idx_task_time_blocks_goal_date",
        "ix_task_time_blocks_scheduled_date",
        "ix_task_time_blocks_task_id",
        "ix_task_time_blocks_plan_day_id",
        "ix_task_time_blocks_plan_version_id",
        "ix_task_time_blocks_goal_id",
    ):
        op.drop_index(name, table_name="task_time_blocks")
    op.drop_table("task_time_blocks")
    for name, table in (
        ("idx_availability_exceptions_goal_date", "availability_exceptions"),
        ("ix_availability_exceptions_goal_id", "availability_exceptions"),
    ):
        op.drop_index(name, table_name=table)
    op.drop_table("availability_exceptions")
    for name in ("idx_availability_windows_goal_weekday", "ix_availability_windows_goal_id"):
        op.drop_index(name, table_name="availability_windows")
    op.drop_table("availability_windows")
    op.drop_index("ix_plan_schedule_preferences_goal_id", table_name="plan_schedule_preferences")
    op.drop_table("plan_schedule_preferences")
