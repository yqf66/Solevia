"""Real web candidate reranking: semantic relevance plus program source guards.

Never changes search permission, source scope or the candidate evidence. Inputs
are complete search titles/snippets, not claims verified by this classifier.
"""
from __future__ import annotations

import math
import re
import threading
import time
from collections import Counter

from planner_api.local_model_shadow import _local_post

from .ranking import source_diversity_key

IDENTITY = "bge-reranker-base@2cfc18c9415c912f9d8155881c133215df768a70:coverage-v2"
_slot = threading.BoundedSemaphore(1)


def recall_constraints(constraints, user_request):
    """Semantic conditions remain on the full query for evidence verification.

    Search snippets are not required to use the same wording. Explicit literal
    terms, versions, numbers and user-specified Latin identifiers stay exact.
    """
    copied = constraints.model_copy(deep=True)
    folded = user_request.casefold()
    quoted = re.findall(r'["“「『](.+?)["”」』]', user_request)
    exact_requested = bool(re.search(r"逐字|字面|必须包含|精确匹配|原文|\b(?:literal|exact)\b", folded))
    # The planner's field is only a proposal: it cannot invent a literal
    # requirement that the user did not express.
    copied.literal_terms = [term for term in constraints.literal_terms
                            if term in quoted or (exact_requested and term.casefold() in folded)]
    copied.strict_terms = [term for term in constraints.strict_terms
        if re.search(r"\d", term) or term in quoted
        or (re.fullmatch(r"[A-Za-z][A-Za-z0-9_.:+/-]*", term)
            and re.search(r"(?<![a-z0-9])" + re.escape(term.casefold()) + r"(?![a-z0-9])", folded))]
    return copied


def rerank_candidates(query, candidates, settings, *, transport=_local_post):
    original = list(candidates)
    info = {"mode": settings.research_rerank_mode, "status": "OFF",
            "candidate_count": len(original), "order_changed": False, "runtime_ms": 0.0}
    if settings.research_rerank_mode != "BGE":
        return original, info
    if len(original) < 2:
        return original, {**info, "status": "SKIPPED_SINGLE"}
    texts = [(item.hit.title + "\n" + item.hit.snippet).strip() for item in original]
    if (not 1 <= len(query) <= 1000 or len(original) > 24
            or any(not 1 <= len(text) <= 2200 for text in texts)
            or sum(map(len, texts)) > 32000):
        return original, {**info, "status": "SKIPPED_INPUT_BUDGET"}
    if not _slot.acquire(blocking=False):
        return original, {**info, "status": "SKIPPED_BUSY"}
    began = time.perf_counter()
    try:
        response = transport(settings.local_model_base_url + "/rerank",
                             {"query": query, "passages": texts},
                             settings.research_rerank_timeout_seconds)
        scores, coverage = response.get("scores"), response.get("coverage")
        if response.get("identity") != IDENTITY:
            raise ValueError("MODEL_IDENTITY")
        if (not isinstance(scores, list) or len(scores) != len(original)
                or any(isinstance(s, bool) or not isinstance(s, (int, float))
                       or not math.isfinite(s) for s in scores)):
            raise ValueError("SCORES")
        if not isinstance(coverage, dict) or coverage.get("complete") is not True:
            raise ValueError("INCOMPLETE_COVERAGE")
        windows = coverage.get("windows_per_passage")
        if (not isinstance(windows, list) or len(windows) != len(original)
                or any(type(w) is not int or w < 1 for w in windows)
                or coverage.get("total_windows") != sum(windows)
                or coverage.get("version") != "bge-coverage-v2"):
            raise ValueError("WINDOW_COVERAGE")
        if len(set(scores)) == 1:
            return original, {**info, "status": "SKIPPED_EQUAL_SCORES", "relevance_scores": scores}
        semantic = sorted(range(len(scores)), key=lambda i: (-scores[i], i))
        semantic_position = {index: rank for rank, index in enumerate(semantic)}
        lexical = sorted(range(len(original)), key=lambda i: (-original[i].score, i))
        lexical_position = {index: rank for rank, index in enumerate(lexical)}
        # Ordinal scores avoid treating uncalibrated relevance logits as truth
        # probabilities. Provenance stays a separate, bounded program signal.
        denominator = max(1, len(original) - 1)
        fused = [65 * (1 - semantic_position[i] / denominator)
                 + 25 * (1 - lexical_position[i] / denominator)
                 + 10 * min(max(item.quality_score, 0), 40) / 40
                 for i, item in enumerate(original)]
        order = sorted(range(len(original)), key=lambda i: (-fused[i], i))
        for i, item in enumerate(original):
            item.score = round(fused[i], 3)
            item.reasons.append("BGE_HYBRID_RANK")
        return [original[i] for i in order], {**info, "status": "APPLIED",
            "relevance_scores": scores,
            "identity": IDENTITY, "coverage": coverage, "order": order,
            "order_changed": order != list(range(len(original))),
            "runtime_ms": round((time.perf_counter() - began) * 1000, 3)}
    except Exception as error:
        if transport is _local_post:
            # Warm only BGE outside the answer path after a cold/failed call.
            # A settings copy reuses the existing worker lifecycle without
            # enabling material reranking in the application configuration.
            from planner_api.materials.reranking import prewarm_bge

            prewarm_bge(settings.model_copy(update={"material_rerank_mode": "GUARDED"}))
        return original, {**info, "status": "FALLBACK", "error_type": type(error).__name__,
                          "runtime_ms": round((time.perf_counter() - began) * 1000, 3)}
    finally:
        _slot.release()


def limit_candidates(candidates, *, max_results, max_per_domain):
    """Apply bounded source diversity after semantic scoring, never before it."""
    selected, counts = [], Counter()
    for item in candidates:
        domain = source_diversity_key(item.canonical_url)
        if counts[domain] >= max_per_domain:
            continue
        counts[domain] += 1
        selected.append(item)
        if len(selected) >= max_results:
            break
    return selected
