import importlib.util
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine


def test_0060_preserves_chunks_and_fts_and_cascades_embeddings(tmp_path):
    engine = create_engine(f"sqlite:///{(tmp_path / 'embeddings.db').as_posix()}")
    path = Path(__file__).parents[1] / "migrations/versions/0060_material_embeddings.py"
    spec = importlib.util.spec_from_file_location("migration_0060", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    with engine.begin() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys=ON")
        connection.exec_driver_sql(
            "CREATE TABLE material_chunks (id VARCHAR(36) PRIMARY KEY, text TEXT)"
        )
        connection.exec_driver_sql(
            "CREATE VIRTUAL TABLE material_chunks_fts USING fts5(chunk_id UNINDEXED, search_text)"
        )
        connection.exec_driver_sql(
            "CREATE TRIGGER material_chunks_fts_delete AFTER DELETE ON material_chunks BEGIN DELETE FROM material_chunks_fts WHERE chunk_id=old.id; END"
        )
        connection.exec_driver_sql(
            "INSERT INTO material_chunks VALUES ('historic', 'preserved evidence')"
        )
        connection.exec_driver_sql(
            "INSERT INTO material_chunks_fts VALUES ('historic', 'preserved evidence')"
        )
        with Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()
        assert (
            connection.exec_driver_sql("SELECT text FROM material_chunks").scalar_one()
            == "preserved evidence"
        )
        assert (
            connection.exec_driver_sql(
                "SELECT chunk_id FROM material_chunks_fts WHERE material_chunks_fts MATCH 'evidence'"
            ).scalar_one()
            == "historic"
        )
        connection.exec_driver_sql(
            "INSERT INTO material_embeddings (id, chunk_id, model, content_hash, vector_json) VALUES ('embedding', 'historic', 'model@revision', 'hash', '[1, 0]')"
        )
        connection.exec_driver_sql("DELETE FROM material_chunks WHERE id='historic'")
        assert (
            connection.exec_driver_sql("SELECT COUNT(*) FROM material_embeddings").scalar_one() == 0
        )
        assert (
            connection.exec_driver_sql("SELECT COUNT(*) FROM material_chunks_fts").scalar_one() == 0
        )
        with Operations.context(MigrationContext.configure(connection)):
            migration.downgrade()
        connection.exec_driver_sql("INSERT INTO material_chunks VALUES ('after', 'still writable')")


def test_0052_adds_locators_and_metrics_without_removing_fts_delete_trigger(tmp_path):
    database = tmp_path / "material-migration.db"
    engine = create_engine(f"sqlite:///{database.as_posix()}")
    migration_path = (
        Path(__file__).parents[1]
        / "migrations"
        / "versions"
        / "0052_material_locators_and_worker_metrics.py"
    )
    spec = importlib.util.spec_from_file_location("migration_0052", migration_path)
    assert spec is not None and spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)

    with engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TABLE material_chunks ("
            "id TEXT PRIMARY KEY, revision_id TEXT NOT NULL, sequence INTEGER NOT NULL, "
            "section_title TEXT NOT NULL, text TEXT NOT NULL, char_start INTEGER NOT NULL, "
            "char_end INTEGER NOT NULL, content_hash TEXT NOT NULL, created_at TEXT NOT NULL)"
        )
        connection.exec_driver_sql(
            "CREATE TABLE material_processing_jobs ("
            "id TEXT PRIMARY KEY, revision_id TEXT NOT NULL, status TEXT NOT NULL, "
            "attempts INTEGER NOT NULL, lease_expires_at TEXT, error_code TEXT, "
            "error_message TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, "
            "completed_at TEXT)"
        )
        connection.exec_driver_sql(
            "CREATE VIRTUAL TABLE material_chunks_fts USING fts5("
            "chunk_id UNINDEXED, revision_id UNINDEXED, search_text)"
        )
        connection.exec_driver_sql(
            "CREATE TRIGGER material_chunks_fts_delete AFTER DELETE ON material_chunks "
            "BEGIN DELETE FROM material_chunks_fts WHERE chunk_id = old.id; END"
        )
        connection.exec_driver_sql(
            "INSERT INTO material_chunks VALUES "
            "('chunk-1','revision-1',0,'','historic text',0,12,'hash','2026-01-01')"
        )
        connection.exec_driver_sql(
            "INSERT INTO material_chunks_fts VALUES ('chunk-1','revision-1','historic text')"
        )
        connection.exec_driver_sql(
            "INSERT INTO material_processing_jobs "
            "(id,revision_id,status,attempts,created_at,updated_at) "
            "VALUES ('job-1','revision-1','READY',1,'2026-01-01','2026-01-01')"
        )

        context = MigrationContext.configure(connection)
        with Operations.context(context):
            migration.upgrade()

        assert (
            connection.exec_driver_sql(
                "SELECT locator_json FROM material_chunks WHERE id='chunk-1'"
            ).scalar_one()
            == "[]"
        )
        queued_at = connection.exec_driver_sql(
            "SELECT queued_at FROM material_processing_jobs WHERE id='job-1'"
        ).scalar_one()
        assert queued_at.startswith("2026-01-01")

        connection.exec_driver_sql("DELETE FROM material_chunks WHERE id='chunk-1'")
        assert (
            connection.exec_driver_sql("SELECT COUNT(*) FROM material_chunks_fts").scalar_one() == 0
        )


def test_0059_rebuilds_tail_terms_and_preserves_delete_trigger(tmp_path):
    engine = create_engine(f"sqlite:///{(tmp_path / 'full-fts.db').as_posix()}")
    migration_path = Path(__file__).parents[1] / "migrations/versions/0059_material_full_fts.py"
    spec = importlib.util.spec_from_file_location("migration_0059", migration_path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TABLE material_chunks (id TEXT, revision_id TEXT, section_title TEXT, text TEXT)"
        )
        connection.exec_driver_sql(
            "CREATE VIRTUAL TABLE material_chunks_fts USING fts5(chunk_id UNINDEXED, revision_id UNINDEXED, search_text)"
        )
        connection.exec_driver_sql(
            "CREATE TRIGGER material_chunks_fts_delete AFTER DELETE ON material_chunks BEGIN DELETE FROM material_chunks_fts WHERE chunk_id=old.id; END"
        )
        content = " ".join(f"term{index}" for index in range(90)) + " tail_unique 人力预算"
        connection.exec_driver_sql(
            "INSERT INTO material_chunks VALUES (?, ?, ?, ?)",
            ("chunk", "revision", "Title", content),
        )
        connection.exec_driver_sql(
            "INSERT INTO material_chunks_fts VALUES ('chunk', 'revision', 'term0')"
        )
        with Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()
        assert (
            connection.exec_driver_sql(
                "SELECT chunk_id FROM material_chunks_fts WHERE material_chunks_fts MATCH 'tail_unique'"
            ).scalar_one()
            == "chunk"
        )
        assert (
            connection.exec_driver_sql("SELECT text FROM material_chunks").scalar_one() == content
        )
        connection.exec_driver_sql("DELETE FROM material_chunks WHERE id='chunk'")
        assert (
            connection.exec_driver_sql("SELECT COUNT(*) FROM material_chunks_fts").scalar_one() == 0
        )
