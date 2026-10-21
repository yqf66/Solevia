from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import func, select

from planner_api.models import (
    Goal,
    PlanActivation,
    PlanDaySchedule,
    PlanTask,
    PlanVersion,
    ProgressEvent,
)
from test_plan import _completed_plan


@pytest.fixture(autouse=True)
def stable_allocation_clock(monkeypatch):
    # Scope tests compare two allocations. A wall-clock minute advancing
    # between preview and confirmation legitimately changes production slots,
    # but must not turn a lineage test into a timing-dependent failure.
    import planner_api.day_allocation as allocation
    fixed = datetime.now(ZoneInfo("Asia/Shanghai"))
    class StableClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed.astimezone(tz) if tz else fixed.replace(tzinfo=None)
    monkeypatch.setattr(allocation, "datetime", StableClock)


def scoped_recovery(client, session_factory):
    workspace = _completed_plan(client)
    goal, plan = workspace["goal_id"], workspace["plan"]["id"]
    today = datetime.now(ZoneInfo("Asia/Shanghai")).date()
    assert client.post(f"/api/goals/{goal}/plan/confirm").status_code == 200
    start = client.post(
        f"/api/goals/{goal}/execution/start",
        json={
            "requested_start_date": str(today),
            "execution_weekdays": sorted({today.weekday(), (today.weekday() + 1) % 7}),
            "excluded_dates": [],
        },
    )
    assert start.status_code == 200, start.text
    client.patch(f"/api/goals/{goal}/execution/schedule/preferences", json={"mode": "FLEXIBLE"})
    with session_factory() as session:
        activation = session.scalar(
            select(PlanActivation).where(PlanActivation.plan_version_id == plan)
        )
        schedules = list(
            session.scalars(
                select(PlanDaySchedule)
                .where(
                    PlanDaySchedule.activation_id == activation.id,
                )
                .order_by(PlanDaySchedule.scheduled_date)
            )
        )
        for index, schedule in enumerate(schedules):
            schedule.scheduled_date = today + timedelta(days=index - 1)
        activation.projected_end_date = schedules[-1].scheduled_date
        for task in session.scalars(select(PlanTask).where(PlanTask.plan_version_id == plan)):
            task.depends_on_json = []
            task.estimate_minutes = 10
        past = schedules[0]
        tasks = list(
            session.scalars(select(PlanTask).where(PlanTask.plan_day_id == past.plan_day_id))
        )
        if len(tasks) == 1:
            second = PlanTask(
                plan_version_id=plan,
                plan_day_id=past.plan_day_id,
                order_in_day=2,
                title="阅读材料",
                description="范围测试任务",
                estimate_minutes=45,
                priority=1,
                task_kind="LEARN",
                scheduling_policy="FLOATING",
            )
            session.add(second)
            session.flush()
            tasks.append(second)
        for task in tasks[2:]:
            task.status = "CANCELLED"
        first, second = tasks[:2]
        first.title = "范围验收：整理材料"
        second.title = "范围验收：论文阅读"
        second.status = "IN_PROGRESS"
        second.estimate_minutes = 45
        second.depends_on_json = ["未确认的前置材料"]
        session.add(
            ProgressEvent(
                goal_id=goal,
                plan_version_id=plan,
                plan_day_id=past.plan_day_id,
                task_id=second.id,
                event_type="PARTIAL_COMPLETION",
                metadata_json={"amount": 15, "unit": "MINUTES"},
            )
        )
        future = session.scalar(
            select(PlanTask).where(PlanTask.plan_day_id == schedules[-1].plan_day_id)
        )
        ids = first.id, second.id, future.id
        session.commit()
    return goal, plan, ids


def preview_selection(client, goal, plan, ids):
    return client.post(
        f"/api/goals/{goal}/execution/recovery/catchup-preview",
        json={
            "source_plan_version_id": plan,
            "selected_task_ids": list(ids),
        },
    )


def apply(client, goal, preview_id):
    return client.post(
        f"/api/goals/{goal}/execution/recovery/catchup",
        json={
            "preview_id": preview_id,
            "accept_unallocated": True,
        },
    )


def test_narrow_apply_retains_unselected_lineage_partial_history_and_recovery(
    client, session_factory
):
    goal, plan, (first, second, _) = scoped_recovery(client, session_factory)
    full = client.get(f"/api/goals/{goal}/execution/recovery/catchup-preview").json()
    assert not full["programmatic_feasible"]
    response = preview_selection(client, goal, plan, [first])
    assert response.status_code == 200, response.text
    preview = response.json()
    assert preview["programmatic_feasible"]
    scope = preview["recovery_scope"]
    assert scope["selected_task_ids"] == [first]
    assert scope["deferred_task_ids"] == [second]
    assert (scope["selected_minutes"], scope["deferred_minutes"]) == (10, 45)
    assert (
        next(item for item in scope["items"] if item["source_task_id"] == second)[
            "remaining_minutes"
        ]
        == 30
    )
    with session_factory() as session:
        assert session.get(Goal, goal).active_plan_version_id == plan
        assert session.scalar(select(func.count()).select_from(PlanVersion)) == 1
    assert apply(client, goal, full["id"]).status_code == 409
    applied = apply(client, goal, preview["id"])
    assert applied.status_code == 200, applied.text
    new_plan = applied.json()["result_plan_version_id"]
    assert apply(client, goal, preview["id"]).json()["result_plan_version_id"] == new_plan
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(PlanVersion)) == 2
        original = session.get(PlanTask, second)
        retained = session.scalar(
            select(PlanTask).where(
                PlanTask.plan_version_id == new_plan,
                PlanTask.lineage_task_id == second,
            )
        )
        assert retained.status == original.status == "IN_PROGRESS"
        assert retained.depends_on_json == original.depends_on_json
        new_date = session.scalar(
            select(PlanDaySchedule.scheduled_date).where(
                PlanDaySchedule.plan_day_id == retained.plan_day_id,
            )
        )
        old_date = next(
            item["source_date"] for item in scope["items"] if item["source_task_id"] == second
        )
        assert str(new_date) == old_date
        events = list(
            session.scalars(
                select(ProgressEvent).where(
                    ProgressEvent.event_type == "PARTIAL_COMPLETION",
                    ProgressEvent.goal_id == goal,
                )
            )
        )
        assert len(events) == 1 and events[0].metadata_json == {"amount": 15, "unit": "MINUTES"}
        assert session.get(PlanTask, first).status != "COMPLETED"
    remaining = client.get(f"/api/goals/{goal}/execution/recovery/catchup-preview").json()[
        "recovery_scope"
    ]
    assert len(remaining["items"]) == 1
    assert remaining["items"][0]["lineage_task_id"] == second
    assert remaining["items"][0]["remaining_minutes"] == 30


def test_range_repreview_invalidates_old_id_even_when_returning_to_previous_range(
    client, session_factory
):
    goal, plan, (first, second, _) = scoped_recovery(client, session_factory)
    one = preview_selection(client, goal, plan, [first]).json()
    assert preview_selection(client, goal, plan, [first]).json()["id"] == one["id"]
    preview_selection(client, goal, plan, [second])
    assert apply(client, goal, one["id"]).status_code == 409
    again = preview_selection(client, goal, plan, [first]).json()
    assert again["id"] != one["id"]
    assert apply(client, goal, one["id"]).status_code == 409
    assert apply(client, goal, again["id"]).status_code == 200


def test_omitted_prerequisite_is_not_automatically_added_or_bypassed(client, session_factory):
    goal, plan, (first, second, _) = scoped_recovery(client, session_factory)
    with session_factory() as session:
        task = session.get(PlanTask, second)
        task.depends_on_json = [first]
        task.estimate_minutes = 10
        session.commit()
    preview = preview_selection(client, goal, plan, [second]).json()
    assert not preview["programmatic_feasible"]
    assert preview["recovery_scope"]["selected_task_ids"] == [second]
    row = next(
        item for item in preview["recovery_scope"]["items"] if item["source_task_id"] == second
    )
    assert row["prerequisite_task_ids"] == [first]
    assert apply(client, goal, preview["id"]).status_code == 409


@pytest.mark.parametrize(
    "invalid", ["empty", "duplicate", "unknown", "future", "completed", "wrong_plan"]
)
def test_scope_rejects_invalid_source_identity(client, session_factory, invalid):
    goal, plan, (first, _, future) = scoped_recovery(client, session_factory)
    ids, expected = [first], 422
    if invalid == "empty":
        ids = []
    elif invalid == "duplicate":
        ids = [first, first]
    elif invalid == "unknown":
        ids = ["not-a-source-task"]
    elif invalid == "future":
        ids = [future]
    elif invalid == "completed":
        with session_factory() as session:
            session.get(PlanTask, first).status = "COMPLETED"
            session.commit()
    elif invalid == "wrong_plan":
        plan, expected = "different-plan", 409
    assert preview_selection(client, goal, plan, ids).status_code == expected


@pytest.mark.parametrize("changed", ["selected_completed", "deferred_progress"])
def test_history_change_invalidates_confirmation(client, session_factory, changed):
    goal, plan, (first, second, _) = scoped_recovery(client, session_factory)
    preview = preview_selection(client, goal, plan, [first]).json()
    with session_factory() as session:
        if changed == "selected_completed":
            session.get(PlanTask, first).status = "COMPLETED"
        else:
            event = session.scalar(select(ProgressEvent).where(ProgressEvent.task_id == second))
            event.metadata_json = {"amount": 20, "unit": "MINUTES"}
        session.commit()
    assert apply(client, goal, preview["id"]).status_code == 409
    with session_factory() as session:
        assert session.get(Goal, goal).active_plan_version_id == plan


def test_catchup_preview_cannot_be_applied_as_whole_shift(client, session_factory):
    goal, plan, (first, _, _) = scoped_recovery(client, session_factory)
    preview = preview_selection(client, goal, plan, [first]).json()
    assert (
        client.post(
            f"/api/goals/{goal}/execution/recovery/shift",
            json={
                "preview_id": preview["id"],
                "accept_unallocated": True,
            },
        ).status_code
        == 404
    )
