"""add route stage options

Revision ID: 0003_route_options
Revises: 0002_route_versions
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003_route_options"
down_revision: str | None = "0002_route_versions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "route_stages",
        sa.Column("options_json", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
    )


def downgrade() -> None:
    op.drop_column("route_stages", "options_json")
