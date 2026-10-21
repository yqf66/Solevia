"""Recovery attempts must have one lifecycle across revisit, dismiss and publish."""
from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from planner_api.api import get_plan_service
from planner_api.models import Goal, PlanningBrief, PlanTask, PlanDaySchedule, PlanActivation
from planner_api.plan_service import PlanService
from test_execution_discussion import _started_goal
from test_recovery_scope import scoped_recovery, preview_selection


def discuss(client, goal, plan, selected):
    scope = preview_selection(client, goal, plan, selected)
    assert scope.status_code == 200, scope.text
    response = client.post(f"/api/goals/{goal}/execution/recovery/discussion", json={"preview_id": scope.json()["id"]})
    assert response.status_code == 200, response.text
    return response.json()


@pytest.mark.parametrize("status", ["CONFIRMED", "DISMISSED", "SUPERSEDED"])
def test_legacy_older_draft_never_resurrects_after_newer_terminal_attempt(client, session_factory, status):
    goal = _started_goal(client)
    with session_factory() as session:
        original = session.scalar(select(PlanningBrief).where(PlanningBrief.goal_id == goal))
        for version, state in [(1, "DRAFT"), (2, status)]:
            session.add(PlanningBrief(goal_id=goal, context_pack_id=original.context_pack_id,
                plan_version_id=original.plan_version_id, brief_kind="RECOVERY", version=version,
                status=state, brief_json={"recovery_context": {"proposed_assignments": [{"old": True}]}}))
        session.commit()
    result = client.get(f"/api/goals/{goal}/plan").json()
    assert result["pending_adjustment"] is None
    assert result["recovery_review"] is None
    assert result["recovery_guard"]["required"] is False


def test_discard_closes_attempt_and_next_ordinary_revision_is_not_recovery(client, session_factory):
    goal, plan, (first, _, _) = scoped_recovery(client, session_factory)
    discuss(client, goal, plan, [first])
    discarded = client.post(f"/api/goals/{goal}/plan/adjustment/discard").json()
    assert discarded["pending_adjustment"] is None
    with session_factory() as session:
        brief = session.scalar(select(PlanningBrief).where(PlanningBrief.goal_id == goal, PlanningBrief.brief_kind == "RECOVERY"))
        assert brief.status == "DISMISSED"
        assert get_plan_service(session)._active_recovery_context(goal) is None
    # Unselected missed work still requires a recovery decision; do not erase it.
    assert discarded["recovery_guard"]["missed_dates"]
    ordinary = client.post(f"/api/goals/{goal}/plan/revise", json={"content": "把第二周安排调松一些"}).json()
    assert ordinary["recovery_review"] is None
    assert not ordinary["messages"][-1]["metadata"].get("recovery_brief_id")


def test_repeated_attempt_then_publish_closes_all_drafts_and_keeps_completed_tasks(client, session_factory):
    goal, plan, (first, _, completed_id) = scoped_recovery(client, session_factory)
    with session_factory() as session:
        completed = session.get(PlanTask, completed_id)
        completed.status = "COMPLETED"
        session.commit()
    discuss(client, goal, plan, [first])
    second = discuss(client, goal, plan, [first])
    assert second["pending_adjustment"]["can_apply"]
    preview = client.post(f"/api/goals/{goal}/plan/adjustment/preview")
    assert preview.status_code == 200, preview.text
    applied = client.post(f"/api/goals/{goal}/plan/adjustment/apply", json={"preview_id": preview.json()["id"], "accept_unallocated": True})
    assert applied.status_code == 200, applied.text
    assert applied.json()["pending_adjustment"] is None
    with session_factory() as session:
        briefs = list(session.scalars(select(PlanningBrief).where(PlanningBrief.goal_id == goal, PlanningBrief.brief_kind == "RECOVERY").order_by(PlanningBrief.version)))
        assert [brief.status for brief in briefs] == ["SUPERSEDED", "CONFIRMED"]
        assert get_plan_service(session)._active_recovery_context(goal) is None
        active_plan_id = session.get(Goal, goal).active_plan_version_id
        preserved = session.scalar(select(PlanTask).where(
            PlanTask.plan_version_id == active_plan_id, PlanTask.source_task_id == completed_id,
        ))
        assert preserved is not None and preserved.status == "COMPLETED"
        calendar = client.get(f"/api/goals/{goal}/execution/calendar").json()
        displayed = next(day for day in calendar["days"] if day["plan_day_id"] == preserved.plan_day_id)
        assert any(task["title"] in displayed["primary_goal"] for task in displayed["tasks"] if task["status"] != "CARRIED_OVER")
    assert applied.json()["messages"][-1]["metadata"]["recovery_brief_id"] == briefs[-1].id


def test_changed_task_ledger_does_not_restore_stale_date_preview(client, session_factory):
    goal = _started_goal(client)
    revised = client.post(f"/api/goals/{goal}/plan/revise", json={"content": "把第二周安排调松一些"})
    assert revised.status_code == 200, revised.text
    preview = client.post(f"/api/goals/{goal}/plan/adjustment/preview")
    assert preview.status_code == 200, preview.text
    assert client.get(f"/api/goals/{goal}/plan").json()["pending_adjustment_preview"]["id"] == preview.json()["id"]
    with session_factory() as session:
        plan = session.get(Goal, goal).active_plan_version_id
        task = session.scalar(select(PlanTask).where(PlanTask.plan_version_id == plan))
        task.status = "COMPLETED"
        session.commit()
    state = client.get(f"/api/goals/{goal}/plan").json()
    assert state["pending_adjustment"] is not None
    assert state["pending_adjustment_preview"] is None
    assert client.post(f"/api/goals/{goal}/plan/adjustment/apply", json={"preview_id": preview.json()["id"]}).status_code == 409


def test_rest_day_progress_is_not_the_next_execution_day(client, session_factory):
    goal = _started_goal(client)
    with session_factory() as session:
        plan = session.get(Goal, goal).active_plan_version_id
        activation = session.scalar(select(PlanActivation).where(PlanActivation.plan_version_id == plan))
        for schedule in session.scalars(select(PlanDaySchedule).where(PlanDaySchedule.activation_id == activation.id)):
            schedule.scheduled_date += timedelta(days=1)
        session.commit()
    progress = client.get(f"/api/goals/{goal}/execution/progress").json()
    assert progress["day_progress"]["total_tasks"] == 0
    assert progress["overall_progress"]["total_tasks"] > 0


def test_fully_allocated_recovery_followup_updates_ledger_and_persists_new_question(client, session_factory, monkeypatch):
    goal, plan, (first, _, _) = scoped_recovery(client, session_factory)
    discuss(client, goal, plan, [first])
    with session_factory() as session:
        brief = session.scalar(select(PlanningBrief).where(PlanningBrief.goal_id == goal, PlanningBrief.brief_kind == "RECOVERY"))
        original = deepcopy(brief.brief_json["recovery_context"])
        brief.brief_json = {**brief.brief_json, "recovery_context": {**original, "ai_allocation_generated": True}}
        session.commit()
    calls = []
    def allocate(self, requirement, route, current, user_input, context, previous=None):
        calls.append(user_input)
        return ({**context, "allocation_clarifying_question": "请明确新增时间的日期"},
                SimpleNamespace(assistant_message="需要明确日期", needs_clarification=True, clarifying_question="请明确新增时间的日期"), False)
    monkeypatch.setattr(PlanService, "_allocate_validated_recovery", allocate)
    result = client.post(f"/api/goals/{goal}/plan/revise", json={"content": "请换一个补做时间"})
    assert result.status_code == 200, result.text
    assert calls == ["请换一个补做时间"]
    reloaded = client.get(f"/api/goals/{goal}/plan").json()
    assert not reloaded["pending_adjustment"]["can_apply"]
    assert "请明确新增时间的日期" in reloaded["pending_adjustment"]["clarifying_question"]
