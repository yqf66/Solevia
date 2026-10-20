"""Add execution conversations, insights, and review records."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0037_execution_collaboration"
down_revision: str | None = "0036_schedule_types"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "execution_insights",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "goal_id", sa.String(36), sa.ForeignKey("goals.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column(
            "plan_version_id",
            sa.String(36),
            sa.ForeignKey("plan_versions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "source_message_id",
            sa.String(36),
            sa.ForeignKey("messages.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("insight_type", sa.String(32), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("scope", sa.String(32), nullable=False, server_default="GOAL"),
        sa.Column("confidence", sa.Float(), nullable=False, server_default="0.5"),
        sa.Column("status", sa.String(16), nullable=False, server_default="PROPOSED"),
        sa.Column(
            "supersedes_id",
            sa.String(36),
            sa.ForeignKey("execution_insights.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_execution_insights_goal_id", "execution_insights", ["goal_id"])
    op.create_index(
        "ix_execution_insights_plan_version_id", "execution_insights", ["plan_version_id"]
    )
    op.create_index(
        "ix_execution_insights_source_message_id", "execution_insights", ["source_message_id"]
    )
    op.create_index("ix_execution_insights_insight_type", "execution_insights", ["insight_type"])
    op.create_index("ix_execution_insights_status", "execution_insights", ["status"])
    op.create_index(
        "idx_execution_insights_goal_status", "execution_insights", ["goal_id", "status"]
    )

    op.create_table(
        "execution_reviews",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "goal_id", sa.String(36), sa.ForeignKey("goals.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column(
            "plan_version_id",
            sa.String(36),
            sa.ForeignKey("plan_versions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "source_message_id",
            sa.String(36),
            sa.ForeignKey("messages.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("trigger_type", sa.String(32), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="SUGGESTED"),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("evidence_json", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("decision", sa.String(32), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_execution_reviews_goal_id", "execution_reviews", ["goal_id"])
    op.create_index(
        "ix_execution_reviews_plan_version_id", "execution_reviews", ["plan_version_id"]
    )
    op.create_index(
        "ix_execution_reviews_source_message_id", "execution_reviews", ["source_message_id"]
    )
    op.create_index("ix_execution_reviews_trigger_type", "execution_reviews", ["trigger_type"])
    op.create_index("ix_execution_reviews_status", "execution_reviews", ["status"])
    op.create_index(
        "idx_execution_reviews_goal_status", "execution_reviews", ["goal_id", "status"]
    )


def downgrade() -> None:
    op.drop_index("idx_execution_reviews_goal_status", table_name="execution_reviews")
    op.drop_index("ix_execution_reviews_status", table_name="execution_reviews")
    op.drop_index("ix_execution_reviews_trigger_type", table_name="execution_reviews")
    op.drop_index("ix_execution_reviews_source_message_id", table_name="execution_reviews")
    op.drop_index("ix_execution_reviews_plan_version_id", table_name="execution_reviews")
    op.drop_index("ix_execution_reviews_goal_id", table_name="execution_reviews")
    op.drop_table("execution_reviews")
    op.drop_index("idx_execution_insights_goal_status", table_name="execution_insights")
    op.drop_index("ix_execution_insights_status", table_name="execution_insights")
    op.drop_index("ix_execution_insights_insight_type", table_name="execution_insights")
    op.drop_index("ix_execution_insights_source_message_id", table_name="execution_insights")
    op.drop_index("ix_execution_insights_plan_version_id", table_name="execution_insights")
    op.drop_index("ix_execution_insights_goal_id", table_name="execution_insights")
    op.drop_table("execution_insights")
