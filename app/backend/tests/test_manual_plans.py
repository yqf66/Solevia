from fastapi.testclient import TestClient

from planner_api.holiday_calendar import suggest_holiday_region


def test_workspace_schedule_profile_is_shared(client: TestClient) -> None:
    response = client.get("/api/schedule/profile")
    assert response.status_code == 200
    assert response.json()["id"] == "default"
    updated = client.patch(
        "/api/schedule/profile",
        json={"timezone": "Asia/Tokyo", "default_mode": "BLOCKS"},
    )
    assert updated.status_code == 200
    assert updated.json()["timezone"] == "Asia/Tokyo"
    assert updated.json()["default_mode"] == "BLOCKS"

    windows = client.put(
        "/api/schedule/profile/availability",
        json={"windows": [{"weekday": 1, "start_local": "19:00", "end_local": "21:00"}]},
    )
    assert windows.status_code == 200
    assert windows.json()[0]["profile_id"] == "default"
    stored = client.get("/api/schedule/profile/availability")
    assert stored.status_code == 200
    assert [
        (item["weekday"], item["start_local"], item["end_local"]) for item in stored.json()
    ] == [(1, "19:00", "21:00")]


def test_china_holiday_calendar_contains_holidays_and_makeup_workdays(client: TestClient) -> None:
    updated = client.patch(
        "/api/schedule/profile",
        json={
            "holiday_country_code": "CN",
            "holiday_locale": "zh-CN",
            "holiday_source": "AUTO",
            "holiday_confidence": 0.98,
        },
    )
    assert updated.status_code == 200
    assert updated.json()["holiday_country_code"] == "CN"

    response = client.get(
        "/api/schedule/profile/holidays",
        params={"start_date": "2026-09-20", "end_date": "2026-10-10"},
    )
    assert response.status_code == 200
    by_date = {item["date"]: item for item in response.json()["days"]}
    assert by_date["2026-09-20"]["kind"] == "WORKDAY"
    assert by_date["2026-09-25"]["kind"] == "HOLIDAY"
    assert by_date["2026-10-10"]["kind"] == "WORKDAY"
    assert by_date["2026-09-25"]["authoritative"] is True

    unavailable = client.get(
        "/api/schedule/profile/holidays",
        params={"start_date": "2027-01-01", "end_date": "2027-12-31"},
    ).json()
    assert unavailable["coverage"] == "UNAVAILABLE"
    assert unavailable["missing_years"] == [2027]


def test_holiday_region_suggestion_requires_agreement_before_auto_apply() -> None:
    agreed = suggest_holiday_region("Asia/Shanghai", "zh-CN")
    assert agreed["country_code"] == "CN"
    assert agreed["requires_confirmation"] is False
    assert agreed["confidence"] == 0.98

    conflict = suggest_holiday_region("Asia/Tokyo", "zh-CN")
    assert conflict["timezone_country_code"] == "JP"
    assert conflict["locale_country_code"] == "CN"
    assert conflict["requires_confirmation"] is True


def test_push_fixed_task_relocates_displaced_tasks_inside_shared_availability(
    client: TestClient,
) -> None:
    client.put(
        "/api/schedule/profile/availability",
        json={"windows": [{"weekday": 4, "start_local": "13:00", "end_local": "17:00"}]},
    )
    old = client.post(
        "/api/manual-plans",
        json={
            "title": "旧安排",
            "tasks": [
                {
                    "title": "阅读",
                    "estimate_minutes": 30,
                    "scheduled_date": "2026-09-11",
                    "start_local": "14:00",
                    "end_local": "14:30",
                },
                {
                    "title": "复盘",
                    "estimate_minutes": 30,
                    "scheduled_date": "2026-09-11",
                    "start_local": "14:15",
                    "end_local": "14:45",
                },
            ],
            "conflict_mode": "PARALLEL",
        },
    )
    assert old.status_code == 201
    inserted = client.post(
        "/api/manual-plans",
        json={
            "title": "固定健身",
            "tasks": [
                {
                    "title": "健身",
                    "estimate_minutes": 60,
                    "scheduled_date": "2026-09-11",
                    "start_local": "14:00",
                    "end_local": "15:00",
                }
            ],
            "conflict_mode": "PUSH",
        },
    )
    assert inserted.status_code == 201
    board = client.get("/api/today?on_date=2026-09-11").json()
    by_title = {item["title"]: item for item in board["fixed_tasks"] + board["flexible_tasks"]}
    assert (by_title["阅读"]["start_local"], by_title["阅读"]["end_local"]) == ("13:00", "13:30")
    assert (by_title["复盘"]["start_local"], by_title["复盘"]["end_local"]) == ("13:30", "14:00")
    assert (by_title["健身"]["start_local"], by_title["健身"]["end_local"]) == ("14:00", "15:00")


def test_push_fixed_task_falls_back_to_flexible_without_complete_slot(client: TestClient) -> None:
    client.put(
        "/api/schedule/profile/availability",
        json={"windows": [{"weekday": 4, "start_local": "14:00", "end_local": "15:00"}]},
    )
    old = client.post(
        "/api/manual-plans",
        json={
            "title": "旧安排",
            "tasks": [
                {
                    "title": "阅读",
                    "estimate_minutes": 30,
                    "scheduled_date": "2026-09-11",
                    "start_local": "14:00",
                    "end_local": "14:30",
                }
            ],
        },
    )
    assert old.status_code == 201
    inserted = client.post(
        "/api/manual-plans",
        json={
            "title": "固定健身",
            "tasks": [
                {
                    "title": "健身",
                    "estimate_minutes": 60,
                    "scheduled_date": "2026-09-11",
                    "start_local": "14:00",
                    "end_local": "15:00",
                }
            ],
            "conflict_mode": "PUSH",
        },
    )
    assert inserted.status_code == 201
    board = client.get("/api/today?on_date=2026-09-11").json()
    assert any(
        item["title"] == "阅读" and item["start_local"] is None for item in board["flexible_tasks"]
    )


def test_manual_plan_can_be_created_without_agent_flow(client: TestClient) -> None:
    response = client.post(
        "/api/manual-plans",
        json={
            "title": "本周个人事务",
            "description": "不经过 Agent 直接建立",
            "tasks": [
                {
                    "title": "整理资料",
                    "estimate_minutes": 45,
                    "scheduled_date": "2026-09-09",
                    "start_local": "19:00",
                    "end_local": "19:45",
                },
                {"title": "阅读文档", "estimate_minutes": 30},
            ],
        },
    )
    assert response.status_code == 201
    body = response.json()
    assert body["title"] == "本周个人事务"
    assert [task["order_in_plan"] for task in body["tasks"]] == [1, 2]
    assert body["tasks"][0]["start_local"] == "19:00"

    listed = client.get("/api/manual-plans")
    assert listed.status_code == 200
    assert any(item["id"] == body["id"] for item in listed.json())


def test_manual_task_rejects_partial_time_block(client: TestClient) -> None:
    response = client.post(
        "/api/manual-plans",
        json={
            "title": "不完整任务",
            "tasks": [{"title": "任务", "estimate_minutes": 20, "start_local": "10:00"}],
        },
    )
    assert response.status_code == 422


def test_recurring_manual_task_can_override_only_one_date(client: TestClient) -> None:
    created = client.post(
        "/api/manual-plans",
        json={
            "title": "晨练",
            "tasks": [
                {
                    "title": "跑步",
                    "estimate_minutes": 30,
                    "scheduled_date": "2026-09-11",
                    "start_local": "07:00",
                    "end_local": "07:30",
                    "recurrence": "DAILY",
                }
            ],
        },
    ).json()
    task = created["tasks"][0]
    changed = client.patch(
        f"/api/manual-plans/{created['id']}/tasks/{task['id']}",
        json={
            "scope": "TODAY",
            "scheduled_date": "2026-09-12",
            "title": "慢跑",
            "start_local": "08:00",
            "end_local": "08:30",
            "status": "COMPLETED",
        },
    )
    assert changed.status_code == 200
    one_day = client.get("/api/today?on_date=2026-09-12").json()["fixed_tasks"][0]
    next_day = client.get("/api/today?on_date=2026-09-13").json()["fixed_tasks"][0]
    assert (one_day["title"], one_day["start_local"], one_day["status"]) == (
        "慢跑",
        "08:00",
        "COMPLETED",
    )
    assert (next_day["title"], next_day["start_local"], next_day["status"]) == (
        "跑步",
        "07:00",
        "NOT_STARTED",
    )


def test_recurring_manual_task_can_delete_only_one_date(client: TestClient) -> None:
    created = client.post(
        "/api/manual-plans",
        json={
            "title": "作息",
            "tasks": [
                {
                    "title": "拉伸",
                    "estimate_minutes": 15,
                    "scheduled_date": "2026-09-11",
                    "start_local": "22:00",
                    "end_local": "22:15",
                    "recurrence": "DAILY",
                }
            ],
        },
    ).json()
    task = created["tasks"][0]
    deleted = client.request(
        "DELETE",
        f"/api/manual-plans/{created['id']}/tasks/{task['id']}",
        json={"scope": "TODAY", "override_date": "2026-09-12"},
    )
    assert deleted.status_code == 200
    assert client.get("/api/today?on_date=2026-09-12").json()["fixed_tasks"] == []
    assert client.get("/api/today?on_date=2026-09-13").json()["fixed_tasks"][0]["title"] == "拉伸"


def test_recurring_manual_task_future_edit_splits_rule(client: TestClient) -> None:
    created = client.post(
        "/api/manual-plans",
        json={
            "title": "晚间",
            "tasks": [
                {
                    "title": "阅读",
                    "estimate_minutes": 20,
                    "scheduled_date": "2026-09-11",
                    "start_local": "21:00",
                    "end_local": "21:20",
                    "recurrence": "DAILY",
                }
            ],
        },
    ).json()
    task = created["tasks"][0]
    changed = client.patch(
        f"/api/manual-plans/{created['id']}/tasks/{task['id']}",
        json={
            "scope": "FUTURE",
            "scheduled_date": "2026-09-13",
            "title": "写日记",
            "start_local": "21:30",
            "end_local": "21:50",
        },
    )
    assert changed.status_code == 200
    assert client.get("/api/today?on_date=2026-09-12").json()["fixed_tasks"][0]["title"] == "阅读"
    future = client.get("/api/today?on_date=2026-09-13").json()["fixed_tasks"][0]
    assert (future["title"], future["start_local"]) == ("写日记", "21:30")
