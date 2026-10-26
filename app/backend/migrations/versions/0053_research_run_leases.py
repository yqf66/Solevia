"""Add research run leases, cancellation and idempotency metadata."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0053_research_run_leases"
down_revision: str | None = "0052_material_locators_worker_metrics"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "research_runs",
        sa.Column("schema_version", sa.Integer(), nullable=False, server_default="1"),
    )
    op.add_column(
        "research_runs",
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "research_runs",
        sa.Column("recovery_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column("research_runs", sa.Column("idempotency_key", sa.String(180), nullable=True))
    op.add_column("research_runs", sa.Column("lease_id", sa.String(36), nullable=True))
    op.add_column(
        "research_runs", sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "research_runs", sa.Column("last_heartbeat_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "research_runs",
        sa.Column("cancel_requested_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column("research_runs", sa.Column("cancel_reason", sa.String(500), nullable=True))
    op.create_index(
        "uq_research_runs_idempotency_key",
        "research_runs",
        ["idempotency_key"],
        unique=True,
    )
    op.create_index("ix_research_runs_lease_id", "research_runs", ["lease_id"])
    op.create_index("ix_research_runs_lease_expires_at", "research_runs", ["lease_expires_at"])


def downgrade() -> None:
    op.drop_index("ix_research_runs_lease_expires_at", table_name="research_runs")
    op.drop_index("ix_research_runs_lease_id", table_name="research_runs")
    op.drop_index("uq_research_runs_idempotency_key", table_name="research_runs")
    op.drop_column("research_runs", "cancel_reason")
    op.drop_column("research_runs", "cancel_requested_at")
    op.drop_column("research_runs", "last_heartbeat_at")
    op.drop_column("research_runs", "lease_expires_at")
    op.drop_column("research_runs", "lease_id")
    op.drop_column("research_runs", "idempotency_key")
    op.drop_column("research_runs", "recovery_count")
    op.drop_column("research_runs", "attempt_count")
    op.drop_column("research_runs", "schema_version")
