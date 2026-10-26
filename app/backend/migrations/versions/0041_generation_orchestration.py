"""Add adaptive generation orchestration and flexible task policies."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0041_generation_orchestration"
down_revision: str | None = "0040_quick_plan_handoff"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("plan_tasks") as batch:
        batch.add_column(
            sa.Column("scheduling_policy", sa.String(24), nullable=False, server_default="EXACT_DAY")
        )
        batch.add_column(
            sa.Column("scheduling_policy_json", sa.JSON(), nullable=False, server_default="{}")
        )

    op.create_table(
        "generation_runs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("goal_id", sa.String(36), nullable=False),
        sa.Column("plan_version_id", sa.String(36), nullable=True),
        sa.Column("context_pack_id", sa.String(36), nullable=True),
        sa.Column("task_type", sa.String(40), nullable=False),
        sa.Column("tier", sa.String(8), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("context_hash", sa.String(64), nullable=False),
        sa.Column("decision_json", sa.JSON(), nullable=False),
        sa.Column("validation_json", sa.JSON(), nullable=False),
        sa.Column("input_units", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("output_units", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("estimated_cost_units", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("latency_ms", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["goal_id"], ["goals.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["plan_version_id"], ["plan_versions.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["context_pack_id"], ["context_packs.id"], ondelete="SET NULL"),
    )
    op.create_index("ix_generation_runs_goal_id", "generation_runs", ["goal_id"])
    op.create_index("ix_generation_runs_plan_version_id", "generation_runs", ["plan_version_id"])
    op.create_index("ix_generation_runs_context_pack_id", "generation_runs", ["context_pack_id"])
    op.create_index("ix_generation_runs_task_type", "generation_runs", ["task_type"])
    op.create_index("ix_generation_runs_status", "generation_runs", ["status"])
    op.create_index("ix_generation_runs_context_hash", "generation_runs", ["context_hash"])
    op.create_index(
        "idx_generation_runs_goal_task_created",
        "generation_runs",
        ["goal_id", "task_type", "created_at"],
    )

    op.create_table(
        "agent_reports",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("generation_run_id", sa.String(36), nullable=False),
        sa.Column("role", sa.String(40), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("input_hash", sa.String(64), nullable=False),
        sa.Column("report_json", sa.JSON(), nullable=False),
        sa.Column("evidence_json", sa.JSON(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False, server_default="0"),
        sa.Column("input_units", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("output_units", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("latency_ms", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["generation_run_id"], ["generation_runs.id"], ondelete="CASCADE"),
        sa.UniqueConstraint(
            "generation_run_id", "role", "input_hash", name="uq_agent_report_run_role_input"
        ),
    )
    op.create_index("ix_agent_reports_generation_run_id", "agent_reports", ["generation_run_id"])
    op.create_index("ix_agent_reports_role", "agent_reports", ["role"])
    op.create_index("ix_agent_reports_status", "agent_reports", ["status"])
    op.create_index("ix_agent_reports_input_hash", "agent_reports", ["input_hash"])


def downgrade() -> None:
    op.drop_table("agent_reports")
    op.drop_table("generation_runs")
    with op.batch_alter_table("plan_tasks") as batch:
        batch.drop_column("scheduling_policy_json")
        batch.drop_column("scheduling_policy")
