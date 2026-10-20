from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session

from planner_api.config import get_settings
from planner_api.database import get_db_session
from planner_api.models import GoalLongTermMemory, KnowledgeChunk, KnowledgeDocument, KnowledgeGoalLink

from .search import KnowledgeSearch
from .store import KnowledgeStore

router = APIRouter(prefix="/api/goals/{goal_id}/knowledge", tags=["knowledge"])
Database = Annotated[Session, Depends(get_db_session)]


class KnowledgeQuery(BaseModel):
    query: str = Field(min_length=1, max_length=1000)
    limit: int = Field(default=6, ge=1, le=12)
    max_chars: int = Field(default=6000, ge=500, le=12000)
    include_shared: bool = True
    phase: Literal["CLARIFICATION", "ROUTE", "PLAN", "EXECUTION", "RECOVERY"] = "CLARIFICATION"

    @field_validator("query")
    @classmethod
    def strip_query(cls, value):
        if not value.strip():
            raise ValueError("query cannot be blank")
        return value.strip()


class SharingUpdate(BaseModel):
    shared: bool


@router.get("/memories")
def memories(goal_id: str, session: Database):
    store(session).goal(goal_id)
    rows = session.scalars(select(GoalLongTermMemory).where(GoalLongTermMemory.goal_id == goal_id,
        GoalLongTermMemory.active.is_(True)).order_by(GoalLongTermMemory.updated_at.desc()))
    return {"items": [{"id": r.id, "memory_key": r.memory_key, "content": r.content,
        "evidence_quote": r.evidence_quote, "source_message_id": r.source_message_id,
        "updated_at": r.updated_at} for r in rows]}


@router.delete("/memories/{memory_id}")
def forget_memory(goal_id: str, memory_id: str, session: Database):
    store(session).goal(goal_id)
    row = session.get(GoalLongTermMemory, memory_id)
    if row is None or row.goal_id != goal_id:
        raise HTTPException(404, "记忆不属于当前规划")
    row.active = False
    session.execute(update(KnowledgeDocument).where(KnowledgeDocument.kind == "GOAL_MEMORY",
        KnowledgeDocument.source_uri == "memory://" + row.id).values(current=False))
    session.commit()
    return {"removed": True}


def store(session):
    settings = get_settings()
    if not settings.knowledge_enabled:
        raise HTTPException(409, "知识库尚未启用")
    return KnowledgeStore(session, settings)


@router.post("/sync")
def sync(goal_id: str, session: Database):
    library = store(session)
    count = library.sync_goal(goal_id)
    usage = library.evict()
    # An explicit sync is also the manual retry after the bounded worker attempts.
    local_docs = select(KnowledgeGoalLink.document_id).where(KnowledgeGoalLink.goal_id == goal_id)
    session.execute(update(KnowledgeChunk).where(KnowledgeChunk.document_id.in_(local_docs),
        KnowledgeChunk.milvus_status == "FAILED").values(milvus_status="PENDING", milvus_attempts=0))
    session.commit()
    return {"captured": count, "usage": usage}


@router.post("/search")
def search(goal_id: str, payload: KnowledgeQuery, session: Database):
    library = store(session)
    result = KnowledgeSearch(session, library.settings).search(goal_id, payload.query,
        limit=payload.limit, max_chars=payload.max_chars, phase=payload.phase,
        include_shared=payload.include_shared, manual=True)
    session.commit()
    return result


@router.get("")
def listing(goal_id: str, session: Database):
    library = store(session)
    visible = library.visible(goal_id, manual=True, phase=None)
    states = dict(session.execute(select(KnowledgeChunk.index_status, func.count()).where(
        KnowledgeChunk.document_id.in_([d.id for d, _ in visible])).group_by(KnowledgeChunk.index_status)).all())
    milvus_states = dict(session.execute(select(KnowledgeChunk.milvus_status, func.count()).where(
        KnowledgeChunk.document_id.in_([d.id for d, _ in visible])).group_by(KnowledgeChunk.milvus_status)).all())
    return {"documents": [{"id": d.id, "title": d.title, "kind": d.kind, "source_uri": d.source_uri,
        "shared": d.shared, "scope": "PLAN" if local else "SHARED", "fetched_at": d.fetched_at,
        "byte_size": d.byte_size, "chunk_count": d.chunk_count, "accessed_count": d.accessed_count}
        for d, local in visible],
        "indexed_chunks": states.get("READY", 0), "index_states": states,
        "vector_backend": library.settings.knowledge_vector_backend, "milvus_sync_states": milvus_states,
        "limits": {"bytes": library.settings.knowledge_max_bytes,
                   "documents": library.settings.knowledge_max_documents, "chunks": library.settings.knowledge_max_chunks}}


@router.get("/documents/{document_id}")
def read(goal_id: str, document_id: str, session: Database, offset: int = 0):
    library = store(session)
    docs = {d.id: d for d, _ in library.visible(goal_id, manual=True, phase=None)}
    doc = docs.get(document_id)
    if doc is None:
        raise HTTPException(404, "资料不存在或当前规划无权读取")
    if offset < 0:
        raise HTTPException(422, "offset must be nonnegative")
    # Chunk pagination also works for excerpt-only webpages and uploaded files;
    # disjoint windows are returned separately, never concatenated as a quote.
    chunks = list(session.scalars(select(KnowledgeChunk).where(KnowledgeChunk.document_id == doc.id,
        KnowledgeChunk.sequence >= offset).order_by(KnowledgeChunk.sequence).limit(8)))
    return {"document_id": doc.id, "source_uri": doc.source_uri, "title": doc.title,
        "fetched_at": doc.fetched_at, "coverage": doc.metadata_json.get("coverage"),
        "next_offset": offset + len(chunks) if offset + len(chunks) < doc.chunk_count else None,
        "chunks": [{"id": c.id, "text": c.text, "char_start": c.char_start, "char_end": c.char_end,
                    "heading": c.heading, "locator": c.locator_json} for c in chunks]}


def owned(session, goal_id, document_id):
    library = store(session)
    library.goal(goal_id)
    if not session.scalar(select(KnowledgeGoalLink.id).where(KnowledgeGoalLink.goal_id == goal_id,
            KnowledgeGoalLink.document_id == document_id)):
        raise HTTPException(404, "资料未关联到当前规划")
    doc = next((d for d, local in library.visible(goal_id, manual=True, phase=None) if local and d.id == document_id), None)
    if doc is None:
        raise HTTPException(404, "资料版本或授权已失效")
    return doc


@router.patch("/documents/{document_id}/sharing")
def sharing(goal_id: str, document_id: str, payload: SharingUpdate, session: Database):
    doc = owned(session, goal_id, document_id)
    if doc.kind == "GOAL_MEMORY":
        raise HTTPException(409, "用户长期记忆仅属于本规划，不可共享")
    doc.shared = payload.shared
    session.commit()
    return {"id": doc.id, "shared": doc.shared}


@router.delete("/documents/{document_id}")
def remove(goal_id: str, document_id: str, session: Database):
    doc = owned(session, goal_id, document_id)
    session.execute(delete(KnowledgeDocument).where(KnowledgeDocument.id == doc.id))
    session.commit()
    return {"removed": True, "original_source_deleted": False}
