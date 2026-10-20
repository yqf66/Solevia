"""persist immutable task stage attribution for stable progress"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0013_progress_lineage"
down_revision: str | None = "0012_recovery_shift"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("plan_tasks") as batch_op:
        batch_op.add_column(sa.Column("origin_stage_order", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("lineage_task_id", sa.String(36), nullable=True))
        batch_op.create_index("ix_plan_tasks_lineage_task_id", ["lineage_task_id"])
    # Backfill legacy rows; subsequent revisions preserve this value even if
    # their calendar dates are shifted.
    op.execute(
        sa.text(
            "UPDATE plan_tasks SET origin_stage_order = "
            "(SELECT stage_order FROM plan_days WHERE plan_days.id = plan_tasks.plan_day_id) "
            "WHERE origin_stage_order IS NULL"
        )
    )
    op.execute(
        sa.text(
            "WITH RECURSIVE lineage(id, root_id) AS ("
            " SELECT id, id FROM plan_tasks WHERE source_task_id IS NULL"
            " UNION ALL"
            " SELECT child.id, lineage.root_id FROM plan_tasks child"
            " JOIN lineage ON child.source_task_id = lineage.id"
            ")"
            " UPDATE plan_tasks SET lineage_task_id = ("
            " SELECT root_id FROM lineage WHERE lineage.id = plan_tasks.id"
            ") WHERE lineage_task_id IS NULL"
        )
    )


def downgrade() -> None:
    with op.batch_alter_table("plan_tasks") as batch_op:
        batch_op.drop_index("ix_plan_tasks_lineage_task_id")
        batch_op.drop_column("origin_stage_order")
        batch_op.drop_column("lineage_task_id")
