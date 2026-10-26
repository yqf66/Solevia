"""Bounded, fail-open local-model comparison; never controls production results."""
from __future__ import annotations

import hashlib
import json
import logging
import math
import threading
from concurrent.futures import ThreadPoolExecutor
from urllib.request import ProxyHandler, Request, build_opener

from planner_api.config import Settings

logger = logging.getLogger(__name__)
_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="local-model-shadow")
_slots = threading.BoundedSemaphore(2)


def _local_post(endpoint: str, data: dict, timeout: float) -> dict:
    request = Request(
        endpoint,
        data=json.dumps(data, ensure_ascii=False).encode("utf8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    # Explicitly bypass environment/system proxies for local private inputs.
    with build_opener(ProxyHandler({})).open(request, timeout=timeout) as response:
        result = json.loads(response.read(128000).decode("utf8"))
    if not isinstance(result, dict):
        raise ValueError("local model returned a non-object response")
    return result


def _submit(settings: Settings, kind: str, data: dict, validate, baseline: dict, *, group_id: str | None = None) -> bool:
    from planner_api.local_model_dataset import record_primary, record_shadow

    sample_id = record_primary(settings, kind=kind, role=baseline.get("scope", kind), data=data, baseline=baseline, group_id=group_id)
    if settings.local_model_mode != "SHADOW" or not _slots.acquire(blocking=False):
        return False
    fingerprint = hashlib.sha256(json.dumps(data, ensure_ascii=False, sort_keys=True).encode()).hexdigest()

    def compare():
        try:
            response = _local_post(
                settings.local_model_base_url + "/" + kind,
                data,
                settings.local_model_timeout_seconds,
            )
            observed = validate(response)
            if sample_id:
                record_shadow(settings, sample_id=sample_id, kind=kind, role=kind,
                              observed=observed, identity=response.get("identity"))
            # Log typed choices / ordinal positions only. No request, passage,
            # material, or conversation text is written to the log.
            logger.info("local_model_shadow %s", json.dumps({
                "kind": kind, "fingerprint": fingerprint, "identity": response.get("identity"),
                "baseline": baseline, "observed": observed, "production_changed": False,
            }, ensure_ascii=False))
        except Exception as error:
            logger.info("local_model_shadow unavailable kind=%s error=%s production_changed=false", kind, type(error).__name__)
        finally:
            _slots.release()

    try:
        _executor.submit(compare)
    except Exception:
        _slots.release()
        return False
    return True


def shadow_research_decision(settings: Settings, question: str, baseline: dict, *, group_id: str | None = None) -> bool:
    if len(question) > 1500:
        return False  # No silent truncation of complex/long decision state.

    def validate(response):
        answers = response.get("answers")
        if not isinstance(answers, dict):
            raise ValueError("missing local decision answers")
        choices = {}
        confidences = {}
        for head, allowed in {
            "search_gate": {"search", "no_search"},
            "source_strategy": {"official", "academic", "community", "balanced"},
        }.items():
            answer = answers.get(head)
            if not isinstance(answer, dict) or answer.get("choice") not in allowed:
                raise ValueError("out-of-domain local choice")
            choices[head] = answer["choice"]
            confidence = answer.get("answer_confidence")
            if isinstance(confidence, (int, float)) and not isinstance(confidence, bool) and math.isfinite(confidence) and 0 <= confidence <= 1:
                confidences[head] = float(confidence)
        if confidences:
            choices["_answer_confidence"] = confidences
        return choices

    return _submit(settings, "decide", {
        "state": {"user_request": question},
        "heads": ["search_gate", "source_strategy"],
    }, validate, baseline, **({"group_id": group_id} if group_id else {}))


def shadow_material_rerank(
    settings: Settings, query: str, passages: list[str], *, scope: str = "MATERIAL", group_id: str | None = None
) -> bool:
    if not 2 <= len(passages) <= 24 or not 1 <= len(query) <= 1000:
        return False
    if any(not 1 <= len(p) <= 2200 for p in passages) or sum(map(len, passages)) > 32000:
        return False

    def validate(response):
        scores = response.get("scores")
        if (not isinstance(scores, list) or len(scores) != len(passages)
                or any(isinstance(score, bool) or not isinstance(score, (int,float)) or not math.isfinite(score) for score in scores)):
            raise ValueError("invalid relevance scores")
        coverage = response.get("coverage")
        if "coverage-v2" in str(response.get("identity", "")):
            if (
                not isinstance(coverage, dict) or coverage.get("complete") is not True
                or not isinstance(coverage.get("windows_per_passage"), list)
                or len(coverage["windows_per_passage"]) != len(passages)
                or any(type(count) is not int or count < 1 for count in coverage["windows_per_passage"])
                or coverage.get("total_windows") != sum(coverage["windows_per_passage"])
            ):
                raise ValueError("invalid relevance token coverage")
        observed = {"ranking": sorted(range(len(scores)), key=lambda i: (-scores[i], i)), "scores": scores}
        if isinstance(coverage, dict):
            observed["coverage"] = coverage
        return observed

    return _submit(settings, "rerank", {"query": query, "passages": passages}, validate,
                   {"ranking": list(range(len(passages))), "scope": scope},
                   **({"group_id": group_id} if group_id else {}))
