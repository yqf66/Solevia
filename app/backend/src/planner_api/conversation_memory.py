from __future__ import annotations

import hashlib
from collections.abc import Iterable

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from planner_api.config import get_settings
from planner_api.context_compiler import compile_user_text
from planner_api.models import ClarificationAnalysisJob, Conversation, ConversationSummary, Message, utc_now
from planner_api.schemas import ConversationMessagePage, MessageRead


def require_clarification_analysis_complete(session: Session, goal_id: str) -> None:
    """Formal planning must not publish using an unprocessed user condition."""
    from fastapi import HTTPException
    pending = session.scalar(select(ClarificationAnalysisJob.id).where(
        ClarificationAnalysisJob.goal_id == goal_id,
        ClarificationAnalysisJob.status != "SUCCEEDED",
    ).limit(1))
    if pending:
        raise HTTPException(409, {"code": "CLARIFICATION_ANALYSIS_PENDING",
            "message": "正在整理最新条件，完成后即可继续规划；对话已保存。"})


def _message_excerpt(message: Message, limit: int = 900) -> str:
    content, _ = compile_user_text(message.content, limit, include_header=False)
    label = "用户" if message.role == "USER" else "助手"
    return f"{label}({message.sequence}): {content}"


def refresh_phase_summary(
    session: Session,
    conversation: Conversation,
    *,
    confirmed_conclusions: Iterable[str] = (),
    open_questions: Iterable[str] = (),
) -> ConversationSummary | None:
    """Create an append-only rolling summary for non-clarification phases."""
    session.flush()
    summary_trigger_chars = get_settings().conversation_summary_trigger_chars
    latest = session.scalar(
        select(ConversationSummary)
        .where(
            ConversationSummary.conversation_id == conversation.id,
            ConversationSummary.summary_level == "PHASE",
        )
        .order_by(ConversationSummary.through_message_sequence.desc())
    )
    through = latest.through_message_sequence if latest else 0
    messages = list(
        session.scalars(
            select(Message)
            .where(
                Message.conversation_id == conversation.id,
                Message.sequence > through,
                Message.generation_status == "SUCCEEDED",
            )
            .order_by(Message.sequence)
        )
    )
    if not messages:
        return latest

    segments: list[ConversationSummary] = []
    batch: list[Message] = []
    batch_chars = 0
    for message in messages:
        batch.append(message)
        batch_chars += len(message.content)
        if len(batch) >= 12 or batch_chars >= summary_trigger_chars:
            segments.append(_persist_segment_summary(session, conversation, batch))
            batch = []
            batch_chars = 0
    if batch:
        segments.append(_persist_segment_summary(session, conversation, batch))

    new_text = "\n".join(segment.summary for segment in segments)
    source = f"{latest.summary}\n{new_text}" if latest else new_text
    summary, _manifest = compile_user_text(source, 6000, include_header=False)
    conclusions = list(
        dict.fromkeys(
            [
                *(latest.confirmed_conclusions_json if latest else []),
                *confirmed_conclusions,
            ]
        )
    )[-40:]
    questions = list(
        dict.fromkeys(
            [
                *(latest.open_questions_json if latest else []),
                *open_questions,
            ]
        )
    )[-12:]
    changes = list(latest.recent_changes_json if latest else [])
    for message in messages:
        metadata = message.metadata_json or {}
        change = metadata.get("change_summary")
        if change:
            changes.append(
                {
                    "sequence": message.sequence,
                    "intent": metadata.get("intent"),
                    "change_summary": change,
                }
            )
    changes = changes[-20:]
    digest = hashlib.sha256(summary.encode("utf-8")).hexdigest()
    if latest and latest.content_hash == digest:
        return latest
    if latest:
        latest.superseded_at = utc_now()
    row = ConversationSummary(
        conversation_id=conversation.id,
        from_message_sequence=1,
        through_message_sequence=messages[-1].sequence,
        summary_level="PHASE",
        summary=summary,
        confirmed_conclusions_json=conclusions,
        open_questions_json=questions,
        conflicts_json=list(latest.conflicts_json if latest else []),
        current_topic=next(
            (message.content[:500] for message in reversed(messages) if message.role == "USER"),
            latest.current_topic if latest else None,
        ),
        recent_changes_json=changes,
        entity_refs_json=list(latest.entity_refs_json if latest else []),
        readiness_json=dict(latest.readiness_json if latest else {}),
        source_summary_ids_json=[
            *([latest.id] if latest else []),
            *(segment.id for segment in segments),
        ],
        content_hash=digest,
    )
    session.add(row)
    session.flush()
    return row


def _persist_segment_summary(
    session: Session,
    conversation: Conversation,
    messages: list[Message],
) -> ConversationSummary:
    source = "\n".join(_message_excerpt(message) for message in messages)
    summary, _manifest = compile_user_text(source, 2600, include_header=False)
    digest = hashlib.sha256(summary.encode("utf-8")).hexdigest()
    existing = session.scalar(
        select(ConversationSummary).where(
            ConversationSummary.conversation_id == conversation.id,
            ConversationSummary.summary_level == "SEGMENT",
            ConversationSummary.from_message_sequence == messages[0].sequence,
            ConversationSummary.through_message_sequence == messages[-1].sequence,
            ConversationSummary.content_hash == digest,
        )
    )
    if existing:
        return existing
    row = ConversationSummary(
        conversation_id=conversation.id,
        from_message_sequence=messages[0].sequence,
        through_message_sequence=messages[-1].sequence,
        summary_level="SEGMENT",
        summary=summary,
        confirmed_conclusions_json=[],
        open_questions_json=[],
        conflicts_json=[],
        current_topic=next(
            (message.content[:500] for message in reversed(messages) if message.role == "USER"),
            None,
        ),
        recent_changes_json=[],
        entity_refs_json=[],
        readiness_json={},
        source_summary_ids_json=[],
        content_hash=digest,
    )
    session.add(row)
    session.flush()
    return row


def read_message_page(
    session: Session,
    goal_id: str,
    phase: str,
    *,
    before_sequence: int | None = None,
    limit: int = 50,
) -> ConversationMessagePage:
    normalized_phase = phase.upper()
    if normalized_phase not in {"CLARIFICATION", "ROUTE", "PLAN", "EXECUTION"}:
        raise ValueError("Unknown conversation phase")
    bounded_limit = max(1, min(limit, 100))
    conversation = session.scalar(
        select(Conversation).where(
            Conversation.goal_id == goal_id,
            Conversation.phase == normalized_phase,
        )
    )
    if conversation is None:
        return ConversationMessagePage(items=[])
    conditions = [Message.conversation_id == conversation.id]
    if before_sequence is not None:
        conditions.append(Message.sequence < before_sequence)
    rows = list(
        session.scalars(
            select(Message)
            .where(*conditions)
            .order_by(Message.sequence.desc())
            .limit(bounded_limit + 1)
        )
    )
    has_more = len(rows) > bounded_limit
    rows = rows[:bounded_limit]
    rows.reverse()
    total = int(
        session.scalar(
            select(func.count(Message.id)).where(Message.conversation_id == conversation.id)
        )
        or 0
    )
    return ConversationMessagePage(
        items=[
            MessageRead(
                id=item.id,
                sequence=item.sequence,
                role=item.role,
                content=item.content,
                generation_status=item.generation_status,
                metadata=dict(item.metadata_json or {}),
                created_at=item.created_at,
            )
            for item in rows
        ],
        next_before_sequence=rows[0].sequence if has_more and rows else None,
        has_more=has_more,
        total=total,
    )
