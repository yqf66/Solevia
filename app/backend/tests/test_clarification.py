from fastapi.testclient import TestClient
from fastapi import BackgroundTasks
from datetime import timedelta
from sqlalchemy import select

from planner_api.ai import (
    ClarificationContext,
    FakeClarificationProvider,
    ProviderUnavailableError,
)
from planner_api.models import (
    ClarificationAnalysisJob,
    Conversation,
    ConversationSummary,
    Goal,
    GoalErrorMemory,
    Message,
    RequirementChange,
    RequirementFact,
    RequirementVersion,
    utc_now,
)
from planner_api.scheduling_approach import recommend_schedule_type
from planner_api.schemas import ClarificationResult, ProposedFact, ReadinessResult
from planner_api.service import ClarificationService


def test_analysis_retry_reclaims_expired_processing_but_keeps_live_request(session_factory):
    from planner_api.api import retry_analysis

    with session_factory() as session:
        goal = Goal(title="合成分析恢复", raw_input="合成需求")
        session.add(goal)
        session.flush()
        conversation = Conversation(goal_id=goal.id, phase="CLARIFICATION")
        session.add(conversation)
        session.flush()
        user = Message(conversation_id=conversation.id, sequence=1, role="USER", content="继续")
        assistant = Message(conversation_id=conversation.id, sequence=2, role="ASSISTANT", content="已保存")
        session.add_all([user, assistant])
        session.flush()
        old = ClarificationAnalysisJob(goal_id=goal.id, conversation_id=conversation.id,
                                       user_message_id=user.id, assistant_message_id=assistant.id,
                                       status="PROCESSING", attempts=4,
                                       updated_at=utc_now()-timedelta(minutes=20))
        fresh = ClarificationAnalysisJob(goal_id=goal.id, conversation_id=conversation.id,
                                         user_message_id=user.id, assistant_message_id=assistant.id,
                                         status="PROCESSING", attempts=2, updated_at=utc_now())
        session.add_all([old, fresh])
        session.commit()
        retry_analysis(goal.id, BackgroundTasks(), ClarificationService(session, FakeClarificationProvider()))
        assert old.status == "PENDING" and old.attempts == 0
        assert fresh.status == "PROCESSING" and fresh.attempts == 2


def test_explicit_observation_window_overrides_longer_direction():
    from planner_api.planning_approach import recommend_planning_approach

    advice = recommend_planning_approach("手机摄影探索", {
        "constraints": "先探索四周的方向，但只细化第一个两周观察窗口",
    })
    assert advice.explicit_trial_weeks == 2


def create_goal(client: TestClient) -> dict:
    response = client.post(
        "/api/goals",
        json={"raw_input": "我想系统学习 Agent 和大模型应用开发，准备找相关实习。"},
    )
    assert response.status_code == 201
    return response.json()


def answer_three_rounds(client: TestClient, workspace: dict) -> dict:
    goal_id = workspace["goal_id"]
    answers = [
        "会 Python 基础语法，但没有完整项目经验；希望完成一个可展示项目。",
        "每周大约 10 到 15 小时，希望两个月后开始投递。",
        "能够独立演示项目、解释关键设计并完成相关面试题。",
    ]
    for answer in answers:
        response = client.post(f"/api/goals/{goal_id}/messages", json={"content": answer})
        assert response.status_code == 200
        workspace = response.json()
    assert workspace["readiness"]["ready_for_review"] is True
    response = client.post(f"/api/goals/{goal_id}/requirements/draft")
    assert response.status_code == 200
    return response.json()


def test_three_rounds_produce_reviewable_requirement(client: TestClient) -> None:
    workspace = answer_three_rounds(client, create_goal(client))

    assert workspace["status"] == "REQUIREMENT_REVIEW"
    assert workspace["readiness"]["ready_for_review"] is True
    assert len(workspace["facts"]) == 6
    assert workspace["requirement_draft"]["status"] == "DRAFT"
    assert len(workspace["messages"]) == 8


def test_discussion_memory_produces_a_visible_exploratory_recommendation(
    client: TestClient,
) -> None:
    response = client.post(
        "/api/goals",
        json={"raw_input": "我只是对摄影有点兴趣，想先试试看，看看实际效果再决定。"},
    )

    assert response.status_code == 201
    advice = response.json()["planning_approach_recommendation"]
    assert advice["recommended_mode"] == "EXPLORATORY"
    assert advice["reasons"]


def test_schedule_type_fallback_distinguishes_outcome_rhythm_and_calendar() -> None:
    outcome = recommend_schedule_type(
        "我对摄影有点兴趣，想慢慢拍出一组自己满意的作品",
        {"objective": "学习摄影", "deadline": None},
    )
    rhythm = recommend_schedule_type(
        "我想养成习惯，每周练习三次",
        {"objective": "练习速写", "deadline": None},
    )
    calendar = recommend_schedule_type(
        "准备作品集",
        {"objective": "完成作品集", "deadline": "2026-12-01"},
    )

    assert outcome.recommended_type == "OUTCOME"
    assert rhythm.recommended_type == "RHYTHM"
    assert calendar.recommended_type == "CALENDAR"


def test_confirmed_deadline_overrides_model_rhythm_recommendation(
    client: TestClient, session_factory
) -> None:
    created = client.post(
        "/api/goals",
        json={"raw_input": "我想每天做一套题，准备北京定选考试"},
    ).json()
    goal_id = created["goal_id"]
    response = client.post(
        f"/api/goals/{goal_id}/messages",
        json={"content": "考试日期是10月17日，必须在这之前完成。"},
    )
    assert response.status_code == 200

    with session_factory() as session:
        conversation = session.scalar(
            select(Conversation).where(
                Conversation.goal_id == goal_id,
                Conversation.phase == "CLARIFICATION",
            )
        )
        assert conversation is not None
        assistant = session.scalar(
            select(Message)
            .where(
                Message.conversation_id == conversation.id,
                Message.role == "ASSISTANT",
            )
            .order_by(Message.sequence.desc())
        )
        assert assistant is not None
        metadata = dict(assistant.metadata_json)
        metadata["schedule_type_recommendation"] = {
            "recommended_type": "RHYTHM",
            "reasons": ["用户提到了每天练习"],
            "confidence": "HIGH",
        }
        assistant.metadata_json = metadata
        session.commit()

    workspace = client.get(f"/api/goals/{goal_id}/clarification").json()
    advice = workspace["schedule_type_recommendation"]
    assert advice["recommended_type"] == "CALENDAR"
    assert advice["confidence"] == "HIGH"


def test_date_words_without_a_hard_deadline_do_not_force_calendar(
    client: TestClient, session_factory
) -> None:
    created = client.post(
        "/api/goals",
        json={"raw_input": "我想慢慢学习摄影，拍出满意的人像作品"},
    ).json()
    goal_id = created["goal_id"]
    with session_factory() as session:
        conversation = session.scalar(
            select(Conversation).where(
                Conversation.goal_id == goal_id,
                Conversation.phase == "CLARIFICATION",
            )
        )
        assistant = session.scalar(
            select(Message)
            .where(Message.conversation_id == conversation.id, Message.role == "ASSISTANT")
            .order_by(Message.sequence.desc())
        )
        metadata = dict(assistant.metadata_json)
        metadata["schedule_type_recommendation"] = {
            "recommended_type": "CALENDAR",
            "reasons": ["用户提到国庆后再看成果"],
            "confidence": "MEDIUM",
            "temporal_anchors": [
                {
                    "label": "国庆后复盘",
                    "kind": "REVIEW_CHECKPOINT",
                    "binding": False,
                    "reason": "用于观察成果，不是错过即失败的期限",
                }
            ],
        }
        assistant.metadata_json = metadata
        session.commit()

    workspace = client.get(f"/api/goals/{goal_id}/clarification").json()
    assert workspace["schedule_type_recommendation"]["recommended_type"] == "OUTCOME"


def test_schedule_type_is_recommended_but_remains_user_selectable(client: TestClient) -> None:
    response = client.post(
        "/api/goals",
        json={"raw_input": "我对摄影感兴趣，想先试试能不能拍出满意的作品。"},
    )
    assert response.status_code == 201
    workspace = response.json()
    assert workspace["schedule_type_recommendation"]["recommended_type"] == "OUTCOME"
    assert workspace["selected_schedule_type"] is None

    selected = client.put(
        f"/api/goals/{workspace['goal_id']}/schedule-type",
        json={"schedule_type": "RHYTHM"},
    )

    assert selected.status_code == 200
    assert selected.json()["selected_schedule_type"] == "RHYTHM"


def test_first_reply_analyzes_goal_before_asking_questions(client: TestClient) -> None:
    workspace = create_goal(client)
    first_reply = workspace["messages"][1]["content"]

    assert "初步理解" in first_reply
    assert "工程基础" in first_reply
    assert first_reply.index("初步理解") < first_reply.index("1.")


def test_deferred_goal_creation_persists_before_generating_first_reply(
    client: TestClient,
) -> None:
    initialized = client.post(
        "/api/goals/initialize",
        json={"raw_input": "我想学习 Agent 开发。", "timezone": "Asia/Shanghai"},
    )

    assert initialized.status_code == 201
    workspace = initialized.json()
    assert [message["role"] for message in workspace["messages"]] == ["USER", "ASSISTANT"]
    assert workspace["messages"][-1]["generation_status"] == "PENDING"
    assert workspace["messages"][-1]["metadata"]["initial_response"] is True

    completed = client.post(f"/api/goals/{workspace['goal_id']}/clarification/start")

    assert completed.status_code == 200
    completed_workspace = completed.json()
    assert completed_workspace["messages"][-1]["generation_status"] == "SUCCEEDED"
    assert "初步理解" in completed_workspace["messages"][-1]["content"]


def test_assistant_message_records_prompt_version(client: TestClient, session_factory) -> None:
    workspace = create_goal(client)
    with session_factory() as session:
        message = session.scalar(
            select(Message).where(
                Message.id == workspace["messages"][1]["id"],
            )
        )

    assert message is not None
    assert message.metadata_json["prompt_version"] == "clarification-v5-natural-memory"


def test_model_cannot_claim_review_ready_while_structured_memory_is_incomplete(
    session_factory,
) -> None:
    class PrematureReadyProvider:
        def clarify(self, _context: ClarificationContext) -> ClarificationResult:
            return ClarificationResult(
                response_type="REVIEW_READY",
                assistant_message="所有需求都已经充足，可以进入下一步。",
                proposed_facts=[],
                conflicts=[],
                questions=[],
                question_keys=[],
                open_questions=[],
                readiness=ReadinessResult(
                    ready_for_review=True,
                    blocking_fields=[],
                    reason="模型认为已经齐全",
                ),
            )

    with session_factory() as session:
        workspace = ClarificationService(session, PrematureReadyProvider()).create_goal(
            "我想准备后端开发面试"
        )

    assert workspace.status == "CLARIFYING"
    assert workspace.readiness.ready_for_review is False
    assert workspace.requirement_draft is None
    assert workspace.open_questions
    assert workspace.messages[-1].content
    assert workspace.messages[-1].metadata["server_ready_for_review"] is True


def test_confirmed_requirement_is_persisted_and_immutable(
    client: TestClient, session_factory
) -> None:
    workspace = answer_three_rounds(client, create_goal(client))
    goal_id = workspace["goal_id"]
    assert (
        client.put(
            f"/api/goals/{goal_id}/planning-approach",
            json={"planning_mode": "COMMITTED"},
        ).status_code
        == 200
    )
    assert (
        client.put(
            f"/api/goals/{goal_id}/schedule-type",
            json={"schedule_type": "RHYTHM"},
        ).status_code
        == 200
    )

    response = client.post(f"/api/goals/{goal_id}/requirements/confirm")

    assert response.status_code == 200
    confirmed = response.json()
    confirmed_snapshot = confirmed["requirement_draft"]["snapshot"]
    assert confirmed["status"] == "ROUTE_DRAFTING"
    assert confirmed["requirement_draft"]["status"] == "CONFIRMED"
    with session_factory() as session:
        version = session.scalar(
            select(RequirementVersion).where(RequirementVersion.goal_id == goal_id)
        )
        assert version is not None
        assert version.status == "CONFIRMED"
        assert version.confirmed_at is not None

    fact_id = confirmed["facts"][0]["id"]
    edit_response = client.patch(
        f"/api/goals/{goal_id}/facts/{fact_id}",
        json={"action": "REPLACE", "value": "试图覆盖已确认需求"},
    )
    assert edit_response.status_code == 200
    edited = edit_response.json()
    assert edited["requirement_draft"]["version"] == 2
    assert edited["requirement_draft"]["status"] == "DRAFT"
    with session_factory() as session:
        versions = list(
            session.scalars(
                select(RequirementVersion)
                .where(RequirementVersion.goal_id == goal_id)
                .order_by(RequirementVersion.version)
            )
        )
    assert versions[0].status == "CONFIRMED"
    assert versions[0].snapshot_json == confirmed_snapshot


def test_replacing_fact_preserves_correction_chain(client: TestClient, session_factory) -> None:
    workspace = create_goal(client)
    goal_id = workspace["goal_id"]
    objective = next(fact for fact in workspace["facts"] if fact["field_key"] == "objective")

    response = client.patch(
        f"/api/goals/{goal_id}/facts/{objective['id']}",
        json={"action": "REPLACE", "value": "准备大模型应用开发实习"},
    )

    assert response.status_code == 200
    visible = response.json()["facts"]
    assert len(visible) == 1
    assert visible[0]["value"] == "准备大模型应用开发实习"
    with session_factory() as session:
        history = list(
            session.scalars(
                select(RequirementFact)
                .where(RequirementFact.goal_id == goal_id)
                .order_by(RequirementFact.created_at)
            )
        )
        assert [fact.status for fact in history] == ["SUPERSEDED", "CONFIRMED"]
        assert history[1].supersedes_fact_id == history[0].id


def test_editing_review_fact_regenerates_same_public_draft_version(client: TestClient) -> None:
    workspace = answer_three_rounds(client, create_goal(client))
    goal_id = workspace["goal_id"]
    assert (
        client.put(
            f"/api/goals/{goal_id}/planning-approach",
            json={"planning_mode": "COMMITTED"},
        ).status_code
        == 200
    )
    assert (
        client.put(
            f"/api/goals/{goal_id}/schedule-type",
            json={"schedule_type": "RHYTHM"},
        ).status_code
        == 200
    )
    current_state = next(
        fact for fact in workspace["facts"] if fact["field_key"] == "current_state"
    )

    response = client.patch(
        f"/api/goals/{goal_id}/facts/{current_state['id']}",
        json={"action": "REPLACE", "value": "会 Python，并做过一个小型 API 项目"},
    )

    assert response.status_code == 200
    updated = response.json()
    assert updated["status"] == "REQUIREMENT_REVIEW"
    assert updated["requirement_draft"]["version"] == 1
    assert updated["requirement_draft"]["snapshot"]["current_state"] == (
        "会 Python，并做过一个小型 API 项目"
    )

    desired_outcome = next(
        fact for fact in updated["facts"] if fact["field_key"] == "desired_outcomes"
    )
    second_response = client.patch(
        f"/api/goals/{goal_id}/facts/{desired_outcome['id']}",
        json={"action": "REPLACE", "value": "完成两个可展示项目"},
    )
    assert second_response.status_code == 200
    second_update = second_response.json()
    assert second_update["requirement_draft"]["version"] == 1
    assert second_update["requirement_draft"]["snapshot"]["desired_outcomes"] == (
        "完成两个可展示项目"
    )


def test_confirm_self_heals_a_missing_draft_when_core_facts_are_complete(
    client: TestClient, session_factory
) -> None:
    workspace = answer_three_rounds(client, create_goal(client))
    goal_id = workspace["goal_id"]
    assert (
        client.put(
            f"/api/goals/{goal_id}/planning-approach",
            json={"planning_mode": "COMMITTED"},
        ).status_code
        == 200
    )
    assert (
        client.put(
            f"/api/goals/{goal_id}/schedule-type",
            json={"schedule_type": "RHYTHM"},
        ).status_code
        == 200
    )
    with session_factory() as session:
        draft = session.scalar(
            select(RequirementVersion).where(
                RequirementVersion.goal_id == goal_id,
                RequirementVersion.status == "DRAFT",
            )
        )
        assert draft is not None
        draft.status = "SUPERSEDED"
        session.commit()

    response = client.post(f"/api/goals/{goal_id}/requirements/confirm")

    assert response.status_code == 200
    repaired = response.json()
    assert repaired["status"] == "ROUTE_DRAFTING"
    assert repaired["requirement_draft"]["status"] == "CONFIRMED"
    assert repaired["requirement_draft"]["version"] == 2


def test_refresh_restores_messages_and_facts(client: TestClient) -> None:
    workspace = create_goal(client)
    goal_id = workspace["goal_id"]
    client.post(
        f"/api/goals/{goal_id}/messages",
        json={"content": "会 Python 基础，希望做一个可展示项目。"},
    )

    restored = client.get(f"/api/goals/{goal_id}/clarification")

    assert restored.status_code == 200
    assert len(restored.json()["messages"]) == 4
    assert len(restored.json()["facts"]) == 3


def test_detailed_goal_is_accepted_and_preserved(client: TestClient) -> None:
    detailed_goal = "我已经有一份成熟路线，需要 Agent 负责监督执行。\n" + (
        "阶段目标、已有资源、任务依赖、时间约束和成功标准。" * 1000
    )

    response = client.post("/api/goals", json={"raw_input": detailed_goal})

    assert response.status_code == 201
    assert response.json()["raw_input"] == detailed_goal


def test_whitespace_only_goal_is_rejected(client: TestClient) -> None:
    response = client.post("/api/goals", json={"raw_input": "   \n  "})

    assert response.status_code == 422


def test_goal_list_is_ordered_by_recent_activity(client: TestClient) -> None:
    first = create_goal(client)
    second_response = client.post("/api/goals", json={"raw_input": "准备一次半程马拉松"})
    assert second_response.status_code == 201

    response = client.get("/api/goals")

    assert response.status_code == 200
    goals = response.json()
    assert [goal["id"] for goal in goals] == [second_response.json()["goal_id"], first["goal_id"]]


def test_model_failure_preserves_user_input_and_records_failed_reply(session_factory) -> None:
    class FailingProvider:
        def clarify(self, _context: ClarificationContext):
            raise ProviderUnavailableError("模型暂时不可用，你的输入已经保存。")

    with session_factory() as session:
        workspace = ClarificationService(session, FailingProvider()).create_goal(
            "这是即使模型失败也不能丢失的目标"
        )
        messages = list(session.scalars(select(Message).order_by(Message.sequence)))

    assert workspace.messages[0].content == "这是即使模型失败也不能丢失的目标"
    assert [message.generation_status for message in messages] == ["SUCCEEDED", "FAILED"]
    assert "输入已经保存" in messages[1].content


def test_retry_failed_clarification_reuses_original_exchange(session_factory) -> None:
    class FailOnceProvider:
        def __init__(self) -> None:
            self.calls = 0

        def clarify(self, context: ClarificationContext):
            self.calls += 1
            if self.calls == 1:
                raise ProviderUnavailableError(
                    "模型服务响应超时。你的输入已经保存，可以直接重试。",
                    code="MODEL_TIMEOUT",
                )
            return FakeClarificationProvider().clarify(context)

    with session_factory() as session:
        service = ClarificationService(session, FailOnceProvider())
        failed = service.create_goal("准备系统学习摄影")
        assert len(failed.messages) == 2
        assert failed.messages[-1].generation_status == "FAILED"

        retried = service.retry_last_failed_message(failed.goal_id)
        messages = list(session.scalars(select(Message).order_by(Message.sequence)))

    assert len(retried.messages) == 2
    assert len(messages) == 2
    assert retried.messages[-1].generation_status == "SUCCEEDED"
    assert retried.messages[-1].metadata["question_keys"]


def test_requirement_change_keeps_old_fact_and_records_reason(client, session_factory) -> None:
    workspace = answer_three_rounds(client, create_goal(client))
    goal_id = workspace["goal_id"]
    objective = next(fact for fact in workspace["facts"] if fact["field_key"] == "objective")

    response = client.patch(
        f"/api/goals/{goal_id}/facts/{objective['id']}",
        json={"action": "REPLACE", "value": "改成做一个阅读笔记工具"},
    )

    assert response.status_code == 200
    updated = response.json()
    assert (
        next(fact for fact in updated["facts"] if fact["field_key"] == "objective")["value"]
        == "改成做一个阅读笔记工具"
    )
    assert any(change["operation"] == "REPLACE" for change in updated["fact_changes"])
    with session_factory() as session:
        changes = list(
            session.scalars(select(RequirementChange).where(RequirementChange.goal_id == goal_id))
        )
        assert any(change.operation == "REPLACE" for change in changes)
        old = session.get(RequirementFact, objective["id"])
        assert old is not None
        assert old.status == "SUPERSEDED"


def test_proposed_memory_never_counts_as_confirmed(session_factory) -> None:
    class CandidateProvider:
        def respond(self, _context: ClarificationContext):
            from planner_api.schemas import ConversationReply

            return ConversationReply(assistant_message="我有一个可能的方向，先和你讨论一下。")

        def analyze(self, _context: ClarificationContext, _assistant_message: str):
            from planner_api.schemas import RequirementAnalysis

            return RequirementAnalysis(
                proposed_facts=[
                    {
                        "category": "OUTCOME",
                        "field_key": "desired_outcomes",
                        "value": "完成一个可展示的作品",
                        "source": "AGENT_INFERENCE",
                        "status": "PROPOSED",
                        "confidence": 0.6,
                        "operation": "REQUEST_CONFIRMATION",
                    }
                ],
                summary="用户可能希望完成一个可展示的作品。",
                current_topic="desired_outcomes",
                open_questions=["是否真的要把它做成公开展示的作品？"],
                readiness={
                    "ready_for_review": True,
                    "blocking_fields": [],
                    "reason": "模型推断已经足够",
                },
            )

    with session_factory() as session:
        workspace = ClarificationService(session, CandidateProvider()).create_goal(
            "我想做一个小工具"
        )

    assert workspace.readiness.ready_for_review is False
    assert any(fact.status == "PROPOSED" for fact in workspace.facts)


def test_analysis_failure_keeps_natural_reply(session_factory) -> None:
    class AnalysisFails:
        def respond(self, _context: ClarificationContext):
            from planner_api.schemas import ConversationReply

            return ConversationReply(
                assistant_message="这个方向很有意思，我们可以先聊聊你最想解决的问题。"
            )

        def analyze(self, _context: ClarificationContext, _assistant_message: str):
            raise ProviderUnavailableError("分析暂时不可用", code="ANALYSIS_TIMEOUT")

    with session_factory() as session:
        workspace = ClarificationService(session, AnalysisFails()).create_goal("我想研究阳台种植")
        assert workspace.messages[-1].content.startswith("这个方向很有意思")
        assert workspace.messages[-1].metadata["analysis_status"] == "FAILED"


def test_follow_up_analysis_receives_persistent_conversation_memory(session_factory) -> None:
    seen_contexts: list[ClarificationContext] = []

    class MemoryProvider:
        def respond(self, context: ClarificationContext):
            from planner_api.schemas import ConversationReply

            seen_contexts.append(context)
            return ConversationReply(assistant_message="我记得你刚刚说过这个方向，我们接着往下想。")

        def analyze(self, context: ClarificationContext, _assistant_message: str):
            from planner_api.schemas import RequirementAnalysis

            return RequirementAnalysis(
                summary=context.summary or "已经开始讨论目标。",
                current_topic="objective",
                open_questions=["还想解决哪个具体问题？"],
                readiness={
                    "ready_for_review": False,
                    "blocking_fields": ["desired_outcomes"],
                    "reason": "成果方向仍在讨论。",
                },
            )

    with session_factory() as session:
        service = ClarificationService(session, MemoryProvider())
        workspace = service.create_goal("我想做一个能整理资料的小工具")
        service.add_user_message(workspace.goal_id, "我更在意它能不能帮我快速回顾")

    assert len(seen_contexts) == 2
    follow_up = seen_contexts[-1]
    assert follow_up.latest_user_message == "我更在意它能不能帮我快速回顾"
    assert follow_up.recent_messages
    assert any(item["role"] == "ASSISTANT" for item in follow_up.recent_messages or [])
    assert follow_up.summary


def test_failed_analysis_is_persisted_and_retried_until_success(session_factory) -> None:
    class RetryProvider:
        def __init__(self) -> None:
            self.analysis_calls = 0

        def respond(self, _context: ClarificationContext):
            from planner_api.schemas import ConversationReply

            return ConversationReply(assistant_message="我们先把这个想法聊清楚。")

        def analyze(self, _context: ClarificationContext, _assistant_message: str):
            from planner_api.schemas import RequirementAnalysis

            self.analysis_calls += 1
            if self.analysis_calls <= 2:
                raise ProviderUnavailableError("暂时超时", code="MODEL_TIMEOUT")
            return RequirementAnalysis(
                summary="用户想把一个想法聊清楚。",
                current_topic="objective",
                open_questions=["还可以继续补充想解决的问题。"],
                readiness={
                    "ready_for_review": False,
                    "blocking_fields": ["desired_outcomes"],
                    "reason": "成果仍待讨论。",
                },
            )

    provider = RetryProvider()
    with session_factory() as session:
        service = ClarificationService(session, provider)
        workspace = service.create_goal("我想做一个能帮助我整理想法的小工具")
        job = session.scalar(
            select(ClarificationAnalysisJob).where(
                ClarificationAnalysisJob.goal_id == workspace.goal_id
            )
        )
        assert job is not None
        assert job.status == "PENDING"
        worked, delay = service.process_pending_analysis()
        assert worked is True
        assert delay is not None
        job.next_retry_at = utc_now()
        session.commit()
        worked, delay = service.process_pending_analysis()
        assert worked is True
        assert delay is None
        session.refresh(job)
        assert job.status == "SUCCEEDED"
        assert job.attempts == 3


def test_additive_fact_updates_keep_capacity_from_different_periods(session_factory) -> None:
    with session_factory() as session:
        goal = Goal(title="容量记忆", raw_input="测试容量记忆")
        session.add(goal)
        session.flush()
        conversation = Conversation(goal_id=goal.id, phase="CLARIFICATION")
        session.add(conversation)
        session.flush()
        service = ClarificationService(session, FakeClarificationProvider())

        first = Message(
            conversation_id=conversation.id,
            sequence=1,
            role="USER",
            content="10月每周只能投入5小时。",
        )
        second = Message(
            conversation_id=conversation.id,
            sequence=2,
            role="USER",
            content="11月开始恢复到每周8小时。",
        )
        session.add_all([first, second])
        session.flush()
        service._apply_fact_candidate(
            goal.id,
            ProposedFact(
                category="CAPACITY",
                field_key="available_capacity",
                value="10月每周只能投入5小时",
                source="USER",
                status="CONFIRMED",
                confidence=1,
            ),
            first.id,
        )
        change = service._apply_fact_candidate(
            goal.id,
            ProposedFact(
                category="CAPACITY",
                field_key="available_capacity",
                value="11月开始恢复到每周8小时",
                source="USER",
                status="CONFIRMED",
                confidence=1,
                operation="REPLACE",
            ),
            second.id,
        )

        capacity = service._confirmed_fact_values(goal.id)["available_capacity"]
        active = list(
            session.scalars(
                select(RequirementFact).where(
                    RequirementFact.goal_id == goal.id,
                    RequirementFact.field_key == "available_capacity",
                    RequirementFact.status == "CONFIRMED",
                )
            )
        )

    assert "10月每周只能投入5小时" in capacity
    assert "11月开始恢复到每周8小时" in capacity
    assert change is not None and change["operation"] == "MERGE"
    assert len(active) == 1


def test_capacity_update_replaces_only_the_same_period(session_factory) -> None:
    with session_factory() as session:
        goal = Goal(title="容量修正", raw_input="测试容量修正")
        session.add(goal)
        session.flush()
        conversation = Conversation(goal_id=goal.id, phase="CLARIFICATION")
        session.add(conversation)
        session.flush()
        first = Message(
            conversation_id=conversation.id,
            sequence=1,
            role="USER",
            content="2027年1月每周8小时。",
        )
        second = Message(
            conversation_id=conversation.id,
            sequence=2,
            role="USER",
            content="2027年1月调整为每周6小时。",
        )
        session.add_all([first, second])
        session.flush()
        service = ClarificationService(session, FakeClarificationProvider())
        service._apply_fact_candidate(
            goal.id,
            ProposedFact(
                category="CAPACITY",
                field_key="available_capacity",
                value="2027年1月每周8小时",
                source="USER",
                status="CONFIRMED",
                confidence=1,
            ),
            first.id,
        )
        service._apply_fact_candidate(
            goal.id,
            ProposedFact(
                category="CAPACITY",
                field_key="available_capacity",
                value="2027年1月调整为每周6小时",
                source="USER",
                status="CONFIRMED",
                confidence=1,
                operation="REPLACE",
            ),
            second.id,
        )

        capacity = service._confirmed_fact_values(goal.id)["available_capacity"]

    assert "2027年1月调整为每周6小时" in capacity
    assert "2027年1月每周8小时" not in capacity


def test_scope_correction_does_not_clear_independent_course_constraint(session_factory) -> None:
    with session_factory() as session:
        goal = Goal(title="范围改口", raw_input="测试范围与课程保护")
        session.add(goal)
        session.flush()
        conversation = Conversation(goal_id=goal.id, phase="CLARIFICATION")
        session.add(conversation)
        session.flush()
        message = Message(conversation_id=conversation.id, sequence=1, role="USER",
                          content="只做本地搜索，以刚才的范围为准。资料不用真实个人笔记，只用合成资料。")
        old = RequirementFact(goal_id=goal.id, field_key="constraints", category="CONSTRAINT",
                              value_json="周二和周四19:00到20:00有课不能占用", source="USER", status="CONFIRMED")
        session.add_all([message, old])
        session.flush()
        service = ClarificationService(session, FakeClarificationProvider())
        change = service._apply_fact_candidate(goal.id, ProposedFact(
            category="CONSTRAINT", field_key="constraints", value="只用合成资料，不用真实个人笔记",
            source="USER", status="CONFIRMED", confidence=1, operation="REPLACE"), message.id)
        value = service._confirmed_fact_values(goal.id)["constraints"]
        assert "周二和周四" in value and "合成资料" in value
        assert change["operation"] == "MERGE"
        assert service._explicit_replacement_for_field("scope_in", message.content)
        assert service._explicit_replacement_for_field("constraints", "原规则作废，约束整体改为周二无课。")


def test_undated_capacity_correction_removes_old_quota_preserves_other_periods() -> None:
    value = ClarificationService._merge_fact_values(
        "暂时估计每周6小时；可用时间20:00到22:00；11月每周8小时",
        "每周只有3个晚上，每次60分钟，共3小时（不是6小时）",
        field_key="available_capacity", evidence_content="容量更正：每周只有3个晚上，每次60分钟，共3小时，不是6小时。",
    )
    assert "暂时估计每周6小时" not in value
    assert "20:00到22:00" in value and "11月每周8小时" in value and "共3小时" in value


def test_capacity_addition_without_correction_does_not_discard_existing_quota() -> None:
    value = ClarificationService._merge_fact_values(
        "每周6小时", "每次60分钟", field_key="available_capacity", evidence_content="每次60分钟。")
    assert "每周6小时" in value and "每次60分钟" in value


def test_quantity_correction_in_same_sentence_keeps_course_constraints() -> None:
    evidence = "保留原课程时间和每日自主学习上限，以刚才更正的4套为准。"
    assert not ClarificationService._explicit_replacement_for_field("constraints", evidence)
    assert not ClarificationService._explicit_replacement_for_field(
        "constraints", "不能覆盖原规则，以新训练数量为准。")
    assert ClarificationService._explicit_replacement_for_field(
        "constraints", "原规则作废，约束整体改为周二无课。")


def test_reordered_full_fact_merge_deduplicates_exact_clauses_only() -> None:
    value = ClarificationService._merge_fact_values(
        "只用合成资料；每天最多30分钟；不接入真实账号",
        "不接入真实账号；只用合成资料；每天最多30分钟；每周复盘一次",
    )
    assert value == "只用合成资料；每天最多30分钟；不接入真实账号；每周复盘一次"
    # Similar-looking but different numbers and negations must never be dropped.
    assert "每天最多20分钟" in ClarificationService._merge_fact_values(value, "每天最多20分钟")
    assert "允许真实账号" in ClarificationService._merge_fact_values(value, "允许真实账号")


def test_quota_change_log_cannot_replace_goal_purpose(session_factory) -> None:
    with session_factory() as session:
        goal = Goal(title="北京定选备考", raw_input="准备北京定向选调笔试")
        session.add(goal)
        session.flush()
        conversation = Conversation(goal_id=goal.id, phase="CLARIFICATION")
        session.add(conversation)
        session.flush()
        message = Message(conversation_id=conversation.id, sequence=1, role="USER",
                          content="整卷由6套改为4套，仍然准备北京定选。")
        old = RequirementFact(goal_id=goal.id, field_key="objective", category="OBJECTIVE",
                              value_json="备考北京定向选调笔试", source="USER", status="CONFIRMED")
        session.add_all([message, old])
        session.flush()
        service = ClarificationService(session, FakeClarificationProvider())
        service._apply_fact_candidate(goal.id, ProposedFact(
            category="OBJECTIVE", field_key="objective", value="整卷量由最初6套更正为总共4套",
            source="USER", status="CONFIRMED", confidence=1, operation="REPLACE"), message.id)
        assert service._confirmed_fact_values(goal.id)["objective"] == "备考北京定向选调笔试"
        message.content = "最终目标改为阳台绿植观察。"
        service._apply_fact_candidate(goal.id, ProposedFact(
            category="OBJECTIVE", field_key="objective", value="阳台绿植观察",
            source="USER", status="CONFIRMED", confidence=1, operation="REPLACE"), message.id)
        assert service._confirmed_fact_values(goal.id)["objective"] == "阳台绿植观察"


def test_turn_only_instruction_is_not_saved_as_long_term_preference(session_factory) -> None:
    with session_factory() as session:
        goal = Goal(title="临时指令", raw_input="测试临时指令")
        session.add(goal)
        session.flush()
        conversation = Conversation(goal_id=goal.id, phase="CLARIFICATION")
        session.add(conversation)
        session.flush()
        message = Message(
            conversation_id=conversation.id,
            sequence=1,
            role="USER",
            content="本轮只输出当前记忆校验结果，不要提出新建议。",
        )
        session.add(message)
        session.flush()
        service = ClarificationService(session, FakeClarificationProvider())

        change = service._apply_fact_candidate(
            goal.id,
            ProposedFact(
                category="PREFERENCE",
                field_key="preferences",
                value="本轮只输出当前记忆校验结果",
                source="USER",
                status="CONFIRMED",
                confidence=1,
            ),
            message.id,
        )

        stored = session.scalar(
            select(RequirementFact).where(RequirementFact.goal_id == goal.id)
        )

    assert change is None
    assert stored is None


def test_clarification_correction_becomes_error_memory_for_next_turn(session_factory) -> None:
    class CorrectionProvider:
        def respond(self, _context: ClarificationContext):
            from planner_api.schemas import ConversationReply

            return ConversationReply(assistant_message="收到，我会按你的纠正继续讨论。")

        def analyze(self, context: ClarificationContext, _assistant_message: str):
            from planner_api.schemas import RequirementAnalysis

            return RequirementAnalysis(
                summary=context.summary or "正在讨论。",
                readiness={
                    "ready_for_review": False,
                    "blocking_fields": ["desired_outcomes"],
                    "reason": "仍在讨论。",
                },
            )

    with session_factory() as session:
        service = ClarificationService(session, CorrectionProvider())
        workspace = service.create_goal("我想规划一个长期项目")
        service.add_user_message(
            workspace.goal_id,
            "你说我担心功能太多做不完，这是误记；我从未说过，后续不要再这样推断。",
        )
        memory = session.scalar(
            select(GoalErrorMemory).where(GoalErrorMemory.goal_id == workspace.goal_id)
        )
        goal = session.get(Goal, workspace.goal_id)
        conversation = session.scalar(
            select(Conversation).where(
                Conversation.goal_id == workspace.goal_id,
                Conversation.phase == "CLARIFICATION",
            )
        )
        assert goal is not None and conversation is not None
        next_context = service._build_context(
            goal,
            conversation,
            latest_user_message="继续",
        )
        latest_summary = session.scalar(
            select(ConversationSummary)
            .where(ConversationSummary.conversation_id == conversation.id)
            .order_by(ConversationSummary.created_at.desc())
        )

    assert memory is not None
    assert "误记" in memory.evidence
    assert next_context.error_avoidance_memory
    assert next_context.error_avoidance_memory[0]["rule"] == memory.rule
    assert latest_summary is not None
