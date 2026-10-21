from datetime import date
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from planner_api.execution_service import ExecutionService
from planner_api.schemas import CommitmentProgressRead, TaskProgressUpdate


def _create_manual_plan(client: TestClient) -> dict:
    response = client.post(
        "/api/manual-plans",
        json={
            "title": "时间块测试",
            "tasks": [
                {
                    "title": "已有固定任务",
                    "estimate_minutes": 30,
                    "scheduled_date": "2026-09-09",
                    "start_local": "09:00",
                    "end_local": "09:30",
                },
                {"title": "待安排任务", "estimate_minutes": 30, "scheduled_date": "2026-09-09"},
            ],
        },
    )
    assert response.status_code == 201
    return response.json()


def test_schedule_free_manual_task(client: TestClient) -> None:
    plan = _create_manual_plan(client)
    task = plan["tasks"][1]
    response = client.post(
        "/api/today/schedule",
        json={
            "source": "MANUAL",
            "task_id": task["id"],
            "scheduled_date": "2026-09-09",
            "start_local": "10:00",
            "end_local": "10:30",
        },
    )
    assert response.status_code == 200
    assert response.json()["scheduled"]["parallel_confirmed"] is False
    today = client.get("/api/today?on_date=2026-09-09").json()
    assert [item["title"] for item in today["fixed_tasks"]] == ["已有固定任务", "待安排任务"]


def test_unified_today_exposes_semantic_rule_for_quick_tasks(client: TestClient) -> None:
    _create_manual_plan(client)
    today = client.get("/api/today?on_date=2026-09-09").json()
    fixed = next(item for item in today["fixed_tasks"] if item["title"] == "已有固定任务")
    flexible = next(item for item in today["flexible_tasks"] if item["title"] == "待安排任务")

    assert fixed["scheduling_policy"] == "EXACT_DAY"
    assert fixed["scheduling_policy_data"]["recurrence"] == "ONCE"
    assert flexible["scheduling_policy"] == "FLOATING"
    assert flexible["scheduling_policy_data"]["source"] == "QUICK_PLAN"


def test_overlapping_task_requires_explicit_parallel_confirmation(client: TestClient) -> None:
    plan = _create_manual_plan(client)
    task = plan["tasks"][1]
    conflict = client.post(
        "/api/today/schedule",
        json={
            "source": "MANUAL",
            "task_id": task["id"],
            "scheduled_date": "2026-09-09",
            "start_local": "09:15",
            "end_local": "09:45",
        },
    )
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["code"] == "SCHEDULE_CONFLICT"

    confirmed = client.post(
        "/api/today/schedule",
        json={
            "source": "MANUAL",
            "task_id": task["id"],
            "scheduled_date": "2026-09-09",
            "start_local": "09:15",
            "end_local": "09:45",
            "allow_parallel": True,
            "parallel_reason": "前一个任务是等待型工作，可以并行处理",
        },
    )
    assert confirmed.status_code == 200
    assert confirmed.json()["scheduled"]["parallel_confirmed"] is True


def test_commitment_steps_credit_one_whole_occurrence_only() -> None:
    plan = SimpleNamespace(commitments_json=[{
        'label': '训练', 'cadence': 'PER_WEEK', 'start_week': 1, 'end_week': 1,
        'minimum_occurrences': 2}])
    days = [SimpleNamespace(id='d1', week_index=1)]
    schedules = [SimpleNamespace(plan_day_id='d1', scheduled_date=date(2026, 10, 10))]
    tasks = [SimpleNamespace(id='prep', plan_day_id='d1', status='COMPLETED',
                             satisfies_commitments_json=['训练'], estimate_minutes=5,
                             scheduling_policy_json={'occurrence_key': 'TRAINING.1'}),
             SimpleNamespace(id='practice', plan_day_id='d1', status='NOT_STARTED',
                             satisfies_commitments_json=['训练'], estimate_minutes=15,
                             scheduling_policy_json={'occurrence_key': 'TRAINING.1'})]
    progress = ExecutionService._commitment_progress(date(2026, 10, 10), plan, schedules, days, tasks)[0]
    assert progress.completed_occurrences == 0
    assert progress.completed_minutes == 5
    tasks[1].status = 'COMPLETED'
    assert ExecutionService._commitment_progress(date(2026, 10, 10), plan, schedules, days, tasks)[0].completed_occurrences == 1
    tasks.append(SimpleNamespace(id='second', plan_day_id='d1', status='COMPLETED',
                                 satisfies_commitments_json=['训练'], estimate_minutes=20,
                                 scheduling_policy_json={'occurrence_key': 'TRAINING.2'}))
    assert ExecutionService._commitment_progress(date(2026, 10, 10), plan, schedules, days, tasks)[0].completed_occurrences == 2
    # Pre-identity historical steps on the same day must not each earn a count.
    tasks.pop()
    for task in tasks:
        task.scheduling_policy_json = {}
    tasks[1].status = 'NOT_STARTED'
    assert ExecutionService._commitment_progress(date(2026, 10, 10), plan, schedules, days, tasks)[0].completed_occurrences == 0


def test_recovery_continuation_credits_once_and_keeps_historical_partial_effort():
    plan = SimpleNamespace(commitments_json=[{
        'label': '训练', 'cadence': 'PER_WEEK', 'start_week': 1, 'end_week': 1,
        'minimum_occurrences': 1}])
    days = [SimpleNamespace(id='d1', week_index=1)]
    schedules = [SimpleNamespace(plan_day_id='d1', scheduled_date=date(2026, 10, 10))]
    tasks = [SimpleNamespace(id='old', plan_day_id='d1', status='CARRIED_OVER',
                             satisfies_commitments_json=['训练'], estimate_minutes=20,
                             scheduling_policy_json={'occurrence_key': 'TRAINING.1'}),
             SimpleNamespace(id='continuation', plan_day_id='d1', status='COMPLETED',
                             satisfies_commitments_json=['训练'], estimate_minutes=20,
                             scheduling_policy_json={'occurrence_key': 'TRAINING.1'})]
    event = SimpleNamespace(task_id='old', event_type='PARTIAL_COMPLETION',
                            metadata_json={'amount': 5, 'unit': 'MINUTES'})
    progress = ExecutionService._commitment_progress(
        date(2026, 10, 10), plan, schedules, days, tasks, [event])[0]
    assert progress.completed_occurrences == 1
    assert progress.completed_minutes == 20
    assert progress.partial_minutes == 5
    assert progress.remaining_minutes == 0
    # A historical placeholder alone never counts as completed.
    assert ExecutionService._commitment_progress(
        date(2026, 10, 10), plan, schedules, days, tasks[:1], [event])[0].completed_occurrences == 0


def test_commitment_progress_projects_mixed_cadences() -> None:
    plan = SimpleNamespace(
        commitments_json=[
            {
                "label": "英语阅读",
                "start_week": 1,
                "end_week": 2,
                "cadence": "PER_WEEK",
                "minimum_occurrences": 2,
                "source_text": "每周完成两篇英语阅读",
            },
            {
                "label": "项目复盘",
                "start_week": 1,
                "end_week": 1,
                "cadence": "TOTAL",
                "minimum_occurrences": 2,
                "source_text": "本阶段完成两次项目复盘",
            },
            {
                "label": "健身",
                "start_week": 1,
                "end_week": 1,
                "cadence": "PER_EXECUTION_DAY",
                "minimum_occurrences": 1,
                "source_text": "每个执行日健身一次",
            },
        ]
    )
    days = [
        SimpleNamespace(id="d1", week_index=1),
        SimpleNamespace(id="d2", week_index=1),
        SimpleNamespace(id="d3", week_index=1),
    ]
    schedules = [
        SimpleNamespace(plan_day_id="d1", scheduled_date=date(2026, 9, 7)),
        SimpleNamespace(plan_day_id="d2", scheduled_date=date(2026, 9, 8)),
        SimpleNamespace(plan_day_id="d3", scheduled_date=date(2026, 9, 9)),
    ]
    tasks = [
        SimpleNamespace(id="t1", plan_day_id="d1", status="COMPLETED", satisfies_commitments_json=["英语阅读", "项目复盘"]),
        SimpleNamespace(id="t2", plan_day_id="d2", status="NOT_STARTED", satisfies_commitments_json=["英语阅读", "健身"]),
        SimpleNamespace(id="t3", plan_day_id="d3", status="COMPLETED", satisfies_commitments_json=["项目复盘"]),
    ]

    result = ExecutionService._commitment_progress(
        date(2026, 9, 8), plan, schedules, days, tasks
    )
    by_label = {item.label: item for item in result}

    assert by_label["英语阅读"].completed_occurrences == 1
    assert by_label["英语阅读"].remaining_occurrences == 1
    assert by_label["项目复盘"].risk == "COMPLETE"
    assert by_label["健身"].completed_occurrences == 0
    assert by_label["健身"].period_start == date(2026, 9, 8)


def test_commitment_alerts_only_include_actionable_risk() -> None:
    progress = [
        CommitmentProgressRead(
            label="英语阅读",
            cadence="PER_WEEK",
            period_start=date(2026, 9, 7),
            period_end=date(2026, 9, 9),
            target_occurrences=2,
            completed_occurrences=1,
            remaining_occurrences=1,
            risk="DUE_SOON",
        ),
        CommitmentProgressRead(
            label="项目复盘",
            cadence="TOTAL",
            period_start=date(2026, 9, 1),
            period_end=date(2026, 9, 30),
            target_occurrences=2,
            completed_occurrences=2,
            remaining_occurrences=0,
            risk="COMPLETE",
        ),
    ]

    alerts = ExecutionService._commitment_alerts(progress)

    assert len(alerts) == 1
    assert alerts[0].label == "英语阅读"
    assert alerts[0].action == "DISCUSS"


def test_commitment_progress_summarizes_partial_minutes_without_counting_occurrence() -> None:
    plan = SimpleNamespace(
        commitments_json=[
            {
                "label": "英语阅读",
                "start_week": 1,
                "end_week": 1,
                "cadence": "TOTAL",
                "minimum_occurrences": 1,
            }
        ]
    )
    days = [SimpleNamespace(id="d1", week_index=1)]
    schedules = [SimpleNamespace(plan_day_id="d1", scheduled_date=date(2026, 9, 8))]
    tasks = [
        SimpleNamespace(
            id="t1",
            plan_day_id="d1",
            status="IN_PROGRESS",
            estimate_minutes=45,
            satisfies_commitments_json=["英语阅读"],
        )
    ]
    events = [
        SimpleNamespace(
            task_id="t1",
            event_type="PARTIAL_COMPLETION",
            metadata_json={"amount": 20, "unit": "MINUTES"},
        ),
        SimpleNamespace(
            task_id="t1",
            event_type="PARTIAL_COMPLETION",
            metadata_json={"amount": 40, "unit": "MINUTES"},
        ),
    ]

    result = ExecutionService._commitment_progress(
        date(2026, 9, 8), plan, schedules, days, tasks, events
    )[0]

    assert result.completed_occurrences == 0
    assert result.remaining_occurrences == 1
    assert result.partial_minutes == 60
    assert result.remaining_minutes == 0

    tasks[0].status = "COMPLETED"
    completed = ExecutionService._commitment_progress(
        date(2026, 9, 8), plan, schedules, days, tasks, events
    )[0]
    assert completed.completed_occurrences == 1
    assert completed.completed_minutes == 45
    assert completed.partial_minutes == 60


def test_task_progress_contract_requires_evidence_for_partial_skip_and_defer() -> None:
    assert TaskProgressUpdate(action="PARTIAL", amount=30, unit="MINUTES").amount == 30
    assert TaskProgressUpdate(action="SKIP", reason="本周出差").reason == "本周出差"
    assert TaskProgressUpdate(action="DEFER", reason="等待导师反馈").action == "DEFER"
    with pytest.raises(ValidationError):
        TaskProgressUpdate(action="PARTIAL")
    with pytest.raises(ValidationError):
        TaskProgressUpdate(action="SKIP")
