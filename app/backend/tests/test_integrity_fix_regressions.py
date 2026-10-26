from datetime import date
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from planner_api.execution_discussion_service import ExecutionDiscussionService
from planner_api.execution_service import ExecutionService
from planner_api.models import ExecutionInsight, Message, PlanTask, PlanWorkItem, WorkItemDependency
from planner_api.plan_ai import normalize_hybrid_contract
from planner_api.plan_discussion_ai import FakePlanDiscussionProvider
from planner_api.schemas import ExecutionDiscussionResult, ExecutionMemoryCandidate, ExecutionScheduleRequest
from planner_api.work_item_materialization import WorkItemMaterializer


def test_memory_correction_reference_survives_changed_key_and_preserves_unrelated_memory(client, session_factory):
    from test_execution_discussion import _started_goal

    goal_id = _started_goal(client)
    with session_factory() as session:
        service = ExecutionDiscussionService(session, FakePlanDiscussionProvider())
        first = service.discuss(goal_id, "我发现自己更喜欢先实践")
        previous = first.insights[0]
        goal, plan = service._goal_and_plan(goal_id)
        message = session.scalar(select(Message).where(Message.role == "USER"))
        message.content = "更正：最多两条理由"
        candidate = ExecutionMemoryCandidate(insight_type="PREFERENCE", content="最多两条理由",
            memory_key="a_new_model_key", supersedes_id=previous.id, confidence=1,
            source_type="EXPLICIT_USER", evidence_span=message.content,
            confirmation_policy="AUTO_CONFIRM_EXPLICIT")
        unrelated = ExecutionInsight(goal_id=goal_id, plan_version_id=plan.id,
            source_message_id=message.id, insight_type="PREFERENCE", content="只在晚间学习",
            scope="GOAL", confidence=1, memory_key="learning_time", status="CONFIRMED")
        session.add(unrelated)
        service._persist_insights(goal, plan, message, ExecutionDiscussionResult(
            assistant_message="已更正", memory_candidates=[candidate]))
        session.flush()
        records = list(session.scalars(select(ExecutionInsight).where(ExecutionInsight.goal_id == goal_id)))
        assert session.get(ExecutionInsight, previous.id).status == "SUPERSEDED"
        replacement = next(item for item in records if item.content == "最多两条理由")
        assert replacement.status == "CONFIRMED" and replacement.memory_key == previous.memory_key
        assert unrelated.status == "CONFIRMED"
        assert replacement.supersedes_id == previous.id


def test_pending_inference_cannot_supersede_confirmed_memory(client, session_factory):
    from test_execution_discussion import _started_goal

    goal_id = _started_goal(client)
    with session_factory() as session:
        service = ExecutionDiscussionService(session, FakePlanDiscussionProvider())
        previous = service.discuss(goal_id, "我发现自己更喜欢先实践").insights[0]
        goal, plan = service._goal_and_plan(goal_id)
        message = session.scalar(select(Message).where(Message.role == "USER"))
        service._persist_insights(goal, plan, message, ExecutionDiscussionResult(
            assistant_message="待确认", memory_candidates=[ExecutionMemoryCandidate(
                insight_type="PREFERENCE", content="也许喜欢理论", memory_key="changed_key",
                supersedes_id=previous.id, confidence=.7)]))
        session.flush()
        assert session.get(ExecutionInsight, previous.id).status == "CONFIRMED"
        pending = session.scalar(select(ExecutionInsight).where(
            ExecutionInsight.goal_id == goal_id, ExecutionInsight.status == "PROPOSED"))
        session.get(ExecutionInsight, previous.id).status = "SUPERSEDED"
        session.flush()
        with pytest.raises(HTTPException, match="已有更新"):
            service.decide_insight(goal_id, pending.id, "CONFIRM")
        assert pending.status == "PROPOSED"


def test_today_dependency_read_and_write_share_one_gate(session_factory):
    from test_work_item_foundation import _plan_graph

    with session_factory() as session:
        goal, plan, milestone, day, activation = _plan_graph(session, execution_model_version="HYBRID_WORK_ITEM")
        items = []
        for key in ["PREPARE", "PRACTICE"]:
            item = PlanWorkItem(plan_version_id=plan.id, item_key=key, scope_type="PLAN",
                title=key, description=key, estimate_minutes=10, priority=2, task_kind="PRACTICE",
                binding_mode="WINDOW", fulfillment_mode="ONCE")
            session.add(item)
            items.append(item)
        session.flush()
        session.add(WorkItemDependency(plan_version_id=plan.id,
            predecessor_work_item_id=items[0].id, successor_work_item_id=items[1].id))
        tasks = []
        for index, item in enumerate(items, 1):
            task = PlanTask(plan_version_id=plan.id, plan_day_id=day.id,
                source_work_item_id=item.id, order_in_day=index, title=item.title,
                description=item.description, estimate_minutes=10, priority=2, task_kind="PRACTICE")
            session.add(task)
            tasks.append(task)
        session.flush()
        execution = ExecutionService(session)
        blocked = execution._day_read(day, tasks).tasks[1].blocked_reason
        assert blocked and "PREPARE" in blocked
        with pytest.raises(HTTPException) as error:
            execution._assert_hybrid_predecessors_completed(plan, tasks[1])
        assert error.value.detail == blocked
        tasks[0].status = "COMPLETED"
        session.flush()
        assert execution._day_read(day, tasks).tasks[1].blocked_reason is None
        execution._assert_hybrid_predecessors_completed(plan, tasks[1])


def test_group_alias_repairs_spelling_but_not_ambiguous_or_different_group():
    import copy

    original = {"work_item_groups": [{"group_key": "THEME.STILL_LIFE", "candidate_item_keys": ["PHOTO"]}],
        "work_items": [{"item_key": "PHOTO", "constraint_set": {"binding_mode": "ON_DEMAND",
            "fulfillment_mode": "SELECT_K", "selection": {"group_key": "THEME_STILL_LIFE"}}}]}
    raw = copy.deepcopy(original)
    normalize_hybrid_contract(raw)
    assert raw["work_items"][0]["constraint_set"]["selection"]["group_key"] == "THEME.STILL_LIFE"
    ambiguous = copy.deepcopy(original)
    ambiguous["work_item_groups"].append({"group_key": "THEME-STILL-LIFE", "candidate_item_keys": []})
    normalize_hybrid_contract(ambiguous)
    assert ambiguous["work_items"][0]["constraint_set"]["selection"]["group_key"] == "THEME_STILL_LIFE"


@pytest.mark.parametrize("fulfillment", ["ONCE", "EVIDENCE", "DURATION"])
def test_candidate_group_gate_survives_its_independent_completion_mode(fulfillment):
    raw = {"work_item_groups": [{"group_key": "THEME", "candidate_item_keys": ["PHOTO"]}],
        "work_items": [{"item_key": "PHOTO", "constraint_set": {"binding_mode": "WINDOW",
            "fulfillment_mode": fulfillment, "selection": {"mode": "SELECT_K", "group_key": "THEME"}}}]}
    normalize_hybrid_contract(raw)
    constraints = raw["work_items"][0]["constraint_set"]
    assert constraints["selection"] == {"mode": "SELECT_K", "group_key": "THEME"}
    assert constraints["fulfillment_mode"] == fulfillment


def test_relative_deadline_is_an_upper_bound_not_an_execution_date():
    plan = SimpleNamespace(total_weeks=2)
    payload = ExecutionScheduleRequest(requested_start_date=date(2026, 10, 19), execution_weekdays=[0, 2, 4])
    temporal = {"start_week": 2, "end_week": 2, "deadline_relative_day_ref": "W2-D6"}
    assert WorkItemMaterializer._date_range(plan, payload, temporal) == (date(2026, 10, 26), date(2026, 10, 31))
    temporal["relative_day_ref"] = "W2-D3"
    assert WorkItemMaterializer._date_range(plan, payload, temporal) == (date(2026, 10, 28), date(2026, 10, 28))


def test_explicit_candidate_membership_fills_only_missing_reverse_reference():
    import copy

    raw = {"work_items": [{"item_key": "TOPIC_STILL", "constraint_set": {}}],
           "work_item_groups": [{"group_key": "TOPIC_CHOICE", "candidate_item_keys": ["TOPIC_STILL"]}]}
    original = copy.deepcopy(raw)
    normalize_hybrid_contract(raw)
    assert raw["work_items"][0]["constraint_set"]["selection"]["group_key"] == "TOPIC_CHOICE"
    ambiguous = copy.deepcopy(original)
    ambiguous["work_item_groups"].append({"group_key": "OTHER_CHOICE", "candidate_item_keys": ["TOPIC_STILL"]})
    normalize_hybrid_contract(ambiguous)
    assert not ambiguous["work_items"][0]["constraint_set"].get("selection", {}).get("group_key")


def test_outline_capacity_conflicts_enter_bounded_repair_without_expanding_supply():
    from planner_api.plan_service import PlanService
    from planner_api.work_item_validation import WorkItemValidationError

    def json_item(value):
        return SimpleNamespace(model_dump=lambda **kwargs: value)
    main = {"label": "总结", "cadence": "TOTAL", "start_week": 1, "end_week": 2,
            "workload_key": "SUMMARY", "minimum_total_minutes": 120, "minimum_occurrences": 1}
    extra = {**main, "label": "卡片", "workload_key": "CARDS", "minimum_total_minutes": 10}
    result = SimpleNamespace(total_weeks=2, weekly_execution_days=1,
        temporal_segments=[json_item({"start_week": 1, "end_week": 2,
            "available_days_per_week": 2, "suggested_daily_minutes": 30})],
        commitments=[json_item(main), json_item(extra)])
    with pytest.raises(WorkItemValidationError, match="130"):
        PlanService._retry_outline_capacity_conflict(result, {}, 0)
    assert result.temporal_segments[0].model_dump()["suggested_daily_minutes"] == 30
    PlanService._retry_outline_capacity_conflict(result, {}, 2)  # Existing final capacity gate decides.
    main["minimum_total_minutes"] = 110
    PlanService._retry_outline_capacity_conflict(result, {}, 0)


def test_selection_action_uses_declared_pool_without_becoming_an_alternative():
    from planner_api.work_item_domain import PlanWorkItemDraft, PlanWorkItemGroupDraft
    from planner_api.work_item_validation import validate_work_item_batch, WorkItemValidationError

    def item(key, description):
        return PlanWorkItemDraft(item_key=key, scope_type="PLAN", title=key, description=description,
            estimate_minutes_per_occurrence=10, priority=1, task_kind="PRACTICE",
            constraint_set={"binding_mode": "WINDOW", "fulfillment_mode": "ONCE"}, source_refs=["user"])
    action = item("FIRST_SESSION", "先从 TOPIC_CHOICE 中选1个，然后练习曝光")
    candidate = item("TOPIC_STILL", "静物摄影")
    candidate.constraint_set.selection.group_key = "TOPIC_CHOICE"
    pool = PlanWorkItemGroupDraft(group_key="TOPIC_CHOICE", title="题材", scope_type="PLAN",
        minimum_selected=1, maximum_selected=1, candidate_item_keys=["TOPIC_STILL"], source_refs=["user"])
    kwargs = dict(work_items=[action, candidate], groups=[pool], dependencies=[], milestone_weeks={}, commitment_labels=set())
    validate_work_item_batch(**kwargs)
    assert pool.candidate_item_keys == ["TOPIC_STILL"] and action.constraint_set.selection.group_key is None
    validate_work_item_batch(work_items=[action], groups=[], dependencies=[], milestone_weeks={},
        commitment_labels=set(), existing_item_keys=["TOPIC_STILL"], existing_group_keys=["TOPIC_CHOICE"],
        existing_group_candidates={"TOPIC_CHOICE": {"TOPIC_STILL"}})
    action.description = "从另外三种题材中选1个，然后练习曝光"
    with pytest.raises(WorkItemValidationError, match="缺少候选组"):
        validate_work_item_batch(**kwargs)


def test_hard_workload_requires_duration_evidence_not_just_deliverable_count():
    from planner_api.plan_service import PlanService
    from planner_api.work_item_validation import WorkItemValidationError

    promise = SimpleNamespace(label="完成3张照片", source_text="第一阶段交付3张照片和每张两句说明",
        cadence="TOTAL", minimum_total_minutes=180, minimum_minutes_per_occurrence=180)
    result = SimpleNamespace(commitments=[promise])
    with pytest.raises(WorkItemValidationError) as rejected:
        PlanService._validate_executable_commitments(result)
    assert rejected.value.code == "PLAN_COMMITMENT_MINUTES_UNSUPPORTED"
    assert promise.minimum_total_minutes == 180  # validation cannot quietly remove obligations
    for evidence in ["完成作品至少投入180分钟", "完成作品至少投入三小时", "Spend 3 hours on the photo set"]:
        promise.source_text = evidence
        PlanService._validate_executable_commitments(result)
    promise.minimum_total_minutes = promise.minimum_minutes_per_occurrence = None
    promise.source_text = "第一阶段交付3张照片和每张两句说明"
    PlanService._validate_executable_commitments(result)


def test_process_claim_uses_birth_identity_not_pid_alone():
    from planner_api.analysis_claim import claim_owner, owner_is_gone

    owner = claim_owner()
    assert owner["birth"]
    assert not owner_is_gone(owner)
    assert owner_is_gone({**owner, "birth": "a different process"})
    assert not owner_is_gone({"pid": owner["pid"]})


def test_global_dependency_cannot_gate_earlier_occurrences():
    from planner_api.work_item_validation import validate_authored_dependency_order, WorkItemValidationError

    def day(week, key):
        return SimpleNamespace(week_index=week, day_index=1, tasks=[SimpleNamespace(
            order=1, work_item_keys=[key], is_splittable=True, stage_contributions=[])])
    dependency = {"predecessor_key": "MOCK", "successor_key": "READING"}
    with pytest.raises(WorkItemValidationError, match="阻断早期练习"):
        validate_authored_dependency_order([day(1, "READING"), day(2, "MOCK"), day(3, "READING")], [dependency])
    validate_authored_dependency_order([day(1, "MOCK"), day(2, "READING")], [dependency])
    combined = day(1, "MOCK")
    combined.tasks[0].work_item_keys.append("READING")
    with pytest.raises(WorkItemValidationError):
        validate_authored_dependency_order([combined], [dependency])


def test_new_analysis_recovers_exhausted_older_turn_only_after_success(session_factory):
    from planner_api.ai import FakeClarificationProvider, ProviderUnavailableError
    from planner_api.models import ClarificationAnalysisJob, Conversation, Goal, utc_now
    from planner_api.schemas import RequirementAnalysis
    from planner_api.service import ClarificationService

    class Provider(FakeClarificationProvider):
        fail = True
        seen = None

        def analyze(self, context, assistant_message):
            self.seen = context
            if self.fail:
                raise ProviderUnavailableError("暂时超时", code="MODEL_TIMEOUT")
            return RequirementAnalysis(summary="已处理全部条件", readiness={
                "ready_for_review": False, "blocking_fields": ["desired_outcomes"], "reason": "继续讨论"})

    with session_factory() as session:
        goal = Goal(title="队列恢复", raw_input="队列恢复")
        session.add(goal)
        session.flush()
        conversation = Conversation(goal_id=goal.id, phase="CLARIFICATION")
        session.add(conversation)
        session.flush()
        messages = [Message(conversation_id=conversation.id, sequence=i, role="USER" if i % 2 else "ASSISTANT",
            content="最早一轮的重要条件" if i == 1 else f"对话{i}") for i in range(1, 21)]
        session.add_all(messages)
        session.flush()
        old = ClarificationAnalysisJob(goal_id=goal.id, conversation_id=conversation.id,
            user_message_id=messages[0].id, assistant_message_id=messages[1].id,
            status="FAILED", attempts=4, max_attempts=4)
        new = ClarificationAnalysisJob(goal_id=goal.id, conversation_id=conversation.id,
            user_message_id=messages[-2].id, assistant_message_id=messages[-1].id, status="PENDING")
        session.add_all([old, new])
        session.commit()
        provider = Provider()
        service = ClarificationService(session, provider)
        assert service.process_pending_analysis()[0]
        assert old.status == "FAILED"
        assert any(item["content"] == "最早一轮的重要条件" for item in provider.seen.recent_messages)
        provider.fail = False
        new.next_retry_at = utc_now()
        session.commit()
        assert service.process_pending_analysis()[0]
        assert old.status == new.status == "SUCCEEDED"
        assert messages[1].metadata_json["recovered_by_analysis_job_id"] == new.id
