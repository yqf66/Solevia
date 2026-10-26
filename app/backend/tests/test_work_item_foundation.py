from datetime import date, timedelta
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError

from planner_api.ai import ProviderUnavailableError
from planner_api.execution_projection import plan_task_read
from planner_api.execution_service import ExecutionService
from planner_api.models import (
    GenerationBatch,
    Goal,
    PlanActivation,
    PlanDay,
    PlanDaySchedule,
    PlanMilestone,
    PlanTask,
    PlanVersion,
    PlanWorkItem,
    PlanWorkItemGroup,
    ProgressEvent,
    RecoveryOperation,
    RequirementVersion,
    RouteStage,
    RouteVersion,
    WorkItemDependency,
    WorkItemWindow,
)
from planner_api.plan_ai import FakePlanProvider
from planner_api.plan_discussion_ai import FakePlanDiscussionProvider
from planner_api.plan_service import PlanService
from planner_api.work_item_materialization import WorkItemMaterializer
from planner_api.work_item_validation import WorkItemValidationError
from planner_api.schemas import (
    ExecutionScheduleRequest,
    HybridPlanDetailResult,
    CandidateReplacementRequest,
    PlanDayDraft,
    PlanTaskDraft,
    TaskProgressUpdate,
    TriggeredTaskActivationApply,
    TriggeredTaskActivationRequest,
)
from planner_api.work_item_domain import (
    AdjustmentAuthority,
    BindingMode,
    CompletionConstraint,
    ExecutionModelVersion,
    FulfillmentMode,
    PlanWorkItemDraft,
    PlanWorkItemGroupDraft,
    RecurrenceConstraint,
    SelectionConstraint,
    TargetUnit,
    TemporalConstraint,
    WorkItemConstraintSet,
    WorkItemDependencyDraft,
    WorkItemScope,
    legacy_policy_to_constraints,
    remap_materialization_key,
    work_items_enabled,
)


def test_authored_activation_preserves_actions_gates_choice_and_initializes_trigger(session_factory):
    with session_factory() as session:
        goal, plan, milestone, day, activation = _plan_graph(
            session, execution_model_version='HYBRID_WORK_ITEM')
        group = PlanWorkItemGroup(plan_version_id=plan.id, scope_type='PLAN', group_key='OPTIONS', title='任选一项',
                                  minimum_selected=1, maximum_selected=1)
        session.add(group)
        session.flush()
        items = []
        for key in ['PREP', 'CHOICE.A', 'CHOICE.B', 'TRIGGER']:
            item = PlanWorkItem(
                plan_version_id=plan.id, milestone_id=milestone.id, stage_order=1,
                scope_type='WEEK', item_key=key, title=key, description='原始工作项',
                task_kind='BUILD', estimate_minutes=10, priority=1,
                group_id=group.id if key.startswith('CHOICE') else None,
                binding_mode='TRIGGERED' if key == 'TRIGGER' else 'WINDOW',
                fulfillment_mode='ONCE', constraints_json={
                    'temporal': {'start_week': 1, 'end_week': 1},
                    'trigger': {'description': '前置完成后'} if key == 'TRIGGER' else {}})
            session.add(item)
            items.append(item)
        session.flush()
        session.add(WorkItemDependency(plan_version_id=plan.id,
                                       predecessor_work_item_id=items[0].id,
                                       successor_work_item_id=items[3].id))
        for order, item in enumerate(items, 1):
            session.add(PlanTask(plan_version_id=plan.id, plan_day_id=day.id,
                                 order_in_day=order, title='具体动作'+item.item_key,
                                 description='保留真实详细说明', task_kind='BUILD', priority=1,
                                 estimate_minutes=10, source_work_item_id=item.id,
                                 scheduling_policy='FLOATING',
                                 scheduling_policy_json={'work_item_keys': [item.item_key]}))
        session.flush()
        request = ExecutionScheduleRequest(requested_start_date=date(2026, 9, 21),
                                            execution_weekdays=[0], selected_work_item_ids=[items[2].id])
        WorkItemMaterializer(session).attach_authored(plan, activation, request)
        session.commit()
        tasks = session.query(PlanTask).filter_by(plan_version_id=plan.id).all()
        assert {task.title for task in tasks} == {'具体动作PREP', '具体动作CHOICE.B'}
        assert all(task.work_item_window_id for task in tasks)
        assert all(task.description == '保留真实详细说明' for task in tasks)
        assert session.query(WorkItemWindow).count() == 3
        pending = session.query(WorkItemWindow).filter_by(status='PENDING').one()
        assert pending.resolution_json['authored_tasks'][0]['title'] == '具体动作TRIGGER'
        goal.active_plan_version_id = plan.id
        goal.status = 'ACTIVE'
        session.commit()
        service = ExecutionService(session)
        service._today_for = lambda _goal: date(2026, 9, 21)
        assert not service.get_triggered_tasks(goal.id)[0].can_activate
        session.add(WorkItemDependency(plan_version_id=plan.id,
                                       predecessor_work_item_id=items[3].id,
                                       successor_work_item_id=items[2].id))
        session.flush()
        selected_task = next(task for task in tasks if task.source_work_item_id == items[2].id)
        with pytest.raises(HTTPException, match='前置工作项'):
            service.update_task(goal.id, selected_task.id, 'COMPLETED')
        next(task for task in tasks if task.source_work_item_id == items[0].id).status = 'COMPLETED'
        session.commit()
        assert service.get_triggered_tasks(goal.id)[0].can_activate
        for task in tasks:
            task.status = 'COMPLETED'
        service._apply_automatic_completions(goal, plan, day)
        assert goal.status == 'ACTIVE'
        from planner_api.schemas import GoalCompleteRequest
        with pytest.raises(HTTPException, match='Plan tasks are not complete'):
            service.complete_goal(goal.id, GoalCompleteRequest())
        # Restore the successor after deliberately probing the all-cards-done
        # completion guard; real execution must keep it pending until trigger.
        selected_task.status = 'NOT_STARTED'
        session.flush()
        preview = service.preview_triggered_activation(goal.id, TriggeredTaskActivationRequest(
            window_id=pending.id, target_date=date(2026, 9, 21), evidence='前置已完成'))
        assert preview.can_apply
        service.apply_triggered_activation(goal.id, TriggeredTaskActivationApply(preview_id=preview.id))
        service.apply_triggered_activation(goal.id, TriggeredTaskActivationApply(preview_id=preview.id))
        assert session.query(PlanTask).filter_by(source_work_item_id=items[3].id).count() == 1


def test_compiler_binding_restores_frequency_refs_without_dropping_quota(session_factory):
    with session_factory() as session:
        _, plan, _, _, _ = _plan_graph(session, execution_model_version='HYBRID_WORK_ITEM')
        plan.commitments_json = [{'label': '两次推进', 'cadence': 'PER_WEEK', 'start_week': 1,
                                  'end_week': 1, 'minimum_occurrences': 2, 'maximum_occurrences': 2}]
        draft = PlanWorkItemDraft(item_key='SESSION', scope_type=WorkItemScope.WEEK,
                                  stage_order=1, title='两次推进', description='本周两次推进',
                                  task_kind='BUILD', estimate_minutes_per_occurrence=30, priority=1,
                                  source_refs=['confirmed-frequency'],
                                  commitment_refs=['两次推进'], constraint_set=WorkItemConstraintSet(
                                      binding_mode=BindingMode.CADENCE, fulfillment_mode=FulfillmentMode.COUNT,
                                      temporal=TemporalConstraint(start_week=1,end_week=1),
                                      recurrence=RecurrenceConstraint(period='WEEK',target_count=2),
                                      completion=CompletionConstraint(target_value=2,target_unit=TargetUnit.COUNT)))
        days = [PlanDayDraft(stage_order=1, week_index=1, day_index=i,
                              primary_goal='推进本周成果', suggested_minutes=30, evidence=[],
                              tasks=[PlanTaskDraft(order=1,title='明确要点',description='整理并核对要点',
                                                   estimate_minutes=30,priority=1,task_kind='BUILD',evidence=[],
                                                   work_item_keys=['SESSION'])]) for i in [1,2]]
        result = HybridPlanDetailResult(work_items=[draft],days=days)
        result.days[0].tasks[0].scheduling_policy_data = {'occurrence_key': 'ADVANCE.1'}
        service = PlanService(session, FakePlanProvider(), FakePlanDiscussionProvider())
        service._bind_hybrid_actions(plan, result)
        service._validate_commitments_for_week(plan, 1, days)
        assert all(day.tasks[0].satisfies_commitments == ['两次推进'] for day in days)
        assert result.days[0].tasks[0].scheduling_policy_data['occurrence_key'] == 'ADVANCE.1'
        assert plan.commitments_json[0]['minimum_occurrences'] == 2
        result.days[0].week_index = 2
        with pytest.raises(WorkItemValidationError, match='周窗口'):
            service._bind_hybrid_actions(plan, result)


@pytest.mark.parametrize('kind,title', [('SELECT', '选择一个'), ('SELECTION', '选择一个'),
                                      ('LEARN', '题材三选一'), ('DECISION', '从三种题材中选一种')])
def test_selection_work_item_requires_explicit_candidate_group(kind, title):
    from planner_api.work_item_validation import validate_work_item_batch, WorkItemValidationError
    item = PlanWorkItemDraft(item_key='SELECT.ONE', scope_type=WorkItemScope.PLAN,
                             title=title, description='从候选中选一项', task_kind=kind,
                             estimate_minutes_per_occurrence=10, priority=1,
                             source_refs=['test'], constraint_set=WorkItemConstraintSet(
                                 binding_mode=BindingMode.WINDOW, fulfillment_mode=FulfillmentMode.ONCE))
    with pytest.raises(WorkItemValidationError, match='候选组'):
        validate_work_item_batch(work_items=[item], groups=[], dependencies=[],
                                 milestone_weeks={}, commitment_labels=set())


def test_duplicate_work_item_identity_is_rejected_before_database_publish():
    from planner_api.work_item_validation import validate_work_item_batch
    item = PlanWorkItemDraft(item_key='REPORT.ONE', scope_type=WorkItemScope.PLAN,
                             title='报告一', description='保留完整报告', task_kind='BUILD',
                             estimate_minutes_per_occurrence=30, priority=1,
                             source_refs=['confirmed'], constraint_set=WorkItemConstraintSet(
                                 binding_mode=BindingMode.WINDOW, fulfillment_mode=FulfillmentMode.ONCE))
    with pytest.raises(WorkItemValidationError, match='工作项键重复'):
        validate_work_item_batch(work_items=[item, item.model_copy(deep=True)], groups=[],
                                 dependencies=[], milestone_weeks={}, commitment_labels=set())


def _draftable_goal(session):
    goal = Goal(
        title="隔离的 Hybrid 生成测试",
        raw_input="每周形成一个可检查成果",
        status="PLAN_DRAFTING",
        planning_mode="COMMITTED",
        schedule_type="RHYTHM",
    )
    session.add(goal)
    session.flush()
    requirement = RequirementVersion(
        goal_id=goal.id,
        version=1,
        status="CONFIRMED",
        snapshot_json={
            "objective": "形成稳定推进节奏",
            "available_capacity": "每周两次，每次九十分钟",
        },
    )
    session.add(requirement)
    session.flush()
    route = RouteVersion(
        goal_id=goal.id,
        requirement_version_id=requirement.id,
        version=1,
        status="CONFIRMED",
        summary="先形成一个可检查成果",
        prompt_version="test",
        planning_mode="COMMITTED",
        schedule_type="RHYTHM",
    )
    session.add(route)
    session.flush()
    session.add(
        RouteStage(
            route_version_id=route.id,
            order=1,
            title="形成基础成果",
            purpose="完成一个可检查的小成果",
            outputs_json=["基础成果"],
        )
    )
    goal.current_requirement_version_id = requirement.id
    goal.active_route_version_id = route.id
    session.commit()
    return goal


def _plan_graph(session, *, execution_model_version="LEGACY_DAY_BASED"):
    goal = Goal(title="兼容性测试", raw_input="验证旧执行链保持可用")
    session.add(goal)
    session.flush()
    requirement = RequirementVersion(
        goal_id=goal.id,
        version=1,
        status="CONFIRMED",
        snapshot_json={},
    )
    session.add(requirement)
    session.flush()
    route = RouteVersion(
        goal_id=goal.id,
        requirement_version_id=requirement.id,
        version=1,
        status="CONFIRMED",
        summary="测试路线",
        prompt_version="test",
    )
    session.add(route)
    session.flush()
    stage = RouteStage(
        route_version_id=route.id,
        order=1,
        title="第一阶段",
        purpose="验证兼容读取",
    )
    session.add(stage)
    session.flush()
    plan = PlanVersion(
        goal_id=goal.id,
        route_version_id=route.id,
        status="CONFIRMED",
        summary="测试计划",
        total_weeks=1,
        weekly_execution_days=1,
        weekly_minutes=30,
        total_estimated_minutes=30,
        prompt_version="test",
        execution_model_version=execution_model_version,
    )
    session.add(plan)
    session.flush()
    milestone = PlanMilestone(
        plan_version_id=plan.id,
        route_stage_id=stage.id,
        stage_order=1,
        title="第一阶段",
        purpose="验证兼容读取",
        start_week=1,
        end_week=1,
    )
    session.add(milestone)
    session.flush()
    day = PlanDay(
        plan_version_id=plan.id,
        milestone_id=milestone.id,
        stage_order=1,
        week_index=1,
        day_index=1,
        primary_goal="完成一个测试任务",
        suggested_minutes=30,
    )
    session.add(day)
    session.flush()
    activation = PlanActivation(
        goal_id=goal.id,
        plan_version_id=plan.id,
        requested_start_date=date(2026, 9, 21),
        projected_end_date=date(2026, 9, 21),
    )
    session.add(activation)
    session.flush()
    session.add(
        PlanDaySchedule(
            activation_id=activation.id,
            plan_day_id=day.id,
            scheduled_date=date(2026, 9, 21),
        )
    )
    session.flush()
    return goal, plan, milestone, day, activation


def test_hybrid_estimate_counts_actions_once_across_detail_batches(session_factory):
    with session_factory() as session:
        _, plan, milestone, day, _ = _plan_graph(session, execution_model_version='HYBRID_WORK_ITEM')
        plan.total_weeks = 2
        milestone.end_week = 2
        for order, minutes in enumerate([20, 30], 1):
            session.add(PlanTask(plan_version_id=plan.id, plan_day_id=day.id,
                order_in_day=order, title=f'实际动作{order}', description='真实工作量',
                task_kind='BUILD', priority=1, estimate_minutes=minutes))
        service = PlanService(session, FakePlanProvider(), FakePlanDiscussionProvider())
        service._persist_hybrid_work_items(plan, [PlanWorkItemDraft(
            item_key='WORK', title='重复引用的工作身份', description='不是额外90分钟的动作',
            task_kind='BUILD', stage_order=1, scope_type=WorkItemScope.STAGE,
            estimate_minutes_per_occurrence=90, priority=1, source_refs=['confirmed-route:1'],
            constraint_set=WorkItemConstraintSet(binding_mode=BindingMode.WINDOW,
                fulfillment_mode=FulfillmentMode.ONCE))], [], [])
        assert plan.total_estimated_minutes == 50
        plan.total_estimated_minutes += service._persist_days(plan, {1: milestone}, [PlanDayDraft(
            stage_order=1, week_index=2, day_index=1, primary_goal='后续实际动作',
            suggested_minutes=10, evidence=['第二周成果'], tasks=[PlanTaskDraft(order=1, title='实际动作3',
                description='新增十分钟', task_kind='BUILD', priority=1, estimate_minutes=10,
                evidence=['十分钟动作成果'])])])
        assert plan.total_estimated_minutes == 60
        service._persist_hybrid_work_items(plan, [], [], [])
        assert plan.total_estimated_minutes == 60


def test_legacy_plan_and_task_keep_their_original_execution_semantics(session_factory):
    with session_factory() as session:
        _, plan, _, day, _ = _plan_graph(session)
        task = PlanTask(
            plan_version_id=plan.id,
            plan_day_id=day.id,
            order_in_day=1,
            title="原有固定任务",
            description="仍然直接属于原来的计划日",
            estimate_minutes=30,
            priority=3,
            task_kind="PRACTICE",
        )
        session.add(task)
        session.commit()

        assert plan.execution_model_version == ExecutionModelVersion.LEGACY_DAY_BASED
        projected = plan_task_read(task)
        assert projected.source_kind == "LEGACY_PLAN_TASK"
        assert projected.source_work_item_id is None
        assert projected.materialization_key is None


def test_hybrid_work_item_can_materialize_into_the_existing_plan_task(session_factory):
    with session_factory() as session:
        _, plan, milestone, day, activation = _plan_graph(
            session, execution_model_version="HYBRID_WORK_ITEM"
        )
        group = PlanWorkItemGroup(
            plan_version_id=plan.id,
            milestone_id=milestone.id,
            scope_type="STAGE",
            stage_order=1,
            group_key="weekly-options",
            title="本周任选训练",
            selection_mode="SELECT_K",
            minimum_selected=1,
            maximum_selected=1,
        )
        session.add(group)
        session.flush()
        prerequisite = PlanWorkItem(
            plan_version_id=plan.id,
            milestone_id=milestone.id,
            item_key="M1.PREPARE.WARMUP",
            scope_type="STAGE",
            stage_order=1,
            title="完成热身",
            description="先完成热身再进入正式训练",
            task_kind="PREPARE",
            estimate_minutes=10,
            priority=5,
            binding_mode="WINDOW",
            fulfillment_mode="ONCE",
        )
        work_item = PlanWorkItem(
            plan_version_id=plan.id,
            milestone_id=milestone.id,
            group_id=group.id,
            item_key="M1.W01.STRENGTH",
            scope_type="WEEK",
            stage_order=1,
            title="完成一次力量训练",
            description="在本周窗口内选择合适日期完成",
            task_kind="PRACTICE",
            estimate_minutes=30,
            priority=4,
            binding_mode="WINDOW",
            fulfillment_mode="COUNT",
            target_value=1,
            constraints_json={"adjustment": {"authority": "USER_CONFIRM"}},
            source_refs_json=["message-1"],
            commitment_refs_json=["每周完成一次力量训练"],
            status="ACTIVE",
        )
        session.add_all([prerequisite, work_item])
        session.flush()
        session.add(
            WorkItemDependency(
                plan_version_id=plan.id,
                predecessor_work_item_id=prerequisite.id,
                successor_work_item_id=work_item.id,
            )
        )
        window = WorkItemWindow(
            activation_id=activation.id,
            work_item_id=work_item.id,
            period_key="2026-W39",
            eligible_from=date(2026, 9, 21),
            eligible_to=date(2026, 9, 27),
            required_value=1,
            status="OPEN",
        )
        session.add(window)
        session.flush()
        task = PlanTask(
            plan_version_id=plan.id,
            plan_day_id=day.id,
            source_work_item_id=work_item.id,
            work_item_window_id=window.id,
            occurrence_index=1,
            materialization_key=f"{window.id}:1",
            order_in_day=1,
            title=work_item.title,
            description=work_item.description,
            estimate_minutes=work_item.estimate_minutes,
            priority=work_item.priority,
            task_kind=work_item.task_kind,
        )
        session.add(task)
        session.commit()

        projected = plan_task_read(task)
        assert projected.source_kind == "WORK_ITEM_OCCURRENCE"
        assert projected.source_work_item_id == work_item.id
        assert projected.work_item_window_id == window.id
        assert projected.occurrence_index == 1

        plan_read = PlanService(
            session,
            FakePlanProvider(),
            FakePlanDiscussionProvider(),
        )._plan_read(plan)
        assert plan_read.execution_model_version == "HYBRID_WORK_ITEM"
        assert [item.group_key for item in plan_read.work_item_groups] == ["weekly-options"]
        assert plan_read.work_item_groups[0].scope_type == "STAGE"
        assert {item.id for item in plan_read.work_items} == {prerequisite.id, work_item.id}
        read_work_item = next(item for item in plan_read.work_items if item.id == work_item.id)
        assert read_work_item.item_key == "M1.W01.STRENGTH"
        assert read_work_item.scope_type == "WEEK"
        assert read_work_item.commitment_refs == ["每周完成一次力量训练"]
        assert read_work_item.dependency_ids == [prerequisite.id]
        assert read_work_item.windows[0].period_key == "2026-W39"
        assert plan_read.days[0].tasks[0].source_kind == "WORK_ITEM_OCCURRENCE"

        duplicate = PlanTask(
            plan_version_id=plan.id,
            plan_day_id=day.id,
            source_work_item_id=work_item.id,
            work_item_window_id=window.id,
            occurrence_index=1,
            materialization_key=task.materialization_key,
            order_in_day=2,
            title=work_item.title,
            description=work_item.description,
            estimate_minutes=work_item.estimate_minutes,
            priority=work_item.priority,
            task_kind=work_item.task_kind,
        )
        session.add(duplicate)
        with pytest.raises(IntegrityError):
            session.commit()


@pytest.mark.parametrize(
    ("legacy_policy", "expected_binding", "expected_fulfillment"),
    [
        ("EXACT_DAY", BindingMode.FIXED, FulfillmentMode.ONCE),
        ("DATE_WINDOW", BindingMode.WINDOW, FulfillmentMode.ONCE),
        ("FLOATING", BindingMode.WINDOW, FulfillmentMode.ONCE),
        ("CADENCE", BindingMode.CADENCE, FulfillmentMode.COUNT),
        ("QUOTA", BindingMode.WINDOW, FulfillmentMode.COUNT),
        ("TASK_POOL", BindingMode.ON_DEMAND, FulfillmentMode.SELECT_K),
        ("CHECKPOINT", BindingMode.WINDOW, FulfillmentMode.EVIDENCE),
        ("TRIGGERED", BindingMode.TRIGGERED, FulfillmentMode.ONCE),
    ],
)
def test_legacy_policy_mapping_is_conservative_and_structured(
    legacy_policy, expected_binding, expected_fulfillment
):
    data = {
        "period": "WEEK",
        "target_count": 2,
        "group_key": "choices",
        "select_count": 1,
        "trigger": {"event": "prerequisite_completed"},
    }
    policy = legacy_policy_to_constraints(
        legacy_policy,
        data,
        relative_day_ref="M1-W1-D1",
    )
    assert policy.binding_mode == expected_binding
    assert policy.fulfillment_mode == expected_fulfillment


def test_invalid_constraint_combinations_are_rejected_before_persistence():
    with pytest.raises(ValidationError, match="FIXED binding"):
        WorkItemConstraintSet(
            binding_mode=BindingMode.FIXED,
            fulfillment_mode=FulfillmentMode.ONCE,
        )
    with pytest.raises(ValidationError, match="repeating period"):
        WorkItemConstraintSet(
            binding_mode=BindingMode.CADENCE,
            fulfillment_mode=FulfillmentMode.COUNT,
        )
    with pytest.raises(ValidationError, match="MINUTES"):
        WorkItemConstraintSet(
            binding_mode=BindingMode.WINDOW,
            fulfillment_mode=FulfillmentMode.DURATION,
            completion=CompletionConstraint(target_value=30, target_unit=TargetUnit.COUNT),
        )
    with pytest.raises(ValidationError, match="group_key"):
        SelectionConstraint(mode="SELECT_K", minimum_selected=1)


def test_hybrid_generation_persists_constraints_and_daily_actions(session_factory):
    with session_factory() as session:
        goal = _draftable_goal(session)
        service = PlanService(
            session,
            FakePlanProvider(),
            FakePlanDiscussionProvider(),
            enable_hybrid_work_items=True,
        )

        workspace = service.draft_plan(goal.id)
        assert workspace.plan is not None
        assert workspace.plan.execution_model_version == "HYBRID_WORK_ITEM"
        assert workspace.plan.days == []
        assert [item.item_key for item in workspace.plan.work_items] == [
            "PLAN.WEEKLY.REVIEW"
        ]

        workspace = service.draft_next_details(goal.id)
        assert workspace.generation_phase == "COMPLETE"
        assert workspace.plan is not None
        assert len(workspace.plan.days) == 2
        assert workspace.plan.days[0].tasks[0].title != workspace.plan.days[1].tasks[0].title
        assert {item.item_key for item in workspace.plan.work_items} == {
            "PLAN.WEEKLY.REVIEW",
            "M1.W01.PRIMARY",
        }
        batches = list(
            session.query(GenerationBatch)
            .filter(GenerationBatch.goal_id == goal.id)
            .order_by(GenerationBatch.created_at)
        )
        assert [item.status for item in batches] == ["COMPLETED", "COMPLETED"]
        assert all(item.staged_result_json["phase"] == "COMPLETED" for item in batches)
        assert all(item.staged_result_json["source_document"] for item in batches)

        confirmed = service.confirm_plan(goal.id)
        assert confirmed.status == "READY_TO_START"
        assert confirmed.plan is not None
        assert confirmed.plan.execution_model_version == "HYBRID_WORK_ITEM"


def test_hybrid_activation_maps_the_reviewed_daily_plan(session_factory):
    with session_factory() as session:
        goal = _draftable_goal(session)
        planning = PlanService(
            session,
            FakePlanProvider(),
            FakePlanDiscussionProvider(),
            enable_hybrid_work_items=True,
        )
        planning.draft_plan(goal.id)
        planning.draft_next_details(goal.id)
        planning.confirm_plan(goal.id)
        execution = ExecutionService(session)
        request = ExecutionScheduleRequest(
            requested_start_date=date(2026, 9, 14),
            execution_weekdays=[0, 3],
            excluded_dates=[],
        )

        preview = execution.preview(goal.id, request)

        assert preview.materialization_preview_id is None
        assert preview.total_execution_days == 2
        assert session.query(PlanDay).count() == 2
        assert session.query(PlanTask).count() == 2

        started = execution.start(
            goal.id,
            request,
        )

        assert started.goal_id == goal.id
        assert session.query(PlanActivation).count() == 1
        assert session.query(WorkItemWindow).count() == 2
        assert session.query(PlanDay).count() == 2
        assert session.query(PlanTask).count() == 2
        assert session.query(PlanDaySchedule).count() == 2

        execution.start(
            goal.id,
            request,
        )
        assert session.query(PlanActivation).count() == 1
        assert session.query(PlanTask).count() == 2


def test_hybrid_review_keeps_generated_daily_details_stable(session_factory):
    with session_factory() as session:
        goal = _draftable_goal(session)
        planning = PlanService(
            session,
            FakePlanProvider(),
            FakePlanDiscussionProvider(),
            enable_hybrid_work_items=True,
        )
        planning.draft_plan(goal.id)
        workspace = planning.draft_next_details(goal.id)

        preview = ExecutionService(session).preview_plan_days(goal.id)

        assert preview.plan_id == workspace.plan.id
        assert preview.days
        assert preview.days[0].tasks
        assert preview.days[0].tasks[0].description
        assert preview.days[0].suggested_minutes > 0
        assert session.query(PlanDay).count() == 2
        assert session.query(PlanTask).count() == 2
        assert session.query(RecoveryOperation).count() == 0

        plan = session.get(PlanVersion, workspace.plan.id)
        plan.planning_mode = "EXPLORATORY"
        plan.next_unplanned_week = 2
        rolling_preview = ExecutionService(session).preview_plan_days(goal.id)
        assert max(day.scheduled_date for day in rolling_preview.days) <= (
            rolling_preview.candidate_start_date + timedelta(days=6)
        )


def test_work_item_occurrences_can_share_execution_days_when_no_gap_is_required(
    session_factory,
):
    with session_factory() as session:
        payload = ExecutionScheduleRequest(
            requested_start_date=date(2026, 9, 21),
            execution_weekdays=[0, 2, 4],
            excluded_dates=[],
        )
        chosen = WorkItemMaterializer(session)._choose_dates(
            date(2026, 9, 21),
            date(2026, 9, 27),
            4,
            payload,
            {},
            minimum_gap_days=0,
            maximum_per_day=2,
            fixed=False,
        )

        assert len(chosen) == 4
        assert set(chosen) == {
            date(2026, 9, 21),
            date(2026, 9, 23),
            date(2026, 9, 25),
        }
        assert chosen.count(date(2026, 9, 21)) == 2


def test_hybrid_setup_expands_weekdays_to_satisfy_cadence_constraints(session_factory):
    with session_factory() as session:
        goal = _draftable_goal(session)
        planning = PlanService(
            session,
            FakePlanProvider(),
            FakePlanDiscussionProvider(),
            enable_hybrid_work_items=True,
        )
        planning.draft_plan(goal.id)
        workspace = planning.draft_next_details(goal.id)
        item = session.query(PlanWorkItem).filter_by(plan_version_id=workspace.plan.id).first()
        item.binding_mode = "CADENCE"
        item.target_value = 4
        item.constraints_json = {
            "temporal": {"start_week": 1, "end_week": 1},
            "recurrence": {
                "period": "WEEK",
                "target_count": 4,
                "minimum_gap_days": 1,
                "maximum_per_day": 1,
            },
        }
        session.query(PlanTask).filter_by(plan_version_id=workspace.plan.id).delete()
        session.query(PlanDay).filter_by(plan_version_id=workspace.plan.id).delete()
        planning.confirm_plan(goal.id)

        setup = ExecutionService(session).get_setup(goal.id)

        assert setup.minimum_execution_days == 4
        start_weekday = ExecutionService._today_for(goal).weekday()
        assert sorted((day - start_weekday) % 7 for day in setup.suggested_weekdays) == [
            0,
            2,
            4,
            6,
        ]


@pytest.mark.parametrize("confirmed_before_adjustment", [False, True])
def test_hybrid_draft_adjustment_creates_a_valid_lineage_preserving_revision(
    session_factory,
    confirmed_before_adjustment,
):
    with session_factory() as session:
        goal = _draftable_goal(session)
        service = PlanService(
            session,
            FakePlanProvider(),
            FakePlanDiscussionProvider(),
            enable_hybrid_work_items=True,
        )
        service.draft_plan(goal.id)
        original = service.draft_next_details(goal.id)
        original_plan_id = original.plan.id
        if confirmed_before_adjustment:
            service.confirm_plan(goal.id)

        discussed = service.revise_plan(goal.id, "请降低每周负担，并保留每周复盘。")

        assert discussed.pending_adjustment is not None
        assert discussed.pending_adjustment.can_apply is True
        preview = service.preview_adjustment(goal.id)
        assert preview.plan["work_items"]
        assert preview.schedule == []

        revised = service.apply_adjustment(goal.id, preview.id)

        assert revised.plan is not None
        assert revised.plan.id != original_plan_id
        assert revised.plan.revision == 2
        assert revised.plan.execution_model_version == "HYBRID_WORK_ITEM"
        assert revised.plan.days
        assert revised.plan.days[0].tasks
        assert revised.pending_adjustment is None
        source_items = session.query(PlanWorkItem).filter_by(
            plan_version_id=original_plan_id
        ).all()
        source_by_key = {item.item_key: item for item in source_items}
        revised_items = session.query(PlanWorkItem).filter_by(
            plan_version_id=revised.plan.id
        ).all()
        assert revised_items
        assert all(
            item.source_work_item_id == source_by_key[item.item_key].id
            for item in revised_items
            if item.item_key in source_by_key
        )
        assert service._generated_weeks(revised.plan.id) == {1}
        if confirmed_before_adjustment:
            assert revised.plan.status == "CONFIRMED"
            assert goal.active_plan_version_id == revised.plan.id
            assert goal.status == "READY_TO_START"
            assert service.confirm_plan(goal.id).plan.id == revised.plan.id


def test_failed_incomplete_hybrid_plan_allows_discussion_but_running_plan_does_not(session_factory):
    with session_factory() as session:
        goal = _draftable_goal(session)
        service = PlanService(session, FakePlanProvider(), FakePlanDiscussionProvider(), enable_hybrid_work_items=True)
        service.draft_plan(goal.id)
        plan = service._current_plan(goal.id)
        batches = session.query(GenerationBatch).filter_by(plan_version_id=plan.id).all()
        batch = batches[0]
        batch.status = "FAILED"
        session.commit()
        assert service._get_review_inputs(goal.id)[0].id == goal.id
        assert service._require_review_plan(goal.id).id == plan.id
        batch.status = "RUNNING"
        session.commit()
        with pytest.raises(HTTPException):
            service._get_review_inputs(goal.id)


def test_active_hybrid_adjustment_preserves_calendar_completion_and_events(session_factory):
    with session_factory() as session:
        goal = _draftable_goal(session)
        planning = PlanService(session, FakePlanProvider(), FakePlanDiscussionProvider(),
                               enable_hybrid_work_items=True)
        planning.draft_plan(goal.id)
        original = planning.draft_next_details(goal.id)
        planning.confirm_plan(goal.id)
        execution = ExecutionService(session)
        today = execution._today_for(goal)
        request = ExecutionScheduleRequest(requested_start_date=today,
                                            execution_weekdays=sorted([today.weekday(), (today.weekday()+2)%7]),
                                            excluded_dates=[])
        preview = execution.preview(goal.id, request)
        active = execution.start(goal.id, request.model_copy(update={
            "materialization_preview_id": preview.materialization_preview_id,
        }))
        first = active.day.tasks[0]
        execution.update_task(goal.id, first.id, "COMPLETED")
        original_plan_id = original.plan.id
        event_ids = {event.id for event in session.query(ProgressEvent).filter_by(goal_id=goal.id)}
        planning.revise_plan(goal.id, "未来任务说明写得更明确，不改变已完成任务、容量和期限。")
        decision = planning.preview_adjustment(goal.id)
        revised = planning.apply_adjustment(goal.id, decision.id)
        assert goal.active_plan_version_id == revised.plan.id
        assert revised.plan.status == "CONFIRMED"
        assert session.query(PlanActivation).filter_by(plan_version_id=revised.plan.id).count() == 1
        current = execution.get_today(goal.id)
        retained = next(task for task in current.day.tasks if task.title == first.title)
        assert retained.status == "COMPLETED"
        row = session.get(PlanTask, retained.id)
        assert row.source_task_id == first.id
        assert row.lineage_task_id == first.id
        source_activation = session.query(PlanActivation).filter_by(plan_version_id=original_plan_id).one()
        target_activation = session.query(PlanActivation).filter_by(plan_version_id=revised.plan.id).one()
        assert session.query(WorkItemWindow).filter_by(activation_id=source_activation.id).count() == 2
        assert session.query(WorkItemWindow).filter_by(activation_id=target_activation.id).count() == 2
        assert session.get(WorkItemWindow, row.work_item_window_id).activation_id == target_activation.id
        assert session.get(PlanWorkItem, row.source_work_item_id).plan_version_id == revised.plan.id
        assert {event.id for event in session.query(ProgressEvent).filter_by(goal_id=goal.id)} == event_ids
        assert all(event.plan_version_id == revised.plan.id for event in
                   session.query(ProgressEvent).filter_by(goal_id=goal.id))
        assert session.get(PlanVersion, original_plan_id).status == "SUPERSEDED"


def test_hybrid_materialization_rejects_stale_preview(session_factory):
    with session_factory() as session:
        goal = _draftable_goal(session)
        planning = PlanService(
            session,
            FakePlanProvider(),
            FakePlanDiscussionProvider(),
            enable_hybrid_work_items=True,
        )
        planning.draft_plan(goal.id)
        workspace = planning.draft_next_details(goal.id)
        session.query(PlanTask).filter_by(plan_version_id=workspace.plan.id).delete()
        session.query(PlanDay).filter_by(plan_version_id=workspace.plan.id).delete()
        planning.confirm_plan(goal.id)
        execution = ExecutionService(session)
        request = ExecutionScheduleRequest(
            requested_start_date=date(2026, 9, 14),
            execution_weekdays=[0, 3],
            excluded_dates=[],
        )
        preview = execution.preview(goal.id, request)
        work_item = session.query(PlanWorkItem).order_by(PlanWorkItem.item_key).first()
        work_item.description = f"{work_item.description} 已更新"
        session.commit()

        with pytest.raises(HTTPException) as raised:
            execution.start(
                goal.id,
                request.model_copy(
                    update={"materialization_preview_id": preview.materialization_preview_id}
                ),
            )

        assert raised.value.status_code == 409
        assert "重新预览" in str(raised.value.detail)
        operation = session.get(RecoveryOperation, preview.materialization_preview_id)
        assert operation.status == "STALE"
        assert session.query(PlanActivation).count() == 0


@pytest.mark.parametrize(
    "scenario",
    [
        "basic",
        "new_day",
        "outside",
        "stale",
        "dependency",
        "dependency_future",
        "successor",
        "successor_late",
        "paused",
        "blank",
        "replacement",
        "skip_pool",
        "replacement_started",
        "replacement_stale",
        "replacement_dependency",
        "replacement_excluded",
        "skip_split",
        "trigger_quota",
        "trigger_duration",
    ],
)
def test_hybrid_materializer_handles_fixed_selection_dependency_and_trigger(
    session_factory,
    scenario,
):
    with session_factory() as session:
        goal = _draftable_goal(session)
        planning = PlanService(
            session,
            FakePlanProvider(),
            FakePlanDiscussionProvider(),
            enable_hybrid_work_items=True,
        )
        planning.draft_plan(goal.id)
        workspace = planning.draft_next_details(goal.id)
        plan_id = workspace.plan.id
        primary = (
            session.query(PlanWorkItem)
            .filter_by(plan_version_id=plan_id, item_key="M1.W01.PRIMARY")
            .one()
        )
        primary.binding_mode = "FIXED"
        primary.constraints_json = WorkItemConstraintSet(
            binding_mode=BindingMode.FIXED,
            fulfillment_mode=FulfillmentMode.ONCE,
            temporal=TemporalConstraint(exact_date=date(2026, 9, 15), start_week=1, end_week=1),
        ).model_dump(mode="json")
        group = PlanWorkItemGroup(
            plan_version_id=plan_id,
            milestone_id=primary.milestone_id,
            scope_type="WEEK",
            stage_order=1,
            group_key="M1.W01.CHOICE",
            title="二选一实践",
            minimum_selected=1,
            maximum_selected=1,
            constraints_json={},
        )
        session.add(group)
        session.flush()
        candidates = []
        for suffix, priority in (("A", 1), ("B", 2)):
            candidate = PlanWorkItem(
                plan_version_id=plan_id,
                milestone_id=primary.milestone_id,
                group_id=group.id,
                item_key=f"M1.W01.CHOICE.{suffix}",
                scope_type="WEEK",
                stage_order=1,
                title=f"候选实践 {suffix}",
                description="在本周完成一个候选实践。",
                task_kind="BUILD",
                estimate_minutes=30,
                priority=priority,
                is_required=True,
                is_splittable=True,
                binding_mode="WINDOW",
                fulfillment_mode="ONCE",
                target_value=1,
                target_unit="COUNT",
                constraints_json=WorkItemConstraintSet(
                    binding_mode=BindingMode.WINDOW,
                    fulfillment_mode=FulfillmentMode.ONCE,
                    temporal=TemporalConstraint(start_week=1, end_week=1),
                ).model_dump(mode="json"),
                completion_rule_json={"target_value": 1, "target_unit": "COUNT"},
                evidence_json=["实践记录"],
                source_refs_json=["test:selection"],
                commitment_refs_json=[],
                status="DRAFT",
            )
            session.add(candidate)
            session.flush()
            candidates.append(candidate)
        triggered = PlanWorkItem(
            plan_version_id=plan_id,
            milestone_id=primary.milestone_id,
            item_key="M1.W01.TRIGGERED",
            scope_type="WEEK",
            stage_order=1,
            title="出现问题时复盘",
            description="只有出现连续失败时才打开。",
            task_kind="REVIEW",
            estimate_minutes=20,
            priority=3,
            is_required=False,
            is_splittable=True,
            binding_mode="TRIGGERED",
            fulfillment_mode=(
                "DURATION" if scenario == "trigger_duration"
                else "COUNT" if scenario == "trigger_quota"
                else "ONCE"
            ),
            target_value=60 if scenario == "trigger_duration" else 3 if scenario == "trigger_quota" else 1,
            target_unit="MINUTES" if scenario == "trigger_duration" else "COUNT",
            constraints_json=WorkItemConstraintSet(
                binding_mode=BindingMode.TRIGGERED,
                fulfillment_mode=(
                    FulfillmentMode.DURATION if scenario == "trigger_duration"
                    else FulfillmentMode.COUNT if scenario == "trigger_quota"
                    else FulfillmentMode.ONCE
                ),
                temporal=TemporalConstraint(start_week=1, end_week=1),
                completion=CompletionConstraint(
                    target_value=60 if scenario == "trigger_duration" else 3 if scenario == "trigger_quota" else 1,
                    target_unit=TargetUnit.MINUTES if scenario == "trigger_duration" else TargetUnit.COUNT,
                ),
                trigger={"description": "连续两次未完成"},
            ).model_dump(mode="json"),
            completion_rule_json={
                "target_value": 60 if scenario == "trigger_duration" else 3 if scenario == "trigger_quota" else 1,
                "target_unit": "MINUTES" if scenario == "trigger_duration" else "COUNT",
            },
            evidence_json=[],
            source_refs_json=["test:trigger"],
            commitment_refs_json=[],
            status="DRAFT",
        )
        session.add(triggered)
        session.flush()
        session.add(
            WorkItemDependency(
                plan_version_id=plan_id,
                predecessor_work_item_id=primary.id,
                successor_work_item_id=candidates[1].id,
                minimum_lag_days=1,
                condition_json={},
            )
        )
        session.query(PlanTask).filter_by(plan_version_id=plan_id).delete()
        session.query(PlanDay).filter_by(plan_version_id=plan_id).delete()
        session.commit()
        planning.confirm_plan(goal.id)
        execution = ExecutionService(session)
        request = ExecutionScheduleRequest(
            requested_start_date=date(2026, 9, 14),
            execution_weekdays=[1, 2],
            excluded_dates=[],
            selected_work_item_ids=[candidates[1].id],
        )
        execution._today_for = lambda _goal: date(2026, 9, 14)

        preview = execution.preview(goal.id, request)
        execution.start(
            goal.id,
            request.model_copy(
                update={"materialization_preview_id": preview.materialization_preview_id}
            ),
        )

        windows = session.query(WorkItemWindow).all()
        tasks = session.query(PlanTask).all()
        assert len(windows) == 4
        assert len(tasks) == 3
        assert (
            session.query(WorkItemWindow)
            .filter_by(work_item_id=triggered.id, status="PENDING")
            .one()
        )
        assert not session.query(WorkItemWindow).filter_by(work_item_id=candidates[0].id).first()
        primary_task = next(item for item in tasks if item.source_work_item_id == primary.id)
        choice_task = next(item for item in tasks if item.source_work_item_id == candidates[1].id)
        primary_date = (
            session.query(PlanDaySchedule)
            .filter_by(plan_day_id=primary_task.plan_day_id)
            .one()
            .scheduled_date
        )
        choice_date = (
            session.query(PlanDaySchedule)
            .filter_by(plan_day_id=choice_task.plan_day_id)
            .one()
            .scheduled_date
        )
        assert primary_date == date(2026, 9, 15)
        assert choice_date >= date(2026, 9, 16)
        assert primary_task.id in choice_task.depends_on_json

        pools = execution.get_candidate_pools(goal.id)
        assert len(pools) == 1
        assert pools[0].selected_item_ids == [candidates[1].id]
        assert pools[0].completed_selected == 0
        assert pools[0].remaining_required == 1
        assert pools[0].period_status == "IN_PROGRESS"
        if scenario.startswith("replacement_"):
            if scenario == "replacement_started":
                choice_task.status = "IN_PROGRESS"
            elif scenario == "replacement_dependency":
                session.add(WorkItemDependency(
                    plan_version_id=plan_id, predecessor_work_item_id=candidates[1].id,
                    successor_work_item_id=primary.id,
                ))
            elif scenario == "replacement_excluded":
                activation = session.query(PlanActivation).filter_by(plan_version_id=plan_id).one()
                activation.excluded_dates_json = [choice_date.isoformat()]
            session.commit()
            protected_preview = execution.preview_candidate_replacement(
                goal.id, CandidateReplacementRequest(
                    group_id=group.id, candidate_item_id=candidates[0].id,
                    target_date=choice_date, reason="候选替换边界测试",
                ),
            )
            if scenario == "replacement_stale":
                assert protected_preview.can_apply
                choice_task.status = "COMPLETED"
                session.commit()
                with pytest.raises(HTTPException) as raised:
                    execution.apply_candidate_replacement(goal.id, protected_preview.id)
                assert raised.value.status_code == 409
                assert session.get(RecoveryOperation, protected_preview.id).status == "STALE"
                assert choice_task.status == "COMPLETED"
            else:
                assert not protected_preview.can_apply
                assert protected_preview.blockers
            assert not session.query(PlanTask).filter_by(source_work_item_id=candidates[0].id).first()
            return
        if scenario == "replacement":
            replacement_preview = execution.preview_candidate_replacement(
                goal.id,
                CandidateReplacementRequest(
                    group_id=group.id,
                    candidate_item_id=candidates[0].id,
                    target_date=choice_date,
                    reason="原候选材料暂时不可用，改做备用实践",
                ),
            )
            assert replacement_preview.can_apply is True, replacement_preview.blockers
            execution.apply_candidate_replacement(goal.id, replacement_preview.id)
            assert choice_task.status == "CANCELLED"
            assert session.query(PlanTask).filter_by(
                source_work_item_id=candidates[0].id
            ).one().status == "NOT_STARTED"
            assert execution.get_candidate_pools(goal.id)[0].selected_item_ids == [candidates[0].id]
            assert session.query(RecoveryOperation).filter_by(
                mode="CANDIDATE_REPLACEMENT", status="APPLIED"
            ).count() == 1
            return
        if scenario == "skip_pool":
            execution._today_for = lambda _goal: choice_date
            execution.record_task_progress(
                goal.id,
                choice_task.id,
                TaskProgressUpdate(action="SKIP", reason="今天无法完成该候选"),
            )
            skipped_window = session.get(WorkItemWindow, choice_task.work_item_window_id)
            assert skipped_window is not None
            assert skipped_window.status == "CANCELLED"
            assert skipped_window.resolution_json["selection_state"] == "SKIPPED"
            pool_after_skip = execution.get_candidate_pools(goal.id)[0]
            assert pool_after_skip.selected_item_ids == []
            assert pool_after_skip.period_status == "IN_PROGRESS"
            assert pool_after_skip.remaining_required == 1
            supplement = execution.preview_candidate_replacement(
                goal.id,
                CandidateReplacementRequest(
                    group_id=group.id,
                    candidate_item_id=candidates[0].id,
                    target_date=choice_date,
                    reason="跳过原候选后补选备用实践",
                ),
            )
            assert supplement.can_apply, supplement.blockers
            assert supplement.replaced_item_ids == []
            execution.apply_candidate_replacement(goal.id, supplement.id)
            pool_after_supplement = execution.get_candidate_pools(goal.id)[0]
            assert pool_after_supplement.selected_item_ids == [candidates[0].id]
            assert pool_after_supplement.completed_selected == 0
            execution.apply_candidate_replacement(goal.id, supplement.id)
            assert session.query(PlanTask).filter_by(source_work_item_id=candidates[0].id).count() == 1
            return
        # A completed split is still only part of one selected candidate.
        remainder = PlanTask(
            plan_version_id=plan_id,
            plan_day_id=choice_task.plan_day_id,
            source_task_id=choice_task.id,
            source_work_item_id=candidates[1].id,
            work_item_window_id=choice_task.work_item_window_id,
            order_in_day=99,
            title="候选剩余部分",
            description="剩余验证",
            estimate_minutes=10,
            priority=2,
            task_kind="BUILD",
            status="NOT_STARTED",
        )
        session.add(remainder)
        if scenario == "skip_split":
            session.commit()
            execution._today_for = lambda _goal: choice_date
            execution.record_task_progress(
                goal.id, choice_task.id, TaskProgressUpdate(action="SKIP", reason="跳过第一部分"),
            )
            assert session.get(WorkItemWindow, choice_task.work_item_window_id).status == "OPEN"
            assert execution.get_candidate_pools(goal.id)[0].selected_item_ids == [candidates[1].id]
            execution.record_task_progress(
                goal.id, remainder.id, TaskProgressUpdate(action="SKIP", reason="跳过剩余部分"),
            )
            assert session.get(WorkItemWindow, choice_task.work_item_window_id).status == "CANCELLED"
            assert execution.get_candidate_pools(goal.id)[0].selected_item_ids == []
            return
        choice_task.status = "COMPLETED"
        session.flush()
        assert execution.get_candidate_pools(goal.id)[0].remaining_required == 1
        remainder.status = "COMPLETED"
        session.flush()
        assert execution.get_candidate_pools(goal.id)[0].completed_selected == 1
        assert execution.get_candidate_pools(goal.id)[0].period_status == "COMPLETE"

        triggered_tasks = execution.get_triggered_tasks(goal.id)
        assert len(triggered_tasks) == 1
        trigger = triggered_tasks[0]
        if scenario == "blank":
            with pytest.raises(ValidationError):
                TriggeredTaskActivationRequest(
                    window_id=trigger.window_id, target_date=date(2026, 9, 16), evidence="   "
                )
            return
        if scenario in {"dependency", "dependency_future"}:
            session.add(
                WorkItemDependency(
                    plan_version_id=plan_id,
                    predecessor_work_item_id=primary.id,
                    successor_work_item_id=triggered.id,
                    minimum_lag_days=1,
                )
            )
            session.flush()
        if scenario in {"successor", "successor_late"}:
            choice_task.status = "NOT_STARTED"
            remainder.status = "NOT_STARTED"
            session.add(
                WorkItemDependency(
                    plan_version_id=plan_id,
                    predecessor_work_item_id=triggered.id,
                    successor_work_item_id=candidates[1].id,
                    minimum_lag_days=1,
                )
            )
            session.flush()
        if scenario == "paused":
            goal.status = "PAUSED"
            session.flush()
        target_date = date(2026, 9, 20) if scenario == "new_day" else date(2026, 9, 16)
        if scenario == "outside":
            target_date = date(2026, 9, 21)
        if scenario == "dependency":
            target_date = date(2026, 9, 14)
        if scenario == "successor":
            target_date = choice_date - timedelta(days=1)
        if scenario == "successor_late":
            target_date = date(2026, 9, 20)
        trigger_preview = execution.preview_triggered_activation(
            goal.id,
            TriggeredTaskActivationRequest(
                window_id=trigger.window_id,
                target_date=target_date,
                evidence="连续两次未完成，用户确认开启复盘",
            ),
        )
        if scenario in {"outside", "dependency", "paused", "successor_late"}:
            assert not trigger_preview.can_apply
            assert trigger_preview.blockers
            with pytest.raises(HTTPException):
                execution.apply_triggered_activation(
                    goal.id, TriggeredTaskActivationApply(preview_id=trigger_preview.id)
                )
            assert not session.query(PlanTask).filter_by(source_work_item_id=triggered.id).first()
            return
        if scenario == "stale":
            pending_window = session.get(WorkItemWindow, trigger.window_id)
            pending_window.materialization_revision += 1
            session.commit()
            with pytest.raises(HTTPException) as raised:
                execution.apply_triggered_activation(
                    goal.id, TriggeredTaskActivationApply(preview_id=trigger_preview.id)
                )
            assert raised.value.status_code == 409
            assert session.get(RecoveryOperation, trigger_preview.id).status == "STALE"
            assert not session.query(PlanTask).filter_by(source_work_item_id=triggered.id).first()
            return
        assert trigger_preview.can_apply is True, trigger_preview.blockers
        count_before = session.query(PlanTask).count()
        execution.apply_triggered_activation(
            goal.id,
            TriggeredTaskActivationApply(preview_id=trigger_preview.id),
        )
        activated_window = session.query(WorkItemWindow).filter_by(
            work_item_id=triggered.id, status="OPEN"
        ).one()
        assert activated_window.status == "OPEN"
        activated_task = session.query(PlanTask).filter_by(source_work_item_id=triggered.id).one()
        assert activated_task.scheduling_policy == "TRIGGERED"
        if scenario == "dependency_future":
            assert primary_task.id in activated_task.depends_on_json
        if scenario == "successor":
            assert activated_task.id in choice_task.depends_on_json
        assert (
            session.query(ProgressEvent)
            .filter_by(task_id=activated_task.id, event_type="TRIGGER_ACTIVATED")
            .one()
        )
        if scenario in {"trigger_quota", "trigger_duration"}:
            remaining_trigger_windows = execution.get_triggered_tasks(goal.id)
            assert len(remaining_trigger_windows) == 1
            expected_target = 60 if scenario == "trigger_duration" else 3
            assert remaining_trigger_windows[0].remaining_value == expected_target
            # Keep this assertion encoding-neutral for Windows test runners.
            assert f"0/{expected_target}" in remaining_trigger_windows[0].explanation
            assert str(expected_target) in remaining_trigger_windows[0].explanation
            assert remaining_trigger_windows[0].can_activate is False
            activated_task = session.query(PlanTask).filter_by(
                plan_version_id=plan_id,
                source_work_item_id=triggered.id,
                work_item_window_id=trigger.window_id,
            ).one()
            assert activated_task is not None
            if scenario == "trigger_duration":
                session.add(ProgressEvent(
                    goal_id=goal.id,
                    plan_version_id=plan_id,
                    plan_day_id=activated_task.plan_day_id,
                    task_id=activated_task.id,
                    event_type="PARTIAL_COMPLETION",
                    metadata_json={"amount": 20, "unit": "MINUTES"},
                ))
            activated_task.status = "COMPLETED"
            session.commit()
            next_trigger = execution.get_triggered_tasks(goal.id)[0]
            assert next_trigger.can_activate is True
            assert next_trigger.completed_value == (20 if scenario == "trigger_duration" else 1)
            assert next_trigger.remaining_value == (40 if scenario == "trigger_duration" else 2)
        else:
            assert execution.get_triggered_tasks(goal.id) == []
        execution.apply_triggered_activation(
            goal.id, TriggeredTaskActivationApply(preview_id=trigger_preview.id)
        )
        assert session.query(PlanTask).count() == count_before + 1
        if scenario == "new_day":
            assert (
                session.query(PlanDaySchedule)
                .filter_by(plan_day_id=activated_task.plan_day_id)
                .one()
                .scheduled_date
                == target_date
            )
            assert session.query(PlanActivation).one().projected_end_date >= target_date


def test_fixed_work_item_date_is_not_moved_by_general_calendar_filter(session_factory):
    with session_factory() as session:
        materializer = WorkItemMaterializer(session, date_allowed=lambda _value: False)
        request = ExecutionScheduleRequest(
            requested_start_date=date(2026, 10, 1),
            execution_weekdays=[0],
            excluded_dates=[],
        )

        selected = materializer._choose_dates(
            date(2026, 10, 1),
            date(2026, 10, 1),
            1,
            request,
            {},
            minimum_gap_days=0,
            maximum_per_day=1,
            fixed=True,
        )

        assert selected == [date(2026, 10, 1)]


def test_hybrid_failed_compile_keeps_author_checkpoint_and_retries_idempotently(
    session_factory,
):
    class FailingHybridProvider(FakePlanProvider):
        def draft_hybrid_details(
            self,
            context,
            outline,
            start_week,
            end_week,
            generation_mode="STANDARD",
            checkpoint=None,
        ):
            if checkpoint:
                checkpoint("# 已正确理解的作者设计稿")
            raise ProviderUnavailableError(
                "结构化编译失败",
                code="INVALID_MODEL_RESPONSE",
                diagnostic="missing work_items",
            )

    with session_factory() as session:
        goal = _draftable_goal(session)
        working = PlanService(
            session,
            FakePlanProvider(),
            FakePlanDiscussionProvider(),
            enable_hybrid_work_items=True,
        )
        working.draft_plan(goal.id)
        failing = PlanService(
            session,
            FailingHybridProvider(),
            FakePlanDiscussionProvider(),
            enable_hybrid_work_items=True,
        )

        with pytest.raises(HTTPException) as raised:
            failing.draft_next_details(goal.id)
        assert raised.value.status_code == 503
        batch = session.query(GenerationBatch).filter_by(scope_type="HYBRID_WEEK").one()
        assert batch.status == "FAILED"
        assert batch.attempt_count == 1
        assert batch.staged_result_json["phase"] == "AUTHOR_COMPLETE"
        assert batch.staged_result_json["source_document"] == "# 已正确理解的作者设计稿"

        workspace = working.draft_next_details(goal.id)
        session.refresh(batch)
        assert workspace.generation_phase == "COMPLETE"
        assert batch.status == "COMPLETED"
        assert batch.attempt_count == 2
        assert session.query(PlanWorkItem).filter_by(plan_version_id=workspace.plan.id).count() == 2


def test_hybrid_business_validation_repairs_outline_and_detail_before_persisting(
    session_factory,
):
    class BusinessRepairProvider(FakePlanProvider):
        outline_calls = 0
        detail_calls = 0

        def draft_hybrid_outline(self, context, generation_mode="STANDARD", checkpoint=None):
            self.outline_calls += 1
            result = super().draft_hybrid_outline(context, generation_mode, checkpoint)
            if self.outline_calls == 1:
                return result.model_copy(
                    update={
                        "work_item_groups": [
                            PlanWorkItemGroupDraft(
                                group_key="PLAN.MISSING.CANDIDATE",
                                scope_type=WorkItemScope.PLAN,
                                title="不完整候选组",
                                minimum_selected=1,
                                maximum_selected=1,
                                candidate_item_keys=["PLAN.NOT.CREATED"],
                                source_refs=["test:first-outline"],
                            )
                        ]
                    }
                )
            return result

        def draft_hybrid_details(
            self,
            context,
            outline,
            start_week,
            end_week,
            generation_mode="STANDARD",
            checkpoint=None,
        ):
            self.detail_calls += 1
            result = super().draft_hybrid_details(
                context,
                outline,
                start_week,
                end_week,
                generation_mode,
                checkpoint,
            )
            if self.detail_calls == 1:
                item = result.work_items[0]
                return result.model_copy(
                    update={
                        "work_items": [
                            item.model_copy(
                                update={
                                    "constraint_set": item.constraint_set.model_copy(
                                        update={
                                            "temporal": TemporalConstraint(
                                                start_week=2, end_week=2
                                            )
                                        }
                                    )
                                }
                            )
                        ]
                    }
                )
            return result

    with session_factory() as session:
        goal = _draftable_goal(session)
        provider = BusinessRepairProvider()
        service = PlanService(
            session,
            provider,
            FakePlanDiscussionProvider(),
            enable_hybrid_work_items=True,
        )

        service.draft_plan(goal.id)
        workspace = service.draft_next_details(goal.id)

        assert provider.outline_calls == 2
        assert provider.detail_calls == 2
        assert workspace.generation_phase == "COMPLETE"
        assert {item.item_key for item in workspace.plan.work_items} == {
            "PLAN.WEEKLY.REVIEW",
            "M1.W01.PRIMARY",
        }


@pytest.mark.parametrize("failed_calls", [1, 3])
def test_hybrid_capacity_failure_repairs_with_visible_progress_and_resumable_draft(
    session_factory, failed_calls,
):
    class CapacityRepairProvider(FakePlanProvider):
        detail_calls = 0

        def draft_hybrid_details(self, context, outline, start_week, end_week,
                                 generation_mode="STANDARD", checkpoint=None):
            self.detail_calls += 1
            if self.detail_calls > 1:
                feedback = context.planning_context["generation_validation_feedback"]
                assert feedback["code"] == "PLAN_TEMPORAL_SEGMENT_MINUTES_INVALID"
                assert "不要减少必做次数" in feedback["repair_policy"]
                assert context.source_document is None
                assert context.requirements["available_capacity"] == "每周两次，每次九十分钟"
                live = service.get_workspace(goal.id)
                if self.detail_calls <= 3:
                    assert live.generation_repair.attempt == self.detail_calls
                    assert live.generation_repair.week_index == 1
                else:
                    assert live.generation_repair is None  # No stale progress on manual retry.
                assert not live.plan.days  # Failed output never becomes execution tasks.
            result = super().draft_hybrid_details(
                context, outline, start_week, end_week, generation_mode, checkpoint,
            )
            if self.detail_calls <= failed_calls:
                days = [day.model_copy(update={
                    "suggested_minutes": 999,
                    "tasks": [task.model_copy(update={"estimate_minutes": 999})
                              for task in day.tasks],
                }) for day in result.days]
                return result.model_copy(update={"days": days})
            return result

    with session_factory() as session:
        goal = _draftable_goal(session)
        provider = CapacityRepairProvider()
        service = PlanService(session, provider, FakePlanDiscussionProvider(),
                              enable_hybrid_work_items=True)
        service.draft_plan(goal.id)
        plan = session.query(PlanVersion).filter_by(goal_id=goal.id).one()
        plan.temporal_segments_json = [{"start_week": 1, "end_week": plan.total_weeks,
                                       "weekly_execution_days": 2, "suggested_daily_minutes": 90,
                                       "label": "正常执行期", "date_scope": "相对周期",
                                       "capacity_summary": "每周两次，每次90分钟"}]
        session.commit()
        if failed_calls == 3:
            with pytest.raises(HTTPException) as raised:
                service.draft_next_details(goal.id)
            assert "自动调整后" in raised.value.detail["message"]
            assert "复核需求" in raised.value.detail["message"]
            assert provider.detail_calls == 3
            batch = session.query(GenerationBatch).filter_by(scope_type="HYBRID_WEEK").one()
            assert batch.status == "FAILED"
            assert batch.staged_result_json["source_document"]
            assert service.get_workspace(goal.id).generation_repair is None
        workspace = service.draft_next_details(goal.id)
        assert workspace.generation_phase == "COMPLETE"
        assert workspace.generation_repair is None
        assert provider.detail_calls == failed_calls + 1
        assert plan.temporal_segments_json[0]["suggested_daily_minutes"] == 90
        batch = session.query(GenerationBatch).filter_by(scope_type="HYBRID_WEEK").one()
        failures = batch.staged_result_json["validation"]["automatic_repair_failures"]
        assert len(failures) == failed_calls
        assert [failure["attempt"] for failure in failures] == list(range(1, failed_calls + 1))


def test_valid_mixed_constraint_and_feature_gate_require_explicit_opt_in():
    policy = WorkItemConstraintSet(
        binding_mode=BindingMode.CADENCE,
        fulfillment_mode=FulfillmentMode.COUNT,
        temporal=TemporalConstraint(start_week=1, end_week=8, allowed_weekdays=[1, 3, 5]),
        recurrence=RecurrenceConstraint(
            period="WEEK",
            target_count=3,
            minimum_gap_days=1,
        ),
        completion=CompletionConstraint(target_value=3),
    )
    assert policy.recurrence.minimum_gap_days == 1
    assert not work_items_enabled(ExecutionModelVersion.LEGACY_DAY_BASED, True)
    assert not work_items_enabled(ExecutionModelVersion.HYBRID_WORK_ITEM, False)
    assert work_items_enabled(ExecutionModelVersion.HYBRID_WORK_ITEM, True)


def test_materialization_key_is_stable_per_target_plan_and_never_reused():
    first = remap_materialization_key("window:1", "plan-a")
    same = remap_materialization_key("window:1", "plan-a")
    other = remap_materialization_key("window:1", "plan-b")
    assert first == same
    assert first != other
    assert remap_materialization_key(None, "plan-a") is None


def test_locked_fixed_policy_remains_explicit():
    policy = legacy_policy_to_constraints(
        "EXACT_DAY", {}, relative_day_ref="M2-W3-D4"
    )
    assert policy.temporal.relative_day_ref == "M2-W3-D4"
    assert policy.adjustment.authority == AdjustmentAuthority.LOCKED


def test_hybrid_work_item_draft_freezes_identity_scope_and_evidence_contract():
    draft = PlanWorkItemDraft(
        item_key="PLAN.STANDING.REVIEW",
        scope_type=WorkItemScope.PLAN,
        title="持续复盘",
        description="整个计划期间保持每周复盘",
        task_kind="REVIEW",
        estimate_minutes_per_occurrence=20,
        priority=4,
        constraint_set=WorkItemConstraintSet(
            binding_mode=BindingMode.CADENCE,
            fulfillment_mode=FulfillmentMode.COUNT,
            temporal=TemporalConstraint(start_week=1, end_week=4),
            recurrence=RecurrenceConstraint(period="WEEK", target_count=1),
            completion=CompletionConstraint(target_value=1),
        ),
        commitment_refs=["每周复盘"],
        source_refs=["fact:weekly-review"],
    )
    assert draft.stage_order is None
    assert draft.constraint_set.binding_mode == BindingMode.CADENCE


@pytest.mark.parametrize(
    "payload, message",
    [
        (
            {
                "item_key": "lowercase-key",
                "scope_type": "PLAN",
                "title": "非法键",
                "description": "稳定键必须规范",
                "task_kind": "REVIEW",
                "estimate_minutes_per_occurrence": 10,
                "priority": 3,
                "constraint_set": {
                    "binding_mode": "WINDOW",
                    "fulfillment_mode": "ONCE",
                },
                "source_refs": ["fact:1"],
            },
            "uppercase stable business key",
        ),
        (
            {
                "item_key": "M1.MISSING_STAGE",
                "scope_type": "STAGE",
                "title": "缺少阶段",
                "description": "阶段工作项必须指向阶段",
                "task_kind": "REVIEW",
                "estimate_minutes_per_occurrence": 10,
                "priority": 3,
                "constraint_set": {
                    "binding_mode": "WINDOW",
                    "fulfillment_mode": "ONCE",
                },
                "source_refs": ["fact:1"],
            },
            "requires a stage_order",
        ),
        (
            {
                "item_key": "M1.WEEK.BAD_RANGE",
                "scope_type": "WEEK",
                "stage_order": 1,
                "title": "跨周事项",
                "description": "周作用域不能跨越两周",
                "task_kind": "REVIEW",
                "estimate_minutes_per_occurrence": 10,
                "priority": 3,
                "constraint_set": {
                    "binding_mode": "WINDOW",
                    "fulfillment_mode": "ONCE",
                    "temporal": {"start_week": 1, "end_week": 2},
                },
                "source_refs": ["fact:1"],
            },
            "one matching start_week and end_week",
        ),
    ],
)
def test_hybrid_work_item_draft_rejects_ambiguous_identity_and_scope(payload, message):
    with pytest.raises(ValidationError, match=message):
        PlanWorkItemDraft.model_validate(payload)


def test_group_and_dependency_drafts_use_stable_keys_and_valid_bounds():
    group = PlanWorkItemGroupDraft(
        group_key="M1.W01.EXERCISE_POOL",
        scope_type=WorkItemScope.WEEK,
        stage_order=1,
        title="本周训练任选一项",
        minimum_selected=1,
        maximum_selected=1,
        candidate_item_keys=["M1.W01.SQUAT", "M1.W01.PUSH"],
        source_refs=["message:12"],
    )
    dependency = WorkItemDependencyDraft(
        predecessor_key="M1.W01.WARMUP",
        successor_key="M1.W01.SQUAT",
        minimum_lag_days=0,
        source_refs=["route-stage:1"],
    )
    assert group.minimum_selected == 1
    assert dependency.predecessor_key != dependency.successor_key

    with pytest.raises(ValidationError, match="candidate count"):
        PlanWorkItemGroupDraft(
            group_key="M1.W01.BAD_POOL",
            scope_type=WorkItemScope.WEEK,
            stage_order=1,
            title="非法候选池",
            minimum_selected=3,
            maximum_selected=3,
            candidate_item_keys=["M1.W01.A", "M1.W01.B"],
            source_refs=["message:13"],
        )
    with pytest.raises(ValidationError, match="depend on itself"):
        WorkItemDependencyDraft(
            predecessor_key="M1.W01.SAME",
            successor_key="M1.W01.SAME",
            source_refs=["message:14"],
        )


def test_hybrid_detail_contract_keeps_constraints_and_daily_actions_together():
    work_item = PlanWorkItemDraft(
        item_key="M1.W01.PRACTICE",
        scope_type=WorkItemScope.WEEK,
        stage_order=1,
        title="完成本周练习",
        description="在第一周的窗口内完成",
        task_kind="PRACTICE",
        estimate_minutes_per_occurrence=30,
        priority=3,
        constraint_set=WorkItemConstraintSet(
            binding_mode=BindingMode.WINDOW,
            fulfillment_mode=FulfillmentMode.ONCE,
            temporal=TemporalConstraint(start_week=1, end_week=1),
        ),
        source_refs=["route-stage:1"],
    )
    day = PlanDayDraft(
        stage_order=1,
        week_index=1,
        day_index=1,
        primary_goal="完成第一轮基础练习",
        suggested_minutes=30,
        tasks=[
            PlanTaskDraft(
                order=1,
                title="记录基线并完成练习",
                description="先记录当前结果，再完成一组可检查的基础练习。",
                estimate_minutes=30,
                priority=1,
                task_kind="PRACTICE",
                evidence=["练习记录"],
            )
        ],
        evidence=["练习记录"],
    )
    detail = HybridPlanDetailResult(work_items=[work_item], days=[day])
    assert detail.work_items[0].item_key == "M1.W01.PRACTICE"
    assert detail.days[0].tasks[0].title == "记录基线并完成练习"

    with pytest.raises(ValidationError, match="days"):
        HybridPlanDetailResult(work_items=[work_item])


def test_explicit_deadline_rejects_an_impossible_plan_horizon():
    requirements = {"deadline": "2026-11-15"}

    PlanService._validate_deadline_horizon(
        SimpleNamespace(total_weeks=9),
        requirements,
        "Asia/Shanghai",
        today=date(2026, 9, 18),
    )
    with pytest.raises(WorkItemValidationError, match="maximum_weeks=9"):
        PlanService._validate_deadline_horizon(
            SimpleNamespace(total_weeks=16),
            requirements,
            "Asia/Shanghai",
            today=date(2026, 9, 18),
        )
