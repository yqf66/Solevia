from fastapi.testclient import TestClient


def _goal(client: TestClient) -> str:
    response = client.post("/api/goals", json={"raw_input": "安排一个学习计划"})
    assert response.status_code == 201
    return response.json()["goal_id"]


def test_schedule_foundation_defaults_to_flexible(client: TestClient) -> None:
    goal_id = _goal(client)
    response = client.get(f"/api/goals/{goal_id}/execution/schedule")
    assert response.status_code == 200
    body = response.json()
    assert body["preference"]["mode"] == "FLEXIBLE"
    assert body["preference"]["timezone"] == "Asia/Shanghai"
    assert body["availability_windows"] == []


def test_schedule_preference_and_windows_are_updated(client: TestClient) -> None:
    goal_id = _goal(client)
    preference = client.patch(
        f"/api/goals/{goal_id}/execution/schedule/preferences",
        json={"mode": "BLOCKS", "break_minutes": 15},
    )
    assert preference.status_code == 200
    assert preference.json()["mode"] == "BLOCKS"
    windows = client.put(
        f"/api/goals/{goal_id}/execution/schedule/availability",
        json={
            "windows": [
                {"weekday": 1, "start_local": "19:00", "end_local": "21:00"},
                {"weekday": 6, "start_local": "09:30", "end_local": "11:00"},
            ]
        },
    )
    assert windows.status_code == 200
    assert [item["start_local"] for item in windows.json()] == ["19:00", "09:30"]

    foundation = client.get(f"/api/goals/{goal_id}/execution/schedule").json()
    assert foundation["preference"]["mode"] == "BLOCKS"
    assert len(foundation["availability_windows"]) == 2


def test_schedule_rejects_invalid_time_window(client: TestClient) -> None:
    goal_id = _goal(client)
    response = client.put(
        f"/api/goals/{goal_id}/execution/schedule/availability",
        json={"windows": [{"weekday": 1, "start_local": "21:00", "end_local": "20:00"}]},
    )
    assert response.status_code == 422
