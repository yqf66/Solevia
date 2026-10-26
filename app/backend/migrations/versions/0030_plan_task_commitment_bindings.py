"""store explicit task commitment bindings

Revision ID: 0030_task_commitments
Revises: 0029_plan_commitments
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0030_task_commitments"
down_revision: str | None = "0029_plan_commitments"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "plan_tasks",
        sa.Column(
            "satisfies_commitments_json",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'[]'"),
        ),
    )


def downgrade() -> None:
    op.drop_column("plan_tasks", "satisfies_commitments_json")
