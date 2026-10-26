"""Persist bounded research runs and their evidence."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0047_research_persistence"
down_revision: str | None = "0046_material_foundation"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "research_runs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("goal_id", sa.String(36), nullable=False),
        sa.Column("conversation_id", sa.String(36), nullable=True),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("status", sa.String(24), nullable=False, server_default="CREATED"),
        sa.Column("current_stage", sa.String(24), nullable=False, server_default="CREATED"),
        sa.Column("decision_json", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("budget_json", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("checkpoint_json", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("result_json", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("stop_reason", sa.String(48), nullable=True),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["goal_id"], ["goals.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["conversation_id"], ["conversations.id"], ondelete="SET NULL"
        ),
    )
    op.create_index("ix_research_runs_goal_id", "research_runs", ["goal_id"])
    op.create_index(
        "ix_research_runs_conversation_id", "research_runs", ["conversation_id"]
    )
    op.create_index("ix_research_runs_status", "research_runs", ["status"])

    op.create_table(
        "research_evidence",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("run_id", sa.String(36), nullable=False),
        sa.Column("canonical_url", sa.Text(), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("title", sa.String(300), nullable=False, server_default=""),
        sa.Column("source", sa.String(160), nullable=False, server_default=""),
        sa.Column("engines_json", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("snippet", sa.Text(), nullable=False, server_default=""),
        sa.Column("content_excerpt", sa.Text(), nullable=False, server_default=""),
        sa.Column("relevance_score", sa.Float(), nullable=False, server_default="0"),
        sa.Column("confidence", sa.Float(), nullable=False, server_default="0"),
        sa.Column(
            "read_status", sa.String(24), nullable=False, server_default="NOT_SELECTED"
        ),
        sa.Column("retrieval_method", sa.String(40), nullable=False, server_default=""),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["research_runs.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("run_id", "canonical_url", name="uq_research_evidence_url"),
    )
    op.create_index("ix_research_evidence_run_id", "research_evidence", ["run_id"])
    op.create_index(
        "ix_research_evidence_read_status", "research_evidence", ["read_status"]
    )


def downgrade() -> None:
    op.drop_table("research_evidence")
    op.drop_table("research_runs")
