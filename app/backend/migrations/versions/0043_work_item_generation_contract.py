"""Add stable work-item identities and generation scopes."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0043_work_item_generation_contract"
down_revision: str | None = "0042_flexible_work_item_foundation"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_GROUP_SCOPE_CHECK = (
    "(scope_type = 'PLAN' AND milestone_id IS NULL AND stage_order IS NULL) OR "
    "(scope_type IN ('STAGE', 'WEEK') AND milestone_id IS NOT NULL "
    "AND stage_order IS NOT NULL) OR "
    "(scope_type = 'CHECKPOINT' AND ((milestone_id IS NULL AND stage_order IS NULL) "
    "OR (milestone_id IS NOT NULL AND stage_order IS NOT NULL)))"
)


def _create_work_item_tables(*, expanded: bool) -> None:
    group_columns: list[sa.SchemaItem] = [
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("plan_version_id", sa.String(36), nullable=False),
        sa.Column("milestone_id", sa.String(36), nullable=expanded is True),
    ]
    if expanded:
        group_columns.extend(
            [
                sa.Column("scope_type", sa.String(24), nullable=False, server_default="STAGE"),
                sa.Column("stage_order", sa.Integer(), nullable=True),
            ]
        )
    group_columns.extend(
        [
            sa.Column("group_key", sa.String(80), nullable=False),
            sa.Column("title", sa.String(180), nullable=False),
            sa.Column(
                "selection_mode", sa.String(24), nullable=False, server_default="SELECT_K"
            ),
            sa.Column("minimum_selected", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("maximum_selected", sa.Integer(), nullable=True),
            sa.Column("constraints_json", sa.JSON(), nullable=False, server_default="{}"),
            sa.Column("status", sa.String(24), nullable=False, server_default="DRAFT"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(
                ["plan_version_id"], ["plan_versions.id"], ondelete="CASCADE"
            ),
            sa.ForeignKeyConstraint(
                ["milestone_id"], ["plan_milestones.id"], ondelete="CASCADE"
            ),
            sa.UniqueConstraint(
                "plan_version_id", "group_key", name="uq_plan_work_item_group_key"
            ),
        ]
    )
    if expanded:
        group_columns.append(
            sa.CheckConstraint(_GROUP_SCOPE_CHECK, name="ck_plan_work_item_group_scope")
        )
    op.create_table("plan_work_item_groups", *group_columns)

    item_columns: list[sa.SchemaItem] = [
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("plan_version_id", sa.String(36), nullable=False),
        sa.Column("milestone_id", sa.String(36), nullable=expanded is True),
        sa.Column("group_id", sa.String(36), nullable=True),
        sa.Column("source_work_item_id", sa.String(36), nullable=True),
        sa.Column("lineage_work_item_id", sa.String(36), nullable=True),
    ]
    if expanded:
        item_columns.extend(
            [
                sa.Column("item_key", sa.String(120), nullable=False),
                sa.Column("scope_type", sa.String(24), nullable=False, server_default="STAGE"),
                sa.Column("stage_order", sa.Integer(), nullable=True),
            ]
        )
    else:
        item_columns.append(sa.Column("stage_order", sa.Integer(), nullable=False))
    item_columns.extend(
        [
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
        ]
    )
    if expanded:
        item_columns.append(
            sa.Column("commitment_refs_json", sa.JSON(), nullable=False, server_default="[]")
        )
    item_columns.extend(
        [
            sa.Column("status", sa.String(24), nullable=False, server_default="DRAFT"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(
                ["plan_version_id"], ["plan_versions.id"], ondelete="CASCADE"
            ),
            sa.ForeignKeyConstraint(
                ["milestone_id"], ["plan_milestones.id"], ondelete="CASCADE"
            ),
            sa.ForeignKeyConstraint(
                ["group_id"], ["plan_work_item_groups.id"], ondelete="SET NULL"
            ),
            sa.ForeignKeyConstraint(
                ["source_work_item_id"], ["plan_work_items.id"], ondelete="SET NULL"
            ),
        ]
    )
    if expanded:
        item_columns.extend(
            [
                sa.UniqueConstraint(
                    "plan_version_id", "item_key", name="uq_plan_work_item_key"
                ),
                sa.CheckConstraint(_GROUP_SCOPE_CHECK, name="ck_plan_work_item_scope"),
            ]
        )
    op.create_table("plan_work_items", *item_columns)

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
    _create_indexes(expanded=expanded)


def _create_indexes(*, expanded: bool) -> None:
    for column in ("plan_version_id", "milestone_id", "status"):
        op.create_index(
            f"ix_plan_work_item_groups_{column}", "plan_work_item_groups", [column]
        )
    if expanded:
        op.create_index(
            "ix_plan_work_item_groups_scope_type", "plan_work_item_groups", ["scope_type"]
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
    if expanded:
        op.create_index("ix_plan_work_items_scope_type", "plan_work_items", ["scope_type"])
    op.create_index(
        "idx_plan_work_items_plan_stage",
        "plan_work_items",
        ["plan_version_id", "stage_order"],
    )
    for column in (
        "plan_version_id",
        "predecessor_work_item_id",
        "successor_work_item_id",
    ):
        op.create_index(
            f"ix_work_item_dependencies_{column}", "work_item_dependencies", [column]
        )
    for column in ("activation_id", "work_item_id", "eligible_from", "eligible_to", "status"):
        op.create_index(f"ix_work_item_windows_{column}", "work_item_windows", [column])
    op.create_index(
        "idx_work_item_windows_activation_dates",
        "work_item_windows",
        ["activation_id", "eligible_from", "eligible_to"],
    )


def _backup_sqlite_tables() -> None:
    for name in (
        "_0043_groups",
        "_0043_items",
        "_0043_dependencies",
        "_0043_windows",
        "_0043_task_links",
    ):
        op.execute(f"DROP TABLE IF EXISTS temp.{name}")
    op.execute("CREATE TEMP TABLE _0043_groups AS SELECT * FROM plan_work_item_groups")
    op.execute("CREATE TEMP TABLE _0043_items AS SELECT * FROM plan_work_items")
    op.execute("CREATE TEMP TABLE _0043_dependencies AS SELECT * FROM work_item_dependencies")
    op.execute("CREATE TEMP TABLE _0043_windows AS SELECT * FROM work_item_windows")
    op.execute(
        "CREATE TEMP TABLE _0043_task_links AS "
        "SELECT id, source_work_item_id, work_item_window_id FROM plan_tasks"
    )


def _drop_work_item_tables() -> None:
    op.drop_table("work_item_dependencies")
    op.drop_table("work_item_windows")
    op.drop_table("plan_work_items")
    op.drop_table("plan_work_item_groups")


def _restore_shared_tables() -> None:
    op.execute(
        "INSERT INTO work_item_dependencies "
        "SELECT * FROM _0043_dependencies"
    )
    op.execute("INSERT INTO work_item_windows SELECT * FROM _0043_windows")
    op.execute(
        "UPDATE plan_tasks SET "
        "source_work_item_id = (SELECT source_work_item_id FROM _0043_task_links "
        "WHERE _0043_task_links.id = plan_tasks.id), "
        "work_item_window_id = (SELECT work_item_window_id FROM _0043_task_links "
        "WHERE _0043_task_links.id = plan_tasks.id) "
        "WHERE id IN (SELECT id FROM _0043_task_links)"
    )
    for name in (
        "_0043_task_links",
        "_0043_windows",
        "_0043_dependencies",
        "_0043_items",
        "_0043_groups",
    ):
        op.execute(f"DROP TABLE temp.{name}")


def _sqlite_upgrade() -> None:
    _backup_sqlite_tables()
    _drop_work_item_tables()
    _create_work_item_tables(expanded=True)
    op.execute(
        "INSERT INTO plan_work_item_groups "
        "(id, plan_version_id, milestone_id, scope_type, stage_order, group_key, title, "
        "selection_mode, minimum_selected, maximum_selected, constraints_json, status, "
        "created_at, updated_at) "
        "SELECT g.id, g.plan_version_id, g.milestone_id, 'STAGE', m.stage_order, g.group_key, "
        "g.title, g.selection_mode, g.minimum_selected, g.maximum_selected, g.constraints_json, "
        "g.status, g.created_at, g.updated_at FROM _0043_groups g "
        "JOIN plan_milestones m ON m.id = g.milestone_id"
    )
    op.execute(
        "INSERT INTO plan_work_items "
        "(id, plan_version_id, milestone_id, group_id, source_work_item_id, "
        "lineage_work_item_id, item_key, scope_type, stage_order, title, description, task_kind, "
        "estimate_minutes, priority, is_required, is_splittable, binding_mode, fulfillment_mode, "
        "target_value, target_unit, constraints_json, completion_rule_json, evidence_json, "
        "source_refs_json, commitment_refs_json, status, created_at, updated_at) "
        "SELECT id, plan_version_id, milestone_id, group_id, source_work_item_id, "
        "lineage_work_item_id, 'LEGACY.' || UPPER(id), 'STAGE', stage_order, title, description, "
        "task_kind, estimate_minutes, priority, is_required, is_splittable, binding_mode, "
        "fulfillment_mode, target_value, target_unit, constraints_json, completion_rule_json, "
        "evidence_json, source_refs_json, '[]', status, created_at, updated_at FROM _0043_items"
    )
    _restore_shared_tables()


def _sqlite_downgrade() -> None:
    connection = op.get_bind()
    unsupported = connection.execute(
        sa.text(
            "SELECT (SELECT COUNT(*) FROM plan_work_item_groups "
            "WHERE milestone_id IS NULL OR stage_order IS NULL) + "
            "(SELECT COUNT(*) FROM plan_work_items "
            "WHERE milestone_id IS NULL OR stage_order IS NULL)"
        )
    ).scalar_one()
    if unsupported:
        raise RuntimeError(
            "Cannot downgrade 0043 while plan-scoped work items or groups exist"
        )
    _backup_sqlite_tables()
    _drop_work_item_tables()
    _create_work_item_tables(expanded=False)
    op.execute(
        "INSERT INTO plan_work_item_groups "
        "(id, plan_version_id, milestone_id, group_key, title, selection_mode, minimum_selected, "
        "maximum_selected, constraints_json, status, created_at, updated_at) "
        "SELECT id, plan_version_id, milestone_id, group_key, title, selection_mode, "
        "minimum_selected, maximum_selected, constraints_json, status, created_at, updated_at "
        "FROM _0043_groups"
    )
    op.execute(
        "INSERT INTO plan_work_items "
        "(id, plan_version_id, milestone_id, group_id, source_work_item_id, "
        "lineage_work_item_id, stage_order, title, description, task_kind, estimate_minutes, "
        "priority, is_required, is_splittable, binding_mode, fulfillment_mode, target_value, "
        "target_unit, constraints_json, completion_rule_json, evidence_json, source_refs_json, "
        "status, created_at, updated_at) "
        "SELECT id, plan_version_id, milestone_id, group_id, source_work_item_id, "
        "lineage_work_item_id, stage_order, title, description, task_kind, estimate_minutes, "
        "priority, is_required, is_splittable, binding_mode, fulfillment_mode, target_value, "
        "target_unit, constraints_json, completion_rule_json, evidence_json, source_refs_json, "
        "status, created_at, updated_at FROM _0043_items"
    )
    _restore_shared_tables()


def upgrade() -> None:
    if op.get_bind().dialect.name == "sqlite":
        _sqlite_upgrade()
        return

    op.add_column(
        "plan_work_item_groups",
        sa.Column("scope_type", sa.String(24), nullable=False, server_default="STAGE"),
    )
    op.add_column("plan_work_item_groups", sa.Column("stage_order", sa.Integer(), nullable=True))
    op.execute(
        "UPDATE plan_work_item_groups g SET stage_order = m.stage_order "
        "FROM plan_milestones m WHERE m.id = g.milestone_id"
    )
    op.alter_column("plan_work_item_groups", "milestone_id", nullable=True)
    op.create_index(
        "ix_plan_work_item_groups_scope_type", "plan_work_item_groups", ["scope_type"]
    )
    op.create_check_constraint(
        "ck_plan_work_item_group_scope", "plan_work_item_groups", _GROUP_SCOPE_CHECK
    )

    op.add_column("plan_work_items", sa.Column("item_key", sa.String(120), nullable=True))
    op.execute("UPDATE plan_work_items SET item_key = 'LEGACY.' || UPPER(id)")
    op.alter_column("plan_work_items", "item_key", nullable=False)
    op.add_column(
        "plan_work_items",
        sa.Column("scope_type", sa.String(24), nullable=False, server_default="STAGE"),
    )
    op.add_column(
        "plan_work_items",
        sa.Column("commitment_refs_json", sa.JSON(), nullable=False, server_default="[]"),
    )
    op.alter_column("plan_work_items", "milestone_id", nullable=True)
    op.alter_column("plan_work_items", "stage_order", nullable=True)
    op.create_unique_constraint(
        "uq_plan_work_item_key", "plan_work_items", ["plan_version_id", "item_key"]
    )
    op.create_index("ix_plan_work_items_scope_type", "plan_work_items", ["scope_type"])
    op.create_check_constraint("ck_plan_work_item_scope", "plan_work_items", _GROUP_SCOPE_CHECK)


def downgrade() -> None:
    if op.get_bind().dialect.name == "sqlite":
        _sqlite_downgrade()
        return

    connection = op.get_bind()
    unsupported = connection.execute(
        sa.text(
            "SELECT (SELECT COUNT(*) FROM plan_work_item_groups "
            "WHERE milestone_id IS NULL OR stage_order IS NULL) + "
            "(SELECT COUNT(*) FROM plan_work_items "
            "WHERE milestone_id IS NULL OR stage_order IS NULL)"
        )
    ).scalar_one()
    if unsupported:
        raise RuntimeError(
            "Cannot downgrade 0043 while plan-scoped work items or groups exist"
        )
    op.drop_constraint("ck_plan_work_item_scope", "plan_work_items", type_="check")
    op.drop_index("ix_plan_work_items_scope_type", table_name="plan_work_items")
    op.drop_constraint("uq_plan_work_item_key", "plan_work_items", type_="unique")
    op.alter_column("plan_work_items", "stage_order", nullable=False)
    op.alter_column("plan_work_items", "milestone_id", nullable=False)
    op.drop_column("plan_work_items", "commitment_refs_json")
    op.drop_column("plan_work_items", "scope_type")
    op.drop_column("plan_work_items", "item_key")

    op.drop_constraint(
        "ck_plan_work_item_group_scope", "plan_work_item_groups", type_="check"
    )
    op.drop_index("ix_plan_work_item_groups_scope_type", table_name="plan_work_item_groups")
    op.alter_column("plan_work_item_groups", "milestone_id", nullable=False)
    op.drop_column("plan_work_item_groups", "stage_order")
    op.drop_column("plan_work_item_groups", "scope_type")
