"""One durable, leased indexing queue; optional warmup never blocks health/replies."""
from __future__ import annotations

from datetime import timedelta
import logging
from threading import Event, Thread
from uuid import uuid4

from sqlalchemy import case, or_, select, update

from planner_api.models import KnowledgeChunk, KnowledgeDocument, utc_now

from .store import KnowledgeStore, bytes_of
from .vectors import _indexing, publish_encoder, valid
from .milvus_sync import MilvusSync


class KnowledgeWorker:
    def __init__(self, session_factory, settings, *, load_encoder=None):
        self.session_factory, self.settings = session_factory, settings
        self.load_encoder = load_encoder
        self.encoder = None
        self.stop_event = Event()
        self.thread = None
        self.milvus_sync = MilvusSync(session_factory, settings)

    def start(self):
        if not self.settings.knowledge_enabled or not self.settings.knowledge_vectors_enabled:
            return
        self.thread = Thread(target=self.run, name="knowledge-index", daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        if self.thread is not None:
            self.thread.join(timeout=1)

    def run(self):
        while not self.stop_event.is_set():
            try:
                processed = self.run_once()
            except Exception as error:
                logging.getLogger(__name__).warning("Knowledge indexing unavailable: %s", type(error).__name__)
                self.stop_event.wait(30)
                continue
            if not processed:
                self.stop_event.wait(2)

    def run_once(self):
        if self.encoder is None:
            if self.load_encoder is not None:
                loaded = self.load_encoder()
            else:
                from planner_api.materials.semantic import encoder
                loaded = encoder(self.settings.material_embedding_model,
                    self.settings.material_embedding_cache_dir, self.settings.material_embedding_revision)
            # Complete one actual inference before publishing readiness.
            warm = loaded.encode(["资料检索预热"], query=True)
            if len(warm) != 1 or not valid(warm[0]):
                self.encoder = None
                raise ValueError("Embedding warmup failed")
            self.encoder = loaded
            publish_encoder(self.settings.material_embedding_identity, self.encoder)
        now = utc_now()
        token = str(uuid4())
        with self.session_factory() as session:
            condition = or_(KnowledgeChunk.index_status == "PENDING",
                (KnowledgeChunk.index_status == "INDEXING") & (KnowledgeChunk.lease_until < now),
                (KnowledgeChunk.index_status == "READY") &
                (KnowledgeChunk.vector_model != self.settings.material_embedding_identity))
            ids = list(session.scalars(select(KnowledgeChunk.id).where(condition,
                KnowledgeChunk.index_attempts < 3).order_by(KnowledgeChunk.id).limit(8)))
            # Compare-and-swap permits several app processes without duplicate
            # writes. No database transaction spans model inference.
            rows = []
            if ids:
                rows = list(session.execute(update(KnowledgeChunk).where(KnowledgeChunk.id.in_(ids), condition,
                    KnowledgeChunk.index_attempts < 3).values(index_status="INDEXING", lease_id=token,
                    lease_until=now + timedelta(minutes=10), index_attempts=KnowledgeChunk.index_attempts + 1)
                    .returning(KnowledgeChunk.id, KnowledgeChunk.text, KnowledgeChunk.heading, KnowledgeChunk.content_hash)))
                session.commit()
        if not rows:
            return self.milvus_sync.run_once()
        try:
            _indexing.set()
            try:
                vectors = self.encoder.encode([row.heading + "\n" + row.text for row in rows])
            finally:
                _indexing.clear()
            if len(vectors) != len(rows) or any(not valid(v) for v in vectors):
                raise ValueError("Invalid embedding batch")
            dimension = len(vectors[0]) if vectors else 0
            if any(len(v) != dimension for v in vectors):
                raise ValueError("Mixed embedding dimensions")
            with self.session_factory() as session:
                for row, vector in zip(rows, vectors, strict=True):
                    chunk = session.scalar(select(KnowledgeChunk).where(KnowledgeChunk.id == row.id,
                        KnowledgeChunk.lease_id == token, KnowledgeChunk.content_hash == row.content_hash))
                    if chunk is None:
                        continue
                    doc = session.get(KnowledgeDocument, chunk.document_id)
                    if doc is None:
                        continue
                    doc.byte_size += bytes_of(vector) - bytes_of(chunk.vector_json)
                    chunk.vector_json = vector
                    chunk.vector_model = self.settings.material_embedding_identity
                    chunk.index_status = "READY"
                    chunk.milvus_status = "PENDING"
                    chunk.milvus_attempts = 0
                    chunk.milvus_target = ""
                    chunk.milvus_lease_id = None
                    chunk.milvus_lease_until = None
                    chunk.index_attempts = 0
                    chunk.lease_id = None
                    chunk.lease_until = None
                KnowledgeStore(session, self.settings).evict()
                session.commit()
        except Exception:
            with self.session_factory() as session:
                session.execute(update(KnowledgeChunk).where(KnowledgeChunk.lease_id == token).values(
                    index_status=case((KnowledgeChunk.index_attempts >= 3, "FAILED"), else_="PENDING"),
                    lease_id=None, lease_until=None))
                session.commit()
            raise
        self.milvus_sync.run_once()
        return True
