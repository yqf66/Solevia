"""Add durable material processing jobs and SQLite FTS5 index."""

import re
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0051_material_jobs_fts"
down_revision: str | None = "0050_material_chunks"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "material_processing_jobs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("revision_id", sa.String(36), nullable=False),
        sa.Column("status", sa.String(24), nullable=False, server_default="QUEUED"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["revision_id"], ["material_revisions.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("revision_id", name="uq_material_processing_job_revision"),
    )
    op.create_index(
        "ix_material_processing_jobs_revision_id",
        "material_processing_jobs",
        ["revision_id"],
    )
    op.create_index(
        "ix_material_processing_jobs_status",
        "material_processing_jobs",
        ["status"],
    )
    op.create_index(
        "ix_material_processing_jobs_status_created_at",
        "material_processing_jobs",
        ["status", "created_at"],
    )
    op.execute(
        """
        CREATE VIRTUAL TABLE material_chunks_fts USING fts5(
            chunk_id UNINDEXED,
            revision_id UNINDEXED,
            search_text,
            tokenize='unicode61 remove_diacritics 2'
        )
        """
    )
    connection = op.get_bind()
    chunks = connection.exec_driver_sql(
        "SELECT id, revision_id, section_title, text FROM material_chunks"
    )
    while batch := chunks.mappings().fetchmany(100):
        rows = []
        for chunk in batch:
            terms = _fts_terms(f"{chunk['section_title']}\n{chunk['text']}")
            if terms:
                rows.append((chunk["id"], chunk["revision_id"], " ".join(terms)))
        if rows:
            connection.exec_driver_sql(
                """
                INSERT INTO material_chunks_fts(chunk_id, revision_id, search_text)
                VALUES (?, ?, ?)
                """,
                rows,
            )
    op.execute(
        """
        CREATE TRIGGER material_chunks_fts_delete
        AFTER DELETE ON material_chunks
        BEGIN
            DELETE FROM material_chunks_fts WHERE chunk_id = old.id;
        END
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS material_chunks_fts_delete")
    op.execute("DROP TABLE IF EXISTS material_chunks_fts")
    op.drop_index(
        "ix_material_processing_jobs_status_created_at",
        table_name="material_processing_jobs",
    )
    op.drop_index(
        "ix_material_processing_jobs_status",
        table_name="material_processing_jobs",
    )
    op.drop_index(
        "ix_material_processing_jobs_revision_id",
        table_name="material_processing_jobs",
    )
    op.drop_table("material_processing_jobs")


def _fts_terms(value: str) -> list[str]:
    terms = re.findall(r"[a-z0-9_]+", value.casefold())
    for sequence in re.findall(r"[\u4e00-\u9fff]+", value):
        if len(sequence) == 1:
            terms.append(sequence)
        else:
            terms.extend(sequence[index : index + 2] for index in range(len(sequence) - 1))
    stop_words = {"a", "an", "and", "for", "in", "is", "of", "on", "or", "the", "to"}
    return [term for term in dict.fromkeys(terms) if term and term not in stop_words]
