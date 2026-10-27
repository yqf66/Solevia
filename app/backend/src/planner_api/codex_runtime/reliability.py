"""Bounded, read-only generation repair; business mutations are never replayed."""
from __future__ import annotations

import copy
import time
from urllib.error import URLError

from planner_api.ai import _safe_validation_feedback
from planner_api.model_telemetry import observed_model_call, mark_attempt_failure, model_node
from .failures import classify_failure, CodexFailure


def retryable(error: Exception) -> bool:
    if isinstance(error, ValueError):
        return 'budget_exhausted' not in str(error).lower()
    return classify_failure(error).retryable


@model_node('codex.validated_generation')
def validated_generation(provider, payload, validate, *, attempts=3):
    deadline = time.monotonic() + getattr(provider.transport, 'codex_timeout_seconds', provider.timeout)
    payload = copy.deepcopy(payload)
    payload['solevia_budget_remaining'] = True
    last_error = None
    previous_feedback = None
    for attempt in range(attempts):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            response = observed_model_call(provider.transport, provider.endpoint, {}, payload, remaining)
            return validate(response)
        except (URLError, TimeoutError, OSError, ValueError) as error:
            mark_attempt_failure(error)
            last_error = error
            if not retryable(error):
                break
            if isinstance(error, ValueError):
                # Only bounded field locations and validator messages, never the
                # complete rejected answer or arbitrary tool/exception contents.
                feedback = _safe_validation_feedback(error)[:2000]
                if feedback == previous_feedback:
                    break
                previous_feedback = feedback
                payload["instructions"] = str(payload.get("instructions", "")).split("\n[CODEX_REPAIR]")[0] + (
                    "\n[CODEX_REPAIR]\n上次结果被校验拦截，请输出修正后的完整结果。"
                    "保留所有已确认约束，不能删任务、放宽容量、移动截止日期或虚构依赖。"
                    f"校验反馈：{feedback}"
                )
            if attempt + 1 < attempts:
                provider.sleeper(min(0.25 * 2**attempt, max(0, deadline - time.monotonic())))
    if last_error is not None and not isinstance(last_error, ValueError):
        raise classify_failure(last_error).as_error() from None
    failure = CodexFailure('CODEX_VALIDATION_EXHAUSTED' if last_error else 'CODEX_TIMEOUT').as_error()
    if last_error:
        failure.diagnostic = _safe_validation_feedback(last_error)
    raise failure from None
