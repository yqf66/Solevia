"""make daily routine exclusions optional and extensible

Revision ID: 0033_flexible_routine
Revises: 0032_user_preferences
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0033_flexible_routine"
down_revision: str | None = "0032_user_preferences"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    columns = (
        sa.Column("available_from", sa.String(5), nullable=False, server_default="08:00"),
        sa.Column("breakfast_enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("lunch_enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("nap_enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("dinner_enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("routine_exclusions_json", sa.JSON(), nullable=False, server_default="[]"),
    )
    for column in columns:
        op.add_column("workspace_schedule_profiles", column)


def downgrade() -> None:
    for name in (
        "routine_exclusions_json", "dinner_enabled", "nap_enabled", "lunch_enabled",
        "breakfast_enabled", "available_from",
    ):
        op.drop_column("workspace_schedule_profiles", name)
