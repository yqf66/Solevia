import pytest

from planner_api.config import Settings
from planner_api.research.context import compile_research_context
from planner_api.research.evidence import build_web_passages, validate_evidence_analysis
from planner_api.research.orchestrator import ResearchOrchestrator
from planner_api.research.schemas import (
    EvidenceLink, PageDocument, ResearchBrief, ResearchClaim, ResearchConflict, ResearchEvidenceAnalysis, ResearchGap,
    ResearchPacket, ResearchQueryConstraints, ResearchReflection, ResearchSource, ResearchSubquery,
)
from planner_api.research.strategy import assess_research_aspects, refresh_research_coverage


def packet_fixture(*, complete=True, text="TaskGroup cancels and awaits remaining tasks after a child fails.", url=None):
    url = url or "https://docs.python.org/3.14/library/asyncio-task.html"
    aspect = "TaskGroup cancellation rules"
    query = ResearchSubquery(key="tasks", query="Python TaskGroup cancellation", query_intent="TaskGroup cancellation rules",
        covers_aspects=[aspect], status="EVIDENCE_READY", purpose="DOCUMENTATION",
        constraints=ResearchQueryConstraints(specifications=["3.14"]))
    source = ResearchSource(url=url, canonical_url=url, title="TaskGroup", read_status="SUCCEEDED",
        content_excerpt=text, matched_queries=[query.query], source_profiles=["official_docs"],
        source_category="DOCUMENTATION", page_quality_flags=["EXCERPT_TRUNCATED"])
    packet = ResearchPacket(question="Python 3.14 TaskGroup cancellation rules", status="READY",
        brief=ResearchBrief(required_aspects=[aspect]), sources=[source], subqueries=[query])
    packet.passages = build_web_passages(packet)
    packet.claims = [ResearchClaim(claim=text, covers_aspects=[aspect], answers_aspects=[aspect] if complete else [],
        confidence=0.9, status="SUPPORTED", evidence_level="DIRECT_EVIDENCE", source_urls=[url],
        evidence_links=[EvidenceLink(passage_id=packet.passages[0].passage_id, quoted_text=text)])]
    packet.coverage.read_pages = 1
    packet.coverage.successful_queries = 1
    return packet


@pytest.mark.parametrize("mode", ["OFF", "BGE"])
def test_related_supported_fact_is_not_a_complete_answer_in_either_path(mode):
    packet = packet_fixture(complete=False)
    engine = ResearchOrchestrator(settings=Settings(_env_file=None, research_rerank_mode=mode))
    engine._update_aspect_coverage(packet)
    assert packet.status == "PARTIAL" and not packet.coverage.answerable_aspects
    assert packet.claims[0].status == "SUPPORTED"  # preserve the usable partial fact


def test_complete_earlier_rule_survives_truncation_elsewhere_on_page():
    packet = packet_fixture()
    packet.passages[0].text += " Another unrelated paragraph is unfinished"
    refresh_research_coverage(packet)
    assert packet.status == "READY" and packet.coverage.answerable_aspects


def test_compaction_retains_normal_and_failure_rules_before_related_overviews():
    normal = "TaskGroup allows adding tasks while normally waiting for completion."
    failure = "When a TaskGroup task fails other than CancelledError, no further tasks can be added."
    body = normal + " " + failure
    packet = packet_fixture(text=body)
    aspect = packet.brief.required_aspects[0]
    passage_id = packet.passages[0].passage_id
    original = packet.claims[0]
    packet.claims = [original.model_copy(update={
        "claim_id": f"answer:{index}", "claim": rule,
        "evidence_links": [EvidenceLink(passage_id=passage_id, quoted_text=rule)],
    }) for index, rule in enumerate([normal, failure])]
    # Many relevant, highly ranked exception overviews previously displaced
    # the failure rule even though they did not answer the requested behavior.
    packet.claims.extend(original.model_copy(update={
        "claim_id": f"overview:{index}",
        "claim": f"TaskGroup special exception rule {index}: re-raise exceptions.",
        "answers_aspects": [],
    }) for index in range(18))

    context = compile_research_context(packet, focus=packet.question, max_chars=5000)

    retained = {claim["claim"] for claim in context["extracted_claims"]}
    assert {normal, failure} <= retained
    assert context["research_status"] == "READY"
    assert context["research_coverage"]["answerable_aspects"] == [aspect]
    assert context["_context_manifest"]["compiled_chars"] <= 5000


def test_quoted_unfinished_tail_cannot_close_aspect_even_with_complete_proposal():
    packet = packet_fixture(text="TaskGroup cancels remaining tasks and")
    analysis, _ = validate_evidence_analysis(ResearchEvidenceAnalysis(claims=packet.claims),
        passages=packet.passages, sources=packet.sources)
    assert analysis.claims and not analysis.claims[0].answers_aspects
    refresh_research_coverage(packet)
    assert packet.status == "PARTIAL"


@pytest.mark.parametrize("bad", ["snippet", "failed_read", "bad_quote", "wrong_version", "moving_version", "inferred"])
def test_labels_cannot_override_invalid_body_support(bad):
    packet = packet_fixture()
    if bad == "snippet":
        packet.passages[0].trust_metadata["is_search_snippet"] = True
    elif bad == "failed_read":
        packet.sources[0].read_status = "FAILED"
        packet.sources[0].snippet = "TaskGroup source summary"
    elif bad == "bad_quote":
        packet.claims[0].evidence_links[0].quoted_text = "invented cancellation rule"
    elif bad == "wrong_version":
        packet = packet_fixture(url="https://docs.python.org/3.12/library/asyncio-task.html")
    elif bad == "moving_version":
        packet = packet_fixture(url="https://docs.python.org/3/library/asyncio-task.html")
    else:
        packet.claims[0].evidence_level = "INFERRED_SUMMARY"
    refresh_research_coverage(packet)
    assert packet.status == "PARTIAL" and not packet.coverage.answerable_aspects


@pytest.mark.parametrize("mode", ["OFF", "BGE"])
def test_reflection_complete_with_body_only_gap_proposes_bounded_followup(mode):
    class Analyzer:
        def reflect(self, packet):
            return ResearchReflection(complete=True, answered_question_keys=["tasks"])

    packet = packet_fixture(complete=False)
    packet.coverage.summary_only_sources = 0
    engine = ResearchOrchestrator(settings=Settings(_env_file=None, research_rerank_mode=mode),
                                  intent_analyzer=Analyzer())
    complete, followups = engine._reflect(packet, None)
    assert not complete and followups and packet.gaps
    assert packet.coverage.uncovered_aspects == packet.brief.required_aspects


def test_old_ready_is_reassessed_without_mutating_saved_packet():
    packet = packet_fixture(complete=False)
    packet.coverage.covered_aspects = list(packet.brief.required_aspects)
    packet.coverage.answerable_aspects = list(packet.brief.required_aspects)
    context = compile_research_context(packet)
    assert packet.status == "READY"
    assert context["research_status"] == "PARTIAL"
    assert not context["research_coverage"]["answerable_aspects"]
    assert context["research_coverage"]["uncovered_aspects"] == packet.brief.required_aspects


def test_budget_compaction_preserves_gap_and_partial_answer_boundary():
    packet = packet_fixture(complete=False)
    packet.sources[0].snippet = "irrelevant candidate text " * 50
    packet.uncertain_points = ["missing essential exception detail " * 20] * 10
    context = compile_research_context(packet, max_chars=2200, max_units=1100)
    assert context["research_status"] == "PARTIAL"
    assert context["research_coverage"]["uncovered_aspects"] == packet.brief.required_aspects
    assert "缺口" in context["source_handling_rule"]


def test_ready_packet_does_not_certify_a_context_that_omitted_its_rule():
    packet = packet_fixture()
    quote = "The rule holds when " + "the required condition is met; " * 56 + "no exception applies."
    packet.passages[0].text = quote
    packet.claims[0].evidence_links[0].quoted_text = quote
    packet.sources[0].content_excerpt = quote
    refresh_research_coverage(packet)
    assert packet.status == "READY"

    context = compile_research_context(packet, max_chars=1200)

    assert packet.status == "READY"
    assert context["research_status"] == "PARTIAL"
    assert context["research_coverage"]["uncovered_aspects"] == packet.coverage.answerable_aspects
    assert context["extracted_claims"] == []


def test_completed_aspect_does_not_hide_second_missing_aspect():
    packet = packet_fixture()
    packet.brief.required_aspects.append("KeyboardInterrupt in ExceptionGroup")
    packet.subqueries.append(ResearchSubquery(query="TaskGroup KeyboardInterrupt ExceptionGroup",
        covers_aspects=[packet.brief.required_aspects[1]]))
    refresh_research_coverage(packet)
    assert [row["answerability"] for row in assess_research_aspects(packet)] == ["ANSWERED", "UNANSWERED"]
    assert packet.status == "PARTIAL" and len(packet.coverage.answerable_aspects) == 1


@pytest.mark.parametrize("serialized", [False, True])
def test_unresolved_conflict_prevents_answered_aspect_and_completion(serialized):
    packet = packet_fixture()
    link = packet.claims[0].evidence_links[0]
    packet.conflicts = [ResearchConflict(topic="TaskGroup conflicting rule", positions=[
        {"evidence_links": [link.model_dump() if serialized else link]},
    ])]
    refresh_research_coverage(packet)
    assert packet.status == "PARTIAL"
    assert not packet.coverage.answerable_aspects
    assert assess_research_aspects(packet)[0]["evidence_strength"] == "CONFLICTED"


def test_old_ready_can_retry_preserving_checkpoint_and_recovery_limit(session_factory):
    from fastapi import HTTPException
    from planner_api.models import Goal, ResearchRun
    from planner_api.research.persistence import ResearchRunService

    with session_factory() as session:
        goal = Goal(title="历史研究", raw_input="补查")
        session.add(goal)
        session.flush()
        packet = packet_fixture(complete=False).model_dump(mode="json")
        row = ResearchRun(goal_id=goal.id, question="补查", status="READY",
                          result_json=packet, checkpoint_json=packet)
        session.add(row)
        session.commit()
        service = ResearchRunService(session)
        assert service.get(goal.id, row.id).status == "PARTIAL"
        assert row.status == "READY"  # Read projection does not rewrite history.
        retried = service.retry(goal.id, row.id)
        assert retried.status == "QUEUED" and retried.checkpoint == packet
        row.status = "READY"
        row.recovery_count = service.max_recovery_attempts
        session.commit()
        with pytest.raises(HTTPException, match="Research recovery limit reached"):
            service.retry(goal.id, row.id)


def test_verified_ready_still_rejects_research_retry(session_factory):
    from fastapi import HTTPException
    from planner_api.models import Goal, ResearchRun
    from planner_api.research.persistence import ResearchRunService

    with session_factory() as session:
        goal = Goal(title="完整研究", raw_input="已核实")
        session.add(goal)
        session.flush()
        row = ResearchRun(goal_id=goal.id, question="已核实", status="READY",
                          result_json=packet_fixture().model_dump(mode="json"))
        session.add(row)
        session.commit()
        service = ResearchRunService(session)
        assert service.get(goal.id, row.id).status == "READY"
        with pytest.raises(HTTPException) as error:
            service.retry(goal.id, row.id)
        assert error.value.status_code == 409 and row.recovery_count == 0


@pytest.mark.parametrize("mode", ["OFF", "BGE"])
@pytest.mark.parametrize("focused", [False, True])
@pytest.mark.parametrize("focus", ["Python TaskGroup exception rules", "Python 3.14 TaskGroup中一个子任务报错时其他任务如何处理，KeyboardInterrupt是否进入ExceptionGroup"])
def test_truncated_api_page_uses_bounded_section_read_or_preserves_original(mode, focused, focus):
    requests = []

    class Reader:
        def read(self, request):
            requests.append(request)
            return PageDocument(url=request.url, finalUrl=request.url, title="TaskGroup docs",
                content=("TaskGroup async tasks official API documentation. " * 35)[:request.max_chars],
                truncated=True, qualityFlags=["SECTION_FOCUSED"] if focused and request.render_mode == "browser" else [])

    engine = ResearchOrchestrator(service=Reader(), settings=Settings(_env_file=None, research_rerank_mode=mode))
    document, _, used_browser = engine._read_page("https://docs.python.org/3.14/library/asyncio-task.html",
        max_chars=1000, focus=focus)
    assert [r.render_mode for r in requests] == ["request", "browser"]
    assert all(r.max_chars == 1000 and r.focus == focus for r in requests)
    assert used_browser is focused
    assert ("SECTION_FOCUSED" in document.quality_flags) is focused


def test_reference_source_is_retained_when_search_only_returns_weak_snippet():
    packet = packet_fixture()
    source = packet.sources[0]
    source.read_status = "NOT_SELECTED"
    source.ranking_reasons = ["MODEL_REFERENCE_CANDIDATE", "SNIPPET_FALLBACK"]
    ResearchOrchestrator()._merge_sources(packet, [])
    assert packet.sources == [source]


def test_stale_evidence_cannot_be_promoted_to_ready():
    packet = packet_fixture()
    packet.gaps = [ResearchGap(aspect=packet.brief.required_aspects[0], gap_type="STALE_EVIDENCE",
                               reason="所需当前规则尚待核查")]
    refresh_research_coverage(packet)
    assert packet.status == "PARTIAL"


def test_shared_official_reference_retains_all_matching_queries():
    packet = ResearchPacket(question="SQLite WAL official rule", subqueries=[
        ResearchSubquery(query="SQLite WAL readers writers", source_profiles=["official_docs"]),
        ResearchSubquery(query="SQLite WAL official concurrency", source_profiles=["official_docs"]),
    ])
    ResearchOrchestrator._seed_reference_sources(packet, extended=True)
    assert len(packet.sources) == 1
    assert packet.sources[0].matched_queries == [q.query for q in packet.subqueries]
