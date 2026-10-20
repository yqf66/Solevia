"""add resumable plan generation jobs

Revision ID: 0008_plan_generation_jobs
Revises: 0007_daily_feedback
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008_plan_generation_jobs"
down_revision: str | None = "0007_daily_feedback"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "plan_generation_jobs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "goal_id",
            sa.String(36),
            sa.ForeignKey("goals.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("mode", sa.String(16), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("phase", sa.String(24), nullable=False),
        sa.Column("generated_weeks", sa.Integer(), nullable=False),
        sa.Column("total_weeks", sa.Integer(), nullable=False),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("diagnostic", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_plan_generation_jobs_goal_id", "plan_generation_jobs", ["goal_id"])
    op.create_index("ix_plan_generation_jobs_status", "plan_generation_jobs", ["status"])


def downgrade() -> None:
    op.drop_table("plan_generation_jobs")
