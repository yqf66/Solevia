"""store completion per recurring manual-task occurrence"""
from collections.abc import Sequence
import sqlalchemy as sa
from alembic import op

revision: str = "0026_manual_task_override_status"
down_revision: str | None = "0025_manual_task_date_overrides"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

def upgrade() -> None:
    op.add_column("manual_task_date_overrides", sa.Column("status", sa.String(24), nullable=True))
    op.add_column("manual_task_date_overrides", sa.Column("time_overridden", sa.Boolean(), nullable=False, server_default=sa.false()))

def downgrade() -> None:
    op.drop_column("manual_task_date_overrides", "time_overridden")
    op.drop_column("manual_task_date_overrides", "status")
