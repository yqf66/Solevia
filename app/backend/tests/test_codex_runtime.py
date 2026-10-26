import json
import sys
import time

import pytest
from fastapi.testclient import TestClient

from planner_api.codex_main import create_app
from planner_api.codex_runtime.service import CodexService
from planner_api.codex_runtime.protocol import CodexAppServer, CodexUnavailable
from planner_api.codex_runtime.store import ChatStore


class FakeProcess:
    def poll(self):
        return None


class FakeServer:
    instances = []

    def __init__(self, **kwargs):
        self.callbacks = kwargs
        self.initialized = False
        self.process = FakeProcess()
        self.calls = []
        self.responses = []
        self.instances.append(self)

    def start(self):
        self.initialized = True

    def stop(self):
        self.initialized = False

    def call(self, method, params, **kwargs):
        self.calls.append((method, params))
        if method in ('model/list', 'skills/list'):
            return {'data': []}
        if method == 'account/read':
            return {'account': {'type': 'apiKey'}}
        if method in ('thread/start', 'thread/resume'):
            return {'thread': {'id': 'thread-test'}}
        if method == 'turn/start':
            self.callbacks['on_event']({'method': 'turn/started', 'params': {'threadId': params['threadId'], 'turn': {'id': 'turn-test'}}})
            return {'turn': {'id': 'turn-test'}}
        if method == 'turn/interrupt':
            self.event('turn/completed', turn={'id': 'turn-test', 'status': 'interrupted'})
            return {}
        raise AssertionError(method)

    def event(self, method, **params):
        self.callbacks['on_event']({'method': method, 'params': {'threadId': 'thread-test', 'turnId': 'turn-test', **params}})

    def respond(self, request_id, result):
        self.responses.append((request_id, result))

    def reject(self, *args):
        pass


def wait_for(predicate):
    deadline = time.monotonic() + 3
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError('Condition did not become true')
        time.sleep(0.01)


@pytest.fixture
def service(tmp_path):
    service = CodexService(data_dir=tmp_path, workspace=tmp_path / 'workspace', server_factory=FakeServer)
    service.start()
    yield service
    service.stop()


def test_store_idempotency_active_gate_and_message_isolation(tmp_path):
    store = ChatStore(tmp_path / 'chat.db')
    session = store.create_session()['id']
    run, duplicate = store.create_run(session, 'first', 'request-1')
    again, duplicate = store.create_run(session, 'first', 'request-1')
    assert again['id'] == run['id'] and duplicate
    with pytest.raises(ValueError):
        store.create_run(session, 'second', 'request-2')
    store.answer(run['id'], 'partial')
    store.finish(run['id'], 'interrupted')
    second, _ = store.create_run(session, 'second', 'request-2')
    store.answer(second['id'], 'new')
    assert [message['content'] for message in store.snapshot(session)['messages']] == ['first', 'partial', 'second', 'new']
    store.close()


def test_restart_records_terminal_cursor_without_replaying(tmp_path):
    path = tmp_path / 'chat.db'
    store = ChatStore(path)
    session = store.create_session()['id']
    run, _ = store.create_run(session, 'question', 'request-1')
    store.answer(run['id'], 'partial answer')
    cursor = store.snapshot(session)['last_event_id']
    store.close()
    store = ChatStore(path)
    snapshot = store.snapshot(session)
    assert snapshot['messages'][-1]['content'] == 'partial answer'
    assert snapshot['active_run'] is None
    assert store.events(run['id'], cursor)[-1]['type'] == 'run_completed'
    assert snapshot['last_run']['status'] == 'interrupted'
    store.close()


def test_completion_failure_reasoning_filter_and_authoritative_content(service):
    session = service.store.create_session()['id']
    run = service.send(session, 'hello', 'request-1')
    wait_for(lambda: service.store.get_run(run['id'])['turn_id'])
    server = service.server
    server.event('item/reasoning/textDelta', delta='private reasoning')
    server.event('item/agentMessage/delta', itemId='answer', delta='part')
    server.event('item/completed', item={'id': 'answer', 'type': 'agentMessage', 'text': 'complete answer'})
    server.event('turn/completed', turn={'id': 'turn-test', 'status': 'failed', 'error': {'message': 'provider failed'}})
    assert service.store.snapshot(session)['messages'][-1]['content'] == 'complete answer'
    assert service.store.get_run(run['id'])['status'] == 'failed'
    assert 'private reasoning' not in json.dumps(service.store.events(run['id']))
    server.event('turn/completed', turn={'id': 'turn-test', 'status': 'completed'})
    assert service.store.get_run(run['id'])['status'] == 'failed'


def test_approval_preserves_rpc_id_requires_explicit_answer(service):
    session = service.store.create_session()['id']
    run = service.send(session, 'hello', 'request-1')
    wait_for(lambda: service.store.get_run(run['id'])['turn_id'])
    service.on_request({'id': 7, 'method': 'item/commandExecution/requestApproval', 'params': {'threadId': 'thread-test', 'command': 'read a file'}})
    req = service.store.snapshot(session)['requests'][0]
    assert not service.server.responses
    with pytest.raises(ValueError):
        service.respond(req['request_id'], {'decision': 'acceptForSession'})
    service.respond(req['request_id'], {'decision': 'decline'})
    assert service.server.responses == [(7, {'decision': 'decline'})]
    service.cancel(run['id'])
    assert service.store.get_run(run['id'])['status'] == 'interrupted'


def test_saved_thread_is_resumed(service):
    session = service.store.create_session()['id']
    service.store.set_thread(session, 'thread-test')
    run = service.send(session, 'continue', 'request-1')
    wait_for(lambda: service.store.get_run(run['id'])['turn_id'])
    assert any(method == 'thread/resume' for method, _ in service.server.calls)
    service.cancel(run['id'])


def test_jsonl_transport_roundtrip_request_id_and_eof(tmp_path, monkeypatch):
    import subprocess
    from planner_api.codex_runtime import protocol

    script = tmp_path / 'fake_app_server.py'
    script.write_text('''import json,sys
for line in sys.stdin:
 message=json.loads(line)
 method=message.get("method")
 if method=="initialize":
  print(json.dumps({"id":message["id"],"result":{}}),flush=True)
 elif method=="test/request":
  print(json.dumps({"id":17,"method":"item/commandExecution/requestApproval","params":{"command":"read"}}),flush=True)
  response=json.loads(sys.stdin.readline())
  print(json.dumps({"id":message["id"],"result":{"response_id":response["id"],"decision":response["result"]["decision"]}}),flush=True)
 elif method=="test/exit":
  break
''', encoding='utf-8')
    actual_popen = subprocess.Popen
    monkeypatch.setattr(protocol, 'find_codex', lambda: sys.executable)
    monkeypatch.setattr(protocol.subprocess, 'Popen', lambda command, **kwargs: actual_popen([sys.executable, '-u', str(script)], **kwargs))
    requests = []
    disconnected = []
    client = CodexAppServer(cwd=tmp_path, on_request=lambda message: (requests.append(message), client.respond(message['id'], {'decision': 'decline'})), on_disconnect=lambda: disconnected.append(True))
    client.start()
    assert client.call('test/request') == {'response_id': 17, 'decision': 'decline'}
    assert requests[0]['id'] == 17
    with pytest.raises(CodexUnavailable):
        client.call('test/exit')
    wait_for(lambda: disconnected)
    client.stop()


def test_api_security_and_sse_cursor(tmp_path):
    def factory(**kwargs):
        return CodexService(**kwargs, server_factory=FakeServer)
    app = create_app(data_dir=tmp_path, workspace=tmp_path / 'workspace', service_factory=factory)
    with TestClient(app, base_url='http://127.0.0.1', headers={}) as client:
        assert client.get('/api/codex/status').status_code == 401
        assert client.get('/api/session', headers={'Host': 'attacker.example', 'X-Solevia-Bootstrap': '1'}).status_code == 400
        assert client.get('/api/session', headers={'Origin': 'https://evil.example', 'X-Solevia-Bootstrap': '1'}).status_code == 403
        token = client.get('/api/session', headers={'X-Solevia-Bootstrap': '1'}).json()['token']
        headers = {'X-Solevia-Token': token}
        session = client.post('/api/codex/sessions', headers=headers).json()['id']
        run = client.post(f'/api/codex/sessions/{session}/messages', json={'content': 'hello', 'client_request_id': 'request-1'}, headers=headers).json()
        server = FakeServer.instances[-1]
        wait_for(lambda: any(method == 'turn/start' for method, _ in server.calls))
        server.event('item/agentMessage/delta', itemId='answer', delta='hello')
        snapshot = client.get(f'/api/codex/sessions/{session}', headers=headers).json()
        server.event('turn/completed', turn={'id': 'turn-test', 'status': 'completed'})
        text = client.get(f"/api/codex/runs/{run['id']}/events?after={snapshot['last_event_id']}", headers=headers).text
        assert 'run_completed' in text and 'assistant_content' not in text
        assert client.get('/api/codex/runs/missing/events', headers=headers).status_code == 404
