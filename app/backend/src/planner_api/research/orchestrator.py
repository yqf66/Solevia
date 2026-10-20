from __future__ import annotations

import logging
import re
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from itertools import zip_longest
from threading import Event, Thread
from time import monotonic, sleep
from typing import Protocol, TypeVar
from urllib.parse import urlsplit

from planner_api.config import Settings, get_settings

from .archive_windows import archived_excerpt
from .engine_router import SUPPORTED_ENGINES, EngineRouter, parse_engine_pool
from .excerpts import archive_for_excerpt, focused_excerpt_window, source_read_focus
from .evidence import (
    build_snippet_fallback_analysis,
    build_web_passages,
    deduplicate_uncertainties,
    estimate_source_evidence_density,
    merge_evidence,
    merge_passages,
    refresh_evidence_metrics,
    validate_evidence_analysis,
)
from .intent import (
    ResearchDecisionGateway,
    ResearchDecisionProvider,
    ResearchIntentAnalyzer,
    build_research_decision_provider,
    build_research_intent_analyzer,
    request_signals_search,
)
from .memory import refresh_research_memory
from .model_contract import MODEL_FINISH_GRACE_SECONDS, model_deadline
from .page_quality import PageQualityAssessment, assess_page_quality
from .providers import ResearchProviderError
from .query_planner import (
    align_question_aspects,
    finalize_query_metadata,
    normalize_research_questions,
    repository_fallback_query,
    validate_question_dependencies,
)
from .ranking import (
    RankedHit,
    RankingPolicy,
    are_near_duplicate,
    canonicalize_url,
    classify_source,
    content_fingerprint,
    content_is_near_duplicate,
    rank_candidates,
    source_cluster_id,
    source_diversity_key,
)
from .schemas import (
    PageReadRequest,
    ResearchAction,
    ResearchBrief,
    ResearchDecision,
    ResearchEvidenceAnalysis,
    ResearchPacket,
    ResearchQueryConstraints,
    ResearchQueryDiagnostic,
    ResearchReflection,
    ResearchSource,
    ResearchStage,
    SearchRequest,
)
from .service import WebResearchService
from .sources import (
    compile_source_queries,
    extract_anchor_entities,
    infer_source_profiles,
    is_search_redirect_url,
    is_source_entry_url,
    normalize_source_profiles,
    official_documentation_domains,
    official_reference_urls,
    source_domains,
    source_engine_preferences,
    source_matches_requested_version,
    source_url_matches_profiles,
)
from .strategy import (
    actions_from_legacy_followups,
    refresh_research_coverage,
    diagnose_research_gaps,
    prepare_strategy_followups,
    update_low_yield_state,
    update_strategy_attempt_outcomes,
)

_COMMUNITY_SOURCE_PROFILES = {
    "community",
    "xiaohongshu",
    "zhihu",
    "tieba",
    "bilibili",
    "douban",
    "csdn",
    "juejin",
}


def _extract_explicit_urls(question: str) -> list[str]:
    """Return URLs explicitly supplied by the user, preserving their order."""
    values = re.findall(r"https?://[^\s<>\u3000]+", question, flags=re.IGNORECASE)
    cleaned: list[str] = []
    seen: set[str] = set()
    for value in values:
        value = value.rstrip(".,;:!?\u3002\uff0c\uff1b\uff1a\uff01\uff1f\uff09\u3011\u3009\u300b")
        if not value or value.casefold() in seen:
            continue
        seen.add(value.casefold())
        cleaned.append(value)
    return cleaned[:6]


def _has_explicit_url(question: str) -> bool:
    return bool(_extract_explicit_urls(question))


def _observed_source_profiles(url: str, requested: list[str]) -> list[str]:
    """Assign only profiles supported by the observed destination URL."""
    category = classify_source(url)
    category_profiles = {
        "DOCUMENTATION": {"official_docs"},
        "GOVERNMENT": {"government", "official_docs"},
        "ACADEMIC": {"arxiv"},
        "REPOSITORY": {"github"},
        "COMMUNITY": {"community"},
        "JOB_POSTING": {"recruitment"},
    }
    observed = set(category_profiles.get(category, set()))
    for profile in requested:
        if profile not in {"official_docs", "community", "government", "arxiv", "github"}:
            if source_url_matches_profiles(url, [profile]):
                observed.add(profile)
    return [profile for profile in requested if profile in observed][:8]

_T = TypeVar("_T")


def _bounded_call[T](callback: Callable[[], T], timeout: float) -> T:
    """Run a provider call without letting a stuck network request block research."""
    result: list[_T] = []
    error: list[BaseException] = []
    finished = Event()
    expired = Event()
    deadline = monotonic() + max(timeout, 0.1)

    def run() -> None:
        try:
            with model_deadline(deadline):
                value = callback()
            if not expired.is_set() and monotonic() <= deadline:
                result.append(value)
            else:
                logging.getLogger(__name__).info("research_model_late_result_discarded")
        except BaseException as exc:  # propagate provider errors to the caller
            error.append(exc)
        finally:
            finished.set()

    thread = Thread(target=run, name="research-provider-call", daemon=True)
    thread.start()
    if not finished.wait(max(0, deadline - monotonic())):
        expired.set()
        raise TimeoutError(f"research provider call exceeded {timeout:.1f}s")
    if error:
        raise error[0]
    if not result:
        raise TimeoutError(f"research provider call exceeded {timeout:.1f}s")
    return result[0]


def _explicit_search_requested(question: str) -> bool:
    return request_signals_search(question)


def _deterministic_fallback_plan(
    question: str,
    *,
    max_questions: int,
) -> tuple[ResearchBrief, list[str]]:
    """Keep an unavailable planner useful without inventing research facets."""
    normalized = " ".join(question.split())
    brief = ResearchBrief(objective=normalized[:500], required_aspects=[normalized[:240]])
    return brief, [normalized[:500]]



@dataclass(frozen=True)
class ResearchLimits:
    max_subqueries: int = 12
    max_queries_per_round: int = 3
    max_rounds: int = 5
    max_queries: int = 8
    results_per_query: int = 12
    max_sources: int = 15
    max_sources_per_domain: int = 3
    min_relevance_score: float = 18.0
    max_read_pages: int = 8
    max_pages_per_round: int = 4
    read_concurrency: int = 3
    max_page_chars: int = 8000
    max_total_page_chars: int = 30000
    enable_browser_fallback: bool = True
    max_runtime_seconds: float = 300.0
    max_failed_page_replacements: int = 4


class ResearchProgressObserver(Protocol):
    def __call__(self, stage: str, packet: ResearchPacket) -> None: ...


class ResearchOrchestrator:
    """Bounded Agent-side workflow for Open-WebSearch research."""

    def __init__(
        self,
        service: WebResearchService | None = None,
        limits: ResearchLimits | None = None,
        settings: Settings | None = None,
        intent_analyzer: ResearchIntentAnalyzer | None = None,
        decision_gateway: ResearchDecisionGateway | None = None,
        decision_provider: ResearchDecisionProvider | None = None,
    ) -> None:
        self.service = service or WebResearchService()
        settings = settings or get_settings()
        self.settings = settings
        self.engine_router = EngineRouter(
            pool=parse_engine_pool(settings.research_engine_pool),
            max_engines=settings.research_max_engines_per_query,
        )
        self.intent_analyzer = intent_analyzer or build_research_intent_analyzer(settings)
        self.decision_provider = (
            decision_provider
            or decision_gateway
            or build_research_decision_provider(
                settings,
                analyzer=self.intent_analyzer,
            )
        )
        # Compatibility alias for integrations created before DecisionProvider.
        self.decision_gateway = self.decision_provider
        self.limits = limits or ResearchLimits(
            max_subqueries=settings.research_max_subqueries,
            max_queries_per_round=settings.research_max_queries_per_round,
            max_rounds=settings.research_max_rounds,
            max_queries=settings.research_max_queries,
            results_per_query=settings.research_results_per_query,
            max_sources=settings.research_max_sources,
            max_sources_per_domain=settings.research_max_sources_per_domain,
            min_relevance_score=settings.research_min_relevance_score,
            max_read_pages=settings.research_max_read_pages,
            max_pages_per_round=settings.research_max_pages_per_round,
            read_concurrency=settings.research_read_concurrency,
            max_page_chars=settings.research_max_page_chars,
            max_total_page_chars=settings.research_max_total_page_chars,
            enable_browser_fallback=settings.research_browser_fallback_enabled,
            max_runtime_seconds=settings.research_timeout_seconds,
        )

    def research(
        self,
        question: str,
        *,
        enabled: bool | None = None,
        run_id: str | None = None,
        on_progress: ResearchProgressObserver | None = None,
        checkpoint: ResearchPacket | None = None,
        resume_stage: str | None = None,
        decision_context: dict[str, object] | None = None,
    ) -> ResearchPacket:
        normalized = " ".join(question.split())
        packet = (
            checkpoint.model_copy(deep=True)
            if checkpoint is not None
            else ResearchPacket(question=normalized, **({"run_id": run_id} if run_id else {}))
        )
        if run_id:
            packet.run_id = run_id
        if enabled is None:
            enabled = self.settings.web_research_enabled
        if not enabled:
            return packet

        if packet.decision is None:
            analysis_started = monotonic()
            self._notify(on_progress, "ANALYZING", packet)
            try:
                try:
                    decision = _bounded_call(
                        lambda: self.decision_provider.analyze(
                            question, context=decision_context
                        ),
                        min(
                            float(self.settings.research_decision_timeout_seconds) + MODEL_FINISH_GRACE_SECONDS,
                            self.limits.max_runtime_seconds,
                        ),
                    )
                except TypeError:
                    # Keep existing lightweight test/custom gateways compatible.
                    decision = _bounded_call(
                        lambda: self.decision_provider.analyze(question),
                        min(
                            float(self.settings.research_decision_timeout_seconds) + MODEL_FINISH_GRACE_SECONDS,
                            self.limits.max_runtime_seconds,
                        ),
                    )
            except Exception as error:
                if not (_explicit_search_requested(question) or _has_explicit_url(question)):
                    packet.status = "FAILED"
                    packet.stop_reason = "DECISION_FAILED"
                    packet.uncertain_points.append(
                        "检索决策未完成；没有确认本题无需联网，请重试或选择普通回答。"
                    )
                    packet.decision = None
                    return packet
                decision = ResearchDecision(
                    should_search=True,
                    knowledge_sufficiency="UNKNOWN",
                    search_value="HIGH",
                    reason=(
                        "检索决策未完成，已使用确定性多维拆解继续执行联网检索。"
                        f"（{type(error).__name__}）"
                    ),
                )
                packet.uncertain_points.append(
                    "需求分析模型不可用，已使用确定性多维拆解；结果仍需按来源和证据强度核验。"
                )
            self._record_stage_time(packet, "ANALYZING", analysis_started)
            packet.decision = decision
        else:
            decision = packet.decision
        if decision_context and not packet.cache_context:
            from planner_api.knowledge.integration import selected_cache
            packet.cache_context = selected_cache(packet, decision_context)
            packet.stage_timings_ms["CACHE_RECALL"] = int(decision_context.get("recall_ms", 0))
        cache_is_sufficient = bool(packet.cache_context and decision.cache_coverage == "SUFFICIENT"
            and not decision.cache_missing_aspects and not re.search(
                r"重新(?:联网|搜索|核实)|最新|实时|现行|截至|今天|明天|本周|这个周末|\b(?:latest|current|today|refresh)\b",
                question, re.I))
        if not decision.should_search and (
            _explicit_search_requested(question) or _has_explicit_url(question)
        ) and not cache_is_sufficient:
            # An explicit user request to browse is stronger than a transient
            # gate miss; keep the model's brief if present and let the bounded
            # planner decide the smallest useful research scope.
            decision.should_search = True
            decision.search_value = "HIGH"
            decision.knowledge_sufficiency = "INSUFFICIENT"
            decision.reason = (
                decision.reason.rstrip("。")
                + "；用户明确要求联网检索或提供了网址，已进入受预算约束的研究流程。"
            )
        decision.engine_group = [
            engine.casefold()
            for engine in decision.engine_group
            if engine.casefold() in SUPPORTED_ENGINES
        ][:6]
        if packet.brief is None:
            packet.brief = decision.brief
        if packet.brief is not None:
            packet.brief.source_profiles = normalize_source_profiles(packet.brief.source_profiles)
        explicit_urls = _extract_explicit_urls(normalized)
        if not decision.should_search:
            packet.status = "SKIPPED"
            return packet
        self._initialize_research_stages(packet)
        if not packet.subqueries:
            if packet.brief is None and "检索决策未完成" in decision.reason:
                # If the intent call is unavailable, keep explicit browsing
                # useful with one user-anchored query. This is only a
                # deterministic safety net; the
                # model remains the owner of the normal planning path.
                packet.brief, fallback_questions = _deterministic_fallback_plan(
                    ((str((decision_context or {}).get("previous_research", {}).get("question", ""))[:300]
                      or str((decision_context or {}).get("original_input", ""))[:300]) + "；" + normalized
                     if re.match(r"^(?:请联网)?(?:再|继续|刚才|那|它|这些|其他)", normalized) else normalized),
                    max_questions=self.limits.max_subqueries,
                )
                decision.research_questions = fallback_questions
            if not decision.planned_questions and not decision.research_questions:
                decision.research_questions = [question]
            packet.subqueries = normalize_research_questions(
                decision.planned_questions or decision.research_questions,
                max_subqueries=self.limits.max_subqueries,
            )
        previous_context = (decision_context or {}).get("previous_research", {})
        inherited = previous_context if decision.topic_relation == "FOLLOW_UP" else {}
        previous_brief = (inherited.get("brief") or {}) if decision.inherit_source_restrictions else {}
        scope_text = " ".join([normalized, *(packet.brief.constraints if packet.brief else []),
                               *previous_brief.get("constraints", [])])
        official_only = bool(re.search(r"只(?:看|用|根据|参考)(?:官方|官网)|仅(?:限|用|看)(?:官方|官网)|official sources only", scope_text, re.I))
        if re.search(r"不限(?:于)?(?:官方|官网)|不(?:是|必|用|再)?只(?:看|用)(?:官方|官网)|不再限于(?:官方|官网)", normalized):
            official_only = False
        if official_only and packet.brief and "只看官方来源" not in packet.brief.constraints:
            packet.brief.constraints = [*packet.brief.constraints, "只看官方来源"][:12]
        previous_entities = list(dict.fromkeys(e for q in inherited.get("queries", [])
                                              for e in q.get("anchor_entities", []) or []))[:6]
        for item in packet.subqueries:
            # A missing subject is recoverable; never overwrite a new subject or
            # indiscriminately merge every entity in a comparison into every query.
            if previous_entities and not item.anchor_entities and not item.constraints.entities:
                if not any(e.casefold() in item.query.casefold() for e in previous_entities):
                    item.query = (" ".join(previous_entities) + " " + item.query)[:500]
                item.anchor_entities = previous_entities
            if official_only:
                item.source_scope_mode = "HARD"
                item.source_profiles = ["official_docs", "government"]
            if item.source_scope_mode == "SOFT":
                # A fine-tuned preference must not generate domains/site filters.
                if not item.source_strategy_hint and item.source_profiles:
                    profiles = set(item.source_profiles)
                    item.source_strategy_hint = (
                        "official" if profiles <= {"official_docs", "government", "github"}
                        else "academic" if profiles == {"arxiv"}
                        else "community" if profiles <= _COMMUNITY_SOURCE_PROFILES | {"linuxdo"}
                        else "balanced"
                    )
                item.source_profiles = []
                continue
            item.anchor_entities = list(
                dict.fromkeys(
                    [
                        *item.anchor_entities,
                        *extract_anchor_entities(item.query),
                    ]
                )
            )[:6]
            explicit_profiles = normalize_source_profiles(item.source_profiles)
            routing_text = " ".join(
                (item.query, item.query_intent, item.primary_aspect)
            )
            inferred_profiles = normalize_source_profiles(
                infer_source_profiles(routing_text, item.purpose, packet.brief)
            )
            if explicit_profiles:
                item.source_profiles = explicit_profiles[:6]
            else:
                item.source_profiles = inferred_profiles
        finalize_query_metadata(
            packet.subqueries,
            packet.brief.required_aspects if packet.brief else [],
        )
        self._assign_questions_to_stages(packet)
        self._seed_explicit_url_sources(packet, explicit_urls)
        self._seed_previous_read_sources(packet, decision_context)
        self._seed_reference_sources(packet, extended=True)
        if not packet.subqueries:
            packet.status = "FAILED"
            packet.uncertain_points.append("模型判断需要联网，但没有提炼出可检索的问题。")
            return packet
        dependency_diagnostics = validate_question_dependencies(packet.subqueries)
        packet.uncertain_points = list(
            dict.fromkeys([*packet.uncertain_points, *dependency_diagnostics])
        )
        if packet.brief is not None:
            align_question_aspects(packet.subqueries, packet.brief.required_aspects)
            finalize_query_metadata(packet.subqueries, packet.brief.required_aspects)
            self._assign_questions_to_stages(packet)
            packet.coverage.uncovered_aspects = list(packet.brief.required_aspects)

        round_limit = self._effective_round_limit(packet)
        deadline = monotonic() + self.limits.max_runtime_seconds
        self._read_deadline = deadline

        searched_keys, searched_queries = self._searched_query_identity(packet)
        pending = [
            item
            for item in packet.subqueries
            if item.key not in searched_keys and item.query.casefold() not in searched_queries
        ]
        pending = self._ready_questions(packet, pending)
        completed = False
        empty_repair_used = False
        resume_round = checkpoint is not None and bool(packet.decision)
        if checkpoint is not None and not pending:
            self._notify(on_progress, "READING", packet)
            reading_started = monotonic()
            newly_read = self._read_selected(packet)
            self._record_stage_time(packet, "READING", reading_started)
            self._notify(on_progress, "CHECKPOINT", packet)
            extraction_started = monotonic()
            self._extract_evidence(packet)
            self._record_stage_time(packet, "EXTRACTING", extraction_started)
            self._update_question_evidence_status(packet)
            if packet.coverage.read_pages:
                reflection_started = monotonic()
                completed, pending = self._reflect(packet, on_progress)
                self._record_stage_time(packet, "REFLECTING", reflection_started)
                resume_round = False
        while pending and (resume_round or packet.coverage.rounds < round_limit):
            if monotonic() >= deadline:
                packet.stop_reason = "TOTAL_TIMEOUT"
                break
            if not resume_round:
                packet.coverage.rounds += 1
            search_started = monotonic()
            resume_round = False
            self._notify(on_progress, "SEARCHING", packet)
            round_ranked: list[list[RankedHit]] = []
            remaining_queries = min(
                self.limits.max_queries - packet.coverage.searched_queries,
                self._stage_query_remaining(packet),
            )
            batch_limit = min(self.limits.max_queries_per_round, remaining_queries)
            if batch_limit <= 0:
                if self._advance_stage(packet):
                    searched_keys, searched_queries = self._searched_query_identity(packet)
                    pending = self._ready_questions(
                        packet,
                        [
                            item
                            for item in packet.subqueries
                            if item.key not in searched_keys
                            and item.query.casefold() not in searched_queries
                        ],
                    )
                    continue
                packet.stop_reason = "STAGE_BUDGET"
                break
            round_request_count = 0
            deferred_queries = []
            selected_questions = self._select_query_batch(packet, pending, batch_limit)
            # Reserve at least one real provider call for each aspect in the
            # round. A source-first question may have several variants, but
            # one broad question must not consume the entire round budget.
            variant_budget = max(1, batch_limit // max(len(selected_questions), 1))
            for subquery in selected_questions:
                fallback = (
                    None if subquery.source_scope_mode == "SOFT"
                    else repository_fallback_query(subquery.query)
                )
                compiled_queries = compile_source_queries(
                    subquery,
                    original_question=packet.question,
                    brief=packet.brief,
                )
                if subquery.source_scope_mode == "SOFT":
                    compiled_queries = []
                source_first = any(
                        profile in {
                        'github', 'arxiv', 'government'
                        }
                        for profile in subquery.source_profiles
                    )
                soft_source = any(
                    profile in _COMMUNITY_SOURCE_PROFILES
                    for profile in subquery.source_profiles
                )
                recruitment_source = "recruitment" in subquery.source_profiles
                submitted_queries = (
                    [*compiled_queries, subquery.query]
                    if source_first
                    else (
                        [*compiled_queries[:2], subquery.query]
                        if recruitment_source
                        else (compiled_queries[:1] or [subquery.query])
                    )
                    if soft_source or recruitment_source
                    else [
                        subquery.query,
                        *(
                            item
                            for item in compiled_queries
                            if ("site:" in item.casefold() or item.count('"') >= 2)
                            and fallback is None
                        ),
                    ]
                )
                submitted_queries = list(dict.fromkeys(submitted_queries))
                has_repository_variant = any(
                    "site:github.com" in item.casefold() for item in submitted_queries
                )
                if (
                    fallback
                    and not has_repository_variant
                    and fallback.casefold() not in {item.casefold() for item in submitted_queries}
                ):
                    submitted_queries.append(fallback)
                if source_first and len(selected_questions) > 1:
                    # Breadth comes first when several source questions are
                    # scheduled in one round. Keep one natural-language query
                    # per question; a later reflection can spend a dedicated
                    # round on site-constrained variants if needed.
                    submitted_queries = [subquery.query]
                submitted_queries = submitted_queries[:variant_budget]
                for submitted_query in submitted_queries:
                    if (
                        round_request_count >= batch_limit
                        or packet.coverage.searched_queries >= self.limits.max_queries
                        or self._stage_query_remaining(packet) <= 0
                    ):
                        break
                    round_request_count += 1
                    if variant_budget == 1 and len(selected_questions) > 1:
                        deferred_queries.append((subquery, submitted_query,
                            source_first and "site:" in submitted_query.casefold()))
                        continue
                    accepted = self._search_question(
                        packet,
                        subquery,
                        submitted_query,
                        enforce_source=source_first and "site:" in submitted_query.casefold(),
                    )
                    strong_accepted = [
                        item for item in accepted if "SNIPPET_FALLBACK" not in item.reasons
                    ]
                    if source_first:
                        for item in accepted:
                            item.source_profiles = _observed_source_profiles(
                                str(item.hit.url), subquery.source_profiles
                            )
                    round_ranked.append(accepted)
                    # Hard source research may try a constrained and a broad
                    # form. Soft community research deliberately spends only
                    # one query here so other required aspects keep a slot;
                    # reflection can later issue a materially different retry.
                    if (
                        not source_first
                        and (
                            strong_accepted
                            or packet.search_diagnostics[-1].status == "FAILED"
                        )
                    ):
                        break
            if deferred_queries:
                round_ranked.extend(self._search_query_batch(packet, deferred_queries))
            self._record_stage_time(packet, "SEARCHING", search_started)
            source_count_before = len(packet.sources)
            claim_count_before = len(packet.claims)
            selected_count_before = len(packet.selected_evidence)
            summary_count_before = packet.coverage.summary_only_sources
            self._merge_sources(packet, round_ranked)
            self._notify(on_progress, "READING", packet)
            reading_started = monotonic()
            newly_read = self._read_selected(packet)
            self._record_stage_time(packet, "READING", reading_started)
            self._notify(on_progress, "CHECKPOINT", packet)
            # Search snippets remain usable weak evidence even when every
            # selected detail page is blocked. Always run the bounded evidence
            # pass; it is a no-op when there are no new passages.
            extraction_started = monotonic()
            self._extract_evidence(packet)
            self._record_stage_time(packet, "EXTRACTING", extraction_started)
            self._update_question_evidence_status(packet)
            has_new_material = bool(
                newly_read
                or len(packet.sources) > source_count_before
                or len(packet.claims) > claim_count_before
                or len(packet.selected_evidence) > selected_count_before
                or packet.coverage.summary_only_sources > summary_count_before
            )
            if not has_new_material:
                searched_keys, searched_queries = self._searched_query_identity(packet)
                pending = self._ready_questions(
                    packet,
                    [
                        item
                        for item in packet.subqueries
                        if item.key not in searched_keys
                        and item.query.casefold() not in searched_queries
                    ],
                )
                if (
                    pending
                    and packet.coverage.rounds < round_limit
                    and packet.coverage.searched_queries < self.limits.max_queries
                ):
                    continue
                if (
                    packet.coverage.rounds < round_limit
                    and packet.coverage.searched_queries < self.limits.max_queries
                    and self._advance_stage(packet)
                ):
                    searched_keys, searched_queries = self._searched_query_identity(packet)
                    pending = self._ready_questions(
                        packet,
                        [
                            item
                            for item in packet.subqueries
                            if item.key not in searched_keys
                            and item.query.casefold() not in searched_queries
                        ],
                    )
                    continue
                if (
                    not empty_repair_used
                    and packet.coverage.rounds < round_limit
                    and packet.coverage.searched_queries < self.limits.max_queries
                    and monotonic() < deadline
                ):
                    empty_repair_used = True
                    repair = self._fallback_reflection(packet, RuntimeError("no new material"))
                    followups, _ = prepare_strategy_followups(
                        packet, repair.strategy_actions,
                        max_new=max(self.limits.max_subqueries - len(packet.subqueries), 0),
                    )
                    if followups:
                        packet.subqueries.extend(followups)
                        self._assign_questions_to_stages(packet)
                        pending = self._ready_questions(packet, followups)
                        if pending:
                            continue
                packet.stop_reason = "NO_NEW_EVIDENCE"
                break
            reflection_started = monotonic()
            reflection_complete, _ = self._reflect(packet, on_progress)
            self._record_stage_time(packet, "REFLECTING", reflection_started)
            if packet.stop_reason in {"LOW_VALUE_SATURATION", "MODEL_LOW_VALUE_STOP"}:
                break
            searched_keys, searched_queries = self._searched_query_identity(packet)
            pending = self._ready_questions(
                packet,
                [
                    item
                    for item in packet.subqueries
                    if item.key not in searched_keys
                    and item.query.casefold() not in searched_queries
                ],
            )
            unresolved_gaps = any(
                gap.gap_type
                in {
                    "MISSING_EVIDENCE",
                    "WEAK_AUTHORITY",
                    "LOW_DIVERSITY",
                    "ACCESS_FAILURE",
                    "CONFLICT",
                    "STALE_EVIDENCE",
                }
                for gap in packet.gaps
            ) or any(c.resolution == "UNRESOLVED" for c in packet.conflicts)
            planned_stages = any(
                stage.status == "PLANNED"
                for stage in (packet.decision.research_stages if packet.decision else [])
            )
            if (
                reflection_complete
                and not unresolved_gaps
                and not planned_stages
                and not self._has_required_pending(packet)
            ):
                self._finalize_active_stage(packet)
                completed = True
                packet.stop_reason = "MODEL_COMPLETE"
                break
            # A reflection may add follow-up questions to a stage whose query
            # budget is already exhausted. Advance immediately so the next
            # loop round performs useful work instead of spending one round
            # merely discovering the exhausted budget.
            if self._stage_query_remaining(packet) <= 0 and self._advance_stage(packet):
                searched_keys, searched_queries = self._searched_query_identity(packet)
                pending = self._ready_questions(
                    packet,
                    [
                        item
                        for item in packet.subqueries
                        if item.key not in searched_keys
                        and item.query.casefold() not in searched_queries
                    ],
                )
            if monotonic() >= deadline:
                packet.stop_reason = "TOTAL_TIMEOUT"
                break
            if packet.coverage.rounds >= round_limit:
                packet.stop_reason = "ROUND_BUDGET"
                break
            if packet.coverage.searched_queries >= self.limits.max_queries:
                packet.stop_reason = "QUERY_BUDGET"
                break
            if self._attempted_pages(packet) >= self.limits.max_read_pages:
                packet.stop_reason = "PAGE_BUDGET"
                break
            if not pending and self._advance_stage(packet):
                searched_keys, searched_queries = self._searched_query_identity(packet)
                pending = self._ready_questions(
                    packet,
                    [
                        item
                        for item in packet.subqueries
                        if item.key not in searched_keys
                        and item.query.casefold() not in searched_queries
                    ],
                )
                if pending:
                    continue
            if not pending:
                packet.stop_reason = "NO_NEW_QUERIES"
                break

        self._update_aspect_coverage(packet)
        self._finalize_active_stage(packet)
        stages = packet.decision.research_stages if packet.decision else []
        if stages and all(stage.status == "COMPLETE" for stage in stages):
            completed = not self._has_required_pending(packet)
        if not packet.coverage.successful_queries:
            packet.status = "FAILED"
            packet.uncertain_points.append("所有搜索请求均未完成。")
        elif not packet.coverage.read_pages:
            packet.status = "PARTIAL"
            packet.uncertain_points.append(
                "仅取得搜索摘要等有限线索，未读取到网页正文；"
                "可作方向性判断，不宜表述为确定事实。"
                if packet.coverage.summary_only_sources
                else "搜索未取得可读取的网页证据。"
            )
        else:
            packet.status = "READY" if completed else "PARTIAL"
        self._update_aspect_coverage(packet)
        if packet.coverage.uncovered_aspects:
            packet.status = "PARTIAL"
            packet.uncertain_points.append(
                "以下必答方面尚无足够证据覆盖：" + "、".join(packet.coverage.uncovered_aspects)
            )
        if packet.coverage.successful_queries < packet.coverage.searched_queries:
            packet.uncertain_points.append("部分搜索请求失败，覆盖范围可能不完整。")
        if packet.coverage.partial_queries:
            packet.uncertain_points.append("部分搜索引擎失败，覆盖范围可能不完整。")
        if packet.coverage.failed_pages:
            packet.uncertain_points.append(
                f"有 {packet.coverage.failed_pages} 个页面读取失败，结论可能不完整。"
            )
        # Claims can be merged during the final evidence pass after the last
        # reflection. Reconcile gaps once more so a formerly missing aspect is
        # not exposed as missing when it now has usable evidence.
        packet.gaps = diagnose_research_gaps(packet, packet.gaps)
        if packet.status == "PARTIAL" and packet.stop_reason in {
            "ROUND_BUDGET",
            "QUERY_BUDGET",
            "PAGE_BUDGET",
            "NO_NEW_QUERIES",
            "TOTAL_TIMEOUT",
            "MODEL_TIMEOUT",
        }:
            packet.uncertain_points.append("研究已达到停止条件，仍可能有未解决的问题。")
        packet.uncertain_points = deduplicate_uncertainties(packet.uncertain_points)
        refresh_research_memory(packet)
        self._notify(on_progress, "COMPLETED", packet)
        return packet

    def _prepare_search_request(self, packet, subquery, submitted_query, *, enforce_source=False):
        official_domains = (
            official_documentation_domains(
                subquery.query_intent + " " + subquery.query,
                extended=True,
            )
            if "official_docs" in subquery.source_profiles and subquery.source_scope_mode != "SOFT" else ()
        )
        if official_domains:
            terms = re.findall(r"[A-Za-z][A-Za-z0-9_.-]*|\d+\.\d+", subquery.query)
            terms = list(dict.fromkeys(term for term in terms
                         if term.casefold() not in {"official", "docs", "documentation"}
                         and not term.casefold().startswith("docs.")))
            submitted_query = f"site:{official_domains[0]} " + " ".join(terms[:10])
            enforce_source = True
        elif (
            "official_docs" in subquery.source_profiles
            and subquery.query_role in {"AUTHORITATIVE", "VERIFY"}
            and subquery.source_scope_mode != "SOFT"
            and not repository_fallback_query(subquery.query)
            and "site:" not in submitted_query.casefold()
        ):
            for reference in subquery.reference_urls[:1]:
                parsed = urlsplit(str(reference))
                if parsed.scheme in {"http", "https"} and parsed.hostname and source_url_matches_profiles(
                    str(reference), ["official_docs"]
                ):
                    # Unknown publisher hints narrow discovery only. They do
                    # not get the trusted-documentation label or direct-read bypass.
                    submitted_query = f"site:{parsed.hostname} {submitted_query}"
                    break
        previous_engines = next(
            (item.engines for item in packet.search_diagnostics if item.engines), []
        )
        failed_engines = {
            failure.engine for item in packet.search_diagnostics for failure in item.failures
        }
        # An engine that repeatedly returns zero results is effectively
        # unavailable even when the provider did not emit a formal failure.
        # Cool it down for the remainder of this run so later rounds spend
        # their budget on engines that actually contribute evidence.
        empty_counts: dict[str, int] = {}
        for diagnostic in packet.search_diagnostics:
            for engine in diagnostic.engines:
                if diagnostic.result_counts.get(engine, 0) <= 0:
                    empty_counts[engine] = empty_counts.get(engine, 0) + 1
        failed_engines.update(
            engine for engine, count in empty_counts.items() if count >= 2
        )
        surviving_previous = [engine for engine in previous_engines if engine not in failed_engines]
        preferred_engines = [
            engine
            for engine in source_engine_preferences(subquery.source_profiles)
            if engine not in failed_engines
        ]
        if preferred_engines:
            configured = set(self.engine_router.pool)
            available_engines = [
                engine for engine in preferred_engines if engine in configured
            ][: self.engine_router.max_engines]
            if not available_engines:
                available_engines = self.engine_router.select(
                    purpose=subquery.purpose,
                    failed=failed_engines,
                    candidates=[*preferred_engines, *surviving_previous],
                    strict_candidates=False,
                )
        else:
            model_engines = packet.decision.engine_group if packet.decision else []
            available_engines = self.engine_router.select(
                purpose=subquery.purpose,
                failed=failed_engines,
                candidates=[*model_engines, *surviving_previous],
                strict_candidates=False,
            )
        if subquery.query_role in {"AUTHORITATIVE", "VERIFY"} and set(
            subquery.source_profiles
        ) & {"official_docs", "government"} and not repository_fallback_query(subquery.query):
            general_engines = [
                engine for engine in available_engines
                if engine not in {"csdn", "juejin", "linuxdo"}
            ]
            if general_engines:
                available_engines = general_engines
        return (SearchRequest(query=submitted_query, limit=self.limits.results_per_query,
                              engines=available_engines), submitted_query, enforce_source, official_domains)

    def _search_query_batch(self, packet, jobs):
        # Only independent, already-ready queries with one variant enter here.
        # Provider I/O overlaps; ranking/BGE and packet mutations remain ordered.
        prepared = [self._prepare_search_request(packet, q, text, enforce_source=enforce)
                    for q, text, enforce in jobs]
        with ThreadPoolExecutor(max_workers=min(3, len(jobs)), thread_name_prefix="web-query") as pool:
            futures = [pool.submit(self.service.search, item[0]) for item in prepared]
            ranked = []
            for (q, text, enforce), item, future in zip(jobs, prepared, futures, strict=True):
                accepted = self._search_question(packet, q, text, enforce_source=enforce,
                                                prefetched=(*item, future))
                if any(p in {"github", "arxiv", "government"} for p in q.source_profiles):
                    for hit in accepted:
                        hit.source_profiles = _observed_source_profiles(str(hit.hit.url), q.source_profiles)
                ranked.append(accepted)
        return ranked

    def _search_question(
        self,
        packet: ResearchPacket,
        subquery,
        submitted_query: str,
        *,
        enforce_source: bool = False,
        prefetched=None,
    ) -> list[RankedHit]:
        packet.coverage.searched_queries += 1
        subquery.status = "SEARCHED"
        request, submitted_query, enforce_source, official_domains = (
            prefetched[:4] if prefetched is not None else
            self._prepare_search_request(packet, subquery, submitted_query, enforce_source=enforce_source))
        try:
            response = prefetched[4].result() if prefetched is not None else self.service.search(request)
        except Exception as error:
            packet.coverage.failed_queries += 1
            subquery.status = "BLOCKED"
            packet.search_diagnostics.append(
                ResearchQueryDiagnostic(
                    key=subquery.key,
                    query=subquery.query,
                    submitted_query=submitted_query,
                    status="FAILED",
                    error_code=(
                        error.code
                        if isinstance(error, ResearchProviderError)
                        else "SEARCH_REQUEST_FAILED"
                    ),
                )
            )
            return []
        failures = {failure.engine for failure in response.partial_failures}
        if not response.results and response.engines and failures.issuperset(response.engines):
            packet.coverage.failed_queries += 1
            status = "FAILED"
        elif response.partial_failures:
            packet.coverage.partial_queries += 1
            packet.coverage.successful_queries += 1
            status = "PARTIAL"
        elif not response.results:
            packet.coverage.empty_queries += 1
            packet.coverage.successful_queries += 1
            status = "EMPTY"
        else:
            packet.coverage.successful_queries += 1
            status = "SUCCEEDED"
        diagnostic = ResearchQueryDiagnostic(
                key=subquery.key,
                query=subquery.query,
                submitted_query=submitted_query,
                status=status,
                engines=response.engines,
                result_counts=response.result_counts,
                failures=response.partial_failures,
                attempted_modes=response.attempted_modes,
                retry_count=response.retry_count,
                retry_reason=response.retry_reason,
                error_code="ALL_ENGINES_FAILED" if status == "FAILED" else None,
                provider_result_count=len(response.results),
            )
        packet.search_diagnostics.append(diagnostic)
        if status == "FAILED":
            subquery.status = "BLOCKED"
            return []
        packet.coverage.result_count += len(response.results)
        results = response.results
        unscoped_results = list(results)
        provider_result_count = len(results)
        if (enforce_source or subquery.source_scope_mode == "HARD") and subquery.source_profiles:
            allowed = official_domains or (
                () if subquery.source_scope_mode == "HARD" and "official_docs" in subquery.source_profiles
                else source_domains(subquery.source_profiles)
            )
            if allowed:
                scoped_results = [
                    hit for hit in results
                    if is_search_redirect_url(str(hit.url)) or (
                        not is_source_entry_url(str(hit.url), subquery.source_profiles)
                        and any(
                            host == domain or host.endswith(f".{domain}")
                            for domain in allowed
                            for host in [
                                (urlsplit(str(hit.url)).hostname or "")
                                .casefold()
                                .removeprefix("www.")
                            ]
                        )
                    )
                ]
                diagnostic.source_scope_dropped_count = len(results) - len(scoped_results)
                results = scoped_results
            elif subquery.source_scope_mode == "HARD":
                results = [hit for hit in results if source_url_matches_profiles(str(hit.url), subquery.source_profiles)]
                diagnostic.source_scope_dropped_count = len(unscoped_results) - len(results)
        # Semantic conditions such as “official opening hours” describe what
        # the page must establish, not words that a search title must repeat.
        # Keep exact identifiers, versions and user-quoted text hard, while
        # allowing a page titled “预约须知” to satisfy “提前预约要求” after
        # its正文 is read and verified.
        from .reranking import recall_constraints
        ranking_constraints = recall_constraints(subquery.constraints, packet.question)
        if subquery.source_scope_mode == "SOFT":
            from .laya_provider import discovery_constraints

            ranking_constraints = discovery_constraints(ranking_constraints)
        baseline_urls = None
        if self.settings.research_rerank_mode == "BGE":
            baseline = rank_candidates(
                subquery.query, results, max_results=24, purpose=subquery.purpose,
                policy=RankingPolicy(min_relevance_score=self.limits.min_relevance_score,
                    max_per_domain=24, allow_search_redirects=enforce_source,
                    allow_exploratory_entity_mismatch=subquery.query_role in {"COMMUNITY", "DISCOVERY"},
                    allow_source_intent_mismatch=subquery.source_scope_mode == "SOFT"),
                constraints=ranking_constraints, query_role=subquery.query_role,
            )
            baseline_urls = {item.canonical_url for item in baseline.accepted}
            ranking_constraints = recall_constraints(subquery.constraints, packet.question)
        outcome = rank_candidates(
            subquery.query,
            results,
            max_results=24 if self.settings.research_rerank_mode == "BGE" else self.limits.max_sources,
            purpose=subquery.purpose,
            policy=RankingPolicy(
                # Semantic matching needs a chance to inspect paraphrases.
                # Explicit entities/versions/strict terms remain hard gates.
                min_relevance_score=0 if self.settings.research_rerank_mode == "BGE" else self.limits.min_relevance_score,
                max_per_domain=24 if self.settings.research_rerank_mode == "BGE" else self.limits.max_sources_per_domain,
                allow_search_redirects=enforce_source,
                allow_partial_overlap=self.settings.research_rerank_mode == "BGE",
                enforce_exact_terms=self.settings.research_rerank_mode == "BGE",
                allow_exploratory_entity_mismatch=subquery.query_role
                in {"COMMUNITY", "DISCOVERY"},
                allow_snippet_fallback=True,
                allow_source_intent_mismatch=(subquery.source_scope_mode == "SOFT"
                                             or self.settings.research_rerank_mode == "BGE"),
            ),
            constraints=ranking_constraints,
            query_role=subquery.query_role,
        )
        if not outcome.accepted and results:
            # Keep a small candidate pool when strict lexical overlap rejects
            # everything.  Page-quality checks and evidence validation remain
            # the hard gate; this only prevents a broad query from becoming an
            # empty research stage before a page can be inspected.
            relaxed = rank_candidates(
                subquery.query,
                results,
                max_results=self.limits.max_sources,
                purpose=subquery.purpose,
                policy=RankingPolicy(
                    min_relevance_score=max(8.0, self.limits.min_relevance_score * 0.55),
                    max_per_domain=self.limits.max_sources_per_domain,
                    allow_search_redirects=enforce_source,
                    allow_partial_overlap=True,
                    enforce_exact_terms=self.settings.research_rerank_mode == "BGE",
                    allow_source_intent_mismatch=True,
                    allow_exploratory_entity_mismatch=subquery.query_role
                    in {"COMMUNITY", "DISCOVERY"},
                    allow_snippet_fallback=True,
                ),
                constraints=ranking_constraints,
                query_role=subquery.query_role,
            )
            for item in relaxed.accepted:
                item.reasons.append("RELAXED_RELEVANCE_CANDIDATE")
            outcome.accepted = relaxed.accepted
            # Keep a small, clearly marked weak-evidence tail when strict
            # constraints rejected otherwise informative search snippets.
            outcome.accepted.extend(
                outcome.snippet_fallback[: max(0, 3 - len(outcome.accepted))]
            )
        elif outcome.snippet_fallback:
            outcome.accepted.extend(
                outcome.snippet_fallback[: max(0, 3 - len(outcome.accepted))]
            )
        if subquery.source_scope_mode != "HARD" and diagnostic.source_scope_dropped_count and unscoped_results:
            scoped_urls = {canonicalize_url(str(item.url)) for item in results}
            excluded = [
                item
                for item in unscoped_results
                if canonicalize_url(str(item.url)) not in scoped_urls
            ]
            if excluded:
                # A source-profile mismatch blocks authority promotion, but it
                # must not erase a useful search summary. Retain only a small
                # weak-evidence tail and never send it to page reading.
                scope_fallback = rank_candidates(
                    subquery.query,
                    excluded,
                    max_results=3,
                    purpose=subquery.purpose,
                    policy=RankingPolicy(
                        min_relevance_score=0,
                        max_per_domain=self.limits.max_sources_per_domain,
                        allow_search_redirects=True,
                        allow_partial_overlap=True,
                        allow_source_intent_mismatch=True,
                        allow_exploratory_entity_mismatch=True,
                        allow_snippet_fallback=True,
                    ),
                    constraints=ResearchQueryConstraints(),
                    query_role="DISCOVERY",
                )
                scope_items = [*scope_fallback.accepted, *scope_fallback.snippet_fallback]
                for item in scope_items:
                    item.reasons = list(
                        dict.fromkeys(
                            [*item.reasons, "SNIPPET_FALLBACK", "SOURCE_PROFILE_MISMATCH"]
                        )
                    )
                outcome.accepted.extend(scope_items[: max(0, 3 - len(outcome.accepted))])
        if subquery.source_scope_mode == "SOFT" and subquery.source_strategy_hint:
            from .laya_provider import soft_source_score

            for item in outcome.accepted:
                bonus = soft_source_score(item.source_category, subquery.source_strategy_hint)
                if bonus:
                    item.score += bonus
                    item.reasons.append("LAYA_SOFT_SOURCE_PREFERENCE")
            outcome.accepted.sort(key=lambda item: -item.score)
        if self.settings.research_rerank_mode == "BGE":
            from .reranking import limit_candidates, rerank_candidates

            strong = [item for item in outcome.accepted if "SNIPPET_FALLBACK" not in item.reasons]
            weak = [item for item in outcome.accepted if "SNIPPET_FALLBACK" in item.reasons]
            ranked, diagnostic.rerank = rerank_candidates(
                subquery.query_intent or subquery.query, strong, self.settings,
            )
            if diagnostic.rerank["status"] != "APPLIED":
                # A cold/failed BGE must not authorize reading extra low-lexical
                # candidates. Restore the conservative program admission gate.
                ranked = [item for item in ranked if item.canonical_url in baseline_urls]
            limited = limit_candidates(ranked, max_results=self.limits.max_sources,
                                       max_per_domain=self.limits.max_sources_per_domain)
            outcome.diversity_dropped += len(ranked) - len(limited)
            outcome.accepted = limited + weak[:max(0, 3 - len(limited))]
        strong_accepted = [
            item for item in outcome.accepted if "SNIPPET_FALLBACK" not in item.reasons
        ]
        packet.coverage.candidate_results += len(strong_accepted)
        packet.coverage.filtered_results += outcome.filtered_count
        packet.coverage.duplicate_results += outcome.duplicate_count
        packet.coverage.diversity_dropped_results += outcome.diversity_dropped
        for reason, count in outcome.filter_reasons.items():
                packet.coverage.filter_reasons[reason] = (
                    packet.coverage.filter_reasons.get(reason, 0) + count
                )
        diagnostic.provider_result_count = provider_result_count
        diagnostic.ranked_candidate_count = len(strong_accepted)
        diagnostic.filtered_count = outcome.filtered_count
        diagnostic.duplicate_count = outcome.duplicate_count
        diagnostic.diversity_dropped_count = outcome.diversity_dropped
        diagnostic.filter_reasons = dict(outcome.filter_reasons)
        if diagnostic.source_scope_dropped_count:
            diagnostic.filter_reasons["SOURCE_PROFILE_MISMATCH"] = (
                diagnostic.source_scope_dropped_count
            )
        diagnostic.accepted_candidates = [
            {
                "title": item.hit.title,
                "url": str(item.hit.url),
                "ranking_score": round(item.score, 2),
                "ranking_reasons": item.reasons,
            }
            for item in outcome.accepted
        ]
        for item in outcome.accepted:
            if not subquery.source_profiles or is_search_redirect_url(str(item.hit.url)):
                    # A search redirect does not identify the destination
                    # page. Do not promote the requested profile (especially
                    # official_docs) to an observed source label.
                    item.source_profiles = []
            else:
                item.source_profiles = _observed_source_profiles(
                    str(item.hit.url), subquery.source_profiles
                )
        return outcome.accepted

    def _reflect(
        self,
        packet: ResearchPacket,
        observer: ResearchProgressObserver | None,
    ) -> tuple[bool, list]:
        self._notify(observer, "REFLECTING", packet)
        try:
            reflection = _bounded_call(
                # A timed-out provider may finish later. It must not mutate
                # the live packet while the fallback continues research.
                lambda: self.decision_provider.reflect(packet.model_copy(deep=True)),
                min(
                    float(self.settings.research_reflection_timeout_seconds) + MODEL_FINISH_GRACE_SECONDS,
                    self.limits.max_runtime_seconds,
                ),
            )
        except TimeoutError as error:
            packet.uncertain_points.append(
                "反思模型调用超时，已保留当前搜索与证据，并使用程序化策略继续补查。"
            )
            reflection = self._fallback_reflection(packet, error)
        except Exception as error:
            # Reflection is an optimization layer, not the owner of research
            # state.  Keep the evidence and let deterministic coverage rules
            # decide whether to continue, instead of turning a malformed model
            # response into an early terminal failure.
            reflection = self._fallback_reflection(packet, error)
        packet.uncertain_points = list(
            dict.fromkeys([*packet.uncertain_points, *reflection.unresolved_points])
        )
        previously_covered = set(packet.coverage.material_aspects)
        known_keys = {item.key for item in packet.subqueries}
        answered = set(reflection.answered_question_keys) & known_keys
        blocked = set(reflection.blocked_question_keys) & known_keys
        if reflection.complete and not reflection.answered_question_keys:
            answered.update(
                item.key for item in packet.subqueries if item.status == "EVIDENCE_READY"
            )
        for item in packet.subqueries:
            if item.key in answered and item.status == "EVIDENCE_READY":
                item.status = "ANSWERED"
            elif item.key in blocked and item.status != "ANSWERED":
                item.status = "BLOCKED"
        self._update_aspect_coverage(packet, reflection.covered_aspects)
        packet.gaps = diagnose_research_gaps(packet, reflection.gaps)
        # A model may mark its currently cited questions complete while the
        # deterministic audit still sees weak authority, missing evidence, or
        # unresolved conflicts. Convert that optimistic completion into one
        # bounded repair pass so incomplete body evidence does not end research early.
        needs_more_evidence = any(
            gap.gap_type
            in {
                "MISSING_EVIDENCE",
                "WEAK_AUTHORITY",
                "LOW_DIVERSITY",
                "ACCESS_FAILURE",
                "CONFLICT",
                "STALE_EVIDENCE",
            }
            for gap in packet.gaps
        ) or any(c.resolution == "UNRESOLVED" for c in packet.conflicts)
        if needs_more_evidence:
            reflection.complete = False
        if needs_more_evidence and not reflection.strategy_actions and not (
            reflection.planned_follow_ups or reflection.follow_up_questions
        ):
            fallback = self._fallback_reflection(
                packet,
                RuntimeError("deterministic evidence audit found unresolved gaps"),
            )
            reflection = reflection.model_copy(
                update={
                    "complete": False,
                    "strategy_actions": fallback.strategy_actions,
                    "gaps": [*reflection.gaps, *fallback.gaps],
                    "unresolved_points": [
                        *reflection.unresolved_points,
                        *fallback.unresolved_points,
                    ],
                }
            )
            packet.gaps = diagnose_research_gaps(packet, reflection.gaps)
        self._apply_budget_adjustments(packet, reflection.budget_adjustments)
        if reflection.next_stage_key:
            self._activate_stage(packet, reflection.next_stage_key)
        refresh_research_memory(packet)

        available_slots = max(self.limits.max_subqueries - len(packet.subqueries), 0)
        structured_follow_ups = normalize_research_questions(
            reflection.planned_follow_ups,
            max_subqueries=available_slots,
        )
        legacy_follow_ups = normalize_research_questions(
            reflection.follow_up_questions,
            max_subqueries=available_slots,
        )
        follow_ups = structured_follow_ups
        if packet.brief is not None:
            align_question_aspects(follow_ups, packet.brief.required_aspects)
            finalize_query_metadata(follow_ups, packet.brief.required_aspects)
        strategy_actions = list(reflection.strategy_actions)
        if strategy_actions:
            # A strategy action already expresses the adaptive intent and source
            # change. Ignore legacy text candidates to avoid executing the same
            # need twice through two schema generations.
            follow_ups = []
        elif follow_ups and not reflection.complete:
            strategy_actions = actions_from_legacy_followups(packet, follow_ups)
            if strategy_actions:
                follow_ups = []
        elif legacy_follow_ups and not reflection.complete:
            if packet.brief is not None:
                finalize_query_metadata(
                    legacy_follow_ups,
                    packet.brief.required_aspects,
                )
            strategy_actions = actions_from_legacy_followups(packet, legacy_follow_ups)
            if strategy_actions:
                follow_ups = []
            else:
                follow_ups = legacy_follow_ups
        adaptive_follow_up = bool(
            packet.gaps
            or strategy_actions
            or packet.memory.strategy_attempts
        )
        if (
            adaptive_follow_up
            and not reflection.complete
            and update_low_yield_state(packet, previously_covered=previously_covered)
        ):
            packet.stop_reason = "LOW_VALUE_SATURATION"
            packet.uncertain_points.append(
                "连续两轮没有新增可核查事实或必答项覆盖，已停止低收益补查。"
            )
            refresh_research_memory(packet)
            return False, []
        strategy_followups, stop_requested = prepare_strategy_followups(
            packet,
            strategy_actions,
            max_new=max(available_slots - len(follow_ups), 0),
            reserved_queries=[item.query for item in follow_ups],
        )
        follow_ups.extend(strategy_followups)
        if stop_requested:
            packet.stop_reason = "MODEL_LOW_VALUE_STOP"
            refresh_research_memory(packet)
            return False, []
        existing_queries = {item.query.casefold() for item in packet.subqueries}
        existing_keys = {item.key for item in packet.subqueries}
        pending = [
            item
            for item in follow_ups
            if item.key not in existing_keys and item.query.casefold() not in existing_queries
        ]
        if pending:
            for item in pending:
                from .strategy import _inherit_aspect_constraints
                for aspect in item.covers_aspects:
                    _inherit_aspect_constraints(packet, aspect, item)
                explicit_profiles = normalize_source_profiles(item.source_profiles)
                item.source_profiles = (
                    explicit_profiles
                    if explicit_profiles
                    else normalize_source_profiles(
                        infer_source_profiles(item.query, item.purpose, packet.brief)
                    )
                )[:6]
            finalize_query_metadata(
                pending,
                packet.brief.required_aspects if packet.brief else [],
            )
            packet.subqueries.extend(pending)
            self._assign_questions_to_stages(packet)
            dependency_diagnostics = validate_question_dependencies(packet.subqueries)
            packet.uncertain_points = list(
                dict.fromkeys([*packet.uncertain_points, *dependency_diagnostics])
            )
        update_strategy_attempt_outcomes(packet)
        refresh_research_memory(packet)
        return reflection.complete, pending

    def _fallback_reflection(
        self,
        packet: ResearchPacket,
        error: Exception,
    ) -> ResearchReflection:
        """Build a conservative reflection when the model cannot be parsed.

        The fallback deliberately emits at most one action per uncovered aspect.
        It reuses the planner's existing query metadata, so it cannot invent a
        new topic merely because the reflection call failed.
        """
        packet.uncertain_points = list(
            dict.fromkeys(
                [
                    *packet.uncertain_points,
                    "研究反思模型暂时不可用，已使用程序化证据覆盖判断。",
                ]
            )
        )[-20:]
        required = packet.brief.required_aspects if packet.brief else []
        self._update_question_evidence_status(packet)
        self._update_aspect_coverage(packet)
        uncovered = list(packet.coverage.uncovered_aspects)
        gaps = diagnose_research_gaps(packet, packet.gaps)
        if not uncovered:
            uncovered = [gap.aspect for gap in gaps if gap.aspect not in uncovered]
        by_aspect = {gap.aspect: gap for gap in gaps}
        actions: list[ResearchAction] = []
        existing_attempts = {
            (item.aspect, item.query.casefold())
            for item in packet.memory.strategy_attempts
        }
        for aspect in uncovered[:3]:
            gap = by_aspect.get(aspect)
            question = next(
                (
                    item
                    for item in packet.subqueries
                    if aspect in item.covers_aspects
                    and (item.status != "BLOCKED" or any(
                        diagnostic.key == item.key or diagnostic.query == item.query
                        for diagnostic in packet.search_diagnostics
                    ))
                ),
                None,
            )
            if question is None:
                continue
            query = question.query_intent or question.query
            if (aspect, query.casefold()) in existing_attempts:
                continue
            profiles = list(gap.preferred_source_profiles if gap else [])
            if not profiles:
                profiles = list(question.source_profiles)
            gap_suffix = {
                "MISSING_EVIDENCE": "具体数据 原始出处",
                "WEAK_AUTHORITY": "官方文档 原始出处",
                "LOW_DIVERSITY": "独立来源 对比",
                "ACCESS_FAILURE": "可读取正文 摘要",
                "CONFLICT": "不同来源 数值对比",
            }.get(gap.gap_type if gap else "", "补充证据")
            query = f"{query} {gap_suffix}"[:500]
            if "official_docs" in profiles:
                # A model reference is a search hint, not certified authority.
                # Restrict one repair query to it; ordinary result/read checks remain.
                for reference in question.reference_urls[:1]:
                    host = urlsplit(str(reference)).hostname
                    if host and source_url_matches_profiles(str(reference), ["official_docs"]):
                        query = f"site:{host} {question.query_intent or question.query}"[:500]
                        break
            kind = "SWITCH_SOURCE" if gap and gap.gap_type in {
                "ACCESS_FAILURE",
                "WEAK_AUTHORITY",
                "LOW_DIVERSITY",
            } else "REWRITE_QUERY"
            actions.append(
                ResearchAction(
                    kind=kind,
                    aspect=aspect,
                    stage_key=question.stage_id,
                    source_profiles=profiles[:8],
                    query_intent=query[:500],
                    priority=question.priority,
                    reason="模型反思失败，沿用已有研究问题生成保守补查动作。",
                )
            )
        complete = not uncovered and not packet.conflicts
        return ResearchReflection(
            complete=complete,
            answered_question_keys=[
                item.key
                for item in packet.subqueries
                if item.status == "EVIDENCE_READY"
            ],
            covered_aspects=[
                aspect for aspect in required if aspect in packet.coverage.covered_aspects
            ],
            gaps=gaps,
            strategy_actions=actions,
            unresolved_points=[
                f"未完成必答方面：{aspect}" for aspect in uncovered[:5]
            ],
        )

    def _searched_query_identity(self, packet: ResearchPacket) -> tuple[set[str], set[str]]:
        """Return both key and legacy text identities from search diagnostics."""
        searched_keys = {
            diagnostic.key for diagnostic in packet.search_diagnostics if diagnostic.key
        }
        searched_queries = {diagnostic.query.casefold() for diagnostic in packet.search_diagnostics}
        # Older checkpoints have no diagnostic key. Associate their normalized
        # query with the current planned node so resume remains idempotent.
        for item in packet.subqueries:
            if item.query.casefold() in searched_queries:
                searched_keys.add(item.key)
        return searched_keys, searched_queries

    @staticmethod
    def _ready_questions(packet: ResearchPacket, questions: list) -> list:
        if (
            packet.decision is not None
            and packet.decision.research_stages
            and packet.active_stage_key is None
        ):
            return []
        completed = {
            item.key for item in packet.subqueries if item.status in {"ANSWERED", "EVIDENCE_READY"}
        }
        ready = []
        for item in questions:
            if item.status == "BLOCKED":
                continue
            if packet.active_stage_key and item.stage_id != packet.active_stage_key:
                continue
            if all(dependency in completed for dependency in item.depends_on):
                ready.append(item)
        return sorted(ready, key=lambda item: -item.priority)

    def _initialize_research_stages(self, packet: ResearchPacket) -> None:
        if packet.decision is None or not packet.decision.research_stages:
            packet.active_stage_key = None
            return
        seen: set[str] = set()
        stages = []
        for stage in sorted(packet.decision.research_stages, key=lambda item: item.order):
            if stage.key in seen:
                packet.uncertain_points.append(f"研究阶段 key 重复：{stage.key}。")
                continue
            seen.add(stage.key)
            stage.source_profiles = normalize_source_profiles(stage.source_profiles)
            stages.append(stage)
        packet.decision.research_stages = stages
        keys = {stage.key for stage in stages}
        for stage in stages:
            if any(dependency not in keys for dependency in stage.depends_on):
                stage.status = "BLOCKED"
                packet.uncertain_points.append(f"研究阶段“{stage.key}”存在无效依赖。")
        active_stages = [stage for stage in stages if stage.status == "ACTIVE"]
        active = active_stages[0] if active_stages else None
        for extra_active in active_stages[1:]:
            extra_active.status = "PLANNED"
        if packet.active_stage_key not in keys:
            packet.active_stage_key = active.key if active else None
        if packet.active_stage_key is None:
            next_stage = next(
                (
                    stage
                    for stage in stages
                    if stage.status == "PLANNED" and not stage.depends_on
                ),
                None,
            )
            if next_stage is not None:
                next_stage.status = "ACTIVE"
                packet.active_stage_key = next_stage.key
            elif stages:
                packet.uncertain_points.append("研究阶段依赖关系无法确定首个可执行阶段。")

    def _effective_round_limit(self, packet: ResearchPacket) -> int:
        """Expand only complex research from the normal 3-round budget to 5.

        The explicit low limits used by callers and tests remain hard bounds.
        A larger cap is useful only when the task actually asks for evidence
        across platforms, measurements, risks, compatibility, or conflicts.
        """
        configured = self.limits.max_rounds
        if configured < 3:
            return configured
        brief = packet.brief
        stages = packet.decision.research_stages if packet.decision else []
        text_parts = [packet.question]
        if brief is not None:
            text_parts.extend(
                [brief.objective, *brief.required_aspects, *brief.constraints]
            )
        for stage in stages:
            text_parts.extend([stage.purpose, *stage.required_aspects])
        text = " ".join(text_parts).casefold()
        markers = (
            "多平台",
            "跨平台",
            "实测",
            "避坑",
            "兼容",
            "驱动",
            "cuda",
            "oom",
            "对比",
            "冲突",
            "风险",
            "显存",
            "速度",
        )
        marker_hits = sum(marker in text for marker in markers)
        has_multi_source_stage = any(len(stage.source_profiles) >= 3 for stage in stages)
        is_complex = len(stages) >= 3 or marker_hits >= 2 or has_multi_source_stage
        return min(5, max(configured, 5)) if is_complex else configured

    def _assign_questions_to_stages(self, packet: ResearchPacket) -> None:
        stages = packet.decision.research_stages if packet.decision else []
        if not stages:
            return
        stage_keys = {stage.key for stage in stages}
        for item in packet.subqueries:
            if item.stage_id in stage_keys:
                continue
            matched = next(
                (
                    stage
                    for stage in stages
                    if set(stage.source_profiles) & set(item.source_profiles)
                ),
                None,
            )
            item.stage_id = (matched or stages[0]).key
        required = set(packet.brief.required_aspects if packet.brief else [])
        for stage in stages:
            derived = [
                aspect
                for item in packet.subqueries
                if item.stage_id == stage.key
                for aspect in item.covers_aspects
                if not required or aspect in required
            ]
            stage.required_aspects = list(
                dict.fromkeys([*stage.required_aspects, *derived])
            )[:8]

    @staticmethod
    def _stage_query_usage(packet: ResearchPacket, stage_key: str) -> int:
        question_keys = {
            item.key for item in packet.subqueries if item.stage_id == stage_key
        }
        return sum(
            bool(diagnostic.key and diagnostic.key in question_keys)
            for diagnostic in packet.search_diagnostics
        )

    @staticmethod
    def _stage_read_usage(packet: ResearchPacket, stage_key: str) -> int:
        stage_queries = {
            item.query.casefold()
            for item in packet.subqueries
            if item.stage_id == stage_key
        }
        return sum(
            source.read_status != "NOT_SELECTED"
            and any(query.casefold() in stage_queries for query in source.matched_queries)
            for source in packet.sources
        )

    def _stage_query_remaining(self, packet: ResearchPacket) -> int:
        if packet.decision is None or not packet.decision.research_stages:
            return self.limits.max_queries
        stage = next(
            (
                item
                for item in packet.decision.research_stages
                if item.key == packet.active_stage_key
            ),
            None,
        )
        if stage is None:
            return 0
        used = self._stage_query_usage(packet, stage.key)
        return max(stage.query_budget - used, 0)

    def _stage_read_remaining(self, packet: ResearchPacket) -> int:
        if packet.decision is None or not packet.decision.research_stages:
            return self.limits.max_read_pages
        stage = next(
            (
                item
                for item in packet.decision.research_stages
                if item.key == packet.active_stage_key
            ),
            None,
        )
        if stage is None:
            return 0
        used = self._stage_read_usage(packet, stage.key)
        return max(stage.read_budget - used, 0)

    def _apply_budget_adjustments(self, packet: ResearchPacket, adjustments: list) -> None:
        stages = packet.decision.research_stages if packet.decision else []
        if not stages or not adjustments:
            return
        by_key = {stage.key: stage for stage in stages}
        requested = {}
        for adjustment in adjustments:
            stage = by_key.get(adjustment.stage_key)
            if stage is None:
                continue
            requested[stage.key] = adjustment

        query_proposals = {}
        read_proposals = {}
        for stage in stages:
            adjustment = requested.get(stage.key)
            query_delta = adjustment.query_delta if adjustment else 0
            read_delta = adjustment.read_delta if adjustment else 0
            query_proposals[stage.key] = max(
                self._stage_query_usage(packet, stage.key),
                min(max(stage.query_budget + query_delta, 1), 12),
            )
            read_proposals[stage.key] = max(
                self._stage_read_usage(packet, stage.key),
                min(max(stage.read_budget + read_delta, 0), 12),
            )
        query_ceiling = max(sum(stage.query_budget for stage in stages), self.limits.max_queries)
        read_ceiling = max(sum(stage.read_budget for stage in stages), self.limits.max_read_pages)
        if sum(query_proposals.values()) > query_ceiling:
            query_proposals = {stage.key: stage.query_budget for stage in stages}
        if sum(read_proposals.values()) > read_ceiling:
            read_proposals = {stage.key: stage.read_budget for stage in stages}

        for stage in stages:
            adjustment = requested.get(stage.key)
            if adjustment is None:
                continue
            old_query = stage.query_budget
            old_read = stage.read_budget
            stage.query_budget = query_proposals[stage.key]
            stage.read_budget = read_proposals[stage.key]
            applied = adjustment.model_copy(
                update={
                    "query_delta": stage.query_budget - old_query,
                    "read_delta": stage.read_budget - old_read,
                }
            )
            if applied.query_delta or applied.read_delta:
                packet.budget_adjustments.append(applied)
        packet.budget_adjustments = packet.budget_adjustments[-20:]

    def _activate_stage(self, packet: ResearchPacket, key: str) -> bool:
        stages = packet.decision.research_stages if packet.decision else []
        target = next((stage for stage in stages if stage.key == key), None)
        if target is None or target.status == "BLOCKED":
            return False
        terminal = {
            stage.key
            for stage in stages
            if stage.status in {"COMPLETE", "PARTIAL", "BLOCKED"}
        }
        if packet.active_stage_key and packet.active_stage_key != target.key:
            self._finalize_active_stage(packet)
            terminal.add(packet.active_stage_key)
        if any(dependency not in terminal for dependency in target.depends_on):
            return False
        if packet.active_stage_key == target.key:
            return True
        target.status = "ACTIVE"
        packet.active_stage_key = target.key
        return True

    def _advance_stage(self, packet: ResearchPacket) -> bool:
        stages = packet.decision.research_stages if packet.decision else []
        if not stages:
            return False
        self._finalize_active_stage(packet)
        terminal = {
            stage.key
            for stage in stages
            if stage.status in {"COMPLETE", "PARTIAL", "BLOCKED"}
        }
        next_stage = next(
            (
                stage
                for stage in sorted(stages, key=lambda item: item.order)
                if stage.status == "PLANNED"
                and all(dependency in terminal for dependency in stage.depends_on)
            ),
            None,
        )
        if next_stage is None:
            packet.active_stage_key = None
            return False
        next_stage.status = "ACTIVE"
        packet.active_stage_key = next_stage.key
        return True

    def _finalize_active_stage(self, packet: ResearchPacket) -> None:
        self._update_aspect_coverage(packet)
        stages = packet.decision.research_stages if packet.decision else []
        for stage in stages:
            if stage.key != packet.active_stage_key or stage.status != "ACTIVE":
                continue
            scope_keys = self._stage_scope_keys(stages, stage)
            stage_questions = [
                item for item in packet.subqueries if item.stage_id in scope_keys
            ]
            stage_queries = {item.query.casefold() for item in stage_questions}
            readable = [
                source
                for source in packet.sources
                if source.read_status == "SUCCEEDED"
                and any(
                    query.casefold() in stage_queries for query in source.matched_queries
                )
            ]
            snippet_sources = [
                source
                for source in packet.sources
                if source.read_status != "SUCCEEDED"
                and source.snippet.strip()
                and any(
                    query.casefold() in stage_queries for query in source.matched_queries
                )
            ]
            independent = {
                source.source_cluster_id or source.canonical_url
                for source in readable
                if source.source_independence_score > 0
            }
            covered_aspects = set(packet.coverage.answerable_aspects)
            missing_aspects = [
                aspect
                for aspect in stage.required_aspects
                if aspect not in covered_aspects
            ]
            enough_reads = len(readable) >= stage.min_successful_reads
            enough_sources = len(independent) >= stage.min_independent_sources
            multi_platform = self._requires_multi_platform_coverage(stage)
            platform_domains = {
                source_diversity_key(source.canonical_url or str(source.url))
                for source in readable
                if set(source.source_profiles) & _COMMUNITY_SOURCE_PROFILES
            }
            required_platforms = min(max(stage.min_independent_sources, 2), 3)
            enough_platforms = not multi_platform or (
                len(platform_domains) >= required_platforms
            )
            stage.verification_status = self._stage_verification_status(packet, readable)
            if not missing_aspects and enough_reads and enough_sources and enough_platforms:
                stage.status = "COMPLETE"
                stage.completion_reason = (
                    f"已覆盖阶段要求，读取 {len(readable)} 页、"
                    f"获得 {len(independent)} 个独立来源；"
                    + (
                        f"覆盖 {len(platform_domains)} 个社区平台域名；"
                        if multi_platform
                        else ""
                    )
                    + f"核验状态为 {stage.verification_status}。"
                )
            elif readable or snippet_sources:
                stage.status = "PARTIAL"
                stage.completion_reason = (
                    "阶段取得可用证据，但未满足全部验收条件；"
                    + (
                        f"其中 {len(snippet_sources)} 条为搜索摘要线索，未提升为正文事实；"
                        if snippet_sources
                        else ""
                    )
                    + (
                        f"多平台要求至少 {required_platforms} 个社区平台域名，"
                        f"当前为 {len(platform_domains)} 个；"
                        if multi_platform and not enough_platforms
                        else ""
                    )
                    + f"核验状态为 {stage.verification_status}。"
                )
            else:
                stage.status = "BLOCKED"
                stage.verification_status = "NOT_ASSESSED"
                stage.completion_reason = "阶段没有取得正文或搜索摘要证据。"

    @staticmethod
    def _requires_multi_platform_coverage(stage: ResearchStage) -> bool:
        description = " ".join([stage.purpose, *stage.required_aspects])
        return "多平台" in description or "跨平台" in description

    @staticmethod
    def _stage_scope_keys(
        stages: list[ResearchStage], stage: ResearchStage
    ) -> set[str]:
        """Synthesis stages can reuse evidence from their dependency chain."""
        scope = {stage.key}
        if stage.mode != "SYNTHESIS":
            return scope
        by_key = {item.key: item for item in stages}
        pending = list(stage.depends_on)
        while pending:
            key = pending.pop()
            if key in scope:
                continue
            scope.add(key)
            dependency = by_key.get(key)
            if dependency is not None:
                pending.extend(dependency.depends_on)
        return scope

    @staticmethod
    def _stage_verification_status(
        packet: ResearchPacket, readable: list[ResearchSource]
    ) -> str:
        if not readable:
            return "NOT_ASSESSED"
        relevant_urls = {
            canonicalize_url(source.canonical_url or str(source.url))
            for source in readable
        }
        relevant_claims = [
            claim
            for claim in packet.claims
            if any(canonicalize_url(str(url)) in relevant_urls for url in claim.source_urls)
        ]
        if any(claim.status == "CONFLICTED" for claim in relevant_claims):
            return "CONFLICTED"
        for conflict in packet.conflicts:
            conflict_urls = {
                canonicalize_url(str(url))
                for position in conflict.positions
                for url in position.get("source_urls", [])
            }
            if conflict_urls & relevant_urls:
                return "CONFLICTED"
        if not relevant_claims:
            return "UNVERIFIED"
        statuses = {claim.status for claim in relevant_claims}
        if statuses == {"SUPPORTED"}:
            return "VERIFIED"
        if statuses & {"SUPPORTED", "PARTIALLY_SUPPORTED"}:
            return "PARTIALLY_VERIFIED"
        return "UNVERIFIED"

    @staticmethod
    def _select_query_batch(packet: ResearchPacket, pending: list, limit: int) -> list:
        """Give distinct required source/aspect groups a slot before filling by priority."""
        if limit <= 0:
            return []
        if packet.brief is None or not (
            packet.brief.required_aspects or packet.brief.source_profiles
        ):
            return pending[:limit]
        searched_keys = {
            diagnostic.key for diagnostic in packet.search_diagnostics if diagnostic.key
        }
        searched_profiles = {
            profile
            for item in packet.subqueries
            if item.key in searched_keys
            for profile in item.source_profiles
        }
        uncovered = set(packet.coverage.uncovered_aspects)
        selected = []
        represented: set[str] = set()

        def group_key(item) -> str | None:
            missing_profile = next(
                (profile for profile in item.source_profiles if profile not in searched_profiles),
                None,
            )
            if missing_profile:
                return f"profile:{missing_profile}"
            missing_aspect = next(
                (aspect for aspect in item.covers_aspects if aspect in uncovered),
                None,
            )
            return f"aspect:{missing_aspect}" if missing_aspect else None

        for item in pending:
            key = group_key(item)
            if item.required and key is not None and key not in represented:
                selected.append(item)
                represented.add(key)
                if len(selected) >= limit:
                    return selected
        for item in pending:
            if item not in selected:
                selected.append(item)
                if len(selected) >= limit:
                    break
        return selected

    @staticmethod
    def _has_required_pending(packet: ResearchPacket) -> bool:
        required_aspects = set(packet.brief.required_aspects if packet.brief else [])
        unresolved_aspects = {
            *packet.coverage.uncovered_aspects,
            *(gap.aspect for gap in packet.gaps),
        }
        for item in packet.subqueries:
            if not item.required or item.status not in {
                "PLANNED",
                "SEARCHED",
                "EVIDENCE_READY",
            }:
                continue
            if not required_aspects or not item.covers_aspects:
                return True
            if set(item.covers_aspects) & unresolved_aspects:
                return True
        return False

    @staticmethod
    def _update_question_evidence_status(packet: ResearchPacket) -> None:
        evidence_queries = {
            query.casefold()
            for source in packet.sources
            if source.read_status == "SUCCEEDED" or source.snippet.strip()
            for query in source.matched_queries
        }
        searched_queries = {item.query.casefold() for item in packet.search_diagnostics}
        for item in packet.subqueries:
            normalized_query = item.query.casefold()
            if normalized_query in evidence_queries and item.status != "ANSWERED":
                item.status = "EVIDENCE_READY"
            elif normalized_query in searched_queries and item.status in {"PLANNED", "SEARCHED"}:
                item.status = "BLOCKED"
        update_strategy_attempt_outcomes(packet)

    @staticmethod
    def _update_aspect_coverage(
        packet: ResearchPacket, requested_aspects: list[str] | None = None
    ) -> None:
        refresh_research_coverage(packet)

    def _extract_evidence(self, packet: ResearchPacket) -> None:
        selector = getattr(self.intent_analyzer, "select_evidence", None)
        if selector is not None and packet.evidence_mode != "SPANS":
            # Rebuild historical normalized web excerpts from the retained raw
            # Reader output. Do not spend the nine-passage cap reselecting both
            # the flattened old copy and the faithful Markdown copy.
            packet.passages = [p for p in packet.passages
                               if p.locator.get("offset_basis") != "normalized_stored_excerpt"]
            retained_ids = {p.passage_id for p in packet.passages}
            packet.extracted_passage_ids = [p for p in packet.extracted_passage_ids if p in retained_ids]
        known_passage_ids = {passage.passage_id for passage in packet.passages}
        known_claim_ids = {claim.claim_id for claim in packet.claims}
        packet.passages = merge_passages(packet.passages, build_web_passages(packet, preserve_markup=selector is not None))
        new_passage_ids = [
            passage.passage_id
            for passage in packet.passages
            if passage.passage_id not in known_passage_ids
        ]
        pending_passages = [
            passage
            for passage in packet.passages
            if passage.passage_id not in packet.extracted_passage_ids
        ]
        if selector is not None:
            from .selection import validated_selected_evidence

            packet.evidence_mode = "SPANS"
            # Old checkpoint summaries are not part of the new evidence pack.
            packet.claims = []
            if pending_passages:
                try:
                    result = selector(packet)
                    selected = {e.evidence_id: e for e in packet.selected_evidence}
                    selected.update({e.evidence_id: e for e in result.evidence})
                    packet.selected_evidence = list(selected.values())
                    packet.selected_evidence = validated_selected_evidence(packet)
                    packet.uncertain_points = list(dict.fromkeys([
                        *packet.uncertain_points, *result.diagnostics,
                    ]))[:20]
                    pending_ids = {p.passage_id for p in pending_passages}
                    packet.extracted_passage_ids = list(dict.fromkeys([
                        *packet.extracted_passage_ids,
                        *(p for p in result.processed_passage_ids if p in pending_ids),
                    ]))
                except Exception:
                    packet.uncertain_points.append("原文片段选择失败，已读原文仍保留待处理。")
            refresh_evidence_metrics(packet)
            reviewer = getattr(self.intent_analyzer, "review_selected_evidence", None)
            if reviewer is not None and pending_passages and packet.selected_evidence:
                # A transient failure must not strand successfully selected
                # passages: they are already marked processed above. Retry the
                # verification once, preserving the original text and gates.
                for attempt in range(2):
                    try:
                        packet.aspect_evidence_review = reviewer(packet.model_copy(deep=True))
                        break
                    except Exception:
                        if attempt == 1:
                            packet.uncertain_points.append("逐项证据核验尚未完成，相关片段暂不视为完整回答。")
            refresh_research_memory(packet, new_passage_ids=new_passage_ids)
            refresh_research_coverage(packet)
            return
        extractor = getattr(self.intent_analyzer, "extract_evidence", None)
        if extractor is None or not pending_passages:
            fallback = build_snippet_fallback_analysis(packet)
            if fallback.claims:
                merge_evidence(packet, fallback)
            refresh_evidence_metrics(packet)
            refresh_research_memory(packet, new_passage_ids=new_passage_ids)
            return
        try:
            analysis: ResearchEvidenceAnalysis = extractor(packet)
        except Exception:
            packet.uncertain_points.append("事实抽取或来源核验失败，网页摘录仍按未核验材料保留。")
            refresh_evidence_metrics(packet)
            refresh_research_memory(packet, new_passage_ids=new_passage_ids)
            return
        validated, diagnostics = validate_evidence_analysis(
            analysis,
            passages=pending_passages,
            sources=packet.sources,
            focus_texts=[
                packet.question,
                *(packet.brief.required_aspects if packet.brief else []),
            ],
        )
        if not validated.claims and not validated.conflicts:
            diagnostics.append(
                "本批正文或搜索摘要未抽取到可核验事实，材料仍作为未核验线索保留。"
            )
        merge_evidence(packet, validated)
        # Preserve uncited search summaries as weak leads after model
        # extraction. This is intentionally additive and never upgrades their
        # evidence level beyond SEARCH_SNIPPET.
        fallback = build_snippet_fallback_analysis(packet)
        if fallback.claims:
            merge_evidence(packet, fallback)
        refresh_research_memory(
            packet,
            new_passage_ids=new_passage_ids,
            new_claim_ids=[
                claim.claim_id for claim in packet.claims if claim.claim_id not in known_claim_ids
            ],
        )
        packet.uncertain_points = list(dict.fromkeys([*packet.uncertain_points, *diagnostics]))[:20]
        refresh_evidence_metrics(packet)
        pending_passage_ids = [passage.passage_id for passage in pending_passages]
        pending_ids = set(pending_passage_ids)
        processed_ids = [
            passage_id
            for passage_id in validated.processed_passage_ids
            if passage_id in pending_ids
        ]
        packet.extracted_passage_ids = list(
            dict.fromkeys(
                [
                    *packet.extracted_passage_ids,
                    *processed_ids,
                ]
            )
        )

    @staticmethod
    def _notify(
        observer: ResearchProgressObserver | None,
        stage: str,
        packet: ResearchPacket,
    ) -> None:
        if observer is not None:
            observer(stage, packet)

    @staticmethod
    def _record_stage_time(packet: ResearchPacket, stage: str, started: float) -> None:
        elapsed_ms = max(0, round((monotonic() - started) * 1000))
        packet.stage_timings_ms[stage] = packet.stage_timings_ms.get(stage, 0) + elapsed_ms

    @staticmethod
    def _seed_reference_sources(packet: ResearchPacket, *, extended: bool = False) -> None:
        """Model URLs are unverified read candidates restricted to known publishers."""
        existing = {source.canonical_url: source for source in packet.sources}
        for question in packet.subqueries:
            if "official_docs" not in question.source_profiles:
                continue
            topic = question.query_intent + " " + question.query
            domains = official_documentation_domains(topic, extended=extended)
            references = [*question.reference_urls, *official_reference_urls(topic, extended=extended)]
            for url in references[:3]:
                canonical = canonicalize_url(str(url))
                host = (urlsplit(canonical).hostname or "").casefold().removeprefix("www.")
                if host not in domains or not source_matches_requested_version(
                    canonical, question.constraints.specifications
                ):
                    continue
                if canonical in existing:
                    source = existing[canonical]
                    source.matched_queries = list(dict.fromkeys([*source.matched_queries, question.query]))
                    continue
                source = ResearchSource(
                    url=url, canonical_url=canonical, source="REFERENCE_CANDIDATE",
                    relevance_score=90, ranking_score=90,
                    source_category="DOCUMENTATION", source_profiles=["official_docs"],
                    evidence_level="UNVERIFIED",
                    matched_queries=[question.query], ranking_reasons=["MODEL_REFERENCE_CANDIDATE"],
                    source_cluster_id=source_cluster_id(canonical),
                )
                packet.sources.append(source)
                existing[canonical] = source

    @staticmethod
    def _seed_previous_read_sources(packet: ResearchPacket, context: dict | None) -> None:
        """Recheck URLs from the same earlier question; never reuse their claims."""
        urls = context.get("previous_read_urls", []) if isinstance(context, dict) else []
        existing = {source.canonical_url for source in packet.sources}
        for value in urls[:6]:
            if not isinstance(value, str):
                continue
            canonical = canonicalize_url(value)
            if canonical in existing or urlsplit(canonical).scheme not in {"http", "https"}:
                continue
            related = [q for q in packet.subqueries
                       if source_url_matches_profiles(canonical, q.source_profiles)]
            if not related:
                continue
            packet.sources.append(ResearchSource(
                url=value, canonical_url=canonical, source="PRIOR_READ_CANDIDATE",
                source_category=classify_source(canonical),
                relevance_score=90, ranking_score=90, evidence_level="UNVERIFIED",
                matched_queries=[q.query for q in related],
                ranking_reasons=["MODEL_REFERENCE_CANDIDATE"],
                source_cluster_id=source_cluster_id(canonical),
            ))
            existing.add(canonical)

    @staticmethod
    def _seed_explicit_url_sources(packet: ResearchPacket, urls: list[str]) -> None:
        """Put user-provided URLs into the read queue before ranked search hits.

        A URL in the prompt is an explicit request to inspect that destination;
        relying on a search engine to rediscover it can silently lose the user's
        intended source. The normal bounded reader, quality checks, browser
        fallback, and snippet fallback still apply to this seed.
        """
        if not urls:
            return
        matched_queries = [item.query for item in packet.subqueries]
        existing = {canonicalize_url(str(source.url)): source for source in packet.sources}
        for value in urls:
            canonical = canonicalize_url(value)
            if canonical in existing:
                source = existing[canonical]
                source.ranking_reasons = list(dict.fromkeys([*source.ranking_reasons, "EXPLICIT_URL"]))
                source.matched_queries = list(dict.fromkeys([*source.matched_queries, *matched_queries]))
                continue
            packet.sources.append(
                ResearchSource(
                    url=value,
                    canonical_url=canonical,
                    source="EXPLICIT_URL",
                    snippet="用户明确提供的来源，将优先尝试读取正文。",
                    relevance_score=100.0,
                    quality_score=90.0,
                    ranking_score=100.0,
                    source_category=classify_source(value),
                    matched_queries=matched_queries,
                    ranking_reasons=["EXPLICIT_URL"],
                    source_cluster_id=source_cluster_id(canonical),
                )
            )
            existing[canonical] = packet.sources[-1]

    def _merge_sources(self, packet: ResearchPacket, ranked: list[list[RankedHit]]) -> None:
        existing = {source.canonical_url: source for source in packet.sources}
        for source in packet.sources:
            existing.update((url, source) for url in source.duplicate_urls)
        domain_counts: dict[str, int] = {}
        for source in packet.sources:
            domain = source_diversity_key(source.canonical_url)
            domain_counts[domain] = domain_counts.get(domain, 0) + 1
        fresh: list[ResearchSource] = []
        for item in (hit for row in zip_longest(*ranked) for hit in row if hit is not None):
            source = existing.get(item.canonical_url)
            if source is not None:
                self._merge_ranked_source(source, item)
                packet.coverage.duplicate_results += 1
                continue
            near_duplicate = next(
                (
                    candidate
                    for candidate in [*packet.sources, *fresh]
                    if are_near_duplicate(
                        candidate.title,
                        candidate.snippet,
                        item.hit.title,
                        item.hit.snippet,
                    )
                ),
                None,
            )
            if near_duplicate is not None:
                self._merge_ranked_source(near_duplicate, item)
                packet.coverage.duplicate_results += 1
                existing[item.canonical_url] = near_duplicate
                continue
            domain = source_diversity_key(item.canonical_url)
            if domain_counts.get(domain, 0) >= self.limits.max_sources_per_domain:
                packet.coverage.diversity_dropped_results += 1
                continue
            source = self._source(item)
            existing[item.canonical_url] = source
            fresh.append(source)
            domain_counts[domain] = domain_counts.get(domain, 0) + 1
        tried = [source for source in packet.sources if source.read_status != "NOT_SELECTED"]
        waiting = [
            source
            for source in packet.sources
            if source.read_status == "NOT_SELECTED"
            and ("SNIPPET_FALLBACK" not in source.ranking_reasons
                 or bool(set(source.ranking_reasons) & {"EXPLICIT_URL", "MODEL_REFERENCE_CANDIDATE"}))
        ]
        explicit_waiting = [
            source for source in waiting if set(source.ranking_reasons) & {"EXPLICIT_URL", "MODEL_REFERENCE_CANDIDATE"}
        ]
        regular_waiting = [
            source for source in waiting if not set(source.ranking_reasons) & {"EXPLICIT_URL", "MODEL_REFERENCE_CANDIDATE"}
        ]
        # A URL explicitly supplied by the user is a direct read instruction,
        # not merely another search candidate. Preserve it ahead of discovered
        # results so source diversity limits cannot silently discard it.
        packet.sources = (
            tried + explicit_waiting + fresh + regular_waiting
        )[: self.limits.max_sources]

    @staticmethod
    def _merge_ranked_source(source: ResearchSource, item: RankedHit) -> None:
        source.relevance_score = max(source.relevance_score, item.relevance_score)
        source.quality_score = max(source.quality_score, item.quality_score)
        source.freshness_score = max(source.freshness_score, item.freshness_score)
        source.ranking_score = max(source.ranking_score, item.score)
        source.engines = list(dict.fromkeys([*source.engines, *item.engines]))
        source.matched_queries = list(
            dict.fromkeys([*source.matched_queries, *item.matched_queries])
        )
        if "SNIPPET_FALLBACK" in source.ranking_reasons and "SNIPPET_FALLBACK" not in item.reasons:
            source.ranking_reasons = [
                reason for reason in source.ranking_reasons if reason != "SNIPPET_FALLBACK"
            ]
        source.ranking_reasons = list(dict.fromkeys([*source.ranking_reasons, *item.reasons]))
        aliases = [item.canonical_url, *item.duplicate_urls]
        source.duplicate_urls = list(
            dict.fromkeys(
                [*source.duplicate_urls, *(url for url in aliases if url != source.canonical_url)]
            )
        )

    @staticmethod
    def _source(item: RankedHit) -> ResearchSource:
        return ResearchSource(
            title=item.hit.title,
            url=item.hit.url,
            canonical_url=item.canonical_url,
            source=item.hit.source,
            engines=item.engines,
            snippet=item.hit.snippet,
            relevance_score=item.relevance_score,
            quality_score=item.quality_score,
            freshness_score=item.freshness_score,
            ranking_score=item.score,
            confidence=0.0,
            source_category=item.source_category,
            source_profiles=item.source_profiles,
            matched_queries=item.matched_queries,
            ranking_reasons=item.reasons,
            duplicate_urls=item.duplicate_urls,
            source_cluster_id=source_cluster_id(item.canonical_url),
        )

    def _read_selected(self, packet: ResearchPacket) -> int:
        self._seed_explicit_url_sources(packet, _extract_explicit_urls(packet.question))
        newly_read = 0
        round_attempts = 0
        failed_pages_at_start = packet.coverage.failed_pages
        total_chars = sum(len(source.content_excerpt) for source in packet.sources)
        round_remaining = max(1000, self.limits.max_total_page_chars // self.limits.max_rounds)
        page_share = max(1000, self.limits.max_total_page_chars // self.limits.max_read_pages)
        candidates = self._ordered_read_candidates(
            packet, enhanced=self.settings.research_rerank_mode == "BGE",
        )
        if self.settings.local_model_mode == "SHADOW" or self.settings.local_model_dataset_mode == "CAPTURE":
            from planner_api.local_model_shadow import shadow_material_rerank

            shadow_material_rerank(
                self.settings, packet.question,
                [(source.title + "\n" + source.snippet).strip() for source in candidates[:24]],
                scope="RESEARCH_READ_CANDIDATES",
            )
        round_read_target = min(
            self.limits.max_pages_per_round,
            self._stage_read_remaining(packet),
        )
        if round_read_target <= 0:
            return 0
        candidate_index = 0
        # Page fetches are independent I/O operations. Fetch a bounded wave in
        # parallel, then apply quality, deduplication, and budget mutations in
        # the original ranking order. This keeps deterministic selection while
        # removing the largest avoidable latency in a research round.
        while candidate_index < len(candidates):
            if monotonic() >= getattr(self, "_read_deadline", float("inf")):
                break
            replacement_slots = min(
                max(packet.coverage.failed_pages - failed_pages_at_start, 0),
                self.limits.max_failed_page_replacements,
            )
            max_attempts = round_read_target + replacement_slots
            if newly_read >= round_read_target or round_attempts >= max_attempts:
                break
            if self._attempted_pages(packet) >= self.limits.max_read_pages + replacement_slots:
                break
            remaining = min(self.limits.max_total_page_chars - total_chars, round_remaining)
            if remaining < 1000:
                break
            batch_size = min(
                self.limits.read_concurrency,
                round_read_target - newly_read,
                max_attempts - round_attempts,
                len(candidates) - candidate_index,
            )
            if self.settings.research_rerank_mode == "BGE":
                batch_size = min(
                    batch_size,
                    self.limits.max_read_pages + replacement_slots - self._attempted_pages(packet),
                )
            # Keep each wave inside the round character budget. If the budget
            # is too small for a multi-page wave, fall back to one request.
            while batch_size > 1 and remaining // batch_size < 1000:
                batch_size -= 1
            batch = candidates[candidate_index : candidate_index + batch_size]
            candidate_index += len(batch)
            max_chars = min(
                self.limits.max_page_chars,
                page_share,
                max(1000, remaining // max(len(batch), 1)),
            )
            packet.coverage.selected_pages += len(batch)
            round_attempts += len(batch)
            with ThreadPoolExecutor(max_workers=len(batch)) as executor:
                results = list(
                    executor.map(
                        lambda source, page_chars=max_chars: self._read_page_result(
                            source.url, self.limits.max_page_chars,
                            focus=source_read_focus(packet, source),
                        ),
                        batch,
                    )
                )
            for source, result in zip(batch, results, strict=True):
                document, assessment, used_fallback, error = result
                if error is not None:
                    source.read_status = "FAILED"
                    source.error_code = (
                        error.code
                        if isinstance(error, ResearchProviderError)
                        else "PAGE_READ_FAILED"
                    )
                    packet.coverage.failed_pages += 1
                    continue
                source.page_quality_score = assessment.score
                source.page_quality_flags = list(assessment.flags)
                source.page_content_type = document.content_type
                source.page_readability_applied = document.readability_applied
                if not assessment.usable:
                    source.read_status = "FAILED"
                    source.error_code = self._page_quality_error_code(assessment)
                    packet.coverage.failed_pages += 1
                    continue
                excerpt_query = source_read_focus(packet, source) or packet.question
                if self.settings.research_rerank_mode == "BGE":
                    related = [q for q in packet.subqueries if q.query in source.matched_queries]
                    if related:
                        excerpt_query = " ".join(q.query_intent or q.query for q in related)
                excerpt_window = focused_excerpt_window(document.content, excerpt_query, max_chars)
                excerpt = excerpt_window.text
                retrieved = archived_excerpt(packet, source, document, max_chars, settings=self.settings)
                excerpt_archive = archive_for_excerpt(
                    document.evidence_archive, excerpt_window, len(document.content))
                if retrieved is not None:
                    excerpt, excerpt_archive = retrieved
                original_canonical = source.canonical_url
                final_canonical = canonicalize_url(str(document.final_url))
                related_queries = [q for q in packet.subqueries if q.query in source.matched_queries]
                if related_queries and not any(source_matches_requested_version(
                    final_canonical, q.constraints.specifications
                ) for q in related_queries):
                    source.read_status = "FAILED"
                    source.error_code = "SOURCE_VERSION_MISMATCH"
                    packet.coverage.failed_pages += 1
                    continue
                if "MODEL_REFERENCE_CANDIDATE" in source.ranking_reasons and (
                    urlsplit(final_canonical).hostname != urlsplit(original_canonical).hostname
                ):
                    source.read_status = "FAILED"
                    source.error_code = "SOURCE_SCOPE_MISMATCH"
                    packet.coverage.failed_pages += 1
                    continue
                if source.source_profiles and not source_url_matches_profiles(
                    final_canonical,
                    source.source_profiles,
                ):
                    source.read_status = "FAILED"
                    source.error_code = "SOURCE_SCOPE_MISMATCH"
                    packet.coverage.failed_pages += 1
                    continue
                fingerprint = content_fingerprint(excerpt)
                duplicate = next(
                    (
                        candidate
                        for candidate in packet.sources
                        if candidate is not source
                        and candidate.read_status == "SUCCEEDED"
                        and (
                            candidate.canonical_url == final_canonical
                            or candidate.content_fingerprint == fingerprint
                            or content_is_near_duplicate(candidate.content_excerpt, excerpt)
                        )
                    ),
                    None,
                )
                source.url = document.final_url
                source.canonical_url = final_canonical
                source.retrieval_method = document.retrieval_method
                source.evidence_archive = excerpt_archive
                if used_fallback:
                    source.ranking_reasons = list(
                        dict.fromkeys([*source.ranking_reasons, "BROWSER_FALLBACK"])
                    )
                source.content_fingerprint = fingerprint
                source.evidence_density = estimate_source_evidence_density(source, excerpt)
                source.evidence_level = "FULL_TEXT"
                if not source.source_cluster_id:
                    source.source_cluster_id = source_cluster_id(final_canonical)
                if original_canonical != final_canonical:
                    source.duplicate_urls = list(
                        dict.fromkeys([*source.duplicate_urls, original_canonical])
                    )
                if duplicate is not None:
                    source.read_status = "DUPLICATE"
                    source.duplicate_of = duplicate.canonical_url
                    source.source_cluster_id = duplicate.source_cluster_id or source_cluster_id(
                        duplicate.canonical_url
                    )
                    source.source_independence_score = 0.0
                    source.ranking_reasons = list(
                        dict.fromkeys([*source.ranking_reasons, "DUPLICATE_SOURCE_CLUSTER"])
                    )
                    duplicate.duplicate_urls = list(
                        dict.fromkeys([*duplicate.duplicate_urls, final_canonical])
                    )
                    packet.coverage.duplicate_pages += 1
                    packet.coverage.duplicate_results += 1
                    continue
                source.read_status = "SUCCEEDED"
                source.source_independence_score = 1.0
                source.content_excerpt = excerpt
                if document.truncated or len(document.content) > len(excerpt):
                    source.page_quality_flags = list(dict.fromkeys([*source.page_quality_flags, "EXCERPT_TRUNCATED"]))
                total_chars += len(excerpt)
                round_remaining -= len(excerpt)
                packet.coverage.read_pages += 1
                newly_read += 1
                packet.coverage.truncated_pages += int(document.truncated)
        self._update_source_cluster_coverage(packet)
        return newly_read

    def _read_page_result(self, url, max_chars: int, *, focus: str = ""):
        try:
            document, assessment, used_fallback = self._read_page(
                url,
                max_chars=max_chars,
                focus=focus,
            )
            return document, assessment, used_fallback, None
        except Exception as error:
            return None, None, False, error

    @staticmethod
    def _ordered_read_candidates(
        packet: ResearchPacket, *, enhanced: bool = False,
    ) -> list[ResearchSource]:
        """Round-robin candidates by research question so one category cannot starve others."""
        active_queries = {
            item.query.casefold()
            for item in packet.subqueries
            if not packet.active_stage_key or item.stage_id == packet.active_stage_key
        }
        def scope_allows_read(source):
            related = [q for q in packet.subqueries if q.query in source.matched_queries
                       and (not packet.active_stage_key or q.stage_id == packet.active_stage_key)]
            # Observed profiles can be empty for an unclassified publisher.
            # Check the originating query too, so known third-party pages do
            # not become official candidates merely because their label was cleared.
            return not related or any(
                q.source_scope_mode == "SOFT"
                or not (q.source_scope_mode == "HARD" or (
                    q.query_role in {"AUTHORITATIVE", "VERIFY"} and "official_docs" in q.source_profiles))
                or source_url_matches_profiles(str(source.url), q.source_profiles) for q in related)

        waiting = [
            source
            for source in packet.sources
            if source.read_status == "NOT_SELECTED"
            and ("EXPLICIT_URL" in source.ranking_reasons or scope_allows_read(source))
            and not (
                enhanced and "SNIPPET_FALLBACK" in source.ranking_reasons
                and set(source.ranking_reasons) & {"CONSTRAINT_MISMATCH", "ENTITY_MISMATCH"}
            )
            and (
                "EXPLICIT_URL" in source.ranking_reasons
                or
                (
                    "SNIPPET_FALLBACK" not in source.ranking_reasons
                    and "SOURCE_PROFILE_MISMATCH" not in source.ranking_reasons
                )
                or (
                    "SNIPPET_FALLBACK" in source.ranking_reasons
                    and "SOURCE_PROFILE_MISMATCH" not in source.ranking_reasons
                    and source.source_category != "OTHER"
                    and not is_search_redirect_url(str(source.url))
                )
            )
            and (
                "EXPLICIT_URL" in source.ranking_reasons
                or
                not packet.active_stage_key
                or any(query.casefold() in active_queries for query in source.matched_queries)
            )
        ]
        explicit = [source for source in waiting if "EXPLICIT_URL" in source.ranking_reasons]
        waiting = [source for source in waiting if "EXPLICIT_URL" not in source.ranking_reasons]
        question_order = {
            item.query.casefold(): index for index, item in enumerate(packet.subqueries)
        }
        buckets: dict[str, list[ResearchSource]] = {}
        ungrouped: list[ResearchSource] = []
        for source in waiting:
            group = next(
                (
                    query.casefold()
                    for query in source.matched_queries
                    if query.casefold() in question_order
                ),
                None,
            )
            if group is None:
                ungrouped.append(source)
            else:
                buckets.setdefault(group, []).append(source)

        semantic_order = enhanced or any("BGE_HYBRID_RANK" in s.ranking_reasons for s in waiting)

        def source_priority(source: ResearchSource) -> tuple[bool, int, float]:
            if semantic_order:
                # Unverified official bookmarks are inspected first; weak
                # snippets cannot regain priority through their site category.
                tier = (0 if "MODEL_REFERENCE_CANDIDATE" in source.ranking_reasons
                        else 1 if "BGE_HYBRID_RANK" in source.ranking_reasons
                        else 3 if "SNIPPET_FALLBACK" in source.ranking_reasons
                        else 2)
                return (is_search_redirect_url(str(source.url)), tier, -source.ranking_score)
            return (
                is_search_redirect_url(str(source.url)),
                source.source_category
                not in {"GOVERNMENT", "ACADEMIC", "DOCUMENTATION", "REPOSITORY"},
                -source.ranking_score,
            )

        ordered_groups = sorted(buckets, key=lambda query: question_order[query])
        rows = [sorted(buckets[group], key=source_priority) for group in ordered_groups]
        interleaved = [item for row in zip_longest(*rows) for item in row if item is not None]
        return [*explicit, *interleaved, *sorted(ungrouped, key=source_priority)]

    def _read_page(
        self,
        url,
        *,
        max_chars: int,
        focus: str = "",
    ) -> tuple[object, PageQualityAssessment, bool]:
        request = PageReadRequest(url=url, max_chars=max_chars, include_links=False, focus=focus)
        first_document = None
        first_error: Exception | None = None
        try:
            try:
                first_document = self.service.read(request)
            except ResearchProviderError as error:
                if (error.code != "CRAWL4AI_BUSY" or monotonic() + 0.25 >=
                        getattr(self, "_read_deadline", float("inf"))):
                    raise
                sleep(0.25)
                first_document = self.service.read(request)
            first_assessment = assess_page_quality(first_document)
            definitive_block = bool(
                set(first_assessment.flags)
                & {"AUTH_REQUIRED", "APP_WALL"}
            )
            truncated_api = first_document.truncated and bool(
                re.search(r"(?<![A-Za-z0-9_-])[A-Z][a-z]+(?:[A-Z][A-Za-z0-9]*)+(?![A-Za-z0-9_])", focus)
            ) and bool(re.search(r"报错|异常|取消|规则|处理|exception|cancel|interrupt|behavior|protocol", focus, re.I))
            # Keep one browser attempt for bot/challenge pages: some sites
            # render the actual article only after JavaScript runs. Login and
            # app walls cannot be solved by rendering, so they fail fast.
            if (
                (first_assessment.usable and not truncated_api)
                or not self.limits.enable_browser_fallback
                or definitive_block
            ):
                return first_document, first_assessment, False
        except Exception as exc:
            first_error = exc
            first_assessment = None

        if first_error is not None and isinstance(first_error, ResearchProviderError):
            if first_error.code in {
                "INVALID_URL", "LOGIN_REQUIRED", "APP_WALL", "SEARCH_CHALLENGE",
                "SEARCH_RATE_LIMIT", "CRAWL4AI_UNAVAILABLE", "CRAWL4AI_INVALID_RESULT",
                "CRAWL4AI_BUSY",
            }:
                raise first_error
            if re.search(r"TIMEOUT|UNAVAILABLE|ECONN|BUSY", first_error.code):
                sleep(0.25)

        if self.limits.enable_browser_fallback and monotonic() < getattr(self, "_read_deadline", float("inf")):
            try:
                browser_document = self.service.read(
                    request.model_copy(update={"render_mode": "browser"})
                )
                browser_assessment = assess_page_quality(browser_document)
                if first_assessment is not None and first_assessment.usable and truncated_api:
                    if browser_assessment.usable and "SECTION_FOCUSED" in browser_document.quality_flags:
                        return browser_document, browser_assessment, True
                    return first_document, first_assessment, False
                if first_document is None or browser_assessment.score >= (
                    first_assessment.score if first_assessment is not None else -1
                ):
                    return browser_document, browser_assessment, True
            except Exception as error:
                if first_assessment is not None:
                    failure_flags = [*first_assessment.flags, "BROWSER_FALLBACK_FAILED"]
                    if (
                        isinstance(error, ResearchProviderError)
                        and error.code == "BROWSER_UNAVAILABLE"
                    ):
                        failure_flags.append("BROWSER_UNAVAILABLE")
                    first_assessment = PageQualityAssessment(
                        score=max(0.0, first_assessment.score - 5.0),
                        flags=tuple(dict.fromkeys(failure_flags)),
                        content_length=first_assessment.content_length,
                        usable=first_assessment.usable,
                    )

        if first_document is not None and first_assessment is not None:
            return first_document, first_assessment, False
        if first_error is not None:
            raise first_error
        raise RuntimeError("page read returned no document")

    @staticmethod
    def _page_quality_error_code(assessment: PageQualityAssessment) -> str:
        """Map quality flags to an actionable, stable read failure reason."""
        flags = set(assessment.flags)
        if not assessment.content_length:
            return "EMPTY_PAGE"
        if "AUTH_REQUIRED" in flags:
            return "LOGIN_REQUIRED"
        if "BOT_CHALLENGE" in flags:
            return "BOT_CHALLENGE"
        if "APP_WALL" in flags:
            return "APP_WALL"
        if "SCRIPT_SHELL" in flags or "NAVIGATION_SHELL" in flags:
            return "JS_SHELL"
        if "UNSUPPORTED_CONTENT_TYPE" in flags:
            return "UNSUPPORTED_CONTENT"
        if "LOW_TEXT" in flags:
            return "LOW_TEXT"
        return "LOW_PAGE_QUALITY"

    @staticmethod
    def _attempted_pages(packet: ResearchPacket) -> int:
        return (
            packet.coverage.read_pages
            + packet.coverage.failed_pages
            + packet.coverage.duplicate_pages
            - sum(s.error_code == "CRAWL4AI_BUSY" and s.read_status == "FAILED" for s in packet.sources)
        )

    @staticmethod
    def _update_source_cluster_coverage(packet: ResearchPacket) -> None:
        readable = [
            source
            for source in packet.sources
            if source.read_status in {"SUCCEEDED", "DUPLICATE"} and source.source_cluster_id
        ]
        packet.coverage.source_cluster_count = len(
            {source.source_cluster_id for source in readable}
        )
        packet.coverage.independent_source_count = sum(
            source.read_status == "SUCCEEDED" and source.source_independence_score > 0
            for source in readable
        )
        packet.coverage.duplicate_source_count = sum(
            source.read_status == "DUPLICATE" for source in readable
        )
