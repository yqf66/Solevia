"""Persist model-specific local material embeddings."""

import sqlalchemy as sa
from alembic import op

revision = "0060_material_embeddings"
down_revision = "0059_material_full_fts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "material_embeddings",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "chunk_id",
            sa.String(36),
            sa.ForeignKey("material_chunks.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("model", sa.String(300), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("vector_json", sa.JSON(), nullable=False),
        sa.UniqueConstraint("chunk_id", "model", name="uq_material_embedding_model"),
    )
    op.create_index("ix_material_embeddings_chunk_id", "material_embeddings", ["chunk_id"])
    op.create_index("ix_material_embeddings_model", "material_embeddings", ["model"])


def downgrade() -> None:
    op.drop_table("material_embeddings")
