from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable
from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from planner_api.models import GoalErrorMemory, utc_now
from planner_api.schemas import ErrorAvoidanceMemoryCandidate


def _normalized(value: str) -> str:
    return re.sub(r"\s+", "", value).casefold()


_CORRECTION_MARKERS = (
    "不合理",
    "不对",
    "错误",
    "误解",
    "误记",
    "不是我说",
    "没有说过",
    "没说过",
    "从未说过",
    "不要再",
    "别再",
)
_EXPLICIT_DENIAL_PATTERN = re.compile(
    r"我(?:从未|没有|没|并未).{0,60}(?:说过|表达过|表示过)|"
    r"(?:这是|属于|你(?:把我)?).{0,12}(?:误记|误解|理解错|记错)|"
    r"不是我说|没有说过|没说过|从未说过"
)
_CORRECTION_DIRECTIVE_PATTERN = re.compile(
    r"不要再|别再|后续(?:不要|别)|以后(?:不要|别)|不能再|不要把|不要将"
)


def explicit_error_memory_candidates(user_content: str) -> list[ErrorAvoidanceMemoryCandidate]:
    """Recognize an explicit user correction without asking the model to infer one."""
    normalized = user_content.strip()
    if not normalized or not any(marker in normalized for marker in _CORRECTION_MARKERS):
        return []
    has_explicit_denial = bool(_EXPLICIT_DENIAL_PATTERN.search(normalized))
    has_directive = bool(_CORRECTION_DIRECTIVE_PATTERN.search(normalized))
    has_named_error = any(marker in normalized for marker in ("不合理", "不对", "错误"))
    if not has_explicit_denial and not (has_named_error and has_directive):
        return []
    sentences = [
        item.strip()
        for item in re.split(r"(?<=[。！？!?；;])", normalized)
        if item.strip()
    ]
    evidence = next(
        (item for item in sentences if any(marker in item for marker in _CORRECTION_MARKERS)),
        normalized,
    )[:500]
    return [
        ErrorAvoidanceMemoryCandidate(
            rule=f"后续生成必须遵守用户纠正：{evidence}"[:800],
            evidence_quote=evidence,
            scope="GOAL",
        )
    ]


def persist_explicit_error_memories(
    session: Session,
    *,
    goal_id: str,
    source_message_id: str,
    user_content: str,
    candidates: Iterable[ErrorAvoidanceMemoryCandidate],
) -> list[GoalErrorMemory]:
    """Persist only candidates whose evidence is literally present in the user message."""
    stored: list[GoalErrorMemory] = []
    normalized_user = _normalized(user_content)
    for candidate in candidates:
        evidence = candidate.evidence_quote.strip()
        if not evidence or _normalized(evidence) not in normalized_user:
            continue
        if candidate.operation == "RETRACT":
            existing = session.get(GoalErrorMemory, candidate.memory_id)
            if existing is None or existing.goal_id != goal_id:
                continue
            existing.status = "SUPERSEDED"
            existing.evidence = evidence
            existing.source_message_id = source_message_id
            existing.updated_at = utc_now()
            stored.append(existing)
            continue
        refs = sorted(set(candidate.target_refs))
        identity = "\n".join([candidate.scope, _normalized(candidate.rule), *refs])
        content_hash = hashlib.sha256(identity.encode("utf-8")).hexdigest()
        existing = session.scalar(
            select(GoalErrorMemory).where(
                GoalErrorMemory.goal_id == goal_id,
                GoalErrorMemory.content_hash == content_hash,
            )
        )
        valid_until = (
            date.today() + timedelta(days=candidate.valid_days)
            if candidate.valid_days is not None
            else None
        )
        if existing is not None:
            existing.rule = candidate.rule.strip()
            existing.evidence = evidence
            existing.source_message_id = source_message_id
            existing.scope = candidate.scope
            existing.target_refs_json = refs
            existing.status = "CONFIRMED"
            existing.valid_until = valid_until
            existing.updated_at = utc_now()
            stored.append(existing)
            continue
        memory = GoalErrorMemory(
            goal_id=goal_id,
            rule=candidate.rule.strip(),
            evidence=evidence,
            source_message_id=source_message_id,
            scope=candidate.scope,
            target_refs_json=refs,
            status="CONFIRMED",
            content_hash=content_hash,
            valid_until=valid_until,
        )
        session.add(memory)
        stored.append(memory)
    session.flush()
    return stored
