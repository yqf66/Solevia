"""mark whether a plan task may be partially executed

Revision ID: 0031_task_splittable
Revises: 0030_task_commitments
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0031_task_splittable"
down_revision: str | None = "0030_task_commitments"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "plan_tasks",
        sa.Column(
            "is_splittable",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
    )
    op.execute(
        """
        UPDATE plan_tasks
        SET is_splittable = 0
        WHERE title LIKE '%整套%'
           OR title LIKE '%套卷%'
           OR title LIKE '%模拟卷%'
           OR title LIKE '%全真%'
           OR title LIKE '%考试%'
           OR title LIKE '%面试%'
           OR title LIKE '%会议%'
           OR title LIKE '%直播%'
        """
    )


def downgrade() -> None:
    op.drop_column("plan_tasks", "is_splittable")
