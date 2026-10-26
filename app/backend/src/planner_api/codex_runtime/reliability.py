"""Bounded, read-only generation repair; business mutations are never replayed."""
from __future__ import annotations

import copy
import time
from urllib.error import HTTPError, URLError

from planner_api.ai import ProviderUnavailableError, _safe_validation_feedback
from planner_api.model_telemetry import observed_model_call, mark_attempt_failure, model_node


def retryable(error: Exception) -> bool:
    if isinstance(error, HTTPError):
        return error.code in {408, 409, 429, 500, 502, 503, 504}
    # Retrying cannot repair authentication, exhausted quotas or denied permissions.
    message = str(error).lower()
    return not any(word in message for word in (
        "unauthorized", "authentication", "usage limit", "usagelimitexceeded", "quota", "permission denied",
        "badrequest", "sandboxerror", "forbidden", "invalid_json_schema", "invalid_request_error",
        "not supported", "invalid params", "budget_exhausted", "未登录", "额度", "尚未连接", "尚未启动",
    ))


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
    raise ProviderUnavailableError(
        "这次结果在有限修复后仍未通过检查，已保留你的输入和当前计划。请补充冲突信息或稍后重试。",
        diagnostic=_safe_validation_feedback(last_error) if last_error else "Codex generation time budget exhausted",
        code="CODEX_VALIDATION_EXHAUSTED" if isinstance(last_error, ValueError) else "CODEX_GENERATION_UNAVAILABLE",
    ) from None
