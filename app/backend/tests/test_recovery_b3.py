from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from planner_api.models import (
    Conversation, Goal, Message, PlanDaySchedule, PlanTask, PlanVersion, PlanningBrief, ProgressEvent,
)
from planner_api.plan_service import PlanService, ProviderUnavailableError
from test_recovery_scope import preview_selection, scoped_recovery


def test_unassigned_recovery_proposal_explains_next_step_and_cannot_publish():
    proposal = PlanService._local_recovery_proposal(
        {
            "backlog_tasks": [{"task_id": "part-3", "title": "阅读报告第3部分"}],
            "unassigned_task_ids": ["part-3"],
        },
        assistant_message="已有两项安排，剩下一项。",
    )
    assert not proposal.can_apply
    assert proposal.needs_clarification
    assert "阅读报告第3部分" in proposal.clarifying_question
    assert "补充可用时间" in proposal.assistant_message
    assert "如果你确认" not in proposal.assistant_message


def split_preview(client, goal, preview_id, task_id, parts):
    return client.post(
        f"/api/goals/{goal}/execution/recovery/split-preview",
        json={"preview_id": preview_id, "source_task_id": task_id, "part_minutes": parts},
    )


def test_model_cannot_allocate_outside_recovery_window():
    context = {
        "backlog_tasks": [{"task_id": "part-1", "estimate_minutes": 40}],
        "allocation_candidate_days": [{"plan_day_id": "allowed"}],
        "candidate_days": [{"plan_day_id": "allowed"}, {"plan_day_id": "outside"}],
    }
    allocation = SimpleNamespace(
        assignments=[SimpleNamespace(backlog_task_id="part-1", target_plan_day_id="outside")],
        unassigned_task_ids=[],
    )
    with pytest.raises(ProviderUnavailableError):
        PlanService._normalize_ai_recovery_allocation(context, allocation)


def test_split_preview_and_apply_preserves_lineage_and_is_idempotent(client, session_factory):
    goal, plan, (first, second, _) = scoped_recovery(client, session_factory)
    preview = preview_selection(client, goal, plan, [first]).json()
    response = split_preview(client, goal, preview["id"], first, [5, 5])
    assert response.status_code == 200, response.text
    split = response.json()
    assert split["original_minutes"] == 10
    assert split["part_minutes"] == [5, 5]
    applied = client.post(
        f"/api/goals/{goal}/execution/recovery/split",
        json={"preview_id": split["id"]},
    )
    assert applied.status_code == 200, applied.text
    result = applied.json()
    assert result["status"] == "APPLIED"
    assert len(result["result_selected_task_ids"]) == 2
    assert client.post(
        f"/api/goals/{goal}/execution/recovery/split",
        json={"preview_id": split["id"]},
    ).json()["result_plan_version_id"] == result["result_plan_version_id"]
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(PlanVersion)) == 2
        parts = list(
            session.scalars(
                select(PlanTask).where(
                    PlanTask.plan_version_id == result["result_plan_version_id"],
                    PlanTask.title.like("范围验收：整理材料（第 %"),
                )
            )
        )
        assert [task.estimate_minutes for task in parts] == [5, 5]
        assert parts[1].depends_on_json == [parts[0].id]
        assert session.get(PlanTask, first).status == "NOT_STARTED"
        event = session.scalar(
            select(ProgressEvent).where(
                ProgressEvent.goal_id == goal, ProgressEvent.event_type == "RECOVERY_SPLIT"
            )
        )
        assert event is not None
        assert event.metadata_json["part_task_ids"] == [task.id for task in parts]


def test_split_rejects_bad_sum_non_selected_and_recorded_task(client, session_factory):
    goal, plan, (first, second, _) = scoped_recovery(client, session_factory)
    preview = preview_selection(client, goal, plan, [first]).json()
    assert split_preview(client, goal, preview["id"], first, [6, 5]).status_code == 422
    assert split_preview(client, goal, preview["id"], second, [20, 25]).status_code == 422
    with session_factory() as session:
        source = session.get(PlanTask, first)
        session.add(
            ProgressEvent(
                goal_id=goal,
                plan_version_id=plan,
                plan_day_id=source.plan_day_id,
                task_id=first,
                event_type="STARTED",
                metadata_json={},
            )
        )
        session.commit()
    fresh = preview_selection(client, goal, plan, [first]).json()
    assert split_preview(client, goal, fresh["id"], first, [5, 5]).status_code == 422


def test_recovery_discussion_receives_selected_scope_and_keeps_plan_unpublished(
    client, session_factory
):
    goal, plan, (first, _, _) = scoped_recovery(client, session_factory)
    preview = preview_selection(client, goal, plan, [first]).json()
    response = client.post(
        f"/api/goals/{goal}/execution/recovery/discussion",
        json={"preview_id": preview["id"]},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["plan"]["id"] == plan
    with session_factory() as session:
        goal_row = session.get(Goal, goal)
        assert goal_row.active_plan_version_id == plan
        assert session.get(PlanVersion, plan) is not None
        conversation_ids = list(
            session.scalars(select(Conversation.id).where(Conversation.goal_id == goal))
        )
        messages = list(
            session.scalars(select(Message).where(Message.conversation_id.in_(conversation_ids)))
        )
        assert any("未选" in message.content for message in messages), [message.content for message in messages]


def test_split_discussion_publish_preserves_deferred_partial_task(client, session_factory):
    goal, plan, (first, deferred, _) = scoped_recovery(client, session_factory)
    preview = preview_selection(client, goal, plan, [first]).json()
    split = split_preview(client, goal, preview["id"], first, [5, 5]).json()
    result = client.post(
        f"/api/goals/{goal}/execution/recovery/split",
        json={"preview_id": split["id"]},
    ).json()
    scoped = preview_selection(
        client, goal, result["result_plan_version_id"], result["result_selected_task_ids"]
    ).json()
    discussion = client.post(
        f"/api/goals/{goal}/execution/recovery/discussion",
        json={"preview_id": scoped["id"]},
    )
    assert discussion.status_code == 200, discussion.text
    assert discussion.json()["pending_adjustment"]["can_apply"]
    reviewed = client.post(f"/api/goals/{goal}/plan/adjustment/preview")
    assert reviewed.status_code == 200, reviewed.text
    confirmed = client.post(
        f"/api/goals/{goal}/plan/adjustment/apply",
        json={"preview_id": reviewed.json()["id"], "accept_unallocated": True},
    )
    assert confirmed.status_code == 200, confirmed.text
    final_plan = confirmed.json()["plan"]["id"]
    assert final_plan != result["result_plan_version_id"]
    with session_factory() as session:
        original = session.get(PlanTask, deferred)
        retained = session.scalar(select(PlanTask).where(
            PlanTask.plan_version_id == final_plan,
            PlanTask.lineage_task_id == deferred,
        ))
        assert retained and retained.status == "IN_PROGRESS"
        parts = list(session.scalars(select(PlanTask).where(
            PlanTask.plan_version_id == final_plan,
            PlanTask.source_task_id.in_(result["result_selected_task_ids"]),
            PlanTask.status != "CARRIED_OVER",
        ).order_by(PlanTask.title)))
        assert len(parts) == 2 and sum(task.estimate_minutes for task in parts) == 10
        assert parts[1].depends_on_json == [parts[0].id]
        original_date = session.scalar(select(PlanDaySchedule.scheduled_date).where(
            PlanDaySchedule.plan_day_id == original.plan_day_id,
        ))
        retained_date = session.scalar(select(PlanDaySchedule.scheduled_date).where(
            PlanDaySchedule.plan_day_id == retained.plan_day_id,
        ))
        assert retained_date == original_date
        assert session.scalar(select(func.count()).select_from(ProgressEvent).where(
            ProgressEvent.goal_id == goal,
            ProgressEvent.event_type == "PARTIAL_COMPLETION",
        )) == 1



def test_followup_reallocates_unassigned_ledger_instead_of_generic_adjustment(
    client, session_factory, monkeypatch,
):
    goal, plan, (first, _, _) = scoped_recovery(client, session_factory)
    scoped = preview_selection(client, goal, plan, [first]).json()
    client.post(f"/api/goals/{goal}/execution/recovery/discussion", json={"preview_id": scoped["id"]})
    with session_factory() as session:
        brief = session.scalar(select(PlanningBrief).where(
            PlanningBrief.goal_id == goal, PlanningBrief.brief_kind == "RECOVERY",
        ))
        original = brief.brief_json["recovery_context"]
        brief.brief_json = {
            **brief.brief_json,
            "recovery_context": {
                **original, "ai_allocation_generated": True,
                "proposed_assignments": [], "unassigned_task_ids": [first],
            },
        }
        session.commit()
    calls = []

    def allocate(self, requirement, route, current, user_input, context, previous=None):
        calls.append(context["recovery_scope"]["selected_task_ids"])
        return (
            {**context, "proposed_assignments": original["proposed_assignments"], "unassigned_task_ids": []},
            SimpleNamespace(assistant_message="已根据补充重新承接", needs_clarification=False, clarifying_question=None),
            False,
        )

    monkeypatch.setattr(PlanService, "_allocate_validated_recovery", allocate)
    response = client.post(f"/api/goals/{goal}/plan/revise", json={"content": "今天可以再投入30分钟"})
    assert response.status_code == 200, response.text
    assert calls == [[first]]
    assert response.json()["pending_adjustment"]["can_apply"]
    reviewed = client.post(f"/api/goals/{goal}/plan/adjustment/preview")
    assert reviewed.status_code == 200, reviewed.text
