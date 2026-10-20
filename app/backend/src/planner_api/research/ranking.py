from __future__ import annotations

import hashlib
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from difflib import SequenceMatcher
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .query_planner import extract_query_constraints, project_name
from .schemas import ResearchQueryConstraints, SearchHit

_ASCII_TOKEN_PATTERN = re.compile(r"[a-z0-9]+", re.IGNORECASE)
_CJK_PATTERN = re.compile(r"[\u4e00-\u9fff]+")
_TRACKING_KEYS = {"fbclid", "gclid", "ref", "ref_src", "source", "spm", "yclid"}
_LOW_INFORMATION_TITLES = {"home", "index", "login", "sign in", "首页", "登录", "主页"}
_AD_MARKERS = ("advertisement", "sponsored", "推广链接", "广告")
_COMMUNITY_INTENT_CUES = (
    "实测",
    "体验",
    "踩坑",
    "避坑",
    "真实经验",
    "真实反馈",
    "测评",
    "分析",
    "讨论",
    "对比",
    "问题",
    "解决",
)
_COMMUNITY_HOSTS = {
    "blog.csdn.net",
    "juejin.cn",
    "reddit.com",
    "stackoverflow.com",
    "www.zhihu.com",
    "zhihu.com",
    "tieba.baidu.com",
    "bilibili.com",
    "douban.com",
    "linux.do",
}
_JOB_POSTING_HOSTS = {
    "zhipin.com",
    "liepin.com",
    "zhaopin.com",
    "51job.com",
    "nowcoder.com",
    "linkedin.com",
}
_ACADEMIC_HOSTS = {
    "arxiv.org",
    "doi.org",
    "nature.com",
    "pubmed.ncbi.nlm.nih.gov",
    "researchgate.net",
    "sciencedirect.com",
    "springer.com",
}
_SEARCH_HOSTS = {"baidu.com", "bing.com", "duckduckgo.com", "google.com", "sogou.com"}
_STOP_WORDS = {
    "a",
    "an",
    "and",
    "for",
    "how",
    "in",
    "is",
    "of",
    "on",
    "or",
    "the",
    "to",
    "what",
    "with",
    "一下",
    "什么",
    "如何",
    "怎么",
    "相关",
}


@dataclass(frozen=True)
class RankingPolicy:
    min_relevance_score: float = 18.0
    max_per_domain: int = 3
    near_duplicate_threshold: float = 0.86
    allow_search_redirects: bool = False
    allow_partial_overlap: bool = False
    allow_source_intent_mismatch: bool = False
    allow_exploratory_entity_mismatch: bool = False
    enforce_exact_terms: bool = False
    # Keep useful search snippets even when the page itself fails a strict
    # candidate gate.  These remain weak evidence and are never promoted to
    # full-text evidence by the orchestrator.
    allow_snippet_fallback: bool = False


@dataclass
class RankedHit:
    hit: SearchHit
    canonical_url: str
    engines: list[str] = field(default_factory=list)
    matched_queries: list[str] = field(default_factory=list)
    duplicate_urls: list[str] = field(default_factory=list)
    source_category: str = "OTHER"
    relevance_score: float = 0.0
    quality_score: float = 0.0
    freshness_score: float = 0.0
    score: float = 0.0
    reasons: list[str] = field(default_factory=list)
    source_profiles: list[str] = field(default_factory=list)


@dataclass
class RankingOutcome:
    accepted: list[RankedHit] = field(default_factory=list)
    raw_count: int = 0
    filtered_count: int = 0
    duplicate_count: int = 0
    diversity_dropped: int = 0
    filter_reasons: dict[str, int] = field(default_factory=dict)
    snippet_fallback: list[RankedHit] = field(default_factory=list)


def canonicalize_url(value: str) -> str:
    parsed = urlsplit(value.strip())
    hostname = (parsed.hostname or "").casefold().removeprefix("www.")
    if not hostname:
        return value.strip()
    port = parsed.port
    netloc = hostname
    if port and not (
        (parsed.scheme == "http" and port == 80) or (parsed.scheme == "https" and port == 443)
    ):
        netloc = f"{hostname}:{port}"
    query = urlencode(
        sorted(
            (key, item)
            for key, item in parse_qsl(parsed.query, keep_blank_values=True)
            if key.casefold() not in _TRACKING_KEYS and not key.casefold().startswith("utm_")
        )
    )
    path = re.sub(r"/{2,}", "/", parsed.path).rstrip("/") or "/"
    if path.casefold().endswith("/index.html"):
        path = path[: -len("index.html")].rstrip("/") or "/"
    return urlunsplit((parsed.scheme.casefold(), netloc, path, query, ""))


def tokenize(value: str) -> set[str]:
    normalized = value.casefold()
    tokens = {
        token for token in _ASCII_TOKEN_PATTERN.findall(normalized) if token not in _STOP_WORDS
    }
    for sequence in _CJK_PATTERN.findall(normalized):
        if len(sequence) <= 2:
            tokens.add(sequence)
        else:
            tokens.update(sequence[index : index + 2] for index in range(len(sequence) - 1))
    return tokens


def source_domain(value: str) -> str:
    return (urlsplit(value).hostname or "").casefold().removeprefix("www.")


def source_diversity_key(value: str) -> str:
    """Count independent code repositories separately on shared hosting domains."""
    parsed = urlsplit(value)
    host = source_domain(value)
    if host in {"github.com", "gitlab.com", "codeberg.org"}:
        parts = [part.casefold() for part in parsed.path.split("/") if part]
        if len(parts) >= 2:
            return f"{host}/{parts[0]}/{parts[1]}"
    return host


def classify_source(value: str) -> str:
    parsed = urlsplit(value)
    host = source_domain(value)
    path = parsed.path.casefold()
    if host.endswith((".gov", ".gov.cn")):
        return "GOVERNMENT"
    if host.endswith((".edu", ".edu.cn", ".ac.cn")) or any(
        host == item or host.endswith(f".{item}") for item in _ACADEMIC_HOSTS
    ):
        return "ACADEMIC"
    if host in {"github.com", "gitlab.com", "codeberg.org"}:
        return "REPOSITORY"
    if (host in {"learn.microsoft.com", "sqlite.org"}
            or host.startswith("docs.") or "readthedocs" in host or "/docs/" in f"{path}/"):
        return "DOCUMENTATION"
    if any(host == item or host.endswith(f".{item}") for item in _COMMUNITY_HOSTS):
        return "COMMUNITY"
    if any(host == item or host.endswith(f".{item}") for item in _JOB_POSTING_HOSTS):
        return "JOB_POSTING"
    if host.startswith("news.") or "/news/" in f"{path}/":
        return "NEWS"
    return "OTHER"


def rank_candidates(
    query: str,
    hits: list[SearchHit],
    *,
    max_results: int,
    purpose: str = "GENERAL",
    policy: RankingPolicy | None = None,
    constraints: ResearchQueryConstraints | None = None,
    query_role: str = "DISCOVERY",
) -> RankingOutcome:
    policy = policy or RankingPolicy()
    constraints = constraints or extract_query_constraints(query)
    outcome = RankingOutcome(raw_count=len(hits))
    merged: dict[str, RankedHit] = {}
    reason_counts: Counter[str] = Counter()

    for hit in hits:
        canonical = canonicalize_url(str(hit.url))
        rejection = _reject_reason(
            query,
            hit,
            canonical,
            purpose,
            allow_search_redirects=policy.allow_search_redirects,
            allow_partial_overlap=policy.allow_partial_overlap,
            allow_source_intent_mismatch=policy.allow_source_intent_mismatch,
            allow_exploratory_entity_mismatch=policy.allow_exploratory_entity_mismatch,
            enforce_exact_terms=policy.enforce_exact_terms,
            constraints=constraints,
            query_role=query_role,
        )
        if rejection:
            if policy.allow_snippet_fallback and _snippet_fallback_eligible(
                query, hit, canonical
            ):
                fallback = _score(query, hit, canonical, purpose, constraints=constraints)
                fallback.reasons = list(
                    dict.fromkeys([*fallback.reasons, "SNIPPET_FALLBACK", rejection])
                )
                outcome.snippet_fallback.append(fallback)
            outcome.filtered_count += 1
            reason_counts[rejection] += 1
            continue
        current = merged.get(canonical)
        if current is not None:
            outcome.duplicate_count += 1
            _merge_exact(current, hit)
            continue
        merged[canonical] = _score(query, hit, canonical, purpose, constraints=constraints)

    for canonical, item in list(merged.items()):
        item.hit.engines = item.engines
        merged[canonical] = _score(
            query, item.hit, canonical, purpose, constraints=constraints
        )
    ranked = sorted(merged.values(), key=lambda item: (-item.score, item.canonical_url))
    unique: list[RankedHit] = []
    for item in ranked:
        duplicate = next(
            (
                existing
                for existing in unique
                if are_near_duplicate(
                    existing.hit.title,
                    existing.hit.snippet,
                    item.hit.title,
                    item.hit.snippet,
                    threshold=policy.near_duplicate_threshold,
                )
            ),
            None,
        )
        if duplicate is not None:
            duplicate.duplicate_urls.append(item.canonical_url)
            duplicate.engines = list(dict.fromkeys([*duplicate.engines, *item.engines]))
            outcome.duplicate_count += 1
            continue
        if item.relevance_score < policy.min_relevance_score:
            outcome.filtered_count += 1
            reason_counts["LOW_RELEVANCE_SCORE"] += 1
            continue
        unique.append(item)

    domain_counts: Counter[str] = Counter()
    for item in unique:
        domain = source_diversity_key(item.canonical_url)
        if domain_counts[domain] >= policy.max_per_domain:
            outcome.diversity_dropped += 1
            continue
        domain_counts[domain] += 1
        outcome.accepted.append(item)
        if len(outcome.accepted) >= max_results:
            break
    outcome.filter_reasons = dict(reason_counts)
    if outcome.snippet_fallback:
        unique_fallback: dict[str, RankedHit] = {}
        for item in sorted(
            outcome.snippet_fallback,
            key=lambda item: (-item.score, item.canonical_url),
        ):
            unique_fallback.setdefault(item.canonical_url, item)
        outcome.snippet_fallback = list(unique_fallback.values())[:3]
    return outcome


def _snippet_fallback_eligible(query: str, hit: SearchHit, canonical: str) -> bool:
    """Return whether a rejected hit still has a useful, bounded search clue."""
    parsed = urlsplit(canonical)
    if parsed.scheme not in {"http", "https"} or not source_domain(canonical):
        return False
    if not hit.title.strip() and not hit.snippet.strip():
        return False
    combined = f"{hit.title} {hit.snippet}".casefold()
    if any(marker in combined for marker in _AD_MARKERS):
        return False
    if source_domain(canonical) in _SEARCH_HOSTS and parsed.path.casefold() in {
        "/search",
        "/s",
        "/web",
    }:
        return False
    query_tokens = tokenize(query)
    candidate_tokens = tokenize(f"{hit.title} {hit.snippet} {canonical}")
    return bool(query_tokens & candidate_tokens)


def rank_results(query: str, hits: list[SearchHit], *, max_results: int) -> list[RankedHit]:
    return rank_candidates(query, hits, max_results=max_results).accepted


def are_near_duplicate(
    first_title: str,
    first_snippet: str,
    second_title: str,
    second_snippet: str,
    *,
    threshold: float = 0.86,
) -> bool:
    first_title_normalized = _normalize_text(first_title)
    second_title_normalized = _normalize_text(second_title)
    if min(len(first_title_normalized), len(second_title_normalized)) < 8:
        return False
    title_similarity = SequenceMatcher(
        None, first_title_normalized, second_title_normalized
    ).ratio()
    if title_similarity < threshold:
        return False
    first_snippet_tokens = tokenize(first_snippet)
    second_snippet_tokens = tokenize(second_snippet)
    if min(len(first_snippet_tokens), len(second_snippet_tokens)) < 4:
        return False
    overlap = len(first_snippet_tokens & second_snippet_tokens) / max(
        len(first_snippet_tokens | second_snippet_tokens), 1
    )
    return overlap >= 0.62


def content_fingerprint(value: str) -> str:
    normalized = _normalize_text(value)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest() if normalized else ""


def source_cluster_id(canonical_url: str) -> str:
    """Return a stable pre-content source id.

    The orchestrator rewires this id when page content proves that two URLs
    are mirrors or reprints, so callers can distinguish URL diversity from
    independent evidence.
    """

    canonical = canonicalize_url(canonical_url)
    digest = hashlib.sha1(canonical.encode("utf-8")).hexdigest()[:16]
    return f"src_{digest}"


def content_simhash(value: str) -> int:
    tokens = tokenize(value)
    if not tokens:
        return 0
    weights = [0] * 64
    for token in tokens:
        hashed = int.from_bytes(hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest())
        for bit in range(64):
            weights[bit] += 1 if hashed & (1 << bit) else -1
    result = 0
    for bit, weight in enumerate(weights):
        if weight >= 0:
            result |= 1 << bit
    return result


def content_is_near_duplicate(first: str, second: str) -> bool:
    if min(len(first.strip()), len(second.strip())) < 300:
        return False
    if content_fingerprint(first) == content_fingerprint(second):
        return True
    distance = (content_simhash(first) ^ content_simhash(second)).bit_count()
    return distance <= 4


def _reject_reason(
    query: str,
    hit: SearchHit,
    canonical: str,
    purpose: str,
    *,
    allow_search_redirects: bool = False,
    allow_partial_overlap: bool = False,
    allow_source_intent_mismatch: bool = False,
    allow_exploratory_entity_mismatch: bool = False,
    enforce_exact_terms: bool = False,
    constraints: ResearchQueryConstraints | None = None,
    query_role: str = "DISCOVERY",
) -> str | None:
    parsed = urlsplit(canonical)
    host = source_domain(canonical)
    if parsed.scheme not in {"http", "https"} or not host:
        return "INVALID_URL"
    if not hit.title.strip() and not hit.snippet.strip():
        return "EMPTY_RESULT"
    title = _normalize_text(hit.title)
    if title in _LOW_INFORMATION_TITLES and len(hit.snippet.strip()) < 40:
        return "LOW_INFORMATION"
    combined = f"{hit.title} {hit.snippet}".casefold()
    if any(marker in combined for marker in _AD_MARKERS):
        return "ADVERTISEMENT"
    if any(host == item or host.endswith(f".{item}") for item in _SEARCH_HOSTS) and (
        parsed.path.casefold() in {"/search", "/s", "/web"}
    ):
        return "SEARCH_RESULT_PAGE"
    if host == "sogou.com" and parsed.path.casefold() == "/link" and not allow_search_redirects:
        return "UNRESOLVED_SEARCH_REDIRECT"
    normalized_candidate = _normalize_text(f"{hit.title} {hit.snippet} {canonical}")
    constraints = constraints or extract_query_constraints(query)
    query_tokens = tokenize(query)
    candidate_tokens = tokenize(f"{hit.title} {hit.snippet} {canonical}")
    supporting_overlap = query_tokens & candidate_tokens
    exploratory = (
        allow_exploratory_entity_mismatch
        and query_role in {"COMMUNITY", "DISCOVERY"}
        and len(supporting_overlap) >= 2
    )
    project_entities = list(
        dict.fromkeys(
            [
                *([project_name(query)] if project_name(query) else []),
                *constraints.entities,
            ]
        )
    )
    project_entities = [
        entity
        for entity in project_entities
        if re.search(r"[-_]", entity) or re.search(r"\d", entity)
    ]
    if project_entities and not any(
        _normalize_text(entity) in normalized_candidate for entity in project_entities
    ):
        if not exploratory:
            return "ENTITY_MISMATCH"
    missing_strict = [
        term
        for term in [*constraints.strict_terms, *constraints.literal_terms]
        if _normalize_text(term) not in normalized_candidate
    ]
    if any(
        _normalize_text(term) not in normalized_candidate
        for term in constraints.literal_terms
    ) or (enforce_exact_terms and any(re.search(r"\d", term) for term in missing_strict)):
        return "CONSTRAINT_MISMATCH"
    exact_project_hit = (
        classify_source(canonical) in {"REPOSITORY", "DOCUMENTATION"}
        and any(
            _normalize_text(entity) in normalized_candidate for entity in project_entities
        )
    )
    # Platform/runtime terms are often absent from a repository title or search
    # snippet even though the README supports that platform. Let an exact
    # project/documentation hit through and verify those terms after reading;
    # retain strict filtering for concrete versions, prices, and other facts.
    deferred_platform_terms = {
        "windows",
        "linux",
        "macos",
        "wsl",
        "wsl2",
        "playwright",
        "chromium",
        "chrome",
        "edge",
    }
    hard_missing = [
        term
        for term in missing_strict
        if _normalize_text(term) not in deferred_platform_terms
    ]
    recruitment_discovery = (
        query_role in {"DISCOVERY", "COMMUNITY"}
        and purpose in {"CURRENT", "GENERAL", "RISK", "COMPARISON"}
        and len(supporting_overlap) >= 2
        and any(marker in query for marker in ("招聘", "岗位", "职位", "任职", "技能要求"))
    )
    if hard_missing and not exploratory and not recruitment_discovery and not (
        exact_project_hit and not hard_missing
    ):
        return "CONSTRAINT_MISMATCH"
    if query_role in {"AUTHORITATIVE", "VERIFY"} and constraints.entities:
        entity_match = any(
            _normalize_text(entity) in normalized_candidate
            for entity in constraints.entities
        )
        if not entity_match:
            return "ENTITY_MISMATCH"
    matched = query_tokens & candidate_tokens
    if query_tokens and not matched:
        return "NO_TERM_OVERLAP"
    if len(query_tokens) >= 3 and len(matched) == 1 and not allow_partial_overlap:
        ascii_query_tokens = {
            token
            for token in _ASCII_TOKEN_PATTERN.findall(query.casefold())
            if token not in _STOP_WORDS
        }
        category = classify_source(canonical)
        purpose_source_match = (
            purpose == "DOCUMENTATION" and category in {"DOCUMENTATION", "REPOSITORY"}
        ) or (purpose == "ACADEMIC" and category == "ACADEMIC")
        if not (purpose_source_match and matched <= ascii_query_tokens):
            return "INSUFFICIENT_TERM_OVERLAP"
    if purpose == "DOCUMENTATION":
        category = classify_source(canonical)
        documentation_terms = {"api", "config", "configuration", "docs", "documentation", "install"}
        if (
            not allow_source_intent_mismatch
            and category not in {"DOCUMENTATION", "GOVERNMENT", "REPOSITORY"}
            and not (
            candidate_tokens & documentation_terms
            )
        ):
            return "SOURCE_INTENT_MISMATCH"
    return None


def _score(
    query: str,
    hit: SearchHit,
    canonical: str,
    purpose: str,
    *,
    constraints: ResearchQueryConstraints | None = None,
) -> RankedHit:
    query_tokens = tokenize(query)
    title_tokens = tokenize(hit.title)
    snippet_tokens = tokenize(hit.snippet)
    url_tokens = tokenize(canonical)
    denominator = max(len(query_tokens), 1)
    title_coverage = len(query_tokens & title_tokens) / denominator
    snippet_coverage = len(query_tokens & snippet_tokens) / denominator
    url_coverage = len(query_tokens & url_tokens) / denominator
    relevance = min(title_coverage * 60 + snippet_coverage * 30 + url_coverage * 10, 100)
    project_entities = list(
        dict.fromkeys(
            [
                *([project_name(query)] if project_name(query) else []),
                *((constraints or ResearchQueryConstraints()).entities),
            ]
        )
    )
    project_entities = [
        entity
        for entity in project_entities
        if re.search(r"[-_]", entity) or re.search(r"\d", entity)
    ]
    entity = next(
        (
            candidate
            for candidate in project_entities
            if _normalize_text(candidate)
            in _normalize_text(f"{hit.title} {hit.snippet} {canonical}")
        ),
        None,
    )
    if entity and classify_source(canonical) in {"REPOSITORY", "DOCUMENTATION"}:
        normalized_entity = _normalize_text(entity)
        normalized_result = _normalize_text(f"{hit.title} {hit.snippet} {canonical}")
        if normalized_entity in normalized_result:
            relevance = max(relevance, 30.0)
    ascii_query_tokens = {
        token
        for token in _ASCII_TOKEN_PATTERN.findall(query.casefold())
        if token not in _STOP_WORDS
    }
    if ascii_query_tokens:
        entity_coverage = len(
            ascii_query_tokens & (title_tokens | snippet_tokens | url_tokens)
        ) / len(ascii_query_tokens)
        if entity_coverage == 1:
            relevance = max(relevance, 45.0)
    normalized_query = _normalize_text(query)
    if normalized_query and normalized_query in _normalize_text(f"{hit.title} {hit.snippet}"):
        relevance = min(relevance + 12, 100)

    category = classify_source(canonical)
    quality = {
        "GOVERNMENT": 18.0,
        "ACADEMIC": 16.0,
        "DOCUMENTATION": 13.0,
        "REPOSITORY": 11.0,
        "NEWS": 7.0,
        "COMMUNITY": 3.0,
        "JOB_POSTING": 10.0,
        "OTHER": 5.0,
    }[category]
    if category == "COMMUNITY" and any(
        cue in normalized_query for cue in _COMMUNITY_INTENT_CUES
    ):
        # Community is intentionally a soft source: semantic cues should make
        # first-party user experience outrank unrelated generic pages without
        # requiring a brittle site: restriction.
        quality += 8.0
    if purpose == "DOCUMENTATION" and category in {"DOCUMENTATION", "REPOSITORY"}:
        quality += 5
    if purpose == "ACADEMIC" and category == "ACADEMIC":
        quality += 5
    if purpose == "CURRENT" and category == "NEWS":
        quality += 4
    if purpose in {"CURRENT", "COMPARISON", "GENERAL"} and category == "JOB_POSTING":
        quality += 4
    exact_repository = False
    if project_entities and category == "REPOSITORY":
        parts = [part for part in urlsplit(canonical).path.split("/") if part]
        exact_repository = len(parts) >= 2 and any(
            parts[1].casefold().replace("_", "-")
            == candidate.casefold().replace("_", "-")
            for candidate in project_entities
        )
        if exact_repository:
            quality += 15

    engines = list(dict.fromkeys([*hit.engines, *([hit.engine] if hit.engine else [])]))
    freshness = _freshness_score(hit.published_at)
    engine_score = min(len(engines), 3) * 3
    total = round(relevance + quality + freshness + engine_score, 3)
    reasons = [f"CATEGORY_{category}"]
    if category == "COMMUNITY" and any(
        cue in normalized_query for cue in _COMMUNITY_INTENT_CUES
    ):
        reasons.append("COMMUNITY_INTENT_MATCH")
    if exact_repository:
        reasons.append("EXACT_PROJECT_REPOSITORY")
    if len(engines) > 1:
        reasons.append("MULTI_ENGINE")
    if freshness > 0:
        reasons.append("RECENT_RESULT")
    return RankedHit(
        hit=hit,
        canonical_url=canonical,
        engines=engines,
        matched_queries=[query],
        source_category=category,
        relevance_score=round(relevance, 3),
        quality_score=round(quality, 3),
        freshness_score=freshness,
        score=total,
        reasons=reasons,
    )


def _merge_exact(current: RankedHit, hit: SearchHit) -> None:
    engines = [*hit.engines, *([hit.engine] if hit.engine else [])]
    current.engines = list(dict.fromkeys([*current.engines, *engines]))
    if len(hit.snippet.strip()) > len(current.hit.snippet.strip()):
        current.hit.snippet = hit.snippet
    if len(hit.title.strip()) > len(current.hit.title.strip()):
        current.hit.title = hit.title


def _freshness_score(value: str | None) -> float:
    if not value:
        return 0.0
    try:
        published = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if published.tzinfo is None:
            published = published.replace(tzinfo=UTC)
    except ValueError:
        return 0.0
    age_days = max((datetime.now(UTC) - published).days, 0)
    if age_days <= 30:
        return 10.0
    if age_days <= 180:
        return 7.0
    if age_days <= 365:
        return 3.0
    if age_days > 1095:
        return -4.0
    return 0.0


def _normalize_text(value: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9\u4e00-\u9fff]+", " ", value.casefold()).split())
