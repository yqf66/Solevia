"""add persistent light arrangement drafts and shared plan metadata"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0034_light_arrangements"
down_revision: str | None = "0033_flexible_routine"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("manual_task_date_overrides", sa.Column("description", sa.Text(), nullable=True))
    op.add_column(
        "manual_task_date_overrides", sa.Column("estimate_minutes", sa.Integer(), nullable=True)
    )
    op.add_column("manual_task_date_overrides", sa.Column("priority", sa.Integer(), nullable=True))
    op.add_column(
        "manual_plans",
        sa.Column(
            "kind",
            sa.String(16),
            nullable=False,
            server_default="MANUAL",
        ),
    )
    op.add_column(
        "manual_plans",
        sa.Column(
            "arrangement_json",
            sa.JSON(),
            nullable=False,
            server_default="{}",
        ),
    )
    op.create_table(
        "light_arrangement_drafts",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("raw_input", sa.Text(), nullable=False),
        sa.Column("timezone", sa.String(64), nullable=False),
        sa.Column("start_date", sa.Date(), nullable=False),
        sa.Column("horizon_days", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("result_json", sa.JSON(), nullable=False),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column(
            "manual_plan_id",
            sa.String(36),
            sa.ForeignKey("manual_plans.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("light_arrangement_drafts")
    op.drop_column("manual_plans", "arrangement_json")
    op.drop_column("manual_plans", "kind")
    op.drop_column("manual_task_date_overrides", "priority")
    op.drop_column("manual_task_date_overrides", "estimate_minutes")
    op.drop_column("manual_task_date_overrides", "description")
