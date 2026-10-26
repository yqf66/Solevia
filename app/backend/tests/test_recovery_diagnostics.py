from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import func, select

from planner_api.models import Goal, PlanTask, PlanVersion, ProgressEvent, TaskTimeBlock
from test_recovery_time_preview import setup_recovery


def test_sources_and_partial_estimate_survive_readonly_preview(client, session_factory):
    goal_id, plan_id = setup_recovery(client)
    with session_factory() as session:
        task = session.scalar(select(PlanTask).where(PlanTask.plan_version_id == plan_id))
        task.status = "IN_PROGRESS"
        task.estimate_minutes = 45
        task_id = task.id
        session.add(
            ProgressEvent(
                goal_id=goal_id,
                plan_version_id=plan_id,
                plan_day_id=task.plan_day_id,
                task_id=task.id,
                event_type="PARTIAL_COMPLETION",
                metadata_json={"amount": 15, "unit": "MINUTES"},
            )
        )
        session.commit()
    preview = client.get(f"/api/goals/{goal_id}/execution/recovery/shift-preview").json()
    repeated = client.get(f"/api/goals/{goal_id}/execution/recovery/shift-preview").json()
    assert preview["time_preview"]["after"] == repeated["time_preview"]["after"]
    assert preview["time_preview"]["before"] == repeated["time_preview"]["before"]
    assert preview["time_preview"]["fingerprint"] == repeated["time_preview"]["fingerprint"]
    assert preview["id"] == repeated["id"]
    row = next(
        item for item in preview["time_preview"]["after"] if item["source_task_id"] == task_id
    )
    assert row["source_plan_version_id"] == plan_id
    assert row["lineage_task_id"] == task_id
    assert row["remaining_minutes"] == 30
    assert row["source_date"]
    assert row["reason_code"]
    assert "KEEP_PENDING" in row["allowed_actions"]
    with session_factory() as session:
        assert session.get(Goal, goal_id).active_plan_version_id == plan_id
        assert session.get(PlanTask, task_id).status == "IN_PROGRESS"
        assert session.scalar(select(func.count()).select_from(PlanVersion)) == 1
        assert session.scalar(select(func.count()).select_from(TaskTimeBlock)) == 0
        assert (
            session.scalar(
                select(func.count())
                .select_from(ProgressEvent)
                .where(ProgressEvent.event_type == "PARTIAL_COMPLETION")
            )
            == 1
        )


def test_infeasible_catchup_returns_reviewable_diagnostics_and_cannot_apply(
    client, session_factory
):
    goal_id, plan_id = setup_recovery(client)
    with session_factory() as session:
        tasks = list(session.scalars(select(PlanTask).where(PlanTask.plan_version_id == plan_id)))
        tasks[0].depends_on_json = ["未知前置材料"]
        task_id = tasks[0].id
        session.commit()
    response = client.get(f"/api/goals/{goal_id}/execution/recovery/catchup-preview")
    assert response.status_code == 200, response.text
    preview = response.json()
    assert not preview["programmatic_feasible"]
    time_preview = preview["time_preview"]
    assert time_preview["diagnostics_only"]
    assert len(time_preview["after"]) == preview["backlog_tasks"]
    row = next(item for item in time_preview["after"] if item["source_task_id"] == task_id)
    assert row["reason_code"] == "UNRESOLVED_DEPENDENCY"
    assert "REVIEW_DEPENDENCY" in row["allowed_actions"]
    denied = client.post(
        f"/api/goals/{goal_id}/execution/recovery/catchup",
        json={"preview_id": preview["id"], "accept_unallocated": True},
    )
    assert denied.status_code == 409
    with session_factory() as session:
        assert session.get(Goal, goal_id).active_plan_version_id == plan_id
        assert session.scalar(select(func.count()).select_from(PlanVersion)) == 1


def test_flexible_mode_is_not_misreported_as_missing_capacity(client):
    goal_id, _ = setup_recovery(client)
    client.patch(f"/api/goals/{goal_id}/execution/schedule/preferences", json={"mode": "FLEXIBLE"})
    preview = client.get(f"/api/goals/{goal_id}/execution/recovery/shift-preview").json()[
        "time_preview"
    ]
    assert preview["after"]
    assert preview["unallocated_count"] == 0
    assert all(
        item["state"] == "FLEXIBLE" and item["reason_code"] is None for item in preview["after"]
    )


def test_window_expiry_is_diagnosed_without_claiming_new_dates_are_valid(client, session_factory):
    goal_id, plan_id = setup_recovery(client)
    yesterday = datetime.now(ZoneInfo("Asia/Shanghai")).date() - timedelta(days=1)
    with session_factory() as session:
        task = session.scalar(select(PlanTask).where(PlanTask.plan_version_id == plan_id))
        task_id = task.id
        task.depends_on_json = []
        task.scheduling_policy = "DATE_WINDOW"
        task.scheduling_policy_json = {"eligible_to": str(yesterday)}
        session.commit()
    preview = client.get(f"/api/goals/{goal_id}/execution/recovery/catchup-preview").json()
    row = next(
        item for item in preview["time_preview"]["after"] if item["source_task_id"] == task_id
    )
    assert row["reason_code"] == "WINDOW_EXPIRED"
    assert "REVIEW_PLAN" in row["allowed_actions"]


def test_no_model_catchup_diagnostics_distinguish_capacity_from_dependency(client, session_factory):
    goal_id, plan_id = setup_recovery(client)
    with session_factory() as session:
        for task in session.scalars(select(PlanTask).where(PlanTask.plan_version_id == plan_id)):
            task.depends_on_json = []
        session.commit()
    preview = client.get(f"/api/goals/{goal_id}/execution/recovery/catchup-preview").json()
    assert preview["time_preview"]["diagnostics_only"]
    assert all(
        row["reason_code"]
        in {
            "WINDOW_EXPIRED",
            "CAPACITY_INSUFFICIENT",
            "COMPLEXITY_REVIEW_REQUIRED",
            "RECOVERY_INCOMPLETE",
        }
        for row in preview["time_preview"]["after"]
    )


def test_ambiguous_lineage_does_not_publish_an_arbitrary_source_identity():
    from types import SimpleNamespace
    from planner_api.recovery_diagnostics import source_for_task

    task = SimpleNamespace(id="transient", source_task_id=None)
    service = SimpleNamespace(_task_lineage_key=lambda _: "shared-lineage")
    sources = {
        "a": {"lineage_task_id": "shared-lineage", "source_task_id": "a"},
        "b": {"lineage_task_id": "shared-lineage", "source_task_id": "b"},
    }
    result = source_for_task(service, task, sources)
    assert "source_task_id" not in result
    assert result["reason_code"] == "SOURCE_IDENTITY_AMBIGUOUS"
    task.source_task_id = "b"
    assert source_for_task(service, task, sources)["source_task_id"] == "b"


def test_partial_minutes_are_not_deducted_twice_from_sibling_tasks(client, session_factory):
    from planner_api.execution_service import ExecutionService
    from planner_api.recovery_diagnostics import source_items

    goal_id, plan_id = setup_recovery(client)
    with session_factory() as session:
        tasks = list(session.scalars(select(PlanTask).where(PlanTask.plan_version_id == plan_id)))[
            :2
        ]
        for task in tasks:
            task.lineage_task_id = tasks[0].id
            task.estimate_minutes = 45
        session.add(
            ProgressEvent(
                goal_id=goal_id,
                plan_version_id=plan_id,
                plan_day_id=tasks[0].plan_day_id,
                task_id=tasks[0].id,
                event_type="PARTIAL_COMPLETION",
                metadata_json={"amount": 15, "unit": "MINUTES"},
            )
        )
        session.commit()
        result = source_items(ExecutionService(session), session.get(PlanVersion, plan_id))
        assert result[tasks[0].id]["remaining_minutes"] == 30
        assert result[tasks[1].id]["remaining_minutes"] == 45
