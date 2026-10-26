from datetime import timedelta

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from planner_api.api import get_plan_service
from planner_api.models import Goal, PlanActivation, PlanDaySchedule, PlanTask, PlanVersion
from planner_api.plan_service import PlanService
from test_execution_discussion import _started_goal


def test_old_saved_recovery_preview_compares_duplicate_titles_by_source_ids(client, session_factory):
    goal_id = _started_goal(client)
    with session_factory() as session:
        service = get_plan_service(session)
        source = service._current_plan(goal_id)
        activation = session.scalar(select(PlanActivation).where(PlanActivation.plan_version_id == source.id))
        days = list(session.scalars(select(PlanDaySchedule).where(PlanDaySchedule.activation_id == activation.id)
            .order_by(PlanDaySchedule.scheduled_date)))
        today = service._today_for_goal_id(goal_id)
        tasks = []
        for index, day in enumerate(days[:3]):
            day.scheduled_date = today + timedelta(days=index - 2)
            task = session.scalar(select(PlanTask).where(PlanTask.plan_day_id == day.plan_day_id))
            task.title = '重复的每日阅读'; task.estimate_minutes = 20; task.status = 'NOT_STARTED'
            tasks.append(task)
        session.flush()
        target = str(today)
        context = {'today': target, 'original_end_date': str(activation.projected_end_date),
            'proposed_assignments': [{'backlog_task_id': task.id, 'target_date': target} for task in tasks[:2]]}
        rows = [{'title': '补回：重复的每日阅读', 'date': target, 'minutes': 20, 'start': None, 'end': None, 'state': 'FLEXIBLE'} for _ in range(2)]
        plan_count = len(list(session.scalars(select(PlanVersion.id).where(PlanVersion.goal_id == goal_id))))
        original = [(task.id, task.status, task.plan_day_id) for task in tasks]
        result = service._adjustment_comparison(source, rows, context)
        assert [row['source_task_id'] for row in result['after']] == [task.id for task in tasks[:2]]
        assert [row['source_date'] for row in result['after']] == [str(today - timedelta(days=2)), str(today - timedelta(days=1))]
        assert all(row['change_kind'] == 'RECOVERED' for row in result['after'])
        assert original == [(task.id, task.status, task.plan_day_id) for task in tasks]
        assert plan_count == len(list(session.scalars(select(PlanVersion.id).where(PlanVersion.goal_id == goal_id))))


def context():
    return {'today': '2035-09-10', 'original_end_date': '2035-09-13', 'backlog_tasks': [
        {'task_id': str(i), 'title': '阅读', 'estimate_minutes': 20, 'source_date': f'2035-09-0{i+7}', 'dependency_constraints': []}
        for i in range(3)], 'candidate_days': [
        {'date': f'2035-09-{day}', 'plan_day_id': str(day), 'week_index': 1, 'day_index': day - 9, 'stage_order': 1, 'existing_minutes': 20}
        for day in range(10, 14)]}


def test_extra_time_offer_spreads_backlog_with_a_hard_daily_budget():
    result = PlanService._with_recovery_availability_offer(context(), {'from_date': '2035-09-10', 'extra_minutes_per_execution_day': 20})
    assert len(result['proposed_assignments']) == 3
    assert len({row['target_date'] for row in result['proposed_assignments']}) == 3
    assert result['projected_peak_daily_minutes'] == 40
    assert not result['unassigned_task_ids']
    assert not result['requires_direct_extension']


def test_insufficient_extra_time_keeps_tasks_pending_and_blocks_publication():
    result = PlanService._with_recovery_availability_offer(context(), {'from_date': '2035-09-13', 'extra_minutes_per_execution_day': 20})
    assert len(result['proposed_assignments']) == 1
    assert len(result['unassigned_task_ids']) == 2
    proposal = PlanService._local_recovery_proposal(result)
    assert not proposal.can_apply
    assert proposal.needs_clarification
    assert result['projected_peak_daily_minutes'] == 40


def test_extra_time_cannot_move_a_dependency_before_its_prerequisite():
    data = context()
    data['backlog_tasks'][1]['dependency_constraints'] = [{'task_id': 'outside-selection', 'status': 'NOT_STARTED'}]
    result = PlanService._with_recovery_availability_offer(data, {'from_date': '2035-09-10', 'extra_minutes_per_execution_day': 20})
    assert '1' in result['unassigned_task_ids']


def test_spreading_respects_saved_extra_time_and_daily_total_limits():
    offered = PlanService._with_recovery_availability_offer(context(), {'from_date': '2035-09-10', 'extra_minutes_per_execution_day': 20})
    spread = PlanService._with_recovery_preview_options(offered, {'action': 'SPREAD'})
    assert len(spread['proposed_assignments']) == 3
    assert spread['projected_peak_daily_minutes'] == 40
    limited = PlanService._with_recovery_preview_options(spread, {'action': 'DAILY_LIMIT', 'daily_limit_minutes': 30})
    assert limited['unassigned_task_ids'] == ['0', '1', '2']
    assert not PlanService._local_recovery_proposal(limited).can_apply
    assert len(limited['backlog_tasks']) == 3


def test_moving_a_task_preserves_its_identity_and_refuses_overload_or_reordering():
    offered = PlanService._with_recovery_availability_offer(context(), {'from_date': '2035-09-10', 'extra_minutes_per_execution_day': 20})
    moved = PlanService._with_recovery_preview_options(offered, {'action': 'MOVE_TASK', 'source_task_id': '2', 'target_date': '2035-09-13'})
    assert moved['proposed_assignments'][-1]['backlog_task_id'] == '2'
    assert moved['proposed_assignments'][-1]['target_date'] == '2035-09-13'
    assert offered['proposed_assignments'][-1]['target_date'] == '2035-09-12'
    for task_id, target in [('2', '2035-09-11'), ('0', '2035-09-13'), ('2', '2035-09-14'), ('unknown', '2035-09-13')]:
        with pytest.raises(HTTPException):
            PlanService._with_recovery_preview_options(offered, {'action': 'MOVE_TASK', 'source_task_id': task_id, 'target_date': target})


def test_saved_budgets_are_revalidated_after_allocation_and_actual_scheduling():
    from copy import deepcopy
    offered = PlanService._with_recovery_availability_offer(context(), {'from_date': '2035-09-10', 'extra_minutes_per_execution_day': 20})
    PlanService._assert_recovery_budget(offered)
    invalid = deepcopy(offered)
    invalid['proposed_assignments'][1]['target_date'] = '2035-09-10'
    with pytest.raises(HTTPException, match='409'):
        PlanService._assert_recovery_budget(invalid)
    with pytest.raises(HTTPException, match='409'):
        PlanService._assert_recovery_budget(offered, [{'date': '2035-09-10', 'minutes': 60}])


def test_availability_and_date_choices_survive_preview_reentry_and_idempotent_publish(client, session_factory):
    from sqlalchemy import func
    from test_recovery_scope import scoped_recovery, preview_selection
    goal, plan, (selected, deferred, _) = scoped_recovery(client, session_factory)
    scope = preview_selection(client, goal, plan, [selected]).json()
    with session_factory() as session:
        activation = session.scalar(select(PlanActivation).where(PlanActivation.plan_version_id == plan))
        today = get_plan_service(session)._today_for_goal_id(goal)
        end_date = str(activation.projected_end_date)
    response = client.post(f'/api/goals/{goal}/execution/recovery/ai', json={
        'missed_dates': scope['missed_dates'], 'backlog_minutes': 10, 'original_end_date': end_date,
        'preview_id': scope['id'], 'instruction': '每天额外十分钟，先预览',
        'availability_offer': {'from_date': str(today), 'extra_minutes_per_execution_day': 10},
    })
    assert response.status_code == 200, response.text
    assert response.json()['pending_adjustment']['can_apply']
    changed = client.post(f'/api/goals/{goal}/plan/revise', json={
        'content': '把这一项补回任务安排到最后一个执行日，保留其余安排',
        'recovery_options': {'action': 'MOVE_TASK', 'source_task_id': selected, 'target_date': end_date},
    })
    assert changed.status_code == 200, changed.text
    reviewed = client.post(f'/api/goals/{goal}/plan/adjustment/preview')
    assert reviewed.status_code == 200, reviewed.text
    comparison = reviewed.json()['comparison']
    row = next(row for row in comparison['after'] if row['source_task_id'] == selected)
    assert row['date'] == end_date
    assert row['source_date'] == str(today - timedelta(days=1))
    assert row['change_kind'] == 'RECOVERED'
    reentered = client.get(f'/api/goals/{goal}/plan').json()
    assert reentered['pending_adjustment_preview']['id'] == reviewed.json()['id']
    assert reentered['pending_adjustment_preview']['comparison'] == comparison
    with session_factory() as session:
        assert session.get(Goal, goal).active_plan_version_id == plan
        assert session.scalar(select(func.count()).select_from(PlanVersion)) == 1
        assert get_plan_service(session)._active_recovery_context(goal)['availability_offer']['extra_minutes_per_execution_day'] == 10
    applied = client.post(f'/api/goals/{goal}/plan/adjustment/apply', json={'preview_id': reviewed.json()['id'], 'accept_unallocated': True})
    assert applied.status_code == 200, applied.text
    final_plan = applied.json()['plan']['id']
    assert final_plan != plan
    repeated = client.post(f'/api/goals/{goal}/plan/adjustment/apply', json={'preview_id': reviewed.json()['id'], 'accept_unallocated': True})
    assert repeated.status_code == 200, repeated.text
    assert repeated.json()['plan']['id'] == final_plan
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(PlanVersion)) == 2
        retained = session.scalar(select(PlanTask).where(PlanTask.plan_version_id == final_plan, PlanTask.lineage_task_id == deferred))
        assert retained.status == 'IN_PROGRESS'


def test_daily_limit_that_cannot_fit_does_not_publish_or_drop_tasks(client, session_factory):
    from test_recovery_scope import scoped_recovery, preview_selection
    goal, plan, (selected, _, _) = scoped_recovery(client, session_factory)
    scope = preview_selection(client, goal, plan, [selected]).json()
    response = client.post(f'/api/goals/{goal}/execution/recovery/discussion', json={'preview_id': scope['id']})
    assert response.status_code == 200, response.text
    response = client.post(f'/api/goals/{goal}/plan/revise', json={
        'content': '每天总用时最多5分钟，放不下请说明',
        'recovery_options': {'action': 'DAILY_LIMIT', 'daily_limit_minutes': 5},
    })
    assert response.status_code == 200, response.text
    assert not response.json()['pending_adjustment']['can_apply']
    assert client.post(f'/api/goals/{goal}/plan/adjustment/preview').status_code == 409
    with session_factory() as session:
        context = get_plan_service(session)._active_recovery_context(goal)
        assert selected in context['unassigned_task_ids']
        assert session.get(Goal, goal).active_plan_version_id == plan
        assert session.get(PlanTask, selected).status == 'NOT_STARTED'


def test_same_summary_with_a_new_budget_cannot_reuse_or_publish_an_old_preview(client, session_factory):
    from planner_api.models import PlanningBrief
    from test_recovery_scope import scoped_recovery, preview_selection
    goal, plan, (selected, _, _) = scoped_recovery(client, session_factory)
    scope = preview_selection(client, goal, plan, [selected]).json()
    assert client.post(f'/api/goals/{goal}/execution/recovery/discussion', json={'preview_id': scope['id']}).status_code == 200
    old = client.post(f'/api/goals/{goal}/plan/adjustment/preview').json()
    original_proposal = client.get(f'/api/goals/{goal}/plan').json()['pending_adjustment']
    with session_factory() as session:
        brief = session.scalar(select(PlanningBrief).where(PlanningBrief.goal_id == goal, PlanningBrief.brief_kind == 'RECOVERY', PlanningBrief.status == 'DRAFT'))
        context = {**brief.brief_json['recovery_context'], 'daily_total_limit_minutes': 120}
        brief.brief_json = {**brief.brief_json, 'recovery_context': context}
        session.commit()
    restored = client.get(f'/api/goals/{goal}/plan').json()
    assert restored['pending_adjustment'] == original_proposal
    assert restored['pending_adjustment_preview'] is None
    rejected = client.post(f'/api/goals/{goal}/plan/adjustment/apply', json={'preview_id': old['id'], 'accept_unallocated': True})
    assert rejected.status_code == 409, rejected.text
    refreshed = client.post(f'/api/goals/{goal}/plan/adjustment/preview')
    assert refreshed.status_code == 200, refreshed.text
    assert refreshed.json()['id'] != old['id']
    assert refreshed.json()['schedule'] == old['schedule']


@pytest.mark.parametrize('offer', [
    {'from_date': '2035-09-09', 'extra_minutes_per_execution_day': 20},
    {'from_date': '2035-09-14', 'extra_minutes_per_execution_day': 20},
    {'from_date': '2035-09-10', 'extra_minutes_per_execution_day': 481},
])
def test_invalid_extra_time_is_rejected(offer):
    with pytest.raises(HTTPException):
        PlanService._with_recovery_availability_offer(context(), offer)


@pytest.mark.parametrize('title,date,start,end', [
    ('unknown', '2035-09-10', '09:00', '09:20'),
    ('阅读', '2035-09-11', '09:00', '09:20'),
    ('阅读', '2035-09-10', '09:00', '09:10'),
    ('阅读', '2035-09-10', '09:00', None),
])
def test_invalid_manual_time_override_is_rejected(title, date, start, end):
    with pytest.raises(HTTPException):
        PlanService._validate_adjustment_time_overrides([{'title': '阅读', 'date': '2035-09-10', 'minutes': 20, 'state': 'SCHEDULED'}],
            [{'title': title, 'scheduled_date': date, 'start_local': start, 'end_local': end}])
