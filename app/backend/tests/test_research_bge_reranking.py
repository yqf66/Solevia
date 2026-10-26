import copy

import pytest

from planner_api.config import Settings
from planner_api.research.orchestrator import ResearchOrchestrator
from planner_api.research.ranking import RankedHit
from planner_api.research.ranking import RankingPolicy, rank_candidates
from planner_api.research.reranking import IDENTITY, limit_candidates, recall_constraints, rerank_candidates
from planner_api.research.schemas import (
    ResearchPacket, ResearchQueryConstraints, ResearchSubquery, SearchHit, SearchResponse,
)


def candidates():
    return [RankedHit(hit=SearchHit(url="https://docs.example.com/general", title="SQLite WAL overview",
                                  snippet="SQLite WAL installation overview", engine="bing"),
                      canonical_url="https://docs.example.com/general", score=90, quality_score=18,
                      source_category="DOCUMENTATION"),
            RankedHit(hit=SearchHit(url="https://stackoverflow.com/questions/12345", title="SQLite WAL writer contention",
                                  snippet="Only one writer can append at a time; concurrent readers proceed.", engine="bing"),
                      canonical_url="https://stackoverflow.com/questions/12345", score=60, quality_score=3,
                      source_category="COMMUNITY")]


def response(scores):
    return {"identity": IDENTITY, "scores": scores,
            "coverage": {"complete": True, "windows_per_passage": [1] * len(scores),
                         "total_windows": len(scores), "version": "bge-coverage-v2"}}


def test_real_order_can_promote_relevant_community_over_generic_documentation():
    items = candidates()
    before = {i.canonical_url: i.hit.model_dump() for i in items}
    ranked, info = rerank_candidates("SQLite WAL single writer", items,
        Settings(_env_file=None, research_rerank_mode="BGE"), transport=lambda *_: response([-4, 5]))
    assert ranked[0].source_category == "COMMUNITY" and info["order_changed"]
    assert {i.canonical_url: i.hit.model_dump() for i in ranked} == before
    packet = ResearchPacket(question="SQLite WAL", subqueries=[ResearchSubquery(query="SQLite WAL single writer")])
    for item in ranked:
        item.matched_queries = [packet.subqueries[0].query]
    packet.sources = [ResearchOrchestrator._source(i) for i in ranked]
    assert ResearchOrchestrator._ordered_read_candidates(packet)[0].source_category == "COMMUNITY"


@pytest.mark.parametrize("bad", [
    {"identity": "other"}, {"scores": [1]}, {"scores": [True, 2]}, {"scores": [float("nan"), 2]},
    {"coverage": {"complete": False}},
    {"coverage": {"complete": True, "windows_per_passage": [1, 1], "total_windows": 3, "version": "bge-coverage-v2"}},
])
def test_invalid_response_keeps_exact_baseline(bad):
    items = candidates()
    before = copy.deepcopy(items)
    ranked, info = rerank_candidates("SQLite WAL", items, Settings(_env_file=None, research_rerank_mode="BGE"),
                                    transport=lambda *_: {**response([1, 2]), **bad})
    assert ranked == before and info["status"] == "FALLBACK"


def test_off_and_timeout_preserve_baseline():
    def failed(*_):
        raise TimeoutError()
    items = candidates()
    assert rerank_candidates("SQLite", items, Settings(_env_file=None), transport=failed)[1]["status"] == "OFF"
    ranked, info = rerank_candidates("SQLite", items, Settings(_env_file=None, research_rerank_mode="BGE"), transport=failed)
    assert ranked == items and info["status"] == "FALLBACK"


def test_full_text_is_never_silently_truncated_for_bge():
    items = candidates()
    items[0].hit.snippet = "x" * 2201
    def forbidden(*_):
        raise AssertionError("must skip oversized inputs")
    ranked, info = rerank_candidates("SQLite", items, Settings(_env_file=None, research_rerank_mode="BGE"), transport=forbidden)
    assert ranked == items and info["status"] == "SKIPPED_INPUT_BUDGET"


def test_diversity_limit_happens_after_semantic_ranking():
    items = candidates()
    other = copy.deepcopy(items[0])
    other.canonical_url = "https://docs.example.com/actual-rule"
    other.hit.url = other.canonical_url
    selected = limit_candidates([other, items[0], items[1]], max_results=3, max_per_domain=1)
    assert selected == [other, items[1]]


def test_semantic_pool_preserves_paraphrases_but_not_wrong_versions():
    hits = [SearchHit(url="https://example.com/actual", title="Write-Ahead Logging",
                      snippet="SQLite WAL 3.14 allows one writer at a time.", engine="bing"),
            SearchHit(url="https://example.com/old", title="Write-Ahead Logging",
                      snippet="SQLite WAL 3.12 allows one writer at a time.", engine="bing")]
    outcome = rank_candidates("SQLite WAL 3.14 concurrent writing", hits, max_results=24,
        policy=RankingPolicy(min_relevance_score=0, allow_partial_overlap=True),
        constraints=ResearchQueryConstraints(entities=["SQLite"], strict_terms=["3.14"]))
    assert len(outcome.accepted) == 1 and outcome.accepted[0].canonical_url.endswith("/actual")


def test_subquery_read_slots_survive_bge_order():
    queries = [ResearchSubquery(query="SQLite WAL"), ResearchSubquery(query="uv Windows")]
    packet = ResearchPacket(question="two aspects", subqueries=queries)
    for q in queries:
        for item in candidates():
            item.matched_queries = [q.query]
            item.reasons = ["BGE_HYBRID_RANK"]
            packet.sources.append(ResearchOrchestrator._source(item))
    ordered = ResearchOrchestrator._ordered_read_candidates(packet)
    assert ordered[0].matched_queries == [queries[0].query]
    assert ordered[1].matched_queries == [queries[1].query]


def test_snippet_recall_separates_semantics_from_exact_requirements():
    original = ResearchQueryConstraints(
        strict_terms=["SQLite", "WAL", "multiple processes", "official documentation", "3.14", "错误原文"],
        literal_terms=["SQLITE_BUSY"], entities=["SQLite"], specifications=["3.14"],
    )
    admitted = recall_constraints(original, '请查SQLite WAL 3.14，保留“错误原文”和“SQLITE_BUSY”')
    assert admitted.strict_terms == ["SQLite", "WAL", "3.14", "错误原文"]
    assert admitted.literal_terms == ["SQLITE_BUSY"] and admitted.specifications == ["3.14"]
    assert "multiple processes" in original.strict_terms


def test_planner_cannot_invent_literal_match_requirements():
    constraints = ResearchQueryConstraints(literal_terms=["exists()"], strict_terms=["3.14"])
    admitted = recall_constraints(constraints, "查Python 3.14 Path.exists 的行为")
    assert not admitted.literal_terms and admitted.strict_terms == ["3.14"]


def test_verified_reference_bookmarks_are_opt_in_and_publisher_scoped():
    packet = ResearchPacket(question="SQLite WAL", subqueries=[ResearchSubquery(
        query="SQLite WAL concurrent writes", source_profiles=["official_docs"],
        reference_urls=["https://evil.example/wal.html"],
    )])
    ResearchOrchestrator._seed_reference_sources(packet)
    assert not packet.sources
    ResearchOrchestrator._seed_reference_sources(packet, extended=True)
    assert [str(s.url) for s in packet.sources] == ["https://www.sqlite.org/wal.html"]
    assert packet.sources[0].evidence_level == "UNVERIFIED"


def test_uv_python_source_preference_targets_uv_publisher():
    from planner_api.research.sources import official_documentation_domains
    assert official_documentation_domains("uv Windows install Python versions", extended=True)[0] == "docs.astral.sh"
    assert official_documentation_domains("uv Windows install Python versions") == ("docs.python.org",)


def test_hard_constraint_weak_snippets_never_enter_bge_read_queue():
    item = candidates()[1]
    item.reasons = ["SNIPPET_FALLBACK", "CONSTRAINT_MISMATCH"]
    packet = ResearchPacket(question="SQLite WAL", sources=[ResearchOrchestrator._source(item)])
    assert ResearchOrchestrator._ordered_read_candidates(packet)
    assert not ResearchOrchestrator._ordered_read_candidates(packet, enhanced=True)


def test_community_exploration_cannot_override_literal_or_version_gates():
    hit = SearchHit(url="https://stackoverflow.com/questions/12345", title="Python 3.12 TaskGroup",
                    snippet="TaskGroup cancel exceptions on Windows", engine="bing")
    policy = RankingPolicy(min_relevance_score=0, allow_partial_overlap=True,
                           allow_exploratory_entity_mismatch=True, enforce_exact_terms=True)
    for constraints in [ResearchQueryConstraints(strict_terms=["3.14"]),
                        ResearchQueryConstraints(literal_terms=["KeyboardInterrupt"])]:
        outcome = rank_candidates("Python TaskGroup cancellation", [hit], max_results=5,
                                  constraints=constraints, query_role="COMMUNITY", policy=policy)
        assert not outcome.accepted and outcome.filter_reasons["CONSTRAINT_MISMATCH"] == 1


def test_bge_parallel_reads_stop_at_remaining_global_page_budget():
    from planner_api.research.orchestrator import ResearchLimits
    from planner_api.research.schemas import PageDocument

    class Service:
        def __init__(self):
            self.calls = []

        def read(self, request):
            self.calls.append(str(request.url))
            return PageDocument(url=request.url, finalUrl=request.url, title="Evidence",
                                content=("SQLite WAL serialized writes and concurrent readers. " * 80),
                                retrievalMethod="request")

    service = Service()
    packet = ResearchPacket(question="SQLite WAL")
    packet.coverage.read_pages = 3
    for item in candidates():
        packet.sources.append(ResearchOrchestrator._source(item))
    engine = ResearchOrchestrator(service=service,
        settings=Settings(_env_file=None, research_rerank_mode="BGE"),
        limits=ResearchLimits(max_read_pages=4, max_pages_per_round=3, read_concurrency=3))
    engine._read_selected(packet)
    assert len(service.calls) == 1 and packet.coverage.read_pages == 4


def test_weak_documentation_cannot_undo_semantic_community_order():
    docs, community = candidates()
    docs.reasons = ["SNIPPET_FALLBACK", "LOW_RELEVANCE"]
    community.reasons = ["BGE_HYBRID_RANK"]
    packet = ResearchPacket(question="SQLite WAL", sources=[ResearchOrchestrator._source(docs),
                                                          ResearchOrchestrator._source(community)])
    assert ResearchOrchestrator._ordered_read_candidates(packet, enhanced=True)[0].source_category == "COMMUNITY"


def test_actual_search_path_applies_bge_but_retains_hard_constraints(monkeypatch):
    from planner_api.research import reranking
    monkeypatch.setattr(reranking, "rerank_candidates", lambda q, items, settings:
        rerank_candidates(q, items, settings, transport=lambda *_: response([-4, 5])))
    class Service:
        def search(self, request):
            self.request = request
            return SearchResponse(query=request.query, searchedAt="2026-10-13T00:00:00Z", engines=["bing"],
                results=[i.hit for i in candidates()] + [SearchHit(url="https://stackoverflow.com/questions/99999",
                    title="Other database writers", snippet="MySQL installation and concurrency", engine="bing")])
    service = Service()
    engine = ResearchOrchestrator(service=service, settings=Settings(_env_file=None, research_rerank_mode="BGE"))
    query = ResearchSubquery(query="SQLite WAL", query_intent="SQLite WAL single writer", purpose="DOCUMENTATION",
                            constraints=ResearchQueryConstraints(entities=["SQLite"], strict_terms=["SQLite", "WAL"]))
    packet = ResearchPacket(question=query.query_intent)
    selected = engine._search_question(packet, query, query.query)
    assert selected[0].source_category == "COMMUNITY"
    assert packet.search_diagnostics[0].rerank["status"] == "APPLIED"
    assert not any("99999" in item.canonical_url and "SNIPPET_FALLBACK" not in item.reasons for item in selected)


def test_declared_official_scope_cannot_be_expanded_by_bge(monkeypatch):
    from planner_api.research import reranking
    monkeypatch.setattr(reranking, "rerank_candidates", lambda q, items, settings: (items, {"status": "SKIPPED_SINGLE"}))
    class Service:
        def search(self, request):
            self.request = request
            return SearchResponse(query=request.query, searchedAt="2026-10-13T00:00:00Z", engines=["bing"],
                results=[SearchHit(url="https://docs.python.org/3.14/library/asyncio-task.html", title="Python 3.14 TaskGroup",
                                   snippet="Python 3.14 TaskGroup exception rules", engine="bing"),
                         SearchHit(url="https://stackoverflow.com/questions/12345", title="Python 3.14 TaskGroup",
                                   snippet="Python 3.14 TaskGroup exception experiences", engine="bing")])
    service = Service()
    engine = ResearchOrchestrator(service=service, settings=Settings(_env_file=None, research_rerank_mode="BGE"))
    query = ResearchSubquery(query="Python 3.14 TaskGroup", purpose="DOCUMENTATION", source_profiles=["official_docs"])
    packet = ResearchPacket(question="only official")
    selected = engine._search_question(packet, query, query.query)
    assert "site:docs.python.org" in service.request.query
    assert all("docs.python.org" in i.canonical_url for i in selected if "SNIPPET_FALLBACK" not in i.reasons)
    assert packet.search_diagnostics[0].source_scope_dropped_count == 1
