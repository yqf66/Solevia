"""Installer boundaries: only the current local UI can issue fixed, consented jobs."""
import importlib.util
import json
from pathlib import Path
import threading
import time
import urllib.error
import urllib.request

import pytest

ROOT = Path(__file__).resolve().parents[3]
spec = importlib.util.spec_from_file_location('installer_bridge', ROOT / 'scripts/installer_bridge.py')
bridge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bridge)


@pytest.fixture
def installer(tmp_path):
    (tmp_path / '.runtime').mkdir()
    (tmp_path / '.runtime/services.json').write_text(json.dumps({'url': 'http://127.0.0.1:54321'}))
    (tmp_path / '.runtime/installed.json').write_text(json.dumps({'features': {'documents': True}}))
    app = bridge.Installer(tmp_path)
    app.plan = lambda features: {'plan': {'features': features}, 'space': {'enough': True}}
    return app


def request(app, path, body=None, origin='http://127.0.0.1:54321', token=None):
    req = urllib.request.Request(app.test_url + path, data=json.dumps(body).encode() if body is not None else None,
                                 headers={'Origin': origin, 'X-Solevia-Installer': app.token if token is None else token, 'Content-Type': 'application/json'})
    try:
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as error:
        return error.code, json.load(error)


@pytest.fixture
def server(installer):
    http = bridge.http.server.ThreadingHTTPServer(('127.0.0.1', 0), bridge.Handler)
    http.installer = installer
    installer.test_url = f'http://127.0.0.1:{http.server_port}'
    threading.Thread(target=http.serve_forever, daemon=True).start()
    yield installer
    http.shutdown()
    http.server_close()


def test_local_server_rejects_untrusted_origin_and_missing_token(server):
    assert request(server, '/status', origin='https://unrelated.example')[0] == 403
    assert request(server, '/status', token='')[0] == 403
    assert request(server, '/status')[0] == 200
    assert request(server, '/execute', {'command': 'anything'})[0] == 403


def test_origin_is_preserved_while_services_stop_and_change_ports(server):
    server.origin = 'http://127.0.0.1:54321'
    (server.runtime / 'services.json').unlink()
    server.publish(status='INSTALLING', bytes=2000000, speed=12345)
    code, value = request(server, '/status')
    assert code == 200 and value['speed'] == 12345
    assert request(server, '/status', origin='http://127.0.0.1:54322')[0] == 403


def test_no_installation_without_fresh_explicit_permit(server):
    body = {'features': {key: False for key in bridge.KEYS}}
    assert request(server, '/install', body)[0] == 409
    permit = server.runtime / 'installer-permit.json'
    permit.write_text(json.dumps({'token': 'chosen-token', 'expires': time.time() - 1}))
    body['permit'] = 'chosen-token'
    assert request(server, '/install', body)[0] == 409
    assert server.state['status'] == 'IDLE'


@pytest.mark.parametrize('change', [{'network': 'custom command'}, {'source': 'http://arbitrary.example'}, {'features': {'web': '1'}}, {'features': {key: 1 for key in bridge.KEYS}}])
def test_web_cannot_supply_commands_paths_or_untyped_flags(server, change):
    body = {'features': {key: False for key in bridge.KEYS}, **change}
    assert request(server, '/install', body)[0] == 409
    assert server.state['status'] == 'IDLE'


def test_space_failure_does_not_consume_permit_or_stop_services(installer):
    installer.plan = lambda features: {'space': {'enough': False}}
    (installer.runtime / 'installer-permit.json').write_text(json.dumps({'token': 'chosen-token', 'expires': time.time() + 60}))
    with pytest.raises(ValueError, match='磁盘空间不足'):
        installer.start({'permit': 'chosen-token', 'features': {key: False for key in bridge.KEYS}}, 'http://127.0.0.1:54321')
    assert installer.state['status'] == 'IDLE'


def test_progress_does_not_invent_totals_and_redacts_proxy_credentials(installer):
    installer.output('[NETWORK_PROGRESS] bytes=123456 total=-1 speed=6789')
    assert installer.snapshot()['bytes'] == 123456
    assert installer.snapshot()['total'] == -1
    assert installer.snapshot()['speed'] == 6789
    installer.output('https://' + 'user:private-password' + '@example.com/file sk-' + 'x' * 24)
    logs = str(installer.snapshot()['logs'])
    assert 'private-password' not in logs and 'x' * 24 not in logs


def test_windows_status_reader_cannot_abort_an_installation(installer, monkeypatch):
    def locked(*args):
        raise PermissionError('another reader briefly holds status file')
    monkeypatch.setattr(bridge.os, 'replace', locked)
    monkeypatch.setattr(bridge.time, 'sleep', lambda _: None)
    installer.output('[NETWORK_PROGRESS] bytes=1234 total=-1 speed=567')
    installer.output('[4/6] 安装依赖')
    assert installer.snapshot()['step'] == 4
    assert installer.snapshot()['status'] == 'IDLE'
