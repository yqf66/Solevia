"""Cross-domain dependency contracts, activation and execution regressions."""

from datetime import date, timedelta
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import select

from planner_api.dependency_matching import DependencyBindingError, select_predecessors
from planner_api.execution_service import ExecutionService
from planner_api.models import PlanDay, PlanDaySchedule, PlanTask, PlanWorkItem, WorkItemDependency
from planner_api.plan_service import PlanService
from planner_api.schemas import ExecutionScheduleRequest, PlanTaskDraft
from planner_api.work_item_dependencies import WorkItemDependencyGraph
from planner_api.work_item_domain import WorkItemDependencyDraft
from planner_api.work_item_materialization import WorkItemMaterializer
from planner_api.work_item_validation import (
    WorkItemValidationError,
    validate_authored_dependency_order,
)
from test_work_item_foundation import _plan_graph


def action(item, occurrence=None, *, period=None, order=1):
    return PlanTaskDraft(
        order=order,
        title=f"具体行动{item}",
        description="执行并保留真实成果",
        estimate_minutes=30,
        priority=1,
        task_kind="BUILD",
        evidence=["成果"],
        work_item_keys=[item],
        occurrence_key=occurrence,
        scheduling_policy_data={"dependency_period_key": period} if period else {},
    )


def day(index, *tasks, week=1):
    return SimpleNamespace(week_index=week, day_index=index, tasks=list(tasks))


def edge(mode="ALL"):
    return dict(predecessor_key="DRAFT", successor_key="REVIEW", matching_mode=mode)


@pytest.mark.parametrize("series", ["EXAM", "ARTICLE", "FEATURE", "PLANT"])
def test_interleaved_objects_do_not_become_global_barriers(series):
    days = [
        day(1, action("DRAFT", f"{series}.01")),
        day(2, action("REVIEW", f"{series}.01")),
        day(3, action("DRAFT", f"{series}.02")),
        day(4, action("REVIEW", f"{series}.02")),
    ]
    validate_authored_dependency_order(days, [edge("MATCH_OCCURRENCE")])
    with pytest.raises(WorkItemValidationError):
        validate_authored_dependency_order(days, [edge()])


def test_split_prerequisite_requires_every_part_and_cross_week_identity():
    days = [
        day(1, action("DRAFT", "ARTICLE.01")),
        day(2, action("DRAFT", "ARTICLE.01")),
        day(1, action("REVIEW", "ARTICLE.01"), week=2),
    ]
    validate_authored_dependency_order(days, [edge("MATCH_OCCURRENCE")])
    days[1] = day(2, action("DRAFT", "ARTICLE.01"), week=2)
    with pytest.raises(WorkItemValidationError):
        validate_authored_dependency_order(days, [edge("MATCH_OCCURRENCE")])


def test_global_gate_cannot_hide_a_final_prerequisite_inside_successor():
    combined = action("DRAFT", order=1)
    combined.work_item_keys.append("REVIEW")
    with pytest.raises(WorkItemValidationError):
        validate_authored_dependency_order([day(1, action("DRAFT")), day(2, combined)], [edge()])


@pytest.mark.parametrize("missing", [None, "OTHER.01"])
def test_missing_or_wrong_instance_fails_before_execution(missing):
    with pytest.raises(WorkItemValidationError, match="标识|对应前置"):
        validate_authored_dependency_order(
            [day(1, action("DRAFT", "ARTICLE.01")), day(2, action("REVIEW", missing))],
            [edge("MATCH_OCCURRENCE")],
        )


def test_weekly_review_does_not_wait_for_future_weeks():
    days = [
        day(1, action("DRAFT")),
        day(2, action("REVIEW")),
        day(1, action("DRAFT"), week=2),
        day(2, action("REVIEW"), week=2),
    ]
    validate_authored_dependency_order(days, [edge("MATCH_PERIOD")])
    with pytest.raises(WorkItemValidationError):
        validate_authored_dependency_order(days, [edge("ALL")])
    # Moving the original first week's review preserves its original period.
    days[1] = day(3, action("REVIEW", period="week:1"), week=2)
    validate_authored_dependency_order(days, [edge("MATCH_PERIOD")])


def test_matching_schema_rejects_unknown_and_reuse_preserves_changes():
    with pytest.raises(ValidationError):
        WorkItemDependencyDraft(**edge("GUESS"), source_refs=["用户要求"])
    old = WorkItemDependencyDraft(**edge(), source_refs=["用户要求"])
    changed = WorkItemDependencyDraft(**edge("MATCH_OCCURRENCE"), source_refs=["用户要求"])
    values = [old, changed]
    assert (
        PlanService._reuse_registered_dependencies(
            values, [old.model_dump(mode="json", exclude={"source_refs"})]
        )
        == 1
    )
    assert values == [changed]


def graph_fixture(session, mode="MATCH_OCCURRENCE"):
    goal, plan, milestone, first_day, activation = _plan_graph(
        session, execution_model_version="HYBRID_WORK_ITEM"
    )
    plan.total_weeks = 2
    milestone.end_week = 2
    activation.projected_end_date = date(2026, 10, 4)
    items = []
    for key in ("DRAFT", "REVIEW", "FINAL"):
        item = PlanWorkItem(
            plan_version_id=plan.id,
            milestone_id=milestone.id,
            stage_order=1,
            scope_type="STAGE",
            item_key=key,
            title=key,
            description="具体工作",
            task_kind="BUILD",
            estimate_minutes=30,
            priority=1,
            binding_mode="WINDOW",
            fulfillment_mode="COUNT",
            target_value=2,
            target_unit="COUNT",
            constraints_json={"temporal": {"start_week": 1, "end_week": 2}},
        )
        session.add(item)
        items.append(item)
    session.flush()
    session.add(
        WorkItemDependency(
            plan_version_id=plan.id,
            predecessor_work_item_id=items[0].id,
            successor_work_item_id=items[1].id,
            matching_mode=mode,
        )
    )
    tasks = []
    for index, (which, occurrence) in enumerate(
        [(0, "SERIES.1"), (1, "SERIES.1"), (0, "SERIES.2"), (1, "SERIES.2")]
    ):
        current_day = first_day
        if index:
            current_day = PlanDay(
                plan_version_id=plan.id,
                milestone_id=milestone.id,
                stage_order=1,
                week_index=1 if index < 2 else 2,
                day_index=index + 1,
                primary_goal="完成本次具体行动",
                suggested_minutes=90,
            )
            session.add(current_day)
            session.flush()
            session.add(
                PlanDaySchedule(
                    activation_id=activation.id,
                    plan_day_id=current_day.id,
                    scheduled_date=date(2026, 9, 21)
                    + timedelta(days=index if index < 2 else index + 5),
                )
            )
        task = PlanTask(
            plan_version_id=plan.id,
            plan_day_id=current_day.id,
            source_work_item_id=items[which].id,
            order_in_day=1,
            title=occurrence + items[which].title,
            description="原始任务说明",
            estimate_minutes=30,
            priority=1,
            task_kind="BUILD",
            scheduling_policy="FLOATING",
            scheduling_policy_json={
                "occurrence_key": occurrence,
                "dependency_period_key": f"week:{current_day.week_index}",
                "work_item_keys": [items[which].item_key],
            },
        )
        session.add(task)
        tasks.append(task)
    session.flush()
    goal.active_plan_version_id = plan.id
    goal.status = "ACTIVE"
    session.flush()
    return goal, plan, activation, items, tasks


@pytest.mark.parametrize("mode", ["MATCH_OCCURRENCE", "MATCH_PERIOD"])
def test_runtime_pairing_partial_cancel_and_independent_completion(session_factory, mode):
    with session_factory() as session:
        _, plan, activation, _, tasks = graph_fixture(session, mode)
        graph = WorkItemDependencyGraph(session, plan.id)
        graph.validate_dates(session, activation.id)
        graph.bind()
        assert tasks[1].depends_on_json == [tasks[0].id]
        assert tasks[3].depends_on_json == [tasks[2].id]
        service = ExecutionService(session)
        assert service._hybrid_predecessor_blocked_reason(plan, tasks[1])
        tasks[0].status = "IN_PROGRESS"
        assert service._hybrid_predecessor_blocked_reason(plan, tasks[1])
        tasks[0].status = "COMPLETED"
        assert service._hybrid_predecessor_blocked_reason(plan, tasks[1]) is None
        assert service._hybrid_predecessor_blocked_reason(plan, tasks[3])
        tasks[2].status = "CANCELLED"
        assert service._hybrid_predecessor_blocked_reason(plan, tasks[3])


def test_runtime_semantic_gate_without_concrete_edges_and_reschedule_filter(session_factory):
    with session_factory() as session:
        _, plan, activation, items, tasks = graph_fixture(session)
        tasks[0].status = "COMPLETED"
        service = ExecutionService(session)
        assert service._hybrid_predecessor_blocked_reason(plan, tasks[1]) is None
        assert service._hybrid_predecessor_blocked_reason(plan, tasks[3])
        blockers, state = service._work_item_dependency_preview(
            plan, activation, items[0], date(2026, 9, 22), date(2026, 9, 20), subject_task=tasks[0]
        )
        assert not blockers
        assert tasks[3].id not in str(state)
        blockers, _ = service._work_item_dependency_preview(
            plan, activation, items[0], date(2026, 9, 23), date(2026, 9, 20), subject_task=tasks[0]
        )
        assert any("后续" in value for value in blockers)


def test_multiple_predecessors_are_merged_and_explicit_edges_preserved(session_factory):
    with session_factory() as session:
        _, plan, _, items, tasks = graph_fixture(session)
        session.add(
            WorkItemDependency(
                plan_version_id=plan.id,
                predecessor_work_item_id=items[2].id,
                successor_work_item_id=items[1].id,
            )
        )
        final = PlanTask(
            plan_version_id=plan.id,
            plan_day_id=tasks[0].plan_day_id,
            source_work_item_id=items[2].id,
            order_in_day=2,
            title="独立准备",
            description="准备成果",
            estimate_minutes=10,
            priority=1,
            task_kind="PREPARE",
        )
        session.add(final)
        session.flush()
        tasks[1].depends_on_json = [tasks[2].id]  # authored explicit constraint
        WorkItemDependencyGraph(session, plan.id).bind()
        assert set(tasks[1].depends_on_json) == {tasks[0].id, tasks[2].id, final.id}


def test_activation_retains_pair_identity_and_ignores_unrelated_later_instance(session_factory):
    with session_factory() as session:
        _, plan, activation, _, tasks = graph_fixture(session)
        request = ExecutionScheduleRequest(
            requested_start_date=date(2026, 9, 21), execution_weekdays=list(range(7))
        )
        WorkItemMaterializer(session).attach_authored(plan, activation, request)
        assert tasks[1].depends_on_json == [tasks[0].id]
        assert tasks[3].depends_on_json == [tasks[2].id]
        assert tasks[1].scheduling_policy_json["occurrence_key"] == "SERIES.1"


@pytest.mark.parametrize("minimum,maximum", [(3, None), (0, 0)])
def test_activation_enforces_lags_for_corresponding_instances(session_factory, minimum, maximum):
    with session_factory() as session:
        _, plan, activation, _, _ = graph_fixture(session)
        dependency = session.scalar(
            select(WorkItemDependency).where(WorkItemDependency.plan_version_id == plan.id)
        )
        dependency.minimum_lag_days = minimum
        dependency.maximum_lag_days = maximum
        with pytest.raises(DependencyBindingError, match="间隔"):
            WorkItemDependencyGraph(session, plan.id).validate_dates(session, activation.id)


def test_unbound_materialization_refuses_to_guess_instance_pairing(session_factory):
    with session_factory() as session:
        _, plan, _, _, _ = graph_fixture(session)
        with pytest.raises(HTTPException, match="实例标识"):
            WorkItemMaterializer(session)._resolve(
                plan,
                ExecutionScheduleRequest(
                    requested_start_date=date(2026, 9, 21), execution_weekdays=list(range(7))
                ),
            )


def test_carried_over_orphan_blocks_but_explicit_continuation_settles_it():
    source = SimpleNamespace(
        id="source",
        source_task_id=None,
        status="CARRIED_OVER",
        scheduling_policy_json={"occurrence_key": "SERIES.1"},
    )
    successor = SimpleNamespace(scheduling_policy_json={"occurrence_key": "SERIES.1"})
    relation = edge("MATCH_OCCURRENCE")
    assert select_predecessors(relation, successor, [source]) == [source]
    continuation = SimpleNamespace(
        id="copy",
        source_task_id="source",
        status="COMPLETED",
        scheduling_policy_json={"occurrence_key": "SERIES.1"},
    )
    assert select_predecessors(relation, successor, [source, continuation]) == [continuation]


def test_direct_reschedule_rejects_semantic_dependency_violation(session_factory):
    from planner_api.schemas import FlexibleTaskRescheduleRequest

    with session_factory() as session:
        goal, plan, _, _, tasks = graph_fixture(session)
        service = ExecutionService(session)
        service._today_for = lambda _goal: date(2026, 9, 20)
        session.commit()
        with pytest.raises(HTTPException, match="后续"):
            service.reschedule_flexible_task(
                goal.id,
                FlexibleTaskRescheduleRequest(
                    task_id=tasks[0].id, target_date=date(2026, 9, 23), reason="验证依赖改期"
                ),
            )
        session.rollback()
        assert session.get(PlanTask, tasks[0].id).plan_day_id != tasks[1].plan_day_id


def test_projection_clone_preserves_matching_and_object_identity(session_factory):
    from planner_api.models import PlanActivation, PlanMilestone, PlanVersion
    from planner_api.work_item_projection import clone_work_item_projection

    with session_factory() as session:
        goal, plan, activation, _, tasks = graph_fixture(session)
        revised = PlanVersion(
            goal_id=goal.id,
            route_version_id=plan.route_version_id,
            version=2,
            status="CONFIRMED",
            summary="调整后规划",
            total_weeks=2,
            weekly_execution_days=2,
            weekly_minutes=90,
            total_estimated_minutes=120,
            prompt_version="test",
            execution_model_version="HYBRID_WORK_ITEM",
        )
        session.add(revised)
        session.flush()
        source_day = session.get(PlanDay, tasks[0].plan_day_id)
        milestone = session.get(PlanMilestone, source_day.milestone_id)
        new_milestone = PlanMilestone(
            plan_version_id=revised.id,
            route_stage_id=milestone.route_stage_id,
            stage_order=1,
            title="调整阶段",
            purpose="继续执行",
            start_week=1,
            end_week=2,
        )
        session.add(new_milestone)
        session.flush()
        new_day = PlanDay(
            plan_version_id=revised.id,
            milestone_id=new_milestone.id,
            stage_order=1,
            week_index=2,
            day_index=1,
            primary_goal="继续",
            suggested_minutes=90,
        )
        session.add(new_day)
        next_activation = PlanActivation(
            goal_id=goal.id,
            plan_version_id=revised.id,
            requested_start_date=activation.requested_start_date,
            projected_end_date=activation.projected_end_date,
        )
        session.add(next_activation)
        session.flush()
        copied = PlanTask(
            plan_version_id=revised.id,
            plan_day_id=new_day.id,
            source_task_id=tasks[0].id,
            source_work_item_id=tasks[0].source_work_item_id,
            order_in_day=1,
            title=tasks[0].title,
            description=tasks[0].description,
            estimate_minutes=30,
            priority=1,
            task_kind="BUILD",
            scheduling_policy_json={},
        )
        session.add(copied)
        session.flush()
        clone_work_item_projection(session, plan, revised, activation, next_activation)
        relation = session.scalar(
            select(WorkItemDependency).where(WorkItemDependency.plan_version_id == revised.id)
        )
        assert relation.matching_mode == "MATCH_OCCURRENCE"
        assert copied.scheduling_policy_json["occurrence_key"] == "SERIES.1"


def test_migration_preserves_old_edges_as_all_and_downgrades(tmp_path):
    import importlib.util
    from pathlib import Path
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import create_engine

    path = Path(__file__).parents[1] / "migrations/versions/0067_dependency_matching.py"
    spec = importlib.util.spec_from_file_location("dependency_matching_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    engine = create_engine(f"sqlite:///{(tmp_path / 'migration.db').as_posix()}")
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TABLE work_item_dependencies (id VARCHAR(36) PRIMARY KEY, condition_json JSON)"
        )
        connection.exec_driver_sql("INSERT INTO work_item_dependencies VALUES ('old', '{}')")
        with Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()
            assert (
                connection.exec_driver_sql(
                    "SELECT matching_mode FROM work_item_dependencies WHERE id='old'"
                ).scalar()
                == "ALL"
            )
            migration.downgrade()
            assert (
                connection.exec_driver_sql("SELECT id FROM work_item_dependencies").scalar()
                == "old"
            )


def test_explicit_instance_trigger_only_waits_for_its_own_predecessor(session_factory):
    from planner_api.models import WorkItemWindow
    from planner_api.schemas import TriggeredTaskActivationApply, TriggeredTaskActivationRequest

    with session_factory() as session:
        goal, plan, activation, items, tasks = graph_fixture(session)
        items[1].binding_mode = "TRIGGERED"
        items[1].target_value = 1
        for task in (tasks[1], tasks[3]):
            session.delete(task)
        tasks[0].status = "COMPLETED"
        window = WorkItemWindow(
            activation_id=activation.id,
            work_item_id=items[1].id,
            period_key="trigger:pending",
            eligible_from=date(2026, 9, 21),
            eligible_to=date(2026, 10, 4),
            required_value=1,
            status="PENDING",
            resolution_json={
                "authored_tasks": [
                    {
                        "title": "对应检查",
                        "description": "检查本次成果",
                        "occurrence_key": "SERIES.1",
                        "dependency_period_key": "week:1",
                    }
                ]
            },
        )
        session.add(window)
        session.commit()
        service = ExecutionService(session)
        service._today_for = lambda _goal: date(2026, 9, 22)
        assert service.get_triggered_tasks(goal.id)[0].can_activate
        preview = service.preview_triggered_activation(
            goal.id,
            TriggeredTaskActivationRequest(
                window_id=window.id, target_date=date(2026, 9, 22), evidence="已确认完成"
            ),
        )
        assert preview.can_apply, preview
        service.apply_triggered_activation(
            goal.id, TriggeredTaskActivationApply(preview_id=preview.id)
        )
        created = session.scalar(
            select(PlanTask).where(PlanTask.source_work_item_id == items[1].id)
        )
        assert created.depends_on_json == [tasks[0].id]
        assert created.scheduling_policy_json["occurrence_key"] == "SERIES.1"


def test_actual_completion_date_controls_waiting_interval(session_factory):
    from datetime import UTC, datetime
    from planner_api.models import ProgressEvent

    with session_factory() as session:
        goal, plan, activation, _, tasks = graph_fixture(session)
        relation = session.scalar(
            select(WorkItemDependency).where(WorkItemDependency.plan_version_id == plan.id)
        )
        relation.minimum_lag_days = 2
        tasks[0].status = "COMPLETED"
        session.add(
            ProgressEvent(
                goal_id=goal.id,
                plan_version_id=plan.id,
                plan_day_id=tasks[0].plan_day_id,
                task_id=tasks[0].id,
                event_type="COMPLETED",
                occurred_at=datetime(2026, 9, 25, 0, 0, tzinfo=UTC),
            )
        )
        session.flush()
        service = ExecutionService(session)
        service._today_for = lambda _goal: date(2026, 9, 26)
        assert "等待" in service._hybrid_predecessor_blocked_reason(plan, tasks[1])
        service._today_for = lambda _goal: date(2026, 9, 27)
        assert service._hybrid_predecessor_blocked_reason(plan, tasks[1]) is None


def test_large_group_barrier_can_be_serialized_and_revalidated(session_factory):
    from planner_api.schemas import PlanDayDraft
    from planner_api.plan_ai import FakePlanProvider
    from planner_api.plan_discussion_ai import FakePlanDiscussionProvider

    with session_factory() as session:
        _, plan, _, items, tasks = graph_fixture(session, mode="ALL")
        session.get(PlanDay, tasks[0].plan_day_id).suggested_minutes = 60
        for index in range(6):
            session.add(
                PlanTask(
                    plan_version_id=plan.id,
                    plan_day_id=tasks[0].plan_day_id,
                    source_work_item_id=items[0].id,
                    order_in_day=index + 2,
                    title=f"独立前置{index}",
                    description="需要全部完成的前置成果",
                    estimate_minutes=5,
                    priority=1,
                    task_kind="BUILD",
                )
            )
        session.flush()
        WorkItemDependencyGraph(session, plan.id).bind()
        assert len(tasks[1].depends_on_json) == 8
        payload = PlanService(
            session, FakePlanProvider(), FakePlanDiscussionProvider()
        )._plan_payload(plan)
        for raw_day in payload["days"]:
            PlanDayDraft.model_validate(raw_day)


def test_group_lag_starts_when_last_prerequisite_finishes(session_factory):
    with session_factory() as session:
        goal, plan, activation, items, tasks = graph_fixture(session, mode="ALL")
        relation = session.scalar(
            select(WorkItemDependency).where(WorkItemDependency.plan_version_id == plan.id)
        )
        relation.maximum_lag_days = 2
        tasks[0].status = tasks[2].status = "COMPLETED"
        graph = WorkItemDependencyGraph(session, plan.id)
        predecessors = graph.predecessors(relation, tasks[3])
        service = ExecutionService(session)
        # First prerequisite was on Sep 21, the second on Sep 28. The group
        # finished Sep 28, so review on Sep 29 satisfies the two-day maximum.
        assert (
            graph.execution_lag_error(
                session,
                relation,
                predecessors,
                activation,
                date(2026, 9, 29),
                lambda value: service._local_date(value, goal),
            )
            is None
        )
        assert graph.execution_lag_error(
            session,
            relation,
            predecessors,
            activation,
            date(2026, 10, 1),
            lambda value: service._local_date(value, goal),
        )
        blockers, _ = service._work_item_dependency_preview(
            plan, activation, items[1], date(2026, 9, 29), date(2026, 9, 20), subject_task=tasks[3]
        )
        assert not blockers


def test_long_interleaved_chains_keep_each_object_independent():
    # Repeated pipelines must not turn into stage-wide barriers as they grow.
    stages = [f"STEP{index}" for index in range(8)]
    dependencies = [
        {"predecessor_key": before, "successor_key": after, "matching_mode": "MATCH_OCCURRENCE"}
        for before, after in zip(stages, stages[1:], strict=False)
    ]
    days = [
        day(
            index + 1,
            *(
                action(stage, f"OBJECT.{index}", order=order + 1)
                for order, stage in enumerate(stages)
            ),
        )
        for index in range(20)
    ]
    validate_authored_dependency_order(days, dependencies)
    # Completing an unrelated object's pipeline never repairs a reversed link.
    days[10].tasks[3].order, days[10].tasks[4].order = 5, 4
    with pytest.raises(WorkItemValidationError):
        validate_authored_dependency_order(days, dependencies)


def test_paired_chains_can_feed_a_global_final_gate(session_factory):
    with session_factory() as session:
        _, plan, _, items, tasks = graph_fixture(session)
        session.add(
            WorkItemDependency(
                plan_version_id=plan.id,
                predecessor_work_item_id=items[1].id,
                successor_work_item_id=items[2].id,
                matching_mode="ALL",
            )
        )
        final = PlanTask(
            plan_version_id=plan.id,
            plan_day_id=tasks[3].plan_day_id,
            source_work_item_id=items[2].id,
            order_in_day=2,
            title="全部方案审阅后整体验收",
            description="核对全部审阅成果",
            estimate_minutes=10,
            priority=1,
            task_kind="REVIEW",
        )
        session.add(final)
        session.flush()
        WorkItemDependencyGraph(session, plan.id).bind()
        assert set(final.depends_on_json) == {tasks[1].id, tasks[3].id}
        service = ExecutionService(session)
        tasks[0].status = "COMPLETED"
        assert service._hybrid_predecessor_blocked_reason(plan, tasks[1]) is None
        tasks[1].status = "COMPLETED"
        assert service._hybrid_predecessor_blocked_reason(plan, final)
        assert service._hybrid_predecessor_blocked_reason(plan, tasks[3])
        tasks[2].status = tasks[3].status = "COMPLETED"
        assert service._hybrid_predecessor_blocked_reason(plan, final) is None
