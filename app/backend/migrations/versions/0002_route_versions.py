"""add route versions and stages

Revision ID: 0002_route_versions
Revises: 0001_day2
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002_route_versions"
down_revision: str | None = "0001_day2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("goals", sa.Column("active_route_version_id", sa.String(36), nullable=True))
    op.create_table(
        "route_versions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "goal_id", sa.String(36), sa.ForeignKey("goals.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column(
            "requirement_version_id",
            sa.String(36),
            sa.ForeignKey("requirement_versions.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("tradeoffs_json", sa.JSON(), nullable=False),
        sa.Column("assumptions_json", sa.JSON(), nullable=False),
        sa.Column("open_questions_json", sa.JSON(), nullable=False),
        sa.Column("change_summary_json", sa.JSON(), nullable=False),
        sa.Column("feedback", sa.Text(), nullable=True),
        sa.Column("prompt_version", sa.String(40), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_route_versions_goal_id", "route_versions", ["goal_id"])
    op.create_index(
        "ix_route_versions_requirement_version_id", "route_versions", ["requirement_version_id"]
    )
    op.create_index("ix_route_versions_status", "route_versions", ["status"])
    op.create_table(
        "route_stages",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "route_version_id",
            sa.String(36),
            sa.ForeignKey("route_versions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("order", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(160), nullable=False),
        sa.Column("purpose", sa.Text(), nullable=False),
        sa.Column("topics_json", sa.JSON(), nullable=False),
        sa.Column("outputs_json", sa.JSON(), nullable=False),
        sa.Column("depends_on_json", sa.JSON(), nullable=False),
    )
    op.create_index("ix_route_stages_route_version_id", "route_stages", ["route_version_id"])


def downgrade() -> None:
    op.drop_table("route_stages")
    op.drop_table("route_versions")
    op.drop_column("goals", "active_route_version_id")
