from pathlib import Path

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
