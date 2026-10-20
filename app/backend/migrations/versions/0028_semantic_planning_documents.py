"""store semantic route and plan source documents

Revision ID: 0028_semantic_planning_documents
Revises: 0027_holiday_calendar
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0028_semantic_planning_documents"
down_revision: str | None = "0027_holiday_calendar"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    for table in ("route_versions", "plan_versions"):
        op.add_column(table, sa.Column("source_document", sa.Text(), nullable=True))
        op.add_column(
            table,
            sa.Column(
                "compiled_snapshot_json",
                sa.JSON(),
                nullable=False,
                server_default=sa.text("'{}'"),
            )
        )
        op.add_column(
            table,
            sa.Column(
                "compiler_report_json",
                sa.JSON(),
                nullable=False,
                server_default=sa.text("'{}'"),
            )
        )
    op.add_column(
        "plan_versions",
        sa.Column(
            "temporal_segments_json",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'[]'"),
        )
    )


def downgrade() -> None:
    op.drop_column("plan_versions", "temporal_segments_json")
    for table in ("plan_versions", "route_versions"):
        op.drop_column(table, "compiler_report_json")
        op.drop_column(table, "compiled_snapshot_json")
        op.drop_column(table, "source_document")
