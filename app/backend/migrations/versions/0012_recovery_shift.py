"""persist recovery previews and task lineage

Revision ID: 0012_recovery_shift
Revises: 0011_planning_compiler
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0012_recovery_shift"
down_revision: str | None = "0011_planning_compiler"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("plan_tasks") as batch_op:
        batch_op.add_column(sa.Column("source_task_id", sa.String(36), nullable=True))
        batch_op.create_foreign_key(
            "fk_plan_tasks_source_task_id",
            "plan_tasks",
            ["source_task_id"],
            ["id"],
            ondelete="SET NULL",
        )
        batch_op.create_index("ix_plan_tasks_source_task_id", ["source_task_id"])

    op.create_table(
        "recovery_operations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "goal_id",
            sa.String(36),
            sa.ForeignKey("goals.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "source_plan_version_id",
            sa.String(36),
            sa.ForeignKey("plan_versions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "result_plan_version_id",
            sa.String(36),
            sa.ForeignKey("plan_versions.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("mode", sa.String(32), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("idempotency_key", sa.String(180), nullable=False),
        sa.Column("preview_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("applied_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("idempotency_key", name="uq_recovery_operations_idempotency_key"),
    )
    for column in (
        "goal_id",
        "source_plan_version_id",
        "result_plan_version_id",
        "mode",
        "status",
    ):
        op.create_index(f"ix_recovery_operations_{column}", "recovery_operations", [column])
    op.create_index(
        "ix_recovery_operations_idempotency_key",
        "recovery_operations",
        ["idempotency_key"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_table("recovery_operations")
    with op.batch_alter_table("plan_tasks") as batch_op:
        batch_op.drop_index("ix_plan_tasks_source_task_id")
        batch_op.drop_constraint("fk_plan_tasks_source_task_id", type_="foreignkey")
        batch_op.drop_column("source_task_id")
