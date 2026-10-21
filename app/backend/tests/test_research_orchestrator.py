from threading import Lock
from time import sleep

from planner_api.research.context import compile_research_context
from planner_api.research.engine_router import EngineRouter, parse_engine_pool
from planner_api.research.evidence import deduplicate_uncertainties
from planner_api.research.evidence import build_web_passages
from planner_api.research.orchestrator import ResearchLimits, ResearchOrchestrator
from planner_api.research.providers import ResearchProviderError
from planner_api.research.query_planner import (
    align_question_aspects,
    classify_query_purpose,
    finalize_query_metadata,
    infer_primary_aspect,
    infer_query_role,
    normalize_research_questions,
    repository_fallback_query,
)
from planner_api.research.ranking import (
    RankingPolicy,
    canonicalize_url,
    classify_source,
    rank_candidates,
    rank_results,
    source_cluster_id,
)
from planner_api.research.schemas import (
    EvidenceLink,
    PageDocument,
    ResearchAction,
    ResearchBrief,
    ResearchBudgetAdjustment,
    ResearchClaim,
    ResearchConflict,
    ResearchDecision,
    ResearchEvidenceAnalysis,
    ResearchGap,
    ResearchPacket,
    ResearchQueryConstraints,
    ResearchReflection,
    ResearchSource,
    ResearchStage,
    ResearchSubquery,
    SearchEngineFailure,
    SearchHit,
    SearchResponse,
)
from planner_api.research.sources import (
    compile_source_queries,
    extract_anchor_entities,
    infer_source_profiles,
    source_engine_preferences,
    source_url_matches_profiles,
)


class FakeResearchService:
    def __init__(self, results: dict[str, list[SearchHit]], failed_pages: set[str] | None = None):
        self.results = results
        self.failed_pages = failed_pages or set()
        self.search_calls = []
        self.read_calls = []

    def search(self, request):
        self.search_calls.append(request)
        hits = self.results.get(request.query, self.results.get("*"))
        if hits is None:
            raise RuntimeError("search unavailable")
        return SearchResponse(
            query=request.query, results=hits, totalResults=len(hits), searchedAt="now"
        )

    def read(self, request):
        self.read_calls.append(request)
        if str(request.url) in self.failed_pages:
            raise RuntimeError("page unavailable")
        return PageDocument(
            url=request.url,
            finalUrl=request.url,
            title="Page",
            content=f"evidence for {request.url} " * 1000,
            retrievalMethod="request",
            truncated=True,
        )


def _attach_complete_stage_evidence(packet):
    """Stage-budget fixtures need quoted evidence as well as successful reads."""
    for source in packet.sources:
        source.content_excerpt = "Verified evidence."
    packet.passages = build_web_passages(packet)
    for passage in packet.passages:
        aspects = [aspect for stage in packet.decision.research_stages for aspect in stage.required_aspects]
        packet.claims.append(ResearchClaim(
            claim="Verified evidence.", confidence=0.9, status="SUPPORTED", evidence_level="DIRECT_EVIDENCE",
            covers_aspects=aspects, answers_aspects=aspects, source_urls=[passage.source_url_or_uri],
            evidence_links=[EvidenceLink(passage_id=passage.passage_id, quoted_text="Verified evidence.")],
        ))


def test_page_reads_are_bounded_and_concurrent() -> None:
    class SlowPageService(FakeResearchService):
        def __init__(self):
            super().__init__({})
            self.active = 0
            self.max_active = 0
            self.lock = Lock()

        def read(self, request):
            with self.lock:
                self.active += 1
                self.max_active = max(self.max_active, self.active)
            try:
                sleep(0.05)
                return super().read(request)
            finally:
                with self.lock:
                    self.active -= 1

    service = SlowPageService()
    packet = ResearchPacket(
        question="并发读取测试",
        sources=[
            ResearchSource(
                title=f"page-{index}",
                url=f"https://example.com/{index}",
                canonical_url=f"https://example.com/{index}",
                matched_queries=["并发读取测试"],
            )
            for index in range(3)
        ],
    )
    orchestrator = ResearchOrchestrator(
        service,
        limits=ResearchLimits(
            max_read_pages=3,
            max_pages_per_round=3,
            max_total_page_chars=30000,
            read_concurrency=3,
        ),
    )

    assert orchestrator._read_selected(packet) == 3
    assert service.max_active == 3
    assert packet.coverage.read_pages == 3


class AlwaysSearchAnalyzer:
    def analyze(self, question: str) -> ResearchDecision:
        return ResearchDecision(should_search=True, research_questions=[question])

    def reflect(self, packet) -> ResearchReflection:
        return ResearchReflection(complete=True)


def test_source_cluster_id_ignores_tracking_parameters() -> None:
    first = "https://example.com/article?utm_source=bing&ref=search"
    second = "https://www.example.com/article"

    assert canonicalize_url(first) == canonicalize_url(second)
    assert source_cluster_id(first) == source_cluster_id(second)


def test_official_profile_does_not_accept_known_community_or_aggregator_pages() -> None:
    assert not source_url_matches_profiles(
        "https://juejin.cn/post/123",
        ["official_docs"],
    )
    assert not source_url_matches_profiles(
        "https://datahub.ac.cn/ai-jobs/topics/large-model-jobs.html",
        ["official_docs"],
    )
    assert source_url_matches_profiles(
        "https://careers.example.com/jobs/agent",
        ["official_docs"],
    )
    assert source_url_matches_profiles(
        "https://careers.example.com/jobs/agent",
        ["official_docs", "github"],
    )


class EvidenceAnalyzer(AlwaysSearchAnalyzer):
    def extract_evidence(self, packet):
        return ResearchEvidenceAnalysis(
            claims=[
                ResearchClaim(
                    claim="The source supports the implementation detail.",
                    source_urls=["https://example.com/source", "https://not-allowed.example/unknown"],
                    confidence=0.9,
                )
            ],
            conflicts=[
                ResearchConflict(
                    topic="version",
                    positions=[
                        {"value": "one", "source_urls": ["https://example.com/source"]},
                        {"value": "two", "source_urls": ["https://not-allowed.example/unknown"]},
                    ],
                )
            ],
            uncertain_points=["没有独立来源确认版本"],
        )


class SnippetOnlyAnalyzer(AlwaysSearchAnalyzer):
    def analyze(self, question: str) -> ResearchDecision:
        return ResearchDecision(
            should_search=True,
            brief=ResearchBrief(required_aspects=["Python 后端岗位要求"]),
            planned_questions=[
                ResearchSubquery(
                    key="job-requirement",
                    query=question,
                    covers_aspects=["Python 后端岗位要求"],
                    required=True,
                )
            ],
        )

    def extract_evidence(self, packet):
        passage = next(
            item
            for item in packet.passages
            if item.trust_metadata.get("is_search_snippet")
        )
        return ResearchEvidenceAnalysis(
            claims=[
                ResearchClaim(
                    claim="该岗位摘要提到 Python 后端开发能力",
                    confidence=0.9,
                    evidence_links=[
                        EvidenceLink(
                            passage_id=passage.passage_id,
                            quoted_text="任职要求包括 Python 后端开发能力",
                        )
                    ],
                )
            ],
            processed_passage_ids=[passage.passage_id],
        )


def _hit(title: str, url: str, engine: str, snippet: str = "") -> SearchHit:
    return SearchHit(title=title, url=url, engine=engine, snippet=snippet, source="test")


def test_blocked_detail_pages_still_extract_search_snippet_evidence() -> None:
    question = "Agent Python 后端 岗位要求"
    url = "https://jobs.example.com/agent-python"
    service = FakeResearchService(
        {
            "*": [
                _hit(
                    "Agent Python 后端岗位要求",
                    url,
                    "bing",
                    "任职要求包括 Python 后端开发能力，熟悉 RAG 者优先。",
                )
            ]
        },
        failed_pages={url},
    )
    packet = ResearchOrchestrator(
        service,
        intent_analyzer=SnippetOnlyAnalyzer(),
        limits=ResearchLimits(
            max_subqueries=2,
            max_queries_per_round=1,
            max_rounds=1,
            max_queries=1,
            results_per_query=5,
            max_sources=5,
            max_sources_per_domain=2,
            min_relevance_score=0,
            max_read_pages=1,
            max_pages_per_round=1,
            max_page_chars=2000,
            max_total_page_chars=4000,
            enable_browser_fallback=False,
        ),
    ).research(question, enabled=True)

    assert packet.coverage.read_pages == 0
    assert packet.coverage.summary_only_sources == 1
    assert packet.passages[0].locator["kind"] == "search_snippet"
    assert packet.claims[0].evidence_level == "SEARCH_SNIPPET"
    assert packet.claims[0].status == "UNVERIFIED"
    assert "Python 后端岗位要求" in packet.coverage.material_aspects
    assert any("搜索摘要" in item for item in packet.uncertain_points)


def test_explicit_browse_request_overrides_transient_no_search_gate() -> None:
    class NoSearchAnalyzer(AlwaysSearchAnalyzer):
        def analyze(self, question):
            return ResearchDecision(should_search=False, reason="模型暂未判断需要联网")

    query = "请联网查找 Open-WebSearch 官方安装要求"
    service = FakeResearchService(
        {"*": [_hit("Open-WebSearch 官方安装要求", "https://example.com/open", "bing")]}
    )
    packet = ResearchOrchestrator(
        service,
        intent_analyzer=NoSearchAnalyzer(),
        limits=ResearchLimits(max_rounds=1, max_queries=1, max_read_pages=1),
    ).research(query, enabled=True)

    assert packet.status in {"READY", "PARTIAL"}
    assert packet.decision is not None and packet.decision.should_search is True
    assert service.search_calls


def test_explicit_url_is_read_even_when_model_gate_says_no_search() -> None:
    class NoSearchAnalyzer(AlwaysSearchAnalyzer):
        def analyze(self, question):
            return ResearchDecision(should_search=False, reason="已有知识足够")

    url = "https://example.com/reference"
    service = FakeResearchService({"*": []})
    packet = ResearchOrchestrator(
        service,
        intent_analyzer=NoSearchAnalyzer(),
        limits=ResearchLimits(
            max_rounds=1,
            max_queries=1,
            max_queries_per_round=1,
            max_read_pages=1,
            max_pages_per_round=1,
        ),
    ).research(f"请阅读这个网址并尽量提取内容：{url}", enabled=True)

    assert packet.decision is not None and packet.decision.should_search is True
    assert service.read_calls and str(service.read_calls[0].url) == url
    explicit = next(source for source in packet.sources if str(source.url) == url)
    assert "EXPLICIT_URL" in explicit.ranking_reasons
    assert explicit.read_status == "SUCCEEDED"


def test_explicit_url_failure_is_reported_without_dropping_search_evidence() -> None:
    url = "https://example.com/blocked"
    service = FakeResearchService(
        {"*": [_hit("blocked page summary", "https://example.com/search-hit", "bing")]},
        failed_pages={url},
    )
    packet = ResearchOrchestrator(
        service,
        intent_analyzer=AlwaysSearchAnalyzer(),
        limits=ResearchLimits(max_rounds=1, max_queries=1, max_read_pages=1),
    ).research(f"分析这个网址的内容：{url}", enabled=True)

    explicit = next(source for source in packet.sources if str(source.url) == url)
    assert explicit.read_status == "FAILED"
    assert explicit.error_code == "PAGE_READ_FAILED"
    assert packet.coverage.summary_only_sources >= 1
    assert packet.status in {"READY", "PARTIAL"}


def test_explicit_url_survives_source_limit_and_is_read_before_discovered_hits() -> None:
    url = "https://example.com/reference"
    discovered = [
        _hit(f"search result {index}", f"https://other.example/{index}", "bing")
        for index in range(6)
    ]
    service = FakeResearchService({"*": discovered})
    packet = ResearchOrchestrator(
        service,
        intent_analyzer=AlwaysSearchAnalyzer(),
        limits=ResearchLimits(
            max_rounds=1,
            max_queries=1,
            max_queries_per_round=1,
            max_sources=2,
            max_read_pages=1,
            max_pages_per_round=1,
        ),
    ).research(f"请阅读这个网址：{url}", enabled=True)

    assert service.read_calls and str(service.read_calls[0].url) == url
    assert any(str(source.url) == url for source in packet.sources)


def test_explicit_source_is_not_demoted_by_snippet_merge_or_stage_fairness() -> None:
    from planner_api.research.schemas import ResearchPacket, ResearchSource

    explicit = ResearchSource(
        url="https://www.rfc-editor.org/rfc/rfc9110.html",
        canonical_url="https://www.rfc-editor.org/rfc/rfc9110.html",
        source_category="OTHER", ranking_score=100,
        ranking_reasons=["EXPLICIT_URL", "SNIPPET_FALLBACK", "SOURCE_PROFILE_MISMATCH"],
        matched_queries=["previous question"],
    )
    discovered = ResearchSource(
        url="https://docs.example/guide", canonical_url="https://docs.example/guide",
        source_category="DOCUMENTATION", ranking_score=200, matched_queries=["current question"],
    )
    packet = ResearchPacket(
        question="Read the specified RFC", active_stage_key="active",
        subqueries=[ResearchSubquery(query="current question", stage_id="active")],
        sources=[discovered, explicit],
    )
    assert ResearchOrchestrator._ordered_read_candidates(packet) == [explicit, discovered]
    explicit.ranking_reasons.remove("EXPLICIT_URL")
    ResearchOrchestrator._seed_explicit_url_sources(packet, [str(explicit.url)])
    assert len(packet.sources) == 2
    assert ResearchOrchestrator._ordered_read_candidates(packet)[0] == explicit
    explicit.read_status = "FAILED"
    ResearchOrchestrator._seed_explicit_url_sources(packet, [str(explicit.url)])
    assert explicit.read_status == "FAILED"


def test_engine_router_selects_by_purpose_and_replaces_failed_engines() -> None:
    router = EngineRouter(
        pool=parse_engine_pool("bing,duckduckgo,baidu,csdn,juejin,linuxdo"),
        max_engines=3,
    )

    assert router.select(purpose="DOCUMENTATION") == ["bing", "duckduckgo", "csdn"]
    assert router.select(
        purpose="DOCUMENTATION",
        candidates=["bing", "baidu", "sogou"],
        failed={"bing", "baidu", "sogou"},
    ) == ["duckduckgo", "csdn", "juejin"]


def test_source_compiler_preserves_project_entities_and_builds_exact_variants() -> None:
    question = ResearchSubquery(
        query="Open-WebSearch official features",
        purpose="DOCUMENTATION",
        source_profiles=["github"],
    )
    question.anchor_entities = extract_anchor_entities(question.query)

    variants = compile_source_queries(question)

    assert question.anchor_entities == ["Open-WebSearch"]
    assert variants[0].startswith('site:github.com "Open-WebSearch"')
    assert all("open features" not in item.casefold() for item in variants)


def test_github_variant_prefers_project_entity_over_windows_platform() -> None:
    question = ResearchSubquery(
        query="Windows 本地 Agent Python Playwright Crawl4AI 部署",
        purpose="DOCUMENTATION",
        source_profiles=["github", "official_docs"],
        anchor_entities=["Windows", "Agent", "Python", "Playwright", "Crawl4AI"],
    )

    variants = compile_source_queries(question)

    assert any(item.startswith('site:github.com "Crawl4AI"') for item in variants)
    assert all('site:github.com "Windows"' not in item for item in variants)


def test_query_normalization_keeps_intent_entities_when_model_query_is_too_short() -> None:
    questions = normalize_research_questions(
        [
            ResearchSubquery(
                query="Open-WebSearch",
                query_intent="Open-WebSearch Crawl4AI JavaScript 页面提取",
                anchor_entities=["Open-WebSearch"],
                required_terms=["Crawl4AI"],
            )
        ]
    )

    assert len(questions) == 1
    assert "Open-WebSearch" in questions[0].query
    assert "Crawl4AI" in questions[0].query
    assert "JavaScript" in questions[0].query


def test_reflection_failure_uses_deterministic_coverage_fallback() -> None:
    class ReflectionFailureAnalyzer(AlwaysSearchAnalyzer):
        def analyze(self, question):
            return ResearchDecision(
                should_search=True,
                brief=ResearchBrief(required_aspects=["官方功能"]),
                planned_questions=[
                    ResearchSubquery(
                        key="official",
                        query="Open-WebSearch 官方功能",
                        covers_aspects=["官方功能"],
                    )
                ],
            )

        def reflect(self, packet):
            raise RuntimeError("invalid model JSON")

    service = FakeResearchService(
        {
            "Open-WebSearch 官方功能": [
                _hit(
                    "Open-WebSearch 官方功能",
                    "https://example.com/open-websearch",
                    "bing",
                    "Open-WebSearch 官方功能与配置说明",
                )
            ]
        }
    )
    packet = ResearchOrchestrator(
        service,
        intent_analyzer=ReflectionFailureAnalyzer(),
        limits=ResearchLimits(max_rounds=1, max_queries=1, max_read_pages=1),
    ).research("Open-WebSearch 官方功能", enabled=True)

    assert packet.status in {"READY", "PARTIAL"}
    assert packet.stop_reason != "REFLECTION_FAILED"
    assert any("程序化证据覆盖判断" in item for item in packet.uncertain_points)
    assert packet.coverage.read_pages == 1


def test_explicit_source_profile_is_not_polluted_by_global_brief_profiles() -> None:
    question = ResearchSubquery(
        query="小红书 上海 租房 避坑 2026",
        source_profiles=["xiaohongshu"],
    )
    variants = compile_source_queries(
        question,
        brief=ResearchBrief(
            source_profiles=["government", "official_docs", "zhihu", "xiaohongshu"]
        ),
    )

    assert variants
    assert all("site:" not in item.casefold() for item in variants)
    assert any(
        cue in " ".join(variants)
        for cue in ("真实经验", "实测", "踩坑", "避坑", "体验")
    )
    assert all("site:gov.cn" not in item for item in variants)


def test_community_intent_uses_a_source_group_without_forcing_xiaohongshu() -> None:
    profiles = infer_source_profiles(
        "上海租房真实经验和避坑讨论",
        "GENERAL",
        ResearchBrief(source_profiles=[]),
    )

    assert profiles[0] == "community"
    assert "xiaohongshu" not in profiles
    assert "zhihu" not in profiles


def test_community_source_group_does_not_filter_to_one_platform() -> None:
    question = ResearchSubquery(
        query="上海租房真实经验",
        source_profiles=["community"],
    )
    variants = compile_source_queries(question)

    assert variants
    assert all("site:" not in item for item in variants)


def test_recruitment_profile_preserves_job_requirements_and_uses_chinese_engines_legacy() -> None:
    question = ResearchSubquery(
        query="大模型应用 Agent 实习岗位 任职要求",
        purpose="CURRENT",
        source_profiles=["recruitment"],
    )
    variants = compile_source_queries(question)

    assert variants
    assert any("招聘" in item and "任职要求" in item for item in variants)
    assert all("site:" not in item.casefold() for item in variants)
    assert "recruitment" in infer_source_profiles(
        "大模型应用 Agent 实习岗位 任职要求", "CURRENT", ResearchBrief()
    )
    assert source_engine_preferences(["recruitment"])[0] == "sogou"
    assert classify_source("https://www.zhipin.com/job/agent") == "JOB_POSTING"


def test_recruitment_query_does_not_add_community_experience_cues() -> None:
    question = ResearchSubquery(
        query="大模型应用 Agent 实习岗位 任职要求",
        purpose="CURRENT",
        source_profiles=["recruitment", "community"],
    )

    variants = compile_source_queries(question)

    assert variants
    assert "招聘" in variants[0]
    assert all(cue not in variants[0] for cue in ("实测", "踩坑", "避坑"))


def test_technical_community_profiles_use_their_dedicated_search_engines() -> None:
    assert source_engine_preferences(["csdn"])[0] == "csdn"
    assert source_engine_preferences(["juejin"])[0] == "juejin"
    assert source_engine_preferences(["linuxdo"])[0] == "linuxdo"


def test_technical_community_profiles_use_one_soft_semantic_variant() -> None:
    question = ResearchSubquery(
        query="RTX 3050 6GB Ollama 显存占用 速度 OOM",
        query_role="COMMUNITY",
        purpose="RISK",
        source_profiles=["csdn", "zhihu", "bilibili", "tieba", "community"],
    )
    variants = compile_source_queries(question)

    assert len(variants) == 1
    assert "site:" not in variants[0].casefold()
    assert all(cue in variants[0] for cue in ("实测", "体验", "踩坑", "避坑"))


def test_soft_community_failure_does_not_repeat_the_same_question_immediately() -> None:
    class CommunityAnalyzer(AlwaysSearchAnalyzer):
        def analyze(self, question):
            return ResearchDecision(
                should_search=True,
                planned_questions=[
                    ResearchSubquery(
                        key="community",
                        query="RTX 3050 6GB Ollama 显存 OOM",
                        query_role="COMMUNITY",
                        purpose="RISK",
                        source_profiles=["community", "csdn", "zhihu"],
                    )
                ],
            )

        def reflect(self, packet):
            return ResearchReflection(complete=False)

    normalized = normalize_research_questions(
        CommunityAnalyzer().analyze("").planned_questions
    )[0]
    compiled = compile_source_queries(normalized)[0]
    service = FakeResearchService({compiled: []})
    packet = ResearchOrchestrator(
        service,
        limits=ResearchLimits(max_rounds=1, max_queries=3, max_read_pages=1),
        intent_analyzer=CommunityAnalyzer(),
    ).research("community research", enabled=True)

    assert [call.query for call in service.search_calls] == [compiled]
    assert packet.coverage.searched_queries == 1


def test_fallback_query_removes_network_request_shell_and_recommendation_tail() -> None:
    question = (
        "请联网调查 RTX 3050 6GB 在 Windows 上通过 Ollama 运行 reranker 的显存占用，"
        "并结合官方资料与社区实测给出可行配置建议"
    )

    normalized = normalize_research_questions([question])[0]

    assert normalized.query.startswith("RTX 3050 6GB")
    assert "请联网调查" not in normalized.query
    assert "给出可行配置建议" not in normalized.query
    assert len(normalized.query) < len(question)


def test_staged_plan_runs_authority_before_community_with_separate_budgets() -> None:
    class StagedAnalyzer(AlwaysSearchAnalyzer):
        def analyze(self, question):
            return ResearchDecision(
                should_search=True,
                brief=ResearchBrief(
                    required_aspects=["官方基线", "社区经验"],
                    source_profiles=["government", "zhihu"],
                ),
                research_stages=[
                    ResearchStage(
                        key="authority",
                        purpose="建立官方基线",
                        source_profiles=["government"],
                        query_budget=1,
                        read_budget=1,
                        order=0,
                    ),
                    ResearchStage(
                        key="community",
                        purpose="补充社区经验",
                        source_profiles=["zhihu"],
                        query_budget=1,
                        read_budget=1,
                        depends_on=["authority"],
                        order=1,
                    ),
                ],
                planned_questions=[
                    ResearchSubquery(
                        key="official",
                        query="上海租赁官方规定",
                        source_profiles=["government"],
                        covers_aspects=["官方基线"],
                        stage_id="authority",
                    ),
                    ResearchSubquery(
                        key="community",
                        query="知乎上海租房经验",
                        source_profiles=["zhihu"],
                        covers_aspects=["社区经验"],
                        stage_id="community",
                    ),
                ],
            )

        def reflect(self, packet):
            if packet.active_stage_key == "authority":
                return ResearchReflection(
                    complete=False,
                    answered_question_keys=["official"],
                    covered_aspects=["官方基线"],
                    next_stage_key="community",
                )
            return ResearchReflection(
                complete=True,
                answered_question_keys=["community"],
                covered_aspects=["社区经验"],
            )

    authority_query = "site:gov.cn 上海租赁官方规定"
    community_query = "知乎上海租房经验 实测 体验 踩坑 避坑 对比"
    service = FakeResearchService(
        {
            authority_query: [
                _hit("上海租赁官方规定", "https://www.gov.cn/rule", "bing")
            ],
            community_query: [
                _hit("知乎上海租房经验", "https://www.zhihu.com/question/1", "bing")
            ],
        }
    )

    packet = ResearchOrchestrator(
        service,
        limits=ResearchLimits(
            max_rounds=3,
            max_queries=4,
            max_queries_per_round=2,
            max_read_pages=4,
            max_pages_per_round=2,
        ),
        intent_analyzer=StagedAnalyzer(),
    ).research("上海租房", enabled=True)

    assert [call.query for call in service.search_calls] == [
        authority_query,
        community_query,
    ]
    assert packet.status == "PARTIAL"
    assert packet.active_stage_key is None
    assert [stage.status for stage in packet.decision.research_stages] == [
        "PARTIAL",
        "PARTIAL",
    ]


def test_reflection_can_switch_source_strategy_without_repeating_the_original_query() -> None:
    class AdaptiveAnalyzer(AlwaysSearchAnalyzer):
        def analyze(self, question):
            return ResearchDecision(
                should_search=True,
                brief=ResearchBrief(required_aspects=["作品集要求"]),
                planned_questions=[
                    ResearchSubquery(
                        key="community-baseline",
                        query="Agent 岗位作品集要求",
                        covers_aspects=["作品集要求"],
                    )
                ],
            )

        def reflect(self, packet):
            if packet.coverage.rounds == 1:
                return ResearchReflection(
                    complete=False,
                    covered_aspects=["作品集要求"],
                    gaps=[
                        ResearchGap(
                            aspect="作品集要求",
                            gap_type="WEAK_AUTHORITY",
                            severity="HIGH",
                            preferred_source_profiles=["official_docs"],
                        )
                    ],
                    strategy_actions=[
                        ResearchAction(
                            kind="SWITCH_SOURCE",
                            aspect="作品集要求",
                            source_profiles=["official_docs"],
                            query_intent="大模型 Agent 工程师 官方招聘 项目经验要求",
                            priority=5,
                        )
                    ],
                )
            return ResearchReflection(complete=True, covered_aspects=["作品集要求"])

    service = FakeResearchService(
        {
            "Agent 岗位作品集要求": [
                _hit("Agent 岗位作品集要求", "https://example.com/community", "bing")
            ],
            "大模型 Agent 工程师 官方招聘 项目经验要求": [
                _hit(
                    "Company Careers 2026",
                    "https://jobs.company.cn/agent",
                    "bing",
                    "大模型 Agent 工程师 官方招聘 项目经验要求",
                )
            ],
        }
    )
    packet = ResearchOrchestrator(
        service,
        limits=ResearchLimits(max_rounds=3, max_queries=4, max_read_pages=4),
        intent_analyzer=AdaptiveAnalyzer(),
    ).research("Agent 岗位作品集要求", enabled=True)

    assert [call.query for call in service.search_calls] == [
        "Agent 岗位作品集要求",
        "大模型 Agent 工程师 官方招聘 项目经验要求",
    ]
    assert packet.status == "PARTIAL"
    assert len(packet.memory.strategy_attempts) == 1
    assert packet.memory.strategy_attempts[0].action_kind == "SWITCH_SOURCE"
    assert packet.memory.strategy_attempts[0].source_profiles == ["official_docs"]
    assert packet.memory.strategy_attempts[0].outcome == "EVIDENCE_READY"


def test_legacy_planned_followup_is_tracked_as_adaptive_strategy_attempt() -> None:
    class LegacyFollowupAnalyzer(AlwaysSearchAnalyzer):
        def analyze(self, question):
            return ResearchDecision(
                should_search=True,
                brief=ResearchBrief(required_aspects=["作品集要求"]),
                planned_questions=[
                    ResearchSubquery(
                        key="baseline",
                        query="Agent 岗位作品集要求",
                        covers_aspects=["作品集要求"],
                    )
                ],
            )

        def reflect(self, packet):
            if packet.coverage.rounds == 1:
                return ResearchReflection(
                    complete=False,
                    planned_follow_ups=[
                        ResearchSubquery(
                            key="legacy-followup",
                            query="Agent 岗位 官方招聘 项目作品要求",
                            source_profiles=["official_docs"],
                            covers_aspects=["作品集要求"],
                        )
                    ],
                )
            return ResearchReflection(complete=True, covered_aspects=["作品集要求"])

    service = FakeResearchService(
        {
            "Agent 岗位作品集要求": [
                _hit("Agent 岗位作品集要求 社区经验", "https://example.com/community", "bing")
            ],
            "Agent 岗位 官方招聘 项目作品要求": [
                _hit("Agent 岗位 官方招聘 项目作品要求", "https://jobs.example.com/agent", "bing")
            ],
        }
    )
    packet = ResearchOrchestrator(
        service,
        limits=ResearchLimits(max_rounds=3, max_queries=4, max_read_pages=4),
        intent_analyzer=LegacyFollowupAnalyzer(),
    ).research("Agent 岗位作品集要求", enabled=True)

    assert len(packet.memory.strategy_attempts) == 1
    assert packet.memory.strategy_attempts[0].action_kind == "SWITCH_SOURCE"


def test_strategy_action_suppresses_duplicate_legacy_followup_candidates() -> None:
    class MixedReflectionAnalyzer(AlwaysSearchAnalyzer):
        def analyze(self, question):
            return ResearchDecision(
                should_search=True,
                brief=ResearchBrief(required_aspects=["作品集要求"]),
                planned_questions=[
                    ResearchSubquery(
                        key="baseline",
                        query="Agent 岗位作品集要求",
                        covers_aspects=["作品集要求"],
                    )
                ],
            )

        def reflect(self, packet):
            if packet.coverage.rounds == 1:
                return ResearchReflection(
                    complete=False,
                    follow_up_questions=["Agent 官方招聘 项目作品要求"],
                    strategy_actions=[
                        ResearchAction(
                            kind="SWITCH_SOURCE",
                            aspect="作品集要求",
                            source_profiles=["official_docs"],
                            query_intent="Agent 官方招聘 项目作品要求",
                        )
                    ],
                )
            return ResearchReflection(
                complete=True,
                covered_aspects=["作品集要求"],
                answered_question_keys=[item.key for item in packet.subqueries],
            )

    service = FakeResearchService(
        {
            "Agent 岗位作品集要求": [
                _hit(
                    "Agent 岗位作品集要求 社区经验",
                    "https://example.com/community",
                    "bing",
                )
            ],
            "Agent 官方招聘 项目作品要求": [
                _hit(
                    "Agent 官方招聘 项目作品要求",
                    "https://jobs.example.com/agent",
                    "bing",
                )
            ],
        }
    )
    packet = ResearchOrchestrator(
        service,
        limits=ResearchLimits(max_rounds=3, max_queries=4, max_read_pages=4),
        intent_analyzer=MixedReflectionAnalyzer(),
    ).research("Agent 岗位作品集要求", enabled=True)

    assert packet.status == "PARTIAL"
    assert len(packet.memory.strategy_attempts) == 1
    assert not any(item.key.startswith("q_") for item in packet.subqueries)
    assert [item.query for item in packet.subqueries] == [
        "Agent 岗位作品集要求",
        "Agent 官方招聘 项目作品要求",
    ]


def test_covered_aspect_does_not_stay_incomplete_due_to_optional_followup() -> None:
    packet = ResearchPacket(
        question="test",
        brief=ResearchBrief(required_aspects=["官方能力"]),
        subqueries=[
            ResearchSubquery(
                query="补充官方文档",
                covers_aspects=["官方能力"],
                status="PLANNED",
            )
        ],
    )
    packet.coverage.covered_aspects = ["官方能力"]
    packet.coverage.uncovered_aspects = []

    assert ResearchOrchestrator._has_required_pending(packet) is False
    packet.gaps = [
        ResearchGap(
            aspect="官方能力",
            gap_type="WEAK_AUTHORITY",
        )
    ]
    assert ResearchOrchestrator._has_required_pending(packet) is True


def test_adaptive_research_stops_after_two_low_yield_follow_up_rounds() -> None:
    queries = [
        "agent portfolio requirements",
        "official agent engineer project requirements",
        "company agent engineer deployment experience",
        "agent role production evidence",
    ]

    class LowYieldAnalyzer(AlwaysSearchAnalyzer):
        def analyze(self, question):
            return ResearchDecision(
                should_search=True,
                brief=ResearchBrief(required_aspects=["deployment proof"]),
                planned_questions=[
                    ResearchSubquery(
                        key="baseline",
                        query=queries[0],
                        covers_aspects=["deployment proof"],
                    )
                ],
            )

        def reflect(self, packet):
            next_query = queries[min(packet.coverage.rounds, len(queries) - 1)]
            return ResearchReflection(
                complete=False,
                covered_aspects=["deployment proof"],
                gaps=[
                    ResearchGap(
                        aspect="deployment proof",
                        gap_type="WEAK_AUTHORITY",
                    )
                ],
                strategy_actions=[
                    ResearchAction(
                        kind="REWRITE_QUERY",
                        aspect="deployment proof",
                        query_intent=next_query,
                    )
                ],
            )

    service = FakeResearchService(
        {
            query: [_hit(query, f"https://source-{index}.example/{index}", "bing")]
            for index, query in enumerate(queries)
        }
    )
    packet = ResearchOrchestrator(
        service,
        limits=ResearchLimits(max_rounds=5, max_queries=5, max_read_pages=5),
        intent_analyzer=LowYieldAnalyzer(),
    ).research("agent portfolio requirements", enabled=True)

    assert [call.query for call in service.search_calls] == queries[:3]
    assert packet.stop_reason == "LOW_VALUE_SATURATION"
    assert packet.status == "PARTIAL"
    assert packet.memory.consecutive_low_yield_rounds == 2


def test_decision_provider_can_be_replaced_without_replacing_evidence_analyzer() -> None:
    class DeepAnalyzer(AlwaysSearchAnalyzer):
        extraction_calls = 0

        def analyze(self, question):
            raise AssertionError("decision provider should handle analyze")

        def reflect(self, packet):
            raise AssertionError("decision provider should handle reflect")

        def extract_evidence(self, packet):
            self.extraction_calls += 1
            return ResearchEvidenceAnalysis()

    class FastProvider:
        def analyze(self, question):
            return ResearchDecision(should_search=True, research_questions=[question])

        def reflect(self, packet):
            return ResearchReflection(complete=True)

    analyzer = DeepAnalyzer()
    packet = ResearchOrchestrator(
        FakeResearchService(
            {"gateway": [_hit("gateway", "https://example.com/gateway", "bing")]}
        ),
        intent_analyzer=analyzer,
        decision_provider=FastProvider(),
    ).research("gateway", enabled=True)

    assert packet.status == "READY"
    assert analyzer.extraction_calls == 1


def test_engine_router_can_hold_a_model_selected_group() -> None:
    router = EngineRouter(pool=parse_engine_pool("bing,sogou,csdn,juejin"), max_engines=3)

    assert router.select(
        purpose="GENERAL",
        candidates=["sogou", "juejin"],
        strict_candidates=True,
    ) == ["sogou", "juejin"]


def test_query_normalization_only_uses_model_selected_questions() -> None:
    questions = normalize_research_questions(
        ["研究架构", "研究架构", "比较成本", "查看最新文档"], max_subqueries=2
    )

    assert [item.query for item in questions] == ["研究架构", "比较成本"]
    assert [item.purpose for item in questions] == ["GENERAL", "COMPARISON"]
    assert classify_query_purpose("Open-WebSearch 官方安装文档") == "DOCUMENTATION"
    assert repository_fallback_query("Aas-ee open-webSearch README") == (
        'site:github.com "open-webSearch"'
    )
    question = ResearchSubquery(query="官方功能", covers_aspects=["Open-WebSearch：官方功能"])
    align_question_aspects([question], ["Open-WebSearch 官方功能"])
    assert question.covers_aspects == ["Open-WebSearch 官方功能"]


def test_query_normalization_keeps_intent_but_compacts_search_phrase() -> None:
    questions = normalize_research_questions(
        [
            ResearchSubquery(
                query="请问如何比较 Open-WebSearch 和 Crawl4AI 是否支持 JavaScript 页面？",
                covers_aspects=["浏览器读取能力"],
                source_profiles=["official_docs"],
            )
        ]
    )
    finalize_query_metadata(questions, ["浏览器读取能力"])

    assert questions[0].query == "比较 Open-WebSearch 和 Crawl4AI 支持 JavaScript 页面"
    assert "如何比较" in questions[0].query_intent
    assert questions[0].query_role == "AUTHORITATIVE"
    assert questions[0].primary_aspect == "浏览器读取能力"
    assert "Open-WebSearch" in questions[0].anchor_entities
    assert "Crawl4AI" in questions[0].anchor_entities

    long_intent = normalize_research_questions(
        [
            ResearchSubquery(
                query=(
                    "检索 Open-WebSearch 官方仓库中关于其搜索返回形式、是否抓取网页正文、"
                    "是否支持 JavaScript 渲染页面及其限制的说明"
                )
            )
        ]
    )[0]
    assert long_intent.query == (
        "Open-WebSearch 官方 搜索返回形式 抓取网页正文 支持 JavaScript 渲染页面 限制"
    )


def test_query_role_inference_distinguishes_authority_and_community() -> None:
    assert infer_query_role("DOCUMENTATION", ["official_docs"]) == "AUTHORITATIVE"
    assert infer_query_role("GENERAL", ["zhihu", "community"]) == "COMMUNITY"
    assert infer_query_role("RISK", ["community"]) == "VERIFY"
    assert (
        infer_query_role(
            "DOCUMENTATION",
            ["official_docs", "community"],
            "Crawl4AI 官方文档 JavaScript 渲染",
        )
        == "AUTHORITATIVE"
    )
    assert (
        infer_query_role(
            "GENERAL",
            ["github", "community"],
            "Open-WebSearch Crawl4AI 社区经验 踩坑",
        )
        == "COMMUNITY"
    )


def test_legacy_followup_can_be_mapped_to_one_required_aspect() -> None:
    aspects = [
        "Open-WebSearch 官方能力",
        "Crawl4AI JavaScript 页面读取方式与限制",
        "开发者社区真实使用经验与分工判断",
    ]

    assert infer_primary_aspect(
        "Crawl4AI 官方文档 JavaScript 渲染与等待策略",
        aspects,
    ) == "Crawl4AI JavaScript 页面读取方式与限制"
    assert infer_primary_aspect(
        "开发者社区如何划分 Open-WebSearch 与 Crawl4AI 职责",
        aspects,
    ) == "开发者社区真实使用经验与分工判断"


def test_exact_project_repository_outscores_similarly_named_clone() -> None:
    ranked = rank_candidates(
        "Crawl4AI official GitHub",
        [
            _hit("Crawl4AI GitHub", "https://github.com/James-jinye/crawl4ai--", "bing"),
            _hit("Crawl4AI GitHub", "https://github.com/unclecode/crawl4ai", "bing"),
        ],
        max_results=2,
    ).accepted

    assert ranked[0].canonical_url == "https://github.com/unclecode/crawl4ai"
    assert "EXACT_PROJECT_REPOSITORY" in ranked[0].reasons


def test_multi_project_query_accepts_each_exact_repository() -> None:
    outcome = rank_candidates(
        "Open-WebSearch Crawl4AI Windows 官方文档",
        [
            _hit(
                "Open-WebSearch - GitHub",
                "https://github.com/Aas-ee/open-webSearch",
                "bing",
            ),
            _hit(
                "Crawl4AI - GitHub",
                "https://github.com/unclecode/crawl4ai",
                "bing",
            ),
        ],
        max_results=5,
        purpose="DOCUMENTATION",
        constraints=ResearchQueryConstraints(
            entities=["Open-WebSearch", "Crawl4AI", "Windows"]
        ),
        query_role="AUTHORITATIVE",
    )

    assert {item.canonical_url for item in outcome.accepted} == {
        "https://github.com/Aas-ee/open-webSearch",
        "https://github.com/unclecode/crawl4ai",
    }


def test_exact_project_can_defer_platform_constraint_to_page_read() -> None:
    outcome = rank_candidates(
        "Open-WebSearch Windows 安装依赖",
        [
            _hit(
                "Open-WebSearch - GitHub",
                "https://github.com/Aas-ee/open-webSearch",
                "bing",
                "Open-WebSearch source code and installation",
            )
        ],
        max_results=5,
        purpose="DOCUMENTATION",
        constraints=ResearchQueryConstraints(
            entities=["Open-WebSearch", "Windows"],
            strict_terms=["Open-WebSearch", "Windows"],
        ),
        query_role="AUTHORITATIVE",
    )

    assert len(outcome.accepted) == 1


def test_repository_diversity_budget_is_per_project_not_entire_github() -> None:
    class TwoRepositoryAnalyzer(AlwaysSearchAnalyzer):
        def analyze(self, question):
            return ResearchDecision(
                should_search=True,
                research_questions=["Open-WebSearch GitHub", "Crawl4AI GitHub"],
            )

    service = FakeResearchService(
        {
            "Open-WebSearch GitHub": [
                _hit("Open-WebSearch GitHub", "https://github.com/Aas-ee/open-webSearch", "bing")
            ],
            "Crawl4AI GitHub": [
                _hit("Crawl4AI GitHub", "https://github.com/unclecode/crawl4ai", "bing")
            ],
        }
    )
    packet = ResearchOrchestrator(
        service,
        limits=ResearchLimits(max_sources_per_domain=1),
        intent_analyzer=TwoRepositoryAnalyzer(),
    ).research("Compare the official repositories", enabled=True)

    assert packet.coverage.read_pages == 2
    assert {str(source.url) for source in packet.sources} == {
        "https://github.com/Aas-ee/open-webSearch",
        "https://github.com/unclecode/crawl4ai",
    }


def test_ranking_deduplicates_tracking_urls_and_demotes_irrelevant_results() -> None:
    hits = [
        _hit("open 英文词典", "https://dictionary.example/open", "bing"),
        _hit(
            "Open-WebSearch GitHub repository",
            "https://github.com/a/project?utm_source=bing",
            "bing",
        ),
        _hit(
            "Open-WebSearch GitHub repository",
            "https://github.com/a/project",
            "sogou",
            "Official repository",
        ),
    ]

    ranked = rank_results("Open-WebSearch GitHub", hits, max_results=10)

    assert len(ranked) == 1
    assert ranked[0].canonical_url == "https://github.com/a/project"
    assert ranked[0].engines == ["bing", "sogou"]
    assert ranked[0].source_category == "REPOSITORY"
    assert "MULTI_ENGINE" in ranked[0].reasons
    assert canonicalize_url("https://example.com/x/?utm_medium=ads#top") == "https://example.com/x"


def test_ranking_requires_complete_hyphenated_project_entity() -> None:
    outcome = rank_candidates(
        "Open-WebSearch Windows 部署失败",
        [
            _hit("open 英文词典", "https://dictionary.example/open", "bing"),
            _hit("Open VPN Windows", "https://vpn.example/open", "bing"),
            _hit(
                "Open-WebSearch Windows setup",
                "https://github.com/example/open-websearch",
                "bing",
            ),
        ],
        max_results=10,
        policy=RankingPolicy(min_relevance_score=0, allow_partial_overlap=True),
    )

    assert [item.hit.title for item in outcome.accepted] == [
        "Open-WebSearch Windows setup"
    ]
    assert outcome.filter_reasons["ENTITY_MISMATCH"] == 2


def test_ranking_rejects_results_missing_a_generic_strict_constraint() -> None:
    outcome = rank_candidates(
        "上海 杭州 2500元 行程预算",
        [
            _hit(
                "上海到杭州周末行程 5000元预算",
                "https://travel.example/expensive",
                "bing",
            ),
            _hit(
                "上海到杭州 2500元行程",
                "https://travel.example/budget",
                "bing",
                "高铁、住宿和市内交通预算明细",
            ),
        ],
        max_results=10,
        policy=RankingPolicy(min_relevance_score=0, allow_partial_overlap=True),
        constraints=ResearchQueryConstraints(strict_terms=["2500元"]),
        query_role="VERIFY",
    )

    assert [item.hit.title for item in outcome.accepted] == ["上海到杭州 2500元行程"]
    assert outcome.filter_reasons["CONSTRAINT_MISMATCH"] == 1


def test_ranking_preserves_related_rejected_snippet_as_weak_fallback() -> None:
    outcome = rank_candidates(
        "RTX 3050 6GB Ollama 显存 OOM",
        [
            _hit(
                "Ollama 多模型显存设置",
                "https://github.com/ollama/ollama/issues/4855",
                "bing",
                "OLLAMA_MAX_LOADED_MODELS controls how many models remain loaded.",
            )
        ],
        max_results=10,
        policy=RankingPolicy(
            min_relevance_score=0,
            allow_partial_overlap=True,
            allow_snippet_fallback=True,
        ),
        constraints=ResearchQueryConstraints(strict_terms=["RTX 3050 6GB"]),
        query_role="VERIFY",
    )

    assert outcome.accepted == []
    assert len(outcome.snippet_fallback) == 1
    assert "SNIPPET_FALLBACK" in outcome.snippet_fallback[0].reasons
    assert outcome.filter_reasons["CONSTRAINT_MISMATCH"] == 1


def test_exploratory_community_query_keeps_related_generic_hardware_clue() -> None:
    outcome = rank_candidates(
        "RTX 3050 6GB Ollama vram benchmark experience",
        [
            _hit(
                "Ollama vram OOM benchmark",
                "https://community.example/ollama-vram",
                "bing",
                "User reports Ollama Windows vram and OOM experience.",
            )
        ],
        max_results=10,
        purpose="CURRENT",
        policy=RankingPolicy(
            min_relevance_score=0,
            allow_exploratory_entity_mismatch=True,
        ),
    )

    assert len(outcome.accepted) == 1
    assert "ENTITY_MISMATCH" not in outcome.filter_reasons


def test_ranking_filters_noise_and_records_reasons() -> None:
    hits = [
        _hit("Home", "https://noise.example/", "bing"),
        _hit(
            "Open-WebSearch advertisement",
            "https://ads.example/open-websearch",
            "bing",
            "Sponsored install offer",
        ),
        _hit(
            "Search",
            "https://www.google.com/search?q=open-websearch",
            "bing",
            "Open-WebSearch install",
        ),
        _hit(
            "Open-WebSearch guide",
            "https://www.sogou.com/link?url=unresolved",
            "sogou",
            "Open-WebSearch install guide",
        ),
        _hit(
            "Open-WebSearch installation guide",
            "https://docs.example.com/open-websearch/install",
            "bing",
            "Official installation and configuration steps",
        ),
    ]

    outcome = rank_candidates("Open-WebSearch install", hits, max_results=10)

    assert [item.canonical_url for item in outcome.accepted] == [
        "https://docs.example.com/open-websearch/install"
    ]
    assert outcome.filtered_count == 4
    assert outcome.filter_reasons == {
        "LOW_INFORMATION": 1,
        "ADVERTISEMENT": 1,
        "SEARCH_RESULT_PAGE": 1,
        "UNRESOLVED_SEARCH_REDIRECT": 1,
    }


def test_documentation_ranking_keeps_cross_language_official_candidate() -> None:
    outcome = rank_candidates(
        "Open-WebSearch 官方安装文档",
        [
            _hit(
                "Open-WebSearch repository",
                "https://github.com/example/open-websearch",
                "bing",
                "Installation and configuration documentation",
            ),
            _hit(
                "Open-WebSearch English dictionary",
                "https://dictionary.example/open-websearch",
                "bing",
                "Definitions and translations",
            ),
        ],
        max_results=10,
        purpose="DOCUMENTATION",
    )

    assert len(outcome.accepted) == 1
    assert outcome.accepted[0].source_category == "REPOSITORY"


def test_ranking_collapses_mirrors_and_limits_one_domain() -> None:
    shared_snippet = "Open-WebSearch installation requires Node runtime and local configuration"
    hits = [
        _hit(
            "Open-WebSearch complete installation guide",
            "https://origin.example/install",
            "bing",
            shared_snippet,
        ),
        _hit(
            "Open-WebSearch complete installation guide",
            "https://mirror.example/copied-install",
            "sogou",
            f"{shared_snippet} for Windows users",
        ),
        _hit(
            "Open-WebSearch Windows setup",
            "https://origin.example/windows",
            "bing",
            "Open-WebSearch install steps for Windows and Node",
        ),
        _hit(
            "Open-WebSearch Linux setup",
            "https://origin.example/linux",
            "bing",
            "Open-WebSearch install steps for Linux and Node",
        ),
    ]

    outcome = rank_candidates(
        "Open-WebSearch install",
        hits,
        max_results=10,
        policy=RankingPolicy(max_per_domain=1),
    )

    assert len(outcome.accepted) == 2
    assert outcome.duplicate_count == 1
    assert outcome.diversity_dropped == 1
    represented_urls = {
        *(item.canonical_url for item in outcome.accepted),
        *(url for item in outcome.accepted for url in item.duplicate_urls),
    }
    assert {
        "https://origin.example/install",
        "https://mirror.example/copied-install",
    }.issubset(represented_urls)


def test_orchestrator_can_complete_with_warning_when_one_page_fails() -> None:
    question = "Open-WebSearch GitHub"
    results = {
        question: [
            _hit("Open-WebSearch GitHub", "https://github.com/a/project", "sogou"),
            _hit("Open-WebSearch docs", "https://example.com/docs", "bing"),
            _hit("Open-WebSearch guide", "https://example.com/guide", "bing"),
        ]
    }
    service = FakeResearchService(results, failed_pages={"https://github.com/a/project"})
    limits = ResearchLimits(
        max_rounds=1, max_read_pages=3, max_page_chars=3000, max_total_page_chars=5000
    )

    packet = ResearchOrchestrator(service, limits, intent_analyzer=AlwaysSearchAnalyzer()).research(
        question, enabled=True
    )

    assert packet.status == "READY"
    assert packet.coverage.failed_pages == 1
    assert packet.coverage.read_pages == 2
    assert packet.coverage.rounds == 1
    assert sum(len(source.content_excerpt) for source in packet.sources) <= 5000
    # The failed request is retried once through browser mode before the
    # remaining pages are read.
    assert len(service.read_calls) == 4
    assert packet.claims == []
    assert any("页面读取失败" in note for note in packet.uncertain_points)


def test_research_context_is_bounded_and_marks_web_as_untrusted() -> None:
    service = FakeResearchService(
        {"Open-WebSearch": [_hit("Open-WebSearch", "https://example.com", "sogou", "summary")]}
    )
    packet = ResearchOrchestrator(
        service,
        ResearchLimits(max_page_chars=2000),
        intent_analyzer=AlwaysSearchAnalyzer(),
    ).research("Open-WebSearch", enabled=True)

    context = compile_research_context(packet, max_chars=3000)

    assert len(str(context)) <= 3600
    assert "untrusted_external_sources" in context
    assert "source_handling_rule" in context


def test_evidence_extraction_requires_allowed_sources_and_marks_single_source_unverified() -> None:
    class EvidenceService(FakeResearchService):
        def search(self, request):
            return SearchResponse(
                query=request.query,
                results=[_hit("Open-WebSearch source", "https://example.com/source", "bing")],
                totalResults=1,
                searchedAt="now",
            )

    packet = ResearchOrchestrator(
        EvidenceService({}),
        ResearchLimits(max_rounds=1, max_read_pages=1),
        intent_analyzer=EvidenceAnalyzer(),
    ).research("Open-WebSearch", enabled=True)

    assert len(packet.claims) == 1
    assert packet.claims[0].status == "UNVERIFIED"
    assert packet.claims[0].confidence == 0.65
    assert [str(url) for url in packet.claims[0].source_urls] == [
        "https://example.com/source"
    ]
    assert packet.conflicts == []
    assert "没有独立来源确认版本" in packet.uncertain_points


def test_orchestrator_returns_failed_when_all_searches_fail() -> None:
    service = FakeResearchService({})

    packet = ResearchOrchestrator(service, intent_analyzer=AlwaysSearchAnalyzer()).research(
        "Search unavailable", enabled=True
    )

    assert packet.status == "FAILED"
    assert packet.coverage.successful_queries == 0
    assert packet.coverage.failed_queries == 1
    assert packet.search_diagnostics[0].error_code == "SEARCH_REQUEST_FAILED"
    assert packet.sources == []
    assert service.read_calls == []


def test_all_engine_failures_are_not_counted_as_successful_empty_search() -> None:
    class FailedEnginesService(FakeResearchService):
        def search(self, request):
            return SearchResponse(
                query=request.query,
                engines=["bing", "baidu"],
                partialFailures=[
                    SearchEngineFailure(engine=name, code="engine_error", message="connect EACCES")
                    for name in ("bing", "baidu")
                ],
                results=[],
                searchedAt="now",
            )

    packet = ResearchOrchestrator(
        FailedEnginesService({}), intent_analyzer=AlwaysSearchAnalyzer()
    ).research("Open-WebSearch", enabled=True)

    assert packet.status == "FAILED"
    assert packet.coverage.failed_queries == 1
    assert packet.coverage.successful_queries == 0
    assert packet.search_diagnostics[0].error_code == "ALL_ENGINES_FAILED"
    assert len(packet.search_diagnostics[0].failures) == 2


def test_partial_engine_failure_and_empty_result_are_distinct() -> None:
    class DiagnosticService(FakeResearchService):
        def search(self, request):
            if request.query == "empty":
                return SearchResponse(
                    query="empty", engines=["bing"], results=[], searchedAt="now"
                )
            return SearchResponse(
                query=request.query,
                engines=["bing", "sogou"],
                resultCounts={"bing": 1, "sogou": 0},
                partialFailures=[
                    SearchEngineFailure(engine="sogou", code="engine_error", message="blocked")
                ],
                results=[_hit("Open-WebSearch docs", "https://example.com/docs", "bing")],
                searchedAt="now",
            )

    class TwoQueryAnalyzer(AlwaysSearchAnalyzer):
        def analyze(self, question):
            return ResearchDecision(should_search=True, research_questions=["empty", "docs"])

    packet = ResearchOrchestrator(
        DiagnosticService({}), intent_analyzer=TwoQueryAnalyzer()
    ).research("Open-WebSearch", enabled=True)

    assert packet.status == "READY"
    assert packet.coverage.empty_queries == 1
    assert packet.coverage.partial_queries == 1
    assert [item.status for item in packet.search_diagnostics] == ["EMPTY", "PARTIAL"]
    assert packet.search_diagnostics[1].result_counts == {"bing": 1, "sogou": 0}
    assert any("部分搜索引擎失败" in note for note in packet.uncertain_points)


def test_irrelevant_results_trigger_bounded_repository_fallback_and_engine_degradation() -> None:
    class NoisyService(FakeResearchService):
        def search(self, request):
            self.search_calls.append(request)
            if request.query.startswith("site:github.com"):
                return SearchResponse(
                    query=request.query,
                    engines=request.engines,
                    results=[
                        _hit(
                            "Open-WebSearch - GitHub",
                            "https://github.com/Aas-ee/open-webSearch",
                            "bing",
                            "Source code, installation and documentation",
                        )
                    ],
                    searchedAt="now",
                )
            return SearchResponse(
                query=request.query,
                engines=["bing", "baidu", "sogou"],
                partialFailures=[
                    SearchEngineFailure(engine="baidu", code="engine_error", message="302"),
                    SearchEngineFailure(engine="sogou", code="engine_error", message="challenge"),
                ],
                results=[
                    _hit("Open English dictionary", "https://dictionary.example/open", "bing"),
                    _hit("微信开放平台", "https://open.weixin.qq.com/", "bing"),
                ],
                searchedAt="now",
            )

    class LongQueryAnalyzer(AlwaysSearchAnalyzer):
        def analyze(self, question):
            return ResearchDecision(
                should_search=True,
                planned_questions=[
                    ResearchSubquery(
                        query=(
                            "Open-WebSearch official documentation local deployment "
                            "Agent responsibilities"
                        ),
                        purpose="Find the project's official role and architecture",
                    )
                ],
            )

    service = NoisyService({})
    packet = ResearchOrchestrator(
        service, intent_analyzer=LongQueryAnalyzer()
    ).research("Open-WebSearch architecture", enabled=True)

    assert packet.status == "READY"
    assert packet.coverage.result_count == 3
    assert packet.coverage.candidate_results == 1
    assert packet.coverage.read_pages == 1
    assert packet.subqueries[0].purpose == "DOCUMENTATION"
    assert [item.query for item in service.search_calls] == [
        "Open-WebSearch official documentation local deployment Agent responsibilities",
        'site:github.com "Open-WebSearch"',
    ]
    assert service.search_calls[1].engines == ["bing", "duckduckgo", "csdn"]
    assert packet.search_diagnostics[1].submitted_query == 'site:github.com "Open-WebSearch"'
    assert packet.coverage.filtered_results == 2
    assert packet.search_diagnostics[0].provider_result_count == 2
    assert packet.search_diagnostics[0].filtered_count == 2
    assert packet.search_diagnostics[1].provider_result_count == 1
    assert packet.search_diagnostics[1].ranked_candidate_count == 1
    assert packet.search_diagnostics[1].accepted_candidates[0]["url"] == (
        "https://github.com/Aas-ee/open-webSearch"
    )


def test_no_evidence_first_round_does_not_skip_later_planned_questions() -> None:
    class TwoQuestionAnalyzer(AlwaysSearchAnalyzer):
        def analyze(self, question):
            return ResearchDecision(
                should_search=True,
                research_questions=["unrelated", "Open-WebSearch documentation"],
            )

    service = FakeResearchService(
        {
            "unrelated": [_hit("Random result", "https://other.example/", "bing")],
            "Open-WebSearch documentation": [
                _hit(
                    "Open-WebSearch documentation",
                    "https://github.com/Aas-ee/open-webSearch",
                    "bing",
                )
            ],
        }
    )
    packet = ResearchOrchestrator(
        service,
        limits=ResearchLimits(max_queries_per_round=1, max_rounds=2),
        intent_analyzer=TwoQuestionAnalyzer(),
    ).research("research", enabled=True)

    assert [call.query for call in service.search_calls] == [
        "unrelated", "Open-WebSearch documentation"
    ]
    assert packet.coverage.rounds == 2
    assert packet.coverage.read_pages == 1
    assert packet.status == "READY"


def test_page_budget_reads_official_candidate_before_higher_scoring_blog() -> None:
    query = "Open-WebSearch install"
    service = FakeResearchService(
        {
            query: [
                _hit(
                    "Open-WebSearch install installation guide",
                    "https://blog.example/open-websearch-install",
                    "bing",
                    "Open-WebSearch install complete guide",
                ),
                _hit(
                    "Open-WebSearch docs",
                    "https://docs.example.com/open-websearch",
                    "bing",
                    "Official installation instructions",
                ),
            ]
        }
    )
    packet = ResearchOrchestrator(
        service,
        limits=ResearchLimits(max_read_pages=1, max_pages_per_round=1, max_rounds=1),
        intent_analyzer=AlwaysSearchAnalyzer(),
    ).research(query, enabled=True)

    assert packet.coverage.read_pages == 1
    assert str(service.read_calls[0].url) == "https://docs.example.com/open-websearch"


def test_query_batch_reserves_slots_for_distinct_required_source_profiles() -> None:
    packet = ResearchPacket(
        question="mixed sources",
        brief=ResearchBrief(
            required_aspects=["rules", "experience"],
            source_profiles=["government", "zhihu"],
        ),
    )
    packet.coverage.uncovered_aspects = ["rules", "experience"]
    pending = [
        ResearchSubquery(
            key="official_one",
            query="official rule one",
            source_profiles=["government"],
            covers_aspects=["rules"],
            priority=5,
        ),
        ResearchSubquery(
            key="official_two",
            query="official rule two",
            source_profiles=["government"],
            covers_aspects=["rules"],
            priority=4,
        ),
        ResearchSubquery(
            key="community",
            query="community experience",
            source_profiles=["zhihu"],
            covers_aspects=["experience"],
            priority=1,
        ),
    ]
    packet.subqueries = pending

    selected = ResearchOrchestrator._select_query_batch(packet, pending, 2)

    assert [item.key for item in selected] == ["official_one", "community"]


def test_page_reader_round_robins_questions_before_second_source_from_same_question() -> None:
    packet = ResearchPacket(
        question="mixed evidence",
        subqueries=[
            ResearchSubquery(key="official", query="official rules"),
            ResearchSubquery(key="community", query="community experience"),
        ],
        sources=[
            ResearchSource(
                title="Official one",
                url="https://gov.example/rule-1",
                canonical_url="https://gov.example/rule-1",
                source_category="GOVERNMENT",
                matched_queries=["official rules"],
                ranking_score=90,
            ),
            ResearchSource(
                title="Official two",
                url="https://gov.example/rule-2",
                canonical_url="https://gov.example/rule-2",
                source_category="GOVERNMENT",
                matched_queries=["official rules"],
                ranking_score=80,
            ),
            ResearchSource(
                title="Community",
                url="https://community.example/post",
                canonical_url="https://community.example/post",
                source_category="OTHER",
                matched_queries=["community experience"],
                ranking_score=60,
            ),
        ],
    )

    ordered = ResearchOrchestrator._ordered_read_candidates(packet)

    assert [source.title for source in ordered] == [
        "Official one",
        "Community",
        "Official two",
    ]


def test_failed_page_gets_one_bounded_replacement_candidate() -> None:
    query = "replacement evidence"
    failed = "https://docs.example.com/failed"
    healthy = "https://docs.example.com/healthy"
    service = FakeResearchService(
        {
            query: [
                _hit("replacement evidence failed", failed, "bing"),
                _hit("replacement evidence healthy", healthy, "bing"),
            ]
        },
        failed_pages={failed},
    )

    packet = ResearchOrchestrator(
        service,
        limits=ResearchLimits(max_rounds=1, max_read_pages=1, max_pages_per_round=1),
        intent_analyzer=AlwaysSearchAnalyzer(),
    ).research(query, enabled=True)

    assert [str(call.url) for call in service.read_calls] == [failed, failed, healthy]
    assert packet.coverage.failed_pages == 1
    assert packet.coverage.read_pages == 1


def test_page_reader_prefers_direct_community_url_over_search_redirect() -> None:
    packet = ResearchPacket(
        question="community",
        subqueries=[ResearchSubquery(key="community", query="小红书租房经验")],
        sources=[
            ResearchSource(
                title="Search redirect",
                url="https://www.sogou.com/link?url=abc",
                canonical_url="https://www.sogou.com/link?url=abc",
                matched_queries=["小红书租房经验"],
                ranking_score=95,
            ),
            ResearchSource(
                title="Direct post",
                url="https://www.xiaohongshu.com/explore/123",
                canonical_url="https://www.xiaohongshu.com/explore/123",
                matched_queries=["小红书租房经验"],
                ranking_score=70,
            ),
        ],
    )

    ordered = ResearchOrchestrator._ordered_read_candidates(packet)

    assert [item.title for item in ordered] == ["Direct post", "Search redirect"]


def test_page_reader_allows_two_bounded_failure_replacements() -> None:
    query = "community evidence"
    failed_one = "https://community.example/failed-one"
    failed_two = "https://community.example/failed-two"
    healthy = "https://community.example/healthy"
    service = FakeResearchService(
        {
            query: [
                _hit(f"{query} failed one", failed_one, "bing"),
                _hit(f"{query} failed two", failed_two, "bing"),
                _hit(f"{query} healthy", healthy, "bing"),
            ]
        },
        failed_pages={failed_one, failed_two},
    )

    packet = ResearchOrchestrator(
        service,
        limits=ResearchLimits(max_rounds=1, max_read_pages=1, max_pages_per_round=1),
        intent_analyzer=AlwaysSearchAnalyzer(),
    ).research(query, enabled=True)

    assert [str(call.url) for call in service.read_calls] == [
        failed_one,
        failed_one,
        failed_two,
        failed_two,
        healthy,
    ]
    assert packet.coverage.failed_pages == 2
    assert packet.coverage.read_pages == 1


def test_budget_adjustments_reallocate_without_exceeding_global_ceiling() -> None:
    packet = ResearchPacket(
        question="adaptive budget",
        decision=ResearchDecision(
            should_search=True,
            research_stages=[
                ResearchStage(key="official", query_budget=4, read_budget=4),
                ResearchStage(key="community", query_budget=4, read_budget=4),
            ],
        ),
    )
    orchestrator = ResearchOrchestrator(
        FakeResearchService({}),
        limits=ResearchLimits(max_queries=8, max_read_pages=8),
        intent_analyzer=AlwaysSearchAnalyzer(),
    )

    orchestrator._apply_budget_adjustments(
        packet,
        [
            ResearchBudgetAdjustment(
                stage_key="official", query_delta=-1, read_delta=-1, reason="已足够"
            ),
            ResearchBudgetAdjustment(
                stage_key="community", query_delta=1, read_delta=1, reason="需补查"
            ),
        ],
    )

    stages = packet.decision.research_stages
    assert [(stage.query_budget, stage.read_budget) for stage in stages] == [(3, 3), (5, 5)]
    assert sum(stage.query_budget for stage in stages) == 8
    assert sum(stage.read_budget for stage in stages) == 8
    assert len(packet.budget_adjustments) == 2


def test_stage_finalization_distinguishes_complete_partial_and_blocked() -> None:
    complete = ResearchStage(
        key="complete",
        required_aspects=["规则"],
        min_successful_reads=1,
        min_independent_sources=1,
        status="ACTIVE",
    )
    partial = ResearchStage(
        key="partial",
        required_aspects=["经验"],
        min_successful_reads=2,
        min_independent_sources=2,
        status="ACTIVE",
    )
    blocked = ResearchStage(key="blocked", status="ACTIVE")
    orchestrator = ResearchOrchestrator(
        FakeResearchService({}),
        intent_analyzer=AlwaysSearchAnalyzer(),
    )

    def finalize(stage, *, sources):
        packet = ResearchPacket(
            question="stages",
            active_stage_key=stage.key,
            decision=ResearchDecision(should_search=True, research_stages=[stage]),
            subqueries=[
                ResearchSubquery(
                    key=stage.key,
                    query=stage.key,
                    stage_id=stage.key,
                    covers_aspects=stage.required_aspects,
                    status="EVIDENCE_READY" if sources else "PLANNED",
                )
            ],
            sources=sources,
        )
        _attach_complete_stage_evidence(packet)
        orchestrator._finalize_active_stage(packet)
        return packet.decision.research_stages[0]

    readable = ResearchSource(
        title="Evidence",
        url="https://example.com/evidence",
        canonical_url="https://example.com/evidence",
        matched_queries=["complete"],
        read_status="SUCCEEDED",
        source_cluster_id="example.com",
    )
    partial_readable = readable.model_copy(
        update={"matched_queries": ["partial"], "canonical_url": "https://example.com/partial"}
    )

    assert finalize(complete, sources=[readable]).status == "COMPLETE"
    assert finalize(partial, sources=[partial_readable]).status == "PARTIAL"
    assert finalize(blocked, sources=[]).status == "BLOCKED"


def test_multi_platform_stage_requires_distinct_community_domains() -> None:
    def finalize(second_url: str):
        stage = ResearchStage(
            key="community-verify",
            purpose="核验多平台社区经验",
            required_aspects=["多平台社区经验"],
            min_successful_reads=2,
            min_independent_sources=2,
            status="ACTIVE",
        )
        packet = ResearchPacket(
            question="community comparison",
            active_stage_key=stage.key,
            decision=ResearchDecision(should_search=True, research_stages=[stage]),
            subqueries=[
                ResearchSubquery(
                    key="community-q",
                    query="community comparison",
                    stage_id=stage.key,
                    covers_aspects=stage.required_aspects,
                    status="EVIDENCE_READY",
                )
            ],
            sources=[
                ResearchSource(
                    title="First",
                    url="https://blog.csdn.net/first",
                    canonical_url="https://blog.csdn.net/first",
                    matched_queries=["community comparison"],
                    source_profiles=["community", "csdn"],
                    read_status="SUCCEEDED",
                    source_cluster_id="first",
                ),
                ResearchSource(
                    title="Second",
                    url=second_url,
                    canonical_url=second_url,
                    matched_queries=["community comparison"],
                    source_profiles=["community"],
                    read_status="SUCCEEDED",
                    source_cluster_id="second",
                ),
            ],
        )
        _attach_complete_stage_evidence(packet)
        ResearchOrchestrator(
            FakeResearchService({}),
            intent_analyzer=AlwaysSearchAnalyzer(),
        )._finalize_active_stage(packet)
        return stage

    same_platform = finalize("https://blog.csdn.net/second")
    different_platforms = finalize("https://www.zhihu.com/question/1")

    assert same_platform.status == "PARTIAL"
    assert "当前为 1 个" in same_platform.completion_reason
    assert different_platforms.status == "COMPLETE"
    assert "覆盖 2 个社区平台域名" in different_platforms.completion_reason


def test_exhausted_stage_advances_without_consuming_the_next_round() -> None:
    class TwoStageAnalyzer(AlwaysSearchAnalyzer):
        def analyze(self, question):
            return ResearchDecision(
                should_search=True,
                brief=ResearchBrief(required_aspects=["发现", "核验"]),
                research_stages=[
                    ResearchStage(
                        key="discover",
                        required_aspects=["发现"],
                        query_budget=1,
                        read_budget=1,
                        order=0,
                    ),
                    ResearchStage(
                        key="verify",
                        required_aspects=["核验"],
                        query_budget=1,
                        read_budget=1,
                        depends_on=["discover"],
                        order=1,
                    ),
                ],
                planned_questions=[
                    ResearchSubquery(
                        key="discover-one",
                        query="first discovery",
                        stage_id="discover",
                        covers_aspects=["发现"],
                        priority=5,
                    ),
                    ResearchSubquery(
                        key="discover-extra",
                        query="unused discovery followup",
                        stage_id="discover",
                        covers_aspects=["发现"],
                    ),
                    ResearchSubquery(
                        key="verify-one",
                        query="second verification",
                        stage_id="verify",
                        covers_aspects=["核验"],
                        priority=5,
                    ),
                ],
            )

        def reflect(self, packet):
            return ResearchReflection(complete=False)

    service = FakeResearchService(
        {
            "first discovery": [
                _hit("first discovery", "https://one.example/evidence", "bing")
            ],
            "second verification": [
                _hit("second verification", "https://two.example/evidence", "bing")
            ],
        }
    )
    packet = ResearchOrchestrator(
        service,
        limits=ResearchLimits(
            max_rounds=2,
            max_queries=2,
            max_queries_per_round=1,
            max_read_pages=2,
            max_pages_per_round=1,
        ),
        intent_analyzer=TwoStageAnalyzer(),
    ).research("two-stage research", enabled=True)

    assert [call.query for call in service.search_calls] == [
        "first discovery",
        "second verification",
    ]
    assert packet.coverage.rounds == 2
    assert [stage.status for stage in packet.decision.research_stages] == [
        "PARTIAL",
        "PARTIAL",
    ]


def test_complex_research_gets_five_round_cap_but_simple_research_stays_at_three() -> None:
    orchestrator = ResearchOrchestrator(
        FakeResearchService({}),
        limits=ResearchLimits(max_rounds=3),
        intent_analyzer=AlwaysSearchAnalyzer(),
    )
    simple = ResearchPacket(
        question="查找一个 API 的官方文档",
        brief=ResearchBrief(required_aspects=["安装说明"]),
        decision=ResearchDecision(should_search=True),
    )
    complex_packet = ResearchPacket(
        question="比较多平台社区的实测、OOM 和驱动兼容性",
        brief=ResearchBrief(
            required_aspects=["显存", "速度", "OOM", "驱动"],
            source_profiles=["community", "zhihu", "bilibili"],
        ),
        decision=ResearchDecision(
            should_search=True,
            research_stages=[
                ResearchStage(key="official", source_profiles=["official_docs"]),
                ResearchStage(
                    key="community",
                    source_profiles=["community", "zhihu", "bilibili"],
                ),
                ResearchStage(key="verify", purpose="跨平台冲突核验"),
            ],
        ),
    )

    assert orchestrator._effective_round_limit(simple) == 3
    assert orchestrator._effective_round_limit(complex_packet) == 5
    bounded = ResearchOrchestrator(
        FakeResearchService({}),
        limits=ResearchLimits(max_rounds=2),
        intent_analyzer=AlwaysSearchAnalyzer(),
    )
    assert bounded._effective_round_limit(complex_packet) == 2


def test_aspect_coverage_is_derived_from_readable_question_not_model_echo() -> None:
    packet = ResearchPacket(
        question="official rules",
        brief=ResearchBrief(required_aspects=["官方规则", "社区经验"]),
        subqueries=[
            ResearchSubquery(
                key="official",
                query="official rules",
                covers_aspects=["官方规则"],
                status="EVIDENCE_READY",
            ),
            ResearchSubquery(
                key="community",
                query="community experience",
                covers_aspects=["社区经验"],
                status="BLOCKED",
            ),
        ],
    )

    ResearchOrchestrator._update_aspect_coverage(packet, requested_aspects=[])

    assert packet.coverage.material_aspects == ["官方规则"]
    assert packet.coverage.covered_aspects == []
    assert packet.coverage.uncovered_aspects == ["官方规则", "社区经验"]


def test_synthesis_stage_reuses_dependency_evidence_without_new_page() -> None:
    official = ResearchStage(key="official", status="COMPLETE", order=0)
    community = ResearchStage(key="community", status="COMPLETE", order=1)
    synthesis = ResearchStage(
        key="synthesis",
        mode="SYNTHESIS",
        required_aspects=["官方规则", "社区经验"],
        min_successful_reads=2,
        min_independent_sources=2,
        depends_on=["official", "community"],
        status="ACTIVE",
        order=2,
    )
    packet = ResearchPacket(
        question="compare evidence",
        active_stage_key="synthesis",
        decision=ResearchDecision(
            should_search=True,
            research_stages=[official, community, synthesis],
        ),
        subqueries=[
            ResearchSubquery(
                key="official-q",
                query="official rule",
                stage_id="official",
                covers_aspects=["官方规则"],
                status="ANSWERED",
            ),
            ResearchSubquery(
                key="community-q",
                query="community experience",
                stage_id="community",
                covers_aspects=["社区经验"],
                status="ANSWERED",
            ),
        ],
        sources=[
            ResearchSource(
                title="Official",
                url="https://gov.example/rule",
                canonical_url="https://gov.example/rule",
                matched_queries=["official rule"],
                read_status="SUCCEEDED",
                source_cluster_id="gov.example",
            ),
            ResearchSource(
                title="Community",
                url="https://community.example/post",
                canonical_url="https://community.example/post",
                matched_queries=["community experience"],
                read_status="SUCCEEDED",
                source_cluster_id="community.example",
            ),
        ],
        claims=[
            ResearchClaim(
                claim="official fact",
                source_urls=["https://gov.example/rule"],
                confidence=0.9,
                status="SUPPORTED",
            ),
            ResearchClaim(
                claim="community report",
                source_urls=["https://community.example/post"],
                confidence=0.6,
                status="UNVERIFIED",
            ),
        ],
    )
    orchestrator = ResearchOrchestrator(
        FakeResearchService({}),
        intent_analyzer=AlwaysSearchAnalyzer(),
    )

    orchestrator._finalize_active_stage(packet)

    assert synthesis.status == "PARTIAL"
    assert synthesis.verification_status == "PARTIALLY_VERIFIED"
    assert "未满足全部验收条件" in synthesis.completion_reason


def test_uncertainty_deduplication_keeps_more_informative_containing_message() -> None:
    values = deduplicate_uncertainties(
        [
            "知乎经验尚未覆盖。",
            "知乎经验尚未覆盖，需要继续读取候选帖子。",
            "官方押金退还期限尚未确认。",
        ]
    )

    assert values == [
        "知乎经验尚未覆盖，需要继续读取候选帖子。",
        "官方押金退还期限尚未确认。",
    ]


def test_total_text_budget_is_shared_across_readable_pages() -> None:
    query = "Open-WebSearch documentation"
    service = FakeResearchService(
        {
            query: [
                _hit(
                    f"Open-WebSearch documentation {index}",
                    f"https://docs{index}.example.com/open-websearch",
                    "bing",
                )
                for index in range(4)
            ]
        }
    )
    packet = ResearchOrchestrator(
        service,
        limits=ResearchLimits(
            max_rounds=1,
            max_read_pages=8,
            max_pages_per_round=8,
            max_total_page_chars=12000,
            max_page_chars=8000,
        ),
        intent_analyzer=AlwaysSearchAnalyzer(),
    ).research(query, enabled=True)

    assert packet.coverage.read_pages == 4
    assert all(len(source.content_excerpt) <= 1500 for source in packet.sources)
    assert sum(len(source.content_excerpt) for source in packet.sources) <= 12000


def test_page_failure_keeps_provider_error_code() -> None:
    class ErrorService(FakeResearchService):
        def read(self, request):
            raise ResearchProviderError("fetch timed out", code="UPSTREAM_TIMEOUT")

    packet = ResearchOrchestrator(
        ErrorService({"Open-WebSearch": [_hit("Open-WebSearch", "https://example.com", "bing")]}),
        intent_analyzer=AlwaysSearchAnalyzer(),
    ).research("Open-WebSearch", enabled=True)

    assert packet.status == "PARTIAL"
    assert packet.sources[0].error_code == "UPSTREAM_TIMEOUT"


def test_disabled_switch_never_calls_analyzer_or_search() -> None:
    class ExplodingAnalyzer:
        def analyze(self, question: str):
            raise AssertionError("must not be called")

    service = FakeResearchService({})
    packet = ResearchOrchestrator(service, intent_analyzer=ExplodingAnalyzer()).research(
        "请根据这段已有信息整理计划", enabled=False
    )

    assert packet.status == "DISABLED"
    assert service.search_calls == []


def test_follow_up_query_uses_new_evidence_and_merges_repeated_urls() -> None:
    class FollowUpAnalyzer(AlwaysSearchAnalyzer):
        def reflect(self, packet):
            if packet.coverage.rounds == 1:
                assert packet.sources[0].content_excerpt
                return ResearchReflection(
                    complete=False,
                    follow_up_questions=["Windows browser fallback", "Windows browser fallback"],
                    unresolved_points=["未核对无浏览器时的安装方式"],
                )
            return ResearchReflection(complete=True)

    service = FakeResearchService(
        {
            "Open-WebSearch install": [
                _hit("Open-WebSearch install", "https://example.com/install", "bing")
            ],
            "Windows browser fallback": [
                _hit(
                    "Open-WebSearch Windows browser fallback install",
                    "https://example.com/install#intro",
                    "sogou",
                ),
                _hit("Browser fallback", "https://example.com/browser", "sogou"),
            ],
        }
    )
    packet = ResearchOrchestrator(service, intent_analyzer=FollowUpAnalyzer()).research(
        "Open-WebSearch install", enabled=True
    )

    assert packet.status == "READY"
    assert packet.stop_reason == "MODEL_COMPLETE"
    assert packet.coverage.rounds == 2
    assert packet.coverage.searched_queries == 2
    assert packet.coverage.read_pages == 2
    assert len(packet.sources) == 2
    assert packet.sources[0].engines == ["bing", "sogou"]
    assert [item.query for item in packet.subqueries] == [
        "Open-WebSearch install",
        "Windows browser fallback",
    ]


def test_iteration_stops_at_total_query_budget() -> None:
    class ContinueAnalyzer:
        def analyze(self, question):
            return ResearchDecision(should_search=True, research_questions=["first", "second"])

        def reflect(self, packet):
            return ResearchReflection(
                complete=False,
                follow_up_questions=["third", "fourth", "fifth"],
                unresolved_points=["仍需核对依赖"],
            )

    service = FakeResearchService(
        {
            name: [_hit(name, f"https://example.com/{name}", "bing")]
            for name in ("first", "second", "third", "fourth")
        }
    )
    limits = ResearchLimits(max_queries=4, max_rounds=3, max_read_pages=8)
    packet = ResearchOrchestrator(service, limits, intent_analyzer=ContinueAnalyzer()).research(
        "complex task", enabled=True
    )

    assert packet.status == "PARTIAL"
    assert packet.stop_reason == "QUERY_BUDGET"
    assert packet.coverage.rounds == 2
    assert packet.coverage.searched_queries == 4
    assert [call.query for call in service.search_calls] == ["first", "second", "third", "fourth"]


def test_initial_queries_respect_smaller_total_budget() -> None:
    class BroadAnalyzer(AlwaysSearchAnalyzer):
        def analyze(self, question):
            return ResearchDecision(should_search=True, research_questions=["one", "two", "three"])

    service = FakeResearchService({"one": [_hit("one", "https://example.com/one", "bing")]})
    packet = ResearchOrchestrator(
        service, ResearchLimits(max_queries=1), intent_analyzer=BroadAnalyzer()
    ).research("complex request", enabled=True)

    assert [item.query for item in packet.subqueries] == ["one", "two", "three"]
    assert [call.query for call in service.search_calls] == ["one"]
    assert packet.stop_reason == "QUERY_BUDGET"


def test_repeated_follow_up_stops_without_searching_again() -> None:
    class RepeatingAnalyzer(AlwaysSearchAnalyzer):
        def reflect(self, packet):
            return ResearchReflection(complete=False, follow_up_questions=["same"])

    service = FakeResearchService({"same": [_hit("same", "https://example.com/one", "bing")]})
    packet = ResearchOrchestrator(service, intent_analyzer=RepeatingAnalyzer()).research(
        "same", enabled=True
    )

    assert packet.status == "PARTIAL"
    assert packet.stop_reason == "NO_NEW_QUERIES"
    assert len(service.search_calls) == 1


def test_checkpoint_query_key_prevents_researching_changed_query_text() -> None:
    class RecordingService(FakeResearchService):
        pass

    service = RecordingService({
        "changed query": [_hit("changed", "https://example.com/changed", "bing")]
    })
    checkpoint = ResearchPacket(
        question="resume",
        decision=ResearchDecision(should_search=True),
        subqueries=[ResearchSubquery(key="stable-q", query="changed query")],
        search_diagnostics=[
            {
                "key": "stable-q",
                "query": "old query text",
                "status": "SUCCEEDED",
            }
        ],
        status="PARTIAL",
    )

    packet = ResearchOrchestrator(
        service, intent_analyzer=AlwaysSearchAnalyzer()
    ).research("resume", enabled=True, checkpoint=checkpoint)

    assert service.search_calls == []
    assert packet.coverage.searched_queries == 0


def test_candidate_questions_are_kept_but_executed_in_round_batches() -> None:
    class NineQuestionAnalyzer(AlwaysSearchAnalyzer):
        def analyze(self, question):
            return ResearchDecision(
                should_search=True,
                planned_questions=[
                    ResearchSubquery(
                        key=f"q{i}",
                        query=f"topic {i}",
                        priority=5 if i < 2 else 3,
                        covers_aspects=[f"aspect {i}"],
                    )
                    for i in range(9)
                ],
            )

    service = FakeResearchService(
        {
            f"topic {i}": [_hit(f"topic {i}", f"https://example.com/topic-{i}", "bing")]
            for i in range(9)
        }
    )
    packet = ResearchOrchestrator(
        service,
        ResearchLimits(
            max_subqueries=12,
            max_queries_per_round=3,
            max_queries=3,
            max_rounds=3,
            max_read_pages=3,
        ),
        intent_analyzer=NineQuestionAnalyzer(),
    ).research("nine topics", enabled=True)

    assert len(packet.subqueries) == 9
    assert [call.query for call in service.search_calls] == [
        "topic 0",
        "topic 1",
        "topic 2",
    ]
    assert packet.coverage.searched_queries == 3
    assert packet.stop_reason == "QUERY_BUDGET"


def test_question_dependencies_hold_back_dependent_query_until_parent_has_evidence() -> None:
    class DependentAnalyzer(AlwaysSearchAnalyzer):
        def analyze(self, question):
            return ResearchDecision(
                should_search=True,
                planned_questions=[
                    ResearchSubquery(key="runtime", query="runtime requirements"),
                    ResearchSubquery(
                        key="browser",
                        query="browser fallback requirements",
                        depends_on=["runtime"],
                    ),
                ],
            )

        def reflect(self, packet):
            if packet.coverage.rounds == 1:
                return ResearchReflection(
                    complete=False,
                    answered_question_keys=["runtime"],
                )
            return ResearchReflection(complete=True, answered_question_keys=["browser"])

    service = FakeResearchService(
        {
            "runtime requirements": [
                _hit("runtime", "https://example.com/runtime", "bing")
            ],
            "browser fallback requirements": [
                _hit(
                    "browser fallback requirements",
                    "https://example.com/browser",
                    "bing",
                )
            ],
        }
    )
    packet = ResearchOrchestrator(
        service,
        ResearchLimits(max_rounds=3, max_queries=3, max_queries_per_round=1, max_read_pages=3),
        intent_analyzer=DependentAnalyzer(),
    ).research("runtime then browser", enabled=True)

    assert [call.query for call in service.search_calls] == [
        "runtime requirements",
        "browser fallback requirements",
    ]
    assert packet.status == "READY"


def test_invalid_dependency_is_blocked_without_provider_call() -> None:
    class InvalidDependencyAnalyzer(AlwaysSearchAnalyzer):
        def analyze(self, question):
            return ResearchDecision(
                should_search=True,
                planned_questions=[
                    ResearchSubquery(
                        key="child",
                        query="child question",
                        depends_on=["missing"],
                    )
                ],
            )

    service = FakeResearchService(
        {"child question": [_hit("child", "https://example.com/child", "bing")]}
    )
    packet = ResearchOrchestrator(
        service, ResearchLimits(max_queries=2), intent_analyzer=InvalidDependencyAnalyzer()
    ).research("invalid dependency", enabled=True)

    assert service.search_calls == []
    assert packet.subqueries[0].status == "BLOCKED"
    assert packet.status == "FAILED"
    assert any("不存在的依赖" in item for item in packet.uncertain_points)


def test_each_research_question_gets_a_candidate_before_lower_ranked_results() -> None:
    class TwoTopicsAnalyzer(AlwaysSearchAnalyzer):
        def analyze(self, question):
            return ResearchDecision(should_search=True, research_questions=["alpha", "beta"])

    service = FakeResearchService(
        {
            "alpha": [_hit("alpha", f"https://example.com/alpha{i}", "bing") for i in range(5)],
            "beta": [_hit("beta", "https://example.com/beta", "sogou")],
        }
    )
    ResearchOrchestrator(
        service,
        ResearchLimits(max_rounds=1, max_read_pages=2, max_sources=3),
        intent_analyzer=TwoTopicsAnalyzer(),
    ).research("alpha beta", enabled=True)

    assert {str(call.url) for call in service.read_calls} == {
        "https://example.com/alpha0",
        "https://example.com/beta",
    }


def test_page_content_duplicates_do_not_count_as_independent_evidence() -> None:
    class DuplicateContentService(FakeResearchService):
        def read(self, request):
            self.read_calls.append(request)
            return PageDocument(
                url=request.url,
                finalUrl=request.url,
                title="Mirrored article",
                content="same republished Open-WebSearch evidence " * 200,
                retrievalMethod="request",
            )

    service = DuplicateContentService(
        {
            "Open-WebSearch evidence": [
                _hit(
                    "Open-WebSearch evidence source one",
                    "https://first.example/article",
                    "bing",
                ),
                _hit(
                    "Open-WebSearch evidence source two",
                    "https://second.example/mirror",
                    "sogou",
                ),
            ]
        }
    )

    packet = ResearchOrchestrator(
        service,
        ResearchLimits(max_rounds=1, max_read_pages=2, max_pages_per_round=2),
        intent_analyzer=AlwaysSearchAnalyzer(),
    ).research("Open-WebSearch evidence", enabled=True)

    assert packet.coverage.read_pages == 1
    assert packet.coverage.duplicate_pages == 1
    assert [source.read_status for source in packet.sources] == ["SUCCEEDED", "DUPLICATE"]
    assert packet.sources[1].duplicate_of == packet.sources[0].canonical_url
    assert packet.sources[0].source_cluster_id
    assert packet.sources[1].source_cluster_id == packet.sources[0].source_cluster_id
    assert packet.sources[0].source_independence_score == 1
    assert packet.sources[1].source_independence_score == 0
    assert "DUPLICATE_SOURCE_CLUSTER" in packet.sources[1].ranking_reasons
    assert packet.coverage.source_cluster_count == 1
    assert packet.coverage.independent_source_count == 1
    assert packet.coverage.duplicate_source_count == 1


def test_low_quality_request_page_falls_back_to_browser() -> None:
    class BrowserFallbackService(FakeResearchService):
        def read(self, request):
            self.read_calls.append(request)
            if request.render_mode == "request":
                return PageDocument(
                    url=request.url,
                    finalUrl=request.url,
                    title="Security check",
                    content="Verify you are human. CAPTCHA required.",
                    retrievalMethod="request",
                )
            return PageDocument(
                url=request.url,
                finalUrl=request.url,
                title="Usable article",
                content="Browser-rendered evidence " * 300,
                retrievalMethod="browser",
                readabilityApplied=True,
            )

    service = BrowserFallbackService(
        {"fallback": [_hit("fallback", "https://example.com/fallback", "bing")]}
    )
    packet = ResearchOrchestrator(
        service,
        ResearchLimits(max_rounds=1, max_read_pages=1, max_pages_per_round=1),
        intent_analyzer=AlwaysSearchAnalyzer(),
    ).research("fallback", enabled=True)

    assert [call.render_mode for call in service.read_calls] == ["request", "browser"]
    assert packet.coverage.read_pages == 1
    assert packet.sources[0].read_status == "SUCCEEDED"
    assert packet.sources[0].retrieval_method == "browser"
    assert "BROWSER_FALLBACK" in packet.sources[0].ranking_reasons
    assert packet.sources[0].page_quality_score > 80


def test_request_read_exception_falls_back_to_browser() -> None:
    class ExceptionFallbackService(FakeResearchService):
        def read(self, request):
            self.read_calls.append(request)
            if request.render_mode == "request":
                raise RuntimeError("request transport failed")
            return PageDocument(
                url=request.url,
                finalUrl=request.url,
                title="Usable article",
                content="Browser-rendered evidence " * 300,
                retrievalMethod="browser",
                readabilityApplied=True,
            )

    service = ExceptionFallbackService(
        {"fallback": [_hit("fallback", "https://example.com/fallback", "bing")]}
    )
    packet = ResearchOrchestrator(
        service,
        ResearchLimits(max_rounds=1, max_read_pages=1, max_pages_per_round=1),
        intent_analyzer=AlwaysSearchAnalyzer(),
    ).research("fallback", enabled=True)

    assert [call.render_mode for call in service.read_calls] == ["request", "browser"]
    assert packet.coverage.read_pages == 1
    assert packet.coverage.failed_pages == 0
    assert packet.sources[0].read_status == "SUCCEEDED"
    assert packet.sources[0].retrieval_method == "browser"
