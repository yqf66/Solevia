"""Opt-in, append-only local dataset events for Laya/BGE review.

The collector deliberately stores metadata and typed outputs by default. Raw
questions/passages require an explicit setting because they may contain private
material. Events are append-only so a later label correction cannot erase the
original model decision.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4
from concurrent.futures import ThreadPoolExecutor

from planner_api.config import BACKEND_ROOT, Settings

logger = logging.getLogger(__name__)
_write_lock = threading.Lock()
_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="local-dataset-capture")
_slots = threading.BoundedSemaphore(64)
_file_counts: dict[str, tuple[int, int]] = {}

_EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")
_URL = re.compile(r"https?://[^\s]+", re.IGNORECASE)
_PHONE = re.compile(r"(?<!\d)(?:\+?\d[\d ()-]{7,}\d)(?!\d)")
_SECRET = re.compile(r"\b(?:sk|key|token|secret)[_-]?[A-Za-z0-9_-]{12,}\b", re.IGNORECASE)
_WINDOWS_PATH = re.compile(r"[A-Za-z]:\\[^\s]+")


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()


def _redact(value: str, limit: int) -> str:
    text = value[:limit]
    for pattern, replacement in (
        (_SECRET, "<REDACTED_SECRET>"),
        (_EMAIL, "<REDACTED_EMAIL>"),
        (_URL, "<REDACTED_URL>"),
        (_WINDOWS_PATH, "<REDACTED_PATH>"),
        (_PHONE, "<REDACTED_PHONE>"),
    ):
        text = pattern.sub(replacement, text)
    return text


def _dataset_dir(settings: Settings) -> Path:
    raw = Path(settings.local_model_dataset_dir)
    return raw if raw.is_absolute() else (BACKEND_ROOT / raw)


def enabled(settings: Settings) -> bool:
    return settings.local_model_dataset_mode == "CAPTURE"


def _bounded_state(data: dict[str, Any], settings: Settings) -> dict[str, Any]:
    """Keep a training row useful without copying unbounded material."""
    result: dict[str, Any] = {"fingerprint": _hash(data)}
    if "state" in data and isinstance(data["state"], dict):
        state = data["state"]
        result["state_hash"] = _hash(state)
        result["state_keys"] = sorted(str(key) for key in state)[:32]
        if settings.local_model_dataset_store_text:
            # Capture only the input contract used by the bounded Laya heads;
            # arbitrary nested context may include document bodies or secrets.
            question = state.get("user_request")
            if isinstance(question, str):
                result["state"] = {"user_request": _redact(question, 1500)}
                result["input_complete"] = len(question) <= 1500
    if "query" in data:
        query = data.get("query")
        result["query_hash"] = _hash(query)
        result["candidate_count"] = len(data.get("passages", [])) if isinstance(data.get("passages"), list) else 0
        if settings.local_model_dataset_store_text and isinstance(query, str):
            result["query"] = _redact(query, 1000)
            result["input_complete"] = len(query) <= 1000
    if isinstance(data.get("passages"), list):
        passages = [item for item in data["passages"] if isinstance(item, str)]
        result["candidate_hashes"] = [_hash(item) for item in passages[:24]]
        result["candidate_lengths"] = [len(item) for item in passages[:24]]
        if settings.local_model_dataset_store_text:
            result["passages"] = [_redact(item, 2200) for item in passages[:24]]
    return result


def append_event(
    settings: Settings,
    *,
    sample_id: str,
    kind: str,
    role: str,
    event_type: str,
    payload: dict[str, Any],
    label_status: str = "PENDING_REVIEW",
) -> bool:
    if not enabled(settings):
        return False
    directory = _dataset_dir(settings)
    path = directory / "events.jsonl"
    try:
        directory.mkdir(parents=True, exist_ok=True)
        with _write_lock:
            if path.exists():
                size = path.stat().st_size
                previous = _file_counts.get(str(path))
                if previous is None or previous[0] != size:
                    with path.open("r", encoding="utf-8") as existing:
                        count = sum(1 for _ in existing)
                else:
                    count = previous[1]
                if count >= settings.local_model_dataset_max_rows * 2:
                    logger.warning("local model dataset capture reached event limit")
                    return False
            else:
                count = 0
            event = {
                "schema_version": "plan-a-local-model-event/v1",
                "event_id": _hash({"sample_id": sample_id, "event_type": event_type, "time": _now()}),
                "sample_id": sample_id,
                "created_at": _now(),
                "kind": kind,
                "role": role,
                "event_type": event_type,
                "label_status": label_status,
                "payload": payload,
            }
            with path.open("a", encoding="utf-8") as stream:
                serialized = json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n"
                if (path.stat().st_size if path.exists() else 0) + len(serialized.encode("utf8")) > settings.local_model_dataset_max_bytes:
                    logger.warning("local model dataset capture reached byte limit")
                    return False
                stream.write(serialized)
            _file_counts[str(path)] = (path.stat().st_size, count + 1)
        return True
    except OSError as error:
        logger.warning("local model dataset capture unavailable: %s", type(error).__name__)
        return False


def _queue(callback) -> bool:
    if not _slots.acquire(blocking=False):
        logger.warning("local model dataset queue full; capture skipped")
        return False

    def task():
        try:
            callback()
        except Exception as error:
            logger.warning("local model dataset capture unavailable: %s", type(error).__name__)
        finally:
            _slots.release()

    try:
        _executor.submit(task)
        return True
    except RuntimeError:
        _slots.release()
        return False


def record_primary(settings: Settings, *, kind: str, role: str, data: dict[str, Any], baseline: dict[str, Any], group_id: str | None = None) -> str | None:
    if not enabled(settings):
        return None
    # Each occurrence is a distinct trajectory sample. Identical user requests
    # under different contexts must not collapse into one training row.
    sample_id = uuid4().hex
    payload = {
        "state": _bounded_state(data, settings),
        "baseline": baseline,
        "label_source": "primary_model_or_rule",
        "group_id": _hash(group_id or {"kind": kind, "input": data.get("state", data.get("query"))}),
        "group_source": "trajectory" if group_id else "input_fingerprint",
        "head_version": "heads-v1" if kind == "decide" else None,
    }
    queued = _queue(lambda: append_event(settings, sample_id=sample_id, kind=kind, role=role,
                                       event_type="PRIMARY", payload=payload))
    return sample_id if queued else None


def record_shadow(
    settings: Settings, *, sample_id: str, kind: str, role: str,
    observed: dict[str, Any], identity: str | None,
) -> None:
    _queue(lambda: append_event(settings, sample_id=sample_id, kind=kind, role=role, event_type="SHADOW",
                               payload={"observed": observed, "identity": identity}))
