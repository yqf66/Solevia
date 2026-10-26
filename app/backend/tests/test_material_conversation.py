from pathlib import Path
import pytest

from fastapi.testclient import TestClient

from planner_api.ai import FakeClarificationProvider
from planner_api.api import get_service
from planner_api.materials.analysis_ai import FakeMaterialAnalysisProvider
from planner_api.materials.worker import MaterialProcessingWorker
from planner_api.service import ClarificationService


def test_material_conversation_scope_context_and_regeneration(
    client: TestClient, tmp_path: Path, session_factory, monkeypatch
) -> None:
    monkeypatch.setattr(
        "planner_api.service.build_material_analysis_provider",
        lambda _settings: FakeMaterialAnalysisProvider(),
    )
    contexts = []

    class CaptureProvider(FakeClarificationProvider):
        def respond(self, context):
            contexts.append(context)
            return super().respond(context)

    def service_override():
        with session_factory() as session:
            yield ClarificationService(session, CaptureProvider())

    client.app.dependency_overrides[get_service] = service_override
    goal_id = client.post("/api/goals", json={"raw_input": "交付规划"}).json()["goal_id"]
    other_goal = client.post("/api/goals", json={"raw_input": "另一个规划"}).json()["goal_id"]
    (tmp_path / "delivery.md").write_text(
        "# 交付\n交付日期为六月底，必须先完成灾备演练。", encoding="utf-8"
    )
    root = client.post(
        "/api/material-roots", json={"display_name": "交付资料", "path": str(tmp_path)}
    ).json()
    source = client.post(
        f"/api/goals/{goal_id}/materials/local",
        json={"root_id": root["id"], "relative_path": "delivery.md"},
    ).json()["source_id"]
    client.post(f"/api/goals/{goal_id}/materials/{source}/process")
    assert MaterialProcessingWorker(session_factory).run_once()
    response = client.post(
        f"/api/goals/{goal_id}/messages",
        json={"content": "交付日期和前置条件？", "material_source_ids": [source]},
    )
    assert response.status_code == 200, response.text
    context = contexts[-1].material_context
    assert context["source_ids"] == [source]
    assert "六月底" in context["passages"][0]["text"]
    assert context["passages"][0]["relative_path"] == "delivery.md"
    user = next(item for item in reversed(response.json()["messages"]) if item["role"] == "USER")
    assert user["metadata"]["material_titles"] == ["delivery.md"]
    count = len(response.json()["messages"])
    regenerated = client.post(f"/api/goals/{goal_id}/messages/regenerate")
    assert regenerated.status_code == 200
    assert len(regenerated.json()["messages"]) == count
    assert contexts[-1].material_context == context
    unrelated = client.post(
        f"/api/goals/{other_goal}/messages",
        json={"content": "交付日期？", "material_source_ids": [source]},
    )
    assert unrelated.status_code == 422
    cleared = client.post(
        f"/api/goals/{goal_id}/messages", json={"content": "谢谢", "material_source_ids": []}
    )
    assert cleared.status_code == 200
    assert contexts[-1].material_context is None


def test_codex_material_tool_rechecks_pinned_revision_and_link(client, tmp_path, session_factory):
    import pytest
    from fastapi import HTTPException
    from planner_api.codex_runtime.materials import material_tools
    from planner_api.materials.reading import MaterialReadingService
    from planner_api.models import MaterialRevision

    goal = client.post('/api/goals', json={'raw_input': '资料授权测试'}).json()['goal_id']
    (tmp_path / 'scope.md').write_text('只读取本次授权正文。', encoding='utf-8')
    root = client.post('/api/material-roots', json={'display_name': '授权资料', 'path': str(tmp_path)}).json()
    source = client.post(f'/api/goals/{goal}/materials/local',
        json={'root_id': root['id'], 'relative_path': 'scope.md'}).json()['source_id']
    client.post(f'/api/goals/{goal}/materials/{source}/process')
    assert MaterialProcessingWorker(session_factory).run_once()
    with session_factory() as session:
        revision = MaterialReadingService(session).revisions(goal, source)[0].id
    handler = material_tools({'goal_id': goal, 'source_ids': [source],
        'sources': [{'source_id': source, 'title': 'scope.md'}],
        'revisions': [{'source_id': source, 'revision_id': revision}]}, session_factory=session_factory)['solevia_tool_handler']
    args = {'source_id': source, 'offset': 0, 'limit': 100}
    result = handler('solevia_read_material', args)
    assert result['text'] == '只读取本次授权正文。' and result['revision_id'] == revision
    with session_factory() as session:
        session.get(MaterialRevision, revision).status = 'FAILED'
        session.commit()
    with pytest.raises(HTTPException) as failed:
        handler('solevia_read_material', args)
    assert failed.value.status_code == 409
    with session_factory() as session:
        session.get(MaterialRevision, revision).status = 'READY'
        session.commit()
    assert client.delete(f'/api/goals/{goal}/materials/{source}').status_code == 204
    with pytest.raises(HTTPException):
        handler('solevia_read_material', args)


@pytest.mark.parametrize('phase', ['route', 'plan'])
def test_review_attachment_reaches_provider_survives_retry_and_rechecks_unlink(client, tmp_path, session_factory, monkeypatch, phase):
    from planner_api.api import get_route_service, get_plan_service
    from planner_api.route_ai import FakeRouteProvider
    from planner_api.route_explanation_ai import FakeRouteExplanationProvider
    from planner_api.route_service import RouteService
    from planner_api.plan_ai import FakePlanProvider
    from planner_api.plan_discussion_ai import FakePlanDiscussionProvider
    from planner_api.plan_service import PlanService
    from test_route import _confirmed_requirement
    from test_plan import _completed_plan

    if phase == 'route':
        goal = _confirmed_requirement(client)['goal_id']
        client.post(f'/api/goals/{goal}/route/draft').raise_for_status()
    else:
        goal = _completed_plan(client)['goal_id']
    monkeypatch.setattr('planner_api.materials.analysis_ai.build_material_analysis_provider', lambda _: FakeMaterialAnalysisProvider())
    (tmp_path / 'review.md').write_text('每周仅240分钟，不增加第二个项目。', encoding='utf-8')
    root = client.post('/api/material-roots', json={'display_name': '复核资料', 'path': str(tmp_path)}).json()
    source = client.post(f'/api/goals/{goal}/materials/local', json={'root_id': root['id'], 'relative_path': 'review.md'}).json()['source_id']
    client.post(f'/api/goals/{goal}/materials/{source}/process')
    assert MaterialProcessingWorker(session_factory).run_once()
    contexts = []
    from planner_api.config import get_settings
    codex_settings = get_settings().model_copy(update={'llm_provider': 'codex'})
    monkeypatch.setattr('planner_api.config.get_settings', lambda: codex_settings)
    class Explain(FakeRouteExplanationProvider):
        def explain(self, context):
            contexts.append(context)
            return super().explain(context)
    class Discuss(FakePlanDiscussionProvider):
        def explain(self, context):
            contexts.append(context)
            return super().explain(context)
    def override():
        with session_factory() as session:
            yield RouteService(session, FakeRouteProvider(), Explain()) if phase == 'route' else PlanService(session, FakePlanProvider(), Discuss())
    client.app.dependency_overrides[get_route_service if phase == 'route' else get_plan_service] = override
    url = f'/api/goals/{goal}/{phase}/explain'
    prompt = '请依据附件解释时间上限'
    first = client.post(url, json={'content': prompt, 'material_source_ids': [source]})
    assert first.status_code == 200, first.text
    assert contexts[-1].material_context['source_ids'] == [source]
    assert contexts[-1].material_context['passages'] == []
    from planner_api.codex_runtime.materials import material_tools
    read = material_tools(contexts[-1].material_context, session_factory=session_factory)['solevia_tool_handler']
    assert '240' in read('solevia_read_material', {'source_id': source, 'offset': 0, 'limit': 100})['text']
    retry = client.post(url, json={'content': prompt, 'regenerate': True})
    assert retry.status_code == 200, retry.text
    assert len(retry.json()['messages']) == len(first.json()['messages'])
    assert contexts[-1].material_context['source_ids'] == [source]
    assert client.delete(f'/api/goals/{goal}/materials/{source}').status_code == 204
    denied = client.post(url, json={'content': prompt, 'regenerate': True})
    assert denied.status_code == 422
    assert len(contexts) == 2
