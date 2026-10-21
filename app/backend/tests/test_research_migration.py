import importlib.util
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect


def test_0053_adds_research_run_lifecycle_columns(tmp_path) -> None:
    database = tmp_path / "research-migration.db"
    engine = create_engine(f"sqlite:///{database.as_posix()}")
    migration_path = (
        Path(__file__).parents[1]
        / "migrations"
        / "versions"
        / "0053_research_run_leases.py"
    )
    spec = importlib.util.spec_from_file_location("migration_0053", migration_path)
    assert spec is not None and spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)

    with engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TABLE research_runs ("
            "id TEXT PRIMARY KEY, goal_id TEXT NOT NULL, question TEXT NOT NULL, "
            "enabled BOOLEAN NOT NULL, status TEXT NOT NULL, current_stage TEXT NOT NULL, "
            "decision_json JSON NOT NULL, budget_json JSON NOT NULL, "
            "checkpoint_json JSON NOT NULL, "
            "result_json JSON NOT NULL, created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL)"
        )
        context = MigrationContext.configure(connection)
        with Operations.context(context):
            migration.upgrade()

        columns = {column["name"] for column in inspect(connection).get_columns("research_runs")}
        assert {
            "schema_version",
            "attempt_count",
            "recovery_count",
            "idempotency_key",
            "lease_id",
            "lease_expires_at",
            "last_heartbeat_at",
            "cancel_requested_at",
            "cancel_reason",
        }.issubset(columns)
