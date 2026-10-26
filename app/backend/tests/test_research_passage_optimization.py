import json
from types import SimpleNamespace

import pytest
from pydantic import SecretStr

from planner_api.config import Settings
from planner_api.research.archive_windows import prose_windows
from planner_api.research.evidence import build_web_passages
from planner_api.research.intent import ModelResearchIntentAnalyzer
from planner_api.research import passage_retrieval as retrieval
from planner_api.research.schemas import ResearchBrief, ResearchPacket, ResearchSource


def settings(**values):
    return Settings(_env_file=None, llm_provider="openai_compatible", llm_api_key=SecretStr("test"), **values)


def test_full_long_paragraph_tail_is_scanned_with_exact_offsets():
    text = "Background without the requested relation. " * 200
    text += "Omega operations require authorization before proceeding. Unless approved, do not proceed.\n"
    ranker = retrieval.PassageRanker(["Omega authorization approved"], settings(research_rerank_mode="OFF"), len(text))
    windows = prose_windows(text, ranker.queries, 300, ranker=ranker)
    assert any("Omega operations" in w.text and "Unless approved" in w.text for w in windows)
    for window in windows:
        assert text[window.char_start:window.char_end] == window.text
    assert len("\n\n".join(w.text for w in windows)) <= 300


def test_no_small_model_route_never_loads_encoder_or_calls_bge(monkeypatch):
    monkeypatch.setattr(retrieval, "_vectors", lambda *a: pytest.fail("E5 was called"))
    monkeypatch.setattr(retrieval, "rerank_candidates", lambda *a: pytest.fail("BGE was called"))
    text = "Navigation and irrelevant background for another subsystem.\n\n" * 300
    text += "Omega operations require separate authorization before a session closes.\n"
    ranker = retrieval.PassageRanker(["Omega authorization"],
        settings(research_rerank_mode="OFF", research_passage_semantic_enabled=True), len(text))
    assert "Omega" in prose_windows(text, ranker.queries, 300, ranker=ranker)[0].text
    assert ranker.info["embedding"] == "OFF"


def test_vector_recall_can_find_paraphrase_without_literal_overlap(monkeypatch):
    from planner_api.materials import semantic

    text = "Unrelated navigation and examples for another subsystem.\n\n" * 3
    text += "Bookings are mandatory; visitors cannot enter without a reserved ticket.\n"
    monkeypatch.setattr(retrieval, "_vectors", lambda texts, *a: [[0, 1]] * (len(texts) - 1) + [[1, 0]])
    monkeypatch.setattr(semantic, "encoder", lambda *a: SimpleNamespace(encode=lambda *a, **kw: [[1, 0]]))
    monkeypatch.setattr(retrieval, "rerank_candidates", lambda query, items, s: (items, {"status": "FALLBACK", "runtime_ms": 1}))
    ranker = retrieval.PassageRanker(["预约入馆要求"],
        settings(research_rerank_mode="BGE", research_passage_semantic_enabled=True), 20000)
    windows = prose_windows(text, ranker.queries, 120, ranker=ranker)
    assert any("Bookings are mandatory" in w.text for w in windows)
    assert ranker.info["embedding"] == "APPLIED"


def test_bge_is_one_bounded_request_with_each_subquestion_best_match_retained(monkeypatch):
    calls = []
    def rerank(query, items, s):
        calls.append((query, items))
        return list(reversed(items)), {"status": "APPLIED", "runtime_ms": 2}
    monkeypatch.setattr(retrieval, "rerank_candidates", rerank)
    blocks = [SimpleNamespace(text=f"original {i}") for i in range(100)]
    ranker = retrieval.PassageRanker(["alpha", "omega"],
        settings(research_rerank_mode="BGE", material_semantic_enabled=True), 20000)
    ranks = ranker(blocks, [list(range(50)), list(range(50, 100))])
    assert len(calls) == 1 and len(calls[0][1]) == 24
    assert {item.index for item in calls[0][1]} == {*range(12), *range(50, 62)}
    assert ranks[0][0] == 0 and ranks[1][0] == 50
    assert ranker.info["embedding"] == "OFF"  # Document E5 cannot enable web E5.


def test_embedding_failure_retains_lexical_fallback_and_budget_is_explicit(monkeypatch):
    monkeypatch.setattr(retrieval, "_vectors", lambda *a: (_ for _ in ()).throw(RuntimeError("unavailable")))
    monkeypatch.setattr(retrieval, "rerank_candidates", lambda query, items, s: (items, {"status": "FALLBACK", "runtime_ms": 1}))
    config = settings(research_rerank_mode="BGE", research_passage_semantic_enabled=True)
    ranker = retrieval.PassageRanker(["alpha"], config, 20000)
    assert ranker([SimpleNamespace(text="alpha")], [[0]]) == [[0]]
    assert ranker.info["embedding"] == "FALLBACK"
    ranker = retrieval.PassageRanker(["alpha"], config, 200000)
    assert ranker([SimpleNamespace(text="alpha")] * 161, [[160, 0]]) == [[160, 0]]
    assert ranker.info["embedding"] == "SKIPPED_BUDGET"


def packet(count, size=100):
    p = ResearchPacket(question="What are the rules?", evidence_mode="SPANS",
                       brief=ResearchBrief(required_aspects=["rules"]))
    for index in range(count):
        text = (f"Source {index} original rule and conditions. " * (size // 40 + 1))[:size]
        p.sources.append(ResearchSource(url=f"https://source{index}.example/rules",
            canonical_url=f"https://source{index}.example/rules", read_status="SUCCEEDED", content_excerpt=text))
    p.passages = build_web_passages(p, preserve_markup=True)
    return p


@pytest.mark.parametrize("optimized,expected", [(True, 1), (False, 3)])
def test_batching_preserves_separate_sources_and_legacy_switch(optimized, expected):
    p, calls = packet(3), []
    def transport(url, headers, payload, timeout):
        data = json.loads(payload["messages"][1]["content"])
        calls.append(data)
        return {"choices": [{"message": {"content": json.dumps({"selections": [
            {"span_id": span["span_id"], "covers_aspects": ["rules"]} for span in data["spans"]]})}}]}
    result = ModelResearchIntentAnalyzer(settings(research_passage_optimization_enabled=optimized),
                                         transport=transport).select_evidence(p)
    assert len(calls) == expected and len(result.processed_passage_ids) == 3
    assert {e.source_url_or_uri for e in result.evidence} == {s.canonical_url for s in p.sources}
    assert all(e.quote == next(b.text for b in p.passages if b.passage_id == e.passage_id) for e in result.evidence)
    if optimized:
        assert len(calls[0]["sources"]) == 3
        assert {s["source_id"] for s in calls[0]["spans"]} == {"S1", "S2", "S3"}


def test_budget_overflow_splits_whole_sources_and_never_marks_omitted_text_processed():
    p, calls = packet(5, 6000), []
    def transport(url, headers, payload, timeout):
        data = json.loads(payload["messages"][1]["content"])
        calls.append(data)
        return {"choices": [{"message": {"content": '{"selections":[]}'}}]}
    result = ModelResearchIntentAnalyzer(settings(), transport=transport).select_evidence(p)
    assert 1 < len(calls) < 5
    assert set(result.processed_passage_ids) == {p.passage_id for p in p.passages}
    submitted = {s["span_id"]: s["text"] for data in calls for s in data["spans"]}
    from planner_api.research.selection import evidence_spans
    assert submitted == {s["span_id"]: s["text"] for p in p.passages for s in evidence_spans(p)}
    assert all(len(json.dumps(data, ensure_ascii=False)) <= 20000 for data in calls)


def test_failed_batch_is_pending_while_other_batch_can_complete():
    p, count = packet(4), 0
    def transport(url, headers, payload, timeout):
        nonlocal count
        count += 1
        if count == 1:
            raise TimeoutError("temporary")
        return {"choices": [{"message": {"content": '{"selections":[]}'}}]}
    result = ModelResearchIntentAnalyzer(settings(), transport=transport).select_evidence(p)
    assert result.processed_passage_ids == [p.passages[-1].passage_id]
    assert result.diagnostics
