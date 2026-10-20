from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from threading import Event, Thread

from sqlalchemy import select, update
from sqlalchemy.orm import Session, sessionmaker

from planner_api.config import Settings, get_settings
from planner_api.models import MaterialProcessingJob, MaterialRevision, MaterialSource

from .processing import MaterialProcessingService

logger = logging.getLogger(__name__)


class MaterialProcessingWorker:
    """Small durable SQLite queue worker; one instance is started by the API app."""

    def __init__(
        self,
        session_factory: sessionmaker[Session] | Callable[[], Session],
        settings: Settings | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.settings = settings or get_settings()
        self._stop = Event()
        self._thread: Thread | None = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = Thread(target=self.run_forever, name="material-processing", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            if not self._thread.is_alive():
                self._thread = None
            else:
                logger.warning("Material processing worker has not stopped yet")

    def run_forever(self) -> None:
        while not self._stop.is_set():
            try:
                processed = self.run_once()
            except Exception:
                logger.exception("Material processing worker iteration failed")
                processed = False
            if not processed:
                self._stop.wait(self.settings.material_processing_poll_seconds)

    def run_once(self) -> bool:
        job_id = self._claim_next_job()
        if job_id is None:
            return False
        with self.session_factory() as session:
            job = session.get(MaterialProcessingJob, job_id)
            if job is None:
                return True
            job.queue_wait_ms = _elapsed_ms(job.queued_at, job.started_at)
            session.commit()
            revision = session.get(MaterialRevision, job.revision_id)
            source = session.get(MaterialSource, revision.source_id) if revision else None
            if revision is None or source is None:
                self._finish_failed(
                    session,
                    job,
                    revision,
                    "MATERIAL_SOURCE_MISSING",
                    "资料 revision 或来源记录不存在。",
                )
                return True
            try:
                processor = MaterialProcessingService(session, self.settings)
                path = processor._resolve_path(source)
                revision = processor.process_revision(
                    revision,
                    path,
                    is_cancel_requested=lambda: self._cancel_requested_fresh(job_id),
                )
                completed_at = datetime.now(UTC)
                job.status = revision.status
                job.error_code = revision.error_code
                job.error_message = revision.error_message
                job.lease_expires_at = None
                job.completed_at = completed_at
                job.processing_duration_ms = _elapsed_ms(job.started_at, completed_at)
                session.commit()
            except Exception as error:
                session.rollback()
                job = session.get(MaterialProcessingJob, job_id)
                revision = session.get(MaterialRevision, job.revision_id) if job else None
                if job is not None:
                    self._finish_failed(
                        session,
                        job,
                        revision,
                        getattr(error, "code", "MATERIAL_PROCESSING_FAILED"),
                        str(error),
                    )
        return True

    def _cancel_requested_fresh(self, job_id: str) -> bool:
        with self.session_factory() as session:
            return self._cancel_requested(session, job_id)

    @staticmethod
    def _cancel_requested(session: Session, job_id: str) -> bool:
        return (
            session.scalar(
                select(MaterialProcessingJob.cancel_requested_at).where(
                    MaterialProcessingJob.id == job_id
                )
            )
            is not None
        )

    def _claim_next_job(self) -> str | None:
        now = datetime.now(UTC)
        with self.session_factory() as session:
            # An idle worker must not reserve SQLite's write lock every poll.
            # Check for work in a read transaction first, then upgrade to an
            # immediate transaction only when stale or queued work exists.
            has_queued = session.scalar(
                select(MaterialProcessingJob.id)
                .where(
                    MaterialProcessingJob.status == "QUEUED",
                    MaterialProcessingJob.cancel_requested_at.is_(None),
                    MaterialProcessingJob.attempts
                    < self.settings.material_processing_max_attempts,
                )
                .limit(1)
            )
            has_stale = session.scalar(
                select(MaterialProcessingJob.id)
                .where(
                    MaterialProcessingJob.status == "PROCESSING",
                    MaterialProcessingJob.lease_expires_at < now,
                )
                .limit(1)
            )
            if has_queued is None and has_stale is None:
                session.rollback()
                return None
            session.rollback()
            if session.get_bind().dialect.name == "sqlite":
                # Acquire the write reservation before selecting a row so competing
                # workers serialize the claim instead of racing a deferred lock upgrade.
                session.connection().exec_driver_sql("BEGIN IMMEDIATE")
            stale_jobs = list(
                session.scalars(
                    select(MaterialProcessingJob).where(
                        MaterialProcessingJob.status == "PROCESSING",
                        MaterialProcessingJob.lease_expires_at < now,
                    )
                )
            )
            for job in stale_jobs:
                revision = session.get(MaterialRevision, job.revision_id)
                if job.cancel_requested_at is not None:
                    job.status = "CANCELLED"
                    job.completed_at = now
                    job.lease_expires_at = None
                    if revision is not None:
                        revision.status = "CANCELLED"
                        revision.error_code = None
                        revision.error_message = None
                        revision.completed_at = now
                    continue
                if revision is not None and revision.status == "READY":
                    job.status = "READY"
                    job.error_code = None
                    job.error_message = None
                    job.lease_expires_at = None
                    job.completed_at = revision.completed_at or now
                    job.processing_duration_ms = _elapsed_ms(job.started_at, job.completed_at)
                    continue
                if job.attempts >= self.settings.material_processing_max_attempts:
                    job.status = "FAILED"
                    job.error_code = "PROCESSING_ATTEMPTS_EXHAUSTED"
                    job.error_message = "资料处理进程多次中断，已停止自动重试。"
                    job.lease_expires_at = None
                    job.completed_at = now
                    job.processing_duration_ms = _elapsed_ms(job.started_at, now)
                    if revision is not None and revision.status == "PROCESSING":
                        revision.status = "FAILED"
                        revision.error_code = job.error_code
                        revision.error_message = job.error_message
                        revision.completed_at = now
                else:
                    job.status = "QUEUED"
                    job.queued_at = now
                    job.lease_expires_at = None
                    if revision is not None and revision.status == "PROCESSING":
                        revision.status = "QUEUED"
            session.flush()
            candidate_id = session.scalar(
                select(MaterialProcessingJob.id)
                .where(
                    MaterialProcessingJob.status == "QUEUED",
                    MaterialProcessingJob.cancel_requested_at.is_(None),
                    MaterialProcessingJob.attempts
                    < self.settings.material_processing_max_attempts,
                )
                .order_by(MaterialProcessingJob.queued_at, MaterialProcessingJob.id)
                .limit(1)
            )
            if candidate_id is None:
                session.commit()
                return None
            claim = session.execute(
                update(MaterialProcessingJob)
                .where(
                    MaterialProcessingJob.id == candidate_id,
                    MaterialProcessingJob.status == "QUEUED",
                )
                .values(
                    status="PROCESSING",
                    attempts=MaterialProcessingJob.attempts + 1,
                    started_at=now,
                    lease_expires_at=now
                    + timedelta(seconds=self.settings.material_processing_lease_seconds),
                    updated_at=now,
                )
            )
            session.commit()
            return candidate_id if claim.rowcount == 1 else None

    @staticmethod
    def _finish_failed(
        session: Session,
        job: MaterialProcessingJob,
        revision: MaterialRevision | None,
        code: str,
        message: str,
    ) -> None:
        now = datetime.now(UTC)
        job.status = "FAILED"
        job.error_code = str(code)[:64]
        job.error_message = message[:2000]
        job.lease_expires_at = None
        job.completed_at = now
        job.processing_duration_ms = _elapsed_ms(job.started_at, now)
        if revision is not None:
            revision.status = "FAILED"
            revision.error_code = job.error_code
            revision.error_message = job.error_message
            revision.completed_at = now
        session.commit()


def _elapsed_ms(start: datetime | None, end: datetime | None) -> int | None:
    if start is None or end is None:
        return None
    if start.tzinfo is None:
        start = start.replace(tzinfo=UTC)
    if end.tzinfo is None:
        end = end.replace(tzinfo=UTC)
    return max(0, round((end - start).total_seconds() * 1000))


def main() -> None:
    from planner_api.database import SessionLocal

    worker = MaterialProcessingWorker(SessionLocal)
    try:
        worker.run_forever()
    except KeyboardInterrupt:
        worker.stop()


if __name__ == "__main__":
    main()
