"""Repair legacy schedule types when an explicit deadline exists."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0038_repair_legacy_schedule_types"
down_revision: str | None = "0037_execution_collaboration"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _legacy_deadline_goal_ids(connection: sa.Connection) -> list[str]:
    rows = connection.execute(
        sa.text(
            """
            SELECT DISTINCT goals.id
            FROM goals
            JOIN requirement_facts ON requirement_facts.goal_id = goals.id
            WHERE goals.schedule_type = 'RHYTHM'
              AND goals.schedule_type_confirmed_at = goals.created_at
              AND requirement_facts.field_key = 'deadline'
              AND requirement_facts.status = 'CONFIRMED'
              AND CAST(requirement_facts.value_json AS TEXT) NOT IN (
                  'null', '""', '[]', '"NONE"', '"无"', '"暂无"', '"没有"'
              )
            """
        )
    )
    return [str(row[0]) for row in rows]


def upgrade() -> None:
    connection = op.get_bind()
    goal_ids = _legacy_deadline_goal_ids(connection)
    if not goal_ids:
        return
    parameters = {f"goal_{index}": goal_id for index, goal_id in enumerate(goal_ids)}
    placeholders = ", ".join(f":goal_{index}" for index in range(len(goal_ids)))
    for table_name in ("goals", "route_versions", "plan_versions", "plan_generation_jobs"):
        connection.execute(
            sa.text(
                f"UPDATE {table_name} SET schedule_type = 'CALENDAR' "
                f"WHERE goal_id IN ({placeholders})"
                if table_name != "goals"
                else f"UPDATE goals SET schedule_type = 'CALENDAR' "
                f"WHERE id IN ({placeholders})"
            ),
            parameters,
        )


def downgrade() -> None:
    # The previous value cannot be distinguished from a genuine user choice
    # after repair, so data classification is intentionally not reversed.
    pass
