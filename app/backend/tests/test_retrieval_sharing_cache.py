import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import event, select, text

from planner_api.ai import ProviderUnavailableError
from planner_api.config import Settings
from planner_api.materials.analysis_ai import RealMaterialAnalysisProvider
from planner_api.materials.analysis_service import MaterialAnalysisService
from planner_api.materials.processing import MaterialProcessingService
from planner_api.materials.retrieval_cache import (
    BoundedCache,
    query_plans,
    query_vectors,
    bge_scores,
)
from planner_api.materials.schemas import (
    MaterialAnalyzeRequest,
    MaterialSearchHitRead,
    MaterialSearchRequest,
)
from planner_api.materials import semantic
from planner_api.models import (
    Goal,
    GoalMaterialLink,
    MaterialChunk,
    MaterialEmbedding,
    MaterialRevision,
    MaterialSource,
)


@pytest.fixture(autouse=True)
def empty_cache():
    query_plans.clear()
    query_vectors.clear()
    bge_scores.clear()


def settings(**kwargs):
    return Settings(
        _env_file=None,
        llm_provider="deepseek",
        llm_api_key="unit-test",
        llm_max_retries=0,
        **kwargs,
    )


def corpus(session):
    goal = Goal(title="Synthetic retrieval", raw_input="two topics")
    source = MaterialSource(title="Source", canonical_uri="synthetic://sharing")
    session.add_all([goal, source])
    session.flush()
    revision = MaterialRevision(
        source_id=source.id, revision=1, status="READY", content_hash="v1", file_size=100
    )
    session.add(revision)
    session.flush()
    session.add(GoalMaterialLink(goal_id=goal.id, source_id=source.id))
    for index, body in enumerate(
        ["alpha retry 12 production 3", "beta export 1536 MiB", "alpha beta 128 memory"]
    ):
        chunk = MaterialChunk(
            revision_id=revision.id,
            sequence=index,
            section_title=f"section {index}",
            text=body,
            content_hash=f"hash{index}",
            char_start=index * 100,
            char_end=index * 100 + len(body),
        )
        session.add(chunk)
        session.flush()
        session.execute(
            text(
                "INSERT INTO material_chunks_fts(chunk_id,revision_id,search_text) VALUES(:id,:r,:t)"
            ),
            {"id": chunk.id, "r": revision.id, "t": body},
        )
        session.add(
            MaterialEmbedding(
                chunk_id=chunk.id,
                model=settings().material_embedding_identity,
                content_hash=chunk.content_hash,
                vector_json=[[1.0, 0.0], [0.0, 1.0], [0.7, 0.7]][index],
            )
        )
    session.commit()
    return goal, source, revision


def requests(source):
    return [
        MaterialSearchRequest(
            query=query, source_ids=[source.id], max_chunks=3, max_chars=200, max_total_chars=1000
        )
        for query in ("alpha", "beta")
    ]


class Encoder:
    def __init__(self):
        self.batches = []

    def encode(self, values, **kwargs):
        assert kwargs == {"query": True}
        self.batches.append(list(values))
        return [[1.0, 0.0] if query == "alpha" else [0.0, 1.0] for query in values]


def test_multi_query_same_results_one_scan_and_vector_batch(session_factory, monkeypatch):
    enc = Encoder()
    monkeypatch.setattr(semantic, "encoder", lambda *_: enc)
    with session_factory() as session:
        goal, source, _ = corpus(session)
        statements = []
        event.listen(
            session.get_bind(),
            "before_cursor_execute",
            lambda c, u, s, p, x, m: statements.append(s),
        )
        old = MaterialProcessingService(
            session,
            settings(material_semantic_enabled=True, material_retrieval_shared_enabled=False),
        )
        baseline = old.search_goal_materials_many(goal.id, requests(source))
        old_scans = sum("JOIN material_embeddings" in s for s in statements)
        statements.clear()
        enc.batches.clear()
        shared = MaterialProcessingService(session, settings(material_semantic_enabled=True))
        actual = shared.search_goal_materials_many(goal.id, requests(source))
        assert actual == baseline
        assert old_scans == 2 and sum("JOIN material_embeddings" in s for s in statements) == 1
        assert enc.batches == [["alpha", "beta"]]
        assert shared.retrieval_status["query_vectors_encoded"] == 2
        statements.clear()
        assert shared.search_goal_materials_many(goal.id, requests(source)) == actual
        assert enc.batches == [["alpha", "beta"]]
        assert shared.retrieval_status["query_vector_hits"] == 2
        assert (
            sum("JOIN material_embeddings" in s for s in statements) == 1
        )  # fresh index read on warm request


@pytest.mark.parametrize(
    "mutation",
    ["new_version", "index_rebuild", "chunk_text", "unlink", "disabled_root", "source_title"],
)
def test_warm_query_never_caches_authority_or_documents(session_factory, monkeypatch, mutation):
    enc = Encoder()
    monkeypatch.setattr(semantic, "encoder", lambda *_: enc)
    with session_factory() as session:
        goal, source, revision = corpus(session)
        service = MaterialProcessingService(session, settings(material_semantic_enabled=True))
        before = service.search_goal_materials_many(goal.id, requests(source))
        if mutation == "unlink":
            session.scalar(select(GoalMaterialLink)).status = "REVOKED"
        elif mutation == "disabled_root":
            source.status = "DISABLED"
        elif mutation == "new_version":
            session.add(
                MaterialRevision(
                    source_id=source.id, revision=2, status="QUEUED", content_hash="v2", file_size=1
                )
            )
        elif mutation == "index_rebuild":
            session.execute(text("UPDATE material_embeddings SET vector_json='[0.0, 1.0]'"))
        elif mutation == "chunk_text":
            session.execute(
                text(
                    "UPDATE material_chunks SET text='alpha new evidence',content_hash='new-hash' WHERE sequence=0"
                )
            )
        else:
            source.title = "Renamed"
        session.commit()
        if mutation in {"unlink", "disabled_root"}:
            with pytest.raises(HTTPException):
                service.search_goal_materials_many(goal.id, requests(source))
        else:
            after = service.search_goal_materials_many(goal.id, requests(source))
            if mutation == "new_version":
                assert after == [[], []]
            elif mutation == "chunk_text":
                assert any("new evidence" in h.text for h in after[0])
            elif mutation == "index_rebuild":
                assert after != before
            else:
                assert after == before  # fresh metadata in scope hash, no text mutation


def test_revoke_during_search_discards_all_results(session_factory, monkeypatch):
    with session_factory() as session:
        goal, source, _ = corpus(session)
        service = MaterialProcessingService(session, settings())
        real = service.search_goal_materials

        def changing(*args, **kwargs):
            hits = real(*args, **kwargs)
            session.execute(text("UPDATE goal_material_links SET status='REVOKED'"))
            session.commit()
            return hits

        monkeypatch.setattr(service, "search_goal_materials", changing)
        with pytest.raises(HTTPException) as failure:
            service.search_goal_materials_many(goal.id, requests(source))
        assert failure.value.status_code == 422


def test_index_changes_during_scan_retry_once_not_mixed(session_factory, monkeypatch):
    with session_factory() as session:
        goal, source, _ = corpus(session)
        service = MaterialProcessingService(session, settings())
        real = service.search_goal_materials
        calls = []

        def changing(*args, **kwargs):
            hits = real(*args, **kwargs)
            calls.append(1)
            if len(calls) == 1:
                session.execute(text("UPDATE material_embeddings SET vector_json='[0.0, 1.0]'"))
                session.commit()
            return hits

        monkeypatch.setattr(service, "search_goal_materials", changing)
        assert service.search_goal_materials_many(goal.id, requests(source))
        assert len(calls) == 4 and service.retrieval_status["snapshot_retries"] == 1


def test_semantic_failure_not_retried_per_topic_fts_preserved(session_factory, monkeypatch):
    calls = []

    def failed(*args, **kwargs):
        calls.append(1)
        raise OSError("offline encoder missing")

    monkeypatch.setattr(semantic, "encoder", failed)
    with session_factory() as session:
        goal, source, _ = corpus(session)
        plain = MaterialProcessingService(session, settings()).search_goal_materials_many(
            goal.id, requests(source)
        )
        service = MaterialProcessingService(session, settings(material_semantic_enabled=True))
        assert service.search_goal_materials_many(goal.id, requests(source)) == plain
        assert calls == [1] and service.retrieval_status["semantic_fallback"]


def test_same_chunk_different_excerpts_preserve_topics_and_citations():
    hits = [
        MaterialSearchHitRead(
            source_id="s",
            revision_id="r",
            revision=1,
            sequence=0,
            text=body,
            score=1,
            locator=location,
        )
        for body, location in [("alpha approved", "page1"), ("beta rejected", "page8")]
    ]
    processing = SimpleNamespace(
        settings=settings(),
        search_goal_materials=lambda g, r: [hits[0 if r.query == "alpha" else 1]],
    )
    session = SimpleNamespace(get=lambda *_: SimpleNamespace(title="source", relative_path="x"))
    service = MaterialAnalysisService(
        session, SimpleNamespace(plan_queries=lambda _: ["alpha", "beta"]), processing
    )
    service._goal = lambda _: None
    passages, _, _ = service._retrieve("g", MaterialAnalyzeRequest(query="alpha and beta"))
    assert len(passages) == 2 and len({p.passage_id for p in passages}) == 2
    assert {p.text for p in passages} == {"alpha approved", "beta rejected"}
    assert {p.locator for p in passages} == {"page1", "page8"}


def provider_transport(calls):
    def transport(*args):
        calls.append(args[2])
        return {
            "choices": [
                {"message": {"content": json.dumps({"queries": ["alpha beta not production"]})}}
            ]
        }

    return transport


def test_query_plan_cache_full_question_scope_and_policy():
    calls = []
    provider = RealMaterialAnalysisProvider(settings(), transport=provider_transport(calls))
    scope = {"goal_id": "g", "source_ids": ["s"]}
    query = "background " * 400 + "only test, exclude production"
    assert provider.plan_queries_scoped(query, scope) == provider.plan_queries_scoped(query, scope)
    assert len(calls) == 1 and provider.query_plan_status["status"] == "HIT"
    provider.plan_queries_scoped(query + " and exclude draft", scope)
    provider.plan_queries_scoped(query, {**scope, "goal_id": "other"})
    provider.plan_queries_scoped(query, {**scope, "source_ids": ["another"]})
    provider.settings.material_query_policy = "STANDARD"
    provider.plan_queries_scoped(query, scope)
    assert len(calls) == 5
    assert json.loads(calls[0]["messages"][1]["content"])["query"] == query


def test_query_fallback_and_length_stop_not_cached(monkeypatch):
    provider = RealMaterialAnalysisProvider(settings(), transport=provider_transport([]))
    calls = []

    def failed(*args, **kwargs):
        calls.append(1)
        raise ProviderUnavailableError("timeout")

    monkeypatch.setattr(provider, "_request", failed)
    for _ in range(2):
        assert provider.plan_queries_scoped("alpha beta", {"goal_id": "g"}) == ["alpha beta"]
    assert calls == [1, 1] and provider.query_plan_status["status"] == "UNCACHED_FALLBACK"

    def truncated(*args, **kwargs):
        calls.append(1)
        return {
            "choices": [
                {"finish_reason": "length", "message": {"content": '{"queries":["alpha"]}'}}
            ]
        }

    provider = RealMaterialAnalysisProvider(settings(), transport=truncated)
    for _ in range(2):
        assert provider.plan_queries_scoped("alpha beta", {"goal_id": "g"}) == ["alpha beta"]
    assert len(calls) == 4


def test_query_plan_off_and_concurrent_deduplication():
    calls = []
    entered = threading.Event()
    release = threading.Event()

    def slow(*args):
        calls.append(1)
        entered.set()
        assert release.wait(2)
        return {"choices": [{"message": {"content": '{"queries":["alpha"]}'}}]}

    provider = RealMaterialAnalysisProvider(settings(), transport=slow)
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [
            pool.submit(provider.plan_queries_scoped, "alpha beta", {"goal_id": "g"})
            for _ in range(4)
        ]
        assert entered.wait(1)
        time.sleep(0.02)
        release.set()
        assert [f.result() for f in futures] == [["alpha"]] * 4
    assert len(calls) == 1
    provider.settings.material_query_cache_enabled = False
    provider.plan_queries_scoped("alpha beta", {"goal_id": "g"})
    provider.plan_queries_scoped("alpha beta", {"goal_id": "g"})
    assert len(calls) == 3


def test_cache_bounded_ttl_copies_and_failure_releases_flight(monkeypatch):
    cache = BoundedCache(max_entries=2, max_bytes=1000)
    clock = [0.0]
    monkeypatch.setattr("planner_api.materials.retrieval_cache.time.monotonic", lambda: clock[0])
    cache.put("a", [1], 10)
    value = cache.get("a")
    value.append(2)
    assert cache.get("a") == [1]
    cache.put("b", [2], 10)
    cache.put("c", [3], 10)
    assert cache.get("a") is None
    clock[0] = 11
    assert cache.get("b") is None and cache.get("c") is None

    def failed():
        raise ValueError("failed")

    with pytest.raises(ValueError):
        cache.compute("x", failed, ttl=10, wait_seconds=1)
    assert cache.compute("x", lambda: ([4], True), ttl=10, wait_seconds=1) == ([4], "MISS")
    cache.put("large", ["x" * 2000], 10)
    assert cache.get("large") is None


@pytest.mark.parametrize(
    "change",
    ["question", "title", "path", "locator", "text", "order", "anchors", "scope", "endpoint"],
)
def test_bge_cache_exact_evidence_contract(monkeypatch, change):
    from dataclasses import replace
    from planner_api.materials import reranking
    from test_material_reranking import passages, valid

    calls = []

    def transport(*args):
        calls.append(1)
        return valid([0, 1, 3, 2, 4, 5])

    monkeypatch.setattr(reranking, "_local_post", transport)
    original = passages()
    config = settings(material_rerank_mode="GUARDED")
    kwargs = {"topic_anchors": set(), "transport": transport, "cache_scope": {"goal_id": "g"}}
    _, a = reranking.guarded_order("migration evidence", original, config, **kwargs)
    _, b = reranking.guarded_order("migration evidence", original, config, **kwargs)
    assert a["score_cache"] == "MISS" and b["score_cache"] == "HIT" and len(calls) == 1
    question = "migration evidence"
    if change == "question":
        question += " except approved drafts"
    elif change == "title":
        original[1] = replace(original[1], source_title="Renamed")
    elif change == "path":
        original[1] = replace(original[1], relative_path="new.pdf")
    elif change == "locator":
        original[1] = replace(original[1], locator="page 20")
    elif change == "text":
        original[1] = replace(original[1], text="new evidence")
    elif change == "order":
        original[1], original[2] = original[2], original[1]
    elif change == "anchors":
        kwargs["topic_anchors"] = {"2"}
    elif change == "scope":
        kwargs["cache_scope"] = {"goal_id": "other"}
    else:
        config.local_model_base_url = "http://127.0.0.1:3213"
    _, c = reranking.guarded_order(question, original, config, **kwargs)
    assert c["score_cache"] == "MISS" and len(calls) == 2


def test_warm_plan_access_rechecked_before_model(session_factory):
    calls = []
    provider = RealMaterialAnalysisProvider(settings(), transport=provider_transport(calls))
    with session_factory() as session:
        goal, source, _ = corpus(session)
        processing = MaterialProcessingService(session, settings())
        service = MaterialAnalysisService(session, provider, processing)
        request = MaterialAnalyzeRequest(query="alpha beta", source_ids=[source.id])
        service._retrieve(goal.id, request)
        assert len(calls) == 1
        session.scalar(select(GoalMaterialLink)).status = "REVOKED"
        session.commit()
        with pytest.raises(HTTPException):
            service._retrieve(goal.id, request)
        assert len(calls) == 1


def test_source_metadata_and_excerpt_are_fresh_in_same_session(session_factory):
    with session_factory() as session:
        goal, source, _ = corpus(session)
        service = MaterialAnalysisService(
            session,
            SimpleNamespace(plan_queries=lambda _: ["alpha"]),
            MaterialProcessingService(session, settings()),
        )
        request = MaterialAnalyzeRequest(query="alpha details", source_ids=[source.id])
        first, _, _ = service._retrieve(goal.id, request)
        assert all(p.source_title == "Source" for p in first)
        session.execute(
            text("UPDATE material_sources SET title='New Title',relative_path='new.md'")
        )
        session.commit()
        final, _, _ = service._retrieve(goal.id, request)
        assert all(p.source_title == "New Title" and p.relative_path == "new.md" for p in final)
