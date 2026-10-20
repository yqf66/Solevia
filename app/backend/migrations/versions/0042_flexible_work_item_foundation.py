"""Add the opt-in flexible work-item execution foundation."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0042_flexible_work_item_foundation"
down_revision: str | None = "0041_generation_orchestration"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "plan_versions",
        sa.Column(
            "execution_model_version",
            sa.String(32),
            nullable=False,
            server_default="LEGACY_DAY_BASED",
        )
    )
    op.create_index(
        "ix_plan_versions_execution_model_version",
        "plan_versions",
        ["execution_model_version"],
    )

    op.create_table(
        "plan_work_item_groups",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("plan_version_id", sa.String(36), nullable=False),
        sa.Column("milestone_id", sa.String(36), nullable=False),
        sa.Column("group_key", sa.String(80), nullable=False),
        sa.Column("title", sa.String(180), nullable=False),
        sa.Column("selection_mode", sa.String(24), nullable=False, server_default="SELECT_K"),
        sa.Column("minimum_selected", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("maximum_selected", sa.Integer(), nullable=True),
        sa.Column("constraints_json", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("status", sa.String(24), nullable=False, server_default="DRAFT"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["plan_version_id"], ["plan_versions.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["milestone_id"], ["plan_milestones.id"], ondelete="CASCADE"),
        sa.UniqueConstraint(
            "plan_version_id", "group_key", name="uq_plan_work_item_group_key"
        ),
    )
    op.create_index(
        "ix_plan_work_item_groups_plan_version_id",
        "plan_work_item_groups",
        ["plan_version_id"],
    )
    op.create_index(
        "ix_plan_work_item_groups_milestone_id",
        "plan_work_item_groups",
        ["milestone_id"],
    )
    op.create_index(
        "ix_plan_work_item_groups_status", "plan_work_item_groups", ["status"]
    )

    op.create_table(
        "plan_work_items",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("plan_version_id", sa.String(36), nullable=False),
        sa.Column("milestone_id", sa.String(36), nullable=False),
        sa.Column("group_id", sa.String(36), nullable=True),
        sa.Column("source_work_item_id", sa.String(36), nullable=True),
        sa.Column("lineage_work_item_id", sa.String(36), nullable=True),
        sa.Column("stage_order", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(180), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("task_kind", sa.String(24), nullable=False),
        sa.Column("estimate_minutes", sa.Integer(), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column("is_required", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("is_splittable", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("binding_mode", sa.String(24), nullable=False, server_default="WINDOW"),
        sa.Column("fulfillment_mode", sa.String(24), nullable=False, server_default="ONCE"),
        sa.Column("target_value", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("target_unit", sa.String(24), nullable=False, server_default="COUNT"),
        sa.Column("constraints_json", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("completion_rule_json", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("evidence_json", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("source_refs_json", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("status", sa.String(24), nullable=False, server_default="DRAFT"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["plan_version_id"], ["plan_versions.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["milestone_id"], ["plan_milestones.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["group_id"], ["plan_work_item_groups.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(
            ["source_work_item_id"], ["plan_work_items.id"], ondelete="SET NULL"
        ),
    )
    for column in (
        "plan_version_id",
        "milestone_id",
        "group_id",
        "source_work_item_id",
        "lineage_work_item_id",
        "binding_mode",
        "fulfillment_mode",
        "status",
    ):
        op.create_index(f"ix_plan_work_items_{column}", "plan_work_items", [column])
    op.create_index(
        "idx_plan_work_items_plan_stage",
        "plan_work_items",
        ["plan_version_id", "stage_order"],
    )

    op.create_table(
        "work_item_dependencies",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("plan_version_id", sa.String(36), nullable=False),
        sa.Column("predecessor_work_item_id", sa.String(36), nullable=False),
        sa.Column("successor_work_item_id", sa.String(36), nullable=False),
        sa.Column(
            "dependency_type", sa.String(24), nullable=False, server_default="FINISH_TO_START"
        ),
        sa.Column("minimum_lag_days", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("maximum_lag_days", sa.Integer(), nullable=True),
        sa.Column("condition_json", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["plan_version_id"], ["plan_versions.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["predecessor_work_item_id"], ["plan_work_items.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["successor_work_item_id"], ["plan_work_items.id"], ondelete="CASCADE"
        ),
        sa.UniqueConstraint(
            "predecessor_work_item_id",
            "successor_work_item_id",
            "dependency_type",
            name="uq_work_item_dependency_edge",
        ),
    )
    for column in (
        "plan_version_id",
        "predecessor_work_item_id",
        "successor_work_item_id",
    ):
        op.create_index(
            f"ix_work_item_dependencies_{column}", "work_item_dependencies", [column]
        )

    op.create_table(
        "work_item_windows",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("activation_id", sa.String(36), nullable=False),
        sa.Column("work_item_id", sa.String(36), nullable=False),
        sa.Column("period_key", sa.String(80), nullable=False),
        sa.Column("eligible_from", sa.Date(), nullable=False),
        sa.Column("eligible_to", sa.Date(), nullable=False),
        sa.Column("required_value", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("status", sa.String(24), nullable=False, server_default="PENDING"),
        sa.Column("resolution_json", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("materialization_revision", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("fulfilled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["activation_id"], ["plan_activations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["work_item_id"], ["plan_work_items.id"], ondelete="CASCADE"),
        sa.UniqueConstraint(
            "activation_id", "work_item_id", "period_key", name="uq_work_item_window_period"
        ),
    )
    for column in ("activation_id", "work_item_id", "eligible_from", "eligible_to", "status"):
        op.create_index(f"ix_work_item_windows_{column}", "work_item_windows", [column])
    op.create_index(
        "idx_work_item_windows_activation_dates",
        "work_item_windows",
        ["activation_id", "eligible_from", "eligible_to"],
    )

    if op.get_bind().dialect.name == "sqlite":
        # Alembic's SQLite batch mode rebuilds plan_tasks. With foreign keys
        # enabled, dropping that source table cascades into progress history.
        # SQLite supports adding nullable REFERENCES columns in place, so use
        # direct DDL and preserve every existing child row.
        op.execute(
            "ALTER TABLE plan_tasks ADD COLUMN source_work_item_id VARCHAR(36) "
            "REFERENCES plan_work_items(id) ON DELETE SET NULL"
        )
        op.execute(
            "ALTER TABLE plan_tasks ADD COLUMN work_item_window_id VARCHAR(36) "
            "REFERENCES work_item_windows(id) ON DELETE SET NULL"
        )
    else:
        op.add_column(
            "plan_tasks",
            sa.Column(
                "source_work_item_id",
                sa.String(36),
                sa.ForeignKey("plan_work_items.id", ondelete="SET NULL"),
                nullable=True,
            ),
        )
        op.add_column(
            "plan_tasks",
            sa.Column(
                "work_item_window_id",
                sa.String(36),
                sa.ForeignKey("work_item_windows.id", ondelete="SET NULL"),
                nullable=True,
            ),
        )
    op.add_column("plan_tasks", sa.Column("occurrence_index", sa.Integer(), nullable=True))
    op.add_column(
        "plan_tasks", sa.Column("materialization_key", sa.String(160), nullable=True)
    )
    op.add_column(
        "plan_tasks", sa.Column("materialized_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.create_index(
        "ix_plan_tasks_source_work_item_id", "plan_tasks", ["source_work_item_id"]
    )
    op.create_index(
        "ix_plan_tasks_work_item_window_id", "plan_tasks", ["work_item_window_id"]
    )
    op.create_index(
        "ix_plan_tasks_materialization_key",
        "plan_tasks",
        ["materialization_key"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("ix_plan_tasks_materialization_key", table_name="plan_tasks")
    op.drop_index("ix_plan_tasks_work_item_window_id", table_name="plan_tasks")
    op.drop_index("ix_plan_tasks_source_work_item_id", table_name="plan_tasks")
    op.drop_column("plan_tasks", "materialized_at")
    op.drop_column("plan_tasks", "materialization_key")
    op.drop_column("plan_tasks", "occurrence_index")
    op.drop_column("plan_tasks", "work_item_window_id")
    op.drop_column("plan_tasks", "source_work_item_id")

    op.drop_table("work_item_windows")
    op.drop_table("work_item_dependencies")
    op.drop_table("plan_work_items")
    op.drop_table("plan_work_item_groups")

    op.drop_index("ix_plan_versions_execution_model_version", table_name="plan_versions")
    op.drop_column("plan_versions", "execution_model_version")
