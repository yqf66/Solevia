from planner_api.research.evidence import (
    build_material_passage,
    build_web_passages,
    estimate_source_evidence_density,
    merge_evidence,
    refresh_evidence_metrics,
    validate_evidence_analysis,
)
from planner_api.research.schemas import (
    EvidenceLink,
    EvidencePassage,
    ResearchClaim,
    ResearchConflict,
    ResearchEvidenceAnalysis,
    ResearchPacket,
    ResearchSource,
)


def _source(url: str, text: str, cluster: str) -> ResearchSource:
    return ResearchSource(
        url=url,
        canonical_url=url,
        title="source",
        content_excerpt=text,
        content_fingerprint="fingerprint",
        source_cluster_id=cluster,
        read_status="SUCCEEDED",
    )


def test_web_passage_is_stable_and_addressable() -> None:
    source = _source("https://example.com/a", "事实 A 位于这里。", "example.com")
    packet = ResearchPacket(question="q", sources=[source])

    first = build_web_passages(packet)
    second = build_web_passages(packet)

    assert first[0].passage_id == second[0].passage_id
    assert first[0].locator["char_start"] == 0
    assert first[0].content_hash
    assert first[0].trust_metadata["source_cluster_id"] == "example.com"


def test_invalid_passage_and_quote_are_rejected() -> None:
    source = _source("https://example.com/a", "事实 A 位于这里。", "example.com")
    packet = ResearchPacket(question="q", sources=[source])
    passage = build_web_passages(packet)[0]
    analysis = ResearchEvidenceAnalysis(
        claims=[
            ResearchClaim(
                claim="伪造事实",
                confidence=0.9,
                evidence_links=[
                    EvidenceLink(
                        passage_id="web:missing",
                        quoted_text="事实 A 位于这里。",
                    )
                ],
            ),
            ResearchClaim(
                claim="错引事实",
                confidence=0.9,
                evidence_links=[
                    EvidenceLink(
                        passage_id=passage.passage_id,
                        quoted_text="这句话不在原文。",
                    )
                ],
            ),
        ]
    )

    validated, diagnostics = validate_evidence_analysis(
        analysis, passages=[passage], sources=packet.sources
    )

    assert validated.claims == []
    assert len(diagnostics) == 4


def test_two_independent_passages_support_claim_and_conflict_is_preserved() -> None:
    first_source = _source("https://one.example/a", "系统需要 Node 20。", "one.example")
    second_source = _source("https://two.example/a", "系统需要 Node 20。", "two.example")
    packet = ResearchPacket(question="q", sources=[first_source, second_source])
    passages = [*build_web_passages(ResearchPacket(question="q", sources=[first_source]))]
    passages.extend(build_web_passages(ResearchPacket(question="q", sources=[second_source])))
    links = [
        EvidenceLink(passage_id=item.passage_id, quoted_text="系统需要 Node 20。")
        for item in passages
    ]
    analysis = ResearchEvidenceAnalysis(
        claims=[
            ResearchClaim(
                claim="系统需要 Node 20",
                confidence=0.8,
                evidence_links=links,
            )
        ],
        conflicts=[
            ResearchConflict(
                topic="Node 版本",
                positions=[
                    {"value": "20", "source_urls": ["https://one.example/a"]},
                    {"value": "18", "source_urls": ["https://two.example/a"]},
                ],
            )
        ],
    )

    validated, diagnostics = validate_evidence_analysis(
        analysis, passages=passages, sources=packet.sources
    )

    assert diagnostics == []
    assert validated.claims[0].status == "SUPPORTED"
    assert len(validated.claims[0].source_urls) == 2
    assert len(validated.conflicts) == 1
    assert validated.conflicts[0].resolution == "UNRESOLVED"


def test_single_primary_source_can_support_its_direct_claim() -> None:
    source = _source("https://gov.example/rule", "条例自九月起施行。", "gov.example")
    source.source_category = "GOVERNMENT"
    source.source_profiles = ["government"]
    packet = ResearchPacket(question="q", sources=[source])
    passage = build_web_passages(packet)[0]
    analysis = ResearchEvidenceAnalysis(
        claims=[
            ResearchClaim(
                claim="条例自九月起施行",
                confidence=0.9,
                evidence_links=[
                    EvidenceLink(
                        passage_id=passage.passage_id,
                        quoted_text="条例自九月起施行。",
                    )
                ],
            )
        ]
    )

    validated, diagnostics = validate_evidence_analysis(
        analysis, passages=[passage], sources=packet.sources
    )

    assert diagnostics == []
    assert validated.claims[0].status == "SUPPORTED"


def test_incremental_merge_keeps_prior_evidence() -> None:
    passage = EvidencePassage(
        passage_id="material:1",
        source_revision_id="revision:1",
        source_url_or_uri="file:///notes.md",
        text="必须先备份。",
        content_hash="12345678",
    )
    packet = ResearchPacket(question="q", passages=[passage])
    merge_evidence(
        packet,
        ResearchEvidenceAnalysis(
            claims=[
                ResearchClaim(
                    claim="必须先备份",
                    confidence=0.5,
                    evidence_links=[
                        EvidenceLink(passage_id="material:1", quoted_text="必须先备份。")
                    ],
                )
            ]
        ),
    )
    merge_evidence(
        packet,
        ResearchEvidenceAnalysis(
            claims=[
                ResearchClaim(
                    claim="必须先备份",
                    confidence=0.8,
                    evidence_links=[
                        EvidenceLink(passage_id="material:1", quoted_text="必须先备份。")
                    ],
                )
            ]
        ),
    )

    assert len(packet.claims) == 1
    assert packet.claims[0].confidence == 0.8
    assert len(packet.claims[0].evidence_links) == 1


def test_material_passage_preserves_locator_and_revision() -> None:
    passage = build_material_passage(
        passage_id="material:revision:2:chunk:4",
        source_revision_id="revision:2",
        source_url_or_uri="file:///plan.pdf",
        text="  第 4 页：关键步骤。\n",
        locator={"page": 4, "char_start": 10, "char_end": 22},
    )

    assert passage.source_revision_id == "revision:2"
    assert passage.text == "第 4 页：关键步骤。"
    assert passage.locator["page"] == 4


def test_inferred_summary_is_retained_but_never_promoted_to_direct_fact() -> None:
    first = _source("https://one.example/test", "测试范围为每秒 16 到 21 token。", "one")
    second = _source("https://two.example/test", "不同设置下速度约为每秒 14 到 19 token。", "two")
    packet = ResearchPacket(question="速度范围", sources=[first, second])
    passages = build_web_passages(packet)
    analysis = ResearchEvidenceAnalysis(
        claims=[
            ResearchClaim(
                claim="综合两组测试，常见速度大致落在每秒 14 到 21 token。",
                confidence=0.92,
                evidence_level="INFERRED_SUMMARY",
                inference_note="合并两组不同设置下的实测区间。",
                conditions=["测试设置不同，不能视为统一基准"],
                evidence_links=[
                    EvidenceLink(
                        passage_id=passages[0].passage_id,
                        quoted_text="测试范围为每秒 16 到 21 token。",
                    ),
                    EvidenceLink(
                        passage_id=passages[1].passage_id,
                        quoted_text="不同设置下速度约为每秒 14 到 19 token。",
                    ),
                ],
            )
        ]
    )

    validated, diagnostics = validate_evidence_analysis(
        analysis, passages=passages, sources=packet.sources
    )

    assert diagnostics == []
    claim = validated.claims[0]
    assert claim.evidence_level == "INFERRED_SUMMARY"
    assert claim.status == "PARTIALLY_SUPPORTED"
    assert claim.confidence == 0.7
    assert "14 到 21" in claim.claim


def test_evidence_metrics_distinguish_snippet_full_text_and_direct_evidence() -> None:
    summary_only = ResearchSource(
        url="https://search.example/result",
        canonical_url="https://search.example/result",
        snippet="搜索摘要",
    )
    direct = _source("https://direct.example/test", "显存占用约 5.2 GB。", "direct")
    direct.matched_queries = ["RTX 3050 6GB 显存占用实测"]
    full_text = _source("https://full.example/test", "这是一篇较长但尚未抽取事实的正文。", "full")
    packet = ResearchPacket(question="显存", sources=[summary_only, direct, full_text])
    packet.passages = build_web_passages(packet)
    direct_passage = next(
        item for item in packet.passages if item.source_url_or_uri == str(direct.url)
    )
    packet.claims = [
        ResearchClaim(
            claim="显存占用约 5.2 GB",
            confidence=0.8,
            status="UNVERIFIED",
            evidence_level="DIRECT_EVIDENCE",
            source_urls=[direct.url],
                evidence_links=[
                    EvidenceLink(
                        passage_id=direct_passage.passage_id,
                        quoted_text="显存占用约 5.2 GB。",
                    )
            ],
        )
    ]
    direct.evidence_density = estimate_source_evidence_density(
        direct, "RTX 3050 6GB 显存占用实测约 5.2 GB。"
    )

    refresh_evidence_metrics(packet)

    assert summary_only.evidence_level == "SEARCH_SNIPPET"
    assert direct.evidence_level == "DIRECT_EVIDENCE"
    assert full_text.evidence_level == "FULL_TEXT"
    assert direct.evidence_density > 0
    assert packet.coverage.summary_only_sources == 1
    assert packet.coverage.full_text_sources == 1
    assert packet.coverage.direct_evidence_sources == 1


def test_blocked_page_snippet_is_retained_as_weak_evidence_only() -> None:
    source = ResearchSource(
        url="https://jobs.example.com/agent",
        canonical_url="https://jobs.example.com/agent",
        title="Agent 开发实习生",
        snippet="任职要求：熟悉 Python 后端开发，了解 RAG 和工具调用者优先。",
        read_status="FAILED",
        error_code="PAGE_READ_FAILED",
    )
    packet = ResearchPacket(question="岗位任职要求", sources=[source])
    passages = build_web_passages(packet)

    assert passages[0].locator["kind"] == "search_snippet"
    assert passages[0].trust_metadata["is_search_snippet"] is True
    analysis = ResearchEvidenceAnalysis(
        claims=[
            ResearchClaim(
                claim="该岗位摘要提到 Python 后端开发",
                confidence=0.9,
                evidence_links=[
                    EvidenceLink(
                        passage_id=passages[0].passage_id,
                        quoted_text="任职要求：熟悉 Python 后端开发，了解 RAG 和工具调用者优先。",
                    )
                ],
            )
        ]
    )
    validated, diagnostics = validate_evidence_analysis(
        analysis, passages=passages, sources=packet.sources
    )

    assert diagnostics == []
    assert validated.claims[0].evidence_level == "SEARCH_SNIPPET"
    assert validated.claims[0].status == "UNVERIFIED"
    assert validated.claims[0].confidence <= 0.55


def test_multi_snippet_synthesis_keeps_inferred_summary_label_and_weak_status() -> None:
    first = ResearchSource(
        url="https://search.example/one",
        canonical_url="https://search.example/one",
        snippet="多名用户摘要提到 6GB 显存下需要减小上下文长度。",
        read_status="FAILED",
    )
    second = ResearchSource(
        url="https://search.example/two",
        canonical_url="https://search.example/two",
        snippet="另一条摘要提到同时加载多个模型时可能触发 OOM。",
        read_status="FAILED",
    )
    passages = build_web_passages(ResearchPacket(question="6GB 显存限制", sources=[first, second]))
    analysis = ResearchEvidenceAnalysis(
        claims=[
            ResearchClaim(
                claim="现有摘要共同提示 6GB 显存需要控制上下文和并发加载的模型数量。",
                confidence=0.9,
                evidence_level="INFERRED_SUMMARY",
                inference_note="由两条独立搜索摘要的限制线索综合得到。",
                evidence_links=[
                    EvidenceLink(
                        passage_id=passages[0].passage_id,
                        quoted_text="多名用户摘要提到 6GB 显存下需要减小上下文长度。",
                    ),
                    EvidenceLink(
                        passage_id=passages[1].passage_id,
                        quoted_text="另一条摘要提到同时加载多个模型时可能触发 OOM。",
                    ),
                ],
            )
        ]
    )

    validated, diagnostics = validate_evidence_analysis(
        analysis,
        passages=passages,
        sources=[first, second],
    )

    assert diagnostics == []
    claim = validated.claims[0]
    assert claim.evidence_level == "INFERRED_SUMMARY"
    assert claim.status == "UNVERIFIED"
    assert claim.confidence <= 0.55
    assert claim.inference_note == "由两条独立搜索摘要的限制线索综合得到。"


def test_off_topic_product_fact_is_not_promoted_for_specific_hardware_question() -> None:
    source = _source(
        "https://example.com/ollama",
        "Ollama 提供 Windows、Linux 和 macOS 安装包。",
        "example",
    )
    passage = build_web_passages(ResearchPacket(question="q", sources=[source]))[0]
    analysis = ResearchEvidenceAnalysis(
        claims=[
            ResearchClaim(
                claim="Ollama 支持 Windows、Linux 和 macOS。",
                confidence=0.9,
                evidence_links=[
                    EvidenceLink(
                        passage_id=passage.passage_id,
                        quoted_text="Ollama 提供 Windows、Linux 和 macOS 安装包。",
                    )
                ],
            )
        ]
    )

    validated, diagnostics = validate_evidence_analysis(
        analysis,
        passages=[passage],
        sources=[source],
        focus_texts=["RTX 3050 6GB 运行 Ollama 的显存占用、速度和 OOM 限制"],
    )

    assert validated.claims == []
    assert any("关系过弱" in item for item in diagnostics)


def test_specific_hardware_fact_passes_focus_gate() -> None:
    source = _source(
        "https://example.com/benchmark",
        "RTX 3050 6GB 运行 7B Q4 模型时显存占用约 5.2 GB。",
        "example",
    )
    passage = build_web_passages(ResearchPacket(question="q", sources=[source]))[0]
    analysis = ResearchEvidenceAnalysis(
        claims=[
            ResearchClaim(
                claim="RTX 3050 6GB 运行 7B Q4 时显存占用约 5.2 GB。",
                confidence=0.8,
                evidence_links=[
                    EvidenceLink(
                        passage_id=passage.passage_id,
                        quoted_text="RTX 3050 6GB 运行 7B Q4 模型时显存占用约 5.2 GB。",
                    )
                ],
            )
        ]
    )

    validated, diagnostics = validate_evidence_analysis(
        analysis,
        passages=[passage],
        sources=[source],
        focus_texts=["RTX 3050 6GB 运行 Ollama 的显存占用、速度和 OOM 限制"],
    )

    assert diagnostics == []
    assert validated.claims[0].evidence_level == "DIRECT_EVIDENCE"


def test_versioned_api_claim_does_not_need_to_repeat_version_in_every_sentence():
    source = _source("https://docs.python.org/3.14/library/asyncio-task.html",
                     "TaskGroup cancels remaining tasks when one fails.", "python-docs")
    passage = build_web_passages(ResearchPacket(question="q", sources=[source]))[0]
    analysis = ResearchEvidenceAnalysis(claims=[ResearchClaim(
        claim="TaskGroup cancels remaining tasks when one fails.", confidence=0.9,
        evidence_links=[EvidenceLink(passage_id=passage.passage_id, quoted_text=passage.text)],
    )])
    validated, diagnostics = validate_evidence_analysis(analysis, passages=[passage], sources=[source],
        focus_texts=["Python 3.14 TaskGroup cancellation rules"])
    assert len(validated.claims) == 1, diagnostics
