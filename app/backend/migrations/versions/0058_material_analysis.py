"""Persist bounded AI analyses of local materials."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0058_material_analysis"
down_revision: str | None = "0057_rollout_priority_and_migration_audit"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "material_analyses",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("goal_id", sa.String(length=36), nullable=False),
        sa.Column("query", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False, server_default="COMPLETED"),
        sa.Column("result_json", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("source_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["goal_id"], ["goals.id"], ondelete="CASCADE"),
    )
    op.create_index("ix_material_analyses_goal_id", "material_analyses", ["goal_id"])
    op.create_index(
        "ix_material_analyses_goal_created_at",
        "material_analyses",
        ["goal_id", "created_at"],
    )
    op.create_index("ix_material_analyses_status", "material_analyses", ["status"])


def downgrade() -> None:
    op.drop_index("ix_material_analyses_status", table_name="material_analyses")
    op.drop_index("ix_material_analyses_goal_created_at", table_name="material_analyses")
    op.drop_index("ix_material_analyses_goal_id", table_name="material_analyses")
    op.drop_table("material_analyses")
