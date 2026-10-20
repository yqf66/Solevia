"""Normalize legacy requirement fact JSON values before runtime planning."""

import json
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0056_normalize_legacy_json_values"
down_revision: str | None = "0055_reminder_history"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Make every stored value valid JSON text.

    Early SQLite databases sometimes received a native integer or plain text in
    a JSON column.  SQLAlchemy then returns that value directly, while planning
    memory expects to be able to decode the JSON representation.  Re-encoding
    the value is lossless and keeps the decoded Python value unchanged.
    """
    connection = op.get_bind()
    rows = connection.execute(sa.text("SELECT id, value_json FROM requirement_facts")).fetchall()
    for fact_id, raw in rows:
        if isinstance(raw, str):
            try:
                json.loads(raw)
            except (TypeError, ValueError):
                encoded = json.dumps(raw, ensure_ascii=False)
            else:
                continue
        else:
            # SQLite's JSON affinity coerces a bare JSON number back to an
            # INTEGER column value.  Store scalar legacy values as JSON strings
            # so the database physically contains text and SQLAlchemy can
            # decode it consistently on every backend.
            if isinstance(raw, (int, float, bool)):
                encoded = json.dumps(str(raw), ensure_ascii=False)
            else:
                encoded = json.dumps(raw, ensure_ascii=False)
        connection.execute(
            sa.text("UPDATE requirement_facts SET value_json = :value_json WHERE id = :id"),
            {"id": fact_id, "value_json": encoded},
        )


def downgrade() -> None:
    # The previous representation was not consistent or recoverable, so it is
    # intentionally left normalized on downgrade.
    pass
