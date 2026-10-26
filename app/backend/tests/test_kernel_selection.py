from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from planner_api.config import get_settings, load_user_settings
from planner_api.kernel import KernelManager, save_values
from planner_api.schemas import ModelConnectionUpdate
from planner_api.user_settings_service import UserSettingsService


@pytest.fixture
def profile(tmp_path, monkeypatch):
    path = tmp_path / ".env"
    monkeypatch.setenv("SOLEVIA_ENV_FILE", str(path))
    monkeypatch.setenv("SOLEVIA_DEFAULT_KERNEL", "codex")
    for name in ("LLM_PROVIDER", "LLM_MODEL", "LLM_API_KEY", "SOLEVIA_KERNEL", "CODEX_MODEL"):
        monkeypatch.delenv(name, raising=False)
    path.write_text(
        "CODEX_MODEL=codex-choice\nCODEX_REASONING_EFFORT=high\nRESEARCH_RERANK_MODE=OFF\n",
        encoding="utf-8",
    )
    get_settings.cache_clear()
    runtime = SimpleNamespace(
        connected=True, generation_status=lambda: {"active_count": 0, "queued_count": 0}
    )
    app = SimpleNamespace(state=SimpleNamespace(codex_business=runtime))
    manager = KernelManager(app, busy_check=lambda: False, env_path=path)
    yield path, manager
    get_settings.cache_clear()


def configure_api(path):
    return UserSettingsService(path).update_model_connection(
        ModelConnectionUpdate(
            provider="openai_compatible",
            base_url="https://example.test/v1",
            model="api-choice",
            api_key="synthetic-key-1234",
        )
    )


def test_switch_round_trip_preserves_separate_profiles_and_encrypted_key(profile):
    path, manager = profile
    saved = configure_api(path)
    assert saved.model == "api-choice" and saved.api_key_configured
    assert get_settings().llm_provider == "codex"
    assert get_settings().llm_model == "codex-choice"
    assert not get_settings().llm_api_key.get_secret_value()
    assert manager.switch("api")["mode"] == "api"
    assert get_settings().llm_model == "api-choice"
    assert get_settings().llm_api_key.get_secret_value() == "synthetic-key-1234"
    assert manager.switch("codex")["mode"] == "codex"
    assert get_settings().llm_model == "codex-choice"
    assert get_settings().codex_reasoning_effort == "high"
    assert load_user_settings(path, kernel="api").llm_model == "api-choice"
    assert get_settings().research_rerank_mode == "OFF"


def test_incomplete_api_never_changes_active_kernel(profile):
    path, manager = profile
    before = path.read_bytes()
    with pytest.raises(HTTPException, match="422"):
        manager.switch("api")
    assert manager.snapshot()["mode"] == "codex"
    assert path.read_bytes() == before


def test_snapshot_distinguishes_saved_api_from_a_connected_codex(profile):
    path, manager = profile
    assert manager.snapshot()['api_configured'] is False
    configure_api(path)
    assert manager.snapshot()['api_configured'] is True
    runtime = manager.application.state.codex_business
    runtime.connected = False
    runtime.stop = lambda: None
    runtime.connection_error = '请先完成登录'
    runtime.connection_error_code = 'CODEX_LOGIN_REQUIRED'
    status = manager.snapshot()
    assert not status['codex_connected']
    assert status['codex_error_code'] == 'CODEX_LOGIN_REQUIRED'
    assert 'synthetic-key' not in str(status)


def test_disconnected_codex_does_not_replace_working_api(profile):
    path, manager = profile
    configure_api(path)
    manager.switch("api")
    manager.application.state.codex_business.connected = False
    with pytest.raises(HTTPException, match="409"):
        manager.switch("codex")
    assert manager.snapshot()["mode"] == "api"


@pytest.mark.parametrize("kind", ["request", "background"])
def test_switch_waits_for_in_flight_work(profile, kind):
    path, manager = profile
    configure_api(path)
    if kind == "background":
        manager.busy_check = lambda: True
        with pytest.raises(HTTPException, match="409"):
            manager.switch("api")
    else:
        with manager.business_request():
            with pytest.raises(HTTPException, match="409"):
                manager.switch("api")
    assert manager.snapshot()["mode"] == "codex"


def test_reload_uses_persisted_selection_over_development_default(profile):
    path, manager = profile
    configure_api(path)
    manager.switch("api")
    get_settings.cache_clear()
    assert load_user_settings(path).llm_provider == "openai_compatible"
    save_values(path, {"CODEX_MODEL": "new-codex-choice"})
    assert get_settings().llm_model == "api-choice"
    manager.switch("codex")
    assert get_settings().llm_model == "new-codex-choice"


def test_research_readiness_follows_selected_kernel(profile, monkeypatch):
    from planner_api.kernel_readiness import KernelReadiness
    import planner_api.codex_runtime.readiness as codex
    import planner_api.research.readiness as api

    path, manager = profile
    configure_api(path)
    save_values(path, {"WEB_SEARCH_PROVIDER": "open_websearch"})
    created = []

    class Ready:
        def __init__(self, settings):
            self.provider = settings.llm_provider
            created.append(self)

        def start(self):
            pass

        def stop(self):
            pass

        def snapshot(self):
            return {"provider": self.provider}

    monkeypatch.setattr(codex, "CodexResearchReadiness", Ready)
    monkeypatch.setattr(api, "ResearchReadiness", Ready)
    readiness = KernelReadiness()
    assert readiness.snapshot()["provider"] == "codex"
    manager.switch("api")
    assert readiness.snapshot()["provider"] == "openai_compatible"
    manager.switch("codex")
    assert readiness.snapshot()["provider"] == "codex"
    assert len(created) == 2
    readiness.stop()


def test_explicit_connect_preserves_unconfigured_mode_on_failure_and_can_retry(profile):
    path, manager = profile
    save_values(path, {"SOLEVIA_KERNEL": "api"})
    runtime = manager.application.state.codex_business
    runtime.connected = False
    attempts = []
    runtime.stop = lambda: None
    def start():
        attempts.append(1)
        runtime.connected = len(attempts) > 1
    runtime.start = start
    assert not attempts
    assert manager.snapshot()["mode"] == "api"
    failed = manager.connect_codex()
    assert not failed["codex_connected"] and failed["mode"] == "api"
    assert "SOLEVIA_CODEX_CONSENT=1" in path.read_text()
    ready = manager.connect_codex()
    assert ready["codex_connected"] and ready["mode"] == "codex"
    manager.connect_codex()
    assert len(attempts) == 2


def test_connect_during_a_generation_never_starts_codex(profile):
    path, manager = profile
    manager.active = 1
    before = path.read_bytes()
    with pytest.raises(HTTPException):
        manager.connect_codex()
    assert path.read_bytes() == before
