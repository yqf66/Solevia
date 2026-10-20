"""Add user-confirmed schedule types to the planning pipeline."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0036_schedule_types"
down_revision: str | None = "0035_quick_plans_and_rolling"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "goals", sa.Column("schedule_type", sa.String(16), nullable=False, server_default="RHYTHM")
    )
    op.add_column(
        "goals",
        sa.Column("schedule_type_confirmed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.execute("UPDATE goals SET schedule_type_confirmed_at = created_at")
    op.add_column(
        "route_versions",
        sa.Column("schedule_type", sa.String(16), nullable=False, server_default="RHYTHM"),
    )
    op.add_column(
        "route_versions",
        sa.Column("optional_questions_json", sa.JSON(), nullable=False, server_default="[]"),
    )
    op.add_column(
        "plan_versions",
        sa.Column("schedule_type", sa.String(16), nullable=False, server_default="RHYTHM"),
    )
    op.add_column(
        "plan_generation_jobs",
        sa.Column("schedule_type", sa.String(16), nullable=False, server_default="RHYTHM"),
    )


def downgrade() -> None:
    op.drop_column("plan_generation_jobs", "schedule_type")
    op.drop_column("plan_versions", "schedule_type")
    op.drop_column("route_versions", "schedule_type")
    op.drop_column("route_versions", "optional_questions_json")
    op.drop_column("goals", "schedule_type_confirmed_at")
    op.drop_column("goals", "schedule_type")
