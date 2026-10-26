"""Store parsed source locators and processing job timings/cancellation."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0052_material_locators_worker_metrics"
down_revision: str | None = "0051_material_jobs_fts"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "material_chunks",
        sa.Column("locator_json", sa.JSON(), nullable=False, server_default="[]"),
    )
    op.add_column(
        "material_processing_jobs",
        sa.Column(
            "queued_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default="1970-01-01 00:00:00",
        ),
    )
    op.add_column(
        "material_processing_jobs",
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "material_processing_jobs",
        sa.Column("queue_wait_ms", sa.Integer(), nullable=True),
    )
    op.add_column(
        "material_processing_jobs",
        sa.Column("processing_duration_ms", sa.Integer(), nullable=True),
    )
    op.add_column(
        "material_processing_jobs",
        sa.Column("cancel_requested_at", sa.DateTime(timezone=True), nullable=True),
    )

    op.execute("UPDATE material_processing_jobs SET queued_at = created_at")


def downgrade() -> None:
    op.drop_column("material_processing_jobs", "cancel_requested_at")
    op.drop_column("material_processing_jobs", "processing_duration_ms")
    op.drop_column("material_processing_jobs", "queue_wait_ms")
    op.drop_column("material_processing_jobs", "started_at")
    op.drop_column("material_processing_jobs", "queued_at")
    op.drop_column("material_chunks", "locator_json")
