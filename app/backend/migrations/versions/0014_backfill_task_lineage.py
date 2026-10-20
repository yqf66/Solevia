"""backfill task lineage roots for existing revisions"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0014_backfill_task_lineage"
down_revision: str | None = "0013_progress_lineage"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
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
            ")"
        )
    )


def downgrade() -> None:
    pass
