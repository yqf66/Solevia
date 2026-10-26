from __future__ import annotations

from contextlib import nullcontext
import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from threading import Event, Thread
from uuid import uuid4

from sqlalchemy import select, update
from sqlalchemy.orm import Session, sessionmaker

from planner_api.config import Settings, get_settings
from planner_api.models import ResearchResponseRequest, ResearchRun

from .persistence import ResearchRunService

logger = logging.getLogger(__name__)


class ResearchWorker:
    """Single local worker for durable queued research runs."""

    def __init__(
        self,
        session_factory: sessionmaker[Session] | Callable[[], Session],
        settings: Settings | None = None,
        *, readiness=None,
    ) -> None:
        self.session_factory = session_factory
        self.settings = settings or get_settings()
        self.readiness = readiness
        self._stop = Event()
        self._thread: Thread | None = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = Thread(target=self.run_forever, name="research-worker", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            if not self._thread.is_alive():
                self._thread = None

    def run_forever(self) -> None:
        while not self._stop.is_set():
            try:
                if get_settings().llm_provider == "fake":
                    self._stop.wait(1)
                    continue
                processed = self.run_once()
            except Exception:
                logger.exception("Research worker iteration failed")
                processed = False
            if not processed:
                self._stop.wait(self.settings.research_worker_poll_seconds)

    def run_once(self) -> bool:
        from .responses import ResearchResponseService

        with self.session_factory() as session:
            if ResearchResponseService(session).process_next():
                return True
        if self.readiness is not None and not self.readiness.snapshot()["ready"]:
            with self.session_factory() as session:
                queued = session.scalar(select(ResearchRun.id).where(ResearchRun.status == "QUEUED").limit(1))
                if queued is not None:
                    self.readiness.request_use()
            return False
        claimed = self._claim_next_run()
        if claimed is None:
            return False
        goal_id, run_id, lease_id = claimed
        with self.session_factory() as session:
            try:
                request = session.scalar(select(ResearchResponseRequest).where(ResearchResponseRequest.run_id == run_id))
                with self.readiness.usage() if self.readiness is not None else nullcontext():
                    ResearchRunService(
                        session,
                        lease_seconds=self.settings.research_worker_lease_seconds,
                    ).process_queued(goal_id, run_id, lease_id=lease_id,
                                     decision_context=request.snapshot_json.get("decision_context") if request else None)
            except Exception:
                logger.exception("Research run %s failed", run_id)
        return True

    def _claim_next_run(self) -> tuple[str, str, str] | None:
        now = datetime.now(UTC)
        with self.session_factory() as session:
            # Do not take SQLite's write lock during idle polling. A read-only
            # preflight is enough to decide whether the claim transaction is
            # needed at all.
            stale_before = now - timedelta(seconds=self.settings.research_worker_lease_seconds)
            interrupted_before = now - timedelta(
                seconds=max(2, self.settings.research_worker_poll_seconds)
            )
            has_queued = session.scalar(
                select(ResearchRun.id)
                .where(
                    ResearchRun.status == "QUEUED",
                    ResearchRun.cancel_requested_at.is_(None),
                )
                .limit(1)
            )
            has_stale = session.scalar(
                select(ResearchRun.id)
                .where(
                    ResearchRun.status.in_(["RUNNING", "CANCEL_REQUESTED"]),
                    ResearchRun.lease_expires_at < now,
                    ResearchRun.updated_at < stale_before,
                )
                .limit(1)
            )
            has_resumable = session.scalar(
                select(ResearchRun.id)
                .where(
                    ResearchRun.status == "INTERRUPTED",
                    ResearchRun.checkpoint_json.is_not(None),
                    ResearchRun.recovery_count < self.settings.research_max_recovery_attempts,
                    ResearchRun.updated_at < interrupted_before,
                )
                .limit(1)
            )
            if has_queued is None and has_stale is None and has_resumable is None:
                session.rollback()
                return None
            session.rollback()
            if session.get_bind().dialect.name == "sqlite":
                session.connection().exec_driver_sql("BEGIN IMMEDIATE")
            stale = list(
                session.scalars(
                    select(ResearchRun).where(
                        ResearchRun.status.in_(["RUNNING", "CANCEL_REQUESTED"]),
                        ResearchRun.lease_expires_at < now,
                        ResearchRun.updated_at < stale_before,
                    )
                )
            )
            for row in stale:
                if row.status == "CANCEL_REQUESTED":
                    row.status = "CANCELLED"
                    row.current_stage = "CANCELLED"
                    row.completed_at = now
                    row.lease_id = None
                    row.lease_expires_at = None
                    continue
                previous_stage = row.current_stage
                row.recovery_count += 1
                row.status = "INTERRUPTED"
                row.current_stage = "INTERRUPTED"
                row.resume_stage = previous_stage
                row.error_code = "RESEARCH_WORKER_INTERRUPTED"
                row.error_message = "研究 Worker 租约已过期，可从检查点恢复。"
                row.completed_at = now
                row.lease_id = None
                row.lease_expires_at = None
                if row.recovery_count >= self.settings.research_max_recovery_attempts:
                    row.status = "FAILED"
                    row.current_stage = "FAILED"
                    row.error_code = "RESEARCH_RECOVERY_LIMIT"
                    row.error_message = "研究任务多次中断，已超过自动恢复上限。"
            # Automatically put checkpointed interruptions back in the queue.
            # A small age guard prevents the same run from being re-claimed in
            # the same transaction immediately after a lease expires.
            resumable = list(
                session.scalars(
                    select(ResearchRun).where(
                        ResearchRun.status == "INTERRUPTED",
                        ResearchRun.checkpoint_json.is_not(None),
                        ResearchRun.recovery_count < self.settings.research_max_recovery_attempts,
                        ResearchRun.updated_at < interrupted_before,
                    )
                )
            )
            for row in resumable:
                row.status = "QUEUED"
                row.current_stage = "QUEUED"
                row.error_code = "RESEARCH_AUTO_RESUME"
                row.error_message = "已从检查点自动重新排队。"
                row.completed_at = None
                row.lease_id = None
                row.lease_expires_at = None
            candidate = session.scalar(
                select(ResearchRun)
                .where(
                    ResearchRun.status == "QUEUED",
                    ResearchRun.cancel_requested_at.is_(None),
                )
                .order_by(ResearchRun.created_at, ResearchRun.id)
                .limit(1)
            )
            if candidate is None:
                session.commit()
                return None
            lease_id = str(uuid4())
            claim = session.execute(
                update(ResearchRun)
                .where(
                    ResearchRun.id == candidate.id,
                    ResearchRun.status == "QUEUED",
                )
                .values(
                    status="RUNNING",
                    current_stage="ANALYZING",
                    attempt_count=ResearchRun.attempt_count + 1,
                    lease_id=lease_id,
                    lease_expires_at=now
                    + timedelta(seconds=self.settings.research_worker_lease_seconds),
                    last_heartbeat_at=now,
                    updated_at=now,
                )
            )
            session.commit()
            return (candidate.goal_id, candidate.id, lease_id) if claim.rowcount == 1 else None


def main() -> None:
    """Run the research queue outside the ASGI process when desired."""
    from planner_api.database import SessionLocal

    worker = ResearchWorker(SessionLocal)
    try:
        worker.run_forever()
    except KeyboardInterrupt:
        worker.stop()


if __name__ == "__main__":
    main()
