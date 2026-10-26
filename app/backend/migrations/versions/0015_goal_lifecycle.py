"""add goal lifecycle and timezone fields"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0015_goal_lifecycle"
down_revision: str | None = "0014_backfill_task_lineage"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("goals") as batch_op:
        batch_op.add_column(
            sa.Column(
                "timezone",
                sa.String(64),
                nullable=False,
                server_default="Asia/Shanghai",
            )
        )
        batch_op.add_column(sa.Column("pause_until", sa.Date(), nullable=True))
        batch_op.add_column(sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column("completion_summary", sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("goals") as batch_op:
        batch_op.drop_column("completion_summary")
        batch_op.drop_column("archived_at")
        batch_op.drop_column("completed_at")
        batch_op.drop_column("pause_until")
        batch_op.drop_column("timezone")
