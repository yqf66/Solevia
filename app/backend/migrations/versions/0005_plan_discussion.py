"""add plan discussion revision metadata

Revision ID: 0005_plan_discussion
Revises: 0004_relative_plans
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005_plan_discussion"
down_revision: str | None = "0004_relative_plans"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "plan_versions",
        sa.Column(
            "change_summary_json",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'[]'"),
        ),
    )
    op.add_column("plan_versions", sa.Column("feedback", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("plan_versions", "feedback")
    op.drop_column("plan_versions", "change_summary_json")
