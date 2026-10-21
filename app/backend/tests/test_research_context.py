import pytest

from planner_api.research.context import compile_research_context, estimate_research_units
from planner_api.research.memory import refresh_research_memory
from planner_api.research.schemas import (
    EvidenceLink,
    EvidencePassage,
    ResearchBrief,
    ResearchClaim,
    ResearchDecision,
    ResearchGap,
    ResearchPacket,
    ResearchSource,
    ResearchStage,
    ResearchStrategyAttempt,
    ResearchSubquery,
)


def _packet() -> ResearchPacket:
    passages = [
        EvidencePassage(
            passage_id="web:route",
            source_url_or_uri="https://route.example",
            text="路线必须先完成基础阶段，再进入高级阶段。",
            content_hash="route1234",
            trust_metadata={"source_cluster_id": "route.example"},
        ),
        EvidencePassage(
            passage_id="web:execution",
            source_url_or_uri="https://execution.example",
            text="当前版本存在执行阻塞，需要先修复接口。",
            content_hash="exec1234",
            trust_metadata={"source_cluster_id": "execution.example"},
        ),
    ]
    return ResearchPacket(
        question="制定路线并处理当前执行阻塞",
        passages=passages,
        claims=[
            ResearchClaim(
                claim="必须先完成基础阶段",
                claim_id="claim:route",
                confidence=0.8,
                source_urls=["https://route.example"],
                evidence_links=[
                    EvidenceLink(
                        passage_id="web:route",
                        quoted_text="路线必须先完成基础阶段",
                    )
                ],
            ),
            ResearchClaim(
                claim="当前版本存在执行阻塞",
                claim_id="claim:execution",
                confidence=0.8,
                source_urls=["https://execution.example"],
                evidence_links=[
                    EvidenceLink(
                        passage_id="web:execution",
                        quoted_text="当前版本存在执行阻塞",
                    )
                ],
            ),
        ],
    )


def test_context_compiler_selects_stage_relevant_claims_and_preserves_links() -> None:
    packet = _packet()

    route_context = compile_research_context(packet, phase="ROUTE", max_chars=5000)
    execution_context = compile_research_context(packet, phase="EXECUTION", max_chars=5000)

    assert route_context["research_phase"] == "ROUTE"
    assert route_context["extracted_claims"][0]["claim_id"] == "claim:route"
    assert route_context["extracted_claims"][0]["evidence_links"][0]["passage_id"] == "web:route"
    assert execution_context["extracted_claims"][0]["claim_id"] == "claim:execution"


def test_context_compiler_omits_already_sent_passage_text() -> None:
    packet = _packet()

    context = compile_research_context(
        packet,
        phase="PLAN",
        max_chars=5000,
        already_sent_passage_ids=["web:route"],
    )

    sent = next(item for item in context["evidence_passages"] if item["passage_id"] == "web:route")
    assert sent["text"] == ""
    assert sent["already_sent"] is True
    assert "web:execution" in context["_context_manifest"]["sent_passage_ids"]


def test_bounded_context_keeps_specific_rule_alongside_related_overviews() -> None:
    rules = [f"TaskGroup exception rule {index}: sibling tasks cancel." for index in range(7)]
    rules.append("TaskGroup KeyboardInterrupt or SystemExit cancels and waits, then re-raises the original exception.")
    original = " ".join(rules) + " unrelated metadata " * 500
    packet = ResearchPacket(
        question="TaskGroup exception cancellation rules",
        passages=[EvidencePassage(passage_id="p1", source_url_or_uri="https://docs.example/api",
                                  text=original, content_hash="12345678")],
        sources=[ResearchSource(url="https://docs.example/api", canonical_url="https://docs.example/api",
                                read_status="SUCCEEDED", content_excerpt=original)],
        claims=[ResearchClaim(claim=rule, claim_id=f"rule:{index}", confidence=0.9,
                              status="SUPPORTED", evidence_level="DIRECT_EVIDENCE",
                              covers_aspects=["exception rules"], source_urls=["https://docs.example/api"],
                              evidence_links=[EvidenceLink(passage_id="p1", quoted_text=rule)])
                for index, rule in enumerate(rules)],
    )
    context = compile_research_context(packet, focus=packet.question, max_chars=12000)
    assert len(context["extracted_claims"]) == 8
    assert any("re-raises the original" in claim["claim"] for claim in context["extracted_claims"])
    assert context["_context_manifest"]["compiled_chars"] <= 12000


def test_repeated_source_links_cannot_displace_verified_exception_rule():
    packet = _packet()
    packet.question = "TaskGroup exception cancellation rules"
    packet.claims = [ResearchClaim(
        claim=f"TaskGroup exception cancellation 可核查的官方链接为 https://docs.example/{index}",
        confidence=0.9, status="SUPPORTED", evidence_level="DIRECT_EVIDENCE",
        covers_aspects=["rules"], source_urls=["https://docs.example/api"],
        evidence_links=[EvidenceLink(passage_id="web:route", quoted_text="source link")])
        for index in range(14)]
    special = "If KeyboardInterrupt or SystemExit fails, cancel and await siblings, then re-raise the original exception."
    packet.claims.append(ResearchClaim(claim=special, confidence=0.9, status="SUPPORTED",
                                      evidence_level="DIRECT_EVIDENCE", covers_aspects=["rules"],
                                      source_urls=["https://docs.example/api"],
                                      evidence_links=[EvidenceLink(passage_id="web:route", quoted_text=special)]))
    context = compile_research_context(packet, focus=packet.question)
    assert context["extracted_claims"][0]["claim"] == special
    assert context["_context_manifest"]["compiled_chars"] <= 12000


def test_context_compiler_reduces_long_material_with_character_and_unit_budgets() -> None:
    packet = _packet()
    packet.passages[0].text = "路线约束：" + ("必须先完成基础阶段。" * 500)
    context = compile_research_context(packet, max_chars=2200, max_units=1100)

    manifest = context["_context_manifest"]
    assert manifest["compiled_chars"] <= 2200
    assert manifest["estimated_input_units"] <= 1100
    assert manifest["omissions"]
    assert estimate_research_units(context) <= 1100


def test_research_memory_is_bounded_and_idempotent() -> None:
    packet = _packet()
    packet.coverage.rounds = 1
    packet.coverage.covered_aspects = ["路线"]
    packet.coverage.uncovered_aspects = ["执行"]
    packet.uncertain_points = ["仍需确认当前版本"]

    refresh_research_memory(packet, new_passage_ids=["web:route"], new_claim_ids=["claim:route"])
    refresh_research_memory(packet, new_passage_ids=["web:route"], new_claim_ids=["claim:route"])

    assert len(packet.memory.recent_rounds) == 1
    assert packet.memory.recent_rounds[0].new_passage_ids == ["web:route"]
    assert packet.memory.unresolved_points == ["执行", "仍需确认当前版本"]


def test_context_exposes_stage_mode_and_verification_separately() -> None:
    packet = _packet()
    packet.decision = ResearchDecision(
        should_search=True,
        research_stages=[
            ResearchStage(
                key="synthesis",
                mode="SYNTHESIS",
                verification_status="PARTIALLY_VERIFIED",
                status="COMPLETE",
            )
        ],
    )

    context = compile_research_context(packet, phase="PLAN", max_chars=5000)
    stage = context["research_plan"]["stages"][0]

    assert stage["mode"] == "SYNTHESIS"
    assert stage["status"] == "COMPLETE"
    assert stage["verification_status"] == "PARTIALLY_VERIFIED"


def test_context_exposes_aspect_strength_gaps_and_strategy_attempts() -> None:
    packet = ResearchPacket(
        question="Agent 岗位作品集要求",
        brief=ResearchBrief(required_aspects=["官方要求", "社区经验"]),
        subqueries=[
            ResearchSubquery(
                key="official",
                query="Agent 官方岗位要求",
                covers_aspects=["官方要求"],
            )
        ],
        sources=[
            ResearchSource(
                title="官方岗位",
                url="https://jobs.example/agent",
                canonical_url="https://jobs.example/agent",
                matched_queries=["Agent 官方岗位要求"],
                read_status="SUCCEEDED",
                source_cluster_id="jobs.example",
            )
        ],
        claims=[
            ResearchClaim(
                claim="岗位要求项目落地经验。",
                source_urls=["https://jobs.example/agent"],
                confidence=0.9,
                status="SUPPORTED",
            )
        ],
        gaps=[
            ResearchGap(
                aspect="社区经验",
                gap_type="MISSING_EVIDENCE",
                severity="HIGH",
            )
        ],
        memory={
            "strategy_attempts": [
                ResearchStrategyAttempt(
                    signature="strategy12345678",
                    round_index=1,
                    action_kind="SWITCH_SOURCE",
                    aspect="官方要求",
                    question_key="adaptive-one",
                    query="Agent 官方岗位要求",
                    outcome="EVIDENCE_READY",
                )
            ]
        },
    )

    context = compile_research_context(packet, phase="PLAN", max_chars=5000)

    assert [item["evidence_strength"] for item in context["aspect_assessments"]] == [
        "WEAK",
        "MISSING",
    ]
    assert any(gap["aspect"] == "社区经验" and gap["gap_type"] == "MISSING_EVIDENCE"
               for gap in context["research_gaps"])
    assert context["research_memory"]["strategy_attempts"][0]["outcome"] == (
        "EVIDENCE_READY"
    )


@pytest.mark.parametrize("budget", [2200, 5000, 12000])
def test_compaction_keeps_complete_rule_quote_and_all_conditions(budget):
    quote = (
        "If any task fails with KeyboardInterrupt or SystemExit, the task group still cancels "
        "the remaining tasks and waits for them, but then the initial KeyboardInterrupt or "
        "SystemExit is re-raised instead of ExceptionGroup or BaseExceptionGroup."
    )
    conditions = ["限定条件 " + str(i) for i in range(5)] + ["KeyboardInterrupt 不归入异常组"]
    packet = ResearchPacket(question="TaskGroup KeyboardInterrupt rule", claims=[ResearchClaim(
        claim="任务组取消并等待其他任务，随后重抛 KeyboardInterrupt，而非归组。", confidence=0.9,
        status="SUPPORTED", evidence_level="DIRECT_EVIDENCE", conditions=conditions,
        source_urls=["https://docs.python.org/3.14/library/asyncio-task.html"],
        evidence_links=[EvidenceLink(passage_id="rule", quoted_text=quote)],
    )], passages=[EvidencePassage(passage_id="rule", text=quote + " background " * 300,
        content_hash="12345678", source_url_or_uri="https://docs.python.org/3.14/library/asyncio-task.html")])

    context = compile_research_context(packet, max_chars=budget)

    claim = context["extracted_claims"][0]
    assert claim["evidence_links"][0]["quoted_text"] == quote
    assert claim["conditions"] == conditions
    assert context["_context_manifest"]["compiled_chars"] <= budget
    assert packet.claims[0].conditions == conditions


def test_tiny_budget_omits_whole_fact_instead_of_a_supported_half_quote():
    quote = "When this condition holds, " + "a required qualifier remains in force; " * 44 + "the action is prohibited."
    packet = ResearchPacket(question="Check the conditional rule", claims=[ResearchClaim(
        claim="The action is prohibited under the stated conditions.", confidence=0.9,
        status="SUPPORTED", evidence_level="DIRECT_EVIDENCE",
        evidence_links=[EvidenceLink(passage_id="rule", quoted_text=quote)],
    )])

    context = compile_research_context(packet, max_chars=1200)

    assert context["extracted_claims"] == []
    assert context["_context_manifest"]["omissions"]["WHOLE_CLAIM_AND_EVIDENCE"] == 1
    assert "不代表当前上下文保留了全部证据" in context["source_handling_rule"]
    assert context["_context_manifest"]["compiled_chars"] <= 1200


def test_source_identity_distinguishes_community_from_official_material():
    packet = ResearchPacket(question="Compare a user report with official rules", sources=[
        ResearchSource(url="https://forum.example/topic", canonical_url="https://forum.example/topic",
                       source_category="COMMUNITY", read_status="SUCCEEDED", content_excerpt="A user report.")
    ])

    source = compile_research_context(packet)["untrusted_external_sources"][0]

    assert source["source_category"] == "COMMUNITY"
    assert source["read_status"] == "SUCCEEDED"


def test_unrelated_search_snippets_do_not_enter_a_scoped_answer_context():
    packet = ResearchPacket(question="Check Path.exists permission errors", brief=ResearchBrief(
        required_aspects=["Path.exists under permission errors"]), sources=[ResearchSource(
        url="https://blog.example/unrelated", canonical_url="https://blog.example/unrelated",
        snippet="Python installation advice, unrelated to the requested rule.", source_category="COMMUNITY",
    )], claims=[ResearchClaim(claim="Python installation advice.", confidence=0.25,
        source_urls=["https://blog.example/unrelated"], covers_aspects=[], evidence_level="SEARCH_SNIPPET")])

    context = compile_research_context(packet)

    assert context["extracted_claims"] == []
    assert context["untrusted_external_sources"] == []
    assert context["research_coverage"]["uncovered_aspects"] == ["Path.exists under permission errors"]
    assert packet.sources and packet.claims


def test_referential_rule_keeps_its_actual_preceding_exception():
    special = "KeyboardInterrupt is re-raised instead of an exception group."
    rule = "For the statement body, the same special case applies as in the previous paragraph."
    body = "Unrelated background. " + special + " " + rule
    packet = ResearchPacket(question="What happens to a statement body KeyboardInterrupt?",
        claims=[ResearchClaim(claim="The body follows the same special case.", confidence=0.9,
            evidence_links=[EvidenceLink(passage_id="rule", quoted_text=rule)])],
        passages=[EvidencePassage(passage_id="rule", text=body, content_hash="12345678",
            source_url_or_uri="https://docs.example/rule")])

    context = compile_research_context(packet, max_chars=2200)

    quote = context["extracted_claims"][0]["evidence_links"][0]["quoted_text"]
    assert special in quote and rule in quote
    assert quote in body
    assert packet.claims[0].evidence_links[0].quoted_text == rule
