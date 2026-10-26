"""Production API must never present offline fixtures as model answers."""
import asyncio
from types import SimpleNamespace
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from starlette.requests import Request
from planner_api.main import create_app
from planner_api.config import get_settings
from planner_api.model_connection import model_required, require_model_connection


@pytest.mark.parametrize("path", [
    "/goals", "/goals/missing/clarification/start", "/goals/missing/messages",
    "/goals/missing/messages/retry", "/goals/missing/messages/regenerate",
    "/goals/missing/requirements/draft", "/goals/missing/route/draft",
    "/goals/missing/route/explain", "/goals/missing/route/revise",
    "/goals/missing/plan/draft", "/goals/missing/plan/details/next",
    "/goals/missing/plan/generation/start", "/goals/missing/plan/explain",
    "/goals/missing/plan/revise", "/goals/missing/plan/compiler/batches",
    "/goals/missing/plan/exploration/draft", "/goals/missing/execution/recovery/ai",
    "/goals/missing/execution/discussion/messages", "/goals/missing/execution/discussion/retry",
    "/goals/missing/execution/calendar/smart-preview", "/quick-plan-drafts/missing/generate",
    "/goals/missing/research/requests", "/today/allocation/preview", "/today/allocation/preview-many",
])
def test_unconfigured_production_endpoints_stop_before_any_fake_generation(monkeypatch, path):
    monkeypatch.setenv("SOLEVIA_DEFAULT_KERNEL", "api")
    get_settings.cache_clear()
    app = create_app()
    client = TestClient(app)
    # Other integration tests explicitly opt into fake dependencies in conftest.
    # Exercise the unmodified production dependency for these requests.
    app.dependency_overrides.pop(require_model_connection, None)
    response = client.post("/api" + path, json={})
    assert response.status_code == 503, response.text
    assert response.json()["detail"]["code"] == "MODEL_CONNECTION_REQUIRED"
    assert "配置 API" in response.json()["detail"]["message"]


def test_disconnected_codex_is_not_silently_replaced_by_an_api(monkeypatch):
    monkeypatch.setenv("SOLEVIA_DEFAULT_KERNEL", "codex")
    get_settings.cache_clear()
    @model_required
    def endpoint(): pass
    app = SimpleNamespace(state=SimpleNamespace(codex_business=SimpleNamespace(connected=False)))
    request = Request({"type": "http", "app": app, "endpoint": endpoint})
    with pytest.raises(HTTPException) as caught:
        asyncio.run(require_model_connection(request))
    assert caught.value.detail["code"] == "MODEL_CONNECTION_REQUIRED"
    assert "Codex 尚未连接" in caught.value.detail["message"]
    app.state.codex_business.connected = True
    asyncio.run(require_model_connection(request))


def test_storage_and_settings_remain_available_without_a_model(monkeypatch):
    monkeypatch.setenv("SOLEVIA_DEFAULT_KERNEL", "api")
    get_settings.cache_clear()
    app = create_app()
    client = TestClient(app)
    app.dependency_overrides.pop(require_model_connection, None)
    assert client.get("/api/settings/kernel").status_code == 200
    assert client.get("/api/health").status_code == 200
