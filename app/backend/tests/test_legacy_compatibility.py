import importlib.util
import json
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, text

from planner_api.plan_service import PlanService


def test_legacy_dependency_labels_are_canonicalized_without_guessing() -> None:
    payload = {
        "days": [
            {
                "stage_order": 1,
                "week_index": 1,
                "day_index": 1,
                "tasks": [
                    {"order": 1, "title": "准备资料", "evidence": [], "depends_on": []},
                    {"order": 2, "title": "完成初稿", "evidence": [], "depends_on": ["准备资料"]},
                    {
                        "order": 3,
                        "title": "复核",
                        "evidence": [],
                        "depends_on": ["任务2", "不存在"],
                    },
                ],
            }
        ]
    }

    PlanService._canonicalize_persisted_dependencies(payload)

    tasks = payload["days"][0]["tasks"]
    assert tasks[1]["depends_on"] == ["M1-W1-D1-T1"]
    assert tasks[2]["depends_on"] == ["M1-W1-D1-T2"]


def test_0056_normalizes_native_and_invalid_json_values(tmp_path) -> None:
    database = tmp_path / "legacy-json.db"
    engine = create_engine(f"sqlite:///{database.as_posix()}")
    migration_path = (
        Path(__file__).parents[1]
        / "migrations"
        / "versions"
        / "0056_normalize_legacy_json_values.py"
    )
    spec = importlib.util.spec_from_file_location("migration_0056", migration_path)
    assert spec is not None and spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)

    with engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TABLE requirement_facts (id TEXT PRIMARY KEY, value_json JSON NOT NULL)"
        )
        connection.exec_driver_sql(
            "INSERT INTO requirement_facts (id, value_json) "
            "VALUES ('number', 6), ('plain', 'raw text')"
        )
        context = MigrationContext.configure(connection)
        with Operations.context(context):
            migration.upgrade()

        rows = connection.execute(
            text("SELECT id, value_json, typeof(value_json) FROM requirement_facts ORDER BY id")
        ).fetchall()
        values = {row[0]: (json.loads(row[1]), row[2]) for row in rows}
        assert values["number"] == ("6", "text")
        assert values["plain"] == ("raw text", "text")
