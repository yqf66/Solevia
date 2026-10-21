from sqlalchemy import select

from planner_api.dependency_resolver import PlanDependencyResolver
from planner_api.models import GenerationBatch, PlanDay, PlanTask, ProgressEvent
from test_recovery_scope import scoped_recovery


def test_legacy_move_resolves_authored_identity_and_rejects_ambiguous_matches(client, session_factory):
    goal, plan, (first, second, _) = scoped_recovery(client, session_factory)
    with session_factory() as session:
        source = session.get(PlanTask, first)
        original_day = session.get(PlanDay, source.plan_day_id)
        coordinate = (original_day.stage_order, original_day.week_index, original_day.day_index)
        reference = f"M{coordinate[0]}-W{coordinate[1]}-D{coordinate[2]}-T{source.order_in_day}"
        dependent = session.get(PlanTask, second)
        dependent.depends_on_json = [reference]
        batch = session.scalar(select(GenerationBatch).where(GenerationBatch.plan_version_id == plan, GenerationBatch.scope_type == "WEEK"))
        batch.staged_result_json = {"days": [{"stage_order": coordinate[0], "week_index": coordinate[1], "day_index": coordinate[2],
            "tasks": [{"order": source.order_in_day, "title": source.title, "description": source.description, "evidence": source.evidence_json}]}]}
        destination = session.scalar(select(PlanDay).where(PlanDay.plan_version_id == plan, PlanDay.id != original_day.id))
        source.plan_day_id = destination.id
        source.order_in_day = 90
        source.status = "COMPLETED"
        session.add(ProgressEvent(goal_id=goal, plan_version_id=plan, plan_day_id=destination.id,
            task_id=source.id, event_type="CARRY_FORWARD", metadata_json={"source_day_id": original_day.id, "partial": False}))
        session.flush()
        resolved = PlanDependencyResolver(session).resolve(reference, dependent)
        assert resolved.id == source.id
        assert resolved.status == "COMPLETED"
        # A matching title alone, without original identity, is insufficient.
        source.description = "edited identity"
        session.flush()
        assert PlanDependencyResolver(session).resolve(reference, dependent) is None


def test_extension_question_never_hides_dependency_blockers():
    from planner_api.plan_service import PlanService
    proposal = PlanService._local_recovery_proposal({
        "backlog_tasks": [{"task_id": "blocked", "title": "阅读任务"}],
        "unassigned_task_ids": ["blocked"], "dependency_blocked_task_ids": ["blocked"],
        "requires_direct_extension": True, "slight_extension_possible": True,
        "direct_extension_execution_days": 1, "direct_extension_dates": ["2026-11-04"],
    }, clarifying_question="是否允许顺延？")
    assert "前置依赖" in proposal.clarifying_question
    assert "2026-11-04" in proposal.clarifying_question
    assert not proposal.can_apply
