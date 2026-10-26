"""Keep explicit attachment selection when a discussion answer is retried."""
from sqlalchemy import select

from planner_api.models import Message


def resolve_discussion_sources(session, conversation, content, intent, sources, *, regenerate=False):
    if sources is not None:
        return sources
    recent = list(session.scalars(select(Message).where(Message.conversation_id == conversation.id)
                                 .order_by(Message.sequence.desc()).limit(2)))
    if len(recent) != 2:
        return []
    assistant, user = recent
    if (assistant.role == 'ASSISTANT' and user.role == 'USER' and user.content.strip() == content
            and (regenerate or assistant.generation_status == 'FAILED')
            and ((assistant.metadata_json or {}).get('intent') or (user.metadata_json or {}).get('intent')) == intent):
        return list((user.metadata_json or {}).get('material_source_ids', []))
    return []


def remember_discussion_sources(session, conversation, sources, context=None):
    session.flush()
    user = session.scalar(select(Message).where(Message.conversation_id == conversation.id, Message.role == 'USER')
                          .order_by(Message.sequence.desc()).limit(1))
    if user is not None:
        user.metadata_json = {**(user.metadata_json or {}), 'material_source_ids': list(sources or [])}
        from planner_api.codex_runtime.conversation_files import bind_file_message
        bind_file_message(context, user)
