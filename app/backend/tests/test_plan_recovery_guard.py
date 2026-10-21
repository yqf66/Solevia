from datetime import date, timedelta

from sqlalchemy import select

from planner_api.models import PlanActivation, PlanDay, PlanDaySchedule, PlanTask
from test_execution_discussion import _started_goal


def _make_first_execution_day_missed(client, session_factory) -> str:
    goal_id = _started_goal(client)
    with session_factory() as session:
        activation = session.scalar(
            select(PlanActivation).where(PlanActivation.goal_id == goal_id)
        )
        assert activation is not None
        schedule = session.scalar(
            select(PlanDaySchedule)
            .where(PlanDaySchedule.activation_id == activation.id)
            .order_by(PlanDaySchedule.scheduled_date)
        )
        assert schedule is not None
        schedule.scheduled_date = date.today() - timedelta(days=1)
        task = session.scalar(
            select(PlanTask).where(PlanTask.plan_day_id == schedule.plan_day_id)
        )
        assert task is not None
        assert task.status not in {"COMPLETED", "CANCELLED", "CARRIED_OVER"}
        session.commit()
    return goal_id


def test_plan_workspace_exposes_recovery_guard_for_unresolved_missed_work(
    client, session_factory
) -> None:
    goal_id = _make_first_execution_day_missed(client, session_factory)

    response = client.get(f"/api/goals/{goal_id}/plan")

    assert response.status_code == 200, response.text
    guard = response.json()["recovery_guard"]
    assert guard["required"] is True
    assert guard["missed_execution_days"] >= 1
    assert guard["interrupted_execution_days"] >= 1
    assert (date.today() - timedelta(days=1)).isoformat() in guard["missed_dates"]
    assert "先决定追赶" in guard["message"]


def test_adjustment_preview_can_be_created_but_publish_requires_recovery_decision(
    client, session_factory
) -> None:
    goal_id = _make_first_execution_day_missed(client, session_factory)
    revised = client.post(
        f"/api/goals/{goal_id}/plan/revise",
        json={"content": "请把第 2 周调松一些。"},
    )
    assert revised.status_code == 200, revised.text

    preview = client.post(f"/api/goals/{goal_id}/plan/adjustment/preview")
    assert preview.status_code == 200, preview.text
    preview_id = preview.json()["id"]
    reloaded = client.get(f"/api/goals/{goal_id}/plan")
    assert reloaded.status_code == 200, reloaded.text
    assert reloaded.json()["pending_adjustment_preview"]["id"] == preview_id

    applied = client.post(
        f"/api/goals/{goal_id}/plan/adjustment/apply",
        json={"preview_id": preview_id},
    )

    assert applied.status_code == 409, applied.text
    detail = applied.json()["detail"]
    assert detail["code"] == "RECOVERY_DECISION_REQUIRED"
    assert detail["missed_dates"]
    with session_factory() as session:
        assert len(list(session.scalars(select(PlanDay)))) > 0


def test_deferring_recovery_removes_guard_and_allows_pending_adjustment_to_publish(
    client, session_factory
) -> None:
    goal_id = _make_first_execution_day_missed(client, session_factory)
    revised = client.post(
        f"/api/goals/{goal_id}/plan/revise",
        json={"content": "请把第 2 周调松一些。"},
    )
    assert revised.status_code == 200, revised.text
    current_workspace = client.get(f"/api/goals/{goal_id}/plan")
    assert current_workspace.status_code == 200, current_workspace.text
    missed_dates = current_workspace.json()["recovery_guard"]["missed_dates"]
    assert missed_dates
    today_workspace = client.get(f"/api/goals/{goal_id}/today")
    assert today_workspace.status_code == 200, today_workspace.text
    assert today_workspace.json()["inactivity"]["missed_dates"] == missed_dates

    deferred = client.post(
        f"/api/goals/{goal_id}/execution/recovery/defer",
        json={"missed_dates": missed_dates},
    )
    assert deferred.status_code == 200, deferred.text

    workspace = client.get(f"/api/goals/{goal_id}/plan")
    assert workspace.status_code == 200, workspace.text
    assert workspace.json()["recovery_guard"]["required"] is False
    assert workspace.json()["recovery_guard"]["recovery_deferred"] is True

    applied = client.post(f"/api/goals/{goal_id}/plan/adjustment/apply")

    assert applied.status_code == 200, applied.text
    assert applied.json()["pending_adjustment"] is None
    assert applied.json()["plan"]["revision"] == 2


def test_recovery_guard_does_not_block_a_clean_plan(client) -> None:
    goal_id = _started_goal(client)

    response = client.get(f"/api/goals/{goal_id}/plan")

    assert response.status_code == 200, response.text
    guard = response.json()["recovery_guard"]
    assert guard["required"] is False
    assert guard["missed_dates"] == []
