from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable

from .query_planner import (
    classify_query_purpose,
    infer_query_role,
    preserve_query_constraints,
    synchronize_query_constraints,
)
from .ranking import canonicalize_url
from .evidence import is_complete_body_support
from .schemas import (
    ResearchAction,
    ResearchGap,
    ResearchPacket,
    ResearchStrategyAttempt,
    ResearchSubquery,
)
from .sources import normalize_source_profiles, source_matches_requested_version

_TEXT_KEY = re.compile(r"[^0-9a-z㐀-鿿]+", re.IGNORECASE)
_PRIMARY_PROFILES = {"government", "official_docs", "github", "arxiv"}
_PRIMARY_CATEGORIES = {"GOVERNMENT", "DOCUMENTATION", "REPOSITORY", "ACADEMIC"}
_GAP_PRIORITY = {
    "CONFLICT": 0,
    "MISSING_EVIDENCE": 1,
    "STALE_EVIDENCE": 2,
    "WEAK_AUTHORITY": 3,
    "LOW_DIVERSITY": 4,
    "ACCESS_FAILURE": 5,
}
_SEVERITY_PRIORITY = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}


def _normalized_text(value: str) -> str:
    return _TEXT_KEY.sub("", value.casefold())


def _same_query_family(left: str, right: str) -> bool:
    left_sites = set(re.findall(r"(?:^|\s)site:([^\s]+)", left.casefold()))
    right_sites = set(re.findall(r"(?:^|\s)site:([^\s]+)", right.casefold()))
    if left_sites != right_sites:
        return False
    left_key = _normalized_text(left)
    right_key = _normalized_text(right)
    if not left_key or not right_key:
        return False
    if left_key == right_key:
        return True
    shorter, longer = sorted((left_key, right_key), key=len)
    return len(shorter) >= 12 and shorter in longer


def _aspect_queries(packet: ResearchPacket, aspect: str) -> set[str]:
    return {
        item.query.casefold()
        for item in packet.subqueries
        if aspect in item.covers_aspects
    }


def _aspect_purpose(packet: ResearchPacket, aspect: str) -> str | None:
    candidate = next(
        (
            item
            for item in sorted(packet.subqueries, key=lambda row: -row.priority)
            if aspect in item.covers_aspects
        ),
        None,
    )
    return candidate.purpose if candidate is not None else None


def _inherit_aspect_constraints(
    packet: ResearchPacket,
    aspect: str,
    followup: ResearchSubquery,
) -> None:
    """Keep the original entity/relation contract when the model rewrites a query."""
    related = [
        item
        for item in sorted(packet.subqueries, key=lambda row: -row.priority)
        if aspect in item.covers_aspects
    ]
    hard_scope = next((item for item in related if item.source_scope_mode == "HARD"), None)
    if hard_scope is not None:
        # A rewrite/source switch may explore within the user's scope, not lift it.
        followup.source_scope_mode = "HARD"
        followup.source_profiles = list(hard_scope.source_profiles)
    for item in related:
        synchronize_query_constraints(item)
    synchronize_query_constraints(
        followup,
        inherited=[item.constraints for item in related],
    )
    followup.query = preserve_query_constraints(followup)


def _aspect_sources(packet: ResearchPacket, aspect: str):
    queries = _aspect_queries(packet, aspect)
    return [
        source
        for source in packet.sources
        if (source.read_status == "SUCCEEDED" or source.snippet.strip())
        and any(query.casefold() in queries for query in source.matched_queries)
    ]


def _aspect_claims(packet: ResearchPacket, aspect: str):
    source_urls = {
        canonicalize_url(source.canonical_url or str(source.url))
        for source in _aspect_sources(packet, aspect)
    }
    queries = [item for item in packet.subqueries if aspect in item.covers_aspects]
    # Distinctive API/project names must occur in the actual claim or quotation.
    # A broad overview of the same language/version does not answer an API rule.
    api_terms = {
        term.casefold() for item in queries
        for term in re.findall(r"\b[A-Z][a-z]+(?:[A-Z][A-Za-z0-9]*)+\b", item.query_intent or item.query)
    }
    def relevant(claim) -> bool:
        if claim.covers_aspects and aspect not in claim.covers_aspects:
            return False
        text = " ".join([claim.claim, *(link.quoted_text for link in claim.evidence_links)]).casefold()
        for aliases in (("异常", "exception"), ("取消", "cancel")):
            if any(word in aspect.casefold() for word in aliases) and not any(word in text for word in aliases):
                return False
        return not api_terms or any(term in text for term in api_terms)

    return [
        claim
        for claim in packet.claims
        if relevant(claim) and any(canonicalize_url(str(url)) in source_urls for url in claim.source_urls)
    ]


def required_research_aspects(packet: ResearchPacket) -> list[str]:
    return list(dict.fromkeys([
        *(packet.brief.required_aspects if packet.brief else []),
        *(aspect for stage in (packet.decision.research_stages if packet.decision else [])
          for aspect in stage.required_aspects),
    ]))[:12]


def _body_meets_version(packet: ResearchPacket, aspect: str, passage) -> bool:
    specifications = [term for q in packet.subqueries if aspect in q.covers_aspects
                      for term in q.constraints.specifications]
    return source_matches_requested_version(passage.source_url_or_uri, specifications)


def assess_research_aspects(packet: ResearchPacket) -> list[dict[str, object]]:
    """Build deterministic answerability and evidence-strength summaries per aspect."""
    required = required_research_aspects(packet)
    assessments: list[dict[str, object]] = []
    passages = {p.passage_id: p for p in packet.passages}
    from .selection import validated_selected_evidence
    selections = validated_selected_evidence(packet) if packet.evidence_mode == "SPANS" else []
    for aspect in required:
        sources = _aspect_sources(packet, aspect)
        claims = [] if packet.evidence_mode == "SPANS" else _aspect_claims(packet, aspect)
        candidates = [e for e in selections if aspect in e.covers_aspects
                      and e.passage_id in passages
                      and not passages[e.passage_id].trust_metadata.get("is_search_snippet")
                      and passages[e.passage_id].trust_metadata.get("read_status") == "SUCCEEDED"
                      and any(s.read_status == "SUCCEEDED"
                              and canonicalize_url(s.canonical_url or str(s.url)) == canonicalize_url(e.source_url_or_uri)
                              for s in sources)
                      and _body_meets_version(packet, aspect, passages[e.passage_id])]
        by_id = {e.evidence_id: e for e in candidates}
        verdicts = [v for v in packet.aspect_evidence_review.verdicts if v.aspect == aspect]
        verified_spans = len(verdicts) == 1 and verdicts[0].status == "SUPPORTED" \
            and not verdicts[0].missing_conditions and bool(verdicts[0].evidence_ids) \
            and all(key in by_id and not by_id[key].locator.get("contains_fragment")
                    for key in verdicts[0].evidence_ids)
        statuses = {claim.status for claim in claims}
        independent_sources = {
            source.source_cluster_id or canonicalize_url(source.canonical_url)
            for source in sources
        }
        primary_sources = [
            source
            for source in sources
            if source.source_category in _PRIMARY_CATEGORIES
            or bool(set(source.source_profiles) & _PRIMARY_PROFILES)
        ]
        direct_sources = [
            source for source in sources if source.evidence_level == "DIRECT_EVIDENCE"
        ]
        inferred_claims = [
            claim for claim in claims if claim.evidence_level == "INFERRED_SUMMARY"
        ]
        claim_keys = {c.normalized_key for c in claims if c.normalized_key}
        claim_passages = {link.passage_id for c in claims for link in c.evidence_links}
        has_conflict = any(
            conflict.resolution == "UNRESOLVED" and (
                conflict.claim_key in claim_keys
                or any((link.get("passage_id", "") if isinstance(link, dict)
                        else getattr(link, "passage_id", "")) in claim_passages
                       for position in conflict.positions
                       for link in position.get("evidence_links", []))
            ) for conflict in packet.conflicts
        )
        if "CONFLICTED" in statuses or has_conflict or any(v.status == "CONFLICTED" for v in verdicts):
            strength = "CONFLICTED"
            answerability = "PARTIAL"
        elif verified_spans:
            strength = "STRONG"
            answerability = "ANSWERED"
        elif any(
            aspect in claim.answers_aspects and aspect in claim.covers_aspects and claim.status == "SUPPORTED"
            and claim.evidence_links and claim.evidence_level == "DIRECT_EVIDENCE"
            and all(link.passage_id in passages
                    and is_complete_body_support(link, passages[link.passage_id])
                    and _body_meets_version(packet, aspect, passages[link.passage_id])
                    and any(s.read_status == "SUCCEEDED"
                            and canonicalize_url(s.canonical_url) == canonicalize_url(passages[link.passage_id].source_url_or_uri)
                            for s in sources)
                    for link in claim.evidence_links)
            for claim in claims
        ):
            strength = "STRONG"
            answerability = "ANSWERED"
        elif "PARTIALLY_SUPPORTED" in statuses:
            strength = "MEDIUM"
            answerability = "PARTIAL"
        elif candidates:
            strength = "CANDIDATE"
            answerability = "PARTIAL"
        elif claims or sources:
            strength = "WEAK"
            answerability = "PARTIAL"
        else:
            strength = "MISSING"
            answerability = "UNANSWERED"
        assessments.append(
            {
                "aspect": aspect,
                "answerability": answerability,
                "evidence_strength": strength,
                "claim_count": len(claims),
                "candidate_evidence_count": len(candidates),
                "verification_reason": verdicts[0].reason if len(verdicts) == 1 else "",
                "missing_conditions": verdicts[0].missing_conditions if len(verdicts) == 1 else [],
                "source_count": len(sources),
                "independent_source_count": len(independent_sources),
                "primary_source_count": len(primary_sources),
                "direct_evidence_source_count": len(direct_sources),
                "inferred_claim_count": len(inferred_claims),
                "summary_only_source_count": sum(
                    source.evidence_level == "SEARCH_SNIPPET" for source in sources
                ),
                "claim_statuses": sorted(statuses),
                "gap_types": [
                    gap.gap_type for gap in packet.gaps if gap.aspect == aspect
                ],
            }
        )
    return assessments


def refresh_research_coverage(packet: ResearchPacket) -> None:
    """One evidence gate for stopping, final status and reused answer context."""
    required = required_research_aspects(packet)
    material = {aspect for q in packet.subqueries if q.status in {"EVIDENCE_READY", "ANSWERED"}
                for aspect in q.covers_aspects}
    if packet.evidence_mode == "SPANS":
        from .selection import validated_selected_evidence
        material.update(a for e in validated_selected_evidence(packet) for a in e.covers_aspects)
    answerable = {str(a["aspect"]) for a in assess_research_aspects(packet) if a["answerability"] == "ANSWERED"}
    if not required and not packet.claims:
        answerable = material
    packet.coverage.material_aspects = [a for a in required if a in material]
    packet.coverage.answerable_aspects = [a for a in required if a in answerable]
    packet.coverage.covered_aspects = list(packet.coverage.answerable_aspects)
    packet.coverage.uncovered_aspects = [a for a in required if a not in answerable]
    if packet.status == "READY" and (
        packet.coverage.uncovered_aspects
        or any(g.gap_type == "STALE_EVIDENCE" for g in packet.gaps)
        or any(c.resolution == "UNRESOLVED" for c in packet.conflicts)
    ):
        packet.status = "PARTIAL"


def diagnose_research_gaps(
    packet: ResearchPacket,
    proposed: Iterable[ResearchGap],
) -> list[ResearchGap]:
    """Keep one current, evidence-compatible gap for each required aspect."""
    required = required_research_aspects(packet)
    allowed_aspects = set(required)
    assessments = {
        str(item["aspect"]): item for item in assess_research_aspects(packet)
    }
    candidates: dict[str, list[ResearchGap]] = {aspect: [] for aspect in required}
    for gap in proposed:
        if allowed_aspects and gap.aspect not in allowed_aspects:
            continue
        strength = str(
            assessments.get(gap.aspect, {}).get("evidence_strength", "MISSING")
        )
        if strength == "STRONG" and gap.gap_type != "STALE_EVIDENCE":
            continue
        if strength == "CONFLICTED" and gap.gap_type != "CONFLICT":
            continue
        if strength == "MISSING" and gap.gap_type not in {
            "MISSING_EVIDENCE",
            "ACCESS_FAILURE",
        }:
            continue
        if strength in {"WEAK", "MEDIUM"} and gap.gap_type in {
            "MISSING_EVIDENCE",
            "CONFLICT",
        }:
            continue
        gap.preferred_source_profiles = normalize_source_profiles(
            gap.preferred_source_profiles
        )[:8]
        candidates.setdefault(gap.aspect, []).append(gap)

    for assessment in assessments.values():
        aspect = str(assessment["aspect"])
        strength = str(assessment["evidence_strength"])
        if strength == "MISSING":
            gap_type = "MISSING_EVIDENCE"
            reason = "尚未取得能够直接回答该必答项的已读证据。"
            severity = "HIGH"
        elif strength == "CONFLICTED":
            gap_type = "CONFLICT"
            reason = "现有事实存在尚未裁决的来源冲突。"
            severity = "HIGH"
        elif strength in {"WEAK", "MEDIUM", "CANDIDATE"}:
            gap_type = "WEAK_AUTHORITY"
            reason = str(assessment.get("verification_reason") or "已有材料只能部分回答，或缺少足够权威的直接证据。")
            if assessment.get("missing_conditions"):
                reason += " 待补查：" + "；".join(assessment["missing_conditions"])
            reason = reason[:400]
            severity = "MEDIUM"
        else:
            continue
        candidates.setdefault(aspect, []).append(
            ResearchGap(
                aspect=aspect,
                gap_type=gap_type,
                severity=severity,
                evidence_needed="补充能够直接支持该必答项的可核查来源。",
                reason=reason,
            )
        )
    result: list[ResearchGap] = []
    for aspect in required or candidates:
        options = candidates.get(aspect, [])
        if not options:
            continue
        options.sort(
            key=lambda item: (
                _GAP_PRIORITY[item.gap_type],
                _SEVERITY_PRIORITY[item.severity],
            )
        )
        result.append(options[0])
    return result[:12]


def actions_from_legacy_followups(
    packet: ResearchPacket,
    followups: Iterable[ResearchSubquery],
) -> list[ResearchAction]:
    """Map legacy follow-ups into the bounded adaptive strategy vocabulary."""
    gaps = sorted(
        packet.gaps,
        key=lambda item: (
            _SEVERITY_PRIORITY[item.severity],
            _GAP_PRIORITY[item.gap_type],
        ),
    )
    gap_aspects = {gap.aspect for gap in gaps}
    required_aspects = list(packet.brief.required_aspects if packet.brief else [])
    actions: list[ResearchAction] = []
    seen: set[tuple[str, str]] = set()
    for followup in followups:
        aspect = next(
            (item for item in followup.covers_aspects if item in gap_aspects),
            next(
                (item for item in followup.covers_aspects if item in required_aspects),
                gaps[0].aspect if gaps else (required_aspects[0] if required_aspects else ""),
            ),
        )
        if not aspect:
            continue
        target_profiles = normalize_source_profiles(followup.source_profiles)[:8]
        previous_profiles = {
            profile
            for question in packet.subqueries
            if aspect in question.covers_aspects
            for profile in normalize_source_profiles(question.source_profiles)
        }
        if followup.purpose == "RISK":
            kind = "VERIFY_CLAIM"
        elif target_profiles and not set(target_profiles).issubset(previous_profiles):
            kind = "SWITCH_SOURCE"
        else:
            kind = "REWRITE_QUERY"
        identity = (aspect, _normalized_text(followup.query))
        if identity in seen:
            continue
        seen.add(identity)
        actions.append(
            ResearchAction(
                kind=kind,
                aspect=aspect,
                stage_key=followup.stage_id,
                source_profiles=target_profiles,
                query_intent=followup.query,
                priority=followup.priority,
                reason="兼容旧版补查候选，并纳入策略尝试去重与收益跟踪。",
            )
        )
    return actions[:12]


def _action_signature(action: ResearchAction, profiles: list[str]) -> str:
    raw = "|".join(
        [
            action.kind,
            _normalized_text(action.aspect),
            _normalized_text(action.query_intent),
            ",".join(profiles),
        ]
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def prepare_strategy_followups(
    packet: ResearchPacket,
    actions: Iterable[ResearchAction],
    *,
    max_new: int,
    reserved_queries: Iterable[str] = (),
) -> tuple[list[ResearchSubquery], bool]:
    """Turn safe, novel strategy actions into executable research questions."""
    if max_new <= 0:
        return [], False
    required = set(packet.brief.required_aspects if packet.brief else [])
    valid_stages = {
        stage.key for stage in (packet.decision.research_stages if packet.decision else [])
    }
    attempted = {item.signature for item in packet.memory.strategy_attempts}
    existing = [item.query for item in packet.subqueries]
    reserved = list(reserved_queries)
    followups: list[ResearchSubquery] = []
    stop_requested = False

    for action in sorted(actions, key=lambda item: -item.priority):
        if required and action.aspect not in required:
            continue
        if action.kind == "STOP_WITH_LIMITATION":
            stop_requested = True
            continue
        query = " ".join(action.query_intent.split())
        if not query:
            continue
        profiles = normalize_source_profiles(action.source_profiles)[:8]
        if not profiles:
            matching_gap = next(
                (gap for gap in packet.gaps if gap.aspect == action.aspect), None
            )
            profiles = list(
                matching_gap.preferred_source_profiles if matching_gap else []
            )
        signature = _action_signature(action, profiles)
        if signature in attempted:
            continue
        if any(_normalized_text(query) == _normalized_text(item) for item in existing):
            continue
        if any(
            _same_query_family(query, item)
            for item in [*existing, *reserved, *(row.query for row in followups)]
            if profiles
            == next(
                (
                    candidate.source_profiles
                    for candidate in packet.subqueries
                    if candidate.query == item
                ),
                profiles,
            )
        ):
            continue
        stage_key = action.stage_key if action.stage_key in valid_stages else ""
        if not stage_key:
            stage_key = packet.active_stage_key or ""
        purpose = "RISK" if action.kind == "VERIFY_CLAIM" else (
            _aspect_purpose(packet, action.aspect) or classify_query_purpose(query)
        )
        question_key = f"adaptive-{signature[:12]}"
        followup = ResearchSubquery(
            key=question_key,
            query=query,
            query_intent=query,
                query_role=(
                    "VERIFY"
                    if action.kind == "VERIFY_CLAIM"
                    else infer_query_role(purpose, profiles, query)
                ),
            primary_aspect=action.aspect,
            source_profiles=profiles,
            purpose=purpose,
            priority=action.priority,
            required=True,
            covers_aspects=[action.aspect],
            stage_id=stage_key,
        )
        _inherit_aspect_constraints(packet, action.aspect, followup)
        followups.append(followup)
        packet.memory.strategy_attempts.append(
            ResearchStrategyAttempt(
                signature=signature,
                round_index=packet.coverage.rounds,
                action_kind=action.kind,
                aspect=action.aspect,
                stage_key=stage_key,
                question_key=question_key,
                query=query,
                source_profiles=profiles,
            )
        )
        attempted.add(signature)
        if len(followups) >= max_new:
            break
    packet.memory.strategy_attempts = packet.memory.strategy_attempts[-20:]
    return followups, stop_requested and not followups


def update_strategy_attempt_outcomes(packet: ResearchPacket) -> None:
    questions = {item.key: item for item in packet.subqueries}
    for attempt in packet.memory.strategy_attempts:
        question = questions.get(attempt.question_key)
        if question is None:
            continue
        attempt.outcome = {
            "PLANNED": "PLANNED",
            "SEARCHED": "SEARCHED",
            "EVIDENCE_READY": "EVIDENCE_READY",
            "ANSWERED": "EVIDENCE_READY",
            "BLOCKED": "BLOCKED",
        }[question.status]


def update_low_yield_state(
    packet: ResearchPacket,
    *,
    previously_covered: set[str],
) -> bool:
    """Stop after two rounds that add neither claims nor required-aspect coverage."""
    current = next(
        (
            row
            for row in reversed(packet.memory.recent_rounds)
            if row.round_index == packet.coverage.rounds
        ),
        None,
    )
    new_claims = bool(current and current.new_claim_ids)
    new_coverage = bool(set(packet.coverage.material_aspects or packet.coverage.covered_aspects) - previously_covered)
    if new_claims or new_coverage:
        packet.memory.consecutive_low_yield_rounds = 0
    else:
        packet.memory.consecutive_low_yield_rounds = min(
            packet.memory.consecutive_low_yield_rounds + 1,
            3,
        )
    return packet.memory.consecutive_low_yield_rounds >= 2
