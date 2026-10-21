from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from test_plan import _completed_plan

from planner_api.ai import ProviderUnavailableError
from planner_api.execution_calendar_service import ExecutionCalendarService
from planner_api.models import (
    Conversation,
    ExecutionDayLog,
    Message,
    PlanDay,
    PlanDaySchedule,
    PlanTask,
    ProgressEvent,
    RecoveryOperation,
    RouteStage,
)
from planner_api.schemas import (
    CalendarIntentResult,
    ExecutionDayMoveRequest,
    SmartCalendarRequest,
    SmartCalendarResult,
)


def _ready(client):
    workspace = _completed_plan(client)
    goal_id = workspace["goal_id"]
    assert client.post(f"/api/goals/{goal_id}/plan/confirm").status_code == 200
    return goal_id


def _started(client):
    goal_id = _ready(client)
    today = datetime.now(ZoneInfo("Asia/Shanghai")).date()
    response = client.post(
        f"/api/goals/{goal_id}/execution/start",
        json={
            "requested_start_date": str(today),
            "execution_weekdays": sorted([today.weekday(), (today.weekday() + 1) % 7]),
            "excluded_dates": [],
        },
    )
    assert response.status_code == 200
    return goal_id, client.get(f"/api/goals/{goal_id}/execution/calendar").json()["days"]


class CalendarProvider:
    def __init__(self, *, questions=None, requires_clarification=False):
        self.contexts = []
        self.questions = questions or []
        self.requires_clarification = requires_clarification
        self.arrange_calls = 0

    def interpret(self, context, validate):
        self.contexts.append(context)
        result = CalendarIntentResult(
            requested_start_date=context["preferred_start_date"] or context["current_date"],
            execution_weekdays=list(range(7)),
            constraints=[],
            rationale="按讨论日期安排，隔日推进。",
            rules=["隔日推进"],
            questions=self.questions,
            requires_clarification=self.requires_clarification,
        )
        validate(result)
        return result

    def arrange(self, context, validate):
        self.arrange_calls += 1
        start = context["preferred_start_date"] or context["current_date"]
        if context["established_policy"]:
            start = context["established_policy"]["requested_start_date"]
        cursor = max(
            [
                datetime.fromisoformat(a["scheduled_date"]).date()
                for a in context["previous_assignments"]
            ],
            default=datetime.fromisoformat(start).date() - timedelta(days=2),
        )
        assignments = []
        for day in context["batch_days"]:
            cursor += timedelta(days=2)
            assignments.append({"plan_day_id": day["plan_day_id"], "scheduled_date": cursor})
        result = SmartCalendarResult(
            requested_start_date=start,
            execution_weekdays=list(range(7)),
            assignments=assignments,
            rationale="按讨论日期安排，隔日推进。",
            rules=["隔日推进"],
            questions=self.questions,
        )
        validate(result)
        return result


def test_smart_dates_roundtrip_and_cache_inherit_discussion(client, session_factory, monkeypatch):
    goal_id = _ready(client)
    with session_factory() as session:
        conversation = session.scalar(select(Conversation).where(Conversation.goal_id == goal_id))
        session.add(
            Message(
                conversation_id=conversation.id,
                sequence=999,
                role="USER",
                content="周日要自己做套卷，国庆不中断。",
            )
        )
        session.commit()
    provider = CalendarProvider()
    monkeypatch.setattr(
        "planner_api.execution_calendar_service.ExecutionCalendarProvider",
        lambda _settings: provider,
    )
    response = client.post(f"/api/goals/{goal_id}/execution/calendar/smart-preview", json={})
    assert response.status_code == 200, response.text
    read = response.json()
    assert read["can_apply"]
    assert len(read["days"]) == 6
    assert len(read["input"]["execution_weekdays"]) == 7  # Not restricted to old weekly count.
    assert "周日要自己做套卷" in str(provider.contexts[0]["discussions"])
    assert (
        client.post(f"/api/goals/{goal_id}/execution/calendar/smart-preview", json={}).json()[
            "preview_id"
        ]
        == read["preview_id"]
    )
    assert len(provider.contexts) == 1
    assert provider.arrange_calls == 0
    assert "description" not in provider.contexts[0]["all_days"][0]
    assert "document" not in provider.contexts[0]["plan"]
    assert (
        client.post(f"/api/goals/{goal_id}/execution/preview", json=read["input"]).status_code
        == 200
    )
    started = client.post(f"/api/goals/{goal_id}/execution/start", json=read["input"])
    assert started.status_code == 200, started.text
    dates = client.get(f"/api/goals/{goal_id}/execution/calendar").json()["days"]
    assert [d["scheduled_date"] for d in dates] == [d["scheduled_date"] for d in read["days"]]
    with session_factory() as session:
        assert session.get(RecoveryOperation, read["preview_id"]).status == "APPLIED"


def test_smart_questions_prevent_start(client, session_factory):
    goal_id = _ready(client)
    with session_factory() as session:
        service = ExecutionCalendarService(
            session,
            CalendarProvider(
                questions=["规划没有套卷，需要补充吗？"],
                requires_clarification=True,
            ),
        )
        read = service.smart_preview(goal_id, SmartCalendarRequest())
        assert not read.can_apply
        with pytest.raises(HTTPException) as caught:
            service.execution.start(goal_id, read.input)
        assert caught.value.status_code == 409


def test_smart_optional_question_does_not_block_valid_preview(client, session_factory):
    goal_id = _ready(client)
    with session_factory() as session:
        read = ExecutionCalendarService(
            session,
            CalendarProvider(questions=["需要把可选休息日换到周六吗？"]),
        ).smart_preview(goal_id, SmartCalendarRequest())
        assert read.questions
        assert read.can_apply


def test_smart_model_failure_preserves_plan(client, session_factory):
    goal_id = _ready(client)

    class Failed:
        def interpret(self, *_args):
            raise ProviderUnavailableError(
                "模型暂时不可用，可手动安排",
                code="CALENDAR_GENERATION_FAILED",
                diagnostic="response was truncated after constraints",
            )

        def arrange(self, *_args):
            raise ProviderUnavailableError("模型暂时不可用，可手动安排")

    with session_factory() as session:
        service = ExecutionCalendarService(session, Failed())
        with pytest.raises(HTTPException) as caught:
            service.smart_preview(goal_id, SmartCalendarRequest())
        assert caught.value.status_code == 503
        assert caught.value.detail["diagnostic"] == "response was truncated after constraints"
        assert service.execution._goal_and_plan(goal_id)[0].status == "READY_TO_START"
        assert not list(session.scalars(select(PlanDaySchedule)))
        operation = session.scalar(
            select(RecoveryOperation).where(
                RecoveryOperation.goal_id == goal_id,
                RecoveryOperation.mode == "SMART_CALENDAR",
            )
        )
        assert operation.status == "FAILED"
        assert operation.preview_json["request"] == {
            "instructions": "",
            "preferred_start_date": None,
        }
        assert operation.preview_json["failure"] == {
            "phase": "UNDERSTANDING_RULES",
            "code": "CALENDAR_GENERATION_FAILED",
            "message": "模型暂时不可用，可手动安排",
            "diagnostic": "response was truncated after constraints",
            "retryable": True,
        }


def test_smart_dates_apply_default_rule_and_content_exception(client, session_factory):
    goal_id = _ready(client)

    class WithException(CalendarProvider):
        def interpret(self, context, validate):
            self.contexts.append(context)
            result = CalendarIntentResult(
                requested_start_date=context["current_date"],
                execution_weekdays=[0, 1, 2, 3, 4, 5],
                constraints=[
                    {
                        "day_codes": [context["all_days"][1]["code"]],
                        "allowed_weekdays": [6],
                    }
                ],
                rationale="常规内容周一到周六，专项检查放周日。",
                rules=["常规内容周一到周六", "专项检查周日"],
            )
            validate(result)
            return result

    with session_factory() as session:
        provider = WithException()
        read = ExecutionCalendarService(session, provider).smart_preview(
            goal_id, SmartCalendarRequest()
        )
        assert len(read.days) == 6
        assert read.days[1].scheduled_date.weekday() == 6
        assert all(
            item.scheduled_date.weekday() in range(6)
            for index, item in enumerate(read.days)
            if index != 1
        )
        assert provider.arrange_calls == 0


def test_smart_dates_preserve_confirmed_stage_interleaving(client, session_factory):
    goal_id = _ready(client)
    with session_factory() as session:
        service = ExecutionCalendarService(session, CalendarProvider())
        _, plan = service.execution._goal_and_plan(goal_id)
        days = service._days(plan)
        for day, stage_order in zip(days, [1, 2, 1, 2, 2, 2], strict=True):
            day.stage_order = stage_order
        for task in service.execution._tasks(plan.id):
            task.depends_on_json = []
        for stage in session.scalars(
            select(RouteStage).where(RouteStage.route_version_id == plan.route_version_id)
        ):
            stage.depends_on_json = [1] if stage.order == 2 else []
        session.commit()

        read = service.smart_preview(goal_id, SmartCalendarRequest())
        assert read.can_apply
        assert len(read.days) == 6


def test_smart_dates_still_reject_backward_task_dependency(client, session_factory):
    goal_id = _ready(client)
    with session_factory() as session:
        service = ExecutionCalendarService(session, CalendarProvider())
        _, plan = service.execution._goal_and_plan(goal_id)
        days = service._days(plan)
        first = session.scalar(
            select(PlanTask)
            .where(PlanTask.plan_day_id == days[0].id)
            .order_by(PlanTask.order_in_day)
        )
        later = session.scalar(
            select(PlanTask)
            .where(PlanTask.plan_day_id == days[1].id)
            .order_by(PlanTask.order_in_day)
        )
        first.depends_on_json = [later.id]
        session.commit()

        with pytest.raises(HTTPException) as caught:
            service.smart_preview(goal_id, SmartCalendarRequest())
        assert caught.value.status_code == 503
        assert "必须在" in caught.value.detail["diagnostic"]


def test_smart_retry_reuses_valid_intent_after_local_validation_failure(client, session_factory):
    goal_id = _ready(client)
    with session_factory() as session:
        provider = CalendarProvider()
        service = ExecutionCalendarService(session, provider)
        original_validate = service._validate_calendar
        first_call = True

        def fail_once(*args, **kwargs):
            nonlocal first_call
            if first_call:
                first_call = False
                raise ValueError("模拟本地校验器缺陷")
            return original_validate(*args, **kwargs)

        service._validate_calendar = fail_once
        with pytest.raises(HTTPException):
            service.smart_preview(goal_id, SmartCalendarRequest())
        read = service.smart_preview(goal_id, SmartCalendarRequest())

        assert read.can_apply
        assert len(provider.contexts) == 1
        operation = session.get(RecoveryOperation, read.preview_id)
        assert operation.preview_json["reused_intent"] is True
        assert operation.preview_json["attempt_count"] == 2


def test_smart_stale_or_foreign_preview_is_rejected(client, session_factory):
    goal_id = _ready(client)
    other_goal = _ready(client)
    with session_factory() as session:
        service = ExecutionCalendarService(session, CalendarProvider())
        read = service.smart_preview(goal_id, SmartCalendarRequest())
        with pytest.raises(HTTPException) as caught:
            service.execution.start(other_goal, read.input)
        assert caught.value.status_code == 404
        task = service.execution._tasks(service.execution._goal_and_plan(goal_id)[1].id)[0]
        task.description += "新增条件"
        session.commit()
        with pytest.raises(HTTPException) as caught:
            service.execution.start(goal_id, read.input)
        assert caught.value.status_code == 409


def test_advance_marks_prefix_complete_and_moves_suffix_to_today(client, session_factory):
    goal_id, days = _started(client)
    read = client.post(
        f"/api/goals/{goal_id}/execution/days/preview",
        json={
            "mode": "ADVANCE",
            "selected_day_id": days[2]["plan_day_id"],
        },
    ).json()
    assert read["can_apply"], read
    assert read["completed_task_count"] > 0
    before = client.get(f"/api/goals/{goal_id}/today").json()
    assert before["overall_progress"]["completed_tasks"] == 0
    result = client.post(
        f"/api/goals/{goal_id}/execution/days/apply", json={"preview_id": read["preview_id"]}
    )
    assert result.status_code == 200, result.text
    assert result.json()["scheduled_day"]["plan_day_id"] == days[2]["plan_day_id"]
    assert result.json()["overall_progress"]["completed_tasks"] == read["completed_task_count"]
    assert (
        client.post(
            f"/api/goals/{goal_id}/execution/days/apply", json={"preview_id": read["preview_id"]}
        ).status_code
        == 200
    )
    with session_factory() as session:
        logs = list(session.scalars(select(ExecutionDayLog)))
        assert all(log.metrics_json.get("focus_seconds", 0) == 0 for log in logs)
        acknowledgements = list(
            session.scalars(
                select(ProgressEvent).where(
                    ProgressEvent.event_type == "COMPLETED",
                )
            )
        )
        assert len(acknowledgements) == read["completed_task_count"]


def test_swap_checks_prerequisites_and_preserves_dates_on_block(client, session_factory):
    goal_id, days = _started(client)
    with session_factory() as session:
        first = session.scalar(
            select(PlanTask)
            .where(PlanTask.plan_day_id == days[0]["plan_day_id"])
            .order_by(PlanTask.order_in_day)
        )
        second = session.scalar(
            select(PlanTask)
            .where(PlanTask.plan_day_id == days[1]["plan_day_id"])
            .order_by(PlanTask.order_in_day)
        )
        second.depends_on_json = [first.id]
        session.commit()
    read = client.post(
        f"/api/goals/{goal_id}/execution/days/preview",
        json={
            "mode": "SWAP",
            "selected_day_id": days[1]["plan_day_id"],
        },
    ).json()
    assert not read["can_apply"]
    assert any("必须在" in b for b in read["blockers"])
    assert (
        client.post(
            f"/api/goals/{goal_id}/execution/days/apply", json={"preview_id": read["preview_id"]}
        ).status_code
        == 409
    )
    assert client.get(f"/api/goals/{goal_id}/execution/calendar").json()["days"] == days


def test_independent_day_swap_changes_only_two_days(client, session_factory):
    goal_id, days = _started(client)
    with session_factory() as session:
        for task in session.scalars(select(PlanTask)):
            task.depends_on_json = []
        for stage in session.scalars(select(RouteStage)):
            stage.depends_on_json = []
        session.commit()
    read = client.post(
        f"/api/goals/{goal_id}/execution/days/preview",
        json={
            "mode": "SWAP",
            "selected_day_id": days[3]["plan_day_id"],
        },
    ).json()
    assert read["can_apply"], read
    assert len(read["changes"]) == 2
    result = client.post(
        f"/api/goals/{goal_id}/execution/days/apply", json={"preview_id": read["preview_id"]}
    )
    assert result.status_code == 200, result.text
    assert result.json()["scheduled_day"]["plan_day_id"] == days[3]["plan_day_id"]
    assert result.json()["overall_progress"]["completed_tasks"] == 0
    current = {
        d["plan_day_id"]: d["scheduled_date"]
        for d in client.get(f"/api/goals/{goal_id}/execution/calendar").json()["days"]
    }
    for index, day in enumerate(days):
        expected = (
            days[3]["scheduled_date"]
            if index == 0
            else days[0]["scheduled_date"]
            if index == 3
            else day["scheduled_date"]
        )
        assert current[day["plan_day_id"]] == expected


def test_move_rechecks_stale_preview(client, session_factory):
    goal_id, days = _started(client)
    with session_factory() as session:
        service = ExecutionCalendarService(session)
        read = service.preview_move(
            goal_id, ExecutionDayMoveRequest(mode="ADVANCE", selected_day_id=days[2]["plan_day_id"])
        )
        task = session.scalar(
            select(PlanTask).where(PlanTask.plan_day_id == days[0]["plan_day_id"])
        )
        task.status = "COMPLETED"
        session.commit()
        with pytest.raises(HTTPException) as caught:
            service.apply_move(goal_id, read.preview_id)
        assert caught.value.status_code == 409


def _interleave_stages(session, goal_id, days):
    service = ExecutionCalendarService(session)
    _, plan = service.execution._goal_and_plan(goal_id)
    for item, stage_order in zip(days, [1, 1, 1, 2, 1, 2], strict=True):
        session.get(PlanDay, item["plan_day_id"]).stage_order = stage_order
    for task in service.execution._tasks(plan.id):
        task.depends_on_json = []
    for stage in session.scalars(
        select(RouteStage).where(RouteStage.route_version_id == plan.route_version_id)
    ):
        stage.depends_on_json = [1] if stage.order == 2 else []
    session.commit()
    return service, plan


def test_advance_from_rest_day_preserves_existing_stage_interleaving(client, session_factory):
    goal_id, days = _started(client)
    with session_factory() as session:
        service, plan = _interleave_stages(session, goal_id, days)
        for task in service.execution._tasks(plan.id):
            if task.plan_day_id in {item["plan_day_id"] for item in days[:2]}:
                task.status = "COMPLETED"
            elif task.plan_day_id == days[2]["plan_day_id"]:
                task.is_required = False
        session.commit()
        original_dates = {
            item["plan_day_id"]: datetime.fromisoformat(item["scheduled_date"]).date()
            for item in days
        }
        blockers, warnings = service._dependency_review(plan, original_dates)
        assert not blockers
        assert warnings

    before = client.get(f"/api/goals/{goal_id}/today").json()
    preview = client.post(
        f"/api/goals/{goal_id}/execution/days/preview",
        json={"mode": "ADVANCE", "selected_day_id": days[2]["plan_day_id"]},
    )
    assert preview.status_code == 200, preview.text
    read = preview.json()
    assert read["can_apply"], read
    assert read["completed_task_count"] == 0
    applied = client.post(
        f"/api/goals/{goal_id}/execution/days/apply", json={"preview_id": read["preview_id"]}
    )
    assert applied.status_code == 200, applied.text
    assert applied.json()["scheduled_day"]["plan_day_id"] == days[2]["plan_day_id"]
    assert applied.json()["overall_progress"] == before["overall_progress"]
    with session_factory() as session:
        assert not any(
            event.metadata_json.get("source") == "USER_ADVANCE_ACKNOWLEDGEMENT"
            for event in session.scalars(select(ProgressEvent))
        )


def test_swap_preserves_existing_stage_interleaving_without_worsening(client, session_factory):
    goal_id, days = _started(client)
    with session_factory() as session:
        _interleave_stages(session, goal_id, days)
    response = client.post(
        f"/api/goals/{goal_id}/execution/days/preview",
        json={"mode": "SWAP", "selected_day_id": days[1]["plan_day_id"]},
    )
    assert response.status_code == 200, response.text
    assert response.json()["can_apply"], response.json()
    assert (
        client.post(
            f"/api/goals/{goal_id}/execution/days/apply",
            json={"preview_id": response.json()["preview_id"]},
        ).status_code
        == 200
    )


@pytest.mark.parametrize("selected_index", [3, 4])
def test_swap_allows_new_stage_overlap_with_an_explicit_warning(
    client, session_factory, selected_index
):
    goal_id, days = _started(client)
    with session_factory() as session:
        _interleave_stages(session, goal_id, days)
    response = client.post(
        f"/api/goals/{goal_id}/execution/days/preview",
        json={"mode": "SWAP", "selected_day_id": days[selected_index]["plan_day_id"]},
    )
    assert response.status_code == 200, response.text
    read = response.json()
    assert read["can_apply"]
    assert not read["blockers"]
    assert any("交错推进" in warning for warning in read["warnings"])
    assert (
        client.post(
            f"/api/goals/{goal_id}/execution/days/apply", json={"preview_id": read["preview_id"]}
        ).status_code
        == 200
    )


def test_existing_stage_inversion_only_warns_when_it_grows(client, session_factory):
    goal_id, days = _started(client)
    with session_factory() as session:
        service, plan = _interleave_stages(session, goal_id, days)
        baseline = {
            item["plan_day_id"]: datetime.fromisoformat(item["scheduled_date"]).date()
            for item in days
        }
        preceding_id = days[4]["plan_day_id"]
        unchanged_blockers, unchanged_warnings = service._dependency_review(
            plan, baseline, baseline_dates=baseline
        )
        assert not unchanged_blockers
        assert not unchanged_warnings
        improved = {**baseline, preceding_id: baseline[preceding_id] - timedelta(days=1)}
        improved_blockers, improved_warnings = service._dependency_review(
            plan, improved, baseline_dates=baseline
        )
        assert not improved_blockers
        assert not improved_warnings
        worsened = {**baseline, preceding_id: baseline[preceding_id] + timedelta(days=1)}
        worsened_blockers, worsened_warnings = service._dependency_review(
            plan, worsened, baseline_dates=baseline
        )
        assert not worsened_blockers
        assert worsened_warnings


def test_task_prerequisites_remain_strict_despite_existing_stage_interleaving(
    client, session_factory
):
    goal_id, days = _started(client)
    with session_factory() as session:
        service, plan = _interleave_stages(session, goal_id, days)
        tasks = service.execution._tasks(plan.id)
        first = next(task for task in tasks if task.plan_day_id == days[0]["plan_day_id"])
        second = next(task for task in tasks if task.plan_day_id == days[1]["plan_day_id"])
        second.depends_on_json = [first.id]
        session.commit()
    read = client.post(
        f"/api/goals/{goal_id}/execution/days/preview",
        json={"mode": "SWAP", "selected_day_id": days[1]["plan_day_id"]},
    ).json()
    assert not read["can_apply"]
    assert any("必须在" in blocker for blocker in read["blockers"])
