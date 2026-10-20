"""Day 2 goal clarification memory.

Revision ID: 0001_day2
Revises:
Create Date: 2026-09-01
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001_day2"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "goals",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("title", sa.String(160), nullable=False),
        sa.Column("raw_input", sa.Text(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("current_requirement_version_id", sa.String(36), nullable=True),
        sa.Column("last_active_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_goals_status", "goals", ["status"])
    op.create_table(
        "conversations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "goal_id", sa.String(36), sa.ForeignKey("goals.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("phase", sa.String(32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_conversations_goal_id", "conversations", ["goal_id"])
    op.create_table(
        "messages",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "conversation_id",
            sa.String(36),
            sa.ForeignKey("conversations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("generation_status", sa.String(16), nullable=False),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_messages_conversation_id", "messages", ["conversation_id"])
    op.create_table(
        "requirement_facts",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "goal_id", sa.String(36), sa.ForeignKey("goals.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("category", sa.String(40), nullable=False),
        sa.Column("field_key", sa.String(64), nullable=False),
        sa.Column("value_json", sa.JSON(), nullable=False),
        sa.Column("source", sa.String(24), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column(
            "evidence_message_id",
            sa.String(36),
            sa.ForeignKey("messages.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "supersedes_fact_id",
            sa.String(36),
            sa.ForeignKey("requirement_facts.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_requirement_facts_goal_id", "requirement_facts", ["goal_id"])
    op.create_index("ix_requirement_facts_field_key", "requirement_facts", ["field_key"])
    op.create_index("ix_requirement_facts_status", "requirement_facts", ["status"])
    op.create_table(
        "conversation_summaries",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "conversation_id",
            sa.String(36),
            sa.ForeignKey("conversations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("through_message_sequence", sa.Integer(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("confirmed_conclusions_json", sa.JSON(), nullable=False),
        sa.Column("open_questions_json", sa.JSON(), nullable=False),
        sa.Column("conflicts_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_conversation_summaries_conversation_id", "conversation_summaries", ["conversation_id"]
    )
    op.create_table(
        "requirement_versions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "goal_id", sa.String(36), sa.ForeignKey("goals.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("snapshot_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_requirement_versions_goal_id", "requirement_versions", ["goal_id"])
    op.create_index("ix_requirement_versions_status", "requirement_versions", ["status"])


def downgrade() -> None:
    op.drop_table("requirement_versions")
    op.drop_table("conversation_summaries")
    op.drop_table("requirement_facts")
    op.drop_table("messages")
    op.drop_table("conversations")
    op.drop_table("goals")
