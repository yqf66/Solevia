"""Task-level dependency guards apply independently of work-item edges."""
from test_execution_discussion import _started_goal

from planner_api.models import PlanDay, PlanTask


def test_task_edges_block_start_partial_and_complete_without_mutating_state(client, session_factory):
    goal_id = _started_goal(client)
    today = client.get(f"/api/goals/{goal_id}/today").json()
    first_id = today["day"]["tasks"][0]["id"]
    with session_factory() as session:
        first = session.get(PlanTask, first_id)
        day = session.get(PlanDay, first.plan_day_id)
        second = PlanTask(plan_version_id=first.plan_version_id, plan_day_id=day.id,
                          order_in_day=99, title="写调用备忘", description="依赖走读任务",
                          estimate_minutes=10, priority=1, task_kind="LEARN", depends_on_json=[
                              f"M{day.stage_order}-W{day.week_index}-D{day.day_index}-T{first.order_in_day}"],
                          scheduling_policy="DATE_WINDOW")
        session.add(second)
        session.commit()
        second_id = second.id
    path = f"/api/goals/{goal_id}/tasks/{second_id}"
    for suffix, payload in [("start", {}), ("complete", {}),
                            ("progress", {"action": "PARTIAL", "amount": 1, "unit": "MINUTES"})]:
        response = client.post(path + "/" + suffix, json=payload)
        assert response.status_code == 409, response.text
    day = client.get(f"/api/goals/{goal_id}/today").json()["day"]
    blocked = next(task for task in day["tasks"] if task["id"] == second_id)
    assert blocked["status"] == "NOT_STARTED" and "前置任务" in blocked["blocked_reason"]
    assert list(blocked["dependency_titles"].values()) == [today["day"]["tasks"][0]["title"]]
    assert client.post(f"/api/goals/{goal_id}/tasks/{first_id}/complete", json={}).status_code == 200
    assert client.post(path + "/complete", json={}).status_code == 200


def test_unresolved_task_edge_fails_closed(client, session_factory):
    goal_id = _started_goal(client)
    task_id = client.get(f"/api/goals/{goal_id}/today").json()["day"]["tasks"][0]["id"]
    with session_factory() as session:
        task = session.get(PlanTask, task_id)
        task.depends_on_json = ["M99-W99-D99-T99"]
        session.commit()
    response = client.post(f"/api/goals/{goal_id}/tasks/{task_id}/complete", json={})
    assert response.status_code == 409
    assert "待确认前置任务" in response.json()["detail"]
