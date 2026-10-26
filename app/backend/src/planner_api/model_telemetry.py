"""Metadata-only, fail-open observation of model nodes and transport attempts.

Observers never change payloads, responses, exceptions, retries or time budgets.
Provider-node acceptance is explicitly separate from domain/business validation.
"""

from __future__ import annotations

from collections import OrderedDict
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import wraps
import hashlib
import inspect
import json
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
import threading
import time
from urllib.error import HTTPError, URLError
import uuid

from pydantic import ValidationError

VERSION = "model-trace-v1"
_node: ContextVar[Node | None] = ContextVar("model_trace_node", default=None)
_trace: ContextVar[str | None] = ContextVar("model_trace_root", default=None)
_sink_lock = threading.RLock()
_sinks: OrderedDict = OrderedDict()


@dataclass(frozen=True)
class TracePolicy:
    enabled: bool
    directory: str
    max_bytes: int
    backups: int
    provider: str


def trace_policy(settings) -> TracePolicy:
    return TracePolicy(
        settings.model_telemetry_enabled,
        settings.model_telemetry_dir,
        settings.model_telemetry_max_bytes,
        settings.model_telemetry_backups,
        settings.llm_provider,
    )


class _QuietHandler(RotatingFileHandler):
    def handleError(self, record):  # telemetry must never break a business request
        pass


def _write(policy: TracePolicy, event: dict) -> None:
    """Bounded local JSONL, at most four open destinations, no raw text."""
    if not policy.enabled:
        return
    try:
        line = json.dumps(event, ensure_ascii=True, allow_nan=False, separators=(",", ":"))
        key = (policy.directory, policy.max_bytes, policy.backups)
        with _sink_lock:
            handler = _sinks.get(key)
            if handler is None:
                folder = Path(policy.directory)
                folder.mkdir(parents=True, exist_ok=True)
                handler = _QuietHandler(
                    folder / "model-events.jsonl",
                    maxBytes=policy.max_bytes,
                    backupCount=policy.backups,
                    encoding="utf8",
                    delay=True,
                )
                handler.setFormatter(logging.Formatter("%(message)s"))
                _sinks[key] = handler
                while len(_sinks) > 4:
                    _sinks.popitem(last=False)[1].close()
            _sinks.move_to_end(key)
            handler.handle(logging.LogRecord("model_trace", logging.INFO, "", 0, line, (), None))
    except Exception:
        pass


def _emit(policy, **fields):
    try:
        _write(
            policy,
            {
                "version": VERSION,
                "timestamp": datetime.now(UTC).isoformat(),
                "trace_id": _trace.get(),
                **fields,
            },
        )
    except Exception:
        pass


@contextmanager
def trace_scope():
    """Request correlation; IDs contain no goal IDs, paths or user identifiers."""
    token = _trace.set(_trace.get() or uuid.uuid4().hex)
    try:
        yield
    finally:
        _trace.reset(token)


def submit_model_task(executor, function, *args, **kwargs):
    """Carry only telemetry correlation into existing worker threads."""
    root = _trace.get() or uuid.uuid4().hex

    def run():
        token = _trace.set(root)
        try:
            return function(*args, **kwargs)
        finally:
            _trace.reset(token)

    return executor.submit(run)


def _hash(value):
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode()
    ).hexdigest()


def _number(value):
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _usage(response):
    data = response.get("usage", {})
    if not isinstance(data, dict):
        data = {}
    output = data.get("output_tokens_details") or data.get("completion_tokens_details") or {}
    inputs = data.get("input_tokens_details") or data.get("prompt_tokens_details") or {}
    output = output if isinstance(output, dict) else {}
    inputs = inputs if isinstance(inputs, dict) else {}
    return {
        "input_tokens": _number(data.get("input_tokens", data.get("prompt_tokens"))),
        "output_tokens": _number(data.get("output_tokens", data.get("completion_tokens"))),
        "reasoning_tokens": _number(output.get("reasoning_tokens")),
        "supplier_cached_input_tokens": _number(inputs.get("cached_tokens")),
    }


def _completion(response):
    choices = response.get("choices", [])
    finishes = (
        {item.get("finish_reason") for item in choices if isinstance(item, dict)}
        if isinstance(choices, list)
        else set()
    )
    details = response.get("incomplete_details")
    reason = details.get("reason") if isinstance(details, dict) else None
    if finishes & {"length", "max_tokens"} or reason == "max_output_tokens":
        return "OUTPUT_TRUNCATED"
    if "content_filter" in finishes:
        return "OUTPUT_FILTERED"
    if response.get("status") in {"incomplete", "failed", "cancelled", "queued", "in_progress"}:
        return "OUTPUT_INCOMPLETE"
    return "RECEIVED"


def failure_category(error):
    """Only fixed categories/codes leave this function, never error messages."""
    if isinstance(error, HTTPError):
        return {401: "AUTHENTICATION", 402: "QUOTA", 403: "ACCESS_DENIED", 429: "RATE_LIMIT"}.get(
            error.code, "HTTP_ERROR"
        )
    if isinstance(error, TimeoutError) or (
        isinstance(error, URLError) and isinstance(error.reason, TimeoutError)
    ):
        return "TIMEOUT"
    if isinstance(error, (URLError, OSError)):
        return "CONNECTION"
    if isinstance(error, (KeyboardInterrupt, SystemExit)):
        return "INTERRUPTED"
    if isinstance(error, ValidationError):
        return "SCHEMA_INVALID"
    # Match fixed internal signals only. Arbitrary text is neither emitted nor used as a label.
    if isinstance(error, ValueError):
        if "truncat" in type(error).__name__.lower() or str(error) in {
            "MATERIAL_OUTPUT_TRUNCATED",
            "SPECIALIST_OUTPUT_TRUNCATED",
        }:
            return "OUTPUT_TRUNCATED"
        return "OUTPUT_INVALID"
    code = getattr(error, "code", None)
    return {
        "MODEL_TIMEOUT": "TIMEOUT",
        "INVALID_MODEL_RESPONSE": "OUTPUT_INVALID",
        "SPECIALIST_BATCH_UNSPLIT": "COVERAGE_INCOMPLETE",
        "SPECIALIST_RUN_SUPERSEDED": "SUPERSEDED",
    }.get(code, "NODE_ERROR")


@dataclass
class Node:
    policy: TracePolicy
    name: str
    node_id: str
    parent_id: str | None
    operation: str | None
    attempts: list[dict] = field(default_factory=list)
    pending: dict | None = None

    def flush(self):
        if self.pending is not None:
            _emit(
                self.policy,
                event="attempt",
                node=self.name,
                node_id=self.node_id,
                parent_node_id=self.parent_id,
                operation=self.operation,
                **self.pending,
            )
            self.attempts.append(self.pending)
            self.pending = None


def mark_attempt_failure(error):
    """Call before a provider catches a parse/validation error and retries."""
    try:
        node = _node.get()
        if node is not None and node.pending is not None:
            pending = node.pending
            if pending.get("failure_category") is None:
                pending["failure_category"] = failure_category(error)
            pending["status"] = "FAILED"
    except Exception:
        pass


def observed_model_call(transport, url, headers, payload, timeout):
    """Same argument objects and same result/exception objects as the original call."""
    node = _node.get()
    if node is None:
        return transport(url, headers, payload, timeout)
    try:
        node.flush()
        builtin = getattr(transport, "__module__", "") == "planner_api.ai" and getattr(
            transport, "__name__", ""
        ) in {"_urllib_json_transport", "proxied_transport"}
        codex = getattr(transport, "__module__", "") in {
            "planner_api.codex_runtime.business", "planner_api.codex_runtime.research"}
        actual_timeout = timeout
        actual_effort = payload.get("reasoning_effort") or (
            (payload.get("reasoning") or {}).get("effort") if isinstance(payload.get("reasoning"), dict) else None
        )
        if codex and (budget := getattr(transport, "codex_timeout_seconds", None)):
            actual_timeout = min(timeout, budget) if payload.get("solevia_budget_remaining") else budget
            actual_effort = getattr(transport, "codex_reasoning_effort", actual_effort)
        node.pending = {
            "attempt": len(node.attempts) + 1,
            "status": "STARTED",
            "failure_category": None,
            "transport_kind": "CODEX_APP_SERVER" if codex else "BUILTIN_HTTP" if builtin else "INJECTED",
            "request_hash": _hash(payload),
            "model_hash": _hash(payload.get("model")),
            "timeout_seconds": actual_timeout,
            "max_output_tokens": _number(
                payload.get("max_output_tokens", payload.get("max_tokens"))
            ),
            "reasoning_effort": actual_effort,
        }
        # Only known policy labels; never emit an arbitrary field from the payload.
        if node.pending["reasoning_effort"] not in {
            None,
            "none",
            "minimal",
            "low",
            "medium",
            "high",
            "xhigh",
            "max",
        }:
            node.pending["reasoning_effort"] = "OTHER"
        _emit(
            node.policy,
            event="attempt_started",
            node=node.name,
            node_id=node.node_id,
            attempt=node.pending["attempt"],
            transport_kind=node.pending["transport_kind"],
        )
    except Exception:
        node.pending = None
    started = time.perf_counter()
    try:
        response = transport(url, headers, payload, timeout)
    except BaseException as error:
        mark_attempt_failure(error)
        raise
    else:
        try:
            if node.pending is not None:
                completion = _completion(response)
                node.pending.update(
                    usage=_usage(response), completion=completion, status="RECEIVED"
                )
                if completion != "RECEIVED":
                    node.pending["failure_category"] = completion
        except Exception:
            pass
        return response
    finally:
        if node.pending is not None:
            node.pending["transport_ms"] = round((time.perf_counter() - started) * 1000, 3)


def model_node(name, *, operation_arg=None):
    """Observe an existing node without changing its control flow or contracts."""

    def decorate(function):
        signature = inspect.signature(function)

        @wraps(function)
        def run(self, *args, **kwargs):
            policy = getattr(self, "_model_trace_policy", None)
            if policy is None or not policy.enabled:
                return function(self, *args, **kwargs)
            operation = None
            if operation_arg:
                try:
                    value = signature.bind(self, *args, **kwargs).arguments.get(operation_arg)
                    owner = value if isinstance(value, type) else getattr(value, "__self__", None)
                    if isinstance(owner, type) and owner.__module__.startswith("planner_api."):
                        operation = owner.__name__
                    elif isinstance(value, str) and value in {
                        "OUTLINE_AUTHOR",
                        "DETAILS_AUTHOR",
                        "HYBRID_OUTLINE_AUTHOR",
                        "HYBRID_DETAILS_AUTHOR",
                        "CALENDAR_SPECIALIST",
                        "HISTORY_GUARDIAN",
                        "PROGRESS_ANALYST",
                        "CONTEXT_CURATOR",
                        "CONSTRAINT_ANALYST",
                        "LOAD_BALANCER",
                    }:
                        operation = value
                    elif callable(value) and getattr(value, "__module__", "") == "planner_api.ai":
                        operation = (
                            "ConversationReply"
                            if value.__name__ == "_parse_conversation_text"
                            else None
                        )
                except Exception:
                    pass
            parent = _node.get()
            current = Node(
                policy, name, uuid.uuid4().hex, parent.node_id if parent else None, operation
            )
            with trace_scope():
                token = _node.set(current)
                started = time.perf_counter()
                _emit(
                    policy,
                    event="node_started",
                    node=name,
                    node_id=current.node_id,
                    parent_node_id=current.parent_id,
                    operation=operation,
                    provider=policy.provider,
                    lane="ADVISORY" if name.startswith("generation_orchestration.") else "MODEL",
                )
                status, failure = "SUCCEEDED", None
                try:
                    return function(self, *args, **kwargs)
                except BaseException as error:
                    status, failure = "FAILED", failure_category(error)
                    if failure == "NODE_ERROR" and current.pending:
                        failure = current.pending.get("failure_category") or failure
                    mark_attempt_failure(error)
                    raise
                finally:
                    try:
                        if current.pending and current.pending["status"] == "RECEIVED":
                            current.pending["status"] = (
                                "NODE_ACCEPTED" if status == "SUCCEEDED" else "FAILED"
                            )
                        current.flush()
                        usage = {
                            key: sum(a.get("usage", {}).get(key) or 0 for a in current.attempts)
                            for key in _usage({})
                        }
                        _emit(
                            policy,
                            event="node_finished",
                            node=name,
                            node_id=current.node_id,
                            parent_node_id=current.parent_id,
                            operation=operation,
                            status=status,
                            failure_category=failure,
                            validation_scope="PROVIDER_NODE_ONLY_NOT_BUSINESS_APPROVAL",
                            lane="ADVISORY"
                            if name.startswith("generation_orchestration.")
                            else "MODEL",
                            attempts=len(current.attempts),
                            retries=max(0, len(current.attempts) - 1),
                            elapsed_ms=round((time.perf_counter() - started) * 1000, 3),
                            transport_ms=round(
                                sum(a.get("transport_ms", 0) for a in current.attempts), 3
                            ),
                            reported_usage=usage,
                            usage_reported_attempts=sum(
                                any(v is not None for v in a.get("usage", {}).values())
                                for a in current.attempts
                            ),
                        )
                    except Exception:
                        pass
                    finally:
                        _node.reset(token)

        return run

    return decorate


def record_decision(settings, node, event, status):
    """Fixed labels at existing cache/fallback branches; no payload or exception text."""
    try:
        if event not in {"cache", "fallback", "validation"} or status not in {
            "HIT",
            "MISS",
            "COALESCED_HIT",
            "OFF",
            "FALLBACK",
            "UNCACHED_FALLBACK",
            "EMPTY_CANDIDATES",
            "PROGRAM_ONLY",
            "DEGRADED",
            "PASSED",
            "FAILED",
        }:
            return
        with trace_scope():
            current = _node.get()
            _emit(
                trace_policy(settings),
                event=event,
                node=node,
                status=status,
                node_id=current.node_id if current else None,
            )
    except Exception:
        pass


def business_check(name):
    """Observe existing domain guards; not an additional guard or approval step."""

    def decorate(function):
        @wraps(function)
        def run(*args, **kwargs):
            from planner_api.config import get_settings

            policy = trace_policy(get_settings())
            if not policy.enabled:
                return function(*args, **kwargs)
            started = time.perf_counter()
            status, category = "PASSED", None
            try:
                return function(*args, **kwargs)
            except BaseException as error:
                status, category = "FAILED", "BUSINESS_CONSTRAINT"
                detail = getattr(error, "detail", None)
                code = (
                    detail.get("code") if isinstance(detail, dict) else getattr(error, "code", None)
                )
                if isinstance(code, str):
                    category = next(
                        (
                            label
                            for word, label in [
                                ("CAPACITY", "CAPACITY_CONFLICT"),
                                ("MINUTES", "WORKLOAD_INVALID"),
                                ("DEPENDENCY", "DEPENDENCY_INVALID"),
                                ("TEMPORAL", "TEMPORAL_INVALID"),
                                ("COMMITMENT", "COMMITMENT_INVALID"),
                                ("RANGE", "SCOPE_INVALID"),
                            ]
                            if word in code
                        ),
                        category,
                    )
                raise
            finally:
                with trace_scope():
                    _emit(
                        policy,
                        event="validation",
                        node=name,
                        status=status,
                        failure_category=category,
                        elapsed_ms=round((time.perf_counter() - started) * 1000, 3),
                        validation_scope="DOMAIN_GUARD_ONLY_NOT_PUBLICATION",
                    )

        return run

    return decorate
