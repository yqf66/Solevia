"""Opt-in BGE permutation after evidence selection. Fail open, no recall authority."""
from __future__ import annotations

import math
import logging
import re
import threading
import time
from collections.abc import Callable
from dataclasses import asdict

from planner_api.config import Settings
from planner_api.context_batches import fingerprint
from planner_api.local_model_shadow import _local_post
from planner_api.model_telemetry import record_decision

from .analysis_ai import MaterialAnalysisPassage

IDENTITY = "bge-reranker-base@2cfc18c9415c912f9d8155881c133215df768a70:coverage-v2"
_slot = threading.BoundedSemaphore(1)
_warmup_lock = threading.Lock()
_last_warmup = 0.0
logger = logging.getLogger(__name__)
_sensitive = re.compile(r"矛盾|冲突|例外|除非|不允许|不得|禁止|不支持|仅当|"
                        r"\b(?:except|unless|conflict|must not|not supported|only if)\b", re.I)


def prewarm_bge(settings: Settings, *, transport: Callable = _local_post):
    """Warm the optional sidecar in the background, never delay an answer.

    The sidecar owns GPU checks and idle release. Only BGE is requested; Laya
    routing is unaffected. Repeated failures are throttled and fail open.
    """
    global _last_warmup
    if settings.material_rerank_mode != "GUARDED" or not _warmup_lock.acquire(False):
        return None
    if time.monotonic() - _last_warmup < 30:
        _warmup_lock.release()
        return None
    _last_warmup = time.monotonic()

    def warm():
        try:
            response = transport(settings.local_model_base_url + "/warmup", {"models": ["bge"]}, 60)
            logger.info("material_bge_warmup ready=%s", "bge" in response.get("ready", []))
        except Exception as error:
            logger.info("material_bge_warmup unavailable error_type=%s", type(error).__name__)
        finally:
            _warmup_lock.release()

    thread = threading.Thread(target=warm, name="material-bge-warmup", daemon=True)
    try:
        thread.start()
    except Exception:
        _warmup_lock.release()
        return None
    return thread


def guarded_order(
    query: str, passages: list[MaterialAnalysisPassage], settings: Settings, *,
    topic_anchors: set[str] | None = None, transport: Callable = _local_post,
    cache_scope: dict | None = None,
) -> tuple[list[MaterialAnalysisPassage], dict]:
    original = list(passages)
    info = {"policy": "same-set-pinned-evidence-v1", "mode": settings.material_rerank_mode,
            "status": "OFF", "candidate_count": len(original), "dropped_count": 0,
            "runtime_ms": 0.0}
    if settings.material_rerank_mode != "GUARDED":
        record_decision(settings, "materials.bge", "cache", "OFF")
        return original, info
    ids = [p.passage_id for p in original]
    if (not 2 <= len(original) <= 24 or len(ids) != len(set(ids))
            or not 1 <= len(query) <= 1000
            or any(not 1 <= len(p.text) <= 2200 for p in original)
            or sum(len(p.text) for p in original) > 32000):
        return original, {**info, "status": "SKIPPED_INPUT_BUDGET"}
    protected = set(topic_anchors or ())
    seen_sources = set()
    for passage in original:
        if passage.source_id not in seen_sources or _sensitive.search(passage.text):
            protected.add(passage.passage_id)
        seen_sources.add(passage.source_id)
    movable = [i for i, p in enumerate(original) if p.passage_id not in protected]
    if len(movable) < 2:
        return original, {**info, "status": "SKIPPED_NO_MOVABLE_PAIR",
                          "pinned_count": len(set(ids) & protected), "order_changed": False}
    if not _slot.acquire(blocking=False):
        return original, {**info, "status": "SKIPPED_BUSY"}
    started = time.perf_counter()
    try:
        from .retrieval_cache import bge_scores
        cache_key = fingerprint({"scope": cache_scope, "query": query,
            "passages": [asdict(p) for p in original], "anchors": sorted(protected),
            "identity": IDENTITY, "guard": info["policy"], "endpoint": settings.local_model_base_url})
        can_cache = bool(settings.material_query_cache_enabled and cache_scope and transport is _local_post)
        response = bge_scores.get(cache_key) if can_cache else None
        record_decision(settings, "materials.bge", "cache", "HIT" if response else "MISS" if can_cache else "OFF")
        hit = response is not None
        if response is None:
            response = transport(settings.local_model_base_url + "/rerank",
                             {"query": query, "passages": [p.text for p in original]},
                             settings.local_model_timeout_seconds)
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
        # Source/topic anchors and counterevidence were pinned before inference.
        ranked = sorted(movable, key=lambda i: (-scores[i], i))
        output = original.copy()
        for position, source_index in zip(movable, ranked, strict=True):
            output[position] = original[source_index]
        # Verify complete object content, not merely citation identities.
        before = {p.passage_id: fingerprint(asdict(p)) for p in original}
        after = {p.passage_id: fingerprint(asdict(p)) for p in output}
        if before != after:
            raise ValueError("EVIDENCE_SET_CHANGED")
        if can_cache and not hit:
            bge_scores.put(cache_key, response, settings.material_query_cache_ttl_seconds)
        return output, {**info, "status": "APPLIED", "identity": IDENTITY,
                        "score_cache": "HIT" if hit else "MISS" if can_cache else "OFF",
                        "runtime_ms": round((time.perf_counter() - started) * 1000, 3),
                        "coverage": coverage, "pinned_count": len(set(ids) & protected),
                        "order_changed": [p.passage_id for p in output] != ids,
                        "evidence_set_hash": fingerprint(before)}
    except Exception as error:
        record_decision(settings, "materials.bge", "fallback", "FALLBACK")
        # Local availability/quality errors never block an answer. No user text
        # or exception message is included in telemetry.
        # A released/cold or restarted runtime is warmed outside this request.
        # Use only the real production transport, never a unit-test replacement.
        if transport is _local_post:
            prewarm_bge(settings)
        return original, {**info, "status": "FALLBACK", "error_type": type(error).__name__,
                          "runtime_ms": round((time.perf_counter() - started) * 1000, 3)}
    finally:
        _slot.release()
