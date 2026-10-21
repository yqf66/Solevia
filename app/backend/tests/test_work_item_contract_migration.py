import importlib.util
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine


def _load_migration(filename: str, module_name: str):
    migration_path = Path(__file__).parents[1] / "migrations" / "versions" / filename
    spec = importlib.util.spec_from_file_location(module_name, migration_path)
    assert spec is not None and spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    return migration


def test_0043_rebuild_preserves_work_item_graph_and_task_links(tmp_path):
    database = tmp_path / "contract-migration.db"
    engine = create_engine(f"sqlite:///{database.as_posix()}")
    foundation = _load_migration(
        "0042_flexible_work_item_foundation.py", "migration_0042_for_0043"
    )
    contract = _load_migration(
        "0043_work_item_generation_contract.py", "migration_0043"
    )

    with engine.begin() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys=ON")
        connection.exec_driver_sql(
            "CREATE TABLE plan_versions (id VARCHAR(36) PRIMARY KEY)"
        )
        connection.exec_driver_sql(
            "CREATE TABLE plan_milestones ("
            "id VARCHAR(36) PRIMARY KEY, stage_order INTEGER NOT NULL)"
        )
        connection.exec_driver_sql(
            "CREATE TABLE plan_activations (id VARCHAR(36) PRIMARY KEY)"
        )
        connection.exec_driver_sql(
            "CREATE TABLE plan_tasks (id VARCHAR(36) PRIMARY KEY)"
        )
        connection.exec_driver_sql("INSERT INTO plan_versions (id) VALUES ('plan-1')")
        connection.exec_driver_sql(
            "INSERT INTO plan_milestones (id, stage_order) VALUES ('milestone-1', 2)"
        )
        connection.exec_driver_sql("INSERT INTO plan_activations (id) VALUES ('activation-1')")
        connection.exec_driver_sql("INSERT INTO plan_tasks (id) VALUES ('task-1')")
        context = MigrationContext.configure(connection)
        with Operations.context(context):
            foundation.upgrade()

        stamp = "2026-09-17 00:00:00"
        connection.exec_driver_sql(
            "INSERT INTO plan_work_item_groups "
            "(id, plan_version_id, milestone_id, group_key, title, selection_mode, "
            "minimum_selected, constraints_json, status, created_at, updated_at) VALUES "
            "('group-1', 'plan-1', 'milestone-1', 'pool', '候选池', 'SELECT_K', "
            f"1, '{{}}', 'DRAFT', '{stamp}', '{stamp}')"
        )
        for item_id, group_id, title in (
            ("item-1", "NULL", "前置事项"),
            ("item-2", "'group-1'", "候选事项"),
        ):
            connection.exec_driver_sql(
                "INSERT INTO plan_work_items "
                "(id, plan_version_id, milestone_id, group_id, stage_order, title, description, "
                "task_kind, estimate_minutes, priority, is_required, is_splittable, binding_mode, "
                "fulfillment_mode, target_value, target_unit, constraints_json, "
                "completion_rule_json, evidence_json, source_refs_json, status, created_at, "
                "updated_at) VALUES "
                f"('{item_id}', 'plan-1', 'milestone-1', {group_id}, 2, '{title}', '说明', "
                f"'PRACTICE', 30, 3, 1, 1, 'WINDOW', 'ONCE', 1, 'COUNT', '{{}}', '{{}}', "
                f"'[]', '[\"fact-1\"]', 'DRAFT', '{stamp}', '{stamp}')"
            )
        connection.exec_driver_sql(
            "INSERT INTO work_item_dependencies "
            "(id, plan_version_id, predecessor_work_item_id, successor_work_item_id, "
            "dependency_type, minimum_lag_days, condition_json, created_at) VALUES "
            f"('dep-1', 'plan-1', 'item-1', 'item-2', 'FINISH_TO_START', 0, '{{}}', '{stamp}')"
        )
        connection.exec_driver_sql(
            "INSERT INTO work_item_windows "
            "(id, activation_id, work_item_id, period_key, eligible_from, eligible_to, "
            "required_value, status, resolution_json, materialization_revision, created_at, "
            "updated_at) VALUES ('window-1', 'activation-1', 'item-2', '2026-W38', "
            f"'2026-09-14', '2026-09-20', 1, 'OPEN', '{{}}', 0, '{stamp}', '{stamp}')"
        )
        connection.exec_driver_sql(
            "UPDATE plan_tasks SET source_work_item_id='item-2', "
            "work_item_window_id='window-1' WHERE id='task-1'"
        )

        with Operations.context(context):
            contract.upgrade()

        item = connection.exec_driver_sql(
            "SELECT item_key, scope_type, stage_order, commitment_refs_json "
            "FROM plan_work_items WHERE id='item-2'"
        ).one()
        assert item[0] == "LEGACY.ITEM-2"
        assert item[1:] == ("STAGE", 2, "[]")
        group = connection.exec_driver_sql(
            "SELECT scope_type, stage_order FROM plan_work_item_groups WHERE id='group-1'"
        ).one()
        assert group == ("STAGE", 2)
        assert connection.exec_driver_sql(
            "SELECT COUNT(*) FROM work_item_dependencies"
        ).scalar_one() == 1
        assert connection.exec_driver_sql(
            "SELECT COUNT(*) FROM work_item_windows"
        ).scalar_one() == 1
        assert connection.exec_driver_sql(
            "SELECT source_work_item_id, work_item_window_id FROM plan_tasks WHERE id='task-1'"
        ).one() == ("item-2", "window-1")
        assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []

        connection.exec_driver_sql(
            "INSERT INTO plan_work_items "
            "(id, plan_version_id, milestone_id, group_id, item_key, scope_type, stage_order, "
            "title, description, task_kind, estimate_minutes, priority, is_required, "
            "is_splittable, binding_mode, fulfillment_mode, target_value, target_unit, "
            "constraints_json, completion_rule_json, evidence_json, source_refs_json, "
            "commitment_refs_json, status, created_at, updated_at) VALUES "
            "('plan-item', 'plan-1', NULL, NULL, 'PLAN.STANDING.REVIEW', 'PLAN', NULL, "
            f"'长期复盘', '说明', 'REVIEW', 20, 3, 1, 1, 'CADENCE', 'COUNT', 1, 'COUNT', "
            f"'{{}}', '{{}}', '[]', '[\"fact-2\"]', '[\"每周复盘\"]', 'DRAFT', "
            f"'{stamp}', '{stamp}')"
        )
        connection.exec_driver_sql("DELETE FROM plan_work_items WHERE id='plan-item'")

        with Operations.context(context):
            contract.downgrade()

        assert connection.exec_driver_sql(
            "SELECT COUNT(*) FROM plan_work_items"
        ).scalar_one() == 2
        assert connection.exec_driver_sql(
            "SELECT source_work_item_id, work_item_window_id FROM plan_tasks WHERE id='task-1'"
        ).one() == ("item-2", "window-1")
        assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []
