import hashlib
import json

import pytest
from pydantic import SecretStr

from planner_api.config import Settings
from planner_api.research import archive_windows as retrieval
from planner_api.research.context import compile_research_context
from planner_api.research.evidence import build_web_passages
from planner_api.research.orchestrator import ResearchLimits, ResearchOrchestrator
from planner_api.research.intent import ModelResearchIntentAnalyzer
from planner_api.research.schemas import (
    EvidenceSpanChoices, PageDocument, ResearchBrief, ResearchPacket,
    ResearchSource, ResearchSubquery,
)
from planner_api.research.selection import evidence_spans, resolve_span_choices
from planner_api.research.strategy import refresh_research_coverage


def archived_packet(tmp_path, monkeypatch, text):
    monkeypatch.setattr(retrieval, "ROOT", tmp_path)
    monkeypatch.setattr(retrieval, "ARCHIVES", tmp_path / "originals")
    artifact = tmp_path / "originals" / "section.md"
    artifact.parent.mkdir()
    data = text.encode()
    artifact.write_bytes(data)
    archive = {"reader": "crawl4ai", "evidence_basis": "section_markdown",
               "artifacts": {"section_markdown": {"path": "originals/section.md",
                                                      "sha256": hashlib.sha256(data).hexdigest()}}}
    source = ResearchSource(url="https://example.com/manual", canonical_url="https://example.com/manual",
                            read_status="NOT_SELECTED", matched_queries=["alpha", "omega"])
    packet = ResearchPacket(question="alpha omega", status="READY", evidence_mode="SPANS",
                            brief=ResearchBrief(required_aspects=["rules"]),
                            sources=[source], subqueries=[
                                ResearchSubquery(key="Q1", query="alpha", required_terms=["alpha"]),
                                ResearchSubquery(key="Q2", query="omega", required_terms=["omega"])])
    document = PageDocument(url=source.url, finalUrl=source.url, content=text[:900],
                            evidenceArchive=archive, qualityScore=90)
    return packet, source, document


def test_distant_original_ranges_do_not_become_one_quotation(tmp_path, monkeypatch):
    first = "Alpha operations are allowed only while this session remains open.\n"
    last = "Omega operations require separate authorization before any session is closed.\n"
    text = first + "\n" + ("Unrelated navigation and sample material without the requested technical terms.\n\n" * 60) + last
    packet, source, document = archived_packet(tmp_path, monkeypatch, text)
    excerpt, archive = retrieval.archived_excerpt(packet, source, document, 250)
    assert first in excerpt and last in excerpt and len(excerpt) <= 250
    source.content_excerpt, source.evidence_archive, source.read_status = excerpt, archive, "SUCCEEDED"
    packet.passages = build_web_passages(packet, preserve_markup=True)
    assert len(packet.passages) == 2
    for passage in packet.passages:
        span = evidence_spans(passage)[0]
        choices = EvidenceSpanChoices(selections=[{"span_id": span["span_id"], "covers_aspects": ["rules"]}])
        selected, diagnostics = resolve_span_choices(packet, passage, choices)
        assert not diagnostics
        for evidence in selected:
            origin = evidence.locator["archive_range"]
            assert evidence.quote == text[origin["char_start"]:origin["char_end"]]
            assert not ("Alpha" in evidence.quote and "Omega" in evidence.quote)
    refresh_research_coverage(packet)
    assert packet.status == "PARTIAL"  # Retrieval fidelity is not completeness.


def test_body_exception_keeps_previous_special_case_within_same_budget():
    special = "KeyboardInterrupt is treated specially and re-raised instead of being grouped.\n"
    body = "An exception from the body is handled with the same special case as in the previous paragraph.\n"
    text = "Navigation and examples for generic task management without exception details.\n\n" * 40 + special + body
    windows = retrieval.prose_windows(text, ["body exception previous paragraph"], 200)
    assert len(windows) == 1 and windows[0].text == special + body
    assert sum(len(w.text) for w in windows) <= 200


def test_chinese_prose_can_retrieve_separate_conditions():
    text = "以下为无关背景资料以及其他接口示例说明。\n\n" * 60
    text += "正常等待期间可以继续添加任务，但必须保留相关生命周期条件。\n\n"
    text += "无关接口示例与背景说明，不涉及任务组错误。\n\n" * 60
    text += "子任务报错后不能再添加任务，需要先完成剩余任务的取消和等待。\n"
    windows = retrieval.prose_windows(text, ["正常等待期间添加任务", "子任务报错后添加任务"], 140)
    assert any("正常等待期间可以" in w.text for w in windows)
    assert any("子任务报错后不能" in w.text for w in windows)
    assert len("\n\n".join(w.text for w in windows)) <= 140


def test_exception_selection_keeps_general_trigger_and_result_in_same_scope():
    general = "When an operation fails, all the remaining operations are cancelled and awaited.\n\n"
    result = "After all operations finish, their non-cancellation failures are raised together.\n\n"
    special = "Two base failures are treated specially: SpecialFailure is raised directly instead.\n\n"
    unrelated = "Background information about unrelated interfaces and examples without failure semantics.\n\n"
    text = unrelated * 30 + general + result + special + unrelated * 30
    windows = retrieval.prose_windows(text, ["SpecialFailure treated specially failures"], 350)
    selected = "\n\n".join(w.text for w in windows)
    assert general in selected and result in selected and special.rstrip() in selected
    assert len(selected) <= 350


def test_degraded_queries_keep_specific_identifier_without_conversational_filler():
    source = ResearchSource(url="https://example.com/manual", canonical_url="https://example.com/manual",
                            matched_queries=["SQLite WAL"])
    packet = ResearchPacket(question="先不改路线。SQLite WAL 的说明，写者数量、SQLITE_BUSY？",
                            subqueries=[ResearchSubquery(key="Q1", query="SQLite WAL",
                                                        anchor_entities=["SQLite", "WAL", "SQLITE_BUSY"])])
    queries = retrieval.source_queries(packet, source)
    assert "SQLITE_BUSY" in queries and "写者数量" in queries
    assert all("先不改路线" not in q for q in queries)


def test_single_combined_query_does_not_erase_separate_user_questions():
    source = ResearchSource(url="https://example.com/manual", canonical_url="https://example.com/manual",
                            matched_queries=["readers writers concurrency"])
    packet = ResearchPacket(question="请直接读取 https://example.com/manual 。读写能否并行、是否只有一个写者、SQLITE_BUSY？",
        subqueries=[ResearchSubquery(key="Q1", query="readers writers concurrency",
                                    required_terms=["readers", "writers", "concurrency"])])
    queries = retrieval.source_queries(packet, source)
    assert "是否只有一个写者" in queries and "SQLITE_BUSY" in queries
    assert all("https://" not in q for q in queries)


@pytest.mark.parametrize("tamper", ["hash", "outside", "missing", "shape"])
def test_untrusted_or_unavailable_archive_keeps_fallback(tmp_path, monkeypatch, tamper):
    packet, source, document = archived_packet(tmp_path, monkeypatch, "Exact original source text.")
    artifact = document.evidence_archive["artifacts"]["section_markdown"]
    if tamper == "hash":
        artifact["sha256"] = "forged"
    elif tamper == "outside":
        outside = tmp_path / "private.txt"
        outside.write_text("private")
        artifact.update(path=str(outside), sha256=hashlib.sha256(b"private").hexdigest())
    elif tamper == "missing":
        artifact["path"] = "originals/missing.md"
    else:
        document.evidence_archive["artifacts"] = None
    assert retrieval.archived_excerpt(packet, source, document, 1000) is None


@pytest.mark.parametrize("mode", ["OFF", "BGE"])
def test_real_read_path_uses_archive_without_expanding_budget(tmp_path, monkeypatch, mode):
    # Unit coverage must remain independent of a running local sidecar.
    monkeypatch.setattr("planner_api.research.passage_retrieval.rerank_candidates",
        lambda query, items, settings: (items, {"status": "FALLBACK", "runtime_ms": 0}))
    first = "Alpha operations are allowed only while this session remains open.\n\n"
    last = "Omega operations require separate authorization before any session is closed.\n"
    text = first + "Unrelated navigation and sample material without the requested technical terms.\n\n" * 60 + last
    packet, source, document = archived_packet(tmp_path, monkeypatch, text)

    class Reader:
        def read(self, request):
            return document

    orchestrator = ResearchOrchestrator(Reader(), settings=Settings(_env_file=None, llm_provider="fake", research_rerank_mode=mode),
        limits=ResearchLimits(max_rounds=1, max_read_pages=1, max_pages_per_round=1,
                              max_page_chars=1000, max_total_page_chars=1000))
    assert orchestrator._read_selected(packet) == 1
    assert first in source.content_excerpt and last in source.content_excerpt
    assert len(source.content_excerpt) <= 1000
    assert len(source.evidence_archive["excerpt_windows"]) >= 2


def test_invalid_multirange_mapping_never_falls_back_to_spliced_quote():
    source = ResearchSource(url="https://example.com/manual", canonical_url="https://example.com/manual",
                            read_status="SUCCEEDED", content_excerpt="Alpha.\n\nOmega.",
                            evidence_archive={"excerpt_windows": [{"stored_start": -1, "stored_end": 12,
                                                                      "char_start": 0, "char_end": 13}]})
    assert build_web_passages(ResearchPacket(question="rules", sources=[source]), preserve_markup=True) == []


def test_multiple_windows_use_one_selection_call_and_keep_independent_addresses(tmp_path, monkeypatch):
    text = "### Session rules\n\nAlpha operations require careful coordination before the active session closes.\n\n"
    text += "Unrelated navigation and sample material without requested technical terms.\n\n" * 60
    text += "Omega operations require authorization from an administrator before proceeding.\n"
    packet, source, document = archived_packet(tmp_path, monkeypatch, text)
    source.content_excerpt, source.evidence_archive = retrieval.archived_excerpt(packet, source, document, 250)
    source.read_status = "SUCCEEDED"
    packet.passages = build_web_passages(packet, preserve_markup=True)
    calls = []

    def transport(url, headers, payload, timeout):
        data = json.loads(payload["messages"][1]["content"])
        calls.append(data)
        return {"choices": [{"message": {"content": json.dumps({"selections": [
            {"span_id": s["span_id"], "covers_aspects": ["rules"]} for s in data["spans"]]})}}]}

    settings = Settings(_env_file=None, llm_provider="openai_compatible", llm_api_key=SecretStr("test-key"))
    result = ModelResearchIntentAnalyzer(settings, transport=transport).select_evidence(packet)
    assert len(packet.passages) == 2 and len(calls) == 1
    assert calls[0]["spans"][0]["heading"] == "### Session rules"
    assert len(result.evidence) == 2 and len(result.processed_passage_ids) == 2
    for evidence in result.evidence:
        loc = evidence.locator["archive_range"]
        assert text[loc["char_start"]:loc["char_end"]] == evidence.quote
    packet.selected_evidence = result.evidence
    assert compile_research_context(packet)["selected_evidence"][0]["heading"] == "### Session rules"


def test_requested_method_is_preferred_over_similar_other_api_rule():
    text = "Navigation and examples explaining other unrelated filesystem operations.\n\n" * 50
    text += "Path.exists()\n\nReturn False when the path is missing or inaccessible due to an OSError.\n\n"
    text += "Path.glob()\n\nAll OSError exceptions including PermissionError from scanning directories are suppressed.\n\n"
    windows = retrieval.prose_windows(text, ["Path.exists() PermissionError OSError inaccessible"], 100)
    assert any("Return False" in w.text for w in windows)
    assert all("scanning directories" not in w.text for w in windows)


@pytest.mark.parametrize("query", ["readers writers concurrent one writer", "是否只有一个写者"])
def test_exact_requested_relationship_is_not_crowded_out_by_broad_concurrency_words(query):
    text = "Readers and writers can work concurrently while readers continue using stored pages.\n\n"
    text += "There can only be one writer at a time because the shared log is singular.\n\n"
    text += "Other navigation and background material without these requested technical details.\n\n" * 30
    windows = retrieval.prose_windows(text, [query], 100)
    assert any("one writer at a time" in w.text for w in windows)
