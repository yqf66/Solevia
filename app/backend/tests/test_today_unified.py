from fastapi.testclient import TestClient


def test_unified_today_is_available_even_without_active_plans(client: TestClient) -> None:
    response = client.get("/api/today?on_date=2026-09-09")
    assert response.status_code == 200
    body = response.json()
    assert body["date"] == "2026-09-09"
    assert body["fixed_tasks"] == []
    assert body["flexible_tasks"] == []


def test_manual_plan_task_is_projected_to_unified_today(client: TestClient) -> None:
    created = client.post(
        "/api/manual-plans",
        json={
            "title": "跨规划 Today",
            "tasks": [
                {
                    "title": "固定任务",
                    "estimate_minutes": 30,
                    "scheduled_date": "2026-09-09",
                    "start_local": "09:00",
                    "end_local": "09:30",
                },
                {
                    "title": "自由任务",
                    "estimate_minutes": 20,
                    "scheduled_date": "2026-09-09",
                },
            ],
        },
    )
    assert created.status_code == 201
    today = client.get("/api/today?on_date=2026-09-09")
    assert today.status_code == 200
    body = today.json()
    assert [item["title"] for item in body["fixed_tasks"]] == ["固定任务"]
    assert [item["title"] for item in body["flexible_tasks"]] == ["自由任务"]


def test_recurring_manual_tasks_reserve_their_fixed_time(client: TestClient) -> None:
    created = client.post(
        "/api/manual-plans",
        json={
            "title": "固定生活安排",
            "tasks": [
                {
                    "title": "健身",
                    "estimate_minutes": 60,
                    "scheduled_date": "2026-09-07",
                    "start_local": "18:00",
                    "end_local": "19:00",
                    "recurrence": "WEEKLY",
                    "recurrence_weekdays": [0, 2, 4],
                    "recurrence_end_date": "2026-10-31",
                }
            ],
        },
    )
    assert created.status_code == 201
    assert created.json()["tasks"][0]["recurrence"] == "WEEKLY"

    monday = client.get("/api/today?on_date=2026-09-14").json()
    tuesday = client.get("/api/today?on_date=2026-09-15").json()
    assert [item["title"] for item in monday["fixed_tasks"]] == ["健身"]
    assert tuesday["fixed_tasks"] == []


def test_daily_manual_task_stops_after_recurrence_end(client: TestClient) -> None:
    client.post(
        "/api/manual-plans",
        json={
            "title": "每天作息",
            "tasks": [
                {
                    "title": "晚饭",
                    "estimate_minutes": 30,
                    "scheduled_date": "2026-09-09",
                    "start_local": "18:30",
                    "end_local": "19:00",
                    "recurrence": "DAILY",
                    "recurrence_end_date": "2026-09-10",
                }
            ],
        },
    )
    assert client.get("/api/today?on_date=2026-09-10").json()["fixed_tasks"]
    assert client.get("/api/today?on_date=2026-09-11").json()["fixed_tasks"] == []
