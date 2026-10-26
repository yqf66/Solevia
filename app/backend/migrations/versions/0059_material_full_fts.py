"""Rebuild truncated material indexes using all chunk terms."""

import re
from collections.abc import Sequence

from alembic import op

revision: str = "0059_material_full_fts"
down_revision: str | None = "0058_material_analysis"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    connection = op.get_bind()
    connection.exec_driver_sql("DELETE FROM material_chunks_fts")
    chunks = connection.exec_driver_sql(
        "SELECT id, revision_id, section_title, text FROM material_chunks"
    )
    while batch := chunks.mappings().fetchmany(100):
        rows = []
        for chunk in batch:
            value = f"{chunk['section_title']}\n{chunk['text']}"
            terms = re.findall(r"[a-z0-9_]+", value.casefold())
            for sequence in re.findall(r"[\u4e00-\u9fff]+", value):
                terms.extend(
                    [sequence]
                    if len(sequence) == 1
                    else [sequence[index : index + 2] for index in range(len(sequence) - 1)]
                )
            stop_words = {"a", "an", "and", "for", "in", "is", "of", "on", "or", "the", "to"}
            search_text = " ".join(sorted(set(terms) - stop_words))
            if search_text:
                rows.append((chunk["id"], chunk["revision_id"], search_text))
        if rows:
            connection.exec_driver_sql(
                "INSERT INTO material_chunks_fts(chunk_id, revision_id, search_text) VALUES (?, ?, ?)",
                rows,
            )


def downgrade() -> None:
    # Full indexes remain compatible with the preceding application version.
    pass
