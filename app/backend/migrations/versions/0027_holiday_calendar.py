"""add workspace holiday preference and annual calendar cache"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0027_holiday_calendar"
down_revision: str | None = "0026_manual_task_override_status"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("workspace_schedule_profiles") as batch_op:
        batch_op.add_column(sa.Column("holiday_country_code", sa.String(2), nullable=True))
        batch_op.add_column(sa.Column("holiday_subdivision_code", sa.String(16), nullable=True))
        batch_op.add_column(sa.Column("holiday_locale", sa.String(32), nullable=True))
        batch_op.add_column(
            sa.Column("holiday_source", sa.String(24), nullable=False, server_default="UNSET")
        )
        batch_op.add_column(
            sa.Column("holiday_confidence", sa.Float(), nullable=False, server_default="0")
        )
        batch_op.add_column(
            sa.Column(
                "holiday_confirmed", sa.Boolean(), nullable=False, server_default=sa.false()
            )
        )
    op.create_table(
        "holiday_calendar_years",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("country_code", sa.String(2), nullable=False),
        sa.Column("subdivision_code", sa.String(16), nullable=False, server_default=""),
        sa.Column("year", sa.Integer(), nullable=False),
        sa.Column("days_json", sa.JSON(), nullable=False),
        sa.Column("source", sa.String(80), nullable=False),
        sa.Column("authoritative", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "country_code",
            "subdivision_code",
            "year",
            name="uq_holiday_calendar_region_year",
        ),
    )
    op.create_index(
        "ix_holiday_calendar_years_country_code",
        "holiday_calendar_years",
        ["country_code"],
    )
    op.create_index("ix_holiday_calendar_years_year", "holiday_calendar_years", ["year"])


def downgrade() -> None:
    op.drop_index("ix_holiday_calendar_years_year", table_name="holiday_calendar_years")
    op.drop_index("ix_holiday_calendar_years_country_code", table_name="holiday_calendar_years")
    op.drop_table("holiday_calendar_years")
    with op.batch_alter_table("workspace_schedule_profiles") as batch_op:
        batch_op.drop_column("holiday_confirmed")
        batch_op.drop_column("holiday_confidence")
        batch_op.drop_column("holiday_source")
        batch_op.drop_column("holiday_locale")
        batch_op.drop_column("holiday_subdivision_code")
        batch_op.drop_column("holiday_country_code")
