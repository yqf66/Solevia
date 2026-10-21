"""Exercise stage boundaries and persisted exploration publication state."""
from datetime import date, timedelta
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import select, func

from planner_api.models import Goal, PlanTask, PlanVersion, PlanWindow, PlanDaySchedule, PlanDay
from planner_api.plan_service import PlanService
from planner_api.execution_discussion_service import ExecutionDiscussionService
from planner_api.plan_discussion_ai import FakePlanDiscussionProvider


def started_exploration(client, schedule_type="RHYTHM"):
    from test_clarification import answer_three_rounds, create_goal

    goal_id = answer_three_rounds(client, create_goal(client))["goal_id"]
    for suffix, payload in (
        ("planning-approach", {"planning_mode": "EXPLORATORY"}),
        ("schedule-type", {"schedule_type": schedule_type}),
    ):
        response = client.put(f"/api/goals/{goal_id}/{suffix}", json=payload)
        assert response.status_code == 200, response.text
    for suffix in ("requirements/confirm", "route/draft", "route/confirm", "plan/draft"):
        response = client.post(f"/api/goals/{goal_id}/{suffix}")
        assert response.status_code == 200, response.text
    workspace = response.json()
    while workspace["generation_phase"] == "DETAILS":
        workspace = client.post(f"/api/goals/{goal_id}/plan/details/next").json()
    assert workspace["plan"]["next_unplanned_week"] <= workspace["plan"]["total_weeks"]
    assert client.post(f"/api/goals/{goal_id}/plan/confirm").status_code == 200
    today = date.today()
    weekdays = sorted((today.weekday() + offset) % 7 for offset in range(workspace["plan"]["weekly_execution_days"]))
    started = client.post(f"/api/goals/{goal_id}/execution/start", json={
        "requested_start_date": today.isoformat(), "execution_weekdays": weekdays, "excluded_dates": [],
    })
    assert started.status_code == 200, started.text
    return goal_id, client.get(f"/api/goals/{goal_id}/plan").json()


@pytest.mark.parametrize("schedule_type", ["RHYTHM", "OUTCOME", "CALENDAR"])
def test_finishing_materialized_stage_keeps_remaining_route_available(client, session_factory, schedule_type):
    goal_id, original = started_exploration(client, schedule_type)
    plan_id = original["plan"]["id"]
    # The earlier days are fixture setup; the final transition uses the real API.
    with session_factory() as session:
        tasks = list(session.scalars(select(PlanTask).where(PlanTask.plan_version_id == plan_id)))
        for task in tasks[:-1]:
            task.status = "COMPLETED"
        last_id = tasks[-1].id
        schedules = list(session.scalars(select(PlanDaySchedule).where(PlanDaySchedule.plan_day_id.in_([task.plan_day_id for task in tasks])).order_by(PlanDaySchedule.scheduled_date)))
        last_day = tasks[-1].plan_day_id
        for index, schedule in enumerate(schedules):
            schedule.scheduled_date = date.today() if schedule.plan_day_id == last_day else date.today() - timedelta(days=len(schedules) - index)
        session.commit()
    completed = client.post(f"/api/goals/{goal_id}/tasks/{last_id}/complete")
    assert completed.status_code == 200, completed.text
    with session_factory() as session:
        assert session.get(Goal, goal_id).status == "ACTIVE"
    refreshed = client.get(f"/api/goals/{goal_id}/plan").json()
    assert refreshed["can_extend_exploration"] is True
    today = client.get(f"/api/goals/{goal_id}/today").json()
    assert today["exploration"]["current_window_completed"] is True
    assert today["exploration"]["next_unplanned_week"] == original["plan"]["next_unplanned_week"]
    progress = client.get(f"/api/goals/{goal_id}/execution/progress").json()
    assert "本轮路线尚未结束" in progress["overall_summary"]
    assert client.post(f"/api/goals/{goal_id}/complete", json={}).status_code == 409
    drafted = client.post(f"/api/goals/{goal_id}/plan/exploration/draft", json={"mode": "STANDARD"})
    assert drafted.status_code == 200, drafted.text
    assert drafted.json()["pending_exploration_window"]["start_week"] == original["plan"]["next_unplanned_week"]
    today = client.get(f"/api/goals/{goal_id}/today").json()
    assert today["exploration"]["pending_window_start_week"] == original["plan"]["next_unplanned_week"]


@pytest.mark.parametrize("schedule_type", ["RHYTHM", "OUTCOME"])
def test_exploration_draft_refresh_discard_apply_preserve_active_history(client, session_factory, schedule_type):
    goal_id, original = started_exploration(client, schedule_type)
    original_id = original["plan"]["id"]
    # A finished action must survive discard and subsequent publication.
    with session_factory() as session:
        task = session.scalar(select(PlanTask).where(PlanTask.plan_version_id == original_id))
        task.status = "COMPLETED"
        completed_title = task.title
        completed_week = session.get(PlanDay, task.plan_day_id).week_index
        session.commit()
    draft = client.post(f"/api/goals/{goal_id}/plan/exploration/draft", json={"mode": "STANDARD"})
    assert draft.status_code == 200, draft.text
    pending = draft.json()["pending_exploration_window"]
    assert pending is not None
    with session_factory() as session:
        assert session.get(Goal, goal_id).active_plan_version_id == original_id
    reread = client.get(f"/api/goals/{goal_id}/plan").json()
    assert reread["pending_exploration_window"]["id"] == pending["id"]
    repeated = client.post(f"/api/goals/{goal_id}/plan/exploration/draft", json={"mode": "STANDARD"})
    assert repeated.json()["pending_exploration_window"]["id"] == pending["id"]
    conflicting = client.post(f"/api/goals/{goal_id}/plan/revise", json={"content": "请把后续任务减半"})
    assert conflicting.status_code == 409
    discard = client.post(f"/api/goals/{goal_id}/plan/exploration/discard")
    assert discard.status_code == 200, discard.text
    assert discard.json()["pending_exploration_window"] is None
    assert discard.json()["plan"]["id"] == original_id
    draft = client.post(f"/api/goals/{goal_id}/plan/exploration/draft", json={"mode": "STANDARD"})
    assert draft.status_code == 200, draft.text
    applied = client.post(f"/api/goals/{goal_id}/plan/exploration/apply")
    assert applied.status_code == 200, applied.text
    published = applied.json()
    assert published["pending_exploration_window"] is None
    assert published["plan"]["id"] != original_id
    assert published["status"] == "ACTIVE"
    with session_factory() as session:
        active_id = session.get(Goal, goal_id).active_plan_version_id
        assert active_id == published["plan"]["id"]
        preserved = session.scalar(select(PlanTask).join(PlanDay).where(PlanTask.plan_version_id == active_id, PlanTask.title == completed_title, PlanDay.week_index == completed_week))
        assert preserved.status == "COMPLETED"
        assert session.scalar(select(func.count(PlanWindow.id)).where(PlanWindow.goal_id == goal_id, PlanWindow.status == "ACTIVE")) == 1
    assert client.get(f"/api/goals/{goal_id}/plan").json()["plan"]["id"] == active_id
    assert client.post(f"/api/goals/{goal_id}/plan/exploration/apply").status_code == 409


def test_exploration_only_completes_after_final_window(client, session_factory):
    goal_id, original = started_exploration(client)
    draft = client.post(f"/api/goals/{goal_id}/plan/exploration/draft", json={"mode": "STANDARD"})
    assert draft.status_code == 200, draft.text
    applied = client.post(f"/api/goals/{goal_id}/plan/exploration/apply")
    assert applied.status_code == 200, applied.text
    plan = applied.json()["plan"]
    assert plan["next_unplanned_week"] > plan["total_weeks"]
    with session_factory() as session:
        tasks = list(session.scalars(select(PlanTask).where(PlanTask.plan_version_id == plan["id"])))
        for task in tasks[:-1]:
            task.status = "COMPLETED"
        last = tasks[-1]
        schedule = session.scalar(select(PlanDaySchedule).where(PlanDaySchedule.plan_day_id == last.plan_day_id))
        schedule.scheduled_date = date.today()
        last_id = last.id
        session.commit()
    response = client.post(f"/api/goals/{goal_id}/tasks/{last_id}/complete")
    assert response.status_code == 200, response.text
    with session_factory() as session:
        assert session.get(Goal, goal_id).status == "COMPLETED"
        assert session.get(Goal, goal_id).active_plan_version_id == plan["id"]
    assert client.get(f"/api/goals/{goal_id}/plan").json()["can_extend_exploration"] is False


def test_stale_exploration_draft_cannot_replace_new_execution_version(client, session_factory):
    goal_id, original = started_exploration(client)
    assert client.post(f"/api/goals/{goal_id}/plan/exploration/draft", json={"mode": "STANDARD"}).status_code == 200
    with session_factory() as session:
        source = session.get(PlanVersion, original["plan"]["id"])
        # Simulate another publication while this window was waiting for approval.
        source.revision = 9
        window = session.scalar(select(PlanWindow).where(PlanWindow.goal_id == goal_id, PlanWindow.status == "DRAFT"))
        window.basis_json = {**window.basis_json, "source_plan_version_id": "superseded-version"}
        session.commit()
    response = client.post(f"/api/goals/{goal_id}/plan/exploration/apply")
    assert response.status_code == 409
    with session_factory() as session:
        assert session.get(Goal, goal_id).active_plan_version_id == original["plan"]["id"]


def test_invalid_stage_contribution_reports_specific_repair_evidence():
    plan = SimpleNamespace(schedule_type="RHYTHM", weekly_execution_days=1, temporal_segments_json=[])
    milestones = {1: SimpleNamespace(start_week=1, end_week=1), 3: SimpleNamespace(start_week=3, end_week=3)}
    task = SimpleNamespace(title="三周复盘", stage_contributions=[{"stage_order": 1, "action": "回顾第一周"}], scheduling_policy_data={}, work_item_keys=[])
    day = SimpleNamespace(week_index=3, day_index=1, stage_order=3, tasks=[task])
    with pytest.raises(HTTPException) as error:
        PlanService._validate_detail_result(plan, milestones, [3], [day])
    assert error.value.detail["code"] == "PLAN_STAGE_CONTRIBUTION_INVALID"
    assert error.value.detail["allowed_stage_orders"] == [3]
    assert error.value.detail["invalid_contribution"]["stage_order"] == 1
    assert error.value.detail["has_work_item_keys"] is False


def test_exploration_repairs_wrong_model_fields_without_user_retry(client, session_factory):
    from planner_api.plan_ai import FakePlanProvider
    from planner_api.models import GenerationBatch

    goal_id, original = started_exploration(client)

    class InvalidOnce(FakePlanProvider):
        calls = 0

        def draft_details(self, context, outline, start_week, end_week, generation_mode="STANDARD"):
            self.calls += 1
            assert "不要输出work_item_keys或stage_contributions" in outline["execution_model_contract"]["rule"]
            result = super().draft_details(context, outline, start_week, end_week, generation_mode)
            if self.calls == 1:
                result.days[0].tasks[0].scheduling_policy_data["stage_contributions"] = [
                    {"stage_order": result.days[0].stage_order, "action": "练习当前阶段"}]
            else:
                feedback = context.planning_context["generation_validation_feedback"]
                assert feedback["code"] == "PLAN_STAGE_CONTRIBUTION_INVALID"
                assert feedback["has_work_item_keys"] is False
            return result

    provider = InvalidOnce()
    with session_factory() as session:
        result = PlanService(session, provider, FakePlanDiscussionProvider()).draft_exploration_window(goal_id)
        assert result.pending_exploration_window is not None
        assert provider.calls == 2
        assert session.get(Goal, goal_id).active_plan_version_id == original["plan"]["id"]
        batch = session.scalar(select(GenerationBatch).where(
            GenerationBatch.goal_id == goal_id, GenerationBatch.scope_type == "EXPLORATION_WEEK"))
        assert batch.status == "COMPLETED"
        assert batch.staged_result_json["compiler_report"]["business_validation_attempts"] == 2


def test_exploration_keeps_successful_weeks_across_later_failure(client, session_factory, monkeypatch):
    from planner_api.ai import ProviderUnavailableError
    from planner_api.plan_ai import FakePlanProvider

    goal_id, original = started_exploration(client)
    monkeypatch.setattr(PlanService, "_next_exploration_stage_end", lambda self, plan, week: plan.total_weeks)
    with session_factory() as session:
        from planner_api.models import PlanMilestone
        plan = session.get(PlanVersion, original["plan"]["id"])
        plan.total_weeks += 1
        milestone = session.scalar(select(PlanMilestone).where(
            PlanMilestone.plan_version_id == plan.id).order_by(PlanMilestone.end_week.desc()))
        milestone.end_week += 1
        segments = [dict(item) for item in plan.temporal_segments_json]
        segments[-1]["end_week"] += 1
        plan.temporal_segments_json = segments
        session.commit()

    class Interrupted(FakePlanProvider):
        calls = []
        fail = True

        def draft_details(self, context, outline, start_week, end_week, generation_mode="STANDARD"):
            self.calls.append(start_week)
            if self.fail and start_week > original["plan"]["next_unplanned_week"]:
                raise ProviderUnavailableError("连接中断", code="PROVIDER_ERROR")
            return super().draft_details(context, outline, start_week, end_week, generation_mode)

    provider = Interrupted()
    with session_factory() as session:
        with pytest.raises(ProviderUnavailableError):
            PlanService(session, provider, FakePlanDiscussionProvider()).draft_exploration_window(goal_id)
        assert session.get(Goal, goal_id).active_plan_version_id == original["plan"]["id"]
    provider.fail = False
    with session_factory() as session:
        result = PlanService(session, provider, FakePlanDiscussionProvider()).draft_exploration_window(goal_id)
        assert result.pending_exploration_window is not None
    assert provider.calls.count(original["plan"]["next_unplanned_week"]) == 1


def test_execution_discussion_distinguishes_active_expansion_from_pending_window(client, session_factory):
    goal_id, original = started_exploration(client)
    assert client.post(f"/api/goals/{goal_id}/plan/exploration/draft", json={"mode": "STANDARD"}).status_code == 200
    with session_factory() as session:
        goal = session.get(Goal, goal_id)
        plan = session.get(PlanVersion, goal.active_plan_version_id)
        service = ExecutionDiscussionService(session, FakePlanDiscussionProvider())
        conversation = service._ensure_conversation(goal_id)
        state = service._context(goal, plan, conversation, "现在展开到哪里了？").current_plan["exploration_state"]
        assert state["generated_through_week"] == original["plan"]["next_unplanned_week"] - 1
        assert state["pending_window"]["start_week"] == original["plan"]["next_unplanned_week"]
        assert state["pending_window"]["status"] == "DRAFT_NOT_PUBLISHED"

