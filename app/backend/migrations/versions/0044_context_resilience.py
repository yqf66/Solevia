"""Add hierarchical conversation summary metadata and execution memory provenance."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0044_context_resilience"
down_revision: str | None = "0043_work_item_generation_contract"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "user_memory_items",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("profile_id", sa.String(36), nullable=False, server_default="default"),
        sa.Column("memory_key", sa.String(80), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("source_type", sa.String(24), nullable=False, server_default="EXPLICIT_USER"),
        sa.Column("status", sa.String(16), nullable=False, server_default="CONFIRMED"),
        sa.Column("evidence_message_id", sa.String(36), nullable=True),
        sa.Column("valid_from", sa.Date(), nullable=True),
        sa.Column("valid_until", sa.Date(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["evidence_message_id"], ["messages.id"], ondelete="SET NULL"),
        sa.UniqueConstraint("profile_id", "memory_key", name="uq_user_memory_profile_key"),
    )
    op.create_index("ix_user_memory_items_profile_id", "user_memory_items", ["profile_id"])
    op.create_index("ix_user_memory_items_status", "user_memory_items", ["status"])
    with op.batch_alter_table("conversation_summaries") as batch:
        batch.add_column(
            sa.Column("from_message_sequence", sa.Integer(), nullable=False, server_default="1")
        )
        batch.add_column(
            sa.Column("summary_level", sa.String(16), nullable=False, server_default="PHASE")
        )
        batch.add_column(
            sa.Column("source_summary_ids_json", sa.JSON(), nullable=False, server_default="[]")
        )
        batch.add_column(sa.Column("content_hash", sa.String(64), nullable=True))
        batch.add_column(sa.Column("superseded_at", sa.DateTime(timezone=True), nullable=True))
        batch.create_index("ix_conversation_summaries_summary_level", ["summary_level"])
        batch.create_index("ix_conversation_summaries_content_hash", ["content_hash"])

    with op.batch_alter_table("execution_insights") as batch:
        batch.add_column(
            sa.Column(
                "source_type", sa.String(24), nullable=False, server_default="AGENT_INFERENCE"
            )
        )
        batch.add_column(sa.Column("evidence_span", sa.Text(), nullable=True))
        batch.add_column(
            sa.Column(
                "confirmation_policy", sa.String(24), nullable=False, server_default="USER_REVIEW"
            )
        )
        batch.add_column(sa.Column("valid_from", sa.Date(), nullable=True))
        batch.add_column(sa.Column("valid_until", sa.Date(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("execution_insights") as batch:
        batch.drop_column("valid_until")
        batch.drop_column("valid_from")
        batch.drop_column("confirmation_policy")
        batch.drop_column("evidence_span")
        batch.drop_column("source_type")
    with op.batch_alter_table("conversation_summaries") as batch:
        batch.drop_index("ix_conversation_summaries_content_hash")
        batch.drop_index("ix_conversation_summaries_summary_level")
        batch.drop_column("superseded_at")
        batch.drop_column("content_hash")
        batch.drop_column("source_summary_ids_json")
        batch.drop_column("summary_level")
        batch.drop_column("from_message_sequence")
    op.drop_index("ix_user_memory_items_status", table_name="user_memory_items")
    op.drop_index("ix_user_memory_items_profile_id", table_name="user_memory_items")
    op.drop_table("user_memory_items")
