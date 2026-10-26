"""store measurable plan commitments

Revision ID: 0029_plan_commitments
Revises: 0028_semantic_planning_documents
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0029_plan_commitments"
down_revision: str | None = "0028_semantic_planning_documents"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "plan_versions",
        sa.Column(
            "commitments_json",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'[]'"),
        ),
    )


def downgrade() -> None:
    op.drop_column("plan_versions", "commitments_json")
