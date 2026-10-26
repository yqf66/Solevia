import json
import threading
from concurrent.futures import ThreadPoolExecutor
from urllib.error import URLError

import pytest

from planner_api.ai import _urllib_json_transport, configured_json_transport, ProviderUnavailableError
from planner_api.codex_runtime.business import BusinessCodexRuntime, bind_runtime
from planner_api.config import Settings, load_user_settings
from planner_api.light_arrangement_ai import LightArrangementProvider
from planner_api.light_schemas import LightDraftCreate
from pydantic import SecretStr


class BusinessServer:
    def __init__(self, **callbacks):
        self.callbacks = callbacks
        self.calls = []
        self.count = 0
        self.completed = True
        self.fail = False
        self.lock = threading.Lock()

    def start(self):
        pass

    def stop(self):
        pass

    def respond(self, request_id, result):
        self.calls.append((request_id, result))

    def reject(self, request_id, reason):
        self.calls.append((request_id, reason))

    def call(self, method, params, **kwargs):
        with self.lock:
            self.calls.append((method, params))
            if method == 'account/read':
                return {'account': {'type': 'apiKey'}, 'requiresOpenaiAuth': False}
            if method == 'thread/start':
                self.count += 1
                return {'thread': {'id': f'thread-{self.count}'}}
        if method == 'turn/start':
            thread = params['threadId']
            event = self.callbacks['on_event']
            event({'method': 'turn/started', 'params': {'threadId': thread, 'turn': {'id': thread + '-turn'}}})
            event({'method': 'item/completed', 'params': {'threadId': thread, 'item': {'id': 'notice', 'type': 'agentMessage', 'phase': 'commentary', 'text': 'private notice'}}})
            event({'method': 'item/completed', 'params': {'threadId': thread, 'item': {'id': 'answer', 'type': 'agentMessage', 'text': json.dumps({'title': thread, 'tasks': [{'title': 'read', 'estimate_minutes': 20, 'scheduled_date': '2026-10-23'}]})}}})
            if self.completed:
                event({'method': 'turn/completed', 'params': {'threadId': thread, 'turn': {'id': thread + '-turn', 'status': 'failed' if self.fail else 'completed', 'error': {'message': 'rejected'} if self.fail else None}}})
            return {'turn': {'id': thread + '-turn'}}
        return {}


@pytest.fixture
def runtime(tmp_path):
    runtime = BusinessCodexRuntime(tmp_path, server_factory=BusinessServer)
    runtime.start()
    bind_runtime(runtime)
    yield runtime
    bind_runtime(None)
    runtime.stop()


def test_existing_schema_provider_works_without_api_key(runtime):
    settings = Settings(_env_file=None, llm_provider='codex', llm_model='test-codex')
    provider = LightArrangementProvider(settings)
    result = provider.arrange(LightDraftCreate(raw_input='read for 20 minutes', start_date='2026-10-23'))
    assert result.tasks[0].estimate_minutes == 20
    assert result.title == 'thread-1'
    serialized = json.dumps(runtime.server.calls)
    assert 'Authorization' not in serialized and 'Bearer' not in serialized
    assert not runtime.pending


def test_ordinary_codex_turn_can_search_without_shell_or_filesystem_expansion(runtime):
    runtime.generate({'input': 'Compare current options'}, 2)
    start = next(params for method, params in runtime.server.calls if method == 'thread/start')
    turn = next(params for method, params in runtime.server.calls if method == 'turn/start')
    assert start['config']['web_search'] == 'live'
    assert start['config']['features.shell_tool'] is False
    assert turn['sandboxPolicy']['type'] == 'readOnly'
    assert turn['sandboxPolicy']['networkAccess'] is False


def test_parallel_requests_have_distinct_threads(runtime):
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: json.loads(runtime.generate({'model': 'codex', 'input': 'hello'}, 2))['title'], range(2)))
    assert set(results) == {'thread-1', 'thread-2'}
    assert not runtime.pending


@pytest.mark.parametrize('effort', ['low', 'medium', 'high', 'xhigh', 'max', 'ultra'])
def test_catalog_effort_reaches_app_server_without_silent_remapping(runtime, effort):
    runtime.generate({'model': 'chosen-model', 'reasoning_effort': effort}, 2)
    assert next(params for method, params in runtime.server.calls if method == 'thread/start')['model'] == 'chosen-model'
    assert next(params for method, params in runtime.server.calls if method == 'turn/start')['effort'] == effort


def test_direct_hybrid_planning_keeps_author_design_contract_without_second_model_pass(monkeypatch):
    from planner_api.codex_runtime import providers
    from planner_api.schemas import HybridPlanOutlineResult
    captured = []
    attempts = []
    def generate(provider, payload, validate, **kwargs):
        captured.append(payload)
        attempts.append(kwargs['attempts'])
        return 'accepted'
    monkeypatch.setattr(providers, 'validated_generation', generate)
    monkeypatch.setattr(providers, 'with_document', lambda result: result)
    provider = providers.CodexPlanProvider(Settings(_env_file=None, llm_provider='codex'))
    result = provider._generate(HybridPlanOutlineResult, 'hybrid_plan_outline', 'compiler constraint', '{}', 6000, 'STANDARD')
    assert result == 'accepted' and len(captured) == 1
    assert '不存在上游作者稿' in captured[0]['instructions']
    assert 'OUTCOME' in captured[0]['instructions'] and 'compiler constraint' in captured[0]['instructions']
    assert captured[0]['solevia_schema'] == HybridPlanOutlineResult.model_json_schema()
    provider._generate(HybridPlanOutlineResult, 'hybrid_plan_outline', '',
        '{"planning_context":{"generation_validation_feedback":null}}', 6000, 'STANDARD')
    provider._generate(HybridPlanOutlineResult, 'hybrid_plan_outline', '',
        '{"planning_context":{"generation_validation_feedback":{"code":"CAPACITY"}}}', 6000, 'STANDARD')
    assert attempts == [3, 3, 1]


def test_failed_and_timed_out_turns_never_return_partial_success(runtime):
    runtime.server.fail = True
    with pytest.raises(URLError, match='rejected'):
        runtime.generate({}, 1)
    runtime.server.completed = False
    with pytest.raises(TimeoutError):
        runtime.generate({}, 0.02)
    assert any(method == 'turn/interrupt' for method, _ in runtime.server.calls)
    assert not runtime.pending
    runtime.disconnected()
    with pytest.raises(URLError):
        runtime.generate({}, 1)


def test_business_nodes_decline_privilege_requests(runtime):
    runtime.on_request({'id': 42, 'method': 'item/fileChange/requestApproval'})
    runtime.on_request({'id': 43, 'method': 'item/permissions/requestApproval'})
    assert (42, {'decision': 'decline'}) in runtime.server.calls
    assert (43, {'permissions': {}, 'scope': 'turn'}) in runtime.server.calls


def test_unavailable_catalog_model_returns_actionable_error_without_leaking_provider_url(runtime):
    event = runtime.server.callbacks['on_event']
    def unavailable(message):
        if message['method'] == 'turn/completed':
            message['params']['turn'].update(status='failed', error={
                'message': 'unexpected status 404 Model "test" is not available for this group, url: https://private-provider.invalid/responses'})
        event(message)
    runtime.server.callbacks['on_event'] = unavailable
    with pytest.raises(ProviderUnavailableError) as error:
        runtime.generate({'model': 'test'}, 2)
    assert error.value.code == 'CODEX_MODEL_UNAVAILABLE'
    assert '切换模型' in str(error.value)
    assert 'private-provider' not in str(error.value) and 'private-provider' not in error.value.diagnostic
    assert not runtime.pending


def test_schema_is_in_protocol_and_model_input(runtime):
    schema = {'type': 'object', 'properties': {'value': {'type': 'integer'}}, 'required': ['value']}
    runtime.generate({'solevia_schema': schema}, 2)
    turn = next(params for method, params in runtime.server.calls if method == 'turn/start')
    assert turn['outputSchema'] == {**schema, 'additionalProperties': False}
    assert json.loads(turn['input'][0]['text'])['output_schema'] == schema


def test_protocol_schema_requires_defaulted_nested_fields_but_keeps_business_schema_intact():
    from planner_api.codex_runtime.business import protocol_output_schema
    from planner_api.research.schemas import EvidenceSpanChoices
    schema = EvidenceSpanChoices.model_json_schema()
    adapted = protocol_output_schema(schema)
    assert adapted['$defs']['EvidenceSpanChoice']['required'] == ['span_id', 'covers_aspects']
    assert schema['$defs']['EvidenceSpanChoice']['required'] == ['span_id']
    assert protocol_output_schema({'type': 'object', 'additionalProperties': {'type': 'string'}}) is None


def test_shared_envelopes_expose_schema_and_explicit_material_scope(runtime):
    from planner_api.codex_runtime.business import codex_json_transport
    schema = {'type': 'object', 'properties': {'value': {'type': 'integer'}}}
    transport = codex_json_transport(Settings(_env_file=None, llm_provider='codex'))
    transport('', {}, {'text': {'format': {'type': 'json_schema', 'schema': schema}},
        'input': json.dumps({'material_context': {'goal_id': 'goal', 'source_ids': ['source'],
            'sources': [{'source_id': 'source', 'title': 'Selected'}],
            'revisions': [{'source_id': 'source', 'revision_id': 'revision'}]}})}, 2)
    start = next(params for method, params in runtime.server.calls if method == 'thread/start')
    assert {tool['name'] for tool in start['dynamicTools']} == {'solevia_list_materials', 'solevia_read_material'}
    turn = next(params for method, params in runtime.server.calls if method == 'turn/start')
    assert json.loads(turn['input'][0]['text'])['output_schema'] == schema


def test_codex_never_reads_or_migrates_solevia_credentials(tmp_path, monkeypatch):
    monkeypatch.setenv('LLM_PROVIDER', 'codex')
    monkeypatch.setattr('planner_api.credential_store.read_key', lambda *a, **k: pytest.fail('Credential store must not be read'))
    settings = load_user_settings(tmp_path / '.env', migrate=True)
    assert settings.model_runtime_configured
    assert not settings.llm_api_key.get_secret_value()


def test_injected_transports_remain_untouched():
    settings = Settings(_env_file=None, llm_provider='codex')
    def injected(*args):
        return {'test': True}
    assert configured_json_transport(settings, injected) is injected
    assert configured_json_transport(settings, _urllib_json_transport) is not _urllib_json_transport


def test_codex_uses_direct_providers_and_api_keeps_existing_strategy():
    from planner_api.ai import build_clarification_provider
    from planner_api.route_ai import build_route_provider, SemanticRouteProvider
    from planner_api.plan_ai import build_plan_provider, SemanticPlanProvider
    from planner_api.codex_runtime.providers import CodexPlanProvider, CodexRouteProvider, CodexClarificationProvider

    settings = Settings(_env_file=None, llm_provider='codex')
    assert isinstance(build_route_provider(settings), CodexRouteProvider)
    assert isinstance(build_plan_provider(settings), CodexPlanProvider)
    assert isinstance(build_clarification_provider(settings), CodexClarificationProvider)
    api_settings = settings.model_copy(update={'llm_provider': 'openai_compatible', 'llm_api_key': SecretStr('test')})
    assert isinstance(build_route_provider(api_settings), SemanticRouteProvider)
    assert isinstance(build_plan_provider(api_settings), SemanticPlanProvider)


def test_commentary_deltas_never_enter_final_conversation(runtime):
    from planner_api.codex_runtime.business import PendingTurn
    pending = PendingTurn()
    runtime.pending['phase-thread'] = pending
    def emit(method, **params):
        runtime.on_event({'method': method, 'params': {'threadId': 'phase-thread', **params}})
    emit('item/started', item={'id': 'notice', 'type': 'agentMessage', 'phase': 'commentary'})
    emit('item/agentMessage/delta', itemId='notice', delta='internal progress')
    assert not pending.parts
    emit('item/started', item={'id': 'reply', 'type': 'agentMessage', 'phase': 'final_answer'})
    emit('item/agentMessage/delta', itemId='reply', delta='accepted answer candidate')
    assert list(pending.parts) == ['reply']
    assert not pending.done.is_set()
