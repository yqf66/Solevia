"""Pair-specific schedule consent."""
from alembic import op
import sqlalchemy as sa

revision = "0021_parallel_pair_consent"
down_revision = "0020_parallel_schedule_confirmation"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "schedule_parallel_consents",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("first_task_id", sa.String(36), nullable=False),
        sa.Column("second_task_id", sa.String(36), nullable=False),
        sa.Column("scheduled_date", sa.Date(), nullable=False),
        sa.Column("first_start", sa.String(5), nullable=False),
        sa.Column("first_end", sa.String(5), nullable=False),
        sa.Column("second_start", sa.String(5), nullable=False),
        sa.Column("second_end", sa.String(5), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    for field in ("first_task_id", "second_task_id", "scheduled_date"):
        op.create_index(f"ix_schedule_parallel_consents_{field}",
                        "schedule_parallel_consents", [field])


def downgrade():
    op.drop_table("schedule_parallel_consents")
