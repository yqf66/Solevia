"""Persist goal-scoped negative constraints learned from feedback."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0045_goal_error_memory"
down_revision: str | None = "0044_context_resilience"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "goal_error_memories",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("goal_id", sa.String(36), nullable=False),
        sa.Column("memory_type", sa.String(24), nullable=False, server_default="ERROR_AVOIDANCE"),
        sa.Column("rule", sa.Text(), nullable=False),
        sa.Column("evidence", sa.Text(), nullable=False),
        sa.Column("source_message_id", sa.String(36), nullable=True),
        sa.Column("scope", sa.String(20), nullable=False, server_default="GOAL"),
        sa.Column("target_refs_json", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("status", sa.String(16), nullable=False, server_default="CONFIRMED"),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("valid_until", sa.Date(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["goal_id"], ["goals.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["source_message_id"], ["messages.id"], ondelete="SET NULL"),
        sa.UniqueConstraint("goal_id", "content_hash", name="uq_goal_error_memory_hash"),
    )
    op.create_index("ix_goal_error_memories_goal_id", "goal_error_memories", ["goal_id"])
    op.create_index(
        "ix_goal_error_memories_source_message_id", "goal_error_memories", ["source_message_id"]
    )
    op.create_index("ix_goal_error_memories_status", "goal_error_memories", ["status"])
    op.create_index(
        "ix_goal_error_memories_goal_status", "goal_error_memories", ["goal_id", "status"]
    )
    op.create_index("ix_goal_error_memories_content_hash", "goal_error_memories", ["content_hash"])


def downgrade() -> None:
    op.drop_index("ix_goal_error_memories_goal_status", table_name="goal_error_memories")
    op.drop_index("ix_goal_error_memories_content_hash", table_name="goal_error_memories")
    op.drop_index("ix_goal_error_memories_status", table_name="goal_error_memories")
    op.drop_index("ix_goal_error_memories_source_message_id", table_name="goal_error_memories")
    op.drop_index("ix_goal_error_memories_goal_id", table_name="goal_error_memories")
    op.drop_table("goal_error_memories")
