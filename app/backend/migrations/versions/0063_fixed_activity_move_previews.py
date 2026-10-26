"""Durable, explicitly confirmed fixed activity moves."""
from alembic import op
import sqlalchemy as sa

revision = "0063_fixed_activity_move_previews"
down_revision = "0062_research_response_active_goal"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("fixed_activity_move_previews",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("goal_id", sa.String(36), sa.ForeignKey("goals.id", ondelete="CASCADE"), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("preview_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("applied_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index("ix_fixed_activity_move_previews_goal_id", "fixed_activity_move_previews", ["goal_id"])


def downgrade():
    op.drop_table("fixed_activity_move_previews")
