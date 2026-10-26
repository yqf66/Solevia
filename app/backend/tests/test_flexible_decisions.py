from datetime import date, timedelta

import pytest
from sqlalchemy import func, select
from test_execution_calendar import _started
from test_task_recommendations import candidates

from planner_api.models import PlanTask, ProgressEvent, TaskTimeBlock

pytest_plugins = ["test_task_recommendations"]


def decision(client, goal, task, action="ARRANGE_TODAY", **extra):
    response = client.post(
        f"/api/goals/{goal}/execution/flexible-tasks/decision-preview",
        json={"action": action, "task_id": task, **extra},
    )
    assert response.status_code == 200, response.text
    return response.json()


def apply(client, goal, preview):
    return client.post(
        f"/api/goals/{goal}/execution/flexible-tasks/decision-apply",
        json={"preview_id": preview["id"]},
    )


def test_arrange_preview_is_read_only_and_confirmation_is_idempotent(
    client, session_factory, recommendation_goal
):
    goal, today, days = recommendation_goal
    task_id = candidates(session_factory, days)[0]
    with session_factory() as session:
        day_before = session.get(PlanTask, task_id).plan_day_id
        event_count = session.scalar(select(func.count()).select_from(ProgressEvent))
        block_count = session.scalar(select(func.count()).select_from(TaskTimeBlock))
    preview = decision(client, goal, task_id)
    assert preview["can_apply"]
    assert preview["target_date"] == today
    assert preview["shared_available_minutes"] == 120
    assert preview["proposed_start_local"] == "19:00"
    with session_factory() as session:
        assert session.get(PlanTask, task_id).plan_day_id == day_before
        assert session.scalar(select(func.count()).select_from(ProgressEvent)) == event_count
        assert session.scalar(select(func.count()).select_from(TaskTimeBlock)) == block_count
    response = apply(client, goal, preview)
    assert response.status_code == 200, response.text
    assert any(task["id"] == task_id for task in response.json()["day"]["tasks"])
    assert apply(client, goal, preview).status_code == 200
    with session_factory() as session:
        assert (
            session.scalar(
                select(func.count())
                .select_from(ProgressEvent)
                .where(
                    ProgressEvent.task_id == task_id, ProgressEvent.event_type == "CARRY_FORWARD"
                )
            )
            == 1
        )


@pytest.mark.parametrize("change", ["availability", "dependency", "window", "status"])
def test_changed_state_rejects_confirmation_and_leaves_task_unmoved(
    client, session_factory, recommendation_goal, change
):
    goal, today, days = recommendation_goal
    first, predecessor = candidates(session_factory, days, 2)
    with session_factory() as session:
        session.get(PlanTask, predecessor).status = "COMPLETED"
        session.get(PlanTask, first).depends_on_json = [predecessor]
        session.commit()
        original_day = session.get(PlanTask, first).plan_day_id
    preview = decision(client, goal, first)
    assert preview["can_apply"]
    if change == "availability":
        assert (
            client.put(
                f"/api/goals/{goal}/execution/schedule/availability", json={"windows": []}
            ).status_code
            == 200
        )
    else:
        with session_factory() as session:
            if change == "dependency":
                session.get(PlanTask, predecessor).status = "NOT_STARTED"
            elif change == "window":
                session.get(PlanTask, first).scheduling_policy_json = {"not_before": "2099-01-01"}
            else:
                session.get(PlanTask, first).status = "COMPLETED"
            session.commit()
    response = apply(client, goal, preview)
    assert response.status_code == 409, response.text
    with session_factory() as session:
        assert session.get(PlanTask, first).plan_day_id == original_day


def test_preview_blocks_cross_period_and_unknown_dependencies(
    client, session_factory, recommendation_goal
):
    goal, today, days = recommendation_goal
    task = candidates(session_factory, days, scheduling_policy="CADENCE")[0]
    with session_factory() as session:
        session.get(PlanTask, task).plan_day_id = days[1]["plan_day_id"]
        session.commit()
    target = str(date.fromisoformat(today) + timedelta(days=8))
    preview = decision(client, goal, task, "RESCHEDULE", target_date=target, reason="测试跨周期")
    assert not preview["can_apply"] and any("周期" in item for item in preview["blockers"])
    assert apply(client, goal, preview).status_code == 409
    with session_factory() as session:
        session.get(PlanTask, task).scheduling_policy = "FLOATING"
        session.get(PlanTask, task).depends_on_json = ["missing"]
        session.commit()
    preview = decision(client, goal, task, "RESCHEDULE", target_date=today, reason="测试前置")
    assert not preview["can_apply"] and any("前置" in item for item in preview["blockers"])


def test_different_reschedule_reasons_do_not_reuse_old_audit_request(
    client, session_factory, recommendation_goal
):
    goal, today, days = recommendation_goal
    task = candidates(session_factory, days)[0]
    first = decision(client, goal, task, "RESCHEDULE", target_date=today, reason="会议")
    second = decision(client, goal, task, "RESCHEDULE", target_date=today, reason="身体不适")
    assert first["id"] != second["id"]


def test_shared_occupancy_change_invalidates_preview(client, session_factory, recommendation_goal):
    goal, today, days = recommendation_goal
    incoming = candidates(session_factory, days)[0]
    other_goal, other_days = _started(client)
    preview = decision(client, goal, incoming)
    with session_factory() as session:
        # A different plan's concrete slot occupies the same shared board.
        task = session.scalar(
            select(PlanTask).where(PlanTask.plan_day_id == other_days[0]["plan_day_id"])
        )
        session.add(
            TaskTimeBlock(
                goal_id=other_goal,
                plan_version_id=task.plan_version_id,
                plan_day_id=task.plan_day_id,
                task_id=task.id,
                scheduled_date=date.fromisoformat(today),
                start_local="19:00",
                end_local="20:00",
                timezone="Asia/Shanghai",
            )
        )
        session.commit()
    assert apply(client, goal, preview).status_code == 409
    fresh = decision(client, goal, incoming)
    assert fresh["fingerprint"] != preview["fingerprint"]
    assert fresh["shared_available_minutes"] < preview["shared_available_minutes"]


def test_rescheduling_in_progress_keeps_partial_history(
    client, session_factory, recommendation_goal
):
    goal, today, days = recommendation_goal
    task_id = candidates(session_factory, days)[0]
    with session_factory() as session:
        task = session.get(PlanTask, task_id)
        task.status = "IN_PROGRESS"
        event = ProgressEvent(
            goal_id=goal,
            plan_version_id=task.plan_version_id,
            plan_day_id=task.plan_day_id,
            task_id=task_id,
            event_type="PARTIAL_COMPLETION",
            metadata_json={"amount": 10, "unit": "MINUTES"},
        )
        session.add(event)
        session.commit()
        event_id, event_day = event.id, event.plan_day_id
    preview = decision(
        client, goal, task_id, "RESCHEDULE", target_date=today, reason="继续完成剩余部分"
    )
    assert preview["can_apply"]
    assert apply(client, goal, preview).status_code == 200
    with session_factory() as session:
        task = session.get(PlanTask, task_id)
        event = session.get(ProgressEvent, event_id)
        assert task.status == "IN_PROGRESS"
        assert event.plan_day_id == event_day
        assert event.metadata_json == {"amount": 10, "unit": "MINUTES"}
        assert task.plan_day_id != event_day


def test_preview_identity_cannot_be_applied_to_other_goal(
    client, recommendation_goal, session_factory
):
    goal, _, days = recommendation_goal
    task = candidates(session_factory, days)[0]
    preview = decision(client, goal, task)
    other, _ = _started(client)
    assert apply(client, other, preview).status_code == 404
