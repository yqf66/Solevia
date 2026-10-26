"""Persist bounded searchable chunks for material revisions."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0050_material_chunks"
down_revision: str | None = "0049_research_resume_stage"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "material_chunks",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("revision_id", sa.String(36), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("section_title", sa.String(300), nullable=False, server_default=""),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("char_start", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("char_end", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["revision_id"], ["material_revisions.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("revision_id", "sequence", name="uq_material_chunk_sequence"),
    )
    op.create_index("ix_material_chunks_revision_id", "material_chunks", ["revision_id"])
    op.create_index("ix_material_chunks_content_hash", "material_chunks", ["content_hash"])


def downgrade() -> None:
    op.drop_table("material_chunks")
