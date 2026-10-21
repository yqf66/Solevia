from planner_api.research.schemas import (
    EvidencePassage,
    ResearchAction,
    ResearchBrief,
    ResearchClaim,
    ResearchCoverage,
    EvidenceLink,
    ResearchGap,
    ResearchPacket,
    ResearchRoundMemory,
    ResearchSource,
    ResearchSubquery,
)
from planner_api.research.strategy import (
    actions_from_legacy_followups,
    assess_research_aspects,
    diagnose_research_gaps,
    prepare_strategy_followups,
    update_low_yield_state,
)


def _packet() -> ResearchPacket:
    return ResearchPacket(
        question="Agent 岗位作品集要求",
        brief=ResearchBrief(required_aspects=["官方要求", "社区经验"]),
        subqueries=[
            ResearchSubquery(
                key="official",
                query="Agent 岗位官方要求",
                covers_aspects=["官方要求"],
            )
        ],
        sources=[
            ResearchSource(
                title="社区解释",
                url="https://example.com/community",
                canonical_url="https://example.com/community",
                source_profiles=["community"],
                matched_queries=["Agent 岗位官方要求"],
                read_status="SUCCEEDED",
                source_cluster_id="example-community",
            )
        ],
        claims=[
            ResearchClaim(
                claim="岗位重视项目经验。",
                source_urls=["https://example.com/community"],
                confidence=0.6,
                status="UNVERIFIED",
            )
        ],
    )


def test_gap_diagnosis_distinguishes_weak_and_missing_aspects() -> None:
    packet = _packet()

    packet.gaps = diagnose_research_gaps(packet, [])
    assessments = assess_research_aspects(packet)

    assert [(item["aspect"], item["evidence_strength"]) for item in assessments] == [
        ("官方要求", "WEAK"),
        ("社区经验", "MISSING"),
    ]
    assert {(gap.aspect, gap.gap_type) for gap in packet.gaps} == {
        ("官方要求", "WEAK_AUTHORITY"),
        ("社区经验", "MISSING_EVIDENCE"),
    }


def test_gap_diagnosis_removes_stale_missing_gap_after_strong_evidence() -> None:
    packet = _packet()
    packet.claims[0].status = "SUPPORTED"
    packet.claims[0].evidence_level = "DIRECT_EVIDENCE"
    packet.claims[0].evidence_links = [EvidenceLink(passage_id="p1", quoted_text="项目经验")]
    packet.claims[0].covers_aspects = ["官方要求"]
    packet.claims[0].answers_aspects = ["官方要求"]
    packet.passages = [EvidencePassage(passage_id="p1", source_url_or_uri="https://example.com/community",
                                      text="岗位重视项目经验。", content_hash="a" * 64)]

    gaps = diagnose_research_gaps(
        packet,
        [
            ResearchGap(
                aspect="官方要求",
                gap_type="MISSING_EVIDENCE",
                severity="HIGH",
            )
        ],
    )

    assert all(gap.aspect != "官方要求" for gap in gaps)
    assert [(gap.aspect, gap.gap_type) for gap in gaps] == [
        ("社区经验", "MISSING_EVIDENCE")
    ]


def test_official_overview_cannot_answer_specific_api_cancellation_rules():
    packet = _packet()
    packet.brief.required_aspects = ["TaskGroup cancellation rules", "version changes"]
    packet.subqueries[0].query_intent = "Python TaskGroup cancellation rules"
    packet.subqueries[0].covers_aspects = packet.brief.required_aspects
    claim = packet.claims[0]
    claim.status = "SUPPORTED"
    claim.evidence_level = "DIRECT_EVIDENCE"
    claim.claim = "Python supports free-threaded execution."
    claim.evidence_links = [EvidenceLink(passage_id="p1", quoted_text=claim.claim)]
    assert assess_research_aspects(packet)[0]["answerability"] == "PARTIAL"
    claim.claim = "TaskGroup cancels siblings on exception."
    claim.evidence_links[0].quoted_text = claim.claim
    claim.covers_aspects = ["TaskGroup cancellation rules"]
    claim.answers_aspects = list(claim.covers_aspects)
    packet.passages = [EvidencePassage(passage_id="p1", source_url_or_uri="https://example.com/community",
                                      text=claim.claim, content_hash="a" * 64)]
    assert [item["answerability"] for item in assess_research_aspects(packet)] == ["ANSWERED", "PARTIAL"]


def test_supported_label_without_verbatim_evidence_does_not_close_gap():
    packet = _packet()
    packet.claims[0].status = "SUPPORTED"
    packet.claims[0].evidence_level = "FULL_TEXT"
    assert assess_research_aspects(packet)[0]["answerability"] == "PARTIAL"


def test_strategy_action_is_recorded_and_exact_retry_is_suppressed() -> None:
    packet = _packet()
    packet.gaps = [
        ResearchGap(
            aspect="官方要求",
            gap_type="WEAK_AUTHORITY",
            preferred_source_profiles=["official_docs"],
        )
    ]
    action = ResearchAction(
        kind="SWITCH_SOURCE",
        aspect="官方要求",
        source_profiles=["official_docs"],
        query_intent="Agent 工程师 官方招聘 项目经验要求",
    )

    first, _ = prepare_strategy_followups(packet, [action], max_new=2)
    second, _ = prepare_strategy_followups(packet, [action], max_new=2)

    assert [item.query for item in first] == ["Agent 工程师 官方招聘 项目经验要求"]
    assert second == []
    assert len(packet.memory.strategy_attempts) == 1


def test_rewrite_keeps_original_purpose_and_claim_verification_uses_risk() -> None:
    packet = _packet()
    packet.subqueries[0].purpose = "COMPARISON"
    actions = [
        ResearchAction(
            kind="REWRITE_QUERY",
            aspect="官方要求",
            query_intent="Agent 工程师项目能力官方要求",
        ),
        ResearchAction(
            kind="VERIFY_CLAIM",
            aspect="社区经验",
            query_intent="Agent 项目演示招聘价值原始出处",
        ),
    ]

    followups, _ = prepare_strategy_followups(packet, actions, max_new=2)

    assert [item.purpose for item in followups] == ["COMPARISON", "RISK"]


def test_adaptive_rewrite_inherits_hardware_workload_and_metric_constraints() -> None:
    aspect = "RTX 3050 6GB 本地模型实测"
    packet = ResearchPacket(
        question="RTX 3050 6GB 跑 Ollama Embedding 和 reranker 的速度、显存与 OOM",
        brief=ResearchBrief(required_aspects=[aspect]),
        subqueries=[
            ResearchSubquery(
                key="technical-baseline",
                query="RTX 3050 6GB Ollama Embedding reranker 速度 显存 OOM",
                query_intent=(
                    "核实 RTX 3050 6GB 运行 Ollama Embedding 与 reranker "
                    "的速度、显存和 OOM 限制"
                ),
                covers_aspects=[aspect],
                priority=5,
            )
        ],
    )
    action = ResearchAction(
        kind="REWRITE_QUERY",
        aspect=aspect,
        query_intent="CUDA GPU 显存管理 多模型 Embedding reranker 官方文档",
    )

    followups, _ = prepare_strategy_followups(packet, [action], max_new=1)

    assert len(followups) == 1
    query = followups[0].query
    assert "RTX 3050" in query
    assert "6GB" in query
    assert "Ollama" in query
    assert "速度" in query
    assert "OOM" in query
    assert followups[0].anchor_entities[0] == "RTX 3050"


def test_legacy_followup_is_converted_to_tracked_strategy_action() -> None:
    packet = _packet()
    packet.gaps = [
        ResearchGap(
            aspect="官方要求",
            gap_type="WEAK_AUTHORITY",
            preferred_source_profiles=["official_docs"],
        )
    ]
    legacy = ResearchSubquery(
        key="legacy-official",
        query="Agent 工程师 官方招聘 项目作品要求",
        source_profiles=["official_docs"],
        purpose="DOCUMENTATION",
        priority=5,
        covers_aspects=["官方要求"],
    )

    actions = actions_from_legacy_followups(packet, [legacy])
    followups, _ = prepare_strategy_followups(packet, actions, max_new=1)

    assert [action.kind for action in actions] == ["SWITCH_SOURCE"]
    assert [item.query for item in followups] == [legacy.query]
    assert packet.memory.strategy_attempts[0].action_kind == "SWITCH_SOURCE"


def test_low_yield_guard_requires_two_consecutive_rounds_and_resets_on_gain() -> None:
    packet = ResearchPacket(
        question="test",
        coverage=ResearchCoverage(rounds=1),
        memory={
            "recent_rounds": [
                ResearchRoundMemory(round_index=1),
            ]
        },
    )

    assert update_low_yield_state(packet, previously_covered=set()) is False
    packet.coverage.rounds = 2
    packet.memory.recent_rounds.append(ResearchRoundMemory(round_index=2))
    assert update_low_yield_state(packet, previously_covered=set()) is True

    packet.coverage.rounds = 3
    packet.coverage.covered_aspects = ["new aspect"]
    packet.memory.recent_rounds.append(
        ResearchRoundMemory(round_index=3, new_claim_ids=["claim:new"])
    )
    assert update_low_yield_state(packet, previously_covered=set()) is False
    assert packet.memory.consecutive_low_yield_rounds == 0
