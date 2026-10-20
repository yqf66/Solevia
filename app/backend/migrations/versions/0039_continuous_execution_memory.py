"""Add durable semantic keys and deferred execution reviews."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0039_continuous_execution_memory"
down_revision: str | None = "0038_repair_legacy_schedule_types"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("execution_insights") as batch:
        batch.add_column(sa.Column("memory_key", sa.String(80), nullable=True))
        batch.create_index("ix_execution_insights_memory_key", ["memory_key"])
    with op.batch_alter_table("execution_reviews") as batch:
        batch.add_column(sa.Column("not_before", sa.Date(), nullable=True))
        batch.create_index("ix_execution_reviews_not_before", ["not_before"])
    # These rows were already extracted solely from explicit user messages.
    # The old UI made users approve them a second time; retain them as normal memory.
    op.execute(
        "UPDATE execution_insights "
        "SET status = 'CONFIRMED', decided_at = created_at "
        "WHERE status = 'PROPOSED'"
    )


def downgrade() -> None:
    with op.batch_alter_table("execution_reviews") as batch:
        batch.drop_index("ix_execution_reviews_not_before")
        batch.drop_column("not_before")
    with op.batch_alter_table("execution_insights") as batch:
        batch.drop_index("ix_execution_insights_memory_key")
        batch.drop_column("memory_key")
