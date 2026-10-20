"""Store explainable research ranking metadata."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0048_research_quality_metadata"
down_revision: str | None = "0047_research_persistence"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "research_evidence",
        sa.Column("ranking_json", sa.JSON(), nullable=False, server_default="{}"),
    )


def downgrade() -> None:
    op.drop_column("research_evidence", "ranking_json")
