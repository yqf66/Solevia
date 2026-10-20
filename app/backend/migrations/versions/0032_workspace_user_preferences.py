"""add workspace routine and lightweight scheduling preferences

Revision ID: 0032_user_preferences
Revises: 0031_task_splittable
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0032_user_preferences"
down_revision: str | None = "0031_task_splittable"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    columns = (
        sa.Column("availability_mode", sa.String(16), nullable=False, server_default="ROUTINE"),
        sa.Column("wake_time", sa.String(5), nullable=False, server_default="07:00"),
        sa.Column("breakfast_start", sa.String(5), nullable=False, server_default="07:30"),
        sa.Column("breakfast_end", sa.String(5), nullable=False, server_default="08:00"),
        sa.Column("lunch_start", sa.String(5), nullable=False, server_default="12:00"),
        sa.Column("lunch_end", sa.String(5), nullable=False, server_default="13:00"),
        sa.Column("nap_start", sa.String(5), nullable=False, server_default="13:00"),
        sa.Column("nap_end", sa.String(5), nullable=False, server_default="14:00"),
        sa.Column("dinner_start", sa.String(5), nullable=False, server_default="18:00"),
        sa.Column("dinner_end", sa.String(5), nullable=False, server_default="19:00"),
        sa.Column("sleep_time", sa.String(5), nullable=False, server_default="23:00"),
        sa.Column("schedule_style", sa.Text(), nullable=False, server_default=""),
    )
    for column in columns:
        op.add_column("workspace_schedule_profiles", column)
    op.execute(
        """
        UPDATE workspace_schedule_profiles
        SET availability_mode = 'CUSTOM'
        WHERE EXISTS (
            SELECT 1 FROM workspace_availability_windows
            WHERE workspace_availability_windows.profile_id = workspace_schedule_profiles.id
        )
        """
    )


def downgrade() -> None:
    for name in (
        "schedule_style", "sleep_time", "dinner_end", "dinner_start", "nap_end",
        "nap_start", "lunch_end", "lunch_start", "breakfast_end", "breakfast_start",
        "wake_time", "availability_mode",
    ):
        op.drop_column("workspace_schedule_profiles", name)
