"""Turn termination edge cases live apart from the transport fixture module."""
from planner_api.codex_runtime.service import CodexService
from test_codex_runtime import FakeServer, wait_for


def test_timeout_waits_for_acknowledged_interrupt(tmp_path, monkeypatch):
    monkeypatch.setenv('SOLEVIA_CODEX_TURN_TIMEOUT', '0.05')
    service = CodexService(data_dir=tmp_path, workspace=tmp_path / 'workspace', server_factory=FakeServer)
    service.start()
    try:
        session = service.store.create_session()['id']
        run = service.send(session, 'hello', 'request-1')
        wait_for(lambda: service.store.get_run(run['id'])['status'] == 'failed')
        assert '超时' in service.store.get_run(run['id'])['error']
        assert any(method == 'turn/interrupt' for method, _ in service.server.calls)
    finally:
        service.stop()


def test_late_events_from_old_turn_do_not_complete_new_run(tmp_path):
    service = CodexService(data_dir=tmp_path, workspace=tmp_path / 'workspace', server_factory=FakeServer)
    service.start()
    try:
        session = service.store.create_session()['id']
        run = service.send(session, 'hello', 'request-1')
        wait_for(lambda: service.store.get_run(run['id'])['turn_id'])
        service.on_event({'method': 'turn/completed', 'params': {'threadId': 'thread-test', 'turnId': 'old-turn', 'turn': {'id': 'old-turn', 'status': 'completed'}}})
        assert service.store.get_run(run['id'])['status'] == 'running'
        service.cancel(run['id'])
    finally:
        service.stop()
