from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from threading import Lock
from time import sleep
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import func, select

from planner_api.execution_service import serialize_recovery_transaction
from planner_api.models import (
    Goal,
    PlanActivation,
    PlanDaySchedule,
    PlanTask,
    PlanVersion,
    RecoveryOperation,
    TaskTimeBlock,
)
from planner_api.schedule_reconciliation import changed_schedule_dates


def test_recovery_transactions_are_serialized():
    state_lock = Lock()
    active = 0
    max_active = 0

    @serialize_recovery_transaction
    def simulate_preview() -> None:
        nonlocal active, max_active
        with state_lock:
            active += 1
            max_active = max(max_active, active)
        sleep(0.02)
        with state_lock:
            active -= 1

    with ThreadPoolExecutor(max_workers=2) as executor:
        list(executor.map(lambda _: simulate_preview(), range(2)))

    assert max_active == 1


def setup_recovery(client):
    from test_plan import _completed_plan

    workspace = _completed_plan(client)
    goal = workspace["goal_id"]
    today = datetime.now(ZoneInfo("Asia/Shanghai")).date()
    client.post(f"/api/goals/{goal}/plan/confirm")
    result = client.post(f"/api/goals/{goal}/execution/start", json={
        "requested_start_date": str(today - timedelta(days=21)),
        "execution_weekdays": sorted({today.weekday(), (today.weekday() + 1) % 7}),
        "excluded_dates": [],
    })
    assert result.status_code == 200
    client.patch(f"/api/goals/{goal}/execution/schedule/preferences", json={"mode": "BLOCKS"})
    return goal, workspace["plan"]["id"]


def test_small_catchup_schedules_only_affected_dates_without_model(
    client, session_factory, monkeypatch,
):
    _small_catchup_case(client, session_factory, monkeypatch)


@pytest.mark.parametrize("advance_minutes", [1, 1440])
def test_elapsed_minute_rejects_catchup_preview_without_publishing(
    client, session_factory, monkeypatch, advance_minutes,
):
    _small_catchup_case(client, session_factory, monkeypatch, advance_minutes=advance_minutes)


def _small_catchup_case(client, session_factory, monkeypatch, *, advance_minutes=0):
    from test_plan import _completed_plan

    # This test checks recovery scope, not wall-clock races at minute boundaries.
    # Today's allocator excludes elapsed minutes; test that expiry separately.
    class Clock:
        value = datetime.now(ZoneInfo("Asia/Shanghai")).replace(hour=10, minute=0, second=30, microsecond=0)

        @classmethod
        def now(cls, tz=None):
            return cls.value.astimezone(tz) if tz else cls.value.replace(tzinfo=None)

    monkeypatch.setattr("planner_api.day_allocation.datetime", Clock)
    workspace = _completed_plan(client)
    goal = workspace["goal_id"]
    today = datetime.now(ZoneInfo("Asia/Shanghai")).date()
    client.post(f"/api/goals/{goal}/plan/confirm")
    started = client.post(f"/api/goals/{goal}/execution/start", json={
        "requested_start_date": str(today - timedelta(days=1)),
        "execution_weekdays": sorted({(today - timedelta(days=1)).weekday(), today.weekday()}),
        "excluded_dates": [],
    })
    assert started.status_code == 200
    client.patch(f"/api/goals/{goal}/execution/schedule/preferences", json={"mode": "BLOCKS"})
    client.put(f"/api/goals/{goal}/execution/schedule/availability", json={
        "windows": [{"weekday": i, "start_local": "00:00", "end_local": "23:59"}
                    for i in range(7)],
    })
    with session_factory() as session:
        activation = session.scalar(select(PlanActivation).where(PlanActivation.goal_id == goal))
        schedules = list(session.scalars(select(PlanDaySchedule).where(
            PlanDaySchedule.activation_id == activation.id,
        ).order_by(PlanDaySchedule.scheduled_date)))
        for task in session.scalars(select(PlanTask).where(
            PlanTask.plan_day_id == schedules[0].plan_day_id,
        )):
            task.estimate_minutes = 10
            task.depends_on_json = []
        untouched = schedules[-1]
        task = session.scalar(select(PlanTask).where(PlanTask.plan_day_id == untouched.plan_day_id))
        title = task.title
        session.add(TaskTimeBlock(
            goal_id=goal, plan_version_id=task.plan_version_id, plan_day_id=task.plan_day_id,
            task_id=task.id, scheduled_date=untouched.scheduled_date,
            start_local="18:00", end_local="19:00", source="USER",
        ))
        untouched_date = untouched.scheduled_date.isoformat()
        session.commit()

    def reject_model(*_args, **_kwargs):
        raise AssertionError("Small catchup must not call the scheduling model")

    monkeypatch.setattr("planner_api.day_schedule_service.build_day_schedule_provider",
                        lambda _settings: SimpleNamespace(suggest=reject_model))
    response = client.get(f"/api/goals/{goal}/execution/recovery/catchup-preview")
    assert response.status_code == 200, response.text
    preview = response.json()
    assert preview["programmatic_feasible"]
    with session_factory() as session:
        stored = session.get(RecoveryOperation, preview["id"]).preview_json
    assert {row["scheduled_date"] for row in stored["time_preview"]["day_schedule"]} == {
        row["scheduled_date"] for row in preview["allocation"]
    }
    preserved = next(row for row in preview["time_preview"]["after"]
                     if row["date"] == untouched_date and row["title"] == title)
    assert (preserved["start"], preserved["end"]) == ("18:00", "19:00")
    Clock.value += timedelta(minutes=advance_minutes)
    applied = client.post(f"/api/goals/{goal}/execution/recovery/catchup", json={
        "preview_id": preview["id"], "accept_unallocated": True,
    })
    if advance_minutes:
        assert applied.status_code == 409, applied.text
        with session_factory() as session:
            assert session.get(Goal, goal).active_plan_version_id == workspace["plan"]["id"]
            assert session.get(RecoveryOperation, preview["id"]).status == "PREVIEW"
            assert session.scalar(select(func.count()).select_from(PlanVersion)) == 1
        return
    assert applied.status_code == 200, applied.text
    assert applied.json()["time_preview"] == preview["time_preview"]
    with session_factory() as session:
        changed = changed_schedule_dates(
            session, workspace["plan"]["id"], applied.json()["result_plan_version_id"],
        )
    assert {str(day) for day in changed if day >= today} == {
        row["scheduled_date"] for row in preview["allocation"]
    }


def test_missing_slots_require_explicit_decision_and_preview_rolls_back(client, session_factory):
    goal, old_plan = setup_recovery(client)
    preview = client.get(f"/api/goals/{goal}/execution/recovery/shift-preview").json()
    assert preview["time_preview"]["unallocated_count"] > 0
    with session_factory() as session:
        assert session.get(Goal, goal).active_plan_version_id == old_plan
        assert session.scalar(select(func.count()).select_from(PlanVersion)) == 1
        assert session.scalar(select(func.count()).select_from(TaskTimeBlock)) == 0
    denied = client.post(f"/api/goals/{goal}/execution/recovery/shift",
                         json={"preview_id": preview["id"]})
    assert denied.status_code == 409
    accepted = client.post(f"/api/goals/{goal}/execution/recovery/shift", json={
        "preview_id": preview["id"], "accept_unallocated": True,
    })
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["time_preview"] == preview["time_preview"]


def test_new_cross_plan_occupancy_invalidates_recovery_preview(client, session_factory):
    goal, old_plan = setup_recovery(client)
    preview = client.get(f"/api/goals/{goal}/execution/recovery/shift-preview").json()
    day = preview["time_preview"]["after"][0]["date"]
    client.post("/api/manual-plans", json={"title": "临时会议", "tasks": [
        {"title": "会议", "estimate_minutes": 30, "scheduled_date": day,
         "start_local": "19:00", "end_local": "19:30"},
    ]})
    result = client.post(f"/api/goals/{goal}/execution/recovery/shift", json={
        "preview_id": preview["id"], "accept_unallocated": True,
    })
    assert result.status_code == 409
    with session_factory() as session:
        assert session.get(Goal, goal).active_plan_version_id == old_plan
        assert session.scalar(select(func.count()).select_from(PlanVersion)) == 1


def test_published_slots_equal_preview_without_draft_residue(client, session_factory):
    goal, _ = setup_recovery(client)
    client.put(f"/api/goals/{goal}/execution/schedule/availability", json={
        "windows": [{"weekday": i, "start_local": "00:00", "end_local": "23:59"}
                    for i in range(7)],
    })
    preview_response = client.get(f"/api/goals/{goal}/execution/recovery/shift-preview")
    assert preview_response.status_code == 200, preview_response.text
    preview = preview_response.json()
    repeated = client.get(f"/api/goals/{goal}/execution/recovery/shift-preview").json()
    assert preview["id"] == repeated["id"]
    assert any(t["state"] == "SCHEDULED" for t in preview["time_preview"]["after"])
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(TaskTimeBlock)) == 0
    applied = client.post(f"/api/goals/{goal}/execution/recovery/shift", json={
        "preview_id": preview["id"], "accept_unallocated": True,
    })
    assert applied.status_code == 200, applied.text
    for expected in preview["time_preview"]["after"]:
        if expected["state"] != "SCHEDULED":
            continue
        board = client.get(f"/api/today?on_date={expected['date']}").json()
        actual = next(t for t in board["fixed_tasks"] if t["title"] == expected["title"])
        assert (actual["start_local"], actual["end_local"]) == (
            expected["start"], expected["end"],
        )
