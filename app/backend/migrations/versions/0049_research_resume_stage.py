"""Store the stage used when resuming interrupted research."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0049_research_resume_stage"
down_revision: str | None = "0048_research_quality_metadata"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("research_runs", sa.Column("resume_stage", sa.String(24), nullable=True))


def downgrade() -> None:
    op.drop_column("research_runs", "resume_stage")
