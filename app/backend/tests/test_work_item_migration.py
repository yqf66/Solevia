import importlib.util
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine


def test_0042_upgrade_and_downgrade_preserve_task_children(tmp_path):
    database = tmp_path / "migration.db"
    engine = create_engine(f"sqlite:///{database.as_posix()}")
    migration_path = (
        Path(__file__).parents[1]
        / "migrations"
        / "versions"
        / "0042_flexible_work_item_foundation.py"
    )
    spec = importlib.util.spec_from_file_location("migration_0042", migration_path)
    assert spec is not None and spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)

    with engine.begin() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys=ON")
        connection.exec_driver_sql(
            "CREATE TABLE plan_versions (id VARCHAR(36) PRIMARY KEY)"
        )
        connection.exec_driver_sql(
            "CREATE TABLE plan_milestones (id VARCHAR(36) PRIMARY KEY)"
        )
        connection.exec_driver_sql(
            "CREATE TABLE plan_activations (id VARCHAR(36) PRIMARY KEY)"
        )
        connection.exec_driver_sql(
            "CREATE TABLE plan_tasks (id VARCHAR(36) PRIMARY KEY)"
        )
        connection.exec_driver_sql(
            "CREATE TABLE progress_events ("
            "id VARCHAR(36) PRIMARY KEY, "
            "task_id VARCHAR(36) REFERENCES plan_tasks(id) ON DELETE CASCADE"
            ")"
        )
        connection.exec_driver_sql("INSERT INTO plan_versions (id) VALUES ('plan-1')")
        connection.exec_driver_sql("INSERT INTO plan_tasks (id) VALUES ('task-1')")
        connection.exec_driver_sql(
            "INSERT INTO progress_events (id, task_id) VALUES ('event-1', 'task-1')"
        )

        context = MigrationContext.configure(connection)
        with Operations.context(context):
            migration.upgrade()

        assert connection.exec_driver_sql("SELECT COUNT(*) FROM plan_tasks").scalar_one() == 1
        assert (
            connection.exec_driver_sql("SELECT COUNT(*) FROM progress_events").scalar_one() == 1
        )
        assert connection.exec_driver_sql(
            "SELECT execution_model_version FROM plan_versions WHERE id='plan-1'"
        ).scalar_one() == "LEGACY_DAY_BASED"
        task_foreign_keys = {
            row[3]: (row[2], row[6])
            for row in connection.exec_driver_sql("PRAGMA foreign_key_list(plan_tasks)")
        }
        assert task_foreign_keys["source_work_item_id"] == (
            "plan_work_items",
            "SET NULL",
        )
        assert task_foreign_keys["work_item_window_id"] == (
            "work_item_windows",
            "SET NULL",
        )

        with Operations.context(context):
            migration.downgrade()

        assert connection.exec_driver_sql("SELECT COUNT(*) FROM plan_tasks").scalar_one() == 1
        assert (
            connection.exec_driver_sql("SELECT COUNT(*) FROM progress_events").scalar_one() == 1
        )
        assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []
