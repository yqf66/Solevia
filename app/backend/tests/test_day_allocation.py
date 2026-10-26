from datetime import date
from types import SimpleNamespace

from sqlalchemy import select

from planner_api.day_allocation import DayAllocator
from planner_api.dependency_resolver import PlanDependencyResolver
from planner_api.models import PlanDay, PlanTask, PlanVersion, RequirementFact

DAY = "2035-09-10"


def test_dependency_parser_resolves_natural_language_day_references():
    dependency = SimpleNamespace(
        id="source-task", plan_day_id="day-1", order_in_day=3, status="COMPLETED"
    )
    current = SimpleNamespace(plan_day_id="day-2", order_in_day=1)
    indexes = {
        "tasks_by_id": {dependency.id: dependency},
        "by_day": {1: [dependency], 2: [current]},
    }
    allocator = DayAllocator.__new__(DayAllocator)

    assert allocator._resolve_dependency("第一天的任务3", current, indexes) is dependency
    assert allocator._resolve_dependency("source-task", current, indexes) is dependency
    assert allocator._chinese_number("十") == 10


def test_dependency_parser_resolves_canonical_and_unique_legacy_title():
    dependency = SimpleNamespace(
        id="source-task",
        plan_day_id="day-1",
        order_in_day=1,
        status="NOT_STARTED",
        title="先完成资料整理",
    )
    current = SimpleNamespace(plan_day_id="day-1", order_in_day=2)
    indexes = {
        "tasks_by_id": {dependency.id: dependency},
        "tasks_by_title": {dependency.title: [dependency]},
        "by_coordinate": {(1, 2, 3, 1): dependency},
        "by_day": {1: [dependency, current]},
    }
    allocator = DayAllocator.__new__(DayAllocator)

    assert allocator._resolve_dependency("M1-W2-D3-T1", current, indexes) is dependency
    assert allocator._resolve_dependency(dependency.title, current, indexes) is dependency


def test_dependency_parser_resolves_unique_legacy_evidence_name():
    dependency = SimpleNamespace(
        id="source-task",
        plan_day_id="day-1",
        order_in_day=1,
        status="NOT_STARTED",
        title="设计 Provider 抽象接口",
    )
    current = SimpleNamespace(plan_day_id="day-1", order_in_day=2)
    indexes = {
        "tasks_by_id": {dependency.id: dependency},
        "tasks_by_title": {dependency.title: [dependency]},
        "tasks_by_evidence": {"接口定义代码": [dependency]},
        "by_coordinate": {},
        "by_day": {1: [dependency, current]},
    }
    allocator = DayAllocator.__new__(DayAllocator)

    assert allocator._resolve_dependency("接口定义代码", current, indexes) is dependency


def test_dependency_parser_keeps_ambiguous_evidence_unresolved():
    first = SimpleNamespace(id="first", title="任务 A")
    second = SimpleNamespace(id="second", title="任务 B")
    current = SimpleNamespace(plan_day_id="day-1", order_in_day=3)
    indexes = {
        "tasks_by_id": {},
        "tasks_by_title": {},
        "tasks_by_evidence": {"共同产出": [first, second]},
        "by_coordinate": {},
        "by_day": {1: [current]},
    }
    allocator = DayAllocator.__new__(DayAllocator)

    assert allocator._resolve_dependency("共同产出", current, indexes) is None


def test_recovery_relative_dependency_uses_source_day_context():
    source_prerequisite = SimpleNamespace(
        id="original-1",
        plan_version_id="old-plan",
        plan_day_id="old-day",
        source_task_id=None,
        lineage_task_id=None,
        order_in_day=1,
        title="原始前置",
        status="NOT_STARTED",
    )
    source_task = SimpleNamespace(
        id="original-2",
        plan_version_id="old-plan",
        plan_day_id="old-day",
        source_task_id=None,
        lineage_task_id=None,
        order_in_day=2,
        title="原始后置",
        status="CARRIED_OVER",
    )
    current_prerequisite = SimpleNamespace(
        id="current-1",
        plan_version_id="new-plan",
        plan_day_id="recovery-day",
        source_task_id="original-1",
        lineage_task_id="original-1",
        order_in_day=2,
        title="补回：原始前置",
        status="NOT_STARTED",
    )
    recovery_task = SimpleNamespace(
        id="current-2",
        plan_version_id="new-plan",
        plan_day_id="recovery-day",
        source_task_id="original-2",
        lineage_task_id="original-2",
        order_in_day=1,
        title="补回：原始后置",
        status="NOT_STARTED",
    )

    class FakeSession:
        def get(self, _model, identifier):
            return source_task if identifier == source_task.id else None

    resolver = PlanDependencyResolver(FakeSession())
    resolver._cache = {
        "old-plan": {
            "tasks": [source_prerequisite, source_task],
            "tasks_by_id": {item.id: item for item in [source_prerequisite, source_task]},
            "tasks_by_title": {},
            "tasks_by_evidence": {},
            "by_coordinate": {},
            "by_day": {1: [source_prerequisite, source_task]},
        },
        "new-plan": {
            "tasks": [recovery_task, current_prerequisite],
            "tasks_by_id": {item.id: item for item in [recovery_task, current_prerequisite]},
            "tasks_by_title": {},
            "tasks_by_evidence": {},
            "by_coordinate": {},
            "by_day": {1: [recovery_task, current_prerequisite]},
        },
    }

    assert resolver.resolve("任务1", recovery_task) is current_prerequisite


def setup_day(client):
    plan = client.post(
        "/api/manual-plans",
        json={
            "title": "共享排课测试",
            "tasks": [
                {
                    "title": "会议",
                    "estimate_minutes": 30,
                    "scheduled_date": DAY,
                    "start_local": "10:00",
                    "end_local": "10:30",
                },
                {"title": "练习", "estimate_minutes": 30, "scheduled_date": DAY},
                {"title": "长任务", "estimate_minutes": 120, "scheduled_date": DAY},
            ],
        },
    ).json()
    payload = {
        "scheduled_date": DAY,
        "windows": [
            {
                "weekday": date.fromisoformat(DAY).weekday(),
                "start_local": "09:00",
                "end_local": "11:00",
            },
        ],
    }
    return plan, payload


def test_preview_is_read_only_and_apply_keeps_fixed_tasks(client):
    plan, payload = setup_day(client)
    preview = client.post("/api/today/allocation/preview", json=payload)
    assert preview.status_code == 200, preview.text
    value = preview.json()
    assert len(value["assignments"]) == 1
    assert value["assignments"][0]["task_id"] == plan["tasks"][1]["id"]
    assert value["assignments"][0]["start_local"] == "09:00"
    assert len(value["unallocated"]) == 1
    assert len(client.get(f"/api/today?on_date={DAY}").json()["fixed_tasks"]) == 1
    applied = client.post(
        "/api/today/allocation/apply",
        json={
            "request": value["request"],
            "fingerprint": value["fingerprint"],
        },
    )
    assert applied.status_code == 200, applied.text
    assert len(applied.json()["fixed_tasks"]) == 2
    assert (
        next(t for t in applied.json()["fixed_tasks"] if t["title"] == "会议")["start_local"]
        == "10:00"
    )
    assert len(applied.json()["flexible_tasks"]) == 1


def test_preview_exposes_single_day_scheduling_context(client):
    plan, payload = setup_day(client)
    value = client.post("/api/today/allocation/preview", json=payload).json()
    context = value["scheduling_context"]
    assert value["strategy"] in {"MODEL_ASSISTED", "LOCAL_VALIDATED"}
    assert context["date"] == DAY
    assert context["break_minutes"] == 10
    assert context["occupied_fixed_tasks"][0]["task_id"] == plan["tasks"][0]["id"]
    assert context["free_windows_before_assignment"] == [["09:00", "09:50"], ["10:40", "11:00"]]
    assert context["tasks"][0]["task_id"] == plan["tasks"][1]["id"]
    assert "前置任务未完成时不安排后置任务" in context["allocation_policy"]


def test_preview_exposes_cross_plan_capacity_competition(client):
    _plan, payload = setup_day(client)
    second = client.post(
        "/api/manual-plans",
        json={
            "title": "第二个规划",
            "tasks": [
                {"title": "第二规划任务", "estimate_minutes": 45, "scheduled_date": DAY},
            ],
        },
    )
    assert second.status_code == 201
    value = client.post("/api/today/allocation/preview", json=payload).json()
    assert len(value["competition_summary"]) == 1
    summary = value["competition_summary"][0]
    assert summary["required_minutes"] >= 195
    assert len(summary["plans"]) == 2
    assert any(item["unallocated_minutes"] > 0 for item in summary["plans"])
    assert summary["tradeoffs"]


def test_preview_exposes_confirmed_calendar_blocks(client, monkeypatch):
    _plan, payload = setup_day(client)
    monkeypatch.setattr(
        "planner_api.day_allocation.confirmed_unavailable_windows",
        lambda _session, _goal_id, _day: [(14 * 60, 16 * 60, "政治理论课")],
    )
    payload["windows"] = [
        {
            "weekday": date.fromisoformat(DAY).weekday(),
            "start_local": "13:00",
            "end_local": "18:00",
        }
    ]

    value = client.post("/api/today/allocation/preview", json=payload).json()

    assert value["scheduling_context"]["occupied_constraints"] == [
        {
            "title": "政治理论课",
            "start_local": "14:00",
            "end_local": "16:00",
            "source": "CONFIRMED_DISCUSSION",
        }
    ]
    assert "14:00–16:00 政治理论课" in "".join(value["notes"])


def test_real_confirmed_course_fact_blocks_shared_allocation(client, session_factory):
    """P0-01: a confirmed discussion fact must reach the real allocator."""
    from test_execution_discussion import _started_goal

    goal_id = _started_goal(client)
    _plan, payload = setup_day(client)
    payload["windows"] = [
        {
            "weekday": date.fromisoformat(DAY).weekday(),
            "start_local": "14:00",
            "end_local": "16:00",
        }
    ]
    with session_factory() as session:
        session.add(
            RequirementFact(
                goal_id=goal_id,
                category="CONSTRAINT",
                field_key="fixed_schedule",
                value_json="周一至周六每天 14:00-16:00 有课",
                source="USER",
                status="CONFIRMED",
                confidence=1.0,
            )
        )
        session.commit()

    response = client.post("/api/today/allocation/preview", json=payload)

    assert response.status_code == 200, response.text
    value = response.json()
    assert value["assignments"] == []
    assert set(value["unallocated_reasons"].values()) == {"FIXED_CONSTRAINT"}
    assert set(value["unallocated_reasons"]) == {
        item["id"] for item in value["unallocated"]
    }
    assert value["scheduling_context"]["occupied_constraints"] == [
        {
            "title": "课程占用",
            "start_local": "14:00",
            "end_local": "16:00",
            "source": "CONFIRMED_DISCUSSION",
        }
    ]
    assert "14:00–16:00 课程占用" in "".join(value["notes"])


def test_batch_preview_uses_the_same_single_day_contract(client):
    _plan, payload = setup_day(client)
    second = {
        **payload,
        "scheduled_date": "2035-09-11",
        "windows": [
            {
                "weekday": date.fromisoformat("2035-09-11").weekday(),
                "start_local": "09:00",
                "end_local": "11:00",
            },
        ],
    }
    response = client.post(
        "/api/today/allocation/preview-many",
        json={
            "requests": [payload, second],
        },
    )
    assert response.status_code == 200, response.text
    values = response.json()
    assert len(values) == 2
    assert [value["request"]["scheduled_date"] for value in values] == [DAY, "2035-09-11"]
    assert values[0]["strategy"] in {"MODEL_ASSISTED", "LOCAL_VALIDATED"}
    assert values[1]["strategy"] == "LOCAL_VALIDATED"
    assert all("scheduling_context" in value for value in values)


def test_changed_calendar_rejects_preview_without_partial_write(client):
    _, payload = setup_day(client)
    preview = client.post("/api/today/allocation/preview", json=payload).json()
    client.post(
        "/api/manual-plans",
        json={
            "title": "临时安排",
            "tasks": [
                {
                    "title": "新会议",
                    "estimate_minutes": 30,
                    "scheduled_date": DAY,
                    "start_local": "09:00",
                    "end_local": "09:30",
                },
            ],
        },
    )
    result = client.post(
        "/api/today/allocation/apply",
        json={
            "request": preview["request"],
            "fingerprint": preview["fingerprint"],
        },
    )
    assert result.status_code == 409
    assert len(client.get(f"/api/today?on_date={DAY}").json()["flexible_tasks"]) == 2


def test_missing_availability_does_not_invent_working_hours(client):
    setup_day(client)
    response = client.post("/api/today/allocation/preview", json={"scheduled_date": DAY})
    assert response.status_code == 200
    assert response.json()["assignments"] == []
    assert len(response.json()["unallocated"]) == 2


def test_parallel_consent_is_not_permission_for_third_task(client):
    plan, _ = setup_day(client)
    schedule = {
        "source": "MANUAL",
        "scheduled_date": DAY,
        "start_local": "10:00",
        "end_local": "10:30",
    }
    response = client.post(
        "/api/today/schedule",
        json={
            **schedule,
            "task_id": plan["tasks"][1]["id"],
            "allow_parallel": True,
            "parallel_reason": "等待期间处理",
        },
    )
    assert response.status_code == 200
    assert client.get(f"/api/today?on_date={DAY}").json()["conflicts"] == []
    third = client.post(
        "/api/today/schedule",
        json={
            **schedule,
            "task_id": plan["tasks"][2]["id"],
        },
    )
    assert third.status_code == 409
    assert len(third.json()["detail"]["conflicts"]) == 2


def test_existing_fixed_task_can_be_moved_to_another_time(client):
    plan, _ = setup_day(client)
    task_id = plan["tasks"][0]["id"]
    response = client.post(
        "/api/today/schedule",
        json={
            "source": "MANUAL",
            "task_id": task_id,
            "scheduled_date": DAY,
            "start_local": "11:00",
            "end_local": "11:30",
        },
    )
    assert response.status_code == 200, response.text
    board = client.get(f"/api/today?on_date={DAY}").json()
    moved = next(task for task in board["fixed_tasks"] if task["id"] == task_id)
    assert (moved["start_local"], moved["end_local"]) == ("11:00", "11:30")


def test_start_inherits_blocks_mode_and_avoids_other_plan(client):
    from test_plan import _completed_plan

    workspace = _completed_plan(client)
    goal = workspace["goal_id"]
    assert client.post(f"/api/goals/{goal}/plan/confirm").status_code == 200
    assert (
        client.patch(
            f"/api/goals/{goal}/execution/schedule/preferences",
            json={
                "mode": "BLOCKS",
            },
        ).status_code
        == 200
    )
    assert (
        client.put(
            f"/api/goals/{goal}/execution/schedule/availability",
            json={
                "windows": [
                    {"weekday": i, "start_local": "09:00", "end_local": "17:00"} for i in range(7)
                ],
            },
        ).status_code
        == 200
    )
    setup_day(client)
    response = client.post(
        f"/api/goals/{goal}/execution/start",
        json={
            "requested_start_date": DAY,
            "execution_weekdays": [
                date.fromisoformat(DAY).weekday(),
                (date.fromisoformat(DAY).weekday() + 2) % 7,
            ],
            "excluded_dates": [],
        },
    )
    assert response.status_code == 200, response.text
    board = client.get(f"/api/today?on_date={DAY}").json()
    assigned = [t for t in board["fixed_tasks"] if t["source"] == "AGENT"]
    assert assigned
    assert all(t["end_local"] <= "10:00" or t["start_local"] >= "10:30" for t in assigned)
    assert len(board["flexible_tasks"]) == 2  # Other plans are not modified.


def test_active_flexible_plan_can_be_scheduled_as_a_whole(client):
    from test_plan import _completed_plan

    workspace = _completed_plan(client)
    goal = workspace["goal_id"]
    assert client.post(f"/api/goals/{goal}/plan/confirm").status_code == 200
    assert (
        client.put(
            f"/api/goals/{goal}/execution/schedule/availability",
            json={
                "windows": [
                    {"weekday": i, "start_local": "09:00", "end_local": "22:00"} for i in range(7)
                ],
            },
        ).status_code
        == 200
    )
    started = client.post(
        f"/api/goals/{goal}/execution/start",
        json={
            "requested_start_date": DAY,
            "execution_weekdays": sorted(
                [
                    date.fromisoformat(DAY).weekday(),
                    (date.fromisoformat(DAY).weekday() + 2) % 7,
                ]
            ),
            "excluded_dates": [],
        },
    )
    assert started.status_code == 200, started.text
    assert (
        client.get(f"/api/goals/{goal}/execution/schedule").json()["preference"]["mode"]
        == "FLEXIBLE"
    )

    response = client.post(f"/api/goals/{goal}/execution/schedule/allocate-plan")
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["scheduled_days"] > 0
    assert result["assigned_tasks"] > 0
    assert (
        client.get(f"/api/goals/{goal}/execution/schedule").json()["preference"]["mode"] == "BLOCKS"
    )
    board = client.get(f"/api/today?on_date={DAY}").json()
    assert any(task["source"] == "AGENT" for task in board["fixed_tasks"])


def test_preview_does_not_overlap_merged_windows(client):
    _, payload = setup_day(client)
    payload["windows"].append(
        {"weekday": date.fromisoformat(DAY).weekday(), "start_local": "09:15", "end_local": "10:45"}
    )
    preview = client.post("/api/today/allocation/preview", json=payload).json()
    assert preview["available_minutes"] == 70  # 120 - 30 fixed - 2 * 10 break
    assert len(preview["assignments"]) == 1


def test_today_places_same_day_prerequisite_before_dependent(client, session_factory):
    from test_plan import _completed_plan

    workspace = _completed_plan(client)
    goal_id = workspace["goal_id"]
    with session_factory() as session:
        plan = session.scalar(select(PlanVersion).where(PlanVersion.goal_id == goal_id))
        day = session.scalar(
            select(PlanDay)
            .where(PlanDay.plan_version_id == plan.id)
            .order_by(PlanDay.week_index, PlanDay.day_index)
        )
        prerequisite = session.scalar(
            select(PlanTask).where(PlanTask.plan_day_id == day.id).order_by(PlanTask.order_in_day)
        )
        dependent = PlanTask(
            plan_version_id=plan.id,
            plan_day_id=day.id,
            source_task_id=None,
            origin_stage_order=day.stage_order,
            lineage_task_id=None,
            order_in_day=2,
            title="使用准备结果完成后续任务",
            description="必须在准备任务完成后才能开始。",
            estimate_minutes=30,
            priority=5,
            task_kind="BUILD",
            is_required=True,
            evidence_json=[],
            depends_on_json=[
                f"M{day.stage_order}-W{day.week_index}-D{day.day_index}-T{prerequisite.order_in_day}"
            ],
            ai_schedule_requested=True,
        )
        session.add(dependent)
        session.commit()
        prerequisite_id = prerequisite.id
        dependent_id = dependent.id

    assert client.post(f"/api/goals/{goal_id}/plan/confirm").status_code == 200
    weekday = date.fromisoformat(DAY).weekday()
    started = client.post(
        f"/api/goals/{goal_id}/execution/start",
        json={
            "requested_start_date": DAY,
            "execution_weekdays": [weekday, (weekday + 2) % 7],
            "excluded_dates": [],
        },
    )
    assert started.status_code == 200, started.text
    board = client.get(f"/api/today?on_date={DAY}").json()
    projected_dependent = next(
        item for item in board["flexible_tasks"] if item["id"] == dependent_id
    )
    assert projected_dependent["dependencies"] == [
        {
            "reference": (
                f"M{day.stage_order}-W{day.week_index}-D{day.day_index}"
                f"-T{prerequisite.order_in_day}"
            ),
            "task_id": prerequisite_id,
            "title": prerequisite.title,
            "status": "PENDING",
        }
    ]
    preview = client.post(
        "/api/today/allocation/preview",
        json={
            "scheduled_date": DAY,
            "include_flexible": True,
            "windows": [{"weekday": weekday, "start_local": "09:00", "end_local": "12:00"}],
        },
    )
    assert preview.status_code == 200, preview.text
    assignments = {item["task_id"]: item for item in preview.json()["assignments"]}
    assert assignments[prerequisite_id]["end_local"] < assignments[dependent_id]["start_local"]
    dependency = next(
        item
        for item in preview.json()["scheduling_context"]["tasks"]
        if item["task_id"] == dependent_id
    )["dependencies"][0]
    assert dependency["task_id"] == prerequisite_id
