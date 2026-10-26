"""store explicit confirmation for overlapping tasks"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0020_parallel_schedule_confirmation"
down_revision: str | None = "0019_shared_schedule_manual_plans"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("task_time_blocks", sa.Column("parallel_confirmed", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("task_time_blocks", sa.Column("parallel_reason", sa.Text(), nullable=True))
    op.add_column("manual_tasks", sa.Column("parallel_confirmed", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("manual_tasks", sa.Column("parallel_reason", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("manual_tasks", "parallel_reason")
    op.drop_column("manual_tasks", "parallel_confirmed")
    op.drop_column("task_time_blocks", "parallel_reason")
    op.drop_column("task_time_blocks", "parallel_confirmed")
