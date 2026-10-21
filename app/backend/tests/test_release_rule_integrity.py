"""Rule fidelity regressions derived from the three synthetic release journeys."""
from datetime import date
from types import SimpleNamespace as NS

import pytest

from planner_api.temporal_contract import resolve_deadline, snapshot_deadline
from planner_api.plan_rule_integrity import validate_recurrence_contracts, validate_action_coverage
from planner_api.work_item_validation import WorkItemValidationError


def promise(**changes):
    return NS(label="每周工程取舍", cadence="PER_WEEK", minimum_occurrences=1,
              **{"start_week": 1, "end_week": 4, "source_text": "每周至少一次用5分钟口述本周工程取舍", **changes})


def test_preparation_deadline_is_distinct_from_exam_day():
    text = "演练假设考试日为2026年11月15日，11月14日晚完成准备；这不是已核实的官方考试时间"
    assert resolve_deadline(text, date(2026, 10, 20)) == date(2026, 11, 14)


@pytest.mark.parametrize("text", ["2026年11月14日", "2026年11月14日晚完成准备", "请在2026-11-14之前完成"])
def test_explicit_absolute_deadlines(text):
    assert resolve_deadline(text) == date(2026, 11, 14)


@pytest.mark.parametrize("text", ["如果通过初试，2026年11月14日前完成", "2026年11月14日或11月15日前完成", "11月14日前完成"])
def test_ambiguous_or_conditional_dates_are_not_invented(text):
    assert resolve_deadline(text) is None


def test_unresolved_deadline_cannot_silently_become_unbounded():
    with pytest.raises(ValueError, match="截止日期"):
        snapshot_deadline({"deadline": "考试前完成，日期待核实"}, timezone="Asia/Shanghai", require_resolved=True)
    assert snapshot_deadline({"deadline": "没有硬性截止日期，四周后复盘"}, timezone="Asia/Shanghai", require_resolved=True) is None


@pytest.mark.parametrize("text", ["先用4周完成这一轮作品打磨，作为估计周期和复盘点，不是硬性求职或offer截止",
    "NONE，两周的观察复盘点没有硬截止"])
def test_review_cycle_is_not_a_hard_deadline(text):
    assert snapshot_deadline({"deadline": text}, timezone="Asia/Shanghai", require_resolved=True) is None


@pytest.mark.parametrize("count", [0, 4])
def test_weekly_count_is_not_whole_plan_count(count):
    item = promise()
    item.minimum_occurrences = count
    with pytest.raises(WorkItemValidationError, match="频次"):
        validate_recurrence_contracts(NS(total_weeks=4, commitments=[item]), {})


def test_weekly_source_cannot_be_compiled_as_total():
    item = promise()
    item.cadence = "TOTAL"
    with pytest.raises(WorkItemValidationError):
        validate_recurrence_contracts(NS(total_weeks=4, commitments=[item]), {})


def test_weekly_practice_cannot_drop_final_week():
    item = promise(end_week=3, source_text="行测专项每周至少3次，每次20分钟")
    item.minimum_occurrences = 3
    with pytest.raises(WorkItemValidationError, match="窗口"):
        validate_recurrence_contracts(NS(total_weeks=4, commitments=[item]), {})


def test_count_is_checked_against_confirmed_source_not_only_model_source():
    item = promise(source_text="每周至少四次用5分钟口述本周工程取舍")
    item.minimum_occurrences = 4
    with pytest.raises(WorkItemValidationError):
        validate_recurrence_contracts(NS(total_weeks=4, commitments=[item]),
            {"constraints": ["每周至少一次用5分钟口述本周工程取舍，计入4小时"]})


def test_valid_weekly_and_explicitly_scoped_contracts():
    validate_recurrence_contracts(NS(total_weeks=4, commitments=[promise()]), {})
    validate_recurrence_contracts(NS(total_weeks=4, commitments=[promise(end_week=3,
        source_text="W1-W3每周一次口述工程取舍")]), {})


def test_removing_count_from_model_source_does_not_bypass_original_count():
    item = promise(source_text="每周口述工程取舍")
    item.minimum_occurrences = 4
    with pytest.raises(WorkItemValidationError):
        validate_recurrence_contracts(NS(total_weeks=4, commitments=[item]),
            {"constraints": ["每周至少一次用5分钟口述工程取舍"]})


def test_deleted_weekly_activity_is_not_hidden_by_an_empty_commitment_list():
    with pytest.raises(WorkItemValidationError):
        validate_recurrence_contracts(NS(total_weeks=4, commitments=[]),
            {"constraints": ["每周至少一次用5分钟口述工程取舍"]})


def test_capacity_frequency_does_not_become_an_activity_requirement():
    validate_recurrence_contracts(NS(total_weeks=4, commitments=[]),
        {"available_capacity": "每周4次，每次60分钟"})


def work(key, end=3, **changes):
    return NS(item_key=key, is_required=True, binding_mode="WINDOW", stage_order=3,
              constraints_json={"temporal": {"start_week": 3, "end_week": end}}, **changes)


def day(week, *keys):
    return NS(week_index=week, tasks=[NS(work_item_keys=list(keys), status="NOT_STARTED")])


def test_missing_stage_work_is_rejected_at_its_last_eligible_week():
    with pytest.raises(WorkItemValidationError, match="STATE_RECOVERY"):
        validate_action_coverage([work("STATE_RECOVERY")], [day(3, "UNRELATED")], through_week=3)


def test_future_work_does_not_need_premature_actions():
    validate_action_coverage([work("STATE_RECOVERY")], [day(2, "UNRELATED")], through_week=2)


def test_secondary_identity_represents_work_but_cancelled_task_does_not():
    validate_action_coverage([work("STATE_RECOVERY")], [day(3, "OTHER", "STATE_RECOVERY")], through_week=3)
    cancelled = day(3, "STATE_RECOVERY")
    cancelled.tasks[0].status = "CANCELLED"
    with pytest.raises(WorkItemValidationError):
        validate_action_coverage([work("STATE_RECOVERY")], [cancelled], through_week=3)


def test_out_of_window_action_does_not_satisfy_coverage():
    with pytest.raises(WorkItemValidationError):
        validate_action_coverage([work("STATE_RECOVERY")], [day(4, "STATE_RECOVERY")], through_week=4)


def test_plan_wide_outcome_is_not_prematurely_due_in_first_week():
    item = work("FINAL_OUTCOME")
    item.stage_order = None
    item.constraints_json = {"temporal": {}}
    validate_action_coverage([item], [], through_week=1, milestone_weeks={None: (1, 4)})
    with pytest.raises(WorkItemValidationError):
        validate_action_coverage([item], [], through_week=4, milestone_weeks={None: (1, 4)})


def test_zero_conditional_target_is_not_reported_as_completed():
    from planner_api.execution_service import ExecutionService
    plan = NS(commitments_json=[{"label": "条件合适再养护", "cadence": "TOTAL", "start_week": 1,
                                 "end_week": 2, "minimum_occurrences": 0}])
    progress = ExecutionService._commitment_progress(date(2026, 10, 20), plan,
        [NS(plan_day_id="day", scheduled_date=date(2026, 10, 20))],
        [NS(id="day", week_index=1)], [])
    assert progress[0].risk != "COMPLETE"


def test_completion_totals_include_focus_outside_recent_window(session_factory):
    from datetime import timedelta
    from planner_api.execution_service import ExecutionService
    from planner_api.models import FocusSession
    from test_work_item_foundation import _plan_graph
    with session_factory() as session:
        goal, plan, _, saved_day, _ = _plan_graph(session)
        goal.active_plan_version_id = plan.id
        goal.status = "ACTIVE"
        plan.status = "CONFIRMED"
        session.add(FocusSession(goal_id=goal.id, plan_version_id=plan.id, plan_day_id=saved_day.id,
            planned_minutes=5, actual_seconds=300, status="COMPLETED"))
        session.flush()
        service = ExecutionService(session)
        service._today_for = lambda _: date(2026, 10, 20) + timedelta(days=60)
        progress = service.get_progress(goal.id)
        assert progress.total_focus_minutes == 5


def test_public_preview_rejects_exam_eve_overrun_without_activation(session_factory):
    from fastapi import HTTPException
    from sqlalchemy import select
    from planner_api.execution_service import ExecutionService
    from planner_api.models import RequirementVersion, PlanActivation
    from planner_api.schemas import ExecutionScheduleRequest
    from test_p0_root_cause_fixes import _sunday_plan
    with session_factory() as session:
        goal, plan = _sunday_plan(session)
        requirement = RequirementVersion(goal_id=goal.id, version=2, status="CONFIRMED", snapshot_json={
            "deadline": "演练假设考试日为2026年11月15日，11月14日晚完成准备；不是已核实的官方考试时间"})
        session.add(requirement); session.flush()
        goal.current_requirement_version_id = requirement.id
        service = ExecutionService(session)
        service._today_for = lambda _: date(2026, 10, 20)
        with pytest.raises(HTTPException) as caught:
            service.preview(goal.id, ExecutionScheduleRequest(requested_start_date=date(2026, 11, 9),
                execution_weekdays=[6]))
        assert caught.value.detail["code"] == "PLAN_START_DEADLINE_CONFLICT"
        assert caught.value.detail["deadline"] == "2026-11-14"
        assert session.scalar(select(PlanActivation).where(PlanActivation.plan_version_id == plan.id)) is None


def test_generation_repairs_missing_work_binding_before_saving_tasks(session_factory):
    from sqlalchemy import select
    from planner_api.plan_ai import FakePlanProvider
    from planner_api.plan_discussion_ai import FakePlanDiscussionProvider
    from planner_api.plan_service import PlanService
    from planner_api.models import PlanTask
    from planner_api.work_item_domain import PlanWorkItemDraft, WorkItemConstraintSet, TemporalConstraint
    from test_work_item_foundation import _draftable_goal

    class MissingBindingProvider(FakePlanProvider):
        calls = 0

        def draft_hybrid_outline(self, *args, **kwargs):
            result = super().draft_hybrid_outline(*args, **kwargs)
            result.standing_work_items.append(PlanWorkItemDraft(item_key="STATE_RECOVERY",
                scope_type="PLAN", title="状态恢复验收", description="真实动作必须承接原身份",
                task_kind="BUILD", estimate_minutes_per_occurrence=30, priority=1,
                constraint_set=WorkItemConstraintSet(binding_mode="WINDOW", fulfillment_mode="ONCE",
                    temporal=TemporalConstraint(start_week=1, end_week=1)),
                source_refs=["confirmed-route"]))
            return result

        def draft_hybrid_details(self, context, *args, **kwargs):
            self.calls += 1
            result = super().draft_hybrid_details(context, *args, **kwargs)
            if self.calls > 1:
                assert context.planning_context["generation_validation_feedback"]["code"] == "HYBRID_REQUIRED_ACTION_MISSING"
                assert not list(session.scalars(select(PlanTask)))
                result.days[-1].tasks[-1].work_item_keys.append("STATE_RECOVERY")
            return result

    with session_factory() as session:
        goal = _draftable_goal(session)
        provider = MissingBindingProvider()
        service = PlanService(session, provider, FakePlanDiscussionProvider(), enable_hybrid_work_items=True)
        service.draft_plan(goal.id)
        workspace = service.draft_next_details(goal.id)
        assert provider.calls == 2
        assert workspace.generation_phase == "COMPLETE"
        assert any("STATE_RECOVERY" in (task.scheduling_policy_json or {}).get("work_item_keys", [])
                   for task in session.scalars(select(PlanTask)))


def test_qualitative_evidence_does_not_add_an_independent_minute_workload():
    from planner_api.plan_service import PlanService
    from test_plan_capacity_contract import _day
    promises = [dict(label="订正", start_week=1, end_week=1, cadence="TOTAL", minimum_occurrences=1,
                     minimum_total_minutes=45, workload_key="CORRECTION"),
                dict(label="错因清单", start_week=1, end_week=1, cadence="TOTAL", minimum_occurrences=1,
                     workload_key="ERROR_LIST")]
    value = _day(1, 1, 45)
    task = value.tasks[0]
    task.satisfies_commitments = ["订正", "错因清单"]
    task.scheduling_policy_data = {"workload_minutes": {"CORRECTION": 45}}
    PlanService._validate_minimum_commitment_minutes(promises, [value])
    # Once the second activity has an independently confirmed time floor,
    # counting the same 45 minutes twice remains forbidden.
    promises[1]["minimum_total_minutes"] = 45
    with pytest.raises(WorkItemValidationError):
        PlanService._validate_minimum_commitment_minutes(promises, [value])


def calendar_plan():
    return NS(total_weeks=4, schedule_type="CALENDAR", weekly_execution_days=7,
              temporal_segments_json=[dict(start_week=1, end_week=1, date_scope="W1（周二至周日）",
                   weekly_execution_days=6, available_days_per_week=6, suggested_daily_minutes=90),
                   dict(start_week=2, end_week=4, available_days_per_week=7, suggested_daily_minutes=60)])


def test_partial_calendar_week_has_one_shared_coordinate_system():
    from planner_api.plan_calendar import plan_week_range, plan_week_index
    from planner_api.plan_capacity import calendar_capacity
    from planner_api.temporal_contract import relative_day_range
    from planner_api.work_item_materialization import WorkItemMaterializer
    plan = calendar_plan()
    anchor = date(2026, 10, 20)
    assert plan_week_range(plan, anchor, 1) == (anchor, date(2026, 10, 25))
    assert plan_week_range(plan, anchor, 4) == (date(2026, 11, 9), date(2026, 11, 15))
    assert plan_week_index(plan, anchor, date(2026, 10, 26)) == 2
    assert plan_week_index(plan, anchor, date(2026, 10, 19)) == 0
    assert calendar_capacity(plan, anchor, date(2026, 10, 26))["daily_minutes"] == 60
    assert relative_day_range("W4-D6", anchor, plan=plan) == (date(2026, 11, 14),) * 2
    assert relative_day_range("W1-D7", anchor, plan=plan) is None
    plan.temporal_segments_json[0]["weekly_execution_days"] = 3
    assert plan_week_range(plan, anchor, 2)[0] == date(2026, 10, 26)
    assert WorkItemMaterializer._date_range(plan, NS(requested_start_date=anchor),
        {"start_week": 4, "end_week": 4, "deadline": "2026-11-14"}) == (
            date(2026, 11, 9), date(2026, 11, 14))
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as caught:
        WorkItemMaterializer._date_range(plan, NS(requested_start_date=anchor),
                                        {"relative_day_ref": "W1_MONDAY"})
    assert caught.value.detail["code"] == "PLAN_ITEM_DATE_CONFLICT"
    # A calendar label without explicit residual-week evidence preserves the
    # legacy rolling contract. Rhythm plans also retain that contract.
    plan.compiler_report_json = {"week_basis": "ROLLING_START"}
    assert plan_week_range(plan, anchor, 4)[0] == date(2026, 11, 10)
    from planner_api.plan_calendar import calendar_weeks
    assert calendar_weeks(plan, from_source=True)
    del plan.compiler_report_json
    plan.schedule_type = "RHYTHM"
    assert plan_week_range(plan, anchor, 4)[0] == date(2026, 11, 10)


def test_weekly_windows_split_at_sunday_including_first_partial_week():
    from planner_api.work_item_materialization import WorkItemMaterializer
    plan = calendar_plan()
    ranges = WorkItemMaterializer(None)._period_ranges(plan,
        NS(requested_start_date=date(2026, 10, 20)),
        NS(binding_mode="CADENCE", target_value=3), {"start_week": 1, "end_week": 2},
        {"period": "WEEK", "target_count": 3})
    assert ranges == [("week:1", date(2026, 10, 20), date(2026, 10, 25), 3),
                      ("week:2", date(2026, 10, 26), date(2026, 11, 1), 3)]


def test_public_preview_maps_last_calendar_week_before_exam_deadline(session_factory):
    from sqlalchemy import select
    from planner_api.execution_service import ExecutionService
    from planner_api.models import PlanDay, PlanMilestone, PlanWorkItem, RequirementVersion
    from planner_api.schemas import ExecutionScheduleRequest
    from test_p0_root_cause_fixes import _sunday_plan
    with session_factory() as session:
        goal, plan = _sunday_plan(session)
        plan.total_weeks = 4
        plan.schedule_type = "CALENDAR"
        plan.temporal_segments_json = calendar_plan().temporal_segments_json
        day = session.scalar(select(PlanDay).where(PlanDay.plan_version_id == plan.id))
        day.week_index = 4
        milestone = session.get(PlanMilestone, day.milestone_id)
        milestone.end_week = 4
        item = session.scalar(select(PlanWorkItem).where(PlanWorkItem.plan_version_id == plan.id))
        item.constraints_json = {"temporal": {"relative_day_ref": "W4-D6", "allowed_weekdays": [5],
                                              "deadline": "2026-11-14"},
                                 "completion": {"target_value": 1}}
        requirement = RequirementVersion(goal_id=goal.id, version=3, status="CONFIRMED",
            snapshot_json={"deadline": "2026-11-14"})
        session.add(requirement); session.flush()
        goal.current_requirement_version_id = requirement.id
        service = ExecutionService(session)
        service._today_for = lambda _: date(2026, 10, 20)
        preview = service.preview(goal.id, ExecutionScheduleRequest(requested_start_date=date(2026, 10, 20),
                                                                     execution_weekdays=[5]))
        assert preview.projected_end_date == date(2026, 11, 14)
        assert preview.upcoming_days[0].week_index == 4


def test_flexible_task_week_guard_uses_the_same_calendar_boundary():
    from unittest.mock import Mock
    from planner_api.execution_service import ExecutionService
    plan = calendar_plan()
    service = ExecutionService(Mock())
    service.session.get.return_value = plan
    activation = NS(plan_version_id="plan", requested_start_date=date(2026, 10, 20))
    task = NS(work_item_window_id=None, scheduling_policy_json={"start_week": 2, "end_week": 2})
    assert service._flexible_date_blocked_reason(activation, task, date(2026, 10, 26)) is None
    assert service._flexible_date_blocked_reason(activation, task, date(2026, 11, 2)) is not None
