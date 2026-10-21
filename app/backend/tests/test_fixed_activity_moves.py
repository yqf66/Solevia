from datetime import date, timedelta

from planner_api.fixed_activity_service import FixedActivityService


def seed(client):
    goal = client.post('/api/goals', json={'raw_input': '合成阅读项目，每周完成一次阅读。'}).json()['goal_id']
    source = date.today() + timedelta(days=5)
    target = source + timedelta(days=1)
    client.patch('/api/schedule/profile', json={'break_minutes': 0})
    result = client.post('/api/manual-plans', json={'title': '娱乐活动', 'tasks': [{
        'title': '玩游戏', 'estimate_minutes': 60, 'scheduled_date': source.isoformat(),
        'start_local': '20:00', 'end_local': '21:00', 'recurrence': 'WEEKLY',
        'recurrence_weekdays': [source.weekday()]}]})
    assert result.status_code == 201, result.text
    task = result.json()['tasks'][0]['id']
    prefix = f'/api/goals/{goal}/fixed-activities'
    payload = {'task_id': task, 'source_date': source.isoformat(), 'target_date': target.isoformat(),
               'start_local': '20:00', 'end_local': '21:00'}
    return prefix, source, target, payload


def activities(client, prefix, day):
    return client.get(prefix, params={'on_date': day.isoformat()}).json()['activities']


def test_preview_confirm_preserves_recurring_occurrences_and_audit(client):
    prefix, source, target, payload = seed(client)
    result = client.post(prefix + '/preview', json=payload)
    assert result.status_code == 200, result.text
    preview = result.json()
    assert preview['can_apply']
    assert [row['delta_minutes'] for row in preview['capacity_changes']] == [60, -60]
    assert len(activities(client, prefix, source)) == 1
    assert not activities(client, prefix, target)
    url = prefix + '/' + preview['id'] + '/apply'
    assert client.post(url, json={}).status_code == 422
    applied = client.post(url, json={'confirmed': True})
    assert applied.status_code == 200, applied.text
    assert applied.json()['status'] == 'APPLIED'
    assert not activities(client, prefix, source)
    assert len(activities(client, prefix, target)) == 1
    assert len(activities(client, prefix, source + timedelta(days=7))) == 1
    assert client.post(url, json={'confirmed': True}).json()['status'] == 'APPLIED'


def test_conflict_does_not_move_other_plan(client):
    prefix, source, target, payload = seed(client)
    client.post('/api/manual-plans', json={'title': '另一个计划', 'tasks': [{
        'title': '已有课程', 'estimate_minutes': 60, 'scheduled_date': target.isoformat(),
        'start_local': '20:00', 'end_local': '21:00'}]})
    preview = client.post(prefix + '/preview', json=payload).json()
    assert not preview['can_apply'] and preview['conflicts']
    assert client.post(prefix + '/' + preview['id'] + '/apply', json={'confirmed': True}).status_code == 409
    assert len(activities(client, prefix, source)) == 1
    board = client.get('/api/today', params={'on_date':target.isoformat()}).json()
    assert any(item['title']=='已有课程' for item in board['fixed_tasks'])


def test_cancel_and_stale_profile_require_new_preview(client):
    prefix, source, _, payload = seed(client)
    preview = client.post(prefix + '/preview', json=payload).json()
    client.post(prefix + '/' + preview['id'] + '/discard')
    assert client.post(prefix + '/' + preview['id'] + '/apply', json={'confirmed': True}).status_code == 409
    preview = client.post(prefix + '/preview', json=payload).json()
    client.patch('/api/schedule/profile', json={'break_minutes': 10})
    assert client.post(prefix + '/' + preview['id'] + '/apply', json={'confirmed': True}).status_code == 409
    assert len(activities(client, prefix, source)) == 1


def test_custom_availability_cannot_override_sleep_or_meals(client):
    prefix, _, target, payload = seed(client)
    client.put('/api/schedule/profile/availability', json={'windows': [
        {'weekday': target.weekday(), 'start_local': '12:00', 'end_local': '13:00'}]})
    preview = client.post(prefix + '/preview', json={**payload, 'start_local': '12:00', 'end_local': '13:00'}).json()
    assert not preview['can_apply']
    assert any('三餐' in item for item in preview['conflicts'])


def test_duration_past_and_duplicate_occurrence_rejected(client):
    prefix, source, _, payload = seed(client)
    assert client.post(prefix + '/preview', json={**payload, 'end_local': '20:30'}).status_code == 422
    assert client.post(prefix + '/preview', json={**payload, 'target_date': (source + timedelta(days=7)).isoformat()}).status_code == 409
    assert client.post(prefix + '/preview', json={**payload, 'target_date': (date.today() - timedelta(days=1)).isoformat()}).status_code == 409


def test_expired_preview_and_new_calendar_conflict_rejected(client, monkeypatch):
    prefix, _, target, payload = seed(client)
    preview = client.post(prefix + '/preview', json=payload).json()
    monkeypatch.setattr(FixedActivityService, '_today', lambda self: target + timedelta(days=1))
    assert client.post(prefix + '/' + preview['id'] + '/apply', json={'confirmed': True}).status_code == 409


def test_preview_expires_after_thirty_minutes(client, session_factory):
    from planner_api.models import FixedActivityMovePreview, utc_now
    prefix, source, _, payload = seed(client)
    preview = client.post(prefix + '/preview', json=payload).json()
    with session_factory() as session:
        session.get(FixedActivityMovePreview, preview['id']).created_at = utc_now() - timedelta(minutes=31)
        session.commit()
    result = client.post(prefix + '/' + preview['id'] + '/apply', json={'confirmed': True})
    assert result.status_code == 409 and '过期' in result.json()['detail']['message']
    assert len(activities(client, prefix, source)) == 1


def test_new_quick_plan_uses_released_slot_and_blocks_moved_slot(client):
    prefix, source, target, payload = seed(client)
    preview = client.post(prefix + '/preview', json=payload).json()
    assert client.post(prefix + '/' + preview['id'] + '/apply', json={'confirmed':True}).status_code == 200
    def add(day):
        return client.post('/api/manual-plans', json={'title':'迁移后安排', 'tasks':[{
            'title':'阅读', 'estimate_minutes':60, 'scheduled_date':day.isoformat(),
            'start_local':'20:00', 'end_local':'21:00'}]})
    assert add(source).status_code == 201
    assert add(target).status_code == 409


def test_protected_named_activity_cannot_be_moved_through_capacity_shortcut(client):
    prefix, source, _, payload = seed(client)
    plan = client.post('/api/manual-plans', json={'title':'工作安排', 'tasks':[{
        'title':'晚间工作', 'estimate_minutes':60,'scheduled_date':source.isoformat(),
        'start_local':'21:00','end_local':'22:00'}]}).json()
    task_id = plan['tasks'][0]['id']
    assert all(item['id']!=task_id for item in activities(client,prefix,source))
    result=client.post(prefix+'/preview',json={**payload,'task_id':task_id})
    assert result.status_code==409 and '保护活动' in result.json()['detail']['message']
