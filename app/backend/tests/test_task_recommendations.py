from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import func, select
from test_execution_calendar import _started

from planner_api.models import (
    PlanDay,
    PlanTask,
    PlanWorkItem,
    ProgressEvent,
    RecoveryOperation,
    TaskTimeBlock,
    WorkItemWindow,
)


@pytest.fixture
def recommendation_goal(client, session_factory, monkeypatch):
    now = datetime.now(ZoneInfo("Asia/Shanghai")).replace(hour=8, minute=0)

    class Clock:
        @staticmethod
        def now(tz):
            return now.astimezone(tz)

    monkeypatch.setattr("planner_api.day_allocation.datetime", Clock)
    goal_id, days = _started(client)
    today = str(now.date())
    with session_factory() as session:
        tasks = list(session.scalars(select(PlanTask)))
        for task in tasks:
            task.status = "COMPLETED"
        session.commit()
    response = client.put(
        f"/api/goals/{goal_id}/execution/schedule/availability",
        json={
            "windows": [{"weekday": now.weekday(), "start_local": "19:00", "end_local": "21:00"}],
        },
    )
    assert response.status_code == 200
    return goal_id, today, days


def candidates(session_factory, days, count=1, **updates):
    future = days[1:]
    with session_factory() as session:
        rows = list(
            session.scalars(
                select(PlanTask)
                .where(
                    PlanTask.plan_day_id.in_([day["plan_day_id"] for day in future]),
                )
                .order_by(PlanTask.plan_day_id, PlanTask.order_in_day)
                .limit(count)
            )
        )
        assert len(rows) == count
        for index, task in enumerate(rows, start=1):
            task.status = "NOT_STARTED"
            task.title = f"任务 {index}"
            task.scheduling_policy = "FLOATING"
            task.scheduling_policy_json = {}
            task.estimate_minutes = 30
            task.depends_on_json = []
            task.satisfies_commitments_json = []
            for name, value in updates.items():
                setattr(task, name, value)
        ids = [task.id for task in rows]
        session.commit()
        return ids


def preview(client, goal_id, **request):
    response = client.post(f"/api/goals/{goal_id}/execution/recommendations/preview", json=request)
    assert response.status_code == 200, response.text
    return response.json()


def test_recommendation_is_read_only_and_reuses_persisted_snapshot(
    client, session_factory, recommendation_goal
):
    goal_id, today, days = recommendation_goal
    ids = candidates(session_factory, days, 2)
    with session_factory() as session:
        before = [(t.id, t.plan_day_id, t.status) for t in session.scalars(select(PlanTask))]
        block_count = session.scalar(select(func.count()).select_from(TaskTimeBlock))
        event_count = session.scalar(select(func.count()).select_from(ProgressEvent))
    result = preview(client, goal_id)
    assert result["strategy"] == "RULES"
    assert result["date"] == today
    assert {item["task_id"] for item in result["items"]} == set(ids)
    assert result["capacity"]["recommended_minutes"] == 60
    assert preview(client, goal_id)["id"] == result["id"]
    with session_factory() as session:
        assert [
            (t.id, t.plan_day_id, t.status) for t in session.scalars(select(PlanTask))
        ] == before
        assert session.scalar(select(func.count()).select_from(TaskTimeBlock)) == block_count
        assert session.scalar(select(func.count()).select_from(ProgressEvent)) == event_count
        assert (
            session.scalar(
                select(func.count())
                .select_from(RecoveryOperation)
                .where(
                    RecoveryOperation.mode == "TASK_RECOMMENDATION",
                )
            )
            == 1
        )


def test_repeated_series_releases_next_and_current_day_suppresses_duplicates(
    client, session_factory, recommendation_goal
):
    goal_id, today, days = recommendation_goal
    ids = candidates(session_factory, days, 3)
    with session_factory() as session:
        for index, task_id in enumerate(ids, start=1):
            session.get(PlanTask, task_id).title = f"阅读第{index}次"
        session.commit()
    result = preview(client, goal_id)
    assert len(result["items"]) == 1
    first = result["items"][0]["task_id"]
    with session_factory() as session:
        session.get(PlanTask, first).status = "COMPLETED"
        session.commit()
    second = preview(client, goal_id)
    assert len(second["items"]) == 1 and second["items"][0]["task_id"] != first
    assert second["fingerprint"] != result["fingerprint"]
    current_id = next(day["plan_day_id"] for day in days if day["scheduled_date"] == today)
    with session_factory() as session:
        task = session.get(PlanTask, second["items"][0]["task_id"])
        task.plan_day_id = current_id
        session.commit()
    result = preview(client, goal_id)
    assert not result["items"]
    assert any(
        item["reason_code"] == "CURRENT_SERIES_PENDING" for item in result["blocked_summary"]
    )


@pytest.mark.parametrize(
    ("rules", "code"),
    [
        ({"not_before": "2099-01-01"}, "WINDOW_NOT_OPEN"),
        ({"not_after": "2000-01-01"}, "WINDOW_EXPIRED"),
        ({"not_before": "bad-date"}, "INVALID_RULE"),
        ({"minimum_gap_days": -1}, "INVALID_RULE"),
        ({"maximum_per_day": 0}, "INVALID_RULE"),
        ({"allowed_weekdays": "Friday"}, "INVALID_RULE"),
        ({"excluded_dates": "2026-10-08"}, "INVALID_RULE"),
        ({"allowed_weekdays": [], "excluded_dates": []}, None),
    ],
)
def test_window_filter(client, session_factory, recommendation_goal, rules, code):
    goal_id, _, days = recommendation_goal
    candidates(session_factory, days, scheduling_policy_json=rules)
    result = preview(client, goal_id)
    if code:
        assert not result["items"] and result["blocked_summary"][0]["reason_code"] == code
    else:
        assert result["items"]


def test_unknown_and_unfinished_dependencies_are_not_actionable(
    client, session_factory, recommendation_goal
):
    goal_id, _, days = recommendation_goal
    ids = candidates(session_factory, days, 2)
    with session_factory() as session:
        session.get(PlanTask, ids[0]).depends_on_json = ["unknown-predecessor"]
        session.get(PlanTask, ids[1]).depends_on_json = [ids[0]]
        session.commit()
    result = preview(client, goal_id)
    assert not result["items"]
    assert {item["reason_code"] for item in result["blocked_summary"]} == {
        "UNRESOLVED_DEPENDENCY",
        "DEPENDENCY_BLOCKED",
    }


def test_combination_fits_breaks_and_prioritizes_existing_tasks(
    client, session_factory, recommendation_goal
):
    goal_id, today, days = recommendation_goal
    candidates(session_factory, days, 3, estimate_minutes=40)
    client.put(
        f"/api/goals/{goal_id}/execution/schedule/availability",
        json={
            "windows": [
                {
                    "weekday": datetime.fromisoformat(today).weekday(),
                    "start_local": "19:00",
                    "end_local": "20:00",
                }
            ],
        },
    )
    result = preview(client, goal_id)
    assert len(result["items"]) == 1
    assert result["capacity"]["recommended_minutes"] == 40
    with session_factory() as session:
        current = session.scalar(
            select(PlanTask).where(
                PlanTask.plan_day_id
                == next(day["plan_day_id"] for day in days if day["scheduled_date"] == today),
            )
        )
        current.status = "NOT_STARTED"
        current.estimate_minutes = 50
        current.depends_on_json = []
        session.commit()
    result = preview(client, goal_id)
    assert not result["items"]
    assert result["capacity"]["current_task_minutes"] == 50


def test_fragmented_capacity_and_no_availability(client, session_factory, recommendation_goal):
    goal_id, today, days = recommendation_goal
    candidates(session_factory, days, estimate_minutes=35)
    client.put(
        f"/api/goals/{goal_id}/execution/schedule/availability",
        json={
            "windows": [
                {
                    "weekday": datetime.fromisoformat(today).weekday(),
                    "start_local": "19:00",
                    "end_local": "19:20",
                },
                {
                    "weekday": datetime.fromisoformat(today).weekday(),
                    "start_local": "20:00",
                    "end_local": "20:20",
                },
            ]
        },
    )
    result = preview(client, goal_id)
    assert not result["items"]
    assert result["blocked_summary"][0]["reason_code"] == "CONTINUOUS_SLOT_REQUIRED"
    client.put(f"/api/goals/{goal_id}/execution/schedule/availability", json={"windows": []})
    assert not preview(client, goal_id)["items"]


def test_future_cadence_and_nonflexible_types_excluded(
    client, session_factory, recommendation_goal
):
    goal_id, _, days = recommendation_goal
    ids = candidates(session_factory, days, 2)
    with session_factory() as session:
        task = session.get(PlanTask, ids[0])
        task.scheduling_policy = "CADENCE"
        task.scheduling_policy_json = {"start_week": 3}
        task.plan_day_id = session.scalar(
            select(PlanDay.id).where(
                PlanDay.plan_version_id == task.plan_version_id,
                PlanDay.week_index == 3,
            )
        )
        session.get(PlanTask, ids[1]).scheduling_policy = "TRIGGERED"
        session.commit()
    result = preview(client, goal_id)
    assert not result["items"]
    assert len(result["blocked_summary"]) == 1
    assert result["blocked_summary"][0]["reason_code"] == "WINDOW_NOT_OPEN"


def test_real_work_item_spacing_and_quota(client, session_factory, recommendation_goal):
    goal_id, today, days = recommendation_goal
    ids = candidates(session_factory, days, 2)
    with session_factory() as session:
        task = session.get(PlanTask, ids[0])
        day = session.get(PlanDay, task.plan_day_id)
        item = PlanWorkItem(
            plan_version_id=task.plan_version_id,
            item_key="reading",
            scope_type="PLAN",
            title="阅读",
            description="定向阅读",
            task_kind="PRACTICE",
            estimate_minutes=30,
            priority=2,
            status="ACTIVE",
            constraints_json={"recurrence": {"minimum_gap_days": 1}},
        )
        session.add(item)
        session.flush()
        activation_id = session.scalar(select(WorkItemWindow.activation_id))
        if not activation_id:
            from planner_api.models import PlanActivation

            activation_id = session.scalar(select(PlanActivation.id))
        window = WorkItemWindow(
            activation_id=activation_id,
            work_item_id=item.id,
            period_key="week-1",
            eligible_from=datetime.fromisoformat(today).date(),
            eligible_to=datetime.fromisoformat(today).date() + timedelta(days=6),
            required_value=2,
        )
        session.add(window)
        session.flush()
        for index, task_id in enumerate(ids, start=1):
            peer = session.get(PlanTask, task_id)
            peer.source_work_item_id = item.id
            peer.work_item_window_id = window.id
            peer.occurrence_index = index
        task.status = "COMPLETED"
        session.add(
            ProgressEvent(
                goal_id=goal_id,
                plan_version_id=task.plan_version_id,
                plan_day_id=day.id,
                task_id=task.id,
                event_type="COMPLETED",
                occurred_at=datetime.fromisoformat(today).replace(
                    hour=7, tzinfo=ZoneInfo("Asia/Shanghai")
                ),
            )
        )
        session.commit()
        item_id, window_id = item.id, window.id
    result = preview(client, goal_id)
    assert not result["items"]
    assert result["blocked_summary"][0]["reason_code"] == "SPACING_REQUIRED"
    with session_factory() as session:
        session.get(PlanWorkItem, item_id).constraints_json = {}
        session.commit()
    result = preview(client, goal_id)
    assert len(result["items"]) == 1 and result["items"][0]["remaining_value"] == 1
    with session_factory() as session:
        session.get(WorkItemWindow, window_id).required_value = 1
        session.commit()
    result = preview(client, goal_id)
    assert not result["items"]
    assert result["blocked_summary"][0]["reason_code"] == "QUOTA_SATISFIED"


def test_preview_request_is_bounded_and_date_is_current(client, recommendation_goal):
    goal_id, today, _ = recommendation_goal
    url = f"/api/goals/{goal_id}/execution/recommendations/preview"
    assert client.post(url, json={"max_items": 4}).status_code == 422
    ai_result = client.post(url, json={"use_ai": True})
    assert ai_result.status_code == 200
    assert ai_result.json()["strategy"] == "RULES"
    assert "未请求模型" in ai_result.json()["warnings"][0]
    assert client.post(url, json={"date": "2099-01-01"}).status_code == 422
    assert client.post(url, json={"date": today}).status_code == 200


def test_rest_day_reads_plan_availability_without_today_tasks(
    client, session_factory, recommendation_goal
):
    goal_id, today, days = recommendation_goal
    ids = candidates(session_factory, days)
    current_id = next(day["plan_day_id"] for day in days if day["scheduled_date"] == today)
    future_id = next(day["plan_day_id"] for day in days if day["scheduled_date"] > today)
    with session_factory() as session:
        for task in session.scalars(select(PlanTask).where(PlanTask.plan_day_id == current_id)):
            task.plan_day_id = future_id
        session.commit()
    result = preview(client, goal_id)
    assert result["capacity"]["spare_minutes"] == 120
    assert result["items"][0]["task_id"] == ids[0]


def test_other_plan_pending_work_reserves_shared_capacity(
    client, session_factory, recommendation_goal
):
    goal_id, today, days = recommendation_goal
    candidates(session_factory, days, estimate_minutes=40)
    other_id, other_days = _started(client)
    with session_factory() as session:
        current_id = next(
            day["plan_day_id"] for day in other_days if day["scheduled_date"] == today
        )
        for index, task in enumerate(
            session.scalars(
                select(PlanTask).where(
                    PlanTask.plan_day_id == current_id,
                )
            )
        ):
            task.status = "NOT_STARTED" if index == 0 else "COMPLETED"
            task.estimate_minutes = 100
            task.depends_on_json = []
        session.commit()
    assert other_id != goal_id
    result = preview(client, goal_id)
    assert not result["items"]
    assert result["capacity"]["current_task_minutes"] == 100


@pytest.mark.parametrize("mode", ["unknown", "duplicate", "overcount", "overcapacity", "timeout"])
def test_ai_invalid_results_fall_back_to_rules(
    client, session_factory, recommendation_goal, monkeypatch, mode
):
    from planner_api.recommendation_schemas import (
        RecommendationModelResult,
        RecommendationModelSelection,
    )

    goal_id, today, days = recommendation_goal
    ids = candidates(session_factory, days, 3, estimate_minutes=40)
    client.put(
        f"/api/goals/{goal_id}/execution/schedule/availability",
        json={
            "windows": [
                {
                    "weekday": datetime.fromisoformat(today).weekday(),
                    "start_local": "19:00",
                    "end_local": "20:00",
                }
            ]
        },
    )

    class Provider:
        def __init__(self, _settings):
            pass

        def rank(self, pool, instruction, max_items, **_kwargs):
            assert set(item["task_id"] for item in pool) == set(ids)
            if mode == "timeout":
                from planner_api.ai import ProviderUnavailableError

                raise ProviderUnavailableError("timeout")
            selected = ["unknown"] if mode == "unknown" else ids[:2]
            if mode == "duplicate":
                selected = [ids[0], ids[0]]
            if mode == "overcount":
                selected = ids
            return RecommendationModelResult(
                selections=[
                    RecommendationModelSelection(task_id=task_id, rationale="测试理由")
                    for task_id in selected
                ]
            )

    monkeypatch.setattr("planner_api.recommendation_service.RecommendationProvider", Provider)
    result = preview(client, goal_id, use_ai=True, max_items=2 if mode == "overcount" else 3)
    assert result["strategy"] == "AI_FALLBACK"
    assert len(result["items"]) == 1
    assert result["capacity"]["recommended_minutes"] == 40
    assert result["warnings"]


def test_ai_selection_cache_and_empty_result(
    client, session_factory, recommendation_goal, monkeypatch
):
    from planner_api.recommendation_schemas import (
        RecommendationModelResult,
        RecommendationModelSelection,
    )

    goal_id, _, days = recommendation_goal
    ids = candidates(session_factory, days, 3)
    calls = []

    class Provider:
        def __init__(self, _settings):
            pass

        def rank(self, pool, instruction, max_items, **_kwargs):
            calls.append(instruction)
            return RecommendationModelResult(
                selections=[]
                if instruction == "休息"
                else [RecommendationModelSelection(task_id=ids[-1], rationale="符合今日偏好")],
                summary="先完成一项",
            )

    monkeypatch.setattr("planner_api.recommendation_service.RecommendationProvider", Provider)
    result = preview(client, goal_id, use_ai=True, instruction="优先第3项")
    assert result["strategy"] == "AI"
    assert [item["task_id"] for item in result["items"]] == [ids[-1]]
    assert result["items"][0]["reason"] == "符合今日偏好"
    assert preview(client, goal_id, use_ai=True, instruction="优先第3项")["id"] == result["id"]
    assert len(calls) == 1
    empty = preview(client, goal_id, use_ai=True, instruction="休息")
    assert empty["strategy"] == "AI" and not empty["items"]
    from planner_api.recommendation_service import get_settings

    changed = get_settings().model_copy(update={"llm_max_output_tokens": 1000})
    monkeypatch.setattr("planner_api.recommendation_service.get_settings", lambda: changed)
    refreshed = preview(client, goal_id, use_ai=True, instruction="优先第3项")
    assert refreshed["strategy"] == "AI" and refreshed["id"] != result["id"]
    assert len(calls) == 3  # Initial selection, rest preference, changed policy.
    assert preview(client, goal_id, use_ai=True, instruction="优先第3项") == refreshed
    assert len(calls) == 3


def test_ai_rejects_state_changed_during_request(
    client, session_factory, recommendation_goal, monkeypatch
):
    from planner_api.recommendation_schemas import (
        RecommendationModelResult,
        RecommendationModelSelection,
    )

    goal_id, _, days = recommendation_goal
    task_id = candidates(session_factory, days)[0]

    class Provider:
        def __init__(self, _settings):
            pass

        def rank(self, *_args, **_kwargs):
            with session_factory() as session:
                session.get(PlanTask, task_id).status = "COMPLETED"
                session.commit()
            return RecommendationModelResult(
                selections=[RecommendationModelSelection(task_id=task_id, rationale="已过时")]
            )

    monkeypatch.setattr("planner_api.recommendation_service.RecommendationProvider", Provider)
    response = client.post(
        f"/api/goals/{goal_id}/execution/recommendations/preview", json={"use_ai": True}
    )
    assert response.status_code == 409


def test_ai_fallback_can_retry_then_reuse_success(
    client, session_factory, recommendation_goal, monkeypatch
):
    from planner_api.ai import ProviderUnavailableError
    from planner_api.recommendation_schemas import (
        RecommendationModelResult,
        RecommendationModelSelection,
    )

    goal_id, _, days = recommendation_goal
    task_id = candidates(session_factory, days)[0]
    calls = []

    class Provider:
        def __init__(self, _settings):
            pass

        def rank(self, *_args, **_kwargs):
            calls.append(1)
            if len(calls) == 1:
                raise ProviderUnavailableError("timeout")
            return RecommendationModelResult(
                selections=[RecommendationModelSelection(task_id=task_id, rationale="优先处理")]
            )

    monkeypatch.setattr("planner_api.recommendation_service.RecommendationProvider", Provider)
    first = preview(client, goal_id, use_ai=True)
    assert first["strategy"] == "AI_FALLBACK"
    second = preview(client, goal_id, use_ai=True)
    assert second["strategy"] == "AI" and second["id"] == first["id"]
    third = preview(client, goal_id, use_ai=True)
    assert third == second and len(calls) == 2
