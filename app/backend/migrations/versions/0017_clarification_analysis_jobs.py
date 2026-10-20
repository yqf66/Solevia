"""persist clarification memory extraction retries"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0017_clarification_analysis_jobs"
down_revision: str | None = "0016_requirement_memory"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "clarification_analysis_jobs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("goal_id", sa.String(36), sa.ForeignKey("goals.id", ondelete="CASCADE"), nullable=False),
        sa.Column("conversation_id", sa.String(36), sa.ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_message_id", sa.String(36), sa.ForeignKey("messages.id", ondelete="CASCADE"), nullable=False),
        sa.Column("assistant_message_id", sa.String(36), sa.ForeignKey("messages.id", ondelete="CASCADE"), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="PENDING"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="4"),
        sa.Column("next_retry_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("last_error_code", sa.String(48), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_clarification_analysis_jobs_goal_id", "clarification_analysis_jobs", ["goal_id"])
    op.create_index("ix_clarification_analysis_jobs_conversation_id", "clarification_analysis_jobs", ["conversation_id"])
    op.create_index("ix_clarification_analysis_jobs_user_message_id", "clarification_analysis_jobs", ["user_message_id"])
    op.create_index("ix_clarification_analysis_jobs_assistant_message_id", "clarification_analysis_jobs", ["assistant_message_id"])
    op.create_index("ix_clarification_analysis_jobs_status", "clarification_analysis_jobs", ["status"])
    op.create_index("ix_clarification_analysis_jobs_next_retry_at", "clarification_analysis_jobs", ["next_retry_at"])


def downgrade() -> None:
    for name in (
        "ix_clarification_analysis_jobs_next_retry_at",
        "ix_clarification_analysis_jobs_status",
        "ix_clarification_analysis_jobs_assistant_message_id",
        "ix_clarification_analysis_jobs_user_message_id",
        "ix_clarification_analysis_jobs_conversation_id",
        "ix_clarification_analysis_jobs_goal_id",
    ):
        op.drop_index(name, table_name="clarification_analysis_jobs")
    op.drop_table("clarification_analysis_jobs")
