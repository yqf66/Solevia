from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable

from .ranking import canonicalize_url, source_cluster_id, tokenize
from .schemas import (
    EvidenceLink,
    EvidencePassage,
    ResearchClaim,
    ResearchConflict,
    ResearchEvidenceAnalysis,
    ResearchPacket,
    ResearchSource,
)

_WHITESPACE = re.compile(r"\s+")
_UNCERTAINTY_MARKS = re.compile(r"[\W_]+", re.UNICODE)


def normalize_evidence_text(value: str) -> str:
    return _WHITESPACE.sub(" ", value).strip()


def normalize_claim_key(value: str) -> str:
    normalized = normalize_evidence_text(value).casefold()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:24]


def deduplicate_uncertainties(values: Iterable[str], *, limit: int = 20) -> list[str]:
    """Remove exact and obvious containment duplicates without semantic model calls."""
    retained: list[tuple[str, str]] = []
    for value in values:
        text = normalize_evidence_text(value)
        key = _UNCERTAINTY_MARKS.sub("", text.casefold())
        if not key:
            continue
        duplicate_index = next(
            (
                index
                for index, (_, existing_key) in enumerate(retained)
                if key == existing_key
                or (min(len(key), len(existing_key)) >= 8 and (
                    key in existing_key or existing_key in key
                ))
            ),
            None,
        )
        if duplicate_index is not None:
            # Keep the more informative wording when one message contains the other.
            if len(key) > len(retained[duplicate_index][1]):
                retained[duplicate_index] = (text, key)
            continue
        retained.append((text, key))
        if len(retained) >= limit:
            break
    return [text for text, _ in retained]


def _passage_id(source: ResearchSource, excerpt: str) -> str:
    digest = hashlib.sha256(
        f"{source.canonical_url}\n{source.content_fingerprint}\n{excerpt}".encode()
    ).hexdigest()[:24]
    return f"web:{digest}"


def build_web_passages(packet: ResearchPacket, *, preserve_markup: bool = False) -> list[EvidencePassage]:
    """Create bounded passages from full text and search snippets.

    Snippets remain weak evidence when a detail page is blocked; validation
    prevents them from being promoted to full-text/direct evidence.
    """
    passages: list[EvidencePassage] = []
    for source in packet.sources:
        is_full_text = source.read_status == "SUCCEEDED"
        original = source.content_excerpt if is_full_text else source.snippet
        text = original if preserve_markup else normalize_evidence_text(original)
        if not text:
            continue
        windows = source.evidence_archive.get("excerpt_windows", []) if is_full_text else []
        if windows:
            # Separate passages prevent a selector from quoting through a gap
            # or merging exceptions from unrelated sections into one quote.
            if not isinstance(windows, list):
                continue
            for window in windows:
                if not isinstance(window, dict) or any(type(window.get(k)) is not int for k in (
                        "stored_start", "stored_end", "char_start", "char_end")):
                    continue
                start, end = window["stored_start"], window["stored_end"]
                if not (0 <= start < end <= len(original) and 0 <= window["char_start"] < window["char_end"]
                        and end - start == window["char_end"] - window["char_start"]):
                    continue
                piece = original[start:end]
                separate = source.model_copy(update={
                    "content_excerpt": piece,
                    "evidence_archive": {k: v for k, v in source.evidence_archive.items() if k != "excerpt_windows"}
                        | {"excerpt_window": window},
                })
                passages.extend(build_web_passages(packet.model_copy(update={"sources": [separate]}),
                                                   preserve_markup=preserve_markup))
            continue
        url = source.canonical_url or str(source.url)
        evidence_level = "FULL_TEXT" if is_full_text else "SEARCH_SNIPPET"
        passages.append(
            EvidencePassage(
                passage_id=_passage_id(source, text),
                source_url_or_uri=url,
                text=text[:12000],
                locator={
                    "kind": "web_excerpt" if is_full_text else "search_snippet",
                    "canonical_url": url,
                    "offset_basis": "stored_raw_markdown_excerpt" if preserve_markup else "normalized_stored_excerpt",
                    "char_start": 0,
                    "char_end": min(len(text), 12000 if is_full_text else 2000),
                },
                content_hash=hashlib.sha256(text.encode("utf-8")).hexdigest(),
                trust_metadata={
                    "source_cluster_id": source.source_cluster_id
                    or source_cluster_id(url),
                    "source_independence_score": source.source_independence_score,
                    "source_category": source.source_category,
                    "source_profiles": source.source_profiles,
                    "read_status": source.read_status,
                    "evidence_archive": source.evidence_archive,
                    "retrieval_method": source.retrieval_method,
                    "page_quality_score": source.page_quality_score,
                    "evidence_level": evidence_level,
                    "is_search_snippet": not is_full_text,
                    "excerpt_truncated": "EXCERPT_TRUNCATED" in source.page_quality_flags or len(text) > 12000,
                    "evidence_density": source.evidence_density,
                },
            )
        )
    return passages


def build_snippet_fallback_analysis(packet: ResearchPacket) -> ResearchEvidenceAnalysis:
    """Turn uncited search summaries into explicitly weak, auditable claims.

    A blocked detail page must not erase a useful search result. These claims
    are deliberately marked SEARCH_SNIPPET/UNVERIFIED and are only a fallback
    for sources that the evidence extractor did not cite. They give the answer
    layer a factual lead without allowing a snippet to become direct evidence.
    """
    passages = {
        item.passage_id: item
        for item in packet.passages
        if item.trust_metadata.get("is_search_snippet")
    }
    cited_urls = {
        canonicalize_url(str(url))
        for claim in packet.claims
        for url in claim.source_urls
    }
    claims: list[ResearchClaim] = []
    for passage in passages.values():
        url = canonicalize_url(passage.source_url_or_uri)
        if not url or url in cited_urls:
            continue
        snippet = normalize_evidence_text(passage.text)[:1800]
        if not snippet:
            continue
        key = normalize_claim_key(f"摘要线索：{snippet}")
        claims.append(
            ResearchClaim(
                claim=f"搜索摘要线索：{snippet}",
                claim_id=f"claim:{key}",
                normalized_key=key,
                evidence_links=[
                    EvidenceLink(
                        passage_id=passage.passage_id,
                        relation="SUPPORTS",
                        quoted_text=snippet[:2000],
                        locator=passage.locator,
                    )
                ],
                source_urls=[url],
                confidence=0.25,
                evidence_level="SEARCH_SNIPPET",
                inference_note="仅来自搜索结果摘要，需读取原文或独立来源复核。",
                status="UNVERIFIED",
            )
        )
        cited_urls.add(url)
        if len(claims) >= 12:
            break
    return ResearchEvidenceAnalysis(claims=claims)


def estimate_source_evidence_density(source: ResearchSource, text: str) -> float:
    """Estimate whether a readable page discusses the requested relationship.

    This is intentionally a small routing signal, not a truth score. Claims only
    become direct evidence after the extractor returns an exact quote.
    """
    content_tokens = tokenize(text[:12000])
    query_tokens = set().union(*(tokenize(query) for query in source.matched_queries))
    if not content_tokens or not query_tokens:
        return 0.0
    overlap = len(content_tokens & query_tokens) / max(len(query_tokens), 1)
    length_bonus = min(len(text) / 12000, 1.0) * 0.15
    return round(min(overlap * 0.85 + length_bonus, 1.0), 3)


def refresh_evidence_metrics(packet: ResearchPacket) -> None:
    """Reconcile source/claim directness after each extraction pass."""
    passage_sources = {
        passage.passage_id: canonicalize_url(passage.source_url_or_uri)
        for passage in packet.passages
    }
    directly_cited_urls = {
        passage_sources[link.passage_id]
        for claim in packet.claims
        if claim.evidence_level == "DIRECT_EVIDENCE"
        for link in claim.evidence_links
        if link.passage_id in passage_sources
    }
    for source in packet.sources:
        if source.read_status != "SUCCEEDED":
            source.evidence_level = "SEARCH_SNIPPET" if source.snippet.strip() else "UNVERIFIED"
            continue
        source.evidence_level = (
            "DIRECT_EVIDENCE"
            if canonicalize_url(source.canonical_url or str(source.url))
            in directly_cited_urls
            else "FULL_TEXT"
        )
    source_levels = {
        canonicalize_url(source.canonical_url or str(source.url)): (
            source.evidence_level,
            source.evidence_density,
        )
        for source in packet.sources
        if source.read_status == "SUCCEEDED"
    }
    for passage in packet.passages:
        metadata = source_levels.get(canonicalize_url(passage.source_url_or_uri))
        if metadata is None:
            continue
        passage.trust_metadata["evidence_level"] = metadata[0]
        passage.trust_metadata["evidence_density"] = metadata[1]
    packet.coverage.summary_only_sources = sum(
        source.evidence_level == "SEARCH_SNIPPET" for source in packet.sources
    )
    packet.coverage.full_text_sources = sum(
        source.evidence_level == "FULL_TEXT" for source in packet.sources
    )
    packet.coverage.direct_evidence_sources = sum(
        source.evidence_level == "DIRECT_EVIDENCE" for source in packet.sources
    )
    packet.coverage.inferred_claims = sum(
        claim.evidence_level == "INFERRED_SUMMARY" for claim in packet.claims
    )


def build_material_passage(
    *,
    passage_id: str,
    source_revision_id: str,
    source_url_or_uri: str,
    text: str,
    locator: dict[str, object] | None = None,
    trust_metadata: dict[str, object] | None = None,
) -> EvidencePassage:
    """Adapt a material chunk to the same evidence contract as web passages."""
    normalized = normalize_evidence_text(text)
    return EvidencePassage(
        passage_id=passage_id,
        source_revision_id=source_revision_id,
        source_url_or_uri=source_url_or_uri,
        text=normalized[:12000],
        locator=locator or {},
        content_hash=hashlib.sha256(normalized.encode("utf-8")).hexdigest(),
        trust_metadata=trust_metadata or {"read_status": "SUCCEEDED"},
    )


def merge_passages(
    existing: Iterable[EvidencePassage], incoming: Iterable[EvidencePassage]
) -> list[EvidencePassage]:
    merged = {item.passage_id: item for item in existing}
    for item in incoming:
        merged.setdefault(item.passage_id, item)
    return list(merged.values())


def _canonical_source_urls(
    sources: Iterable[ResearchSource],
) -> tuple[set[str], dict[str, ResearchSource]]:
    allowed: dict[str, ResearchSource] = {}
    for source in sources:
        if source.read_status != "SUCCEEDED" and not source.snippet.strip():
            continue
        allowed[canonicalize_url(source.canonical_url or str(source.url))] = source
    return set(allowed), allowed


def _quote_matches(quote: str, passage_text: str) -> bool:
    quote = normalize_evidence_text(quote)
    if not quote:
        return False
    return quote.casefold() in normalize_evidence_text(passage_text).casefold()


def is_complete_body_support(link: EvidenceLink, passage: EvidencePassage) -> bool:
    """Structural check; semantic completeness is the extractor's proposal."""
    if (link.relation != "SUPPORTS" or passage.trust_metadata.get("is_search_snippet")
            or not _quote_matches(link.quoted_text, passage.text)):
        return False
    text = normalize_evidence_text(passage.text)
    quote = normalize_evidence_text(link.quoted_text)
    # A quoted unfinished tail cannot close an aspect. Complete earlier rules
    # remain usable even if another part of the page was truncated.
    return not (
        text.casefold().endswith(quote.casefold())
        and not re.search(r'[.!?。！？;；]["”\')\]]*$', text)
    )


def _valid_links(
    links: Iterable[EvidenceLink], passages: dict[str, EvidencePassage]
) -> tuple[list[EvidenceLink], list[str]]:
    valid: list[EvidenceLink] = []
    diagnostics: list[str] = []
    for link in links:
        passage = passages.get(link.passage_id)
        if passage is None:
            diagnostics.append(f"丢弃不存在的证据段落：{link.passage_id}")
            continue
        if not _quote_matches(link.quoted_text, passage.text):
            diagnostics.append(f"丢弃与原文不匹配的引文：{link.passage_id}")
            continue
        valid.append(link)
    return valid, diagnostics


def _focus_tokens(values: Iterable[str]) -> set[str]:
    tokens: set[str] = set()
    for value in values:
        tokens.update(tokenize(value))
    return {token for token in tokens if len(token) >= 2}


def _claim_matches_focus(claim: str, focus_tokens: set[str]) -> bool:
    """Reject obvious off-topic extraction while retaining raw passages.

    A generic question may have no usable focus tokens, so this gate only
    activates when the request contains several concrete entities or constraints.
    Two matching anchors prevent a page that merely mentions one product name
    from becoming evidence for a specific hardware measurement question.
    """
    if len(focus_tokens) < 3:
        return True
    claim_tokens = tokenize(claim)
    def with_word_families(tokens: set[str]) -> set[str]:
        expanded = set(tokens)
        for token in tokens:
            if token.startswith("cancel") or token == "取消":
                expanded.add("cancel")
            if token.startswith("exception") or token == "异常":
                expanded.add("exception")
        return expanded
    claim_tokens = with_word_families(claim_tokens)
    focus_tokens = with_word_families(focus_tokens)
    numeric_anchors = {
        token for token in focus_tokens if any(character.isdigit() for character in token)
    }
    domain_terms = {
        token
        for token in focus_tokens
        if token in {"显存", "速度", "OOM", "模型", "量化", "上下文", "驱动", "兼容"}
    }
    product_terms = {
        token for token in focus_tokens if token.casefold() in {"ollama", "lm", "studio", "rtx"}
    }
    if numeric_anchors and (domain_terms or product_terms) and not (claim_tokens & numeric_anchors):
        if len(claim_tokens & domain_terms) < 1 or not (claim_tokens & product_terms):
            return False
    matched = claim_tokens & focus_tokens
    return len(matched) >= 2


def _status_for_links(
    links: Iterable[EvidenceLink], passages: dict[str, EvidencePassage]
) -> str:
    links = list(links)
    if not links:
        return "UNVERIFIED"
    snippet_only = all(
        bool(passages[link.passage_id].trust_metadata.get("is_search_snippet"))
        for link in links
        if link.passage_id in passages
    )
    if snippet_only:
        return "UNVERIFIED"
    relations = {link.relation for link in links}
    if "CONTRADICTS" in relations:
        return "CONFLICTED"
    if "QUALIFIES" in relations:
        return "PARTIALLY_SUPPORTED"
    clusters = {
        str(passages[link.passage_id].trust_metadata.get("source_cluster_id", ""))
        for link in links
        if link.passage_id in passages
    }
    clusters.discard("")
    primary_profiles = {"government", "official_docs", "github", "arxiv"}
    primary_categories = {"GOVERNMENT", "DOCUMENTATION", "REPOSITORY", "ACADEMIC"}
    has_primary_source = any(
        (
            str(passages[link.passage_id].trust_metadata.get("source_category", ""))
            in primary_categories
            or bool(
                primary_profiles
                & set(passages[link.passage_id].trust_metadata.get("source_profiles", []))
            )
        )
        for link in links
        if link.passage_id in passages
    )
    if len(clusters) == 1 and has_primary_source:
        return "SUPPORTED"
    return "SUPPORTED" if len(clusters) >= 2 else "UNVERIFIED"


def _claim_with_valid_evidence(
    claim: ResearchClaim,
    *,
    passages: dict[str, EvidencePassage],
    allowed_urls: set[str],
    focus_tokens: set[str] | None = None,
) -> tuple[ResearchClaim | None, list[str]]:
    diagnostics: list[str] = []
    text = normalize_evidence_text(claim.claim)
    if not text:
        return None, ["丢弃空事实声明"]
    if focus_tokens and not _claim_matches_focus(text, focus_tokens):
        return None, [f"丢弃与当前研究主题关系过弱的事实：{text[:80]}"]
    key = claim.normalized_key or normalize_claim_key(text)
    links, link_diagnostics = _valid_links(claim.evidence_links, passages)
    diagnostics.extend(link_diagnostics)
    if claim.evidence_links and not links:
        return None, [*diagnostics, f"丢弃没有有效证据引用的事实：{text[:80]}"]

    if links:
        urls = list(
            dict.fromkeys(
                passages[link.passage_id].source_url_or_uri
                for link in links
                if link.passage_id in passages
                and canonicalize_url(passages[link.passage_id].source_url_or_uri)
                in allowed_urls
            )
        )
        # A passage not backed by a successful source is never promoted.
        links = [
            link
            for link in links
            if canonicalize_url(passages[link.passage_id].source_url_or_uri) in allowed_urls
        ]
        if not links:
            return None, [*diagnostics, f"丢弃未关联成功来源的事实：{text[:80]}"]
        snippet_only = all(
            bool(passages[link.passage_id].trust_metadata.get("is_search_snippet"))
            for link in links
            if link.passage_id in passages
        )
        inferred = claim.evidence_level == "INFERRED_SUMMARY"
        status = _status_for_links(links, passages)
        if inferred and status == "SUPPORTED":
            status = "PARTIALLY_SUPPORTED"
        confidence = min(
            claim.confidence,
            0.55
            if snippet_only
            else (0.7 if inferred else (0.95 if status == "SUPPORTED" else 0.75)),
        )
        evidence_level = (
            "INFERRED_SUMMARY"
            if inferred
            else ("SEARCH_SNIPPET" if snippet_only else "DIRECT_EVIDENCE")
        )
        inference_note = (
            claim.inference_note or "该结论由已引证材料综合推断，不是来源原文的直接表述。"
            if inferred
            else ""
        )
    else:
        urls = list(
            dict.fromkeys(
                str(url)
                for url in claim.source_urls
                if canonicalize_url(str(url)) in allowed_urls
            )
        )
        if not urls:
            return None, [*diagnostics, f"丢弃没有成功来源的事实：{text[:80]}"]
        # Legacy URL-only model output is retained but cannot be verified.
        status = "UNVERIFIED"
        confidence = min(claim.confidence, 0.65)
        evidence_level = "UNVERIFIED"
        inference_note = claim.inference_note

    return (
        ResearchClaim.model_validate(
            {
                **claim.model_dump(mode="python"),
                "claim": text,
                "claim_id": claim.claim_id or f"claim:{key}",
                "normalized_key": key,
                "evidence_links": links,
                "source_urls": urls,
                "confidence": confidence,
                "status": status,
                "evidence_level": evidence_level,
                "inference_note": inference_note,
                "answers_aspects": [aspect for aspect in claim.answers_aspects
                    if aspect in claim.covers_aspects
                    and status == "SUPPORTED" and evidence_level == "DIRECT_EVIDENCE"
                    and all(is_complete_body_support(link, passages[link.passage_id]) for link in links)],
            }
        ),
        diagnostics,
    )


def validate_evidence_analysis(
    analysis: ResearchEvidenceAnalysis,
    *,
    passages: Iterable[EvidencePassage],
    sources: Iterable[ResearchSource],
    focus_texts: Iterable[str] = (),
) -> tuple[ResearchEvidenceAnalysis, list[str]]:
    passage_map = {item.passage_id: item for item in passages}
    allowed_urls, _ = _canonical_source_urls(sources)
    claims: list[ResearchClaim] = []
    diagnostics: list[str] = []
    focus_tokens = _focus_tokens(focus_texts)
    seen: set[str] = set()
    for claim in analysis.claims:
        validated, errors = _claim_with_valid_evidence(
            claim,
            passages=passage_map,
            allowed_urls=allowed_urls,
            focus_tokens=focus_tokens,
        )
        diagnostics.extend(errors)
        if validated is None or validated.normalized_key in seen:
            continue
        seen.add(validated.normalized_key)
        claims.append(validated)

    conflicts: list[ResearchConflict] = []
    for conflict in analysis.conflicts:
        positions: list[dict[str, object]] = []
        for position in conflict.positions:
            position_links = [
                EvidenceLink.model_validate(link)
                for link in position.get("evidence_links", [])
            ]
            valid_links, errors = _valid_links(position_links, passage_map)
            diagnostics.extend(errors)
            urls = [
                str(url)
                for url in position.get("source_urls", [])
                if canonicalize_url(str(url)) in allowed_urls
            ]
            urls.extend(
                passage_map[link.passage_id].source_url_or_uri
                for link in valid_links
                if canonicalize_url(passage_map[link.passage_id].source_url_or_uri)
                in allowed_urls
            )
            urls = list(dict.fromkeys(urls))
            if urls or valid_links:
                positions.append(
                    {
                        **position,
                        "source_urls": urls,
                        "evidence_links": valid_links,
                    }
                )
        if len(positions) >= 2:
            topic = normalize_evidence_text(conflict.topic)
            key = conflict.claim_key or normalize_claim_key(topic)
            conflicts.append(
                conflict.model_copy(
                    update={
                        "topic": topic,
                        "claim_key": key,
                        "conflict_id": conflict.conflict_id or f"conflict:{key}",
                        "positions": positions,
                        "resolution": "UNRESOLVED",
                    }
                )
            )

    return (
        ResearchEvidenceAnalysis(
            claims=claims,
            conflicts=conflicts,
            uncertain_points=analysis.uncertain_points,
            processed_passage_ids=analysis.processed_passage_ids,
        ),
        diagnostics,
    )


def merge_evidence(
    packet: ResearchPacket, analysis: ResearchEvidenceAnalysis
) -> None:
    """Idempotently merge one extraction batch into the packet."""
    by_key = {
        claim.normalized_key or normalize_claim_key(claim.claim): claim
        for claim in packet.claims
    }
    passage_map = {item.passage_id: item for item in packet.passages}
    for incoming in analysis.claims:
        key = incoming.normalized_key or normalize_claim_key(incoming.claim)
        current = by_key.get(key)
        if current is None:
            by_key[key] = incoming
            continue
        links = {link.model_dump_json(): link for link in current.evidence_links}
        links.update({link.model_dump_json(): link for link in incoming.evidence_links})
        urls = list(
            dict.fromkeys([*map(str, current.source_urls), *map(str, incoming.source_urls)])
        )
        status = _status_for_links(links.values(), passage_map) if links else "UNVERIFIED"
        evidence_level = (
            "INFERRED_SUMMARY"
            if "INFERRED_SUMMARY" in {current.evidence_level, incoming.evidence_level}
            else ("DIRECT_EVIDENCE" if links else "UNVERIFIED")
        )
        if evidence_level == "INFERRED_SUMMARY" and status == "SUPPORTED":
            status = "PARTIALLY_SUPPORTED"
        confidence = max(current.confidence, incoming.confidence)
        if evidence_level == "INFERRED_SUMMARY":
            confidence = min(confidence, 0.7)
        by_key[key] = ResearchClaim.model_validate(
            {
                **current.model_dump(mode="python"),
                "evidence_links": list(links.values()),
                "source_urls": urls,
                "confidence": confidence,
                "status": status,
                "evidence_level": evidence_level,
                "inference_note": incoming.inference_note or current.inference_note,
                "covers_aspects": list(dict.fromkeys([*current.covers_aspects, *incoming.covers_aspects])),
                "answers_aspects": list(dict.fromkeys([*current.answers_aspects, *incoming.answers_aspects]))
                    if status == "SUPPORTED" and evidence_level == "DIRECT_EVIDENCE" else [],
            }
        )
    packet.claims = list(by_key.values())[:20]
    conflicts = {
        item.conflict_id or f"conflict:{normalize_claim_key(item.topic)}": item
        for item in packet.conflicts
    }
    for conflict in analysis.conflicts:
        conflict_key = conflict.conflict_id or f"conflict:{normalize_claim_key(conflict.topic)}"
        current = conflicts.get(conflict_key)
        if current is None:
            conflicts[conflict_key] = conflict
            continue
        position_keys = {str(position): position for position in current.positions}
        position_keys.update({str(position): position for position in conflict.positions})
        conflicts[conflict_key] = current.model_copy(
            update={"positions": list(position_keys.values())[:10]}
        )
    packet.conflicts = list(conflicts.values())[:10]
    packet.uncertain_points = deduplicate_uncertainties(
        [*packet.uncertain_points, *analysis.uncertain_points]
    )
