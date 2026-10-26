import importlib.util
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect


def test_0055_creates_reminder_history_table(tmp_path) -> None:
    database = tmp_path / "reminder-history.db"
    engine = create_engine(f"sqlite:///{database.as_posix()}")
    migration_path = (
        Path(__file__).parents[1]
        / "migrations"
        / "versions"
        / "0055_reminder_history.py"
    )
    spec = importlib.util.spec_from_file_location("migration_0055", migration_path)
    assert spec is not None and spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)

    with engine.begin() as connection:
        connection.exec_driver_sql("CREATE TABLE goals (id TEXT PRIMARY KEY)")
        connection.exec_driver_sql("CREATE TABLE plan_versions (id TEXT PRIMARY KEY)")
        context = MigrationContext.configure(connection)
        with Operations.context(context):
            migration.upgrade()

        columns = {column["name"] for column in inspect(connection).get_columns("reminder_events")}
        assert {"goal_id", "dedupe_key", "status", "read_at", "dismissed_at"} <= columns
        indexes = {item["name"] for item in inspect(connection).get_indexes("reminder_events")}
        assert "idx_reminder_events_goal_status" in indexes
