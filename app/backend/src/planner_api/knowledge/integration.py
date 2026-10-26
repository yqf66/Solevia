"""Bounded cache/decision handoff. Candidate recall is never proof of coverage."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, TimeoutError
from copy import deepcopy
import json
from time import perf_counter
import logging
import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from planner_api.models import KnowledgeChunk
from .search import cached_context
from .store import KnowledgeStore, digest

_recall_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="goal-recall")


def recall_context(bind, settings, goal_id, question, *, phase="CLARIFICATION", anchor=""):
    # Independent sessions: neither worker shares the caller's transaction.
    def evidence():
        try:
            with Session(bind) as session:
                return cached_context(session, settings, goal_id, question, phase=phase,
                                      anchor=anchor, track_access=False)
        except Exception as error:
            logging.getLogger(__name__).warning("Cache recall unavailable: %s", type(error).__name__)
            return None

    def memory():
        from planner_api.goal_memory import recall_memory
        with Session(bind) as session:
            return recall_memory(session, goal_id, question, settings=settings)

    started = perf_counter()
    deadline = started + 3
    jobs = {"cached_evidence": _recall_pool.submit(evidence), "long_term_memory": _recall_pool.submit(memory)}
    result = {"cached_evidence": {}, "long_term_memory": [], "recall_diagnostics": {}}
    for name, future in jobs.items():
        try:
            result[name] = future.result(timeout=max(0, deadline - perf_counter())) or result[name]
            result["recall_diagnostics"][name] = "COMPLETED"
        except TimeoutError:
            future.cancel()
            # A bounded optional recall timeout is UNKNOWN, not a true cache miss.
            result["recall_diagnostics"][name] = "TIMED_OUT_UNKNOWN"
        except Exception as error:
            result["recall_diagnostics"][name] = "UNAVAILABLE_UNKNOWN"
            logging.getLogger(__name__).warning("Optional recall unavailable: %s", type(error).__name__)
    result["recall_ms"] = round((perf_counter() - started) * 1000, 2)
    return result


def selected_cache(packet, context):
    """Accept only IDs present in this recall; model text never supplies quotes."""
    decision = packet.decision
    candidate = deepcopy(context.get("cached_evidence") or {})
    if decision is None or decision.cache_coverage in {"INSUFFICIENT", "NOT_ASSESSED"}:
        return {}
    allowed = set(decision.cached_evidence_ids)
    candidate["selected_evidence"] = [e for e in candidate.get("selected_evidence", [])
                                      if e.get("evidence_id") in allowed]
    inherited = context.get("previous_research") or {} if decision.topic_relation == "FOLLOW_UP" and decision.inherit_source_restrictions else {}
    constraints = " ".join([packet.question, *(decision.brief.constraints if decision.brief else []),
        *((inherited.get("brief") or {}).get("constraints", []))])
    if re.search(r"只(?:看|用|根据|参考)(?:官方|官网)|仅(?:限|用|看)(?:官方|官网)|official sources only", constraints, re.I):
        from planner_api.research.sources import source_url_matches_profiles
        candidate["selected_evidence"] = [e for e in candidate["selected_evidence"]
            if source_url_matches_profiles(e["source_url"], ["official_docs", "government"])]
    if not candidate["selected_evidence"]:
        return {}
    candidate["knowledge_cache"]["coverage_assessment"] = decision.cache_coverage
    candidate["knowledge_cache"]["missing_aspects"] = decision.cache_missing_aspects
    return candidate


def merge_context(fresh, cached, *, max_chars=12000):
    """Keep old/new provenance and whole quotes. Never upgrade fresh run status."""
    if not cached or not cached.get("selected_evidence"):
        return fresh
    result = deepcopy(fresh or {})
    if not result.get("extracted_claims"):
        result["evidence_mode"] = "SPANS"
    result.setdefault("evidence_mode", "SPANS")
    result.setdefault("research_status", "PARTIAL")
    result["knowledge_cache"] = deepcopy(cached["knowledge_cache"])
    result["cached_source_handling_rule"] = cached["source_handling_rule"]
    target = "selected_evidence" if result.get("evidence_mode") == "SPANS" else "cached_selected_evidence"
    rows = result.setdefault(target, [])
    seen = {(r.get("source_url"), r.get("quote")) for r in rows}
    for row in cached["selected_evidence"]:
        if (row.get("source_url"), row.get("quote")) in seen:
            continue
        rows.append(row)
        if len(json.dumps(result, ensure_ascii=False, default=str)) > max_chars:
            rows.pop()
            result["knowledge_cache"]["omitted_whole_chunks"] = True
            continue
        seen.add((row.get("source_url"), row.get("quote")))
    returned = [r for r in rows if str(r.get("evidence_id", "")).startswith("kb:")]
    result["knowledge_cache"]["returned_evidence_ids"] = [r["evidence_id"] for r in returned]
    while len(json.dumps(result, ensure_ascii=False, default=str)) > max_chars and returned:
        rows.remove(returned.pop())
        result["knowledge_cache"]["returned_evidence_ids"] = [r["evidence_id"] for r in returned]
    if not returned:
        return fresh
    return result


def reauthorize_cache(session, settings, goal_id, cached, *, phase):
    if not cached or not settings.knowledge_enabled:
        return {}
    cached = deepcopy(cached)
    visible = {d.id: d for d, _ in KnowledgeStore(session, settings).visible(goal_id, phase=phase)}
    ids = [e.get("chunk_id") for e in cached.get("selected_evidence", [])]
    chunks = {c.id: c for c in session.scalars(select(KnowledgeChunk).where(KnowledgeChunk.id.in_(ids)))}
    cached["selected_evidence"] = [e for e in cached.get("selected_evidence", [])
        if (c := chunks.get(e.get("chunk_id"))) is not None and c.document_id in visible
        and c.document_id == e.get("document_id") and c.text == e.get("quote")
        and e.get("source_url") == visible[c.document_id].source_uri
        and c.content_hash == e.get("content_hash") == digest(c.text)]
    return cached if cached["selected_evidence"] else {}
