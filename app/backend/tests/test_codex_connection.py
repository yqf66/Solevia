"""First-install discovery and account readiness, without existing machine state."""
import os
from pathlib import Path

import pytest

from planner_api.codex_runtime import protocol
from planner_api.codex_runtime.business import BusinessCodexRuntime


@pytest.fixture
def clean_discovery(monkeypatch, tmp_path):
    monkeypatch.delenv('SOLEVIA_CODEX_EXECUTABLE', raising=False)
    monkeypatch.setenv('APPDATA', str(tmp_path / 'roaming'))
    monkeypatch.setenv('LOCALAPPDATA', str(tmp_path / 'local'))
    monkeypatch.setattr(protocol.shutil, 'which', lambda _: None)
    return tmp_path


def binary(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b'test discovery only')
    return str(path)


def test_empty_computer_has_no_codex(clean_discovery):
    assert protocol.find_codex() is None


def test_explicit_binary_precedes_path(clean_discovery, monkeypatch):
    selected = binary(clean_discovery / 'custom path' / 'codex.exe')
    monkeypatch.setenv('SOLEVIA_CODEX_EXECUTABLE', selected)
    monkeypatch.setattr(protocol.shutil, 'which', lambda _: 'other.exe')
    assert protocol.find_codex() == selected


def test_path_native_binary(clean_discovery, monkeypatch):
    selected = binary(clean_discovery / 'path' / 'codex.exe')
    monkeypatch.setattr(protocol.shutil, 'which', lambda _: selected)
    assert protocol.find_codex() == selected


@pytest.mark.skipif(os.name != 'nt', reason='Windows npm shim')
def test_npm_cmd_is_skipped_and_native_binary_used(clean_discovery, monkeypatch):
    selected = binary(clean_discovery / 'roaming/npm/node_modules/@openai/codex/node_modules/@openai/codex-win32-x64/vendor/x86_64-pc-windows-msvc/bin/codex.exe')
    monkeypatch.setattr(protocol.shutil, 'which', lambda name: None if name.endswith('.exe') else str(clean_discovery / 'codex.cmd'))
    assert protocol.find_codex() == selected


def test_desktop_latest_binary_and_invalid_override(clean_discovery, monkeypatch):
    older = binary(clean_discovery / 'local/OpenAI/Codex/bin/older/codex.exe')
    newer = binary(clean_discovery / 'local/OpenAI/Codex/bin/newer/codex.exe')
    os.utime(older, (1, 1))
    os.utime(newer, (2, 2))
    monkeypatch.setenv('SOLEVIA_CODEX_EXECUTABLE', str(clean_discovery / 'missing.exe'))
    assert protocol.find_codex() == newer


@pytest.mark.parametrize('account,ready', [
    ({'account': None, 'requiresOpenaiAuth': True}, False),
    ({'account': {'type': 'chatgpt'}, 'requiresOpenaiAuth': True}, True),
    ({'account': {'type': 'apiKey'}, 'requiresOpenaiAuth': False}, True),
    ({'account': None, 'requiresOpenaiAuth': False}, True),
    ({}, False),
])
def test_account_readiness_is_distinct_from_process_start(tmp_path, account, ready):
    class Server:
        stopped = False
        def __init__(self, **_): pass
        def start(self): pass
        def stop(self): self.stopped = True
        def call(self, method, params, **_):
            assert method == 'account/read'
            return account
    runtime = BusinessCodexRuntime(tmp_path, server_factory=Server)
    try:
        runtime.start()
        assert runtime.connected is ready
        if not ready:
            assert runtime.server.stopped
            assert '登录' in runtime.connection_error
    finally:
        runtime.stop()


def test_failed_account_probe_closes_process_and_does_not_expose_raw_error(tmp_path):
    class Server:
        stopped = False
        def __init__(self, **_): pass
        def start(self): pass
        def stop(self): self.stopped = True
        def call(self, *_args, **_kwargs): raise RuntimeError('private transport detail')
    runtime = BusinessCodexRuntime(tmp_path, server_factory=Server)
    runtime.start()
    assert not runtime.connected and runtime.server.stopped
    assert 'private' not in runtime.connection_error


@pytest.mark.parametrize('error,code,action', [
    (protocol.CodexUnavailable('private executable path', code='CODEX_NOT_FOUND'), 'CODEX_NOT_FOUND', '安装'),
    (PermissionError('private executable path'), 'CODEX_ACCESS_DENIED', '权限'),
    (TimeoutError('private transport detail'), 'CODEX_START_TIMEOUT', '超时'),
    (RuntimeError('private transport detail'), 'CODEX_START_FAILED', '正常运行'),
])
def test_start_failures_have_specific_safe_recovery_guidance(tmp_path, error, code, action):
    class Server:
        def __init__(self, **_): self.stopped = False
        def start(self): raise error
        def stop(self): self.stopped = True
    runtime = BusinessCodexRuntime(tmp_path, server_factory=Server)
    runtime.start()
    assert not runtime.connected and runtime.server.stopped
    assert runtime.connection_error_code == code
    assert action in runtime.connection_error
    assert 'private' not in runtime.connection_error


def test_transport_disconnect_replaces_previously_healthy_status(tmp_path):
    class Server:
        def __init__(self, **_): pass
    runtime = BusinessCodexRuntime(tmp_path, server_factory=Server)
    runtime.connected = True
    runtime.disconnected()
    assert not runtime.connected
    assert runtime.connection_error_code == 'CODEX_DISCONNECTED'
    assert '检测并连接' in runtime.connection_error
