"""Persist the research-to-response handoff across page refreshes."""

from alembic import op
import sqlalchemy as sa

revision = "0061_research_response_requests"
down_revision = "0060_material_embeddings"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "research_response_requests",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("goal_id", sa.String(36), sa.ForeignKey("goals.id", ondelete="CASCADE"), nullable=False),
        sa.Column("run_id", sa.String(36), sa.ForeignKey("research_runs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("conversation_id", sa.String(36), sa.ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("phase", sa.String(24), nullable=False),
        sa.Column("intent", sa.String(24), nullable=False),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("idempotency_key", sa.String(160), nullable=False, unique=True),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("snapshot_json", sa.JSON(), nullable=False),
        sa.Column("fallback", sa.Boolean(), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("response_message_id", sa.String(36), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_research_response_requests_goal_id", "research_response_requests", ["goal_id"])
    op.create_index("ix_research_response_requests_status", "research_response_requests", ["status"])


def downgrade() -> None:
    op.drop_table("research_response_requests")
