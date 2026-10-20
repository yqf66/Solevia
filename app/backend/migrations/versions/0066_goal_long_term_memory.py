"""Goal-scoped user memories, independent of evictable evidence cache."""
from alembic import op
import sqlalchemy as sa

revision = "0066_goal_long_term_memory"
down_revision = "0065_knowledge_milvus_sync"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("goal_long_term_memories",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("goal_id", sa.String(36), sa.ForeignKey("goals.id", ondelete="CASCADE"), nullable=False),
        sa.Column("memory_key", sa.String(100), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("evidence_quote", sa.Text(), nullable=False),
        sa.Column("source_message_id", sa.String(36), sa.ForeignKey("messages.id", ondelete="CASCADE"), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("goal_id", "memory_key", name="uq_goal_long_memory_key"))
    op.create_index("ix_goal_long_term_memories_goal_id", "goal_long_term_memories", ["goal_id"])


def downgrade():
    op.drop_table("goal_long_term_memories")
