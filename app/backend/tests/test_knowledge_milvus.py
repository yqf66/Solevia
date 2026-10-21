from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import delete, select

from planner_api.config import Settings
from planner_api.knowledge.milvus import MilvusStore, MilvusUnavailable, collection_name, normalized, sync_target, try_store
from planner_api.knowledge.milvus_sync import MilvusSync
from planner_api.knowledge.search import KnowledgeSearch
from planner_api.models import Goal, KnowledgeChunk, KnowledgeDocument, utc_now


def test_milvus_collection_is_stable_per_model_dimension():
    settings = Settings(_env_file=None, knowledge_vector_backend="MILVUS")
    assert collection_name(settings, 384) == collection_name(settings, 384)
    assert collection_name(settings, 384) != collection_name(settings, 768)


def test_milvus_backend_is_opt_in_and_lazy():
    settings = Settings(_env_file=None)
    assert try_store(settings) is None
    settings = Settings(_env_file=None, knowledge_vector_backend="MILVUS")
    adapter = try_store(settings)
    assert adapter is not None
    # Importing the adapter must not require pymilvus until a connection is used.
    assert adapter._client is None


@pytest.fixture
def remote_library(session_factory):
    with session_factory() as session:
        goals = [Goal(title="one", raw_input="mutex"), Goal(title="two", raw_input="other")]
        session.add_all(goals)
        session.commit()
        settings = Settings(_env_file=None, knowledge_enabled=True, knowledge_vectors_enabled=True,
            knowledge_vector_backend="MILVUS", research_rerank_mode="OFF")
        store = KnowledgeSearch(session, settings)
        def put(goal, text="concurrency writer", *, shared=False, vector=None):
            doc = store.put(goal.id, kind="WEB", uri="https://example.test/" + goal.id,
                title="Writer", body=text, shared=shared, metadata={"coverage": "FULL_DOCUMENT"},
                pieces=[{"text": text, "start": 0, "end": len(text), "vector": vector or [1., 0., 0.],
                    "model": settings.material_embedding_identity}])
            session.commit()
            return doc
        yield store, goals, session_factory, put


class Remote:
    def __init__(self):
        self.rows = {}
        self.calls = []
        self.fail = False

    def upsert(self, rows):
        if self.fail:
            raise TimeoutError("temporary")
        self.rows.update({r[0]: r for r in rows})

    def search(self, query, hashes):
        self.calls.append(hashes)
        if self.fail:
            raise TimeoutError("temporary")
        return [(key, .99) for key in hashes if key in self.rows][:24]

    def inventory(self, dimension):
        if self.fail:
            raise TimeoutError("temporary")
        return [key for key, row in self.rows.items() if len(row[3]) == dimension]

    def delete(self, ids, dimension):
        for key in ids:
            self.rows.pop(key, None)

    def failed(self):
        pass


class QueryEncoder:
    def encode(self, values, *, query=False):
        assert query, "Migration must not re-encode stored passages"
        return [[1., 0., 0.] for _ in values]


def test_database_model_endpoint_isolation():
    s = Settings(_env_file=None)
    assert collection_name(s, 384) != collection_name(s.model_copy(update={"database_url": "sqlite:///other.db"}), 384)
    assert collection_name(s, 384) != collection_name(s.model_copy(update={"material_embedding_revision": "new"}), 384)
    other = s.model_copy(update={"knowledge_milvus_uri": "http://127.0.0.1:19531"})
    assert sync_target(s, 384) != sync_target(other, 384)
    assert normalized([3., 4.]) == [.6, .8]
    with pytest.raises(ValueError):
        normalized([float("nan")])


def test_reuses_vectors_and_authorizes_before_remote_search(remote_library, monkeypatch):
    store, goals, factory, put = remote_library
    local = put(goals[0])
    private = put(goals[1])
    remote = Remote()
    monkeypatch.setattr("planner_api.knowledge.milvus_sync.try_store", lambda s: remote)
    monkeypatch.setattr("planner_api.knowledge.search.try_store", lambda s: remote)
    sync = MilvusSync(factory, store.settings)
    assert sync.run_once()
    store.session.expire_all()
    assert not sync.run_once()
    result = store.search(goals[0].id, "mutex", query_encoder=QueryEncoder())
    assert [h["document_id"] for h in result["hits"]] == [local.id]
    assert result["diagnostics"]["vector"] == "MILVUS_FLAT_IP"
    assert all(private.id != row[1] for call in remote.calls for key, row in remote.rows.items() if key in call)
    assert all(c.milvus_status == "READY" for c in store.session.scalars(select(KnowledgeChunk)))


def test_outage_falls_back_without_losing_semantic_evidence(remote_library, monkeypatch):
    store, goals, _, put = remote_library
    doc = put(goals[0])
    remote = Remote()
    remote.fail = True
    monkeypatch.setattr("planner_api.knowledge.search.try_store", lambda s: remote)
    result = store.search(goals[0].id, "mutex", query_encoder=QueryEncoder())
    assert result["hits"][0]["document_id"] == doc.id
    assert result["diagnostics"]["vector"].startswith("MILVUS_FALLBACK_")


def test_bounded_sync_attempts_preserve_vector_and_source(remote_library, monkeypatch):
    store, goals, factory, put = remote_library
    doc = put(goals[0])
    remote = Remote()
    remote.fail = True
    monkeypatch.setattr("planner_api.knowledge.milvus_sync.try_store", lambda s: remote)
    for _ in range(3):
        assert MilvusSync(factory, store.settings).run_once()
    assert not MilvusSync(factory, store.settings).run_once()
    store.session.expire_all()
    c = store.session.scalar(select(KnowledgeChunk))
    assert (c.index_status, c.milvus_status, c.milvus_attempts) == ("READY", "FAILED", 3)
    assert c.vector_json == [1., 0., 0.]
    assert store.session.get(KnowledgeDocument, doc.id).body == "concurrency writer"


def test_expired_sync_lease_recovers_and_deleted_source_is_not_resurrected(remote_library, monkeypatch):
    store, goals, factory, put = remote_library
    doc = put(goals[0])
    doc_id = doc.id
    c = store.session.scalar(select(KnowledgeChunk))
    c.milvus_status, c.milvus_attempts = "SYNCING", 1
    c.milvus_lease_until = utc_now() - timedelta(seconds=1)
    store.session.commit()
    remote = Remote()
    original = remote.upsert
    def deleting(rows):
        with factory() as session:
            session.execute(delete(KnowledgeDocument).where(KnowledgeDocument.id == doc_id))
            session.commit()
        original(rows)
    remote.upsert = deleting
    monkeypatch.setattr("planner_api.knowledge.milvus_sync.try_store", lambda s: remote)
    sync = MilvusSync(factory, store.settings)
    assert sync.run_once()
    assert not sync.run_once()  # reconciliation deletes the orphan remotely
    assert not remote.rows
    store.session.expire_all()
    assert store.session.get(KnowledgeDocument, doc_id) is None


def test_reconcile_rebuilds_missing_ready_rows(remote_library, monkeypatch):
    store, goals, factory, put = remote_library
    put(goals[0])
    remote = Remote()
    monkeypatch.setattr("planner_api.knowledge.milvus_sync.try_store", lambda s: remote)
    sync = MilvusSync(factory, store.settings)
    assert sync.run_once()
    remote.rows.clear()  # lost index, original text/vector remains
    assert not sync.run_once()
    store.session.expire_all()
    assert store.session.scalar(select(KnowledgeChunk.milvus_status)) == "PENDING"
    assert sync.run_once()
    assert remote.rows


def test_adapter_rejects_stale_remote_hash_and_normalizes_query():
    settings = Settings(_env_file=None, knowledge_vector_backend="MILVUS")
    class FakeClient:
        def has_collection(self, **kw):
            return True
        def search(self, **kw):
            assert kw["data"] == [[.6, .8]]
            assert '"only-authorized"' in kw["filter"]
            return [[SimpleNamespace(id="only-authorized", distance=.9, entity={"content_hash": "old"})]]
    adapter = MilvusStore(settings, client=FakeClient())
    adapter._collections.add(collection_name(settings, 2))
    with pytest.raises(MilvusUnavailable, match="Stale"):
        adapter.search([3., 4.], {"only-authorized": "new"})


def test_sync_vectors_not_recomputed_by_worker(remote_library, monkeypatch):
    from planner_api.knowledge.worker import KnowledgeWorker
    store, goals, factory, put = remote_library
    put(goals[0])
    remote = Remote()
    monkeypatch.setattr("planner_api.knowledge.milvus_sync.try_store", lambda s: remote)
    worker = KnowledgeWorker(factory, store.settings, load_encoder=QueryEncoder)
    assert worker.run_once()
    assert remote.rows


def test_lease_after_last_crashed_attempt_finishes_failed(remote_library):
    store, goals, factory, put = remote_library
    put(goals[0])
    c = store.session.scalar(select(KnowledgeChunk))
    c.milvus_status, c.milvus_attempts = "SYNCING", 3
    c.milvus_lease_until = utc_now() - timedelta(seconds=1)
    store.session.commit()
    sync = MilvusSync(factory, store.settings)
    sync.next_reconcile = float("inf")
    assert not sync.run_once()
    store.session.expire_all()
    assert store.session.scalar(select(KnowledgeChunk.milvus_status)) == "FAILED"


def test_missing_collection_clears_cached_schema_for_recreation():
    settings = Settings(_env_file=None, knowledge_vector_backend="MILVUS")
    adapter = MilvusStore(settings, client=SimpleNamespace(has_collection=lambda **kw: False))
    name = collection_name(settings, 384)
    adapter._collections.add(name)
    assert adapter.inventory(384) is None
    assert name not in adapter._collections


def test_migration_upgrade_and_downgrade_preserve_existing_vectors():
    import importlib.util
    from pathlib import Path
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import create_engine, inspect
    path = Path(__file__).resolve().parents[1] / "migrations/versions/0065_knowledge_milvus_sync.py"
    spec = importlib.util.spec_from_file_location("milvus_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    engine = create_engine("sqlite://")
    with engine.begin() as c:
        c.exec_driver_sql("CREATE TABLE knowledge_chunks (id TEXT PRIMARY KEY, vector_json TEXT)")
        c.exec_driver_sql("INSERT INTO knowledge_chunks VALUES ('one', '[1,2,3]')")
        with Operations.context(MigrationContext.configure(c)):
            module.upgrade()
        assert c.exec_driver_sql("SELECT milvus_status, milvus_attempts FROM knowledge_chunks").one() == ("PENDING", 0)
        with Operations.context(MigrationContext.configure(c)):
            module.downgrade()
        assert c.exec_driver_sql("SELECT vector_json FROM knowledge_chunks").scalar() == "[1,2,3]"
        assert {col["name"] for col in inspect(c).get_columns("knowledge_chunks")} == {"id", "vector_json"}
