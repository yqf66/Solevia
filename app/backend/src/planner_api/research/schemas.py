from datetime import UTC, datetime
from typing import Any, Literal
from uuid import uuid4

from pydantic import AnyHttpUrl, BaseModel, ConfigDict, Field, field_validator
from pydantic.json_schema import SkipJsonSchema


class SearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=1000)
    limit: int = Field(default=10, ge=1, le=20)
    engines: list[str] = Field(default_factory=list, max_length=10)
    search_mode: Literal["auto", "request", "playwright"] = "auto"

    @field_validator("query")
    @classmethod
    def strip_query(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("query cannot be blank")
        return value


class SearchHit(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    title: str = ""
    url: AnyHttpUrl
    snippet: str = ""
    source: str = ""
    engine: str = ""
    engines: list[str] = Field(default_factory=list)
    published_at: str | None = Field(default=None, alias="publishedAt")


class SearchEngineFailure(BaseModel):
    engine: str
    code: str
    message: str = ""


class SearchResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    query: str
    results: list[SearchHit] = Field(default_factory=list)
    total_results: int = Field(default=0, ge=0, alias="totalResults")
    engines: list[str] = Field(default_factory=list)
    result_counts: dict[str, int] = Field(default_factory=dict, alias="resultCounts")
    partial_failures: list[SearchEngineFailure] = Field(
        default_factory=list, alias="partialFailures"
    )
    attempted_modes: list[str] = Field(default_factory=list, alias="attemptedModes")
    retry_count: int = Field(default=0, ge=0, alias="retryCount")
    retry_reason: str | None = Field(default=None, alias="retryReason")
    searched_at: str = Field(alias="searchedAt")


class PageReadRequest(BaseModel):
    url: AnyHttpUrl
    max_chars: int = Field(default=12000, ge=1000, le=30000)
    render_mode: Literal["request", "browser"] = "request"
    include_links: bool = False
    focus: str = Field(default="", max_length=500)


class PageDocument(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    url: AnyHttpUrl
    final_url: AnyHttpUrl = Field(alias="finalUrl")
    title: str = ""
    content: str = ""
    retrieval_method: str = Field(default="request", alias="retrievalMethod")
    content_type: str = Field(default="", alias="contentType")
    readability_applied: bool = Field(default=False, alias="readabilityApplied")
    excerpt: str = ""
    byline: str = ""
    site_name: str = Field(default="", alias="siteName")
    truncated: bool = False
    links: list[dict[str, object]] = Field(default_factory=list)
    quality_score: float = Field(default=0.0, ge=0, le=100, alias="qualityScore")
    quality_flags: list[str] = Field(default_factory=list, alias="qualityFlags")
    evidence_archive: dict[str, object] = Field(default_factory=dict, alias="evidenceArchive")


class ResearchQueryConstraints(BaseModel):
    """Portable query contract shared by planners, rankers and future local models."""

    entities: list[str] = Field(default_factory=list, max_length=8)
    specifications: list[str] = Field(default_factory=list, max_length=8)
    relations: list[str] = Field(default_factory=list, max_length=8)
    metrics: list[str] = Field(default_factory=list, max_length=8)
    strict_terms: list[str] = Field(default_factory=list, max_length=12)
    literal_terms: list[str] = Field(
        default_factory=list, max_length=12,
        description="Only terms explicitly required to match literally; semantic requirements belong in strict_terms.",
    )
    relaxable_terms: list[str] = Field(default_factory=list, max_length=12)


class ResearchSubquery(BaseModel):
    key: str = ""
    query: str = Field(min_length=1, max_length=500)
    reference_urls: list[AnyHttpUrl] = Field(default_factory=list, max_length=3)
    query_intent: str = Field(default="", max_length=500)
    query_role: Literal[
        "DISCOVERY",
        "AUTHORITATIVE",
        "COMMUNITY",
        "VERIFY",
    ] = "DISCOVERY"
    primary_aspect: str = Field(default="", max_length=240)
    anchor_entities: list[str] = Field(default_factory=list, max_length=6)
    required_terms: list[str] = Field(default_factory=list, max_length=8)
    constraints: ResearchQueryConstraints = Field(default_factory=ResearchQueryConstraints)
    source_profiles: list[str] = Field(default_factory=list, max_length=8)
    # Runtime diagnostics persist, but do not expand the baseline LLM schema.
    source_scope_mode: Literal["LEGACY", "SOFT", "HARD"] = "LEGACY"
    source_strategy_hint: SkipJsonSchema[Literal["", "official", "academic", "community", "balanced"]] = ""
    purpose: str = "general"
    priority: int = Field(default=3, ge=1, le=5)
    required: bool = True
    depends_on: list[str] = Field(default_factory=list, max_length=8)
    covers_aspects: list[str] = Field(default_factory=list, max_length=8)
    stage_id: str = Field(default="", max_length=64)
    status: Literal["PLANNED", "SEARCHED", "EVIDENCE_READY", "ANSWERED", "BLOCKED"] = "PLANNED"


class ResearchBrief(BaseModel):
    objective: str = ""
    required_aspects: list[str] = Field(default_factory=list, max_length=12)
    constraints: list[str] = Field(default_factory=list, max_length=12)
    exclusions: list[str] = Field(default_factory=list, max_length=12)
    freshness_requirement: str = ""
    source_profiles: list[str] = Field(default_factory=list, max_length=8)


class ResearchStage(BaseModel):
    """A bounded phase in an adaptive research plan."""

    key: str = Field(min_length=1, max_length=64)
    mode: Literal["RETRIEVAL", "SYNTHESIS"] = "RETRIEVAL"
    purpose: str = Field(default="", max_length=240)
    source_profiles: list[str] = Field(default_factory=list, max_length=8)
    required_aspects: list[str] = Field(default_factory=list, max_length=8)
    query_budget: int = Field(default=2, ge=1, le=12)
    read_budget: int = Field(default=2, ge=0, le=12)
    min_successful_reads: int = Field(default=1, ge=0, le=6)
    min_independent_sources: int = Field(default=1, ge=0, le=6)
    depends_on: list[str] = Field(default_factory=list, max_length=6)
    order: int = Field(default=0, ge=0, le=12)
    status: Literal["PLANNED", "ACTIVE", "COMPLETE", "PARTIAL", "BLOCKED"] = "PLANNED"
    verification_status: Literal[
        "NOT_ASSESSED",
        "UNVERIFIED",
        "PARTIALLY_VERIFIED",
        "VERIFIED",
        "CONFLICTED",
    ] = "NOT_ASSESSED"
    completion_reason: str = Field(default="", max_length=400)


class ResearchDecision(BaseModel):
    should_search: bool
    cache_coverage: Literal["SUFFICIENT", "PARTIAL", "INSUFFICIENT", "NOT_ASSESSED"] = "NOT_ASSESSED"
    cached_evidence_ids: list[str] = Field(default_factory=list, max_length=6)
    cache_missing_aspects: list[str] = Field(default_factory=list, max_length=8)
    topic_relation: Literal["FOLLOW_UP", "NEW_TOPIC"] = "FOLLOW_UP"
    inherit_source_restrictions: bool = True
    knowledge_sufficiency: Literal["SUFFICIENT", "PARTIAL", "INSUFFICIENT", "UNKNOWN"] = "UNKNOWN"
    search_value: Literal["NONE", "LOW", "MEDIUM", "HIGH"] = "NONE"
    research_questions: list[str] = Field(default_factory=list, max_length=12)
    planned_questions: list[ResearchSubquery] = Field(default_factory=list, max_length=12)
    brief: ResearchBrief | None = None
    research_stages: list[ResearchStage] = Field(default_factory=list, max_length=6)
    planning_mode: Literal["ADAPTIVE", "LINEAR", "BROADCAST"] = "ADAPTIVE"
    engine_group: list[str] = Field(default_factory=list, max_length=6)
    search_strategy: Literal["CORE_BROADCAST", "FULL_BROADCAST"] = "CORE_BROADCAST"
    reason: str = ""


class ResearchBudgetAdjustment(BaseModel):
    """A model-proposed, program-bounded change to one stage's remaining budget."""

    stage_key: str = Field(min_length=1, max_length=64)
    query_delta: int = Field(default=0, ge=-6, le=6)
    read_delta: int = Field(default=0, ge=-6, le=6)
    reason: str = Field(default="", max_length=300)


class ResearchGap(BaseModel):
    """A structured explanation of why one required aspect is not answerable yet."""

    aspect: str = Field(min_length=1, max_length=240)
    gap_type: Literal[
        "MISSING_EVIDENCE",
        "WEAK_AUTHORITY",
        "LOW_DIVERSITY",
        "STALE_EVIDENCE",
        "CONFLICT",
        "ACCESS_FAILURE",
    ]
    severity: Literal["LOW", "MEDIUM", "HIGH"] = "MEDIUM"
    evidence_needed: str = Field(default="", max_length=500)
    preferred_source_profiles: list[str] = Field(default_factory=list, max_length=8)
    reason: str = Field(default="", max_length=400)


class ResearchAction(BaseModel):
    """A bounded strategy change proposed after inspecting the current evidence."""

    kind: Literal[
        "REWRITE_QUERY",
        "SWITCH_SOURCE",
        "VERIFY_CLAIM",
        "STOP_WITH_LIMITATION",
    ]
    aspect: str = Field(min_length=1, max_length=240)
    stage_key: str = Field(default="", max_length=64)
    source_profiles: list[str] = Field(default_factory=list, max_length=8)
    query_intent: str = Field(default="", max_length=500)
    priority: int = Field(default=3, ge=1, le=5)
    reason: str = Field(default="", max_length=400)


class ResearchReflection(BaseModel):
    complete: bool
    follow_up_questions: list[str] = Field(default_factory=list, max_length=12)
    planned_follow_ups: list[ResearchSubquery] = Field(default_factory=list, max_length=12)
    answered_question_keys: list[str] = Field(default_factory=list, max_length=12)
    blocked_question_keys: list[str] = Field(default_factory=list, max_length=12)
    covered_aspects: list[str] = Field(default_factory=list, max_length=12)
    unresolved_points: list[str] = Field(default_factory=list, max_length=5)
    next_stage_key: str | None = Field(default=None, max_length=64)
    stage_reason: str = Field(default="", max_length=400)
    gaps: list[ResearchGap] = Field(default_factory=list, max_length=12)
    strategy_actions: list[ResearchAction] = Field(default_factory=list, max_length=12)
    budget_adjustments: list[ResearchBudgetAdjustment] = Field(
        default_factory=list,
        max_length=6,
    )


class ResearchSource(BaseModel):
    title: str = ""
    url: AnyHttpUrl
    canonical_url: str
    source: str = ""
    engines: list[str] = Field(default_factory=list)
    snippet: str = ""
    content_excerpt: str = ""
    evidence_archive: dict[str, object] = Field(default_factory=dict)
    relevance_score: float = 0.0
    quality_score: float = 0.0
    freshness_score: float = 0.0
    ranking_score: float = 0.0
    confidence: float = 0.0
    source_category: str = "OTHER"
    source_profiles: list[str] = Field(default_factory=list, max_length=8)
    matched_queries: list[str] = Field(default_factory=list)
    ranking_reasons: list[str] = Field(default_factory=list)
    duplicate_urls: list[str] = Field(default_factory=list)
    duplicate_of: str | None = None
    content_fingerprint: str = ""
    source_cluster_id: str = ""
    source_independence_score: float = Field(default=1.0, ge=0, le=1)
    read_status: Literal["NOT_SELECTED", "SUCCEEDED", "FAILED", "DUPLICATE"] = "NOT_SELECTED"
    retrieval_method: str = ""
    page_quality_score: float = Field(default=0.0, ge=0, le=100)
    page_quality_flags: list[str] = Field(default_factory=list)
    page_content_type: str = ""
    page_readability_applied: bool = False
    # Evidence strength is deliberately separate from search ranking. A high
    # ranked result may still be only a search snippet or a blocked page.
    evidence_level: Literal[
        "SEARCH_SNIPPET",
        "FULL_TEXT",
        "DIRECT_EVIDENCE",
        "UNVERIFIED",
    ] = "SEARCH_SNIPPET"
    evidence_density: float = Field(default=0.0, ge=0, le=1)
    error_code: str | None = None


class EvidencePassage(BaseModel):
    """A bounded, addressable piece of material that can support a claim."""

    passage_id: str = Field(min_length=1, max_length=160)
    source_revision_id: str | None = None
    source_url_or_uri: str = Field(min_length=1, max_length=2000)
    text: str = Field(min_length=1, max_length=12000)
    locator: dict[str, object] = Field(default_factory=dict)
    content_hash: str = Field(min_length=8, max_length=128)
    extracted_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    trust_metadata: dict[str, object] = Field(default_factory=dict)


class EvidenceLink(BaseModel):
    passage_id: str = Field(min_length=1, max_length=160)
    relation: Literal["SUPPORTS", "CONTRADICTS", "QUALIFIES"] = "SUPPORTS"
    quoted_text: str = Field(default="", max_length=2000)
    locator: dict[str, object] = Field(default_factory=dict)


class ResearchClaim(BaseModel):
    claim: str
    covers_aspects: list[str] = Field(default_factory=list, max_length=12)
    answers_aspects: list[str] = Field(
        default_factory=list, max_length=12,
        description="Subset of covers_aspects fully answered by this directly quoted fact, including requested conditions and exceptions; related or partial facts leave this empty.",
    )
    claim_id: str = ""
    normalized_key: str = ""
    time_scope: str = ""
    conditions: list[str] = Field(default_factory=list, max_length=12)
    evidence_links: list[EvidenceLink] = Field(default_factory=list, max_length=20)
    source_urls: list[AnyHttpUrl] = Field(default_factory=list)
    confidence: float = Field(ge=0, le=1)
    evidence_level: Literal[
        "SEARCH_SNIPPET",
        "FULL_TEXT",
        "DIRECT_EVIDENCE",
        "INFERRED_SUMMARY",
        "UNVERIFIED",
    ] = "UNVERIFIED"
    inference_note: str = Field(default="", max_length=300)
    status: Literal[
        "UNVERIFIED",
        "SUPPORTED",
        "PARTIALLY_SUPPORTED",
        "CONFLICTED",
        "OUTDATED",
        "REJECTED",
    ] = "UNVERIFIED"


class ResearchConflict(BaseModel):
    topic: str
    conflict_id: str = ""
    claim_key: str = ""
    positions: list[dict[str, object]] = Field(default_factory=list)
    difference_type: Literal["VALUE", "DATE", "SCOPE", "METHOD", "INTERPRETATION", "OTHER"] = (
        "OTHER"
    )
    resolution: Literal["UNRESOLVED", "USER_DECIDED", "RULE_RESOLVED"] = "UNRESOLVED"


class ResearchEvidenceAnalysis(BaseModel):
    claims: list[ResearchClaim] = Field(default_factory=list, max_length=20)
    conflicts: list[ResearchConflict] = Field(default_factory=list, max_length=10)
    uncertain_points: list[str] = Field(default_factory=list, max_length=10)
    processed_passage_ids: list[str] = Field(default_factory=list, max_length=40)


class EvidenceSpanChoice(BaseModel):
    """Model output contains addresses, never generated factual conclusions."""

    model_config = ConfigDict(extra="forbid")
    span_id: str = Field(min_length=1, max_length=180)
    covers_aspects: list[str] = Field(default_factory=list, max_length=12)


class EvidenceSpanChoices(BaseModel):
    model_config = ConfigDict(extra="forbid")
    selections: list[EvidenceSpanChoice] = Field(max_length=40)


class SelectedEvidence(BaseModel):
    evidence_id: str
    passage_id: str
    source_url_or_uri: str
    quote: str = Field(min_length=1, max_length=12000)
    context_before: str = Field(default="", max_length=12000)
    context_after: str = Field(default="", max_length=12000)
    covers_aspects: list[str] = Field(default_factory=list, max_length=12)
    question_keys: list[str] = Field(default_factory=list)
    locator: dict[str, object] = Field(default_factory=dict)
    trust_metadata: dict[str, object] = Field(default_factory=dict)
    coverage: Literal["candidate"] = "candidate"


class EvidenceSelectionResult(BaseModel):
    evidence: list[SelectedEvidence] = Field(default_factory=list)
    processed_passage_ids: list[str] = Field(default_factory=list)
    diagnostics: list[str] = Field(default_factory=list)


class AspectEvidenceVerdict(BaseModel):
    model_config = ConfigDict(extra="forbid")
    aspect: str = Field(min_length=1, max_length=500)
    status: Literal["SUPPORTED", "PARTIAL", "MISSING", "CONFLICTED"]
    evidence_ids: list[str] = Field(default_factory=list, max_length=20)
    reason: str = Field(min_length=2, max_length=800)
    missing_conditions: list[str] = Field(default_factory=list, max_length=10)


class AspectEvidenceReview(BaseModel):
    model_config = ConfigDict(extra="forbid")
    verdicts: list[AspectEvidenceVerdict] = Field(default_factory=list, max_length=12)


class ResearchCoverage(BaseModel):
    rounds: int = 0
    searched_queries: int = 0
    successful_queries: int = 0
    failed_queries: int = 0
    empty_queries: int = 0
    partial_queries: int = 0
    result_count: int = 0
    candidate_results: int = 0
    filtered_results: int = 0
    duplicate_results: int = 0
    diversity_dropped_results: int = 0
    filter_reasons: dict[str, int] = Field(default_factory=dict)
    selected_pages: int = 0
    read_pages: int = 0
    failed_pages: int = 0
    duplicate_pages: int = 0
    truncated_pages: int = 0
    source_cluster_count: int = 0
    independent_source_count: int = 0
    duplicate_source_count: int = 0
    summary_only_sources: int = 0
    full_text_sources: int = 0
    direct_evidence_sources: int = 0
    inferred_claims: int = 0
    covered_aspects: list[str] = Field(default_factory=list)
    material_aspects: list[str] = Field(default_factory=list)
    answerable_aspects: list[str] = Field(default_factory=list)
    uncovered_aspects: list[str] = Field(default_factory=list)


class ResearchQueryDiagnostic(BaseModel):
    # Stable query identity makes checkpoint resume idempotent. Legacy
    # checkpoints may omit it and are matched by normalized query text.
    key: str = ""
    query: str
    submitted_query: str | None = None
    status: Literal["SUCCEEDED", "PARTIAL", "EMPTY", "FAILED"]
    engines: list[str] = Field(default_factory=list)
    result_counts: dict[str, int] = Field(default_factory=dict)
    failures: list[SearchEngineFailure] = Field(default_factory=list)
    attempted_modes: list[str] = Field(default_factory=list, alias="attemptedModes")
    retry_count: int = Field(default=0, ge=0, alias="retryCount")
    retry_reason: str | None = Field(default=None, alias="retryReason")
    error_code: str | None = None
    provider_result_count: int = 0
    source_scope_dropped_count: int = 0
    ranked_candidate_count: int = 0
    filtered_count: int = 0
    duplicate_count: int = 0
    diversity_dropped_count: int = 0
    filter_reasons: dict[str, int] = Field(default_factory=dict)
    accepted_candidates: list[dict[str, object]] = Field(default_factory=list)
    rerank: dict[str, object] = Field(default_factory=dict)


class ResearchRoundMemory(BaseModel):
    round_index: int = Field(ge=0)
    new_passage_ids: list[str] = Field(default_factory=list, max_length=20)
    new_claim_ids: list[str] = Field(default_factory=list, max_length=20)
    conflict_ids: list[str] = Field(default_factory=list, max_length=10)
    covered_aspects: list[str] = Field(default_factory=list, max_length=12)
    unresolved_points: list[str] = Field(default_factory=list, max_length=10)
    summary: str = Field(default="", max_length=1200)


class ResearchStrategyAttempt(BaseModel):
    signature: str = Field(min_length=8, max_length=64)
    round_index: int = Field(ge=0)
    action_kind: Literal["REWRITE_QUERY", "SWITCH_SOURCE", "VERIFY_CLAIM"]
    aspect: str = Field(min_length=1, max_length=240)
    stage_key: str = Field(default="", max_length=64)
    question_key: str = Field(default="", max_length=80)
    query: str = Field(min_length=1, max_length=500)
    source_profiles: list[str] = Field(default_factory=list, max_length=8)
    outcome: Literal["PLANNED", "SEARCHED", "EVIDENCE_READY", "BLOCKED"] = "PLANNED"


class ResearchMemory(BaseModel):
    covered_aspects: list[str] = Field(default_factory=list, max_length=12)
    unresolved_points: list[str] = Field(default_factory=list, max_length=20)
    conflict_ids: list[str] = Field(default_factory=list, max_length=10)
    recent_rounds: list[ResearchRoundMemory] = Field(default_factory=list, max_length=6)
    strategy_attempts: list[ResearchStrategyAttempt] = Field(default_factory=list, max_length=20)
    consecutive_low_yield_rounds: int = Field(default=0, ge=0, le=3)


class ResearchPacket(BaseModel):
    run_id: str = Field(default_factory=lambda: str(uuid4()))
    question: str
    decision: ResearchDecision | None = None
    active_stage_key: str | None = None
    gaps: list[ResearchGap] = Field(default_factory=list, max_length=12)
    budget_adjustments: list[ResearchBudgetAdjustment] = Field(
        default_factory=list,
        max_length=20,
    )
    brief: ResearchBrief | None = None
    subqueries: list[ResearchSubquery] = Field(default_factory=list)
    sources: list[ResearchSource] = Field(default_factory=list)
    passages: list[EvidencePassage] = Field(default_factory=list)
    extracted_passage_ids: list[str] = Field(default_factory=list)
    evidence_mode: Literal["CLAIMS", "SPANS"] = "CLAIMS"
    selected_evidence: list[SelectedEvidence] = Field(default_factory=list)
    aspect_evidence_review: AspectEvidenceReview = Field(default_factory=AspectEvidenceReview)
    claims: list[ResearchClaim] = Field(default_factory=list)
    conflicts: list[ResearchConflict] = Field(default_factory=list)
    memory: ResearchMemory = Field(default_factory=ResearchMemory)
    uncertain_points: list[str] = Field(default_factory=list)
    coverage: ResearchCoverage = Field(default_factory=ResearchCoverage)
    search_diagnostics: list[ResearchQueryDiagnostic] = Field(default_factory=list)
    # Accumulated wall-clock time by orchestration stage, in milliseconds.
    # This is diagnostic metadata and does not affect research decisions.
    stage_timings_ms: dict[str, int] = Field(default_factory=dict)
    cache_context: dict[str, Any] = Field(default_factory=dict)
    stop_reason: str | None = None
    status: Literal["DISABLED", "SKIPPED", "READY", "PARTIAL", "FAILED"] = "DISABLED"


class ResearchEvidenceRead(BaseModel):
    id: str
    canonical_url: str
    url: str
    title: str
    source: str
    engines: list[str] = Field(default_factory=list)
    snippet: str
    content_excerpt: str
    relevance_score: float
    confidence: float
    ranking: dict = Field(default_factory=dict)
    read_status: str
    retrieval_method: str
    error_code: str | None = None
    created_at: datetime
    updated_at: datetime


class ResearchRunRead(BaseModel):
    id: str
    goal_id: str
    conversation_id: str | None = None
    question: str
    enabled: bool
    status: str
    current_stage: str
    resume_stage: str | None = None
    schema_version: int = 1
    attempt_count: int = 0
    recovery_count: int = 0
    idempotency_key: str | None = None
    lease_expires_at: datetime | None = None
    last_heartbeat_at: datetime | None = None
    cancel_requested_at: datetime | None = None
    cancel_reason: str | None = None
    decision: dict = Field(default_factory=dict)
    budget: dict = Field(default_factory=dict)
    checkpoint: dict = Field(default_factory=dict)
    result: ResearchPacket | None = None
    stop_reason: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    created_at: datetime
    started_at: datetime | None = None
    updated_at: datetime
    completed_at: datetime | None = None
    evidence: list[ResearchEvidenceRead] = Field(default_factory=list)


class ResearchRunCreate(BaseModel):
    question: str = Field(min_length=1, max_length=12000)
    enabled: bool = True
    conversation_id: str | None = Field(default=None, min_length=1, max_length=36)
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=180)
