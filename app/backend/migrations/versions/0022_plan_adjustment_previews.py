"""persist ordinary AI adjustment drafts for review before publishing"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0022_plan_adjustment_previews"
down_revision: str | None = "0021_parallel_pair_consent"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "plan_adjustment_previews",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("goal_id", sa.String(36), sa.ForeignKey("goals.id", ondelete="CASCADE"), nullable=False),
        sa.Column("source_plan_version_id", sa.String(36), sa.ForeignKey("plan_versions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("status", sa.String(24), nullable=False, server_default="PREVIEW"),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("preview_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("applied_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_plan_adjustment_previews_goal_id", "plan_adjustment_previews", ["goal_id"])
    op.create_index("ix_plan_adjustment_previews_source_plan_version_id", "plan_adjustment_previews", ["source_plan_version_id"])
    op.create_index("ix_plan_adjustment_previews_status", "plan_adjustment_previews", ["status"])
    op.create_index("ix_plan_adjustment_previews_fingerprint", "plan_adjustment_previews", ["fingerprint"])


def downgrade() -> None:
    op.drop_table("plan_adjustment_previews")
