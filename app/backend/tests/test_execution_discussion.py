from datetime import timedelta

from clock_helpers import shanghai_today

from sqlalchemy import select

from planner_api.ai import ProviderUnavailableError
from planner_api.execution_discussion_service import ExecutionDiscussionService
from planner_api.models import (
    Conversation,
    ExecutionInsight,
    ExecutionReview,
    GoalErrorMemory,
    Message,
    UserMemoryItem,
    utc_now,
)
from planner_api.plan_discussion_ai import FakePlanDiscussionProvider
from planner_api.planning_context import build_requirement_memory
from planner_api.schemas import ExecutionReviewSignalRead


def _started_goal(client) -> str:
    from test_plan import _completed_plan

    workspace = _completed_plan(client)
    goal_id = workspace["goal_id"]
    assert client.post(f"/api/goals/{goal_id}/plan/confirm").status_code == 200
    today = shanghai_today()
    started = client.post(
        f"/api/goals/{goal_id}/execution/start",
        json={
            "requested_start_date": today.isoformat(),
            "execution_weekdays": sorted({today.weekday(), (today.weekday() + 2) % 7}),
            "excluded_dates": [],
        },
    )
    assert started.status_code == 200
    return goal_id


def test_execution_discussion_handles_multiple_intents_and_automatic_memory(
    client, session_factory
) -> None:
    goal_id = _started_goal(client)

    response = client.post(
        f"/api/goals/{goal_id}/execution/discussion/messages",
        json={"content": "最近加班有点累，我发现自己更喜欢做项目，下周能不能轻一点？"},
    )

    assert response.status_code == 200
    workspace = response.json()
    intent_types = {item["type"] for item in workspace["latest_analysis"]["intents"]}
    assert {"CONSTRAINT_CHANGE", "INSIGHT", "DIFFICULTY", "PLAN_CHANGE_REQUEST"} <= intent_types
    assert workspace["latest_analysis"]["adjustment_signal"]["recommended"] is True
    assert len(workspace["insights"]) == 2
    assert all(item["status"] == "CONFIRMED" for item in workspace["insights"])

    with session_factory() as session:
        saved = list(
            session.scalars(select(ExecutionInsight).where(ExecutionInsight.goal_id == goal_id))
        )
        assert len(saved) == 2
        assert all(item.status == "CONFIRMED" for item in saved)


def test_execution_discussion_can_record_without_recommending_a_change(client) -> None:
    goal_id = _started_goal(client)

    response = client.post(
        f"/api/goals/{goal_id}/execution/discussion/messages",
        json={"content": "我发现自己更喜欢做项目，先记下来，不要改计划。"},
    )

    assert response.status_code == 200
    workspace = response.json()
    assert workspace["insights"][0]["status"] == "CONFIRMED"
    assert workspace["latest_analysis"]["adjustment_signal"]["recommended"] is False


def test_execution_criticism_is_available_to_later_generation(client, session_factory) -> None:
    goal_id = _started_goal(client)
    content = "这个排布不合理，以后不要把高强度任务连续安排。"

    response = client.post(
        f"/api/goals/{goal_id}/execution/discussion/messages",
        json={"content": content},
    )

    assert response.status_code == 200
    with session_factory() as session:
        stored = session.scalar(select(GoalErrorMemory).where(GoalErrorMemory.goal_id == goal_id))
        assert stored is not None
        assert stored.evidence == content
        memory = build_requirement_memory(session, goal_id)
        assert memory["error_avoidance_memory"][0]["rule"] == stored.rule


def test_user_can_start_a_review_from_natural_discussion(client) -> None:
    goal_id = _started_goal(client)

    response = client.post(
        f"/api/goals/{goal_id}/execution/discussion/messages",
        json={"content": "帮我复盘一下最近的情况，再讨论下一阶段。"},
    )

    assert response.status_code == 200
    workspace = response.json()
    assert workspace["active_review"]["status"] == "DISCUSSING"
    assert workspace["active_review"]["trigger_type"] == "USER_REQUEST"


def test_rhythm_progress_reports_frequency_instead_of_deadline(client) -> None:
    goal_id = _started_goal(client)

    response = client.get(f"/api/goals/{goal_id}/execution/progress")

    assert response.status_code == 200
    progress = response.json()
    assert progress["schedule_type"] == "RHYTHM"
    assert progress["rhythm"]["target_sessions"] == 2
    assert progress["rhythm"]["remaining_sessions"] == 2
    assert "截止" not in progress["rhythm"]["forecast_summary"]


def test_model_failure_keeps_execution_message_for_retry(session_factory, client) -> None:
    goal_id = _started_goal(client)

    class FailingProvider(FakePlanDiscussionProvider):
        def discuss_execution(self, context):
            raise ProviderUnavailableError(
                "这次回复失败了，但内容已经保存。",
                code="MODEL_TIMEOUT",
            )

    with session_factory() as session:
        workspace = ExecutionDiscussionService(session, FailingProvider()).discuss(
            goal_id, "这是一次不应该丢失的心得"
        )

    assert workspace.messages[-2].role == "USER"
    assert workspace.messages[-2].content == "这是一次不应该丢失的心得"
    assert workspace.messages[-1].generation_status == "FAILED"
    assert workspace.messages[-1].metadata["retryable"] is True
    with session_factory() as session:
        saved = session.scalar(select(Message).where(Message.id == workspace.messages[-2].id))
        assert saved is not None
        assert saved.content == "这是一次不应该丢失的心得"


def test_retry_reuses_saved_user_message_instead_of_duplicating_it(session_factory, client) -> None:
    goal_id = _started_goal(client)

    class FailingProvider(FakePlanDiscussionProvider):
        def discuss_execution(self, context):
            raise ProviderUnavailableError("暂时失败", code="MODEL_TIMEOUT")

    with session_factory() as session:
        ExecutionDiscussionService(session, FailingProvider()).discuss(
            goal_id, "我喜欢先实践再看理论"
        )
    with session_factory() as session:
        workspace = ExecutionDiscussionService(session, FakePlanDiscussionProvider()).retry_last(
            goal_id
        )

    user_messages = [item for item in workspace.messages if item.role == "USER"]
    assert len(user_messages) == 1
    assert user_messages[0].content == "我喜欢先实践再看理论"
    assert workspace.messages[-1].role == "ASSISTANT"
    assert workspace.messages[-1].generation_status == "SUCCEEDED"


def test_retry_regenerates_when_user_turn_has_no_assistant_row(session_factory, client) -> None:
    goal_id = _started_goal(client)
    with session_factory() as session:
        service = ExecutionDiscussionService(session, FakePlanDiscussionProvider())
        conversation = service._ensure_conversation(goal_id)
        user_message = Message(
            conversation_id=conversation.id,
            sequence=1,
            role="USER",
            content="RSI 和 JEV 需要掌握吗？",
            metadata_json={"intent": "EXECUTION_DISCUSSION"},
        )
        session.add(user_message)
        session.commit()
        workspace = service.retry_last(goal_id)

    user_messages = [item for item in workspace.messages if item.role == "USER"]
    assistant_messages = [item for item in workspace.messages if item.role == "ASSISTANT"]
    assert len(user_messages) == 1
    assert len(assistant_messages) == 1
    assert assistant_messages[0].generation_status == "SUCCEEDED"


def test_generation_turn_is_persisted_before_provider_call(session_factory, client) -> None:
    goal_id = _started_goal(client)

    class InspectingProvider(FakePlanDiscussionProvider):
        def __init__(self, session_factory):
            self.session_factory = session_factory
            self.observed = None

        def discuss_execution(self, context):
            with self.session_factory() as session:
                conversation = session.scalar(
                    select(Conversation).where(
                        Conversation.goal_id == goal_id,
                        Conversation.phase == "EXECUTION",
                    )
                )
                self.observed = [
                    (item.role, item.generation_status)
                    for item in session.scalars(
                        select(Message)
                        .where(Message.conversation_id == conversation.id)
                        .order_by(Message.sequence)
                    )
                ]
            return super().discuss_execution(context)

    provider = InspectingProvider(session_factory)
    with session_factory() as session:
        workspace = ExecutionDiscussionService(session, provider).discuss(goal_id, "生成前先落库")

    assert provider.observed == [("USER", "SUCCEEDED"), ("ASSISTANT", "GENERATING")]
    assert workspace.messages[-1].generation_status == "SUCCEEDED"


def test_stale_generation_is_recovered_as_retryable_failure(session_factory, client) -> None:
    goal_id = _started_goal(client)
    with session_factory() as session:
        service = ExecutionDiscussionService(session, FakePlanDiscussionProvider())
        conversation = service._ensure_conversation(goal_id)
        session.add(
            Message(
                conversation_id=conversation.id,
                sequence=1,
                role="USER",
                content="之前的问题",
                metadata_json={"intent": "EXECUTION_DISCUSSION"},
            )
        )
        pending = Message(
            conversation_id=conversation.id,
            sequence=2,
            role="ASSISTANT",
            content="",
            generation_status="GENERATING",
            metadata_json={"intent": "EXECUTION_DISCUSSION"},
            created_at=utc_now() - timedelta(seconds=400),
        )
        session.add(pending)
        session.commit()
        workspace = service.get_workspace(goal_id)

    assert workspace.messages[-1].generation_status == "FAILED"
    assert "超时" in workspace.messages[-1].content


def test_execution_discussion_inherits_clarification_history(session_factory, client) -> None:
    goal_id = _started_goal(client)

    class CapturingProvider(FakePlanDiscussionProvider):
        context = None

        def discuss_execution(self, context):
            self.context = context
            return super().discuss_execution(context)

    provider = CapturingProvider()
    with session_factory() as session:
        ExecutionDiscussionService(session, provider).discuss(goal_id, "接着之前的路线聊聊")

    assert provider.context is not None
    inherited = provider.context.planning_context["clarification_memory"]
    assert inherited["summary"]
    assert inherited["messages"]
    assert inherited["messages"][0]["role"] == "USER"


def test_workspace_refresh_does_not_fail_a_recent_retry_of_an_old_message(session_factory, client):
    goal_id = _started_goal(client)
    with session_factory() as session:
        service = ExecutionDiscussionService(session, FakePlanDiscussionProvider())
        conversation = service._ensure_conversation(goal_id)
        pending = Message(conversation_id=conversation.id, sequence=1, role="ASSISTANT",
            content="", generation_status="GENERATING", created_at=utc_now() - timedelta(days=1),
            metadata_json={"intent": "EXECUTION_DISCUSSION",
                           "generation_started_at": utc_now().isoformat()})
        session.add(pending)
        session.commit()
        assert service.get_workspace(goal_id).messages[-1].generation_status == "GENERATING"


def test_memory_with_same_semantic_key_supersedes_previous_value(session_factory, client) -> None:
    goal_id = _started_goal(client)
    with session_factory() as session:
        service = ExecutionDiscussionService(session, FakePlanDiscussionProvider())
        service.discuss(goal_id, "我发现自己更喜欢先实践")
        workspace = service.discuss(goal_id, "我发现自己更喜欢先拍摄再复盘")

    assert len(workspace.insights) == 1
    assert workspace.insights[0].content == "我发现自己更喜欢先拍摄再复盘"
    with session_factory() as session:
        records = list(
            session.scalars(
                select(ExecutionInsight)
                .where(ExecutionInsight.goal_id == goal_id)
                .order_by(ExecutionInsight.created_at)
            )
        )
    assert [item.status for item in records] == ["SUPERSEDED", "CONFIRMED"]


def test_inferred_execution_memory_waits_for_user_confirmation(session_factory, client) -> None:
    goal_id = _started_goal(client)

    class InferenceProvider(FakePlanDiscussionProvider):
        def discuss_execution(self, context):
            result = super().discuss_execution(context)
            inferred = [
                item.model_copy(
                    update={
                        "source_type": "AGENT_INFERENCE",
                        "evidence_span": None,
                        "confirmation_policy": "USER_REVIEW",
                    }
                )
                for item in result.memory_candidates
            ]
            return result.model_copy(update={"memory_candidates": inferred})

    with session_factory() as session:
        workspace = ExecutionDiscussionService(session, InferenceProvider()).discuss(
            goal_id, "我发现自己更喜欢先实践"
        )

    assert workspace.insights[0].status == "PROPOSED"
    assert workspace.insights[0].source_type == "AGENT_INFERENCE"


def test_confirmed_user_scope_memory_is_available_across_goals(session_factory, client) -> None:
    goal_id = _started_goal(client)

    class UserMemoryProvider(FakePlanDiscussionProvider):
        def discuss_execution(self, context):
            result = super().discuss_execution(context)
            memories = [
                item.model_copy(
                    update={
                        "scope": "USER",
                        "source_type": "AGENT_INFERENCE",
                        "evidence_span": None,
                        "confirmation_policy": "USER_REVIEW",
                    }
                )
                for item in result.memory_candidates
            ]
            return result.model_copy(update={"memory_candidates": memories})

    with session_factory() as session:
        workspace = ExecutionDiscussionService(session, UserMemoryProvider()).discuss(
            goal_id, "我发现自己更喜欢先实践"
        )
    insight_id = workspace.insights[0].id

    confirmed = client.put(
        f"/api/goals/{goal_id}/execution/insights/{insight_id}",
        json={"action": "CONFIRM"},
    )

    assert confirmed.status_code == 200
    with session_factory() as session:
        stored = session.scalar(select(UserMemoryItem))
        assert stored is not None
        memory = build_requirement_memory(session, goal_id)
        assert memory["user_profile_memory"][0]["memory_key"] == stored.memory_key


def test_future_review_is_scheduled_without_showing_an_immediate_prompt(
    session_factory, client
) -> None:
    goal_id = _started_goal(client)
    future_date = shanghai_today() + timedelta(days=10)

    class FutureReviewProvider(FakePlanDiscussionProvider):
        def discuss_execution(self, context):
            result = super().discuss_execution(context)
            return result.model_copy(
                update={
                    "review_signal": ExecutionReviewSignalRead(
                        recommended=True,
                        trigger_type="OUTCOME_CHECKPOINT",
                        reason="到约定日期再检查阶段成果",
                        timing="FUTURE",
                        not_before=future_date,
                    )
                }
            )

    with session_factory() as session:
        workspace = ExecutionDiscussionService(session, FutureReviewProvider()).discuss(
            goal_id, "十天后再看效果"
        )
    assert workspace.active_review is None
    with session_factory() as session:
        review = session.scalar(select(ExecutionReview).where(ExecutionReview.goal_id == goal_id))
    assert review is not None
    assert review.status == "SCHEDULED"
    assert review.not_before == future_date
