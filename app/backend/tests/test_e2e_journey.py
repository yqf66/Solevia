from datetime import UTC, date, datetime, timedelta

from sqlalchemy import select
from test_clarification import answer_three_rounds, create_goal

from planner_api.models import (
    Goal,
    PlanActivation,
    PlanDaySchedule,
    PlanTask,
    PlanVersion,
    ProgressEvent,
)


def test_full_goal_journey_can_execute_and_recover(client, session_factory) -> None:
    clarification = answer_three_rounds(client, create_goal(client))
    goal_id = clarification["goal_id"]
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

    requirement = client.post(f"/api/goals/{goal_id}/requirements/confirm")
    assert requirement.status_code == 200
    route = client.post(f"/api/goals/{goal_id}/route/draft")
    assert route.status_code == 200
    revised_route = client.post(
        f"/api/goals/{goal_id}/route/revise",
        json={"content": "保留三阶段结构，但让第一个阶段更强调可演示产出。"},
    )
    assert revised_route.status_code == 200
    assert revised_route.json()["route"]["revision"] == 2
    assert client.post(f"/api/goals/{goal_id}/route/confirm").status_code == 200

    planning = client.post(f"/api/goals/{goal_id}/plan/draft").json()
    while planning["generation_phase"] == "DETAILS":
        planning = client.post(f"/api/goals/{goal_id}/plan/details/next").json()
    assert planning["generation_phase"] == "COMPLETE"
    assert client.post(f"/api/goals/{goal_id}/plan/confirm").status_code == 200

    today = date.today()
    weekly_days = planning["plan"]["weekly_execution_days"]
    execution_weekdays = sorted((today.weekday() + offset) % 7 for offset in range(weekly_days))
    started = client.post(
        f"/api/goals/{goal_id}/execution/start",
        json={
            "requested_start_date": today.isoformat(),
            "execution_weekdays": execution_weekdays,
            "excluded_dates": [],
        },
    )
    assert started.status_code == 200
    first_task_id = started.json()["day"]["tasks"][0]["id"]
    partial = client.post(
        f"/api/goals/{goal_id}/tasks/{first_task_id}/progress",
        json={"action": "PARTIAL", "amount": 25, "unit": "MINUTES"},
    )
    assert partial.status_code == 200
    assert next(
        item for item in partial.json()["day"]["tasks"] if item["id"] == first_task_id
    )["status"] == "IN_PROGRESS"
    deferred = client.post(
        f"/api/goals/{goal_id}/tasks/{first_task_id}/progress",
        json={"action": "DEFER", "reason": "今天时间不够，保留到后续安排"},
    )
    assert deferred.status_code == 200
    assert client.post(f"/api/goals/{goal_id}/tasks/{first_task_id}/complete").status_code == 200

    with session_factory() as session:
        progress_events = list(
            session.scalars(
                select(ProgressEvent).where(
                    ProgressEvent.goal_id == goal_id,
                    ProgressEvent.task_id == first_task_id,
                )
            )
        )
        assert {event.event_type for event in progress_events} >= {
            "PARTIAL_COMPLETION",
            "DEFERRED",
        }
        completed_before_recovery = session.get(PlanTask, first_task_id)
        assert completed_before_recovery is not None
        assert completed_before_recovery.status == "COMPLETED"
        goal_event_ids_before_recovery = {
            event.id
            for event in session.scalars(
                select(ProgressEvent).where(ProgressEvent.goal_id == goal_id)
            )
        }

    with session_factory() as session:
        goal = session.get(Goal, goal_id)
        plan = session.get(PlanVersion, goal.active_plan_version_id)
        activation = session.scalar(
            select(PlanActivation).where(PlanActivation.plan_version_id == plan.id)
        )
        schedules = list(
            session.scalars(
                select(PlanDaySchedule)
                .where(PlanDaySchedule.activation_id == activation.id)
                .order_by(PlanDaySchedule.scheduled_date)
            )
        )
        schedules[1].scheduled_date = today - timedelta(days=7)
        activation.activated_at = datetime.now(UTC) - timedelta(days=8)
        goal.last_active_at = datetime.now(UTC) - timedelta(days=8)
        for event in session.scalars(select(ProgressEvent).where(ProgressEvent.goal_id == goal_id)):
            event.occurred_at = datetime.now(UTC) - timedelta(days=8)
        session.commit()

    checked = client.post("/api/goals/status-check")
    assert checked.status_code == 200
    goal_item = next(item for item in checked.json() if item["id"] == goal_id)
    assert goal_item["status"] == "RECOVERY_REQUIRED"

    preview = client.get(f"/api/goals/{goal_id}/execution/recovery/shift-preview")
    assert preview.status_code == 200
    preview_data = preview.json()
    assert preview_data["model_called"] is False
    assert preview_data["changes"]
    recovered = client.post(
        f"/api/goals/{goal_id}/execution/recovery/shift",
        json={"preview_id": preview_data["id"]},
    )
    assert recovered.status_code == 200

    lifecycle = client.get(f"/api/goals/{goal_id}/lifecycle").json()
    progress = client.get(f"/api/goals/{goal_id}/execution/progress").json()
    assert lifecycle["status"] == "ACTIVE"
    assert progress["plan_revision"] == 2
    assert progress["overall_progress"]["completed_tasks"] >= 1
    assert progress["stage_summary"]
    assert progress["overall_summary"]
    with session_factory() as session:
        completed_after_recovery = session.get(PlanTask, first_task_id)
        assert completed_after_recovery is not None
        assert completed_after_recovery.status == "COMPLETED"
        recovered_events = list(
            session.scalars(select(ProgressEvent).where(ProgressEvent.goal_id == goal_id))
        )
        assert goal_event_ids_before_recovery <= {event.id for event in recovered_events}
