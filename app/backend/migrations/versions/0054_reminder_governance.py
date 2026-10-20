"""Add per-goal reminder and supervision controls."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "0054_reminder_governance"
down_revision: str | None = "0053_research_run_leases"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "plan_schedule_preferences",
        sa.Column("reminders_enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
    )
    op.add_column(
        "plan_schedule_preferences",
        sa.Column("supervision_mode", sa.String(length=16), nullable=False, server_default="STANDARD"),
    )
    op.add_column(
        "plan_schedule_preferences",
        sa.Column("quiet_start_local", sa.String(length=5), nullable=True),
    )
    op.add_column(
        "plan_schedule_preferences",
        sa.Column("quiet_end_local", sa.String(length=5), nullable=True),
    )
    op.add_column(
        "plan_schedule_preferences",
        sa.Column(
            "muted_commitments_json",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'[]'"),
        ),
    )


def downgrade() -> None:
    op.drop_column("plan_schedule_preferences", "muted_commitments_json")
    op.drop_column("plan_schedule_preferences", "quiet_end_local")
    op.drop_column("plan_schedule_preferences", "quiet_start_local")
    op.drop_column("plan_schedule_preferences", "supervision_mode")
    op.drop_column("plan_schedule_preferences", "reminders_enabled")
