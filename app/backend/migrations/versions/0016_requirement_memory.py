"""add auditable, evolving requirement memory"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0016_requirement_memory"
down_revision: str | None = "0015_goal_lifecycle"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("requirement_facts") as batch_op:
        batch_op.add_column(sa.Column("operation", sa.String(24), nullable=False, server_default="ADD"))
        batch_op.add_column(sa.Column("change_reason", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True))

    with op.batch_alter_table("conversation_summaries") as batch_op:
        batch_op.add_column(sa.Column("current_topic", sa.Text(), nullable=True))
        batch_op.add_column(
            sa.Column("recent_changes_json", sa.JSON(), nullable=False, server_default=sa.text("'[]'"))
        )
        batch_op.add_column(
            sa.Column("entity_refs_json", sa.JSON(), nullable=False, server_default=sa.text("'[]'"))
        )
        batch_op.add_column(
            sa.Column("readiness_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'"))
        )

    op.create_table(
        "requirement_changes",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("goal_id", sa.String(36), sa.ForeignKey("goals.id", ondelete="CASCADE"), nullable=False),
        sa.Column("field_key", sa.String(64), nullable=False),
        sa.Column("operation", sa.String(32), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("old_fact_id", sa.String(36), sa.ForeignKey("requirement_facts.id", ondelete="SET NULL"), nullable=True),
        sa.Column("new_fact_id", sa.String(36), sa.ForeignKey("requirement_facts.id", ondelete="SET NULL"), nullable=True),
        sa.Column("evidence_message_id", sa.String(36), sa.ForeignKey("messages.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_requirement_changes_goal_id", "requirement_changes", ["goal_id"])
    op.create_index("ix_requirement_changes_field_key", "requirement_changes", ["field_key"])

    op.execute(
        sa.text(
            "UPDATE requirement_facts SET confirmed_at = created_at "
            "WHERE status = 'CONFIRMED' AND confirmed_at IS NULL"
        )
    )


def downgrade() -> None:
    op.drop_index("ix_requirement_changes_field_key", table_name="requirement_changes")
    op.drop_index("ix_requirement_changes_goal_id", table_name="requirement_changes")
    op.drop_table("requirement_changes")
    with op.batch_alter_table("conversation_summaries") as batch_op:
        batch_op.drop_column("readiness_json")
        batch_op.drop_column("entity_refs_json")
        batch_op.drop_column("recent_changes_json")
        batch_op.drop_column("current_topic")
    with op.batch_alter_table("requirement_facts") as batch_op:
        batch_op.drop_column("confirmed_at")
        batch_op.drop_column("change_reason")
        batch_op.drop_column("operation")
