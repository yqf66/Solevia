import json
from datetime import date, timedelta
from urllib.error import HTTPError, URLError

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from planner_api.ai import ProviderUnavailableError
from planner_api.config import Settings
from planner_api.light_arrangement_ai import LightArrangementProvider
from planner_api.light_arrangement_service import LightArrangementService
from planner_api.light_schemas import LightDraftCreate, LightDraftResult, LightRevisionRequest
from planner_api.models import Goal, LightArrangementDraft, ManualPlan, RequirementFact


def create(
    client: TestClient,
    raw="修改简历\n练习口语",
    start_date="2026-09-15",
) -> dict:
    response = client.post(
        "/api/light-arrangements",
        json={
            "raw_input": raw,
            "start_date": start_date,
            "horizon_days": 7,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def edit(client: TestClient, draft: dict, **changes) -> dict:
    response = client.patch(
        f"/api/light-arrangements/{draft['id']}",
        json={
            "revision": draft["revision"],
            "result": {**draft["result"], **changes},
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def publish(client: TestClient, draft: dict) -> dict:
    response = client.post(
        f"/api/light-arrangements/{draft['id']}/apply", json={"revision": draft["revision"]}
    )
    assert response.status_code == 200, response.text
    return client.get(f"/api/manual-plans/{response.json()['manual_plan_id']}").json()


def next_weekday(weekday: int) -> date:
    today = date.today()
    offset = (weekday - today.weekday()) % 7 or 7
    return today + timedelta(days=offset)


def test_upgrade_preserves_bounded_recurrence_in_raw_and_durable_memory(client, session_factory):
    from planner_api.planning_context import build_requirement_memory

    draft = create(client, "接下来三天每天做5分钟卡片")
    task = {**draft["result"]["tasks"][0], "scheduled_date": "2026-09-15",
            "recurrence": "DAILY", "recurrence_end_date": "2026-09-17", "estimate_minutes": 5}
    plan = publish(client, edit(client, draft, tasks=[task]))
    path = f"/api/manual-plans/{plan['id']}/tasks/{plan['tasks'][0]['id']}"
    assert client.patch(path, json={"scope": "TODAY", "scheduled_date": "2026-09-15",
                                    "status": "COMPLETED"}).status_code == 200
    upgraded = client.post(f"/api/manual-plans/{plan['id']}/upgrade")
    assert upgraded.status_code == 200
    goal_id = upgraded.json()["goal_id"]
    with session_factory() as session:
        assert "重复截止日期（含当日）2026-09-17" in session.get(Goal, goal_id).raw_input
        source = session.get(ManualPlan, plan["id"])
        source.status = "ARCHIVED"
        session.commit()
        memory = build_requirement_memory(session, goal_id)["quick_arrangement_handoff"]
        assert len(memory) == 1
        assert memory[0]["recurrence_end_date"] == "2026-09-17"
        assert memory[0]["recorded_occurrences"][0]["status"] == "COMPLETED"
        assert memory[0]["recorded_occurrences"][0]["date"] == "2026-09-15"


def test_fallback_is_durable_editable_and_idempotent(client):
    draft = create(client)
    assert [item["title"] for item in draft["result"]["tasks"]] == ["修改简历", "练习口语"]
    assert all(item["scheduled_date"] is None for item in draft["result"]["tasks"])
    failed = client.post(
        f"/api/light-arrangements/{draft['id']}/generate", json={"revision": draft["revision"]}
    ).json()
    assert failed["status"] == "FAILED"
    assert failed["result"] == draft["result"]
    restored = client.get(f"/api/light-arrangements/{draft['id']}").json()
    assert restored["result"] == failed["result"]
    assert restored["status"] == failed["status"]
    assert restored["revision"] == failed["revision"]
    plan = publish(client, failed)
    assert plan["kind"] == "QUICK"
    assert plan["description"] == draft["raw_input"]
    assert publish(client, failed)["id"] == plan["id"]
    assert len(client.get("/api/manual-plans").json()) == 1
    assert client.get("/api/light-arrangements").json() == []
    assert client.get("/api/today?on_date=2026-09-15").json()["flexible_tasks"] == []


def test_edit_rejects_stale_revision_and_dates_outside_horizon(client):
    draft = create(client)
    updated = edit(client, draft, title="这周的杂事")
    response = client.patch(
        f"/api/light-arrangements/{draft['id']}",
        json={
            "revision": draft["revision"],
            "result": draft["result"],
        },
    )
    assert response.status_code == 409
    result = updated["result"]
    result["tasks"][0]["scheduled_date"] = "2026-09-22"
    response = client.patch(
        f"/api/light-arrangements/{draft['id']}",
        json={
            "revision": updated["revision"],
            "result": result,
        },
    )
    assert response.status_code == 422
    assert (
        client.get(f"/api/light-arrangements/{draft['id']}").json()["revision"]
        == updated["revision"]
    )


def test_recurring_completion_skip_edit_and_allocation_are_per_date(client):
    draft = create(client, "每天练习口语")
    task = {**draft["result"]["tasks"][0], "scheduled_date": "2026-09-15", "recurrence": "DAILY"}
    plan = publish(client, edit(client, draft, tasks=[task]))
    task_id = plan["tasks"][0]["id"]
    assert plan["tasks"][0]["recurrence_end_date"] == "2026-09-21"
    path = f"/api/manual-plans/{plan['id']}/tasks/{task_id}"
    assert (
        client.patch(
            path,
            json={
                "scope": "TODAY",
                "scheduled_date": "2026-09-15",
                "status": "COMPLETED",
                "description": "已练习",
                "estimate_minutes": 20,
            },
        ).status_code
        == 200
    )
    first = client.get("/api/today?on_date=2026-09-15").json()["flexible_tasks"][0]
    second = client.get("/api/today?on_date=2026-09-16").json()["flexible_tasks"][0]
    assert (first["status"], first["description"], first["estimate_minutes"]) == (
        "COMPLETED",
        "已练习",
        20,
    )
    assert (second["status"], second["estimate_minutes"], second["plan_kind"]) == (
        "NOT_STARTED",
        30,
        "QUICK",
    )
    assert (
        client.patch(
            path, json={"scope": "TODAY", "scheduled_date": "2026-09-16", "status": "SKIPPED"}
        ).status_code
        == 200
    )
    assert client.get("/api/today?on_date=2026-09-16").json()["flexible_tasks"] == []
    assert (
        client.get("/api/today?on_date=2026-09-17").json()["flexible_tasks"][0]["status"]
        == "NOT_STARTED"
    )
    response = client.post(
        "/api/today/schedule",
        json={
            "task_id": task_id,
            "source": "MANUAL",
            "scheduled_date": "2026-09-17",
            "start_local": "19:00",
            "end_local": "19:30",
        },
    )
    assert response.status_code == 200, response.text
    assert (
        client.get("/api/today?on_date=2026-09-17").json()["fixed_tasks"][0]["start_local"]
        == "19:00"
    )
    assert (
        client.get("/api/today?on_date=2026-09-18").json()["flexible_tasks"][0]["start_local"]
        is None
    )
    assert client.get("/api/today?on_date=2026-09-22").json()["flexible_tasks"] == []


def test_fixed_conflict_preserves_existing_plan_and_draft(client):
    old = client.post(
        "/api/manual-plans",
        json={
            "title": "会议",
            "tasks": [
                {
                    "title": "例会",
                    "estimate_minutes": 60,
                    "scheduled_date": "2026-09-15",
                    "start_local": "09:00",
                    "end_local": "10:00",
                }
            ],
        },
    ).json()
    draft = create(client, "修改简历")
    task = {
        **draft["result"]["tasks"][0],
        "scheduled_date": "2026-09-15",
        "start_local": "09:30",
        "end_local": "10:00",
    }
    draft = edit(client, draft, tasks=[task])
    response = client.post(
        f"/api/light-arrangements/{draft['id']}/apply", json={"revision": draft["revision"]}
    )
    assert response.status_code == 409
    preserved = client.get(f"/api/manual-plans/{old['id']}").json()
    assert preserved["tasks"] == old["tasks"]
    assert preserved["title"] == old["title"]
    assert client.get(f"/api/light-arrangements/{draft['id']}").json()["status"] == "READY"
    assert len(client.get("/api/manual-plans").json()) == 1


def test_shared_unavailability_and_current_plan_only_allocation(client):
    target = next_weekday(1).isoformat()
    draft = create(client, "口语练习", target)
    task = {**draft["result"]["tasks"][0], "scheduled_date": target}
    plan = publish(
        client,
        edit(
            client,
            draft,
            tasks=[task],
            unavailable_windows=[
                {
                    "date": target,
                    "start_local": "19:00",
                    "end_local": "20:00",
                    "reason": "聚餐",
                }
            ],
        ),
    )
    other = client.post(
        "/api/manual-plans",
        json={
            "title": "其他安排",
            "tasks": [
                {
                    "title": "读书",
                    "estimate_minutes": 30,
                    "scheduled_date": target,
                }
            ],
        },
    ).json()
    response = client.post(
        "/api/today/allocation/preview",
        json={
            "scheduled_date": target,
            "manual_plan_id": plan["id"],
            "use_ai": False,
            "windows": [{"weekday": 1, "start_local": "19:00", "end_local": "22:00"}],
        },
    )
    assert response.status_code == 200, response.text
    preview = response.json()
    assert {item["task_id"] for item in preview["assignments"]} == {plan["tasks"][0]["id"]}
    assert preview["assignments"][0]["start_local"] >= "20:00"
    blocked = client.post(
        "/api/today/schedule",
        json={
            "source": "MANUAL",
            "task_id": other["tasks"][0]["id"],
            "scheduled_date": target,
            "start_local": "19:00",
            "end_local": "19:30",
        },
    )
    assert blocked.status_code == 409


def test_add_move_skip_and_upgrade_preserve_progress(client):
    plan = publish(client, create(client))
    added = client.post(
        f"/api/manual-plans/{plan['id']}/tasks",
        json={
            "title": "跑步",
            "estimate_minutes": 30,
            "scheduled_date": "2026-09-15",
            "recurrence": "DAILY",
        },
    )
    assert added.status_code == 201, added.text
    task = added.json()["tasks"][-1]
    path = f"/api/manual-plans/{plan['id']}/tasks/{task['id']}"
    client.patch(
        path, json={"status": "COMPLETED", "scope": "TODAY", "scheduled_date": "2026-09-15"}
    )
    before = client.get(f"/api/manual-plans/{plan['id']}").json()["tasks"]
    upgraded = client.post(f"/api/manual-plans/{plan['id']}/upgrade")
    assert upgraded.status_code == 200, upgraded.text
    assert client.post(f"/api/manual-plans/{plan['id']}/upgrade").json() == upgraded.json()
    after = client.get(f"/api/manual-plans/{plan['id']}").json()
    assert after["tasks"] == before
    workspace = client.get(f"/api/goals/{upgraded.json()['goal_id']}/clarification").json()
    assert "COMPLETED" in workspace["raw_input"]
    assert after["arrangement"]["upgraded_goal_id"] == workspace["goal_id"]
    once = before[0]
    assert (
        client.patch(
            f"/api/manual-plans/{plan['id']}/tasks/{once['id']}",
            json={
                "scheduled_date": "2026-09-16",
                "status": "SKIPPED",
            },
        ).status_code
        == 200
    )


def test_started_successor_archives_source_without_deleting_history(client, session_factory):
    from test_plan import _completed_plan

    source = publish(client, create(client, "每天健身"))
    workspace = _completed_plan(client)
    goal_id = workspace["goal_id"]
    with session_factory() as session:
        plan = session.get(ManualPlan, source["id"])
        plan.arrangement_json = {**plan.arrangement_json, "upgraded_goal_id": goal_id}
        session.commit()

    assert client.post(f"/api/goals/{goal_id}/plan/confirm").status_code == 200
    started = client.post(
        f"/api/goals/{goal_id}/execution/start",
        json={
            "requested_start_date": date.today().isoformat(),
            "execution_weekdays": sorted(
                {date.today().weekday(), (date.today().weekday() + 2) % 7}
            ),
            "excluded_dates": [],
        },
    )
    assert started.status_code == 200, started.text
    archived = client.get(f"/api/manual-plans/{source['id']}").json()
    assert archived["status"] == "ARCHIVED"
    assert archived["arrangement"]["handoff_start_date"] == date.today().isoformat()
    assert archived["tasks"] == source["tasks"]
    blocked = client.patch(
        f"/api/manual-plans/{source['id']}/tasks/{source['tasks'][0]['id']}",
        json={"status": "COMPLETED"},
    )
    assert blocked.status_code == 409
    assert client.post(f"/api/manual-plans/{source['id']}/restore").status_code == 409


def test_invalid_provider_result_never_overwrites_fallback(session_factory):
    class OutOfScopeProvider:
        def arrange(self, request):
            return LightDraftResult(
                title="错误结果",
                tasks=[
                    {
                        "title": "不该采用",
                        "estimate_minutes": 30,
                        "scheduled_date": "2026-12-01",
                    }
                ],
            )

    with session_factory() as session:
        service = LightArrangementService(session, OutOfScopeProvider())
        draft = service.create(LightDraftCreate(raw_input="原始事项", start_date=date(2026, 9, 15)))
        failed = service.generate(draft.id, LightRevisionRequest(revision=draft.revision))
        assert failed.status == "FAILED"
        assert failed.result == draft.result
        assert failed.raw_input == "原始事项"


def test_interrupted_generation_can_be_manually_recovered(client, session_factory):
    draft = create(client)
    with session_factory() as session:
        saved = session.get(LightArrangementDraft, draft["id"])
        saved.status = "GENERATING"
        session.commit()
    recovered = edit(client, draft, title="手动接管")
    assert recovered["status"] == "READY"
    assert publish(client, recovered)["title"] == "手动接管"


def test_late_model_output_does_not_overwrite_manual_recovery(session_factory):
    class DelayedProvider:
        def arrange(self, request):
            with session_factory() as another:
                saved = another.get(LightArrangementDraft, draft.id)
                saved.revision += 1
                saved.status = "READY"
                saved.result_json = {**saved.result_json, "title": "手动新版本"}
                another.commit()
            return LightDraftResult(title="迟到的模型版本", tasks=[])

    with session_factory() as session:
        service = LightArrangementService(session, DelayedProvider())
        draft = service.create(LightDraftCreate(raw_input="原始事项", start_date=date(2026, 9, 15)))
        recovered = service.generate(draft.id, LightRevisionRequest(revision=draft.revision))
        assert recovered.status == "READY"
        assert recovered.result.title == "手动新版本"


def test_future_series_edit_keeps_recorded_occurrences(client):
    draft = create(client, "每天练口语")
    task = {**draft["result"]["tasks"][0], "recurrence": "DAILY", "scheduled_date": "2026-09-15"}
    plan = publish(client, edit(client, draft, tasks=[task]))
    path = f"/api/manual-plans/{plan['id']}/tasks/{plan['tasks'][0]['id']}"
    client.patch(
        path, json={"scope": "TODAY", "scheduled_date": "2026-09-17", "status": "COMPLETED"}
    )
    changed = client.patch(
        path,
        json={
            "scope": "FUTURE",
            "scheduled_date": "2026-09-16",
            "title": "每天口语",
            "estimate_minutes": 20,
        },
    )
    assert changed.status_code == 200, changed.text
    assert len(changed.json()["tasks"]) == 2
    board = client.get("/api/today?on_date=2026-09-17").json()
    assert board["flexible_tasks"][0]["status"] == "COMPLETED"
    assert board["flexible_tasks"][0]["estimate_minutes"] == 20


def test_light_schedule_respects_active_goal_course_constraints(client, session_factory):
    target = next_weekday(1).isoformat()
    with session_factory() as session:
        goal = Goal(title="已有目标", raw_input="学习", status="ACTIVE", timezone="Asia/Shanghai")
        session.add(goal)
        session.flush()
        session.add(
            RequirementFact(
                goal_id=goal.id,
                category="CONSTRAINT",
                field_key="course",
                value_json="每天19:00到20:00有课",
                source="USER",
                status="CONFIRMED",
            )
        )
        session.commit()
    draft = create(client, "整理文件", target)
    task = {**draft["result"]["tasks"][0], "scheduled_date": target}
    plan = publish(client, edit(client, draft, tasks=[task]))
    preview = client.post(
        "/api/today/allocation/preview",
        json={
            "scheduled_date": target,
            "manual_plan_id": plan["id"],
            "use_ai": False,
            "windows": [{"weekday": 1, "start_local": "19:00", "end_local": "22:00"}],
        },
    )
    assert preview.status_code == 200, preview.text
    assert preview.json()["assignments"][0]["start_local"] >= "20:00"


@pytest.mark.parametrize("raw", ["a" * 12000, "\n".join(f"task-{i}" for i in range(40))])
def test_maximum_input_always_has_manual_fallback(client, raw):
    draft = create(client, raw)
    assert draft["raw_input"] == raw
    assert len(draft["result"]["tasks"]) <= 30


def test_user_can_move_once_task_beyond_original_generation_horizon(client):
    plan = publish(client, create(client))
    task = plan["tasks"][0]
    changed = client.patch(
        f"/api/manual-plans/{plan['id']}/tasks/{task['id']}",
        json={
            "scheduled_date": "2026-10-15",
        },
    )
    assert changed.status_code == 200, changed.text
    assert changed.json()["tasks"][0]["scheduled_date"] == "2026-10-15"


def test_archive_restore_and_discard_keep_other_plans_untouched(client):
    discarded = create(client, "丢弃的草案")
    response = client.request(
        "DELETE",
        f"/api/light-arrangements/{discarded['id']}",
        json={"revision": discarded["revision"]},
    )
    assert response.status_code == 204
    assert client.get(f"/api/light-arrangements/{discarded['id']}").status_code == 404
    draft = create(client, "健身")
    task = {**draft["result"]["tasks"][0], "scheduled_date": "2026-09-15"}
    plan = publish(client, edit(client, draft, tasks=[task]))
    assert client.post(f"/api/manual-plans/{plan['id']}/archive").json()["status"] == "ARCHIVED"
    assert client.get("/api/today?on_date=2026-09-15").json()["flexible_tasks"] == []
    assert client.post(f"/api/manual-plans/{plan['id']}/restore").json()["status"] == "ACTIVE"
    assert (
        client.get("/api/today?on_date=2026-09-15").json()["flexible_tasks"][0]["title"] == "健身"
    )


def provider(transport, **options):
    return LightArrangementProvider(
        Settings(
            _env_file=None,
            llm_provider="openai_compatible",
            llm_api_key=SecretStr("test-key"),
            llm_base_url="https://example.test/v1",
            llm_model="test",
            llm_max_retries=2,
            **options,
        ),
        transport=transport,
        sleeper=lambda _: None,
    )


def test_provider_repairs_invalid_output_with_feedback():
    calls = []

    def transport(url, headers, payload, timeout):
        calls.append(json.loads(json.dumps(payload)))
        output = {
            "title": "近期事项",
            "tasks": [
                {
                    "title": "事项",
                    "estimate_minutes": 30,
                    "scheduled_date": "2026-12-01" if len(calls) == 1 else "2026-09-15",
                }
            ],
        }
        return {"choices": [{"message": {"content": json.dumps(output)}, "finish_reason": "stop"}]}

    result = provider(transport).arrange(
        LightDraftCreate(raw_input="事项", start_date=date(2026, 9, 15))
    )
    assert result.tasks[0].scheduled_date == date(2026, 9, 15)
    assert len(calls) == 2
    assert "修复上次输出" in calls[1]["messages"][0]["content"]


def test_responses_ignores_reasoning_and_combines_output_chunks():
    response = {
        "status": "completed",
        "output": [
            {"type": "reasoning", "content": [{"type": "text", "text": "not JSON"}]},
            {
                "type": "message",
                "content": [
                    {"type": "output_text", "text": '{"title":'},
                    {"type": "output_text", "text": '"安排","tasks":[]}'},
                ],
            },
        ],
    }
    parsed = LightDraftResult.model_validate_json(LightArrangementProvider._text(response))
    assert parsed.title == "安排"


@pytest.mark.parametrize("failure", ["timeout", "malformed", "truncated", "empty", "unauthorized"])
def test_provider_has_bounded_retries(failure):
    calls = []

    def transport(url, headers, payload, timeout):
        calls.append(1)
        if failure == "timeout":
            raise URLError("timed out")
        if failure == "unauthorized":
            raise HTTPError(url, 401, "Unauthorized", {}, None)
        if failure == "empty":
            return {"choices": []}
        return {
            "choices": [
                {
                    "message": {"content": "{}" if failure == "truncated" else "not-json"},
                    "finish_reason": "length" if failure == "truncated" else "stop",
                }
            ]
        }

    with pytest.raises(ProviderUnavailableError):
        provider(transport).arrange(
            LightDraftCreate(raw_input="事项", start_date=date(2026, 9, 15))
        )
    assert len(calls) == (1 if failure == "unauthorized" else 3)
