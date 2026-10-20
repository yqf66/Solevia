"""Source-backed, bounded original-text cache. No summaries or generated facts."""
from __future__ import annotations

import hashlib
import json
import logging
import math
from datetime import UTC

from fastapi import HTTPException
from sqlalchemy import delete, select
from sqlalchemy.orm import Session, aliased

from planner_api.config import Settings
from planner_api.materials.chunking import chunk_material
from planner_api.models import (
    Goal, GoalMaterialLink, KnowledgeChunk, KnowledgeDocument, KnowledgeGoalLink,
    MaterialChunk, MaterialEmbedding, MaterialRevision, MaterialRoot, MaterialSource, ResearchRun,
    utc_now,
)


def digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf8")).hexdigest()


def bytes_of(value) -> int:
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf8"))


class KnowledgeStore:
    def __init__(self, session: Session, settings: Settings):
        self.session, self.settings = session, settings

    def goal(self, goal_id):
        if self.session.get(Goal, goal_id) is None:
            raise HTTPException(404, "Goal not found")

    def _link(self, doc, goal_id):
        if self.session.scalar(select(KnowledgeGoalLink.id).where(
                KnowledgeGoalLink.document_id == doc.id, KnowledgeGoalLink.goal_id == goal_id)) is None:
            self.session.add(KnowledgeGoalLink(document_id=doc.id, goal_id=goal_id))
            self.session.flush()

    def put(self, goal_id, *, kind, uri, title, body, pieces, metadata,
            revision_id=None, shared=False, fetched_at=None):
        """Caller owns the transaction. Oversized documents are skipped whole."""
        self.goal(goal_id)
        if not pieces:
            return None
        original = [{k: v for k, v in p.items() if k not in {"vector", "model"}} for p in pieces]
        content_hash = digest(body if body else json.dumps(original, sort_keys=True, ensure_ascii=False))
        identity = digest(json.dumps([kind, uri, revision_id, content_hash], ensure_ascii=False))
        doc = self.session.scalar(select(KnowledgeDocument).where(KnowledgeDocument.identity == identity))
        if doc is not None:
            self._link(doc, goal_id)
            # Identical fetches renew freshness; cache retrieval never does.
            if fetched_at is not None and fetched_at > _utc(doc.fetched_at):
                doc.fetched_at = fetched_at
                self._supersede(doc)
            return doc
        cost = len(body.encode("utf8")) + bytes_of([uri, title, metadata, pieces])
        if cost > self.settings.knowledge_max_bytes or len(pieces) > self.settings.knowledge_max_chunks:
            return None
        doc = KnowledgeDocument(identity=identity, kind=kind, source_uri=uri, title=title[:300],
            content_hash=content_hash, body=body, material_revision_id=revision_id, shared=shared,
            metadata_json=metadata, byte_size=cost, chunk_count=len(pieces), fetched_at=fetched_at or utc_now())
        self.session.add(doc)
        self.session.flush()
        self._supersede(doc)
        for index, piece in enumerate(pieces):
            vector = piece.get("vector", [])
            self.session.add(KnowledgeChunk(document_id=doc.id, sequence=index, text=piece["text"],
                heading=piece.get("heading", "")[:300], char_start=piece["start"], char_end=piece["end"],
                locator_json=piece.get("locator", {}), content_hash=digest(piece["text"]),
                vector_json=vector, vector_model=piece.get("model", ""),
                index_status="READY" if vector else "PENDING"))
        self._link(doc, goal_id)
        self.session.flush()
        self.evict(protect=doc.id)
        return doc

    def _supersede(self, doc):
        if doc.metadata_json.get("coverage") != "FULL_DOCUMENT":
            return
        others = list(self.session.scalars(select(KnowledgeDocument).where(
            KnowledgeDocument.kind == doc.kind, KnowledgeDocument.source_uri == doc.source_uri,
            KnowledgeDocument.id != doc.id)))
        if any(d.metadata_json.get("coverage") == "FULL_DOCUMENT"
               and _utc(d.fetched_at) > _utc(doc.fetched_at) for d in others):
            doc.current = False
            return
        doc.current = True
        for previous in others:
            previous.current = False

    def ingest_packet(self, goal_id, packet, *, fetched_at=None):
        count = 0
        for source in packet.sources:
            if source.read_status != "SUCCEEDED" or source.evidence_level not in {"FULL_TEXT", "DIRECT_EVIDENCE"}:
                continue
            passages = [p for p in packet.passages if p.source_url_or_uri in {str(source.url), source.canonical_url}
                        and not p.trust_metadata.get("is_search_snippet") and digest(p.text) == p.content_hash]
            body = _archive_body(source.evidence_archive)
            if body is not None:
                leading = len(body) - len(body.lstrip())
                pieces = [{"text": c.text, "start": c.char_start + leading, "end": c.char_end + leading,
                           "heading": c.section_title, "locator": {"artifact": "section_markdown"}}
                          for c in chunk_material(body, max_chars=1200, overlap_chars=160)]
            else:
                # Distant excerpts remain distinct; never invent their intervening context.
                pieces = [{"text": c.text, "start": c.char_start, "end": c.char_end,
                           "heading": c.section_title,
                           "locator": {"passage_id": p.passage_id, "original_locator": p.locator}}
                          for p in passages for c in chunk_material(p.text, max_chars=1200, overlap_chars=160)]
            collected_at = max((p.extracted_at for p in passages), default=fetched_at or utc_now())
            doc = self.put(goal_id, kind="WEB", uri=source.canonical_url, title=source.title,
                body=body or "", pieces=pieces, shared=True, fetched_at=collected_at,
                metadata={"coverage": "FULL_DOCUMENT" if body is not None else "EXCERPTS",
                          "source_category": source.source_category, "archive": source.evidence_archive,
                          "origin_run_id": packet.run_id, "fact_status": "CANDIDATE"})
            count += doc is not None
        return count

    def ingest_material(self, revision):
        if revision.status != "READY":
            return 0
        source = self.session.get(MaterialSource, revision.source_id)
        links = list(self.session.scalars(select(GoalMaterialLink).where(
            GoalMaterialLink.source_id == revision.source_id, GoalMaterialLink.status == "ACTIVE")))
        if source is None or not links:
            return 0
        chunks = list(self.session.scalars(select(MaterialChunk).where(
            MaterialChunk.revision_id == revision.id).order_by(MaterialChunk.sequence)))
        existing = {e.chunk_id: e for e in self.session.scalars(select(MaterialEmbedding).where(
            MaterialEmbedding.chunk_id.in_([c.id for c in chunks]),
            MaterialEmbedding.model == self.settings.material_embedding_identity))}
        pieces = []
        for c in chunks:
            if digest(c.text) != c.content_hash:
                return 0
            e = existing.get(c.id)
            valid = e is not None and e.content_hash == c.content_hash
            pieces.append({"text": c.text, "start": c.char_start, "end": c.char_end,
                "heading": c.section_title, "locator": {"material_chunk_id": c.id, "locations": c.locator_json},
                "vector": e.vector_json if valid else [], "model": e.model if valid else ""})
        count = 0
        for link in links:
            doc = self.put(link.goal_id, kind="MATERIAL", uri=source.canonical_uri, title=source.title,
                body="", pieces=pieces, revision_id=revision.id, fetched_at=_utc(revision.completed_at or revision.created_at),
                metadata={"coverage": "PARSED_CHUNKS", "source_id": source.id,
                          "revision": revision.revision, "source_content_hash": revision.content_hash,
                          "warnings": revision.metadata_json.get("warnings", []), "fact_status": "CANDIDATE"})
            count += doc is not None
        return count

    def visible(self, goal_id, *, phase="CLARIFICATION", manual=False, local_only=False):
        """Filter access BEFORE lexical/vector search. Recheck live versions and links."""
        self.goal(goal_id)
        local_ids = set(self.session.scalars(select(KnowledgeGoalLink.document_id).where(
            KnowledgeGoalLink.goal_id == goal_id)))
        docs = list(self.session.scalars(select(KnowledgeDocument).where(KnowledgeDocument.current.is_(True))
            .order_by(KnowledgeDocument.fetched_at.desc()).execution_options(populate_existing=True)))
        allowed = []
        newer = aliased(MaterialRevision)
        for doc in docs:
            local = doc.id in local_ids
            if doc.kind == "GOAL_MEMORY":
                # These projections are never public external evidence.
                if not local:
                    continue
                from planner_api.models import GoalLongTermMemory
                memory = self.session.get(GoalLongTermMemory, doc.metadata_json.get("memory_id"))
                if memory is None or memory.goal_id != goal_id or not memory.active:
                    continue
                if doc.body != memory.content + "\n用户原话：" + memory.evidence_quote:
                    continue
            if not local and (local_only or not doc.shared):
                continue
            if doc.kind == "MATERIAL":
                source = self.session.execute(select(MaterialRevision, MaterialSource, MaterialRoot.status)
                    .join(MaterialSource, MaterialSource.id == MaterialRevision.source_id)
                    .outerjoin(MaterialRoot, MaterialRoot.id == MaterialSource.root_id)
                    .where(MaterialRevision.id == doc.material_revision_id).execution_options(populate_existing=True)).first()
                if (source is None or source[0].status != "READY" or source[1].status != "ACTIVE"
                        or source[2] not in {None, "ACTIVE"}):
                    continue
                if self.session.scalar(select(newer.id).where(newer.source_id == source[0].source_id,
                        newer.revision > source[0].revision).limit(1)):
                    continue
                links = list(self.session.scalars(select(GoalMaterialLink).where(
                    GoalMaterialLink.source_id == source[1].id, GoalMaterialLink.status == "ACTIVE")
                    .execution_options(populate_existing=True)))
                usable = [link for link in links if (link.goal_id == goal_id or (not local and doc.shared))
                          and (phase is None or link.scope in {"ALL", phase}) and (manual or link.usage_policy == "AUTO")]
                if not usable:
                    continue
            allowed.append((doc, local))
        return allowed

    def sync_goal(self, goal_id):
        self.goal(goal_id)
        count = 0
        from planner_api.models import GoalLongTermMemory
        for memory in self.session.scalars(select(GoalLongTermMemory).where(
                GoalLongTermMemory.goal_id == goal_id, GoalLongTermMemory.active.is_(True))):
            text = memory.content + "\n用户原话：" + memory.evidence_quote
            doc = self.put(goal_id, kind="GOAL_MEMORY", uri="memory://" + memory.id,
                title=memory.memory_key, body=text, pieces=[{"text": text, "start": 0, "end": len(text)}],
                metadata={"memory_id": memory.id, "coverage": "USER_CONTEXT"}, shared=False)
            if doc is not None:
                doc.current = True
                count += 1
        sources = list(self.session.scalars(select(GoalMaterialLink.source_id).where(
            GoalMaterialLink.goal_id == goal_id, GoalMaterialLink.status == "ACTIVE")))
        for source_id in sources:
            revision = self.session.scalar(select(MaterialRevision).where(MaterialRevision.source_id == source_id)
                .order_by(MaterialRevision.revision.desc()).limit(1))
            if revision is not None:
                count += self.ingest_material(revision)
        from planner_api.research.schemas import ResearchPacket
        for run in self.session.scalars(select(ResearchRun).where(ResearchRun.goal_id == goal_id,
                ResearchRun.status.in_({"READY", "PARTIAL", "COMPLETED"}))
                .order_by(ResearchRun.created_at.desc()).limit(20)):
            try:
                packet = ResearchPacket.model_validate(run.result_json or {})
            except ValueError:
                continue
            count += self.ingest_packet(goal_id, packet, fetched_at=_utc(run.completed_at or run.created_at))
        self.session.flush()
        return count

    def evict(self, protect=None):
        self.session.flush()
        docs = list(self.session.scalars(select(KnowledgeDocument)))
        total = sum(d.byte_size for d in docs)
        chunks = sum(d.chunk_count for d in docs)
        now = utc_now()
        def value(d):
            age = max(0, (now - _utc(d.last_accessed_at)).total_seconds() / 86400)
            return (int(d.current), math.log1p(d.accessed_count) / (1 + age / 7) + 2 / (1 + age), d.id)
        for doc in sorted((d for d in docs if d.id != protect), key=value):
            if total <= self.settings.knowledge_max_bytes and chunks <= self.settings.knowledge_max_chunks and len(docs) <= self.settings.knowledge_max_documents:
                break
            total -= doc.byte_size
            chunks -= doc.chunk_count
            docs.remove(doc)
            self.session.execute(delete(KnowledgeDocument).where(KnowledgeDocument.id == doc.id))
        self.session.flush()
        return {"documents": len(docs), "chunks": chunks, "bytes": total}


def _utc(value):
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _archive_body(archive):
    """Same reader-owned path/size/hash boundary as existing archive reading."""
    from planner_api.research.archive_windows import ARCHIVES, ROOT
    try:
        if archive.get("reader") != "crawl4ai":
            return None
        artifact = archive["artifacts"]["section_markdown"]
        path = (ROOT / artifact["path"]).resolve()
        if not path.is_relative_to(ARCHIVES.resolve()) or path.stat().st_size > 2_000_000:
            return None
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != artifact["sha256"]:
            return None
        return data.decode("utf8").replace("\r\n", "\n")
    except (AttributeError, KeyError, TypeError, OSError, UnicodeError, ValueError):
        return None


def capture(session, settings, *, goal_id=None, packet=None, revision=None):
    """Cache failure cannot roll back the owning research/material transaction."""
    if not settings.knowledge_enabled:
        return
    try:
        with session.begin_nested():
            store = KnowledgeStore(session, settings)
            if packet is not None:
                store.ingest_packet(goal_id, packet)
            if revision is not None:
                store.ingest_material(revision)
    except Exception as error:
        logging.getLogger(__name__).warning("Knowledge capture unavailable: %s", type(error).__name__)
