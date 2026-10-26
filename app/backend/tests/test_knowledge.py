from datetime import timedelta
import hashlib
import importlib.util
import json
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import delete, func, select

from planner_api.config import Settings
from planner_api.database import get_db_session
from planner_api.knowledge.search import KnowledgeSearch, cached_context, reuse_allowed, retrieval_view
from planner_api.knowledge.store import KnowledgeStore, capture, digest
from planner_api.knowledge.vectors import nearest
from planner_api.knowledge.worker import KnowledgeWorker
from planner_api.main import create_app
from planner_api.models import (
    Goal, GoalMaterialLink, KnowledgeChunk, KnowledgeDocument, MaterialChunk,
    MaterialRevision, MaterialSource, ResearchRun, utc_now,
)
from planner_api.research.consumer import ResearchContextService
from planner_api.research.schemas import EvidencePassage, ResearchPacket, ResearchSource


@pytest.fixture
def library(session_factory):
    with session_factory() as session:
        goals = [Goal(title="first", raw_input="SQLite concurrency"), Goal(title="second", raw_input="Python tasks")]
        session.add_all(goals)
        session.commit()
        settings = Settings(_env_file=None, knowledge_enabled=True, knowledge_vectors_enabled=False,
                            research_rerank_mode="OFF", material_semantic_min_similarity=0.7)
        yield KnowledgeSearch(session, settings), goals, session_factory


def web(store, goal, text, *, uri="https://example.org/sqlite", shared=True, fetched_at=None, vector=None):
    return store.put(goal.id, kind="WEB", uri=uri, title="Reference", body=text,
        pieces=[{"text": text, "start": 0, "end": len(text), "vector": vector or [],
                 "model": store.settings.material_embedding_identity if vector else ""}],
        metadata={"coverage": "FULL_DOCUMENT"}, shared=shared, fetched_at=fetched_at)


def material(store, goal, *, policy="AUTO", scope="ALL"):
    source = MaterialSource(kind="UPLOAD", canonical_uri="upload://private/report", title="Private report")
    store.session.add(source)
    store.session.flush()
    revision = MaterialRevision(source_id=source.id, revision=1, content_hash=digest("private sqlite"),
        file_size=20, status="READY", completed_at=utc_now())
    link = GoalMaterialLink(goal_id=goal.id, source_id=source.id, usage_policy=policy, scope=scope)
    store.session.add_all([revision, link])
    store.session.flush()
    store.session.add(MaterialChunk(revision_id=revision.id, sequence=0, text="private sqlite writer report",
        section_title="Concurrency", char_start=5, char_end=33, content_hash=digest("private sqlite writer report"),
        locator_json=[{"page": 3}]))
    store.session.flush()
    store.ingest_material(revision)
    store.session.commit()
    return source, revision, link


def test_persistence_dedup_and_scope_priority(library):
    store, goals, factory = library
    local = web(store, goals[0], "sqlite writer concurrency", uri="https://local.test/sqlite")
    global_doc = web(store, goals[1], "sqlite writer concurrency sqlite", uri="https://global.test/sqlite")
    store.session.commit()
    same = web(store, goals[0], "sqlite writer concurrency", uri="https://local.test/sqlite")
    assert same.id == local.id
    store.session.commit()
    with factory() as session:
        result = KnowledgeSearch(session, store.settings).search(goals[0].id, "sqlite writer")
        assert [h["document_id"] for h in result["hits"]] == [local.id, global_doc.id]
        assert result["new_searches"] == 0
        assert all(h["evidence_status"] == "CANDIDATE" for h in result["hits"])
        session.commit()
    store.session.expire_all()
    assert store.session.get(KnowledgeDocument, local.id).accessed_count == 1


def test_private_documents_manual_policy_scope_and_live_revocation(library):
    store, goals, _ = library
    source, revision, link = material(store, goals[0], policy="MANUAL_ONLY", scope="PLAN")
    assert not store.search(goals[0].id, "private sqlite", phase="PLAN")["hits"]
    assert not store.search(goals[0].id, "private sqlite", manual=True)["hits"]
    hit = store.search(goals[0].id, "private sqlite", manual=True, phase="PLAN")["hits"][0]
    assert hit["locator"]["locations"] == [{"page": 3}]
    assert not store.search(goals[1].id, "private sqlite", manual=True, phase="PLAN")["hits"]
    doc = store.session.get(KnowledgeDocument, hit["document_id"])
    doc.shared = True
    store.session.commit()
    assert store.search(goals[1].id, "private sqlite", manual=True, phase="PLAN")["hits"]
    source.status = "DISABLED"
    store.session.commit()
    assert not store.search(goals[1].id, "private sqlite", manual=True, phase="PLAN")["hits"]
    source.status = "ACTIVE"
    store.session.commit()
    link.status = "REMOVED"
    store.session.commit()
    assert not store.search(goals[1].id, "private sqlite", manual=True, phase="PLAN")["hits"]


def test_new_material_revision_invalidates_cached_previous_revision(library):
    store, goals, _ = library
    source, revision, _ = material(store, goals[0])
    assert store.search(goals[0].id, "sqlite writer")["hits"]
    store.session.add(MaterialRevision(source_id=source.id, revision=2, content_hash="new", file_size=20, status="QUEUED"))
    store.session.commit()
    assert not store.search(goals[0].id, "sqlite writer")["hits"]
    store.session.delete(revision)
    store.session.commit()
    assert store.session.scalar(select(func.count()).select_from(KnowledgeDocument)) == 0


def test_source_changes_during_ranking_discard_hits(library, monkeypatch):
    store, goals, _ = library
    source, _, _ = material(store, goals[0])
    def revoke(query, candidates, settings):
        source.status = "DISABLED"
        store.session.commit()
        return candidates, {"status": "OFF"}
    monkeypatch.setattr("planner_api.knowledge.search.rerank_candidates", revoke)
    assert not store.search(goals[0].id, "private sqlite")["hits"]


def test_shared_visibility_cannot_be_inferred_from_a_copied_goal_link(library):
    store, goals, _ = library
    _, _, _ = material(store, goals[0])
    doc = store.session.scalar(select(KnowledgeDocument))
    store._link(doc, goals[1].id)
    store.session.commit()
    assert not store.search(goals[1].id, "sqlite writer", manual=True)["hits"]


def test_web_supersession_uses_fetch_time_and_supports_content_reverting(library):
    store, goals, _ = library
    now = utc_now()
    newest = web(store, goals[0], "new sqlite policy", fetched_at=now)
    old = web(store, goals[0], "old sqlite policy", fetched_at=now - timedelta(days=2))
    store.session.commit()
    assert not old.current and newest.current
    reverted = web(store, goals[0], "old sqlite policy", fetched_at=now + timedelta(days=1))
    store.session.commit()
    assert reverted.id == old.id and reverted.current and not newest.current
    assert store.search(goals[0].id, "sqlite policy")["hits"][0]["quote"] == "old sqlite policy"


def test_capacity_keeps_hot_and_recent_entries_without_deleting_material_source(library):
    store, goals, _ = library
    source, _, _ = material(store, goals[0])
    hot = web(store, goals[0], "hot sqlite", uri="https://hot.test", fetched_at=utc_now() - timedelta(days=1))
    hot.accessed_count = 100
    hot.last_accessed_at = utc_now() - timedelta(days=1)
    cold = web(store, goals[0], "cold sqlite", uri="https://cold.test")
    cold.last_accessed_at = utc_now() - timedelta(days=60)
    store.settings = store.settings.model_copy(update={"knowledge_max_documents": 2})
    store.evict()
    store.session.commit()
    assert store.session.get(KnowledgeDocument, cold.id) is None
    assert store.session.get(KnowledgeDocument, hot.id) is not None
    assert store.session.get(MaterialSource, source.id) is not None


def test_oversized_document_is_skipped_whole_and_eviction_cascades_chunks(library):
    store, goals, _ = library
    store.settings = store.settings.model_copy(update={"knowledge_max_bytes": 1024})
    assert web(store, goals[0], "sqlite" * 1000) is None
    doc = web(store, goals[0], "sqlite writer")
    store.session.flush()
    store.session.execute(delete(KnowledgeDocument).where(KnowledgeDocument.id == doc.id))
    store.session.commit()
    assert store.session.scalar(select(func.count()).select_from(KnowledgeChunk)) == 0


def test_archive_keeps_verbatim_offsets_and_rejects_untrusted_path(library, tmp_path, monkeypatch):
    store, goals, _ = library
    import planner_api.research.archive_windows as archives
    monkeypatch.setattr(archives, "ROOT", tmp_path)
    monkeypatch.setattr(archives, "ARCHIVES", tmp_path / "allowed")
    (tmp_path / "allowed").mkdir()
    raw = "  # SQLite\n\n~~obsolete rule~~\n\n" + "正文必须保留条件与例外。" * 300
    target = tmp_path / "allowed/raw.md"
    target.write_text(raw, encoding="utf8")
    archive = {"reader": "crawl4ai", "artifacts": {"section_markdown": {
        "path": "allowed/raw.md", "sha256": hashlib.sha256(target.read_bytes()).hexdigest()}}}
    def packet(data):
        return ResearchPacket(question="sqlite", sources=[ResearchSource(url="https://sqlite.test", canonical_url="https://sqlite.test",
            read_status="SUCCEEDED", evidence_level="FULL_TEXT", evidence_archive=data)])
    assert store.ingest_packet(goals[0].id, packet(archive)) == 1
    doc = store.session.scalar(select(KnowledgeDocument))
    for chunk in store.session.scalars(select(KnowledgeChunk)):
        assert chunk.text == doc.body[chunk.char_start:chunk.char_end]
    assert "~~obsolete rule~~" in doc.body
    bad = json.loads(json.dumps(archive))
    bad["artifacts"]["section_markdown"]["path"] = "outside.md"
    assert store.ingest_packet(goals[1].id, packet(bad)) == 0


def test_search_snippets_and_distant_excerpts_are_not_made_into_full_documents(library):
    store, goals, _ = library
    packet = ResearchPacket(question="sqlite", sources=[ResearchSource(url="https://example.org/a", canonical_url="https://example.org/a",
        read_status="SUCCEEDED", evidence_level="FULL_TEXT")], passages=[
        EvidencePassage(passage_id="a", source_url_or_uri="https://example.org/a", text="sqlite writer first", content_hash=digest("sqlite writer first")),
        EvidencePassage(passage_id="b", source_url_or_uri="https://example.org/a", text="sqlite second window", content_hash=digest("sqlite second window")),
        EvidencePassage(passage_id="s", source_url_or_uri="https://example.org/a", text="snippet not evidence", content_hash="c" * 64,
                        trust_metadata={"is_search_snippet": True})])
    store.ingest_packet(goals[0].id, packet)
    store.session.commit()
    doc = store.session.scalar(select(KnowledgeDocument))
    assert doc.body == "" and doc.metadata_json["coverage"] == "EXCERPTS"
    texts = list(store.session.scalars(select(KnowledgeChunk.text)))
    assert set(texts) == {"sqlite writer first", "sqlite second window"}


class Encoder:
    def encode(self, values, *, query=False):
        return [[1.0, 0.0, 0.0] if "writer" in t or "mutex" in t else [0.0, 1.0, 0.0] for t in values]


def test_real_faiss_recall_without_word_overlap_and_corrupt_vectors_are_ignored(library):
    pytest.importorskip("faiss")
    store, goals, _ = library
    doc = web(store, goals[0], "single writer", vector=[1.0, 0.0, 0.0])
    web(store, goals[0], "museum", uri="https://unrelated.test", vector=[0.0, 1.0, 0.0])
    store.session.commit()
    store.settings = store.settings.model_copy(update={"knowledge_vectors_enabled": True})
    result = store.search(goals[0].id, "mutex", query_encoder=Encoder())
    assert result["diagnostics"]["vector"] == "FAISS_FLAT_IP"
    assert [h["document_id"] for h in result["hits"]] == [doc.id]
    chunk = store.session.scalar(select(KnowledgeChunk).where(KnowledgeChunk.document_id == doc.id))
    chunk.vector_json = [0.0, 1.0, 0.0]
    store.session.commit()
    assert not store.search(goals[0].id, "mutex", query_encoder=Encoder())["hits"]
    chunk.vector_json = [1.0, 0.0]
    store.session.commit()
    assert not store.search(goals[0].id, "mutex", query_encoder=Encoder())["hits"]
    with pytest.raises(ValueError):
        nearest([chunk], [float("nan")])


def test_vector_failure_falls_back_and_model_revision_mismatch_is_not_searched(library):
    store, goals, _ = library
    doc = web(store, goals[0], "sqlite writer", vector=[1.0, 0.0, 0.0])
    store.session.commit()
    store.settings = store.settings.model_copy(update={"knowledge_vectors_enabled": True,
                                                      "material_embedding_revision": "different"})
    result = store.search(goals[0].id, "mutex", query_encoder=Encoder())
    assert not result["hits"]
    class Broken:
        def encode(self, *args, **kwargs):
            raise RuntimeError("offline")
    result = store.search(goals[0].id, "sqlite writer", query_encoder=Broken())
    assert result["hits"][0]["document_id"] == doc.id
    assert result["diagnostics"]["vector"].startswith("FALLBACK")


def test_shared_vectors_do_not_displace_local_recall_budget(library):
    store, goals, _ = library
    local = web(store, goals[0], "one local", vector=[.9, .1, 0.0])
    for n in range(26):
        web(store, goals[1], "one shared", uri=f"https://shared.test/{n}", vector=[1.0, 0.0, 0.0])
    store.session.commit()
    store.settings = store.settings.model_copy(update={"knowledge_vectors_enabled": True})
    result = store.search(goals[0].id, "mutex", query_encoder=Encoder())
    assert result["hits"][0]["document_id"] == local.id
    assert any(h["scope"] == "SHARED" for h in result["hits"])


def test_failed_indexing_is_bounded_and_lexical_search_still_works(library):
    store, goals, factory = library
    web(store, goals[0], "sqlite writer")
    store.session.commit()
    class BrokenPassages(Encoder):
        def encode(self, values, *, query=False):
            if not query:
                raise RuntimeError("index unavailable")
            return super().encode(values, query=query)
    worker = KnowledgeWorker(factory, store.settings, load_encoder=BrokenPassages)
    for _ in range(3):
        with pytest.raises(RuntimeError):
            worker.run_once()
    store.session.expire_all()
    assert store.session.scalar(select(KnowledgeChunk.index_status)) == "FAILED"
    assert not worker.run_once()
    assert store.search(goals[0].id, "sqlite writer")["hits"]


def test_worker_restarts_from_durable_queue_and_does_not_resurrect_evicted_documents(library):
    store, goals, factory = library
    doc = web(store, goals[0], "sqlite writer")
    store.session.commit()
    worker = KnowledgeWorker(factory, store.settings, load_encoder=Encoder)
    assert worker.run_once()
    store.session.expire_all()
    assert store.session.scalar(select(KnowledgeChunk.index_status)) == "READY"
    assert not KnowledgeWorker(factory, store.settings, load_encoder=Encoder).run_once()
    chunk = store.session.scalar(select(KnowledgeChunk))
    chunk.index_status = "INDEXING"
    chunk.lease_until = utc_now() - timedelta(seconds=1)
    store.session.commit()
    class DeleteDuringEncode(Encoder):
        def encode(self, values, *, query=False):
            if not query:
                with factory() as session:
                    session.execute(delete(KnowledgeDocument).where(KnowledgeDocument.id == doc.id))
                    session.commit()
            return super().encode(values, query=query)
    assert KnowledgeWorker(factory, store.settings, load_encoder=DeleteDuringEncode).run_once()
    assert store.session.scalar(select(func.count()).select_from(KnowledgeChunk)) == 0


def test_warmup_failure_can_recover_before_publishing_query_encoder(library):
    store, goals, factory = library
    web(store, goals[0], "sqlite writer")
    store.session.commit()
    class BrokenWarmup(Encoder):
        def encode(self, *args, **kwargs):
            raise RuntimeError("temporary warmup failure")
    worker = KnowledgeWorker(factory, store.settings, load_encoder=BrokenWarmup)
    with pytest.raises(RuntimeError):
        worker.run_once()
    assert worker.encoder is None
    worker.load_encoder = Encoder
    assert worker.run_once()


def test_optional_capture_failure_preserves_callers_transaction(library, monkeypatch):
    store, goals, _ = library
    def broken(*args):
        raise RuntimeError("unavailable")
    monkeypatch.setattr(KnowledgeStore, "ingest_packet", broken)
    goals[0].title = "business update preserved"
    capture(store.session, store.settings, goal_id=goals[0].id, packet=ResearchPacket(question="q"))
    store.session.commit()
    assert store.session.get(Goal, goals[0].id).title == "business update preserved"


@pytest.mark.parametrize("query", ["只用官网", "不要用论坛", "Python latest rules", "明天余票", "site:sqlite.org policy"])
def test_reuse_does_not_guess_explicit_source_or_freshness_constraints(query):
    assert not reuse_allowed(query)


def test_old_packets_with_null_optional_fields_remain_compatible():
    assert reuse_allowed("sqlite writer", {"brief": None, "subqueries": None})
    assert not reuse_allowed("sqlite writer", {"brief": {"constraints": ["official only"]}})


def test_automatic_shared_reuse_rejects_negative_bge_and_falls_back_to_topic_anchor(library, monkeypatch):
    store, goals, _ = library
    doc = web(store, goals[1], "露营活动 野餐 三人预算", uri="https://museum.test/visiting")
    store.session.commit()
    def unrelated(query, candidates, settings):
        return candidates, {"status": "APPLIED", "relevance_scores": [-2.0] * len(candidates)}
    monkeypatch.setattr("planner_api.knowledge.search.rerank_candidates", unrelated)
    assert not store.search(goals[0].id, "露营活动 野餐", automatic=True)["hits"]
    assert store.search(goals[0].id, "露营活动 野餐")["hits"]
    monkeypatch.setattr("planner_api.knowledge.search.rerank_candidates", lambda q,c,s: (c,{"status":"OFF"}))
    assert not store.search(goals[0].id, "露营活动 野餐", automatic=True)["hits"]
    doc.title = "露营活动 野餐"
    store.session.commit()
    assert store.search(goals[0].id, "露营活动 野餐", automatic=True)["hits"]


def test_reference_definitions_cannot_win_as_repeated_keyword_evidence(library):
    store, goals, _ = library
    web(store, goals[0], "sqlite writer concurrency")
    web(store, goals[0], "[1]: https://sqlite.org/writer\n[2]: https://sqlite.org/writer", uri="https://refs.test")
    store.session.commit()
    hits = store.search(goals[0].id, "sqlite writer")["hits"]
    assert len(hits) == 1
    assert hits[0]["quote"] == "sqlite writer concurrency"
    assert retrieval_view("Original rule\n[1]: https://example.org") == "Original rule"


def test_bounded_context_and_consumer_use_cache_without_faking_new_research(library, monkeypatch):
    store, goals, _ = library
    web(store, goals[0], "sqlite writer concurrency")
    store.session.commit()
    monkeypatch.setattr("planner_api.research.consumer.get_settings", lambda: store.settings)
    context, run_id, status = ResearchContextService(store.session).prepare(
        goals[0].id, "sqlite writer", phase="CLARIFICATION")
    assert run_id is None and status == "PARTIAL"
    assert context["knowledge_cache"]["new_searches"] == 0
    assert context["selected_evidence"][0]["quote"] == "sqlite writer concurrency"
    assert len(json.dumps(context, ensure_ascii=False)) <= store.settings.knowledge_context_chars
    assert not cached_context(store.session, store.settings.model_copy(update={"knowledge_enabled": False}), goals[0].id, "sqlite")
    assert not ResearchContextService(store.session).prepare(goals[0].id, "只用官网查sqlite", phase="CLARIFICATION")[0]
    assert not ResearchContextService(store.session).prepare(goals[0].id, "sqlite writer", phase="CLARIFICATION", reuse_latest=False)[0]
    goals[0].raw_input = "只用官网讨论 SQLite writer"
    store.session.commit()
    assert not ResearchContextService(store.session).prepare(goals[0].id, "那写入呢", phase="CLARIFICATION")[0]


def test_existing_research_packet_has_priority_over_optional_knowledge(library, monkeypatch):
    store, goals, _ = library
    web(store, goals[0], "sqlite writer concurrency")
    packet = ResearchPacket(question="original research", status="PARTIAL")
    run = ResearchRun(goal_id=goals[0].id, question=packet.question, status="PARTIAL",
                      result_json=packet.model_dump(mode="json"))
    store.session.add(run)
    store.session.commit()
    monkeypatch.setattr("planner_api.research.consumer.get_settings", lambda: store.settings)
    def never_replace(*args, **kwargs):
        raise AssertionError("Must not replace the original research packet")
    monkeypatch.setattr("planner_api.knowledge.search.cached_context", never_replace)
    context, run_id, _ = ResearchContextService(store.session).prepare(goals[0].id, "sqlite writer", phase="CLARIFICATION")
    assert run_id == run.id and not context.get("knowledge_cache")


def test_management_api_sharing_reading_and_deletion(library, monkeypatch):
    store, goals, _ = library
    doc = web(store, goals[0], "sqlite writer", shared=False)
    store.session.commit()
    monkeypatch.setattr("planner_api.knowledge.api.get_settings", lambda: store.settings)
    app = create_app()
    app.dependency_overrides[get_db_session] = lambda: store.session
    with TestClient(app) as client:
        root = f"/api/goals/{goals[0].id}/knowledge"
        other = f"/api/goals/{goals[1].id}/knowledge"
        assert client.post(other + "/search", json={"query": "sqlite"}).json()["hits"] == []
        assert client.patch(root + f"/documents/{doc.id}/sharing", json={"shared": True}).status_code == 200
        assert client.get(other + f"/documents/{doc.id}").json()["chunks"][0]["text"] == "sqlite writer"
        assert client.delete(other + f"/documents/{doc.id}").status_code == 404
        assert client.delete(root + f"/documents/{doc.id}").json()["original_source_deleted"] is False
        assert client.get(root + f"/documents/{doc.id}").status_code == 404
        assert client.post(root + "/search", json={"query": " "}).status_code == 422


def test_migration_preserves_existing_data_and_can_be_downgraded(session_factory):
    module_path = Path(__file__).parents[1] / "migrations/versions/0064_shared_knowledge.py"
    spec = importlib.util.spec_from_file_location("knowledge_migration", module_path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    with session_factory() as session:
        engine = session.get_bind()
    with engine.begin() as connection:
        for name in ("knowledge_chunks", "knowledge_goal_links", "knowledge_documents"):
            connection.exec_driver_sql(f"DROP TABLE {name}")
        connection.exec_driver_sql("INSERT INTO goals (id,title,raw_input,status,planning_mode,schedule_type,timezone,created_at,updated_at,last_active_at) "
                                   "VALUES ('preserved','original','original','CLARIFYING','COMMITTED','RHYTHM','Asia/Shanghai',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)")
        with Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()
            assert connection.exec_driver_sql("SELECT title FROM goals WHERE id='preserved'").scalar_one() == "original"
            migration.downgrade()
        assert connection.exec_driver_sql("SELECT title FROM goals WHERE id='preserved'").scalar_one() == "original"
