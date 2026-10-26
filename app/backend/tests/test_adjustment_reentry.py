"""Saved previews survive lazy initialization, not real schedule edits."""
from datetime import timedelta

import pytest
from fastapi import HTTPException
from sqlalchemy import delete, select

from planner_api.api import get_plan_service
from planner_api.execution_service import ExecutionService
from planner_api.models import Goal, PlanActivation, PlanAdjustmentPreview, PlanDaySchedule, PlanSchedulePreference
from planner_api.schemas import PlanAdjustmentProposal
from test_exploration_lifecycle import started_exploration


def test_saved_preview_survives_first_today_read_and_real_changes_still_invalidate(client, session_factory):
    goal_id, workspace = started_exploration(client)
    with session_factory() as session:
        service = get_plan_service(session)
        plan = service._current_plan(goal_id)
        session.execute(delete(PlanSchedulePreference).where(PlanSchedulePreference.goal_id == goal_id))
        session.flush()
        proposal = PlanAdjustmentProposal(
            assistant_message="保持每周一次的原有节奏，只调整第一周练习方法并保留原计划。",
            user_intent_summary="调整第一周做法", scope="WEEK",
            affected_stage_orders=[1], affected_weeks=[1],
            proposed_changes=["改变练习方法"], tradeoffs=[], unchanged_guarantees=["总时长不变"],
            can_apply=True, needs_clarification=False, weekly_intensity_change="UNCHANGED", buffer_change="UNCHANGED",
        )
        source_fingerprint = service._adjustment_schedule_fingerprint(plan)
        preview = PlanAdjustmentPreview(
            goal_id=goal_id, source_plan_version_id=plan.id, fingerprint="saved-read-initialization",
            preview_json={"plan": service._plan_payload(plan), "schedule": [],
                "source_fingerprint": source_fingerprint, "proposal": proposal.model_dump(mode="json")},
        )
        session.add(preview); session.flush()
        preview_id = preview.id
        goal = session.get(Goal, goal_id)
        ExecutionService(session)._schedule_preference(goal)
        assert service._adjustment_schedule_fingerprint(plan) == source_fingerprint
        assert service._pending_adjustment_preview(goal_id, plan, proposal).id == preview_id
        session.commit()
    for _ in range(3):
        assert client.get(f"/api/goals/{goal_id}/today").status_code == 200
        with session_factory() as session:
            service = get_plan_service(session); plan = service._current_plan(goal_id)
            assert service._pending_adjustment_preview(goal_id, plan, proposal).id == preview_id
    with session_factory() as session:
        service = get_plan_service(session); plan = service._current_plan(goal_id)
        preference = session.scalar(select(PlanSchedulePreference).where(PlanSchedulePreference.goal_id == goal_id))
        preference.mode = "BLOCKS"; session.flush()
        assert service._pending_adjustment_preview(goal_id, plan, proposal) is None
        preference.mode = "FLEXIBLE"; session.flush()
        assert service._pending_adjustment_preview(goal_id, plan, proposal).id == preview_id
        day = session.scalar(select(PlanDaySchedule).join(PlanActivation).where(PlanActivation.plan_version_id == plan.id))
        day.scheduled_date += timedelta(days=1); session.flush()
        assert service._pending_adjustment_preview(goal_id, plan, proposal) is None


def test_expired_adjustment_scope_has_actionable_conflict_and_keeps_plan(client, session_factory):
    goal_id, workspace = started_exploration(client)
    with session_factory() as session:
        service = get_plan_service(session); goal, requirement, route = service._get_review_inputs(goal_id)
        plan = service._current_plan(goal_id)
        for day in session.scalars(select(PlanDaySchedule).join(PlanActivation).where(PlanActivation.plan_version_id == plan.id)):
            day.scheduled_date = service._today_for_goal_id(goal_id) - timedelta(days=1)
        session.flush()
        proposal = PlanAdjustmentProposal(assistant_message="只调整第一周的练习方法，其他周的任务和完成记录保持不变。", user_intent_summary="调整第一周",
            scope="WEEK", affected_stage_orders=[1], affected_weeks=[1],
            proposed_changes=["调整做法"], tradeoffs=[], unchanged_guarantees=["保留已完成记录"], can_apply=True,
            weekly_intensity_change="UNCHANGED", buffer_change="UNCHANGED")
        with pytest.raises(HTTPException) as caught:
            service._build_adjustment_result(goal, requirement, route, plan, proposal)
        assert caught.value.status_code == 409
        assert "原计划和建议已保留" in caught.value.detail
        assert goal.active_plan_version_id == workspace["plan"]["id"]
