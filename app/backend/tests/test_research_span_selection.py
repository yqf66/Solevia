import json

import pytest
from pydantic import SecretStr, ValidationError

from planner_api.config import Settings
from planner_api.research.context import compile_research_context, estimate_research_units
from planner_api.research.evidence import build_web_passages
from planner_api.research.intent import ModelResearchIntentAnalyzer
from planner_api.research.orchestrator import ResearchOrchestrator
from planner_api.research.schemas import (
    EvidenceSpanChoices, ResearchBrief, ResearchPacket, ResearchSource,
    ResearchSubquery, ResearchQueryConstraints,
)
from planner_api.research.selection import evidence_spans, resolve_span_choices, validated_selected_evidence
from planner_api.research.strategy import assess_research_aspects, refresh_research_coverage


def packet(text, *, snippet=False):
    source = ResearchSource(url="https://docs.python.org/3.14/library/asyncio-task.html",
        canonical_url="https://docs.python.org/3.14/library/asyncio-task.html",
        content_excerpt=text, snippet=text if snippet else "", read_status="FAILED" if snippet else "SUCCEEDED",
        source_category="DOCUMENTATION", matched_queries=["TaskGroup"],
        evidence_archive={"archive_id": "original-html-markdown", "evidence_basis": "raw_markdown"})
    p = ResearchPacket(question="TaskGroup exceptions", status="READY", evidence_mode="SPANS",
        brief=ResearchBrief(required_aspects=["exceptions"]), sources=[source],
        subqueries=[ResearchSubquery(key="Q1", query="TaskGroup", covers_aspects=["exceptions"])])
    p.passages = build_web_passages(p, preserve_markup=True)
    return p


def choose(p, indices=None):
    body = p.passages[0]
    spans = evidence_spans(body)
    choices = EvidenceSpanChoices(selections=[{"span_id": s["span_id"], "covers_aspects": ["exceptions"]}
        for i, s in enumerate(spans) if indices is None or i in indices])
    p.selected_evidence, diagnostics = resolve_span_choices(p, body, choices)
    assert not diagnostics
    return p


def test_original_markdown_negation_version_and_strikethrough_survive_roundtrip():
    text = "## Changed in 3.14\n\n~~Old rule~~\n\nExcept KeyboardInterrupt, exceptions are grouped.\n"
    p = choose(packet(text))
    restored = ResearchPacket.model_validate_json(p.model_dump_json())
    e = validated_selected_evidence(restored)[0]
    assert e.quote == text
    assert e.coverage == "candidate"
    assert e.question_keys == ["Q1"]
    assert e.trust_metadata["evidence_archive"]["archive_id"] == "original-html-markdown"
    assert p.claims == []


@pytest.mark.parametrize("field,value", [
    ("quote", "KeyboardInterrupt is always grouped."),
    ("source_url_or_uri", "https://other.example/"),
    ("context_after", "invented exception"),
])
def test_fabricated_or_misattributed_checkpoint_evidence_is_not_delivered(field, value):
    p = choose(packet("KeyboardInterrupt is re-raised."))
    setattr(p.selected_evidence[0], field, value)
    context = compile_research_context(p)
    assert context["selected_evidence"] == []
    assert "extracted_claims" not in context


def test_changed_passage_hash_or_offset_rejects_stale_evidence():
    p = choose(packet("Exact rule."))
    p.selected_evidence[0].locator["char_start"] = 1
    assert validated_selected_evidence(p) == []


def test_archive_offsets_trace_actual_section_and_checkpoint_cannot_replace_them():
    p = packet("Complete exception rule.")
    archive = {"archive_id": "chapter", "evidence_basis": "section_markdown",
               "excerpt_window": {"char_start": 80, "partial_start": True},
               "artifacts": {"section_markdown": {"sha256": "actual-sha"}}}
    p.sources[0].evidence_archive = archive
    p.passages = build_web_passages(p, preserve_markup=True)
    choose(p)
    e = p.selected_evidence[0]
    loc = e.locator["archive_range"]
    assert loc["char_start"] == 80 and loc["char_end"] == 80 + len(e.quote)
    assert e.locator["contains_fragment"] and e.locator["context_before_truncated"]
    e.locator["archive_range"]["char_start"] = 9999
    checked = validated_selected_evidence(p)[0]
    assert checked.locator["archive_range"]["char_start"] == 80
    assert checked.locator["archive_range"]["artifact_sha256"] == "actual-sha"
    p = choose(packet("Exact rule."))
    p.passages[0].text = "Altered rule."
    assert validated_selected_evidence(p) == []


def test_unknown_addresses_and_aspects_rejected_without_trusting_model_prose():
    p = packet("Exact rule.")
    span_id = evidence_spans(p.passages[0])[0]["span_id"]
    choices = EvidenceSpanChoices(selections=[
        {"span_id": "invented", "covers_aspects": ["exceptions"]},
        {"span_id": span_id, "covers_aspects": ["invented aspect"]}])
    evidence, diagnostics = resolve_span_choices(p, p.passages[0], choices)
    assert not evidence and diagnostics
    with pytest.raises(ValidationError):
        EvidenceSpanChoices.model_validate({"selections": [], "claim": "invented conclusion"})
    with pytest.raises(ValidationError):
        EvidenceSpanChoices.model_validate({})


def test_adjacent_exception_and_previous_paragraph_referent_retained():
    text = ("Normal rule. " * 60 + "\n\nKeyboardInterrupt is a special case; re-raise the original.\n\n"
            + "The same special case as in the previous paragraph applies to body exceptions. " * 10)
    p = packet(text)
    spans = evidence_spans(p.passages[0])
    index = next(i for i, s in enumerate(spans) if "previous paragraph" in s["text"])
    choose(p, [index])
    e = p.selected_evidence[0]
    joined = e.context_before + e.quote + e.context_after
    assert "KeyboardInterrupt is a special case" in joined
    assert joined in text


def test_exact_original_quote_does_not_certify_complete_answer_or_snippet():
    for snippet in (False, True):
        p = choose(packet("KeyboardInterrupt is re-raised.", snippet=snippet))
        refresh_research_coverage(p)
        assessment = assess_research_aspects(p)[0]
        assert assessment["answerability"] == "PARTIAL"
        assert assessment["evidence_strength"] == ("WEAK" if snippet else "CANDIDATE")
        assert p.coverage.answerable_aspects == [] and p.status == "PARTIAL"
        assert compile_research_context(p)["selected_evidence"][0]["evidence_level"] == (
            "SEARCH_SNIPPET" if snippet else "FULL_TEXT")


def test_requested_version_mismatch_never_counts_as_body_candidate():
    p = choose(packet("Exact rule."))
    p.subqueries[0].constraints = ResearchQueryConstraints(specifications=["Python 3.13"])
    assert assess_research_aspects(p)[0]["candidate_evidence_count"] == 0
    assert compile_research_context(p)["selected_evidence"][0]["requested_version_matches"] is False


def test_budget_omits_whole_window_instead_of_truncating_conditions():
    text = "Conditional rule. " * 450 + "Unless cancelled, this rule applies."
    p = choose(packet(text))
    context = compile_research_context(p, max_chars=2600, max_units=1000)
    assert context["selected_evidence"] == []
    assert context["_context_manifest"]["omissions"]["WHOLE_EVIDENCE_WINDOW"] == len(p.selected_evidence)
    assert len(json.dumps(context, ensure_ascii=False, separators=(",", ":"))) <= 2600
    assert estimate_research_units(context) <= 1000
    assert context["research_status"] == "PARTIAL"


def test_long_selected_page_does_not_collapse_into_one_unusable_window():
    text = "A relevant rule with conditions. " * 230
    p = choose(packet(text))
    assert len(p.selected_evidence) > 1
    assert all(len(e.quote) + len(e.context_before) + len(e.context_after) <= 3200 for e in p.selected_evidence)
    context = compile_research_context(p, max_chars=6000, max_units=2500)
    assert context["selected_evidence"]
    assert all(e["context_before"] + e["quote"] + e["context_after"] in text for e in context["selected_evidence"])


def test_budget_retains_distinct_required_aspect_before_duplicate_official_windows():
    p = choose(packet("A relevant official exception rule. " * 190))
    p.brief.required_aspects.append("experience")
    p.sources.append(ResearchSource(url="https://engineer.example/report", canonical_url="https://engineer.example/report",
        content_excerpt="In my test, the workload was cancelled.", read_status="SUCCEEDED", ranking_score=-10))
    body = build_web_passages(p, preserve_markup=True)[-1]
    p.passages.append(body)
    selected, diagnostics = resolve_span_choices(p, body, EvidenceSpanChoices(selections=[
        {"span_id": evidence_spans(body)[0]["span_id"], "covers_aspects": ["experience"]}]))
    assert not diagnostics
    p.selected_evidence.extend(selected)
    context = compile_research_context(p, max_chars=7000, max_units=3000)
    assert {a for e in context["selected_evidence"] for a in e["covers_aspects"]} == {"exceptions", "experience"}


def test_overlabelled_community_candidate_cannot_evict_requested_official_rules():
    p = packet("Official rule: readers do not block writers. " * 25)
    p.brief.required_aspects.append("experience")
    p.subqueries[0].query_role = "AUTHORITATIVE"
    p.subqueries.append(ResearchSubquery(key="Q2", query="field report", query_role="COMMUNITY",
                                        covers_aspects=["experience"]))
    choose(p)
    p.sources.append(ResearchSource(url="https://engineer.example/report", canonical_url="https://engineer.example/report",
        content_excerpt="In this workload, checkpoint left the WAL file allocated. " * 16,
        read_status="SUCCEEDED", ranking_score=100, source_category="OTHER"))
    body = build_web_passages(p, preserve_markup=True)[-1]
    p.passages.append(body)
    selected, _ = resolve_span_choices(p, body, EvidenceSpanChoices(selections=[
        {"span_id": evidence_spans(body)[0]["span_id"], "covers_aspects": ["exceptions", "experience"]}]))
    p.selected_evidence.extend(selected)
    context = compile_research_context(p, max_chars=6500, max_units=3000)
    assert {e["source_url"] for e in context["selected_evidence"]} == {
        str(p.sources[0].url), "https://engineer.example/report"}


@pytest.mark.parametrize("fail_review_once", [False, True])
def test_production_orchestrator_uses_selector_and_never_fact_extractor(fail_review_once):
    captured = []
    def transport(url, headers, payload, timeout):
        submitted = json.loads(payload["messages"][1]["content"])
        captured.append(submitted)
        if "evidence" in submitted:
            if fail_review_once and sum("evidence" in call for call in captured) == 1:
                raise RuntimeError("transient review transport failure")
            choices = {"verdicts": [{"aspect": "exceptions", "status": "SUPPORTED",
                "evidence_ids": [submitted["evidence"][0]["evidence_id"]],
                "reason": "完整原文明确规定该异常重新抛出", "missing_conditions": []}]}
        else:
            choices = {"selections": [{"span_id": submitted["spans"][0]["span_id"], "covers_aspects": ["exceptions"]}]}
        return {"choices": [{"message": {"content": json.dumps(choices)}}]}
    settings = Settings(_env_file=None, llm_provider="openai_compatible", llm_api_key=SecretStr("test-key"))
    analyzer = ModelResearchIntentAnalyzer(settings, transport=transport)
    analyzer.extract_evidence = lambda p: pytest.fail("Generated-fact path was invoked")
    engine = ResearchOrchestrator(settings=settings, intent_analyzer=analyzer)
    p = packet("## Rule\n\nKeyboardInterrupt is re-raised.")
    p.passages = []
    engine._extract_evidence(p)
    engine._extract_evidence(p)
    assert len(captured) == (3 if fail_review_once else 2)
    assert assess_research_aspects(p)[0]["answerability"] == "ANSWERED"
    assert p.selected_evidence and not p.claims
    assert "\n\n" in p.selected_evidence[0].quote
    assert p.coverage.answerable_aspects == ["exceptions"]


@pytest.mark.parametrize("failure", ["missing_id", "missing_condition", "wrong_aspect", "fragment", "changed_body", "conflict"])
def test_independent_span_review_cannot_certify_missing_or_changed_evidence(failure):
    from planner_api.research.schemas import AspectEvidenceReview

    p = choose(packet("The complete exception rule applies only under this condition."))
    e = p.selected_evidence[0]
    verdict = {"aspect": "exceptions", "status": "SUPPORTED", "evidence_ids": [e.evidence_id],
               "reason": "原文在完整条件下支持", "missing_conditions": []}
    if failure == "missing_id": verdict["evidence_ids"] = ["unknown"]
    if failure == "missing_condition": verdict["missing_conditions"] = ["触发条件不清楚"]
    if failure == "wrong_aspect": verdict["aspect"] = "unasked"
    if failure == "fragment": p.passages[0].locator["partial_start"] = True
    if failure == "changed_body": p.passages[0].text = "A different version."
    if failure == "conflict": verdict["status"] = "CONFLICTED"
    p.aspect_evidence_review = AspectEvidenceReview(verdicts=[verdict])
    if failure == "fragment":
        # Truncated original archive boundaries are rebuilt from trusted data.
        p.passages[0].trust_metadata["evidence_archive"]["excerpt_window"] = {"partial_start": True, "char_start": 30}
        p.passages[0].trust_metadata["evidence_archive"]["evidence_basis"] = "section_markdown"
    assert assess_research_aspects(p)[0]["answerability"] != "ANSWERED"


def test_failed_or_invalid_selector_output_is_retryable():
    calls = []
    def transport(url, headers, payload, timeout):
        calls.append(payload)
        return {"choices": [{"message": {"content": '{"selections":[{"span_id":"wrong"}]}'}}]}
    settings = Settings(_env_file=None, llm_provider="openai_compatible", llm_api_key=SecretStr("test-key"))
    engine = ResearchOrchestrator(settings=settings, intent_analyzer=ModelResearchIntentAnalyzer(settings, transport=transport))
    p = packet("Exact rule.")
    engine._extract_evidence(p)
    engine._extract_evidence(p)
    assert len(calls) == 2 and not p.extracted_passage_ids
    assert not p.selected_evidence and p.uncertain_points


def test_historical_flattened_passages_are_reselected_from_retained_markdown():
    def transport(url, headers, payload, timeout):
        data = json.loads(payload["messages"][1]["content"])
        return {"choices": [{"message": {"content": json.dumps({"selections": [
            {"span_id": data["spans"][0]["span_id"], "covers_aspects": ["exceptions"]}]})}}]}
    settings = Settings(_env_file=None, llm_provider="openai_compatible", llm_api_key=SecretStr("test-key"))
    engine = ResearchOrchestrator(settings=settings, intent_analyzer=ModelResearchIntentAnalyzer(settings, transport=transport))
    p = packet("## Rule\n\nKeyboardInterrupt is re-raised.")
    p.evidence_mode = "CLAIMS"
    p.passages = build_web_passages(p)
    p.extracted_passage_ids = [p.passages[0].passage_id]
    engine._extract_evidence(p)
    assert len(p.passages) == 1
    assert "\n\n" in p.selected_evidence[0].quote
    assert p.passages[0].locator["offset_basis"] == "stored_raw_markdown_excerpt"
