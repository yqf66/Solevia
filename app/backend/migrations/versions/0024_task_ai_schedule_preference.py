"""persist whether Agent plan tasks should receive automatic time allocation"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0024_task_ai_schedule_preference"
down_revision: str | None = "0023_manual_task_recurrence"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "plan_tasks",
        sa.Column("ai_schedule_requested", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_column("plan_tasks", "ai_schedule_requested")
