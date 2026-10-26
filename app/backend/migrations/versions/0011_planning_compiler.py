"""persist planning compiler context, briefs, and generation checkpoints

Revision ID: 0011_planning_compiler
Revises: 0010_execution_companion
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011_planning_compiler"
down_revision: str | None = "0010_execution_companion"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "context_packs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "goal_id", sa.String(36), sa.ForeignKey("goals.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column(
            "requirement_version_id",
            sa.String(36),
            sa.ForeignKey("requirement_versions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "route_version_id",
            sa.String(36),
            sa.ForeignKey("route_versions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "plan_version_id",
            sa.String(36),
            sa.ForeignKey("plan_versions.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("facts_json", sa.JSON(), nullable=False),
        sa.Column("locked_day_ids_json", sa.JSON(), nullable=False),
        sa.Column("locked_task_ids_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    for column in ("goal_id", "requirement_version_id", "route_version_id", "plan_version_id"):
        op.create_index(f"ix_context_packs_{column}", "context_packs", [column])

    op.create_table(
        "planning_briefs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "goal_id", sa.String(36), sa.ForeignKey("goals.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column(
            "context_pack_id",
            sa.String(36),
            sa.ForeignKey("context_packs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "plan_version_id",
            sa.String(36),
            sa.ForeignKey("plan_versions.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("brief_kind", sa.String(24), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("brief_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
    )
    for column in ("goal_id", "context_pack_id", "plan_version_id", "brief_kind", "status"):
        op.create_index(f"ix_planning_briefs_{column}", "planning_briefs", [column])

    op.create_table(
        "generation_batches",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "goal_id", sa.String(36), sa.ForeignKey("goals.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column(
            "plan_version_id",
            sa.String(36),
            sa.ForeignKey("plan_versions.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "brief_id",
            sa.String(36),
            sa.ForeignKey("planning_briefs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("idempotency_key", sa.String(160), nullable=False),
        sa.Column("scope_type", sa.String(24), nullable=False),
        sa.Column("scope_json", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("failure_code", sa.String(64), nullable=True),
        sa.Column("failure_message", sa.Text(), nullable=True),
        sa.Column("staged_result_json", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("idempotency_key", name="uq_generation_batches_idempotency_key"),
    )
    for column in ("goal_id", "plan_version_id", "brief_id", "scope_type", "status"):
        op.create_index(f"ix_generation_batches_{column}", "generation_batches", [column])
    op.create_index(
        "ix_generation_batches_idempotency_key",
        "generation_batches",
        ["idempotency_key"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_table("generation_batches")
    op.drop_table("planning_briefs")
    op.drop_table("context_packs")
