"""Bounded shared original-text knowledge cache."""
from alembic import op
import sqlalchemy as sa

revision = "0064_shared_knowledge"
down_revision = "0063_fixed_activity_move_previews"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("knowledge_documents",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("identity", sa.String(64), nullable=False, unique=True),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("source_uri", sa.Text(), nullable=False),
        sa.Column("title", sa.String(300), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("material_revision_id", sa.String(36), sa.ForeignKey("material_revisions.id", ondelete="CASCADE")),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("shared", sa.Boolean(), nullable=False),
        sa.Column("current", sa.Boolean(), nullable=False),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.Column("byte_size", sa.BigInteger(), nullable=False),
        sa.Column("chunk_count", sa.Integer(), nullable=False),
        sa.Column("accessed_count", sa.Integer(), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_accessed_at", sa.DateTime(timezone=True), nullable=False))
    op.create_index("ix_knowledge_documents_material_revision_id", "knowledge_documents", ["material_revision_id"])
    op.create_table("knowledge_goal_links",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("goal_id", sa.String(36), sa.ForeignKey("goals.id", ondelete="CASCADE"), nullable=False),
        sa.Column("document_id", sa.String(36), sa.ForeignKey("knowledge_documents.id", ondelete="CASCADE"), nullable=False),
        sa.UniqueConstraint("goal_id", "document_id", name="uq_knowledge_goal_document"))
    op.create_index("ix_knowledge_goal_links_goal_id", "knowledge_goal_links", ["goal_id"])
    op.create_index("ix_knowledge_goal_links_document_id", "knowledge_goal_links", ["document_id"])
    op.create_table("knowledge_chunks",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("document_id", sa.String(36), sa.ForeignKey("knowledge_documents.id", ondelete="CASCADE"), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("heading", sa.String(300), nullable=False),
        sa.Column("char_start", sa.Integer(), nullable=False),
        sa.Column("char_end", sa.Integer(), nullable=False),
        sa.Column("locator_json", sa.JSON(), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("vector_json", sa.JSON(), nullable=False),
        sa.Column("vector_model", sa.String(300), nullable=False),
        sa.Column("index_status", sa.String(16), nullable=False),
        sa.Column("index_attempts", sa.Integer(), nullable=False),
        sa.Column("lease_id", sa.String(36)),
        sa.Column("lease_until", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("document_id", "sequence", name="uq_knowledge_chunk_sequence"))
    op.create_index("ix_knowledge_chunks_document_id", "knowledge_chunks", ["document_id"])
    op.create_index("ix_knowledge_chunks_index_status", "knowledge_chunks", ["index_status"])


def downgrade():
    op.drop_table("knowledge_chunks")
    op.drop_table("knowledge_goal_links")
    op.drop_table("knowledge_documents")
