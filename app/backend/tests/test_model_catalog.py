from types import SimpleNamespace

import httpx
import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from planner_api.config import Settings, get_settings
from planner_api import model_catalog as catalog
from planner_api.schemas import PlanGenerationRequest, ExplorationWindowRequest
from planner_api.user_settings_service import UserSettingsService
from test_kernel_selection import profile, configure_api


def discover(handler):
    settings = Settings(_env_file=None, llm_provider="openai_compatible",
                        llm_base_url="https://provider.test/v1", llm_api_key="synthetic-test-key",
                        llm_model="current")
    return catalog.discover_models(settings, client_factory=lambda **kwargs: httpx.Client(
        transport=httpx.MockTransport(handler), **kwargs))


def test_paged_catalog_deduplicates_ids_without_following_provider_next_url():
    calls = []

    def serve(request):
        calls.append(request)
        assert request.headers["Authorization"] == "Bearer synthetic-test-key"
        assert request.url.host == "provider.test"
        if not request.url.params:
            return httpx.Response(200, json={"data": [{"id": "beta"}, {"id": "alpha"},
                {"id": "bad\nvalue"}, {"id": "beta"}], "has_more": True, "last_id": "beta",
                "next": "https://untrusted.test/steal"})
        assert request.url.params["after"] == "beta"
        return httpx.Response(200, json={"data": [{"id": "gamma"}, {"id": "alpha"}]})

    result = discover(serve)
    assert result["available"]
    assert result["models"] == [{"id": value} for value in ["alpha", "beta", "gamma"]]
    assert result["model"] == "current" and len(calls) == 2
    assert "synthetic-test-key" not in str(result)


@pytest.mark.parametrize("status,payload", [(401, {"key": "synthetic-test-key"}),
    (404, {}), (200, {"error": "synthetic-test-key"}), (200, []), (200, "invalid")])
def test_unavailable_catalog_keeps_manual_model_and_does_not_expose_response(status, payload):
    result = discover(lambda request: httpx.Response(status, json=payload))
    assert not result["available"] and result["models"] == []
    assert result["model"] == "current" and len(result["connection_id"]) == 64
    assert "手动填写" in result["message"]
    assert "synthetic-test-key" not in str(result)


def test_catalog_redirect_never_forwards_key():
    calls = []

    def serve(request):
        calls.append(request)
        return httpx.Response(302, headers={"Location": "https://other.test/models"})

    assert not discover(serve)["available"]
    assert len(calls) == 1


@pytest.mark.parametrize('status,action', [(401, '密钥'), (403, '权限'), (404, 'Base URL'), (429, '额度'), (503, '稍后重试')])
def test_catalog_failure_explains_the_next_action_without_provider_body(status, action):
    result = discover(lambda _: httpx.Response(status, json={'error': 'private-provider-secret'}))
    assert not result['available']
    assert action in result['message']
    assert 'private-provider-secret' not in str(result)


@pytest.mark.parametrize('failure,action', [(httpx.ReadTimeout('private secret'), '超时'), (httpx.ConnectError('private secret'), '代理')])
def test_catalog_transport_failure_does_not_hide_the_recovery_action(failure, action):
    def fail(_): raise failure
    result = discover(fail)
    assert not result['available']
    assert action in result['message']
    assert 'private secret' not in str(result)


@pytest.mark.parametrize("base,expected", [
    ("https://api.test/v1/", "https://api.test/v1/models"),
    ("https://api.test/chat/completions", "https://api.test/models"),
    ("https://api.test/prefix/v1/responses", "https://api.test/prefix/v1/models")])
def test_model_list_url_preserves_provider_prefix(base, expected):
    assert catalog.models_url(base) == expected


def test_draft_catalog_does_not_save_and_only_reuses_key_for_identical_connection(profile, monkeypatch):
    path, _ = profile
    configure_api(path)
    before = path.read_bytes()
    captured = []
    monkeypatch.setattr(catalog, "discover_models", lambda settings: captured.append(settings))
    request = {"provider": "openai_compatible", "base_url": "https://example.test/v1"}
    catalog.preview_catalog(catalog.ModelCatalogRequest(**request))
    assert captured[-1].llm_api_key.get_secret_value() == "synthetic-key-1234"
    catalog.preview_catalog(catalog.ModelCatalogRequest(**{**request, "base_url": "https://other.test/v1"}))
    assert not captured[-1].llm_api_key.get_secret_value()
    catalog.preview_catalog(catalog.ModelCatalogRequest(**request, clear_api_key=True, api_key="unused"))
    assert not captured[-1].llm_api_key.get_secret_value()
    assert before == path.read_bytes() and get_settings().llm_provider == "codex"


def test_model_selection_preserves_key_kernel_and_rejects_stale_profile(profile):
    path, manager = profile
    configure_api(path)
    service = UserSettingsService(path)
    selection = catalog.ModelSelection(model="next-choice", connection_id=catalog.connection_id(service._settings()))
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(kernel=manager)))
    assert catalog.select_model(selection, request)["model"] == "next-choice"
    assert service._settings().llm_model == "next-choice"
    assert service._settings().llm_api_key.get_secret_value() == "synthetic-key-1234"
    assert get_settings().llm_provider == "codex" and get_settings().codex_reasoning_effort == "high"
    with pytest.raises(HTTPException) as error:
        catalog.select_model(selection.model_copy(update={"connection_id": "0" * 64}), request)
    assert error.value.status_code == 409
    manager.switch("api")
    with manager.business_request(), pytest.raises(HTTPException) as error:
        catalog.select_model(selection, request)
    assert error.value.status_code == 409
    assert get_settings().llm_model == "next-choice"


@pytest.mark.parametrize("schema", [PlanGenerationRequest, ExplorationWindowRequest])
def test_new_generation_only_accepts_standard_mode(schema):
    assert schema().mode == "STANDARD"
    with pytest.raises(ValidationError):
        schema(mode="DEEP")


def test_native_codex_readiness_never_constructs_legacy_components(monkeypatch):
    from planner_api.codex_runtime.readiness import CodexResearchReadiness
    monkeypatch.setattr("planner_api.research.readiness.ResearchReadiness.__init__",
                        lambda *args, **kwargs: pytest.fail("Native Codex must not warm legacy search"))
    readiness = CodexResearchReadiness(SimpleNamespace(research_rerank_mode="BGE"))
    readiness.start()
    with readiness.usage():
        assert readiness.connection("test-client", True, 1) == {
            "ready": True, "status": "NATIVE", "components": {}}
    readiness.stop()


def test_model_routes_and_codex_startup_skip_all_optional_prewarm(profile, monkeypatch):
    from fastapi.testclient import TestClient
    from planner_api.kernel import save_values
    from planner_api.main import create_app

    path, _ = profile
    configure_api(path)
    save_values(path, {"MATERIAL_RERANK_MODE": "GUARDED", "RESEARCH_RERANK_MODE": "BGE",
                       "MATERIAL_SEMANTIC_ENABLED": "true", "MATERIAL_SEMANTIC_PREWARM": "true"})
    def unexpected(*args, **kwargs):
        pytest.fail("Codex startup must not prewarm API components")
    monkeypatch.setattr("planner_api.materials.reranking.prewarm_bge", unexpected)
    monkeypatch.setattr("planner_api.materials.semantic.prewarm_encoder", unexpected)
    monkeypatch.setattr(catalog, "discover_models", lambda settings: {
        "model": settings.llm_model, "models": [{"id": "next"}], "available": True,
        "connection_id": catalog.connection_id(settings), "message": "Test provider"})
    with TestClient(create_app()) as client:
        bootstrap = client.get("/api/session", headers={"X-Solevia-Bootstrap": "1"})
        if bootstrap.status_code == 200:
            client.headers["X-Solevia-Token"] = bootstrap.json()["token"]
        response = client.get("/api/settings/model/catalog")
        assert response.status_code == 200
        assert response.json()["models"] == [{"id": "next"}]
        selected = client.put("/api/settings/model/selection", json={
            "model": "next", "connection_id": response.json()["connection_id"]})
        assert selected.status_code == 200 and selected.json()["model"] == "next"
    assert get_settings().llm_provider == "codex"
