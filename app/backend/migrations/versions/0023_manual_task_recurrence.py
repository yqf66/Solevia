"""add recurrence rules to manual tasks"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0023_manual_task_recurrence"
down_revision: str | None = "0022_plan_adjustment_previews"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "manual_tasks",
        sa.Column("recurrence", sa.String(16), nullable=False, server_default="ONCE"),
    )
    op.add_column(
        "manual_tasks",
        sa.Column("recurrence_weekdays_json", sa.JSON(), nullable=False, server_default="[]"),
    )
    op.add_column("manual_tasks", sa.Column("recurrence_end_date", sa.Date(), nullable=True))


def downgrade() -> None:
    op.drop_column("manual_tasks", "recurrence_end_date")
    op.drop_column("manual_tasks", "recurrence_weekdays_json")
    op.drop_column("manual_tasks", "recurrence")
