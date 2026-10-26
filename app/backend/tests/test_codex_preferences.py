import json
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from planner_api.codex_runtime import business
from planner_api.codex_runtime.preferences import CodexPreferenceUpdate, generation_timeout, read_preferences, save_preferences
from planner_api.config import Settings, get_settings
from planner_api.codex_runtime.preferences import ensure_initial_model


class Catalog:
    def call(self, method, params, **kwargs):
        assert method == 'model/list'
        return {'data': [{'model': 'gpt-6.1-sol', 'supportedReasoningEfforts': [
            {'reasoningEffort': effort} for effort in ['low', 'medium', 'high']]}]}


@pytest.mark.parametrize('configured,expected', [('configured-model', 'configured-model'), ('', 'catalog-default')])
def test_first_connection_resolves_model_without_overwriting_api_profile(tmp_path, monkeypatch, configured, expected):
    path = tmp_path / '.env'
    path.write_text('LLM_PROVIDER=fake\nLLM_MODEL=api-model\nCODEX_MODEL=\nSOLEVIA_KERNEL=api\n')
    for key in ('LLM_PROVIDER', 'LLM_MODEL', 'CODEX_MODEL'):
        monkeypatch.delenv(key, raising=False)
    calls = []
    class InitialCatalog:
        def call(self, method, params, **kwargs):
            calls.append(method)
            if method == 'config/read':
                return {'config': {'model': configured}}
            return {'data': [{'model': 'first', 'supportedReasoningEfforts': [{'reasoningEffort': 'low'}]},
                             {'model': 'catalog-default', 'isDefault': True, 'supportedReasoningEfforts': [{'reasoningEffort': 'low'}]}]}
    runtime = SimpleNamespace(connected=True, server=InitialCatalog())
    assert ensure_initial_model(runtime, path) == expected
    assert 'LLM_MODEL=api-model' in path.read_text()
    before = list(calls)
    assert ensure_initial_model(runtime, path) == expected
    assert calls == before  # An existing explicit model is never replaced.


def test_supported_effort_is_persisted_and_unsupported_effort_is_rejected(tmp_path, monkeypatch):
    path = tmp_path / '.env'
    monkeypatch.setenv('SOLEVIA_ENV_FILE', str(path))
    monkeypatch.setenv('LLM_PROVIDER', 'codex')
    monkeypatch.setenv('LLM_MODEL', 'gpt-6.1-sol')
    monkeypatch.delenv('CODEX_REASONING_EFFORT', raising=False)
    monkeypatch.delenv('CODEX_MODEL', raising=False)
    get_settings.cache_clear()
    runtime = SimpleNamespace(connected=True, server=Catalog())
    try:
        saved = save_preferences(runtime, CodexPreferenceUpdate(reasoning_effort='high'), env_path=path)
        assert saved['reasoning_effort'] == 'high' and saved['wait_seconds'] == 2400
        assert read_preferences(runtime)['supported_efforts'] == ['low', 'medium', 'high']
        with pytest.raises(HTTPException) as error:
            save_preferences(runtime, CodexPreferenceUpdate(reasoning_effort='xhigh'), env_path=path)
        assert error.value.status_code == 422
        assert 'high' in path.read_text()
    finally:
        get_settings.cache_clear()


def test_model_change_uses_target_capabilities_and_survives_startup_model_override(tmp_path, monkeypatch):
    path = tmp_path / '.env'
    path.write_text('LLM_MODEL=api-model\nOTHER_SETTING=keep\n', encoding='utf-8')
    monkeypatch.setenv('SOLEVIA_ENV_FILE', str(path))
    monkeypatch.setenv('LLM_PROVIDER', 'codex')
    monkeypatch.setenv('LLM_MODEL', 'gpt-6.1-sol')
    for key in ('CODEX_REASONING_EFFORT', 'CODEX_MODEL'):
        monkeypatch.delenv(key, raising=False)
    class Pages:
        def call(self, method, params, **kwargs):
            if not params.get('cursor'):
                return {'data': Catalog().call(method, params)['data'], 'nextCursor': 'page2'}
            return {'data': [
                {'model': 'target', 'displayName': 'Target', 'defaultReasoningEffort': 'max',
                 'supportedReasoningEfforts': [{'reasoningEffort': 'max'}, {'reasoningEffort': 'ultra'}]},
                {'model': 'hidden', 'hidden': True, 'supportedReasoningEfforts': [{'reasoningEffort': 'low'}]}]}
    runtime = SimpleNamespace(connected=True, server=Pages())
    get_settings.cache_clear()
    try:
        saved = save_preferences(runtime, CodexPreferenceUpdate(model='target', reasoning_effort='max'), env_path=path)
        assert saved['model'] == 'target' and saved['wait_seconds'] == 5400
        assert [m['id'] for m in saved['models']] == ['gpt-6.1-sol', 'target']
        get_settings.cache_clear()
        assert get_settings().llm_model == 'target'
        previous = path.read_bytes()
        for model, effort in [('target', 'low'), ('unknown', 'max'), ('hidden', 'low')]:
            with pytest.raises(HTTPException):
                save_preferences(runtime, CodexPreferenceUpdate(model=model, reasoning_effort=effort), env_path=path)
            assert path.read_bytes() == previous
        assert 'LLM_MODEL=api-model' in path.read_text() and 'OTHER_SETTING=keep' in path.read_text()
    finally:
        get_settings.cache_clear()


def test_codex_transport_uses_selected_effort_and_long_budget_but_respects_repair_remainder(monkeypatch):
    calls = []
    def generate(payload, timeout):
        calls.append((payload, timeout))
        return json.dumps({'ok': True})
    monkeypatch.setattr(business, '_runtime', SimpleNamespace(generate=generate))
    settings = Settings(_env_file=None, llm_provider='codex', codex_reasoning_effort='high')
    transport = business.codex_json_transport(settings)
    transport('', {}, {'reasoning': {'effort': 'none'}}, 180)
    assert calls[-1][1] == generation_timeout(settings) == 2400
    assert calls[-1][0]['reasoning_effort'] == 'high' and 'reasoning' not in calls[-1][0]
    transport('', {}, {'solevia_budget_remaining': True}, 2100)
    assert calls[-1][1] == 2100
    assert transport.codex_timeout_seconds == 2400
    assert Settings(_env_file=None, llm_provider='openai_compatible').plan_timeout_seconds == 180


def test_generation_status_is_metadata_only_and_reports_disconnect(tmp_path):
    runtime = business.BusinessCodexRuntime(tmp_path)
    runtime.pending['sensitive-thread-id'] = business.PendingTurn(
        parts={'answer': 'unvalidated private answer'}, effort='high', stage='THINKING', timeout_seconds=2400)
    snapshot = runtime.generation_status()
    assert snapshot['active_count'] == 1 and snapshot['requests'][0]['stage'] == 'THINKING'
    assert 'private' not in json.dumps(snapshot) and 'sensitive-thread' not in json.dumps(snapshot)
    runtime.disconnected()
    assert not runtime.generation_status()['connected']
