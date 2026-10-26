"""allow recurring manual tasks to be edited for one date"""
from collections.abc import Sequence
import sqlalchemy as sa
from alembic import op

revision: str = "0025_manual_task_date_overrides"
down_revision: str | None = "0024_task_ai_schedule_preference"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

def upgrade() -> None:
    op.create_table("manual_task_date_overrides",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("manual_task_id", sa.String(36), sa.ForeignKey("manual_tasks.id", ondelete="CASCADE"), nullable=False),
        sa.Column("override_date", sa.Date(), nullable=False),
        sa.Column("title", sa.String(180), nullable=True),
        sa.Column("start_local", sa.String(5), nullable=True),
        sa.Column("end_local", sa.String(5), nullable=True),
        sa.Column("is_deleted", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("manual_task_id", "override_date", name="uq_manual_task_override_date"),
    )
    op.create_index("ix_manual_task_date_overrides_manual_task_id", "manual_task_date_overrides", ["manual_task_id"])
    op.create_index("ix_manual_task_date_overrides_override_date", "manual_task_date_overrides", ["override_date"])

def downgrade() -> None:
    op.drop_table("manual_task_date_overrides")
