from __future__ import annotations

from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from planner_api.models import (
    Conversation,
    Goal,
    ResearchEvidence,
    ResearchRun,
    Message,
    utc_now,
)

from .orchestrator import ResearchOrchestrator
from .schemas import ResearchEvidenceRead, ResearchPacket, ResearchRunRead
from .strategy import refresh_research_coverage


class ResearchRunCancelled(Exception):
    """Raised internally when a checkpoint observes a cancellation request."""


class ResearchLeaseLost(Exception):
    """Raised internally when another worker owns an active lease."""


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


class ResearchRunService:
    """Executes bounded research while durably recording progress and evidence."""

    def __init__(
        self,
        session: Session,
        orchestrator: ResearchOrchestrator | None = None,
        *,
        lease_seconds: int = 300,
        max_recovery_attempts: int = 3,
    ) -> None:
        self.session = session
        if orchestrator is None:
            from planner_api.config import get_settings
            if get_settings().llm_provider == "codex":
                from planner_api.codex_runtime.research import CodexResearchOrchestrator
                orchestrator = CodexResearchOrchestrator()
        self.orchestrator = orchestrator or ResearchOrchestrator()
        self.lease_seconds = max(30, lease_seconds)
        self.max_recovery_attempts = max(1, max_recovery_attempts)

    def execute(
        self,
        goal_id: str,
        question: str,
        *,
        enabled: bool = True,
        conversation_id: str | None = None,
        idempotency_key: str | None = None,
        decision_context: dict[str, object] | None = None,
    ) -> ResearchRunRead:
        self._validate_scope(goal_id, conversation_id)
        if idempotency_key:
            existing = self.session.scalar(
                select(ResearchRun).where(ResearchRun.idempotency_key == idempotency_key)
            )
            if existing is not None:
                if existing.goal_id != goal_id:
                    raise HTTPException(409, "Research idempotency key belongs to another goal")
                return self._read(existing)
        row = ResearchRun(
            goal_id=goal_id,
            conversation_id=conversation_id,
            question=question,
            enabled=enabled,
            idempotency_key=idempotency_key,
            budget_json=asdict(self.orchestrator.limits),
        )
        self.session.add(row)
        try:
            self.session.commit()
        except IntegrityError:
            self.session.rollback()
            if not idempotency_key:
                raise
            existing = self.session.scalar(
                select(ResearchRun).where(ResearchRun.idempotency_key == idempotency_key)
            )
            if existing is None or existing.goal_id != goal_id:
                raise
            return self._read(existing)

        self._claim(row, "ANALYZING" if enabled else "DISABLED")
        row.started_at = utc_now()
        self.session.commit()
        return self._run_claimed(row, decision_context=decision_context)

    def enqueue(
        self,
        goal_id: str,
        question: str,
        *,
        enabled: bool = True,
        conversation_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> ResearchRunRead:
        """Persist a research request without running network or model work."""
        self._validate_scope(goal_id, conversation_id)
        if idempotency_key:
            existing = self.session.scalar(
                select(ResearchRun).where(ResearchRun.idempotency_key == idempotency_key)
            )
            if existing is not None:
                if existing.goal_id != goal_id:
                    raise HTTPException(409, "Research idempotency key belongs to another goal")
                return self._read(existing)
        row = ResearchRun(
            goal_id=goal_id,
            conversation_id=conversation_id,
            question=question,
            enabled=enabled,
            status="QUEUED" if enabled else "DISABLED",
            current_stage="QUEUED" if enabled else "DISABLED",
            idempotency_key=idempotency_key,
            budget_json=asdict(self.orchestrator.limits),
        )
        self.session.add(row)
        try:
            self.session.commit()
        except IntegrityError:
            self.session.rollback()
            if not idempotency_key:
                raise
            existing = self.session.scalar(
                select(ResearchRun).where(ResearchRun.idempotency_key == idempotency_key)
            )
            if existing is None or existing.goal_id != goal_id:
                raise
            return self._read(existing)
        if not enabled:
            packet = ResearchPacket(question=question, status="DISABLED", run_id=row.id)
            row.current_stage = "COMPLETED"
            row.completed_at = utc_now()
            self._store_packet(row, packet, final=True)
            self.session.commit()
        return self._read(row)

    def process_queued(
        self, goal_id: str, run_id: str, *, lease_id: str | None = None,
        decision_context: dict[str, object] | None = None,
    ) -> ResearchRunRead:
        row = self.session.get(ResearchRun, run_id)
        if row is None or row.goal_id != goal_id:
            raise HTTPException(404, "Research run not found")
        owned_lease = (
            row.status == "RUNNING"
            and lease_id is not None
            and row.lease_id == lease_id
            and row.lease_expires_at is not None
            and _as_utc(row.lease_expires_at) > utc_now()
        )
        if not owned_lease and row.status != "QUEUED":
            return self._read(row)
        if not owned_lease:
            self._claim(row, "ANALYZING")
            row.started_at = utc_now()
            self.session.commit()
        checkpoint_packet = None
        resume_stage = None
        if row.checkpoint_json and row.resume_stage:
            try:
                checkpoint_packet = ResearchPacket.model_validate(row.checkpoint_json)
                resume_stage = row.resume_stage
            except (TypeError, ValueError):
                checkpoint_packet = None
        return self._run_claimed(
            row, checkpoint_packet=checkpoint_packet, resume_stage=resume_stage,
            decision_context=decision_context,
        )

    def _run_claimed(
        self,
        row: ResearchRun,
        *,
        decision_context: dict[str, object] | None = None,
        checkpoint_packet: ResearchPacket | None = None,
        resume_stage: str | None = None,
    ) -> ResearchRunRead:

        # Both synchronous entry points and queued workers need the same bounded
        # conversation, rather than treating a follow-up as an isolated query.
        goal = self.session.get(Goal, row.goal_id)
        messages = list(self.session.scalars(select(Message).where(
            Message.conversation_id == row.conversation_id,
            Message.generation_status == "SUCCEEDED",
        ).order_by(Message.sequence.desc()).limit(6))) if row.conversation_id else []
        previous = self.session.scalar(select(ResearchRun).where(
            ResearchRun.goal_id == row.goal_id, ResearchRun.id != row.id,
            ResearchRun.status.in_({"READY", "PARTIAL", "COMPLETED"}),
        ).order_by(ResearchRun.created_at.desc()).limit(1))
        previous_data = (previous.result_json or previous.checkpoint_json or {}) if previous else {}
        decision_context = {
            "original_input": goal.raw_input[:1800] if goal else "",
            "recent_messages": [{"role": m.role, "content": m.content[:1200]}
                                for m in reversed(messages)],
            "previous_research": {"question": previous.question[:1000],
                                  "brief": previous_data.get("brief"),
                                  "queries": [{k: q.get(k) for k in ("query", "anchor_entities", "source_profiles", "source_scope_mode")}
                                              for q in previous_data.get("subqueries", [])[:4]]} if previous else {},
            **(decision_context or {}),
        }
        if row.enabled and checkpoint_packet is None:
            from planner_api.config import get_settings
            settings = get_settings()
            if settings.knowledge_enabled:
                from planner_api.knowledge.integration import recall_context
                conversation = self.session.get(Conversation, row.conversation_id) if row.conversation_id else None
                decision_context.update(recall_context(self.session.get_bind(), settings, row.goal_id,
                    row.question, phase=conversation.phase if conversation else "CLARIFICATION",
                    anchor=goal.raw_input[:400] if goal else ""))

        def checkpoint(stage: str, packet: ResearchPacket) -> None:
            self._assert_active(row)
            self._heartbeat(row, stage)
            row.current_stage = stage
            self._store_packet(row, packet, final=False)
            self.session.commit()

        try:
            packet = self.orchestrator.research(
                row.question,
                enabled=row.enabled,
                run_id=row.id,
                on_progress=checkpoint,
                decision_context=decision_context,
                checkpoint=checkpoint_packet,
                resume_stage=resume_stage,
            )
        except ResearchRunCancelled:
            return self._read(row)
        except ResearchLeaseLost:
            self._interrupt(row, "研究运行租约已失效，等待恢复。", "RESEARCH_LEASE_LOST")
            return self._read(row)
        except Exception as error:
            self.session.rollback()
            failed = self.session.get(ResearchRun, row.id)
            if failed is not None:
                failed.status = "FAILED"
                failed.current_stage = "FAILED"
                failed.error_code = "RESEARCH_EXECUTION_FAILED"
                failed.error_message = str(error)[:2000]
                failed.completed_at = utc_now()
                self.session.commit()
            raise

        self._assert_active(row)
        row.status = packet.status
        row.current_stage = "COMPLETED"
        row.completed_at = utc_now()
        self._release_lease(row)
        self._store_packet(row, packet, final=True)
        self.session.commit()
        return self._read(row)

    def resume(self, goal_id: str, run_id: str) -> ResearchRunRead:
        row = self.session.get(ResearchRun, run_id)
        if row is None or row.goal_id != goal_id:
            raise HTTPException(404, "Research run not found")
        if row.status != "INTERRUPTED":
            raise HTTPException(409, "Only interrupted research runs can be resumed")
        if not row.checkpoint_json:
            raise HTTPException(409, "Research run has no checkpoint to resume")
        checkpoint = ResearchPacket.model_validate(row.checkpoint_json)
        self._claim(row, row.resume_stage or "ANALYZING")
        row.error_code = None
        row.error_message = None
        row.completed_at = None
        self.session.commit()

        def checkpoint_progress(stage: str, packet: ResearchPacket) -> None:
            self._assert_active(row)
            self._heartbeat(row, stage)
            row.current_stage = stage
            self._store_packet(row, packet, final=False)
            self.session.commit()

        try:
            packet = self.orchestrator.research(
                row.question,
                enabled=row.enabled,
                run_id=row.id,
                on_progress=checkpoint_progress,
                checkpoint=checkpoint,
                resume_stage=row.current_stage,
            )
        except ResearchRunCancelled:
            return self._read(row)
        except ResearchLeaseLost:
            self._interrupt(row, "研究运行租约已失效，等待恢复。", "RESEARCH_LEASE_LOST")
            return self._read(row)
        except Exception as error:
            self.session.rollback()
            failed = self.session.get(ResearchRun, row.id)
            if failed is not None:
                failed.status = "FAILED"
                failed.current_stage = "FAILED"
                failed.error_code = "RESEARCH_RESUME_FAILED"
                failed.error_message = str(error)[:2000]
                failed.completed_at = utc_now()
                self.session.commit()
            raise
        self._assert_active(row)
        row.status = packet.status
        row.current_stage = "COMPLETED"
        row.resume_stage = None
        row.completed_at = utc_now()
        self._release_lease(row)
        self._store_packet(row, packet, final=True)
        self.session.commit()
        return self._read(row)

    def cancel(self, goal_id: str, run_id: str, reason: str = "用户取消研究") -> ResearchRunRead:
        row = self.session.get(ResearchRun, run_id)
        if row is None or row.goal_id != goal_id:
            raise HTTPException(404, "Research run not found")
        if row.status in {"COMPLETED", "READY", "PARTIAL", "SKIPPED", "FAILED", "CANCELLED"}:
            return self._read(row)
        if row.status == "QUEUED":
            row.status = "CANCELLED"
            row.current_stage = "CANCELLED"
            row.cancel_requested_at = utc_now()
            row.cancel_reason = reason[:500]
            row.completed_at = utc_now()
            self._release_lease(row)
            self.session.commit()
            return self._read(row)
        row.cancel_requested_at = utc_now()
        row.cancel_reason = reason[:500]
        row.status = "CANCEL_REQUESTED"
        self.session.commit()
        return self._read(row)

    def retry(self, goal_id: str, run_id: str) -> ResearchRunRead:
        """Requeue a failed/partial/interrupted run, retaining its checkpoint."""
        row = self.session.get(ResearchRun, run_id)
        if row is None or row.goal_id != goal_id:
            raise HTTPException(404, "Research run not found")
        if row.status == "READY" and row.result_json:
            reassessed = ResearchPacket.model_validate(row.result_json)
            refresh_research_coverage(reassessed)
            if reassessed.status == "PARTIAL":
                row.status = "PARTIAL"
        if row.status not in {"FAILED", "PARTIAL", "INTERRUPTED", "CANCELLED"}:
            raise HTTPException(
                409, "Only failed, partial, interrupted or cancelled research runs can be retried"
            )
        if row.recovery_count >= self.max_recovery_attempts:
            raise HTTPException(409, "Research recovery limit reached")
        row.recovery_count += 1
        row.status = "QUEUED"
        row.current_stage = "QUEUED"
        row.error_code = None
        row.error_message = None
        row.completed_at = None
        row.cancel_requested_at = None
        row.cancel_reason = None
        if row.checkpoint_json:
            row.resume_stage = row.resume_stage or "ANALYZING"
        row.lease_id = None
        row.lease_expires_at = None
        self.session.commit()
        return self._read(row)

    def get(self, goal_id: str, run_id: str) -> ResearchRunRead:
        row = self.session.get(ResearchRun, run_id)
        if row is None or row.goal_id != goal_id:
            raise HTTPException(404, "Research run not found")
        return self._read(row)

    def list(self, goal_id: str) -> list[ResearchRunRead]:
        if self.session.get(Goal, goal_id) is None:
            raise HTTPException(404, "Goal not found")
        rows = self.session.scalars(
            select(ResearchRun)
            .where(ResearchRun.goal_id == goal_id)
            .order_by(ResearchRun.created_at.desc())
        )
        return [self._read(row) for row in rows]

    def recover_stale_runs(
        self,
        stale_before: datetime,
        *,
        goal_id: str | None = None,
    ) -> int:
        now = utc_now()
        query = select(ResearchRun).where(
            ResearchRun.status.in_(["RUNNING", "CANCEL_REQUESTED"])
        )
        if goal_id is not None:
            query = query.where(ResearchRun.goal_id == goal_id)
        rows = list(self.session.scalars(query))
        changed = 0
        for row in rows:
            lease_expired = (
                row.lease_expires_at is not None and _as_utc(row.lease_expires_at) <= now
            )
            if not lease_expired and _as_utc(row.updated_at) >= _as_utc(stale_before):
                continue
            changed += 1
            if row.status == "CANCEL_REQUESTED":
                row.status = "CANCELLED"
                row.current_stage = "CANCELLED"
                row.completed_at = now
                self._release_lease(row)
                continue
            if row.recovery_count >= self.max_recovery_attempts:
                row.status = "FAILED"
                row.current_stage = "FAILED"
                row.error_code = "RESEARCH_RECOVERY_LIMIT"
                row.error_message = "研究任务多次中断，已超过自动恢复上限。"
                row.completed_at = now
            else:
                row.recovery_count += 1
                row.resume_stage = row.current_stage
                row.status = "INTERRUPTED"
                row.current_stage = "INTERRUPTED"
                row.error_code = "RESEARCH_INTERRUPTED"
                row.error_message = "研究任务在完成前中断，可使用已保存的检查点重新发起。"
                row.completed_at = now
            self._release_lease(row)
        if rows:
            self.session.commit()
        return changed

    def _claim(self, row: ResearchRun, stage: str) -> None:
        row.status = "RUNNING"
        row.current_stage = stage
        row.attempt_count += 1
        row.lease_id = str(uuid4())
        row.lease_expires_at = utc_now() + timedelta(seconds=self.lease_seconds)
        row.last_heartbeat_at = utc_now()

    def _heartbeat(self, row: ResearchRun, stage: str) -> None:
        row.current_stage = stage
        row.last_heartbeat_at = utc_now()
        row.lease_expires_at = utc_now() + timedelta(seconds=self.lease_seconds)

    def _assert_active(self, row: ResearchRun) -> None:
        self.session.refresh(row)
        if row.cancel_requested_at is not None or row.status == "CANCEL_REQUESTED":
            row.status = "CANCELLED"
            row.current_stage = "CANCELLED"
            row.completed_at = utc_now()
            self._release_lease(row)
            self.session.commit()
            raise ResearchRunCancelled
        if row.lease_expires_at is not None and _as_utc(row.lease_expires_at) <= utc_now():
            raise ResearchLeaseLost

    @staticmethod
    def _release_lease(row: ResearchRun) -> None:
        row.lease_id = None
        row.lease_expires_at = None

    def _interrupt(self, row: ResearchRun, message: str, code: str) -> None:
        row.resume_stage = row.current_stage
        row.status = "INTERRUPTED"
        row.current_stage = "INTERRUPTED"
        row.error_code = code
        row.error_message = message
        row.completed_at = utc_now()
        self._release_lease(row)
        self.session.commit()

    def _validate_scope(self, goal_id: str, conversation_id: str | None) -> None:
        if self.session.get(Goal, goal_id) is None:
            raise HTTPException(404, "Goal not found")
        if conversation_id is None:
            return
        conversation = self.session.get(Conversation, conversation_id)
        if conversation is None or conversation.goal_id != goal_id:
            raise HTTPException(404, "Conversation not found for this goal")

    def _store_packet(
        self,
        row: ResearchRun,
        packet: ResearchPacket,
        *,
        final: bool,
    ) -> None:
        payload = packet.model_dump(mode="json")
        row.checkpoint_json = payload
        row.decision_json = packet.decision.model_dump(mode="json") if packet.decision else {}
        row.stop_reason = packet.stop_reason
        if final:
            row.result_json = payload
        self._upsert_evidence(row.id, packet)
        if final:
            from planner_api.config import get_settings
            from planner_api.knowledge.store import capture
            capture(self.session, get_settings(), goal_id=row.goal_id, packet=packet)

    def _upsert_evidence(self, run_id: str, packet: ResearchPacket) -> None:
        existing = {
            evidence.canonical_url: evidence
            for evidence in self.session.scalars(
                select(ResearchEvidence).where(ResearchEvidence.run_id == run_id)
            )
        }
        for source in packet.sources:
            evidence = existing.get(source.canonical_url)
            if evidence is None:
                evidence = ResearchEvidence(
                    run_id=run_id,
                    canonical_url=source.canonical_url,
                    url=str(source.url),
                )
                self.session.add(evidence)
                existing[source.canonical_url] = evidence
            evidence.url = str(source.url)
            evidence.title = source.title[:300]
            evidence.source = source.source[:160]
            evidence.engines_json = list(source.engines)
            evidence.snippet = source.snippet
            evidence.content_excerpt = source.content_excerpt
            evidence.relevance_score = source.relevance_score
            evidence.confidence = source.confidence
            evidence.ranking_json = {
                "ranking_score": source.ranking_score,
                "relevance_score": source.relevance_score,
                "quality_score": source.quality_score,
                "freshness_score": source.freshness_score,
                "source_category": source.source_category,
                "matched_queries": source.matched_queries,
                "ranking_reasons": source.ranking_reasons,
                "duplicate_urls": source.duplicate_urls,
                "duplicate_of": source.duplicate_of,
                "content_fingerprint": source.content_fingerprint,
                "source_cluster_id": source.source_cluster_id,
                "source_independence_score": source.source_independence_score,
                "page_quality_score": source.page_quality_score,
                "page_quality_flags": source.page_quality_flags,
                "page_content_type": source.page_content_type,
                "page_readability_applied": source.page_readability_applied,
                "evidence_level": source.evidence_level,
                "evidence_density": source.evidence_density,
            }
            evidence.read_status = source.read_status
            evidence.retrieval_method = source.retrieval_method[:40]
            evidence.error_code = source.error_code

    def _read(self, row: ResearchRun) -> ResearchRunRead:
        evidence_rows = self.session.scalars(
            select(ResearchEvidence)
            .where(ResearchEvidence.run_id == row.id)
            .order_by(ResearchEvidence.created_at)
        )
        result = ResearchPacket.model_validate(row.result_json) if row.result_json else None
        if result is not None:
            refresh_research_coverage(result)
        return ResearchRunRead(
            id=row.id,
            goal_id=row.goal_id,
            conversation_id=row.conversation_id,
            question=row.question,
            enabled=row.enabled,
            status=result.status if row.status == "READY" and result is not None else row.status,
            current_stage=row.current_stage,
            decision=dict(row.decision_json or {}),
            budget=dict(row.budget_json or {}),
            checkpoint=dict(row.checkpoint_json or {}),
            result=result,
            stop_reason=row.stop_reason,
            error_code=row.error_code,
            error_message=row.error_message,
            created_at=row.created_at,
            started_at=row.started_at,
            updated_at=row.updated_at,
            resume_stage=row.resume_stage,
            schema_version=row.schema_version,
            attempt_count=row.attempt_count,
            recovery_count=row.recovery_count,
            idempotency_key=row.idempotency_key,
            lease_expires_at=row.lease_expires_at,
            last_heartbeat_at=row.last_heartbeat_at,
            cancel_requested_at=row.cancel_requested_at,
            cancel_reason=row.cancel_reason,
            completed_at=row.completed_at,
            evidence=[self._evidence_read(item) for item in evidence_rows],
        )

    @staticmethod
    def _evidence_read(row: ResearchEvidence) -> ResearchEvidenceRead:
        return ResearchEvidenceRead(
            id=row.id,
            canonical_url=row.canonical_url,
            url=row.url,
            title=row.title,
            source=row.source,
            engines=list(row.engines_json or []),
            snippet=row.snippet,
            content_excerpt=row.content_excerpt,
            relevance_score=row.relevance_score,
            confidence=row.confidence,
            ranking=dict(row.ranking_json or {}),
            read_status=row.read_status,
            retrieval_method=row.retrieval_method,
            error_code=row.error_code,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )
