import json
import os
import time
from types import SimpleNamespace
from fastapi.testclient import TestClient
from planner_api.main import create_app


def client_for(root, monkeypatch):
    monkeypatch.setenv("SOLEVIA_DESKTOP_ROOT", str(root))
    runtime = root / ".runtime"
    runtime.mkdir()
    (runtime / "installed.json").write_text(json.dumps({"features": {"documents": True}}))
    app = create_app()
    app.state.kernel.busy_check = lambda: False
    return TestClient(app), runtime


def test_shell_is_honest_about_missing_optional_dependencies(tmp_path, monkeypatch):
    client, runtime = client_for(tmp_path, monkeypatch)
    state = client.get("/api/settings/capabilities").json()
    assert state == {"desktop_available": False, "installed": {"web": False, "documents": True, "ocr": False, "semantic": False}}
    assert client.post("/api/settings/capabilities/manage").status_code == 409
    assert not (runtime / "desktop-request.json").exists()


def test_handoff_is_a_fixed_action_and_requires_a_live_desktop(tmp_path, monkeypatch):
    client, runtime = client_for(tmp_path, monkeypatch)
    heartbeat = runtime / "desktop-host.json"
    heartbeat.write_text("{}")
    response = client.post("/api/settings/capabilities/manage")
    assert response.status_code == 200, response.text
    command = json.loads((runtime / "desktop-request.json").read_text())
    assert set(command) == {"action", "id", "expires"}
    assert command["action"] == "manage-capabilities"
    assert time.time() < command["expires"] < time.time() + 31
    assert client.post("/api/settings/capabilities/manage").status_code == 409
    os.utime(heartbeat, (time.time() - 60,) * 2)
    assert client.get("/api/settings/capabilities").json()["desktop_available"] is False


def test_handoff_waits_for_active_generation(tmp_path, monkeypatch):
    client, runtime = client_for(tmp_path, monkeypatch)
    (runtime / "desktop-host.json").write_text("{}")
    client.app.state.kernel.active = 1
    assert client.post("/api/settings/capabilities/manage").status_code == 409
    assert not (runtime / "desktop-request.json").exists()


def test_web_install_prepare_requires_live_local_bridge_and_no_active_work(tmp_path, monkeypatch):
    client, runtime = client_for(tmp_path, monkeypatch)
    (runtime / 'desktop-host.json').write_text('{}')
    assert client.post('/api/settings/capabilities/prepare').status_code == 409
    (runtime / 'installer-bridge.json').write_text(json.dumps({'url': 'https://remote.example', 'token': 'a' * 43}))
    assert client.post('/api/settings/capabilities/prepare').status_code == 409
    (runtime / 'installer-bridge.json').write_text(json.dumps({'url': 'http://127.0.0.1:54322', 'token': 'a' * 43}))
    client.app.state.kernel.active = 1
    assert client.post('/api/settings/capabilities/prepare').status_code == 409
    assert not (runtime / 'installer-permit.json').exists()
    client.app.state.kernel.active = 0
    response = client.post('/api/settings/capabilities/prepare')
    assert response.status_code == 200
    permit = json.loads((runtime / 'installer-permit.json').read_text())
    assert response.json()['permit'] == permit['token']
    assert time.time() < permit['expires'] < time.time() + 61
