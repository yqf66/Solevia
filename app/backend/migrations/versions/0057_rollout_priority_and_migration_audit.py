"""Persist scheduler priority and Hybrid rollout checkpoints."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0057_rollout_priority_and_migration_audit"
down_revision: str | None = "0056_normalize_legacy_json_values"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "plan_schedule_preferences",
        sa.Column("cross_plan_priority_mode", sa.String(length=16), nullable=False, server_default="BALANCED"),
    )
    op.create_table(
        "hybrid_migration_audits",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("plan_version_id", sa.String(length=36), nullable=False),
        sa.Column("source_execution_model", sa.String(length=32), nullable=False, server_default="LEGACY_DAY_BASED"),
        sa.Column("target_execution_model", sa.String(length=32), nullable=False, server_default="HYBRID_WORK_ITEM"),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="PENDING"),
        sa.Column("rollout_bucket", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("details_json", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["plan_version_id"], ["plan_versions.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("plan_version_id"),
    )
    op.create_index("ix_hybrid_migration_audits_plan_version_id", "hybrid_migration_audits", ["plan_version_id"])
    op.create_index("ix_hybrid_migration_audits_status", "hybrid_migration_audits", ["status"])


def downgrade() -> None:
    op.drop_index("ix_hybrid_migration_audits_status", table_name="hybrid_migration_audits")
    op.drop_index("ix_hybrid_migration_audits_plan_version_id", table_name="hybrid_migration_audits")
    op.drop_table("hybrid_migration_audits")
    op.drop_column("plan_schedule_preferences", "cross_plan_priority_mode")
