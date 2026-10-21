"""Regression cases for the saved real P0 failures, without provider calls."""
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

from fastapi import HTTPException
import pytest
from sqlalchemy import select

from planner_api.execution_service import ExecutionService
from planner_api.models import (Conversation, Goal, Message, PlanActivation, PlanDay, PlanDaySchedule,
                                PlanTask, PlanWorkItem, RequirementFact, RequirementVersion, WorkItemDependency)
from planner_api.plan_ai import normalize_hybrid_contract
from planner_api.plan_capacity import capacity_assessment, capacity_segment_summary, week_capacity
from planner_api.service import ClarificationService
from planner_api.plan_service import PlanService
from planner_api.schemas import ExecutionScheduleRequest, HybridPlanDetailResult, PlanStageContributionDraft, ProposedFact
from planner_api.temporal_contract import local_date, relative_day_range, snapshot_deadline
from planner_api.work_item_materialization import WorkItemMaterializer
from planner_api.work_item_validation import WorkItemValidationError, validate_work_item_batch
from planner_api.work_item_domain import PlanWorkItemDraft
from test_plan_capacity_contract import _capacity_plan, _day
from test_work_item_foundation import _plan_graph


def test_authorized_extension_updates_related_standing_work_window():
    from planner_api.schemas import PlanTemporalSegmentDraft
    plan = _capacity_plan("OUTCOME")
    plan.commitments_json = [{"label": "报告一", "start_week": 1, "end_week": 4,
        "cadence": "TOTAL", "minimum_occurrences": 1, "minimum_total_minutes": 700,
        "workload_key": "REPORT1", "match_terms": ["报告一"], "source_text": "报告一700分钟"}]
    segment = PlanTemporalSegmentDraft(label="五周容量", start_week=1, end_week=5,
        date_scope="授权五周", capacity_summary="每周原时间不变", suggested_daily_minutes=120,
        weekly_execution_days=1, available_days_per_week=4)
    item = PlanWorkItemDraft(item_key="REPORT1", scope_type="PLAN", title="完成报告一",
        description="按原工作量完成报告一", estimate_minutes_per_occurrence=700,
        task_kind="LEARN", priority=1, source_refs=["用户报告要求"],
        commitment_refs=["报告一"], constraint_set={"binding_mode":"WINDOW", "fulfillment_mode":"ONCE", "temporal": {"start_week":1,"end_week":4}})
    outline = SimpleNamespace(total_weeks=5, temporal_segments=[], standing_work_items=[item], commitments=[])
    proposal = SimpleNamespace(capacity_segments=[segment], total_weeks_change=1)
    PlanService._restore_negotiated_contract(plan, outline, proposal)
    assert outline.commitments[0].end_week == item.constraint_set.temporal.end_week == 5
    assert outline.commitments[0].minimum_total_minutes == item.estimate_minutes_per_occurrence == 700


def _sunday_plan(session):
    goal, plan, *_ = _plan_graph(session, execution_model_version="HYBRID_WORK_ITEM")
    for schedule in session.scalars(select(PlanDaySchedule).join(PlanActivation).where(PlanActivation.plan_version_id == plan.id)):
        session.delete(schedule)
    session.flush()
    for activation in session.scalars(select(PlanActivation).where(PlanActivation.plan_version_id == plan.id)):
        session.delete(activation)
    day = session.scalar(select(PlanDay).where(PlanDay.plan_version_id == plan.id))
    item = PlanWorkItem(plan_version_id=plan.id, item_key="SUNDAY_ONLY", scope_type="PLAN",
        title="周日阅读", description="只能在周日阅读", task_kind="LEARN", estimate_minutes=30, priority=1,
        binding_mode="WINDOW", fulfillment_mode="ONCE", target_value=1,
        constraints_json={"temporal": {"start_week": 1, "end_week": 1, "allowed_weekdays": [6]},
                          "completion": {"target_value": 1}})
    session.add(item)
    session.flush()
    session.add(PlanTask(plan_version_id=plan.id, plan_day_id=day.id, order_in_day=1,
        title="周日阅读动作", description="阅读并记录要点", estimate_minutes=30, priority=1,
        task_kind="LEARN", source_work_item_id=item.id, scheduling_policy_json={"work_item_keys": [item.item_key]}))
    for task in session.scalars(select(PlanTask).where(PlanTask.plan_day_id == day.id)):
        task.source_work_item_id = item.id
        task.scheduling_policy_json = {"work_item_keys": [item.item_key]}
    plan.total_weeks = 1
    plan.weekly_execution_days = 1
    plan.temporal_segments_json = [{"start_week": 1, "end_week": 1, "available_days_per_week": 1,
                                   "suggested_daily_minutes": 60}]
    goal.status = "READY_TO_START"
    goal.active_plan_version_id = plan.id
    session.flush()
    return goal, plan


def test_confirmed_short_weekend_supply_is_not_increased():
    raw = {"total_weeks": 1, "temporal_segments": [{"start_week": 1, "end_week": 1,
        "available_days_per_week": 5, "suggested_daily_minutes": 120,
        "exception_days_per_week": 1, "exception_daily_minutes": 60, "exception_weekdays": [5]}]}
    normalize_hybrid_contract(raw)
    plan = SimpleNamespace(temporal_segments_json=raw["temporal_segments"], weekly_execution_days=1)
    assert week_capacity(plan, 1)["weekly_minutes"] == 540
    summary = capacity_segment_summary(raw["temporal_segments"][0])
    assert "4天×120" in summary and "1天×60" in summary


@pytest.mark.parametrize("relative", ["W1_SUNDAY_2500_2600", "W1_AFTER_EXAM", "W1-D8", "garbageW1-D1"])
def test_opaque_relative_dates_are_not_silently_first_day(relative):
    request = ExecutionScheduleRequest(requested_start_date=date(2026, 10, 12), execution_weekdays=[6])
    with pytest.raises(HTTPException) as error:
        WorkItemMaterializer._date_range(SimpleNamespace(total_weeks=1), request, {"relative_day_ref": relative})
    assert error.value.status_code == 409


def test_historical_named_sunday_is_fully_parsed_not_truncated():
    request = ExecutionScheduleRequest(requested_start_date=date(2026, 10, 12), execution_weekdays=[6])
    assert WorkItemMaterializer._date_range(SimpleNamespace(total_weeks=1), request,
        {"relative_day_ref": "W1_SUNDAY_0900_1030", "allowed_weekdays": [6]}) == (
            date(2026, 10, 18), date(2026, 10, 18))
    item = SimpleNamespace(id="accept", constraints_json={"temporal": {"relative_day_ref": "W1_AFTER_EXAM"}})
    edge = SimpleNamespace(successor_work_item_id="accept")
    assert WorkItemMaterializer.item_temporal(None, item, [edge])["relative_day_ref"] == "W1"
    assert WorkItemMaterializer.item_temporal(None, item, [])["relative_day_ref"] == "W1_AFTER_EXAM"


def test_bare_week_is_a_window_and_relative_day_is_clipped_by_deadline():
    anchor = date(2026, 10, 12)
    assert relative_day_range("W1", anchor) == (anchor, date(2026, 10, 18))
    assert relative_day_range("M1-W2-D3", anchor) == (date(2026, 10, 21), date(2026, 10, 21))
    request = ExecutionScheduleRequest(requested_start_date=anchor, execution_weekdays=[6])
    assert WorkItemMaterializer._date_range(SimpleNamespace(total_weeks=1), request,
        {"relative_day_ref": "W1", "latest_date": "2026-10-16"}) == (anchor, date(2026, 10, 16))


def test_relative_deadline_uses_original_message_date_even_after_delayed_confirmation():
    snapshot = {"deadline": "本周日", "temporal_context": {"deadline": {
        "source_text": "本周日", "reference_date": "2026-10-10", "resolved_date": "2026-10-11"}}}
    assert snapshot_deadline(snapshot, timezone="Asia/Shanghai",
                            fallback_time=datetime(2026, 10, 15, tzinfo=UTC)) == date(2026, 10, 11)
    snapshot["deadline"] = "2026-10-25"
    assert snapshot_deadline(snapshot, timezone="Asia/Shanghai") == date(2026, 10, 25)
    assert local_date(datetime(2026, 10, 10, 17, tzinfo=UTC), "Asia/Shanghai") == date(2026, 10, 11)


def test_manual_calendar_preserves_sunday_constraint_and_rejects_monday(session_factory):
    with session_factory() as session:
        goal, plan = _sunday_plan(session)
        service = ExecutionService(session)
        anchor = service._today_for(goal)
        request = ExecutionScheduleRequest(requested_start_date=anchor, execution_weekdays=[0])
        with pytest.raises(HTTPException) as error:
            service.preview(goal.id, request)
        assert error.value.detail["code"] == "PLAN_ITEM_DATE_CONFLICT"
        sunday = service.preview(goal.id, request.model_copy(update={"execution_weekdays": [6]}))
        assert all(day.scheduled_date.weekday() == 6 for day in sunday.upcoming_days)
        assert service.get_setup(goal.id).suggested_weekdays == [6]


def test_delayed_start_rechecks_expired_deadline_without_publishing(session_factory):
    with session_factory() as session:
        goal, plan = _sunday_plan(session)
        service = ExecutionService(session)
        today = service._today_for(goal)
        requirement = RequirementVersion(goal_id=goal.id, version=99, status="CONFIRMED",
                                         snapshot_json={"deadline": (today - timedelta(days=1)).isoformat()})
        session.add(requirement)
        session.flush()
        goal.current_requirement_version_id = requirement.id
        with pytest.raises(HTTPException) as error:
            service.start(goal.id, ExecutionScheduleRequest(requested_start_date=today, execution_weekdays=[6]))
        assert error.value.detail["code"] == "PLAN_START_DEADLINE_CONFLICT"
        assert service._activation(plan.id) is None
        assert goal.status == "READY_TO_START"


def test_future_start_cannot_push_execution_beyond_unexpired_deadline(session_factory):
    with session_factory() as session:
        goal, _ = _sunday_plan(session)
        service = ExecutionService(session)
        anchor = service._today_for(goal)
        requirement = RequirementVersion(goal_id=goal.id, version=99, status="CONFIRMED",
            snapshot_json={"deadline": (anchor + timedelta(days=1)).isoformat()})
        session.add(requirement)
        session.flush()
        goal.current_requirement_version_id = requirement.id
        with pytest.raises(HTTPException) as error:
            service.preview(goal.id, ExecutionScheduleRequest(
                requested_start_date=anchor + timedelta(days=3), execution_weekdays=[6]))
        assert error.value.detail["code"] == "PLAN_START_DEADLINE_CONFLICT"


def test_reference_only_detail_is_valid_but_missing_unknown_or_wrong_week_is_not():
    result = HybridPlanDetailResult(work_items=[], days=[_day(1, 1, 30)])
    result.days[0].tasks[0].work_item_keys = ["KNOWN"]
    kwargs = dict(work_items=[], groups=[], dependencies=[], milestone_weeks={1: (1, 1)},
                  commitment_labels=set(), existing_item_keys=["KNOWN"], requested_weeks={1})
    validate_work_item_batch(**kwargs, referenced_item_weeks={"KNOWN": {1}})
    for references in [{}, {"MISSING": {1}}, {"KNOWN": {2}}]:
        with pytest.raises(WorkItemValidationError):
            validate_work_item_batch(**kwargs, referenced_item_weeks=references)


def test_one_action_can_cover_multiple_real_stages_without_duplicating_minutes():
    plan = _capacity_plan("OUTCOME")
    day = _day(1, 1, 30)
    day.stage_order = 2
    task = day.tasks[0]
    task.work_item_keys = ["KNOWN"]
    task.stage_contributions = [PlanStageContributionDraft(stage_order=index, action=f"第{index}阶段的实际验收动作")
                                for index in [1, 3]]
    milestones = {index: SimpleNamespace(start_week=1, end_week=1) for index in [1, 2, 3]}
    PlanService._validate_detail_result(plan, milestones, [1], [day])
    assert sum(task.estimate_minutes for task in day.tasks) == 30
    task.stage_contributions[0].stage_order = 4
    with pytest.raises(HTTPException) as error:
        PlanService._validate_detail_result(plan, milestones, [1], [day])
    assert error.value.detail["code"] == "PLAN_STAGE_CONTRIBUTION_INVALID"


def test_deadline_snapshot_retains_source_message_date_not_review_date(session_factory):
    with session_factory() as session:
        goal = Goal(title="延迟确认", raw_input="本周日交稿", timezone="Asia/Shanghai")
        session.add(goal)
        session.flush()
        conversation = Conversation(goal_id=goal.id, phase="CLARIFICATION")
        session.add(conversation)
        session.flush()
        message = Message(conversation_id=conversation.id, sequence=1, role="USER", content="本周日交稿",
                          created_at=datetime(2026, 10, 10, 10, tzinfo=UTC))
        session.add(message)
        session.flush()
        session.add(RequirementFact(goal_id=goal.id, category="DEADLINE", field_key="deadline",
            value_json="本周日", source="USER", status="CONFIRMED", evidence_message_id=message.id))
        session.flush()
        snapshot = ClarificationService(session, None)._build_snapshot(goal.id)
        assert snapshot["temporal_context"]["deadline"]["source_message_id"] == message.id
        assert snapshot_deadline(snapshot, timezone=goal.timezone) == date(2026, 10, 11)


def test_total_supply_with_unknown_phase_distribution_is_not_feasible():
    plan = _capacity_plan("OUTCOME")
    plan.temporal_segments_json = []
    plan.commitments_json = [{"label": f"报告{i}", "cadence": "TOTAL", "start_week": 1, "end_week": 4,
        "minimum_occurrences": 1, "minimum_total_minutes": 700, "workload_key": f"R{i}"} for i in [1, 2]]
    assessment = capacity_assessment(plan, "保持原先总供给1080分钟")
    assert assessment["state"] == "INFEASIBLE"
    assert assessment["verified_conflicts"][-1]["deficit_minutes"] == 320
    plan.commitments_json[0]["minimum_total_minutes"] = 200
    plan.commitments_json[1]["minimum_total_minutes"] = 200
    assert capacity_assessment(plan, "总供给1080分钟")["state"] == "UNKNOWN"


def test_fixed_task_cannot_bypass_its_explicit_weekday(session_factory):
    with session_factory() as session:
        materializer = WorkItemMaterializer(session)
        request = ExecutionScheduleRequest(requested_start_date=date(2026, 10, 12), execution_weekdays=[0])
        chosen = materializer._choose_dates(date(2026, 10, 12), date(2026, 10, 18), 1, request,
            {"allowed_weekdays": [6]}, minimum_gap_days=0, maximum_per_day=1, fixed=True)
        assert chosen == [date(2026, 10, 18)]


def test_large_outcome_window_keeps_real_weekly_and_daily_capacity_checks():
    plan = _capacity_plan("OUTCOME")
    plan.temporal_segments_json[1].update(available_days_per_week=5,
        exception_days_per_week=1, exception_daily_minutes=60, exception_weekdays=[5])
    milestones = {1: SimpleNamespace(start_week=1, end_week=4)}
    PlanService._validate_detail_result(plan, milestones, [3], [_day(3, 1, 540)])
    with pytest.raises(HTTPException):
        PlanService._validate_detail_result(plan, milestones, [3], [_day(3, 1, 541)])
    plan.schedule_type = "RHYTHM"
    with pytest.raises(HTTPException):
        PlanService._validate_detail_result(plan, milestones, [3], [_day(3, 1, 540)])


def test_future_window_lookahead_preserves_independent_work_and_deduplicates_aliases():
    plan = _capacity_plan("OUTCOME")
    promises = [{"label": label, "workload_key": key, "minimum_total_minutes": minimum, "end_week": 4}
                for label, key, minimum in [("报告一", "A", 400), ("报告一总计", "A", 500), ("报告二", "B", 500)]]
    lookahead = PlanService._workload_capacity_lookahead(plan, promises, [], 3)
    assert lookahead["shared_capacity_floors"][0]["minimum_combined_minutes_this_week"] == 520
    with pytest.raises(WorkItemValidationError, match="1000分钟"):
        PlanService._validate_remaining_workload_feasibility(plan, promises, [], 3)
    days = [_day(3, 1, 240), _day(3, 2, 240), _day(3, 3, 40)]
    days[0].tasks[0].satisfies_commitments = ["报告一", "报告一总计"]
    for day in days[1:]:
        day.tasks[0].satisfies_commitments = ["报告二"]
    PlanService._validate_remaining_workload_feasibility(plan, promises, days, 3)


def test_conditional_activity_change_keeps_original_confirmed_capacity(session_factory):
    with session_factory() as session:
        goal = Goal(title="待协商活动", raw_input="每天30分钟", timezone="Asia/Shanghai")
        session.add(goal)
        session.flush()
        conversation = Conversation(goal_id=goal.id, phase="CLARIFICATION")
        session.add(conversation)
        session.flush()
        message = Message(conversation_id=conversation.id, sequence=1, role="USER",
                          content="如果挪动晚餐可以讨论多30分钟，但尚未授权。")
        original = RequirementFact(goal_id=goal.id, category="CAPACITY", field_key="available_capacity",
            value_json="每天30分钟", source="USER", status="CONFIRMED")
        session.add_all([message, original])
        session.flush()
        candidate = ProposedFact(category="CAPACITY", field_key="available_capacity", value="每天60分钟",
            source="USER", status="CONFIRMED", confidence=1, operation="REPLACE")
        ClarificationService(session, None)._apply_fact_candidate(goal.id, candidate, message.id)
        assert original.status == "CONFIRMED" and original.value_json == "每天30分钟"
        facts = list(session.scalars(select(RequirementFact).where(RequirementFact.goal_id == goal.id)))
        assert any(f.status == "PROPOSED" and f.value_json == "每天60分钟" for f in facts)


def test_auxiliary_trigger_cannot_silently_hide_an_ordinary_action(session_factory):
    with session_factory() as session:
        _, plan = _sunday_plan(session)
        gate = PlanWorkItem(plan_version_id=plan.id, item_key="GATE", scope_type="PLAN",
            title="待确认触发", description="用户尚未确认的触发条件", task_kind="LEARN", estimate_minutes=10,
            priority=1, binding_mode="TRIGGERED", fulfillment_mode="ONCE", target_value=1,
            constraints_json={"temporal": {"start_week": 1, "end_week": 1}})
        session.add(gate)
        session.flush()
        day = _day(1, 1, 30)
        day.tasks[0].work_item_keys = ["SUNDAY_ONLY", "GATE"]
        result = HybridPlanDetailResult(work_items=[], days=[day])
        with pytest.raises(WorkItemValidationError) as error:
            PlanService(session, None, None)._bind_hybrid_actions(plan, result)
        assert error.value.code == "HYBRID_ACTION_GATE_MIXED"
        assert day.tasks[0].work_item_keys == ["SUNDAY_ONLY", "GATE"]


def test_default_weekday_resolves_exact_relative_date_against_start_anchor(session_factory):
    with session_factory() as session:
        goal, plan = _sunday_plan(session)
        item = session.scalar(select(PlanWorkItem).where(PlanWorkItem.item_key == "SUNDAY_ONLY"))
        item.constraints_json = {"temporal": {"relative_day_ref": "W1-D7", "start_week": 1, "end_week": 1}}
        service = ExecutionService(session)
        service._today_for = lambda _goal: date(2026, 10, 12)
        session.flush()
        assert service.get_setup(goal.id).suggested_weekdays == [6]


def test_compiler_reuses_identical_registered_contract_without_losing_actions():
    original = PlanWorkItemDraft(item_key="REPORT", scope_type="PLAN", title="报告大纲",
        description="原始确认大纲", task_kind="BUILD", estimate_minutes_per_occurrence=30,
        priority=1, source_refs=["confirmed-user"], constraint_set={"binding_mode": "WINDOW",
            "fulfillment_mode": "ONCE", "temporal": {"start_week": 1, "end_week": 4}})
    duplicate = original.model_copy(update={"description": "本次行动描述", "source_refs": ["author-current"]})
    day = _day(3, 1, 30)
    day.tasks[0].work_item_keys = ["REPORT"]
    result = HybridPlanDetailResult(work_items=[duplicate], days=[day])
    PlanService._reuse_registered_detail_items(result, [original])
    assert result.work_items == [] and len(result.days[0].tasks) == 1
    assert original.description == "原始确认大纲"
    result.work_items = [duplicate.model_copy(update={"scope_type": original.scope_type, "stage_order": None})]
    PlanService._reuse_registered_detail_items(result, [original])
    result.work_items = [duplicate.model_copy(update={"estimate_minutes_per_occurrence": 20})]
    with pytest.raises(WorkItemValidationError):
        PlanService._reuse_registered_detail_items(result, [original])
    result.work_items = [duplicate.model_copy(update={"commitment_refs": ["新增报告工作量"]})]
    with pytest.raises(WorkItemValidationError):
        PlanService._reuse_registered_detail_items(result, [original])


def test_continuous_exam_title_keeps_an_atomic_action():
    from planner_api.schemas import PlanDayDraft
    day = _day(1, 1, 90).model_dump()
    day["tasks"][0].update(title="连续90分钟整套模考并留档", is_splittable=True)
    compiled = PlanDayDraft.model_validate(day)
    assert compiled.tasks[0].estimate_minutes == 90
    assert not compiled.tasks[0].is_splittable


def test_binding_shared_material_does_not_invent_second_report_workload(session_factory):
    with session_factory() as session:
        _, plan = _sunday_plan(session)
        plan.commitments_json = [{"label": label, "workload_key": key, "minimum_total_minutes": 700}
                                 for label, key in [("报告一", "A"), ("报告二", "B")]]
        item = session.scalar(select(PlanWorkItem).where(PlanWorkItem.item_key == "SUNDAY_ONLY"))
        item.commitment_refs_json = ["报告一", "报告二"]
        session.flush()
        day = _day(1, 1, 30)
        day.tasks[0].work_item_keys = [item.item_key]
        day.tasks[0].satisfies_commitments = ["报告一"]
        result = HybridPlanDetailResult(work_items=[], days=[day])
        PlanService(session, None, None)._bind_hybrid_actions(plan, result)
        assert day.tasks[0].satisfies_commitments == ["报告一"]
        assert PlanService._task_workload_minutes(day.tasks[0], "报告一", {"报告一": "A", "报告二": "B"}) == 30


def test_named_slot_and_relative_day_receive_a_feasible_default_anchor(session_factory):
    with session_factory() as session:
        goal, plan = _sunday_plan(session)
        item = session.scalar(select(PlanWorkItem).where(PlanWorkItem.item_key == "SUNDAY_ONLY"))
        item.title = "锁定周日09:00–10:30时段"
        item.constraints_json = {"temporal": {"relative_day_ref": "W1-D1", "start_week": 1, "end_week": 1}}
        session.flush()
        service = ExecutionService(session)
        service._today_for = lambda _goal: date(2026, 10, 12)
        setup = service.get_setup(goal.id)
        assert setup.suggested_start_date == date(2026, 10, 18)
        assert setup.suggested_weekdays == [6]
        with pytest.raises(HTTPException):
            service.preview(goal.id, ExecutionScheduleRequest(requested_start_date=date(2026, 10, 12), execution_weekdays=[0]))


@pytest.mark.parametrize('minimum', [700, None])
def test_explicit_workload_ownership_restores_only_its_redundant_label(session_factory, minimum):
    with session_factory() as session:
        _, plan = _sunday_plan(session)
        plan.commitments_json = [{'label': label, 'workload_key': key, 'minimum_total_minutes': minimum}
                                 for label, key in [('报告一', 'A'), ('报告二', 'B')]]
        item = session.scalar(select(PlanWorkItem).where(PlanWorkItem.item_key == 'SUNDAY_ONLY'))
        item.commitment_refs_json = ['报告一', '报告二']
        session.flush()
        day = _day(1, 1, 30)
        task = day.tasks[0]
        task.work_item_keys = [item.item_key]
        task.satisfies_commitments = []
        task.scheduling_policy_data['workload_minutes'] = {'A': 20}
        result = HybridPlanDetailResult(work_items=[], days=[day])
        PlanService(session, None, None)._bind_hybrid_actions(plan, result)
        assert task.satisfies_commitments == ['报告一']
        assert PlanService._task_workload_minutes(task, '报告一', {'报告一': 'A', '报告二': 'B'}) == 20
        assert task.estimate_minutes == 30
        task.scheduling_policy_data['workload_minutes'] = {'UNKNOWN': 20}
        with pytest.raises(WorkItemValidationError):
            PlanService(session, None, None)._bind_hybrid_actions(plan, result)
        task.scheduling_policy_data['workload_minutes'] = {'A': 31}
        with pytest.raises(WorkItemValidationError) as failure:
            PlanService(session, None, None)._bind_hybrid_actions(plan, result)
        assert failure.value.detail['expected']['maximum_allocated_minutes'] == 30
        assert failure.value.detail['actual']['allocation'] == {'A': 31}
        assert failure.value.detail['json_path'] == '/scheduling_policy_data/workload_minutes'


def test_authorized_extension_moves_derived_intermediate_windows_but_keeps_explicit_ones():
    from planner_api.schemas import PlanCommitmentDraft, PlanTemporalSegmentDraft
    plan = _capacity_plan("OUTCOME")
    base = {"label": "报告一", "start_week": 2, "end_week": 3, "cadence": "TOTAL", "minimum_occurrences": 1,
            "minimum_total_minutes": 700, "workload_key": "REPORT1", "match_terms": ["报告一"], "source_text": "报告一需要700分钟"}
    plan.commitments_json = [base]
    segments = [PlanTemporalSegmentDraft(**item, date_scope="确认范围", capacity_summary="确认容量") for item in plan.temporal_segments_json]
    segments[-1].end_week = 5
    proposal = SimpleNamespace(capacity_segments=segments, total_weeks_change=1)
    outline = SimpleNamespace(total_weeks=5, temporal_segments=[], standing_work_items=[], commitments=[PlanCommitmentDraft(**base)])
    PlanService._restore_negotiated_contract(plan, outline, proposal)
    assert outline.commitments[0].end_week == 4
    assert outline.commitments[0].start_week == 1
    assert outline.commitments[0].minimum_total_minutes == 700
    plan.commitments_json[0] = {**base, "source_text": "报告一第3周必须交稿，需要700分钟"}
    PlanService._restore_negotiated_contract(plan, outline, proposal)
    assert outline.commitments[0].end_week == 3


def test_atomic_internal_readiness_dependency_does_not_deadlock_completion(session_factory):
    with session_factory() as session:
        _, plan = _sunday_plan(session)
        service = ExecutionService(session)
        primary = session.scalar(select(PlanWorkItem).where(PlanWorkItem.item_key == "SUNDAY_ONLY"))
        ready = PlanWorkItem(plan_version_id=plan.id, item_key="READY", scope_type="PLAN", title="时段检查",
            description="行动内部检查时段", task_kind="LEARN", estimate_minutes=10, priority=1,
            binding_mode="WINDOW", fulfillment_mode="ONCE", target_value=1, constraints_json={})
        session.add(ready)
        session.flush()
        edge = WorkItemDependency(plan_version_id=plan.id, predecessor_work_item_id=ready.id,
            successor_work_item_id=primary.id, dependency_type="FINISH_TO_START", minimum_lag_days=0)
        session.add(edge)
        task = service._tasks(plan.id)[0]
        task.is_splittable = False
        for other in service._tasks(plan.id)[1:]:
            session.delete(other)
        task.scheduling_policy_json = {"work_item_keys": [primary.item_key, ready.item_key],
            "stage_contributions": [{"stage_order": 1, "action": "先检查时段"}, {"stage_order": 2, "action": "再实际执行"}]}
        session.flush()
        service._assert_hybrid_predecessors_completed(plan, task)
        edge.minimum_lag_days = 1
        session.flush()
        with pytest.raises(HTTPException):
            service._assert_hybrid_predecessors_completed(plan, task)
