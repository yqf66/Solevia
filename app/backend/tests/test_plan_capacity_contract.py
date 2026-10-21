from types import SimpleNamespace

import pytest

from planner_api.plan_service import PlanService
from planner_api.work_item_validation import WorkItemValidationError


@pytest.mark.parametrize("source", [
    "每周3天可投入，每天总容量90分钟；剩余70分钟不强制安排。",
    "每日总可用时间90分钟", "每周总容量270分钟", "第3周总供给180分钟",
])
def test_scoped_total_is_not_whole_plan_supply(source):
    from planner_api.plan_capacity import capacity_assessment

    plan = SimpleNamespace(total_weeks=2, weekly_execution_days=3,
        temporal_segments_json=[{"start_week": 1, "end_week": 2,
            "available_days_per_week": 3, "suggested_daily_minutes": 90}],
        commitments_json=[{"label": "模拟卷", "cadence": "TOTAL", "start_week": 1,
            "end_week": 2, "workload_key": "mock", "minimum_total_minutes": 230,
            "minimum_occurrences": 1}],
    )
    result = capacity_assessment(plan, source)
    assert result["confirmed_total_minutes"] is None
    assert result["state"] == "LOWER_BOUND_VALID" and not result["verified_conflicts"]


def test_explicit_whole_plan_total_still_limits_supply():
    from planner_api.plan_capacity import capacity_assessment

    plan = SimpleNamespace(total_weeks=2, weekly_execution_days=3,
        temporal_segments_json=[{"start_week": 1, "end_week": 2,
            "available_days_per_week": 3, "suggested_daily_minutes": 90}],
        commitments_json=[{"label": "模拟卷", "cadence": "TOTAL", "start_week": 1,
            "end_week": 2, "workload_key": "mock", "minimum_total_minutes": 230,
            "minimum_occurrences": 1}],
    )
    result = capacity_assessment(plan, "每天总容量90分钟；整个规划总供给180分钟")
    assert result["state"] == "INFEASIBLE" and result["confirmed_total_minutes"] == 180
    assert result["verified_conflicts"][0]["deficit_minutes"] == 50


def test_outcome_adjustment_keeps_supply_separate_from_checkpoint_count():
    from planner_api.schemas import PlanTemporalSegmentDraft
    from planner_api.plan_capacity import week_capacity
    segment = PlanTemporalSegmentDraft(label="活动迁移后的本周", start_week=1, end_week=1,
        date_scope="只限本周", capacity_summary="周三和周六各60分钟", weekly_execution_days=2,
        suggested_daily_minutes=60)
    proposal = SimpleNamespace(target_schedule_type=None, capacity_segments=[segment])
    PlanService._normalize_outcome_adjustment(proposal, "OUTCOME")
    assert segment.weekly_execution_days == 1
    assert segment.available_days_per_week == 2
    plan = SimpleNamespace(total_weeks=1, weekly_execution_days=1, temporal_segments_json=[segment.model_dump(mode="json")])
    assert week_capacity(plan,1)["weekly_minutes"] == 120
    segment.weekly_execution_days = 2
    PlanService._normalize_outcome_adjustment(proposal, "RHYTHM")
    assert segment.weekly_execution_days == 2


@pytest.mark.parametrize("label", ["执行时段限定 19:00–20:00，不占用课程时间", "可用时间：每天 60 分钟", "不得占用课程时间", "每周三个执行日，每次60分钟", "两周内保持每周三次、每次 60 分钟的执行节奏，累计 6 个执行单元"])
def test_capacity_conditions_cannot_be_countable_commitments(label):
    with pytest.raises(WorkItemValidationError, match="temporal_segments"):
        PlanService._validate_executable_commitments(SimpleNamespace(commitments=[SimpleNamespace(label=label)]))


@pytest.mark.parametrize("label", ["每周三次阅读训练", "19:00–20:00 健身", "完成两套试卷", "学习时间管理并完成总结", "每周三个执行日阅读论文"])
def test_real_actions_keep_their_countable_commitments(label):
    PlanService._validate_executable_commitments(SimpleNamespace(commitments=[SimpleNamespace(label=label)]))


def test_week_limit_uses_local_capacity_without_a_global_clamp():
    plan = SimpleNamespace(weekly_execution_days=4, temporal_segments_json=[{
        "start_week": 1, "end_week": 3, "weekly_execution_days": 3,
        "weekly_capacity_overrides": [{"week_index": 2, "weekly_execution_days": 2}],
    }])
    assert [PlanService._effective_week_day_limit(plan, week) for week in (1, 2, 3)] == [3, 2, 3]
    plan.weekly_execution_days = 1
    assert PlanService._effective_week_day_limit(plan, 2) == 2
    plan.schedule_type = "OUTCOME"
    assert PlanService._effective_week_day_limit(plan, 2) == 1


def test_outline_rejects_more_single_week_stages_than_available_execution_days():
    result = SimpleNamespace(total_weeks=2, weekly_execution_days=3, temporal_segments=[],
                             milestones=[SimpleNamespace(stage_order=index, start_week=2, end_week=2)
                                         for index in range(3, 7)])
    with pytest.raises(WorkItemValidationError, match="capacity=3"):
        PlanService._validate_stage_day_capacity(result)
    result.milestones[0].start_week = 1
    PlanService._validate_stage_day_capacity(result)


def test_one_off_experiment_does_not_inherit_global_daily_cadence():
    commitment = SimpleNamespace(
        label="取消与异常传播实验", cadence="PER_EXECUTION_DAY",
        source_text="手动 cancel 一个等待任务，观察取消，再改回代码。",
    )
    result = SimpleNamespace(commitments=[commitment])
    with pytest.raises(WorkItemValidationError, match="TOTAL"):
        PlanService._validate_executable_commitments(result)
    commitment.cadence = "TOTAL"
    PlanService._validate_executable_commitments(result)
    commitment.cadence = "PER_EXECUTION_DAY"
    commitment.source_text = "每个执行日完成取消实验并记录观察。"
    PlanService._validate_executable_commitments(result)


def test_saved_capacity_failure_explains_actual_load_without_blaming_requirements():
    message = PlanService._generation_failure_message(
        "PLAN_TEMPORAL_SEGMENT_MINUTES_INVALID",
        "week=2, day_minutes=[30, 20, 85], normal_limit=20, exception_limit=45, "
        "weekly_load=135, weekly_capacity=85, weekly_tolerance=1.1",
        "internal fallback",
    )
    assert "第 2 周的草稿" in message
    assert "135 分钟" in message
    assert "85 分钟" in message
    assert "已完成周" in message
    assert "继续自动修正" in message
    assert "需求不合理" not in message


def _capacity_plan(schedule_type="RHYTHM"):
    return SimpleNamespace(total_weeks=4, weekly_execution_days=2, weekly_minutes=270,
        schedule_type=schedule_type, commitments_json=[], temporal_segments_json=[
            {"label": "前期", "start_week": 1, "end_week": 2,
             "weekly_execution_days": 1 if schedule_type == "OUTCOME" else 2,
             "available_days_per_week": 2, "suggested_daily_minutes": 30},
            {"label": "后期", "start_week": 3, "end_week": 4,
             "weekly_execution_days": 1 if schedule_type == "OUTCOME" else 4,
             "available_days_per_week": 4, "suggested_daily_minutes": 120}])


def _day(week, index, minutes):
    from planner_api.schemas import PlanDayDraft, PlanTaskDraft
    return PlanDayDraft(stage_order=1, week_index=week, day_index=index,
        primary_goal="完成当前阶段的研究报告", suggested_minutes=minutes,
        tasks=[PlanTaskDraft(order=1, title="阅读资料并完成报告", description="阅读合成资料、整理结果并完成报告。",
            estimate_minutes=min(240, minutes), priority=1, task_kind="BUILD", is_required=True,
            evidence=["报告稿件"], depends_on=[])], evidence=["报告稿件"], do_not=[])


@pytest.mark.parametrize("kind", ["RHYTHM", "OUTCOME"])
def test_front_light_back_heavy_capacity_and_checkpoint_semantics(kind):
    from planner_api.plan_capacity import week_capacity
    plan = _capacity_plan(kind)
    assert [week_capacity(plan, week)["weekly_minutes"] for week in (1, 2, 3, 4)] == [60, 60, 480, 480]
    milestone = {1: SimpleNamespace(start_week=1, end_week=4)}
    days = [_day(3, 1, 300)] if kind == "OUTCOME" else [_day(3, index, 100) for index in range(1, 5)]
    PlanService._validate_detail_result(plan, milestone, [3], days)
    with pytest.raises(Exception) as error:
        PlanService._validate_detail_result(plan, milestone, [1], [_day(1, 1, 100)])
    assert error.value.detail["code"] == "PLAN_TEMPORAL_SEGMENT_MINUTES_INVALID"


def test_total_goal_can_use_later_capacity_but_weekly_promise_cannot():
    from planner_api.plan_capacity import capacity_conflicts
    plan = _capacity_plan("OUTCOME")
    promise = {"label": "完成报告", "start_week": 1, "end_week": 4,
               "cadence": "TOTAL", "minimum_occurrences": 1, "minimum_minutes_per_occurrence": 600}
    plan.commitments_json = [promise]
    assert capacity_conflicts(plan) == []  # 1080 available; not 4 x averaged quota.
    promise["minimum_minutes_per_occurrence"] = 1400
    assert capacity_conflicts(plan)[0]["deficit_minutes"] == 320
    promise.update(cadence="PER_WEEK", minimum_minutes_per_occurrence=100)
    assert [item["weeks"] for item in capacity_conflicts(plan)] == [[1], [2]]


def test_overloaded_tasks_never_create_holiday_capacity():
    plan = _capacity_plan()
    plan.temporal_segments_json[0]["holiday_rules"] = ["国庆假期可利用整块时间"]
    assert PlanService._week_capacity_override(plan.temporal_segments_json[0], 1, [_day(1, 1, 240)], "国庆假期") is None
    plan.temporal_segments_json[0]["weekly_capacity_overrides"] = [{"week_index": 2, "weekly_execution_days": 2,
        "suggested_daily_minutes": 30, "exception_days_per_week": 1, "exception_daily_minutes": 90}]
    from planner_api.plan_capacity import week_capacity
    assert [week_capacity(plan, week)["weekly_minutes"] for week in (1, 2, 3)] == [60, 120, 480]


def test_one_saturday_exception_is_not_borrowed_by_weekdays_or_other_weeks(session_factory):
    from datetime import date, timedelta
    from planner_api.execution_service import ExecutionService
    from planner_api.plan_capacity import calendar_capacity
    from planner_api.schemas import PlanWeekCapacityOverrideDraft
    plan = _capacity_plan("OUTCOME")
    plan.id = "saturday-boundary"
    override = PlanWeekCapacityOverrideDraft(week_index=2, reason="仅第2周周六额外60分钟",
        weekly_execution_days=1, available_days_per_week=3, suggested_daily_minutes=30,
        exception_days_per_week=1, exception_daily_minutes=60, exception_weekdays=[5])
    plan.temporal_segments_json[0]["weekly_capacity_overrides"] = [override.model_dump()]
    anchor = date(2026, 11, 9)
    activation = SimpleNamespace(id="saturday-activation", requested_start_date=anchor)
    with session_factory() as session:
        service = ExecutionService(session)
        assert service._candidate_target_capacity(plan, activation, anchor+timedelta(days=12))[1] == 60
        assert service._candidate_target_capacity(plan, activation, anchor+timedelta(days=8))[1] == 30
        assert service._candidate_target_capacity(plan, activation, anchor+timedelta(days=5))[1] == 30
        assert service._candidate_target_capacity(plan, activation, anchor-timedelta(days=1))[1] == 0
    assert calendar_capacity(plan, anchor, anchor+timedelta(days=12))["weekly_minutes"] == 120


def test_short_weekend_exception_preserves_normal_days_and_exact_weekly_supply():
    from datetime import date, timedelta
    from planner_api.plan_capacity import calendar_capacity
    from planner_api.schemas import PlanTemporalSegmentDraft
    plan = _capacity_plan()
    late = PlanTemporalSegmentDraft(label="后两周", start_week=3, end_week=4,
        date_scope="第3及4周", capacity_summary="四个120分钟普通日和周六60分钟",
        weekly_execution_days=5, available_days_per_week=5, suggested_daily_minutes=120,
        exception_days_per_week=1, exception_daily_minutes=60, exception_weekdays=[5])
    plan.temporal_segments_json[1] = late.model_dump()
    anchor = date(2026, 11, 9)
    assert calendar_capacity(plan, anchor, anchor+timedelta(days=15))["daily_minutes"] == 120
    assert calendar_capacity(plan, anchor, anchor+timedelta(days=19))["daily_minutes"] == 60
    assert calendar_capacity(plan, anchor, anchor+timedelta(days=19))["weekly_minutes"] == 540
    with pytest.raises(ValueError):
        late.model_copy().model_validate({**late.model_dump(), "exception_weekdays": [5, 6]})


def test_negotiated_preview_cannot_expand_capacity_or_drop_a_promise():
    from planner_api.schemas import PlanTemporalSegmentDraft
    current = _capacity_plan()
    current.commitments_json = [{"label": "完成阅读", "cadence": "TOTAL", "minimum_occurrences": 2}]
    def segment(item):
        return PlanTemporalSegmentDraft(**item, date_scope="已确认阶段", capacity_summary="按用户确认的容量")
    outline = SimpleNamespace(total_weeks=4, commitments=[], temporal_segments=[segment(item) for item in current.temporal_segments_json])
    proposal = SimpleNamespace(total_weeks_change=0, capacity_segments=None)
    with pytest.raises(WorkItemValidationError, match="完成阅读"):
        PlanService._validate_negotiated_outline(current, outline, proposal)
    current.commitments_json = []
    # Include schema defaults in the source numeric contract.
    current.temporal_segments_json = [item.model_dump() for item in outline.temporal_segments]
    PlanService._validate_negotiated_outline(current, outline, proposal)
    outline.temporal_segments[0].suggested_daily_minutes = 60
    with pytest.raises(WorkItemValidationError, match="容量"):
        PlanService._validate_negotiated_outline(current, outline, proposal)


def test_negotiation_compiler_uses_confirmed_supply_and_original_work_identity():
    from planner_api.schemas import PlanCommitmentDraft, PlanTemporalSegmentDraft
    current = _capacity_plan("OUTCOME")
    current.commitments_json = [{"label": "原确认报告", "start_week": 1, "end_week": 4,
        "cadence": "TOTAL", "minimum_occurrences": 1, "minimum_total_minutes": 700,
        "workload_key": "REPORT", "match_terms": ["报告"], "source_text": "报告需要700分钟"}]
    segments = [PlanTemporalSegmentDraft(**item, date_scope="确认范围", capacity_summary="用户确认供应")
                for item in current.temporal_segments_json]
    segments[-1].end_week = 5
    proposal = SimpleNamespace(capacity_segments=segments, total_weeks_change=1)
    generated = PlanCommitmentDraft.model_validate({**current.commitments_json[0],
        "label": "模型重命名报告", "minimum_total_minutes": 500, "end_week": 5})
    outline = SimpleNamespace(total_weeks=5, temporal_segments=[], commitments=[generated],
        standing_work_items=[SimpleNamespace(commitment_refs=["模型重命名报告"],
            constraint_set=SimpleNamespace(temporal=SimpleNamespace(end_week=4)))])
    PlanService._restore_negotiated_contract(current, outline, proposal)
    assert outline.commitments[0].label == "原确认报告"
    assert outline.commitments[0].minimum_total_minutes == 700
    assert outline.commitments[0].end_week == 5
    assert outline.standing_work_items[0].commitment_refs == ["原确认报告"]
    assert outline.temporal_segments[-1].suggested_daily_minutes == 120
    PlanService._validate_negotiated_outline(current, outline, proposal)
    # Keeping the contract must still reject insufficient generated actions.
    day = _day(5,1,100)
    day.tasks[0].satisfies_commitments = ["原确认报告"]
    with pytest.raises(WorkItemValidationError, match="不能削减"):
        PlanService._validate_minimum_commitment_minutes(
            [item.model_dump() for item in outline.commitments], [day])


def test_minimum_work_cannot_be_shortened_to_get_a_preview_through():
    promise = {"label": "阅读训练", "start_week": 1, "end_week": 1, "cadence": "TOTAL",
               "minimum_occurrences": 1, "minimum_minutes_per_occurrence": 60}
    day = _day(1, 1, 20)
    day.tasks[0].satisfies_commitments = [promise["label"]]
    with pytest.raises(WorkItemValidationError, match="不能削减"):
        PlanService._validate_minimum_commitment_minutes([promise], [day])
    day.tasks[0].estimate_minutes = 60
    PlanService._validate_minimum_commitment_minutes([promise], [day])



def test_capacity_negotiation_survives_reload_and_applies_only_validated_preview(client, session_factory):
    from fastapi import HTTPException
    from sqlalchemy import select, func
    from test_plan import _confirmed_route
    from planner_api.models import Goal, PlanVersion, PlanTask
    from planner_api.plan_ai import FakePlanProvider
    from planner_api.plan_discussion_ai import FakePlanDiscussionProvider
    from planner_api.schemas import PlanTemporalSegmentDraft

    class OverloadedProvider(FakePlanProvider):
        outline_calls = 0
        fail_preview_once = False
        def draft_hybrid_outline(self, context, generation_mode="STANDARD", checkpoint=None):
            self.outline_calls += 1
            result = super().draft_hybrid_outline(context, generation_mode, checkpoint)
            proposal = (context.planning_context or {}).get("revision_request") or {}
            if proposal.get("capacity_segments"):
                result.temporal_segments = [PlanTemporalSegmentDraft.model_validate(item) for item in proposal["capacity_segments"]]
            else:
                result.temporal_segments[0].suggested_daily_minutes = 30
            return result
        def draft_hybrid_details(self, *args, **kwargs):
            if self.fail_preview_once:
                from planner_api.ai import ProviderUnavailableError
                self.fail_preview_once = False
                raise ProviderUnavailableError("模拟接口断连，仅用于自动化故障注入测试")
            return super().draft_hybrid_details(*args, **kwargs)

    route = _confirmed_route(client)
    goal_id = route["goal_id"]
    provider = OverloadedProvider()
    with session_factory() as session:
        service = PlanService(session, provider, FakePlanDiscussionProvider(), enable_hybrid_work_items=True)
        initial = service.draft_plan(goal_id)
        original_id = initial.plan.id
        with pytest.raises(HTTPException):
            service.draft_next_details(goal_id)  # all three attempts overload 30 minutes
        pending = service.get_workspace(goal_id)
        assert pending.capacity_negotiation["classification"] == "DRAFT_OVERLOAD"
        assert session.scalar(select(func.count()).select_from(PlanTask)) == 0

    with session_factory() as session:
        service = PlanService(session, provider, FakePlanDiscussionProvider(), enable_hybrid_work_items=True)
        assert service.get_workspace(goal_id).capacity_negotiation["state"] == "AWAITING_USER"
        pending = service.revise_plan(goal_id, "我可以把每个执行日增加到90分钟，原期限与任务不变。先预览。")
        # The generic fake discussion has no numeric adjustment contract: it
        # cannot bypass the production consent/preview gate.
        conversation = service._ensure_plan_conversation(goal_id)
        proposal = pending.pending_adjustment.model_copy(update={
            "weekly_intensity_change": "HIGHER", "needs_clarification": False,
            "clarifying_question": None, "can_apply": True,
            "capacity_segments": [initial.plan.temporal_segments[0].model_copy(update={"suggested_daily_minutes": 90})],
        })
        service._add_assistant_message(conversation, proposal.assistant_message, "REVISE", {"adjustment_proposal": proposal.model_dump(mode="json")})
        session.commit()
        with pytest.raises(HTTPException, match="容量协商"):
            service.apply_adjustment(goal_id)
        provider.fail_preview_once = True
        with pytest.raises(HTTPException):
            service.preview_adjustment(goal_id)
        outlines_before_resume = provider.outline_calls
        preview = service.preview_adjustment(goal_id)
        assert provider.outline_calls == outlines_before_resume  # durable author/outline reuse after network failure
        assert service.get_workspace(goal_id).plan.id == original_id
        assert session.scalar(select(func.count()).select_from(PlanTask)) == 0
        # Verified preview can be reread without a second generation.
        assert service.preview_adjustment(goal_id).id == preview.id

    with session_factory() as session:
        service = PlanService(session, provider, FakePlanDiscussionProvider(), enable_hybrid_work_items=True)
        pending = service.get_workspace(goal_id)
        assert pending.pending_adjustment_preview.id == preview.id
        applied = service.apply_adjustment(goal_id, preview.id)
        assert applied.generation_phase == "COMPLETE"
        assert applied.status == "PLAN_REVIEW"
        assert applied.plan.id != original_id
        assert applied.plan.temporal_segments[0].suggested_daily_minutes == 90
        from planner_api.models import RequirementVersion, RouteVersion
        goal = session.get(Goal, goal_id)
        new_requirements = session.get(RequirementVersion, goal.current_requirement_version_id)
        assert new_requirements.snapshot_json["confirmed_capacity_adjustment"]["preview_id"] == preview.id
        assert session.get(RouteVersion, goal.active_route_version_id).requirement_version_id == new_requirements.id
        assert session.get(PlanVersion, original_id).status == "SUPERSEDED"
        assert service.apply_adjustment(goal_id, preview.id).plan.id == applied.plan.id
        assert service.confirm_plan(goal_id).status == "READY_TO_START"
        assert session.get(Goal, goal_id).active_plan_version_id == applied.plan.id


def test_proven_capacity_conflict_persists_outline_without_generating_actions(client, session_factory):
    from fastapi import HTTPException
    from sqlalchemy import select, func
    from test_plan import _confirmed_route
    from planner_api.models import PlanTask
    from planner_api.plan_ai import FakePlanProvider
    from planner_api.plan_discussion_ai import FakePlanDiscussionProvider

    class ImpossibleProvider(FakePlanProvider):
        def draft_hybrid_outline(self, context, generation_mode="STANDARD", checkpoint=None):
            result = super().draft_hybrid_outline(context, generation_mode, checkpoint)
            result.commitments[0].minimum_minutes_per_occurrence = 240
            # This test exercises a proven demand conflict, rather than a
            # model-invented duration without obligation-level evidence.
            result.commitments[0].source_text = "用户确认此训练至少240分钟"
            return result

    goal_id = _confirmed_route(client)["goal_id"]
    with session_factory() as session:
        service = PlanService(session, ImpossibleProvider(), FakePlanDiscussionProvider(), enable_hybrid_work_items=True)
        with pytest.raises(HTTPException) as error:
            service.draft_plan(goal_id)
        assert error.value.detail["code"] == "PLAN_COMMITMENT_CAPACITY_INVALID"
        conflict = service.get_workspace(goal_id).capacity_negotiation
        assert conflict["classification"] == "CONSTRAINT_CONFLICT"
        assert conflict["verified_conflicts"][0]["deficit_minutes"] == 60
        assert service.get_workspace(goal_id).plan.commitments[0].minimum_minutes_per_occurrence == 240
        assert session.scalar(select(func.count()).select_from(PlanTask)) == 0
        with pytest.raises(HTTPException):
            service.prepare_generation(goal_id, "STANDARD")


def test_independent_workloads_with_different_release_weeks_still_share_capacity():
    from planner_api.plan_capacity import capacity_conflicts
    plan = _capacity_plan("OUTCOME")
    plan.commitments_json = [
        {"label": "报告1", "start_week": 1, "end_week": 4, "cadence": "TOTAL", "minimum_occurrences": 1,
         "minimum_total_minutes": 700, "workload_key": "REPORT1"},
        {"label": "报告2", "start_week": 2, "end_week": 4, "cadence": "TOTAL", "minimum_occurrences": 1,
         "minimum_total_minutes": 700, "workload_key": "REPORT2"},
        {"label": "报告1早期提纲", "start_week": 1, "end_week": 2, "cadence": "TOTAL", "minimum_occurrences": 1,
         "minimum_total_minutes": 60, "workload_key": "REPORT1"},
    ]
    conflicts = capacity_conflicts(plan)
    assert conflicts[0]["minimum_minutes"] == 1400  # no duplicate 60 minutes
    assert conflicts[0]["deficit_minutes"] == 320


def test_explicit_capacity_has_no_hidden_estimate_tolerance():
    plan = _capacity_plan("RHYTHM")
    with pytest.raises(Exception) as error:
        PlanService._validate_detail_result(plan, {1: SimpleNamespace(start_week=1, end_week=4)},
                                          [1], [_day(1, 1, 31)])
    assert error.value.detail["code"] == "PLAN_TEMPORAL_SEGMENT_MINUTES_INVALID"


def test_independent_reports_cannot_double_count_the_same_task_minutes():
    promises = [{"label": label, "start_week": 1, "end_week": 1, "cadence": "TOTAL",
                 "minimum_occurrences": 1, "minimum_total_minutes": 30, "workload_key": label}
                for label in ["报告1", "报告2"]]
    day = _day(1, 1, 30)
    day.tasks[0].satisfies_commitments = ["报告1", "报告2"]
    with pytest.raises(WorkItemValidationError, match="重复计入"):
        PlanService._validate_minimum_commitment_minutes(promises, [day])
    day.tasks[0].scheduling_policy_data["workload_minutes"] = 30
    with pytest.raises(WorkItemValidationError, match="重复计入"):
        PlanService._validate_minimum_commitment_minutes(promises, [day])
    day.tasks[0].estimate_minutes = 60
    day.tasks[0].scheduling_policy_data["workload_minutes"] = {"报告1": 30, "报告2": 30}
    PlanService._validate_minimum_commitment_minutes(promises, [day])


def test_persisted_plan_roundtrip_keeps_workload_allocation_and_atomicity(client, session_factory):
    from sqlalchemy import select
    from test_plan import _completed_plan
    from planner_api.models import PlanVersion, PlanTask
    from planner_api.plan_ai import FakePlanProvider
    from planner_api.plan_discussion_ai import FakePlanDiscussionProvider
    from planner_api.schemas import PlanDayDraft
    workspace = _completed_plan(client)
    with session_factory() as session:
        plan = session.get(PlanVersion, workspace["plan"]["id"])
        task = session.scalar(select(PlanTask).where(PlanTask.plan_version_id == plan.id))
        task.title = "独立成果分钟分配往返检查"
        task.estimate_minutes = 60
        task.satisfies_commitments_json = ["报告1", "报告2"]
        task.is_splittable = False
        task.scheduling_policy = "DATE_WINDOW"
        task.scheduling_policy_json = {"workload_minutes": {"REPORT1": 30, "REPORT2": 30},
                                      "work_item_keys": ["REPORT1", "REPORT2"], "occurrence_key": "joint-review"}
        session.flush()
        service = PlanService(session, FakePlanProvider(), FakePlanDiscussionProvider())
        days = [PlanDayDraft.model_validate(item) for item in service._plan_payload(plan)["days"]]
        restored = next(t for day in days for t in day.tasks if t.title == task.title)
        assert not restored.is_splittable
        assert restored.scheduling_policy == "DATE_WINDOW"
        assert restored.work_item_keys == ["REPORT1", "REPORT2"]
        assert restored.occurrence_key == "joint-review"
        assert restored.scheduling_policy_data["workload_minutes"] == {"REPORT1": 30, "REPORT2": 30}
        promises = [{"label": f"报告{i}", "workload_key": f"REPORT{i}", "cadence": "TOTAL",
                     "start_week": 1, "end_week": plan.total_weeks, "minimum_occurrences": 1,
                     "minimum_total_minutes": 30} for i in (1, 2)]
        PlanService._validate_minimum_commitment_minutes(promises, days)


def test_unscheduled_flexible_task_uses_phase_daily_capacity_and_deadline(session_factory):
    from datetime import date, timedelta
    from planner_api.execution_service import ExecutionService
    plan = _capacity_plan("OUTCOME")
    plan.id = "isolated-capacity"
    activation = SimpleNamespace(id="isolated-activation", requested_start_date=date(2026, 11, 9))
    with session_factory() as session:
        service = ExecutionService(session)
        assert service._candidate_target_capacity(plan, activation, activation.requested_start_date)[1] == 30
        assert service._candidate_target_capacity(plan, activation, activation.requested_start_date + timedelta(days=14))[1] == 120
        assert service._candidate_target_capacity(plan, activation, activation.requested_start_date + timedelta(days=28))[1] == 0


def test_new_phased_calendar_keeps_relative_weeks(client, session_factory):
    from datetime import date, timedelta
    from sqlalchemy import select
    from test_plan import _completed_plan
    from planner_api.models import PlanVersion, PlanDay
    from planner_api.execution_service import ExecutionService
    from planner_api.schemas import ExecutionScheduleRequest
    workspace = _completed_plan(client)
    with session_factory() as session:
        plan = session.get(PlanVersion, workspace["plan"]["id"])
        plan.temporal_segments_json = [{"label": "可用日期", "start_week": 1, "end_week": plan.total_weeks,
                                       "available_days_per_week": 4, "suggested_daily_minutes": 90}]
        session.flush()
        payload = ExecutionScheduleRequest(requested_start_date=date(2026, 11, 9), execution_weekdays=[0, 2, 4, 6])
        source = list(session.scalars(select(PlanDay).where(PlanDay.plan_version_id == plan.id).order_by(
            PlanDay.week_index, PlanDay.day_index)))
        mapped = ExecutionService(session)._mapped_days(plan, payload)
        assert len(mapped) == len(source)
        for day, scheduled in zip(source, mapped, strict=True):
            anchor = payload.requested_start_date + timedelta(days=(day.week_index - 1) * 7)
            assert anchor <= scheduled.scheduled_date < anchor + timedelta(days=7)


def test_relative_preview_ignores_calendar_initialization_but_active_preview_does_not(client, session_factory):
    from datetime import date
    from test_plan import _completed_plan
    from planner_api.models import PlanVersion, WorkspaceScheduleProfile, PlanActivation
    from planner_api.plan_ai import FakePlanProvider
    from planner_api.plan_discussion_ai import FakePlanDiscussionProvider
    workspace = _completed_plan(client)
    with session_factory() as session:
        plan = session.get(PlanVersion, workspace["plan"]["id"])
        plan.execution_model_version = "HYBRID_WORK_ITEM"
        service = PlanService(session, FakePlanProvider(), FakePlanDiscussionProvider())
        session.flush()
        original = service._adjustment_schedule_fingerprint(plan)
        profile = WorkspaceScheduleProfile(id="workspace", timezone="Asia/Shanghai", holiday_country_code="CN")
        session.add(profile)
        session.flush()
        assert service._adjustment_schedule_fingerprint(plan) == original
        plan.weekly_minutes += 1
        session.flush()
        assert service._adjustment_schedule_fingerprint(plan) != original
        session.add(PlanActivation(goal_id=plan.goal_id, plan_version_id=plan.id, requested_start_date=date(2026, 11, 9),
                                   projected_end_date=date(2026, 12, 9),
                                   execution_weekdays_json=[0, 2, 4]))
        session.flush()
        active = service._adjustment_schedule_fingerprint(plan)
        profile.break_minutes += 5
        session.flush()
        assert service._adjustment_schedule_fingerprint(plan) != active
