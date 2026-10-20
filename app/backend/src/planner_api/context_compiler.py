from __future__ import annotations

import hashlib
import json
import logging
import re
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

CONTEXT_COMPILER_POLICY_VERSION = "context-v2-contract-preserving"

_IMPORTANT_LINE = re.compile(
    r"截止|必须|不能|不要|只做|不做|改为|调整|优先|约束|可用|每周|每天|日期|"
    r"完成|进行中|阻塞|风险|依赖|目标|成果|成功标准|deadline|must|constraint|blocked",
    re.IGNORECASE,
)
_WEEK_REFERENCE = re.compile(
    r"(?:第\s*)?(\d{1,3})\s*(?:周)?\s*(?:到|至|-|–|~|～)\s*(?:第\s*)?(\d{1,3})\s*周"
    r"|(?:第\s*)?(\d{1,3})\s*周"
    r"|\bW(\d{1,3})(?:\s*(?:to|-|–|~)\s*W?(\d{1,3}))?\b",
    re.IGNORECASE,
)
_SIGNAL_PATTERNS = {
    "constraints": re.compile(r"必须|不能|不要|只做|不做|约束|must|constraint", re.IGNORECASE),
    "changes": re.compile(r"改为|调整|替换|取消|增加|减少|不再|change|replace", re.IGNORECASE),
    "dates": re.compile(r"\d{4}[-/.年]\d{1,2}|截止|日期|周|月|deadline", re.IGNORECASE),
    "status": re.compile(r"已完成|进行中|未开始|阻塞|积压|暂停|completed|blocked", re.IGNORECASE),
    "dependencies": re.compile(r"依赖|前置|之后|先.+再|depends", re.IGNORECASE),
}


def estimate_units(value: object) -> int:
    """Return a deterministic, provider-independent input size estimate."""
    return max(1, len(json.dumps(value, ensure_ascii=False, default=str)) // 4)


_REFERENCE_NOTE = (
    "$context_ref 指向本次输入中已完整提供的相同快照；请读取该路径，事实与约束没有省略。"
)


def _reference_source_feedback(
    compiled: dict[str, Any], references: list[dict[str, str]],
) -> None:
    source = compiled.get("source_document")
    # For small drafts a pointer and its explanation can cost more than a copy.
    if not isinstance(source, str) or len(source) < 1024:
        return
    matches = []
    for prefix in ("planning_context", "existing_context"):
        context = compiled.get(prefix)
        feedback = context.get("generation_validation_feedback") if isinstance(context, dict) else None
        if isinstance(feedback, dict) and feedback.get("previous_source_document") == source:
            matches.append((prefix, feedback))
    if not matches:
        return

    # Keep the authoritative draft complete and before every pointer. Preserve
    # the stable requirements/route prefix so supplier prefix caching can reuse it.
    names = list(compiled)
    first_context = min((prefix for prefix, _ in matches), key=names.index)
    if names.index("source_document") > names.index(first_context):
        reordered = {}
        for key, value in compiled.items():
            if key == first_context:
                reordered["source_document"] = source
            if key != "source_document":
                reordered[key] = value
        compiled.clear()
        compiled.update(reordered)
    for prefix, feedback in matches:
        feedback["previous_source_document"] = {"$context_ref": "$.source_document"}
        references.append({
            "path": f"$.{prefix}.generation_validation_feedback.previous_source_document",
            "target": "$.source_document",
        })


def deduplicate_source_feedback(
    payload: dict[str, Any], *, enabled: bool = True,
) -> dict[str, Any]:
    """Only reference a byte-identical current draft in known retry feedback.

    No whitespace normalization, summaries, previous-call pointers or hashing
    substitutes. Different drafts and feedback without a current draft stay full.
    """
    compiled = deepcopy(payload)
    references: list[dict[str, str]] = []
    if enabled:
        _reference_source_feedback(compiled, references)
    if references:
        metadata = compiled.get("_context_references") or {}
        compiled["_context_references"] = {
            "note": _REFERENCE_NOTE,
            "references": [*metadata.get("references", []), *references],
        }
    return compiled


def deduplicate_context_snapshots(
    payload: dict[str, Any], *, include_source_feedback: bool = True,
) -> dict[str, Any]:
    """Reference identical named snapshots within one request, never across calls.

    This is lossless and only touches known snapshot fields. Tasks, execution
    records, partial progress and similar-looking user text are not deduplicated.
    A reference always points to an earlier complete value in the same request.
    """
    compiled = deduplicate_source_feedback(payload, enabled=include_source_feedback)
    keys = {
        "confirmed_requirements", "requirements", "requirement", "confirmed_route",
        "route", "current_plan", "plan", "plan_outline", "requirement_memory",
        "source_document",
    }
    seen: dict[str, str] = {}
    references: list[dict[str, str]] = list(
        (compiled.get("_context_references") or {}).get("references", [])
    )
    keys.add("snapshot")
    source_is_anchor = any(ref["target"] == "$.source_document" for ref in references)

    def visit(prefix: str, container: dict[str, Any]) -> None:
        for key, value in list(container.items()):
            path = f"{prefix}.{key}"
            if key in keys and value:
                encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
                if len(encoded) >= 256:
                    if encoded in seen and not (path == "$.source_document" and source_is_anchor):
                        container[key] = {"$context_ref": seen[encoded]}
                        references.append({"path": path, "target": seen[encoded]})
                        # A replaced parent has no accessible child paths.
                        continue
                    seen[encoded] = path
            if isinstance(value, dict) and (
                (prefix == "$" and key in {"planning_context", "existing_context"})
                or (prefix in {"$.planning_context", "$.existing_context"}
                    and key in {"requirement", "route", "plan"} and "snapshot" in value)
            ):
                visit(path, value)

    visit("$", compiled)
    if references:
        compiled["_context_references"] = {
            "note": _REFERENCE_NOTE,
            "references": references,
        }
    return compiled


def _chunks(text: str, size: int = 1800) -> list[str]:
    paragraphs = [item.strip() for item in re.split(r"\n\s*\n", text) if item.strip()]
    result: list[str] = []
    current = ""

    def append_piece(piece: str) -> None:
        nonlocal current
        separator = "\n" if current else ""
        if len(piece) > size:
            if current:
                result.append(current)
                current = ""
            result.extend(piece[index : index + size] for index in range(0, len(piece), size))
        elif len(current) + len(separator) + len(piece) <= size:
            current += separator + piece
        else:
            result.append(current)
            current = piece

    for paragraph in paragraphs or [text]:
        # Summary sources use one message per line. Respect those boundaries so
        # a critical sentence cannot be split across arbitrary character chunks.
        lines = [line.strip() for line in paragraph.splitlines() if line.strip()]
        for line in lines or [paragraph]:
            append_piece(line)
    if current:
        result.append(current)
    return result


def _important_excerpt(text: str, limit: int = 1400) -> str:
    """Keep evidence around signal matches instead of blindly taking line prefixes."""
    matches = list(_IMPORTANT_LINE.finditer(text))
    if not matches:
        return ""
    windows: list[tuple[int, int]] = []
    for match in matches[:12]:
        start = max(0, match.start() - 180)
        end = min(len(text), match.end() + 420)
        if windows and start <= windows[-1][1] + 40:
            windows[-1] = (windows[-1][0], max(windows[-1][1], end))
        else:
            windows.append((start, end))
    excerpts = [text[start:end].strip() for start, end in windows]
    return "\n...\n".join(excerpts)[:limit]


def compile_user_text(
    text: str,
    max_chars: int = 12000,
    *,
    include_header: bool = True,
) -> tuple[str, dict[str, object]]:
    """Compile oversized user text while keeping raw text in the durable message store."""
    normalized = text.strip()
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    detected_signals = {
        name: len(pattern.findall(normalized)) for name, pattern in _SIGNAL_PATTERNS.items()
    }
    if len(normalized) <= max_chars:
        return normalized, {
            "source_chars": len(normalized),
            "compiled_chars": len(normalized),
            "chunk_count": 1,
            "truncated": False,
            "source_hash": digest,
            "detected_signals": detected_signals,
        }

    chunks = _chunks(normalized)
    excerpts: list[tuple[int, str, int]] = []
    last_index = len(chunks) - 1
    for index, chunk in enumerate(chunks):
        important = _important_excerpt(chunk)
        if important:
            excerpt = important
            priority = 3
        else:
            excerpt = chunk if len(chunk) <= 700 else f"{chunk[:420]}\n...\n{chunk[-220:]}"
            priority = 1
        if index in {0, last_index}:
            priority += 2
        excerpts.append((priority, excerpt, index))

    selected: list[tuple[int, str]] = []
    used = 180 if include_header else 0
    for _, excerpt, index in sorted(excerpts, key=lambda item: (-item[0], item[2])):
        allowance = max_chars - used
        if allowance <= 120:
            break
        clipped = excerpt[: min(len(excerpt), allowance - 40)]
        selected.append((index, clipped))
        used += len(clipped) + 40
    selected.sort()
    body = "\n\n".join(
        f"[片段 {index + 1}/{len(chunks)}]\n{excerpt}" for index, excerpt in selected
    )
    header = (
        (
            f"[长输入编译视图：原文 {len(normalized)} 字符，共 {len(chunks)} 段；"
            "原文已持久化。以下优先保留首尾、约束、日期、状态和明确变更。]\n"
        )
        if include_header
        else ""
    )
    compiled = (header + body)[:max_chars]
    return compiled, {
        "source_chars": len(normalized),
        "compiled_chars": len(compiled),
        "chunk_count": len(chunks),
        "selected_chunks": [index for index, _ in selected],
        "truncated": True,
        "source_hash": digest,
        "detected_signals": detected_signals,
    }


@dataclass(frozen=True)
class CompiledContext:
    payload: dict[str, Any]
    manifest: dict[str, Any]


class ContextCompiler:
    """Keep the complete planning contract; the input budget is advisory.

    Unknown fields are authoritative by default. Without a typed coverage
    proof, a generic compiler cannot know whether a sentence or a middle
    list entry contains a unique user constraint. Only exact duplicates of
    evidence indexes are removed; tasks and chronological messages retain
    their identity and multiplicity. Narrative summaries use compile_user_text
    separately and must not be mistaken for the full planning contract.
    """

    _DEDUPLICATE_EXACT_LISTS = {
        "change_history", "recent_changes", "confirmed_execution_insights",
        "error_avoidance_memory", "fixed_occupancy_rules",
        "fixed_occupancy_overrides", "date_exceptions",
        "cached_holiday_calendars", "source_refs", "evidence_ids",
    }
    _REQUEST_KEYS = {
        "user_input", "user_request", "user_feedback", "adjustment_request",
        "latest_user_message", "original_goal", "raw_input",
    }

    def __init__(self, max_chars: int = 60000, max_user_text_chars: int = 12000) -> None:
        self.max_chars = max_chars
        self.max_user_text_chars = max_user_text_chars

    def compile(self, payload: dict[str, Any], *, task_type: str) -> CompiledContext:
        source = deepcopy(payload)
        # A second stage may compile an already compiled request. The previous
        # manifest describes metadata, not user evidence; avoid recursive
        # manifests without erasing its original coverage provenance.
        previous = source.pop("_context_manifest", None)
        omissions: list[dict[str, object]] = []
        compiled = self._compact(source, (), omissions)
        manifest: dict[str, Any] = {
            "compiler_version": CONTEXT_COMPILER_POLICY_VERSION,
            "task_type": task_type,
            "source_chars": self._size(source),
            "compiled_chars": self._size(compiled),
            "estimated_input_units": estimate_units(compiled),
            "max_chars": self.max_chars,
            "requested_weeks": sorted(self._requested_weeks(source)),
            "quality_policy": "complete-contract-soft-budget",
            "lossy": False,
            "budget_exceeded": False,
            "omissions": omissions[:40],
            "source_hash": hashlib.sha256(
                json.dumps(source, ensure_ascii=False, sort_keys=True, default=str).encode()
            ).hexdigest(),
        }
        if isinstance(previous, dict):
            manifest["upstream_source_hash"] = previous.get("source_hash")
            manifest["upstream_lossy"] = (
                previous.get("lossy", previous.get("compiler_version") == "context-v1")
            )
            manifest["lossy"] = bool(manifest["upstream_lossy"])
        compiled["_context_manifest"] = manifest
        # A contract larger than the target budget remains intact. This flag
        # is observable, not a claim that every provider can accept the input.
        # No deadline, capacity, task, evidence, or dependency is sacrificed.
        manifest["budget_exceeded"] = self._size(compiled) > self.max_chars
        # Account for the metadata itself without an unbounded fixed-point loop.
        for _ in range(3):
            manifest["compiled_chars"] = self._size(compiled)
        if manifest["budget_exceeded"]:
            logging.getLogger(__name__).warning(
                "context_soft_budget_exceeded task=%s chars=%s target=%s source_hash=%s contract_preserved=true",
                task_type, manifest["compiled_chars"], self.max_chars, manifest["source_hash"],
            )
        return CompiledContext(payload=compiled, manifest=manifest)

    @staticmethod
    def _size(value: object) -> int:
        return len(json.dumps(value, ensure_ascii=False, default=str))

    def _compact(
        self, value: Any, path: tuple[str, ...], omissions: list[dict[str, object]]
    ) -> Any:
        if isinstance(value, list):
            items = value
            if path and path[-1] in self._DEDUPLICATE_EXACT_LISTS:
                seen: set[str] = set()
                items = []
                for item in value:
                    identity = json.dumps(item, ensure_ascii=False, sort_keys=True, default=str)
                    if identity not in seen:
                        items.append(item)
                        seen.add(identity)
                if len(items) != len(value):
                    omissions.append({
                        "path": ".".join(path), "reason": "DUPLICATE_EXACT_ITEMS",
                        "omitted": len(value) - len(items), "lossless": True,
                    })
            return [self._compact(item, path + (str(i),), omissions)
                    for i, item in enumerate(items)]
        if isinstance(value, dict):
            return {str(key): self._compact(child, path + (str(key),), omissions)
                    for key, child in value.items()}
        return value

    @classmethod
    def _requested_weeks(cls, payload: dict[str, Any]) -> set[int]:
        result: set[int] = set()

        def visit(value: Any) -> None:
            if isinstance(value, dict):
                for key, child in value.items():
                    if key in cls._REQUEST_KEYS and isinstance(child, str):
                        for match in _WEEK_REFERENCE.finditer(child):
                            start = int(match.group(1) or match.group(3) or match.group(4))
                            end = int(match.group(2) or match.group(5) or start)
                            result.update(range(max(1, min(start, end)), max(start, end) + 1))
                    elif key == "requested_weeks" and isinstance(child, dict):
                        start, end = child.get("start"), child.get("end")
                        if (isinstance(start, int) and isinstance(end, int)
                                and 1 <= start <= end <= 999):
                            result.update(range(start, end + 1))
                    else:
                        visit(child)
            elif isinstance(value, list):
                for child in value:
                    visit(child)

        visit(payload)
        return result


def compile_context_payload(
    payload: dict[str, Any],
    *,
    task_type: str,
    max_chars: int | None = None,
    max_user_text_chars: int | None = None,
) -> dict[str, Any]:
    if max_chars is None or max_user_text_chars is None:
        from planner_api.config import get_settings

        settings = get_settings()
        max_chars = max_chars or settings.context_max_input_chars
        max_user_text_chars = max_user_text_chars or settings.context_max_user_text_chars
    return (
        ContextCompiler(max_chars, max_user_text_chars)
        .compile(payload, task_type=task_type)
        .payload
    )
