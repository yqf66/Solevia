from datetime import date, datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from test_execution_discussion import _started_goal

from planner_api.execution_service import ExecutionService
from planner_api.models import ReminderEvent, utc_now
from planner_api.schemas import CommitmentProgressRead


def _progress() -> list[CommitmentProgressRead]:
    return [
        CommitmentProgressRead(
            label="英语阅读",
            cadence="PER_WEEK",
            period_start=date(2026, 10, 5),
            period_end=date(2026, 10, 6),
            target_occurrences=3,
            completed_occurrences=1,
            remaining_occurrences=2,
            risk="DUE_SOON",
        )
    ]


def test_alerts_have_stable_dedupe_keys_and_respect_mute() -> None:
    progress = _progress()
    preference = SimpleNamespace(
        reminders_enabled=True,
        supervision_mode="STANDARD",
        muted_commitments=[],
        quiet_start_local=None,
        quiet_end_local=None,
        timezone="Asia/Shanghai",
    )

    first = ExecutionService._commitment_alerts(progress, preference=preference)
    second = ExecutionService._commitment_alerts(progress, preference=preference)

    assert len(first) == 1
    assert first[0].dedupe_key == second[0].dedupe_key
    preference.muted_commitments_json = ["英语阅读"]
    assert ExecutionService._commitment_alerts(progress, preference=preference) == []


def test_alerts_respect_disabled_and_quiet_periods() -> None:
    preference = SimpleNamespace(
        reminders_enabled=False,
        supervision_mode="STANDARD",
        muted_commitments=[],
        quiet_start_local="22:00",
        quiet_end_local="07:00",
        timezone="Asia/Shanghai",
    )
    assert ExecutionService._commitment_alerts(_progress(), preference=preference) == []

    preference.reminders_enabled = True
    assert ExecutionService._commitment_alerts(
        _progress(),
        preference=preference,
        now=datetime(2026, 10, 5, 23, 30, tzinfo=ZoneInfo("Asia/Shanghai")),
    ) == []
    assert ExecutionService._commitment_alerts(
        _progress(),
        preference=preference,
        now=datetime(2026, 10, 5, 12, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
    )


def test_reminder_settings_and_single_commitment_mute_are_persisted(client) -> None:
    response = client.post("/api/goals", json={"raw_input": "测试提醒治理"})
    assert response.status_code == 201
    goal_id = response.json()["goal_id"]

    updated = client.patch(
        f"/api/goals/{goal_id}/execution/schedule/preferences",
        json={
            "reminders_enabled": True,
            "supervision_mode": "INTENSIVE",
            "quiet_start_local": "22:00",
            "quiet_end_local": "07:00",
        },
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["supervision_mode"] == "INTENSIVE"
    assert updated.json()["quiet_start_local"] == "22:00"

    muted = client.patch(
        f"/api/goals/{goal_id}/execution/schedule/reminders",
        json={"label": "英语阅读", "enabled": False},
    )
    assert muted.status_code == 200, muted.text
    assert muted.json()["muted_commitments"] == ["英语阅读"]

    unmuted = client.patch(
        f"/api/goals/{goal_id}/execution/schedule/reminders",
        json={"label": "英语阅读", "enabled": True},
    )
    assert unmuted.status_code == 200, unmuted.text
    assert unmuted.json()["muted_commitments"] == []


def test_reminder_history_can_be_read_acknowledged_and_dismissed(
    client, session_factory
) -> None:
    goal_id = _started_goal(client)
    now = utc_now()
    with session_factory() as session:
        session.add(
            ReminderEvent(
                goal_id=goal_id,
                dedupe_key="英语阅读|AT_RISK|2026-10-06|2",
                label="英语阅读",
                risk="AT_RISK",
                severity="URGENT",
                title="英语阅读的完成窗口需要处理",
                message="当前还差 2 次。",
                action="REVIEW_PLAN",
                remaining_occurrences=2,
                first_seen_at=now,
                last_seen_at=now,
            )
        )
        session.commit()

    listed = client.get(f"/api/goals/{goal_id}/execution/reminders")
    assert listed.status_code == 200
    event_id = next(
        item["id"]
        for item in listed.json()
        if item["dedupe_key"] == "英语阅读|AT_RISK|2026-10-06|2"
    )

    read = client.patch(
        f"/api/goals/{goal_id}/execution/reminders/{event_id}",
        json={"action": "READ"},
    )
    assert read.status_code == 200
    assert read.json()["status"] == "READ"
    assert read.json()["read_at"] is not None

    dismissed = client.patch(
        f"/api/goals/{goal_id}/execution/reminders/{event_id}",
        json={"action": "DISMISS"},
    )
    assert dismissed.status_code == 200
    assert dismissed.json()["status"] == "DISMISSED"
    assert event_id not in {
        item["id"]
        for item in client.get(f"/api/goals/{goal_id}/execution/reminders").json()
    }
    history = client.get(
        f"/api/goals/{goal_id}/execution/reminders?include_closed=true"
    )
    assert history.status_code == 200
    assert history.json()[0]["status"] == "DISMISSED"
