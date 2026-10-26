"""Allow only one active research response per goal."""

from alembic import op
import sqlalchemy as sa

revision = "0062_research_response_active_goal"
down_revision = "0061_research_response_requests"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Early local 0061 installs may already contain this index.
    for index in sa.inspect(op.get_bind()).get_indexes("research_response_requests"):
        if index["name"] == "uq_research_response_active_goal":
            if not index["unique"] or index["column_names"] != ["goal_id"]:
                raise RuntimeError("Existing active research response index is incompatible")
            return
    condition = sa.text("status IN ('WAITING_RESEARCH', 'QUEUED_ANSWER', 'ANSWERING')")
    op.create_index(
        "uq_research_response_active_goal", "research_response_requests", ["goal_id"],
        unique=True, sqlite_where=condition, postgresql_where=condition,
    )


def downgrade() -> None:
    op.drop_index("uq_research_response_active_goal", table_name="research_response_requests")
