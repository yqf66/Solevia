import json
from datetime import UTC, datetime

from planner_api.models import Conversation, Goal, ResearchRun
from planner_api.plan_ai import FakePlanProvider
from planner_api.plan_discussion_ai import (
    FakePlanDiscussionProvider,
    PlanDiscussionContext,
    _discussion_input,
)
from planner_api.plan_service import PlanService
from planner_api.research.schemas import ResearchPacket
from planner_api.schemas import PlanFeedback


def test_plan_feedback_research_is_explicit_and_off_by_default() -> None:
    assert PlanFeedback(content="解释一下").web_research_enabled is False
    assert PlanFeedback(content="查一下最新资料", web_research_enabled=True).web_research_enabled
    assert PlanFeedback(content="复用研究", research_run_id="run-1").research_run_id == "run-1"


def test_plan_discussion_can_reuse_a_completed_research_run(session_factory) -> None:
    with session_factory() as session:
        goal = Goal(title="复用研究", raw_input="使用研究结果讨论计划")
        session.add(goal)
        session.flush()
        conversation = Conversation(goal_id=goal.id, phase="PLAN")
        session.add(conversation)
        session.flush()
        run = ResearchRun(
            goal_id=goal.id,
            conversation_id=conversation.id,
            question="已完成研究",
            status="READY",
            current_stage="COMPLETED",
        )
        session.add(run)
        session.flush()
        run.result_json = ResearchPacket(
            run_id=run.id,
            question=run.question,
            status="READY",
        ).model_dump(mode="json")
        session.commit()

        service = PlanService(session, FakePlanProvider(), FakePlanDiscussionProvider())
        context, projection = service._prepare_plan_research(
            goal.id,
            conversation,
            "请基于已有研究解释安排",
            message_id="message-1",
            web_research_enabled=False,
            research_run_id=run.id,
        )

        assert projection is not None
        assert projection.id == run.id
        assert context is not None
        assert context["research_run_id"] == run.id


def test_plan_discussion_receives_bounded_research_projection() -> None:
    context = PlanDiscussionContext(
        requirements={},
        route={},
        current_plan={},
        user_input="结合最新资料调整计划",
        research_context={
            "research_run_id": "run-1",
            "research_status": "READY",
            "extracted_claims": [{"claim_id": "claim-1", "claim": "可核查结论"}],
            "evidence_passages": [{"passage_id": "passage-1", "text": "有限摘录"}],
            "untrusted_external_sources": [{"url": "https://example.com"}],
            "_context_manifest": {"max_chars": 12000},
        },
    )

    payload = json.loads(_discussion_input(context, "test-prompt", "user_input"))

    assert payload["research_context"]["research_run_id"] == "run-1"
    assert "passages" not in payload["research_context"]
    assert payload["research_context"]["_context_manifest"]["max_chars"] == 12000


def test_research_run_read_projection_is_bounded() -> None:
    # This guards the workspace projection contract independently of a live
    # provider: raw evidence must not be exposed without explicit truncation.
    row = ResearchRun(
        id="run-1",
        goal_id="goal-1",
        question="q",
        status="PARTIAL",
        current_stage="COMPLETED",
        result_json={
            "status": "PARTIAL",
            "sources": [{"title": "x" * 500, "url": "https://example.com"}],
            "coverage": {"read_pages": 2},
            "uncertain_points": ["u" * 500],
        },
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    projected = PlanService._research_read(row)
    assert len(projected.sources[0]["title"]) == 160
    assert len(projected.unresolved_points[0]) == 300
    assert projected.read_pages == 2
