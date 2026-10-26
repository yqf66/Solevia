"""Reuse existing vectors, synchronize outside SQL transactions, recover leases."""
from __future__ import annotations

from datetime import timedelta
import logging
import time
from uuid import uuid4

from sqlalchemy import or_, select, update

from planner_api.models import KnowledgeChunk, KnowledgeDocument, utc_now

from .milvus import sync_target, try_store
from .store import digest
from .vectors import valid

log = logging.getLogger(__name__)


class MilvusSync:
    def __init__(self, factory, settings):
        self.factory, self.settings = factory, settings
        self.next_attempt = 0.0
        self.next_reconcile = 0.0
        self.dimensions = {384}

    def run_once(self):
        if (self.settings.knowledge_vector_backend != "MILVUS"
                or time.monotonic() < self.next_attempt):
            return False
        store = try_store(self.settings)
        now, token = utc_now(), str(uuid4())
        with self.factory() as session:
            session.execute(update(KnowledgeChunk).where(KnowledgeChunk.milvus_status == "SYNCING",
                KnowledgeChunk.milvus_lease_until < now, KnowledgeChunk.milvus_attempts >= 3).values(
                milvus_status="FAILED", milvus_lease_id=None, milvus_lease_until=None))
            rows = list(session.scalars(select(KnowledgeChunk).join(KnowledgeDocument).where(
                KnowledgeDocument.current.is_(True), KnowledgeChunk.index_status == "READY",
                KnowledgeChunk.vector_model == self.settings.material_embedding_identity,
                or_(KnowledgeChunk.milvus_status != "SYNCING", KnowledgeChunk.milvus_lease_until < now),
                KnowledgeChunk.milvus_attempts < 3).order_by(KnowledgeChunk.id)))
            eligible = [c for c in rows if valid(c.vector_json) and digest(c.text) == c.content_hash
                and (c.milvus_status != "READY"
                     or c.milvus_target != sync_target(self.settings, len(c.vector_json)))][:64]
            records = []
            for c in eligible:
                claimed = session.execute(update(KnowledgeChunk).where(KnowledgeChunk.id == c.id,
                    or_(KnowledgeChunk.milvus_status != "SYNCING", KnowledgeChunk.milvus_lease_until < now)
                ).values(milvus_status="SYNCING", milvus_lease_id=token,
                    milvus_lease_until=now + timedelta(minutes=2),
                    milvus_attempts=KnowledgeChunk.milvus_attempts + 1).execution_options(
                        synchronize_session=False)).rowcount
                if claimed:
                    records.append((c.id, c.document_id, c.content_hash, list(c.vector_json)))
            session.commit()
        if not records:
            self.reconcile(store)
            return False
        self.dimensions.update(len(v) for _, _, _, v in records)
        succeeded = False
        try:
            for dimension in {len(v) for _, _, _, v in records}:
                store.upsert([r for r in records if len(r[3]) == dimension])
            succeeded = True
        except Exception as error:
            store.failed()
            self.next_attempt = time.monotonic() + 30
            log.warning("Milvus sync unavailable: %s", type(error).__name__)
        with self.factory() as session:
            for key, _, content_hash, vector in records:
                session.execute(update(KnowledgeChunk).where(KnowledgeChunk.id == key,
                    KnowledgeChunk.milvus_lease_id == token, KnowledgeChunk.content_hash == content_hash,
                    KnowledgeChunk.vector_model == self.settings.material_embedding_identity).values(
                    milvus_status="READY" if succeeded else "FAILED",
                    milvus_target=sync_target(self.settings, len(vector)) if succeeded else "",
                    milvus_attempts=0 if succeeded else KnowledgeChunk.milvus_attempts,
                    milvus_lease_id=None, milvus_lease_until=None))
            session.commit()
        return True

    def reconcile(self, store):
        """Remove evicted versions and detect a dropped index; no source deletion."""
        if time.monotonic() < self.next_reconcile:
            return
        self.next_reconcile = time.monotonic() + 300
        with self.factory() as session:
            dimensions = set(len(v) for v in session.scalars(select(KnowledgeChunk.vector_json).where(
                KnowledgeChunk.vector_model == self.settings.material_embedding_identity)) if valid(v))
        self.dimensions.update(dimensions)
        try:
            for dimension in self.dimensions:
                remote = store.inventory(dimension)
                with self.factory() as session:
                    live = {c.id for c in session.scalars(select(KnowledgeChunk).join(KnowledgeDocument).where(
                        KnowledgeDocument.current.is_(True), KnowledgeChunk.index_status == "READY",
                        KnowledgeChunk.vector_model == self.settings.material_embedding_identity))
                        if valid(c.vector_json, dimension) and digest(c.text) == c.content_hash}
                    stale = sorted(set(remote or []) - live)
                    missing = live - set(remote or [])
                    if missing:
                        session.execute(update(KnowledgeChunk).where(KnowledgeChunk.id.in_(missing),
                            KnowledgeChunk.milvus_status == "READY").values(milvus_status="PENDING",
                            milvus_attempts=0, milvus_target=""))
                    session.commit()
                if stale:
                    store.delete(stale, dimension)
        except Exception as error:
            store.failed()
            log.warning("Milvus reconciliation unavailable: %s", type(error).__name__)
