import json

from sqlalchemy import select

from planner_api.context_compiler import ContextCompiler, compile_user_text
from planner_api.conversation_memory import refresh_phase_summary
from planner_api.goal_error_memory import (
    explicit_error_memory_candidates,
    persist_explicit_error_memories,
)
from planner_api.models import Conversation, ConversationSummary, Goal, GoalErrorMemory, Message
from planner_api.schemas import ErrorAvoidanceMemoryCandidate


def test_long_user_input_is_chunked_and_keeps_critical_middle_evidence() -> None:
    text = "开头背景。\n\n" + ("一般说明。" * 3000)
    text += "\n\n硬约束：截止日期是 2026-12-31，不能修改。\n\n"
    text += ("后续说明。" * 3000) + "\n\n最终要求：优先保留已经完成的任务。"

    compiled, manifest = compile_user_text(text, 6000)

    assert len(compiled) <= 6000
    assert manifest["truncated"] is True
    assert manifest["chunk_count"] > 10
    assert "截止日期是 2026-12-31" in compiled
    assert "优先保留已经完成的任务" in compiled


def test_context_compiler_enforces_budget_and_scopes_large_plan() -> None:
    payload = {
        "user_request": "请调整第 42 周，其他周保持不变。" + ("补充背景" * 5000),
        "confirmed_requirements": {"deadline": "不能修改", "objective": "完成长期计划"},
        "current_relative_plan": {
            "days": [
                {
                    "week_index": week,
                    "day_index": day,
                    "primary_goal": f"第 {week} 周第 {day} 天" + ("详细任务" * 80),
                    "tasks": [
                        {"title": f"任务 {index}", "description": "说明" * 100}
                        for index in range(8)
                    ],
                }
                for week in range(1, 53)
                for day in range(1, 6)
            ]
        },
        "requirement_memory": {
            "recent_messages": [
                {"role": "USER", "content": f"历史消息 {index}" + ("内容" * 500)}
                for index in range(100)
            ]
        },
    }

    result = ContextCompiler(max_chars=16000, max_user_text_chars=4000).compile(
        payload, task_type="PLAN_ADJUSTMENT"
    )
    encoded = json.dumps(result.payload, ensure_ascii=False)
    weeks = {item["week_index"] for item in result.payload["current_relative_plan"]["days"]}

    # The input contains a complete 52-week task contract. The compiler may
    # exceed its soft provider budget, but it must report that explicitly
    # rather than deleting weeks or task details to make the JSON look small.
    assert result.manifest["budget_exceeded"] is True
    assert len(encoded) > 16000
    assert 42 in weeks
    assert weeks == set(range(1, 53))
    assert len(result.payload["current_relative_plan"]["days"][0]["tasks"]) == 8
    assert result.payload["current_relative_plan"]["days"][0]["tasks"][0]["description"]
    assert result.manifest["requested_weeks"] == [42]
    assert result.manifest["omissions"] == []


def test_context_compiler_expands_week_ranges_without_dropping_middle_scope() -> None:
    payload = {
        "user_request": "请把第1周到第35周全部核对，其他周保持原样。",
        "current_relative_plan": {
            "days": [
                {"week_index": week, "day_index": 1, "tasks": [{"id": f"w{week}"}]}
                for week in range(1, 41)
            ]
        },
    }
    result = ContextCompiler(max_chars=4000).compile(payload, task_type="PLAN_ADJUSTMENT")
    weeks = {row["week_index"] for row in result.payload["current_relative_plan"]["days"]}

    assert result.manifest["requested_weeks"] == list(range(1, 36))
    assert set(range(1, 36)).issubset(weeks)
    assert {row["tasks"][0]["id"] for row in result.payload["current_relative_plan"]["days"]}


def test_phase_summary_rolls_forward_without_sending_full_history(session_factory) -> None:
    with session_factory() as session:
        goal = Goal(title="长对话", raw_input="测试长对话")
        session.add(goal)
        session.flush()
        conversation = Conversation(goal_id=goal.id, phase="PLAN")
        session.add(conversation)
        session.flush()
        for sequence in range(1, 241):
            role = "USER" if sequence % 2 else "ASSISTANT"
            content = f"第 {sequence} 条普通讨论。" + ("补充内容" * 30)
            if sequence == 3:
                content = "关键约束：最终截止日期不能改变。"
            session.add(
                Message(
                    conversation_id=conversation.id,
                    sequence=sequence,
                    role=role,
                    content=content,
                )
            )
        session.flush()

        summary = refresh_phase_summary(session, conversation)
        session.commit()

        assert summary is not None
        assert summary.through_message_sequence == 240
        assert len(summary.summary) <= 6000
        assert "最终截止日期不能改变" in summary.summary
        stored = list(
            session.scalars(
                select(ConversationSummary).where(
                    ConversationSummary.conversation_id == conversation.id
                )
            )
        )
        assert len([item for item in stored if item.summary_level == "PHASE"]) == 1
        assert len([item for item in stored if item.summary_level == "SEGMENT"]) > 1


def test_phase_summary_keeps_middle_signal_without_compiler_header_pollution(
    session_factory,
) -> None:
    with session_factory() as session:
        goal = Goal(title="摘要头污染回归", raw_input="测试")
        session.add(goal)
        session.flush()
        conversation = Conversation(goal_id=goal.id, phase="PLAN")
        session.add(conversation)
        session.flush()
        for sequence in range(1, 601):
            marker = "用户纠错：不要连续安排三个高强度任务。" if sequence == 300 else ""
            session.add(
                Message(
                    conversation_id=conversation.id,
                    sequence=sequence,
                    role="USER" if sequence % 2 else "ASSISTANT",
                    content=marker + f"第 {sequence} 条一般讨论。" + "普通补充。" * 80,
                )
            )
        session.flush()

        summary = refresh_phase_summary(session, conversation)

        assert summary is not None
        assert "不要连续安排三个高强度任务" in summary.summary
        assert "长输入编译视图" not in summary.summary


def test_message_history_endpoint_is_cursor_paginated(client, session_factory) -> None:
    workspace = client.post("/api/goals", json={"raw_input": "分页测试"}).json()
    goal_id = workspace["goal_id"]
    with session_factory() as session:
        conversation = session.scalar(
            select(Conversation).where(
                Conversation.goal_id == goal_id,
                Conversation.phase == "CLARIFICATION",
            )
        )
        assert conversation is not None
        for sequence in range(3, 123):
            session.add(
                Message(
                    conversation_id=conversation.id,
                    sequence=sequence,
                    role="USER" if sequence % 2 else "ASSISTANT",
                    content=f"分页消息 {sequence}",
                )
            )
        session.commit()

    first = client.get(f"/api/goals/{goal_id}/conversations/clarification/messages?limit=20").json()
    second = client.get(
        f"/api/goals/{goal_id}/conversations/clarification/messages"
        f"?limit=20&before_sequence={first['next_before_sequence']}"
    ).json()

    assert first["total"] == 122
    assert len(first["items"]) == 20
    assert first["has_more"] is True
    assert second["items"][-1]["sequence"] < first["items"][0]["sequence"]


def test_error_memory_requires_literal_user_evidence(session_factory) -> None:
    with session_factory() as session:
        goal = Goal(title="证据校验", raw_input="测试")
        session.add(goal)
        session.flush()
        conversation = Conversation(goal_id=goal.id, phase="PLAN")
        session.add(conversation)
        session.flush()
        message = Message(
            conversation_id=conversation.id,
            sequence=1,
            role="USER",
            content="请把周三的任务移到周四。",
        )
        session.add(message)
        session.flush()

        stored = persist_explicit_error_memories(
            session,
            goal_id=goal.id,
            source_message_id=message.id,
            user_content=message.content,
            candidates=[
                ErrorAvoidanceMemoryCandidate(
                    rule="不要连续安排三个高强度任务。",
                    evidence_quote="这个安排强度太高",
                    scope="GOAL",
                )
            ],
        )

        assert stored == []
        assert session.scalar(select(GoalErrorMemory)) is None


def test_error_memory_auto_detector_requires_explicit_correction() -> None:
    assert explicit_error_memory_candidates("错误记忆功能要支持撤销，规则要留记录。") == []
    candidates = explicit_error_memory_candidates(
        "我从未说过功能太多怕做不完，后续不要再这样解释我的范围控制。"
    )
    assert len(candidates) == 1
    assert "从未说过" in candidates[0].evidence_quote


def test_explicit_error_memory_can_be_retracted(session_factory) -> None:
    with session_factory() as session:
        goal = Goal(title="撤销避错规则", raw_input="测试")
        session.add(goal)
        session.flush()
        conversation = Conversation(goal_id=goal.id, phase="PLAN")
        session.add(conversation)
        session.flush()
        first = Message(
            conversation_id=conversation.id,
            sequence=1,
            role="USER",
            content="以后不要连续安排高强度任务。",
        )
        session.add(first)
        session.flush()
        memory = persist_explicit_error_memories(
            session,
            goal_id=goal.id,
            source_message_id=first.id,
            user_content=first.content,
            candidates=[
                ErrorAvoidanceMemoryCandidate(
                    rule="不要连续安排高强度任务。",
                    evidence_quote=first.content,
                )
            ],
        )[0]
        retraction = Message(
            conversation_id=conversation.id,
            sequence=2,
            role="USER",
            content="之前那条不用了，可以连续安排。",
        )
        session.add(retraction)
        session.flush()

        persist_explicit_error_memories(
            session,
            goal_id=goal.id,
            source_message_id=retraction.id,
            user_content=retraction.content,
            candidates=[
                ErrorAvoidanceMemoryCandidate(
                    rule=memory.rule,
                    evidence_quote=retraction.content,
                    operation="RETRACT",
                    memory_id=memory.id,
                )
            ],
        )

        assert memory.status == "SUPERSEDED"
