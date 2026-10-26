from datetime import UTC, datetime, timedelta

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from planner_api.models import Conversation, Goal, ResearchEvidence, ResearchRun
from planner_api.research.orchestrator import ResearchLimits, ResearchOrchestrator
from planner_api.research.persistence import ResearchRunService
from planner_api.research.schemas import (
    PageDocument,
    ResearchDecision,
    ResearchPacket,
    ResearchReflection,
    SearchHit,
    SearchResponse,
)
from planner_api.research.worker import ResearchWorker


class CompletingAnalyzer:
    def analyze(self, question: str) -> ResearchDecision:
        return ResearchDecision(should_search=True, research_questions=[question])

    def reflect(self, packet) -> ResearchReflection:
        return ResearchReflection(complete=True)


class FakeResearchService:
    def search(self, request) -> SearchResponse:
        return SearchResponse(
            query=request.query,
            results=[
                SearchHit(
                    title="Authoritative result",
                    url="https://example.com/guide?utm_source=test",
                    snippet="A useful source",
                    source="example",
                    engine="bing",
                ),
                SearchHit(
                    title="Authoritative result",
                    url="https://example.com/guide",
                    snippet="The same useful source",
                    source="example",
                    engine="sogou",
                ),
            ],
            totalResults=2,
            searchedAt="now",
        )

    def read(self, request) -> PageDocument:
        return PageDocument(
            url=request.url,
            finalUrl=request.url,
            title="Guide",
            content=f"durable evidence for {request.url} " * 200,
            retrievalMethod="request",
        )


def _goal_and_conversation(session):
    goal = Goal(title="研究持久化", raw_input="研究后制定计划")
    session.add(goal)
    session.flush()
    conversation = Conversation(goal_id=goal.id, phase="PLAN")
    session.add(conversation)
    session.commit()
    return goal, conversation


def test_research_run_persists_result_checkpoints_and_deduplicated_evidence(
    session_factory,
) -> None:
    with session_factory() as session:
        goal, conversation = _goal_and_conversation(session)
        orchestrator = ResearchOrchestrator(
            service=FakeResearchService(),
            limits=ResearchLimits(max_rounds=2, max_read_pages=2),
            intent_analyzer=CompletingAnalyzer(),
        )

        result = ResearchRunService(session, orchestrator).execute(
            goal.id,
            "Authoritative result implementation",
            conversation_id=conversation.id,
        )

        assert result.status == "READY"
        assert result.current_stage == "COMPLETED"
        assert result.result is not None
        assert result.result.run_id == result.id
        assert len(result.result.passages) == 1
        assert result.checkpoint["passages"][0]["passage_id"].startswith("web:")
        assert result.checkpoint["memory"]["recent_rounds"]
        assert result.checkpoint["coverage"]["read_pages"] == 1
        assert result.decision["should_search"] is True
        assert result.budget["max_queries"] == 8
        assert result.stop_reason == "MODEL_COMPLETE"
        assert len(result.evidence) == 1
        assert result.evidence[0].canonical_url == "https://example.com/guide"
        assert result.evidence[0].read_status == "SUCCEEDED"
        assert "durable evidence" in result.evidence[0].content_excerpt
        assert result.evidence[0].ranking["source_category"] == "OTHER"
        assert result.evidence[0].ranking["ranking_score"] > 0
        assert result.evidence[0].ranking["source_cluster_id"]
        assert result.evidence[0].ranking["source_independence_score"] == 1

        assert session.query(ResearchRun).count() == 1
        assert session.query(ResearchEvidence).count() == 1


def test_research_run_idempotency_key_returns_existing_run_without_reexecution(
    session_factory,
) -> None:
    with session_factory() as session:
        goal, conversation = _goal_and_conversation(session)
        service = ResearchRunService(
            session,
            ResearchOrchestrator(
                service=FakeResearchService(),
                limits=ResearchLimits(max_rounds=1, max_read_pages=1),
                intent_analyzer=CompletingAnalyzer(),
            ),
        )

        first = service.execute(
            goal.id,
            "幂等研究",
            conversation_id=conversation.id,
            idempotency_key="research:one",
        )
        second = service.execute(
            goal.id,
            "幂等研究",
            conversation_id=conversation.id,
            idempotency_key="research:one",
        )

        assert first.id == second.id
        assert session.query(ResearchRun).count() == 1
        assert first.attempt_count == 1


def test_research_run_can_be_enqueued_then_processed_from_a_checkpoint(session_factory) -> None:
    with session_factory() as session:
        goal, conversation = _goal_and_conversation(session)
        orchestrator = ResearchOrchestrator(
            service=FakeResearchService(),
            limits=ResearchLimits(max_rounds=2, max_read_pages=2),
            intent_analyzer=CompletingAnalyzer(),
        )
        service = ResearchRunService(session, orchestrator)

        queued = service.enqueue(
            goal.id,
            "异步研究",
            conversation_id=conversation.id,
            idempotency_key="research:queued",
        )
        assert queued.status == "QUEUED"
        assert queued.current_stage == "QUEUED"

        processed = service.process_queued(goal.id, queued.id)

        assert processed.status in {"READY", "PARTIAL"}
        assert processed.attempt_count == 1
        assert processed.result is not None


def test_queued_research_can_be_claimed_by_worker_with_matching_lease(session_factory) -> None:
    with session_factory() as session:
        goal, conversation = _goal_and_conversation(session)
        service = ResearchRunService(
            session,
            ResearchOrchestrator(
                service=FakeResearchService(),
                limits=ResearchLimits(max_rounds=1, max_read_pages=1),
                intent_analyzer=CompletingAnalyzer(),
            ),
        )
        service.enqueue(goal.id, "Worker 研究", conversation_id=conversation.id)

    worker = ResearchWorker(session_factory)
    claimed = worker._claim_next_run()
    assert claimed is not None
    claimed_goal_id, claimed_run_id, lease_id = claimed

    with session_factory() as session:
        row = session.get(ResearchRun, claimed_run_id)
        assert row is not None
        assert row.status == "RUNNING"
        assert row.lease_id == lease_id

        processed = ResearchRunService(
            session,
            ResearchOrchestrator(
                service=FakeResearchService(),
                limits=ResearchLimits(max_rounds=1, max_read_pages=1),
                intent_analyzer=CompletingAnalyzer(),
            ),
        ).process_queued(claimed_goal_id, claimed_run_id, lease_id=lease_id)

        assert processed.status in {"READY", "PARTIAL"}


def test_cancelling_queued_research_finishes_immediately(session_factory) -> None:
    with session_factory() as session:
        goal, conversation = _goal_and_conversation(session)
        service = ResearchRunService(session)
        queued = service.enqueue(goal.id, "取消排队研究", conversation_id=conversation.id)

        cancelled = service.cancel(goal.id, queued.id, "用户取消")

        assert cancelled.status == "CANCELLED"
        assert cancelled.current_stage == "CANCELLED"


def test_worker_finalizes_expired_cancel_request(session_factory) -> None:
    with session_factory() as session:
        goal, _ = _goal_and_conversation(session)
        old = datetime.now(UTC) - timedelta(hours=2)
        row = ResearchRun(
            goal_id=goal.id,
            question="过期取消",
            status="CANCEL_REQUESTED",
            current_stage="READING",
            cancel_requested_at=old,
            lease_expires_at=old,
            updated_at=old,
        )
        session.add(row)
        session.commit()
        run_id = row.id

    assert ResearchWorker(session_factory)._claim_next_run() is None

    with session_factory() as session:
        row = session.get(ResearchRun, run_id)
        assert row is not None
        assert row.status == "CANCELLED"
        assert row.current_stage == "CANCELLED"


def test_research_run_stops_at_checkpoint_after_cancel_request(session_factory) -> None:
    with session_factory() as session:
        goal, conversation = _goal_and_conversation(session)
        service_holder: dict[str, ResearchRunService] = {}

        class CancelAfterFirstCheckpoint:
            limits = ResearchLimits(max_rounds=1)

            def research(self, question, *, run_id, on_progress, **kwargs):
                packet = ResearchPacket(question=question, run_id=run_id)
                on_progress("ANALYZING", packet)
                service_holder["service"].cancel(goal.id, run_id, "用户不再需要")
                on_progress("SEARCHING", packet)
                return packet

        service = ResearchRunService(session, CancelAfterFirstCheckpoint())
        service_holder["service"] = service
        result = service.execute(
            goal.id,
            "可取消研究",
            conversation_id=conversation.id,
        )

        assert result.status == "CANCELLED"
        assert result.cancel_reason == "用户不再需要"
        assert result.current_stage == "CANCELLED"


def test_research_run_recovery_limit_marks_repeated_interruptions_failed(session_factory) -> None:
    with session_factory() as session:
        goal, _ = _goal_and_conversation(session)
        old = datetime.now(UTC) - timedelta(hours=2)
        row = ResearchRun(
            goal_id=goal.id,
            question="重复中断",
            status="RUNNING",
            current_stage="READING",
            checkpoint_json={"question": "重复中断"},
            updated_at=old,
        )
        session.add(row)
        session.commit()
        service = ResearchRunService(session, max_recovery_attempts=1)

        assert service.recover_stale_runs(datetime.now(UTC) - timedelta(minutes=10)) == 1
        session.refresh(row)
        assert row.status == "INTERRUPTED"
        row.status = "RUNNING"
        row.current_stage = "READING"
        row.updated_at = old
        session.commit()

        assert service.recover_stale_runs(datetime.now(UTC) - timedelta(minutes=10)) == 1
        session.refresh(row)
        assert row.status == "FAILED"
        assert row.error_code == "RESEARCH_RECOVERY_LIMIT"


def test_disabled_research_is_recorded_without_provider_calls(session_factory) -> None:
    class ExplodingService:
        def search(self, request):
            raise AssertionError("search must not run")

        def read(self, request):
            raise AssertionError("read must not run")

    with session_factory() as session:
        goal, _ = _goal_and_conversation(session)
        orchestrator = ResearchOrchestrator(
            service=ExplodingService(),
            intent_analyzer=CompletingAnalyzer(),
        )

        result = ResearchRunService(session, orchestrator).execute(
            goal.id, "只整理已有材料", enabled=False
        )

        assert result.status == "DISABLED"
        assert result.current_stage == "COMPLETED"
        assert result.evidence == []


def test_unexpected_execution_error_is_persisted_before_reraising(session_factory) -> None:
    class ExplodingOrchestrator:
        limits = ResearchLimits()

        def research(self, *args, **kwargs):
            raise RuntimeError("unexpected failure")

    with session_factory() as session:
        goal, _ = _goal_and_conversation(session)
        service = ResearchRunService(session, ExplodingOrchestrator())

        with pytest.raises(RuntimeError, match="unexpected failure"):
            service.execute(goal.id, "触发异常")

        persisted = session.scalar(select(ResearchRun))
        assert persisted is not None
        assert persisted.status == "FAILED"
        assert persisted.current_stage == "FAILED"
        assert persisted.error_code == "RESEARCH_EXECUTION_FAILED"
        assert persisted.completed_at is not None


def test_stale_running_research_is_marked_interrupted_without_losing_checkpoint(
    session_factory,
) -> None:
    with session_factory() as session:
        goal, _ = _goal_and_conversation(session)
        old = datetime.now(UTC) - timedelta(hours=2)
        row = ResearchRun(
            goal_id=goal.id,
            question="中断前的问题",
            status="RUNNING",
            current_stage="READING",
            checkpoint_json={"coverage": {"read_pages": 2}},
            updated_at=old,
        )
        session.add(row)
        session.commit()

        recovered = ResearchRunService(session).recover_stale_runs(
            datetime.now(UTC) - timedelta(minutes=10)
        )

        session.refresh(row)
        assert recovered == 1
        assert row.status == "INTERRUPTED"
        assert row.current_stage == "INTERRUPTED"
        assert row.error_code == "RESEARCH_INTERRUPTED"
        assert row.checkpoint_json == {"coverage": {"read_pages": 2}}


def test_interrupted_research_resumes_from_checkpoint(session_factory) -> None:
    with session_factory() as session:
        goal, conversation = _goal_and_conversation(session)
        orchestrator = ResearchOrchestrator(
            service=FakeResearchService(),
            limits=ResearchLimits(max_rounds=2, max_read_pages=2),
            intent_analyzer=CompletingAnalyzer(),
        )
        checkpoint = orchestrator.research(
            "Authoritative result implementation", enabled=True, run_id="resume-run"
        )
        row = ResearchRun(
            id="resume-run",
            goal_id=goal.id,
            conversation_id=conversation.id,
            question="Authoritative result implementation",
            enabled=True,
            status="INTERRUPTED",
            current_stage="INTERRUPTED",
            resume_stage="CHECKPOINT",
            checkpoint_json=checkpoint.model_dump(mode="json"),
        )
        session.add(row)
        session.commit()

        result = ResearchRunService(session, orchestrator).resume(goal.id, row.id)

        assert result.status == "READY"
        assert result.current_stage == "COMPLETED"
        assert session.get(ResearchRun, row.id).resume_stage is None


def test_research_rejects_conversation_from_another_goal(session_factory) -> None:
    with session_factory() as session:
        first, _ = _goal_and_conversation(session)
        second, second_conversation = _goal_and_conversation(session)
        service = ResearchRunService(session)

        with pytest.raises(HTTPException) as caught:
            service.execute(first.id, "越权对话", conversation_id=second_conversation.id)

        assert caught.value.status_code == 404
        assert service.list(second.id) == []
