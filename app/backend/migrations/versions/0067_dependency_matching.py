"""Add explicit dependency matching without changing historical gates."""

import sqlalchemy as sa
from alembic import op

revision = "0067_dependency_matching"
down_revision = "0066_goal_long_term_memory"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "work_item_dependencies",
        sa.Column("matching_mode", sa.String(24), nullable=False, server_default="ALL"),
    )


def downgrade():
    op.drop_column("work_item_dependencies", "matching_mode")
