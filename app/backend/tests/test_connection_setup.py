"""Installer choices must never replace credentials or reset a repaired installation."""
import importlib.util
from pathlib import Path
import json
import os
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[3]
SCRIPTS = ROOT / 'scripts'
spec = importlib.util.spec_from_file_location('connection_setup', SCRIPTS / 'connection_setup.py')
setup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(setup)


def test_keep_is_a_true_noop_even_without_installation(tmp_path):
    root = tmp_path / 'not installed'
    assert setup.select_kernel(root, 'keep') is False
    assert not root.exists()


@pytest.mark.parametrize('mode', ['codex', 'api'])
def test_switch_preserves_both_profiles_and_all_other_settings(tmp_path, mode):
    config = tmp_path / 'user-data/.env'
    config.parent.mkdir()
    before = b'# private settings\r\nLLM_PROVIDER=openai_compatible\r\nLLM_API_KEY="local-test-secret"\r\nSOLEVIA_KERNEL=api\r\nLLM_MODEL=test-model\r\nCODEX_REASONING_EFFORT=high\r\n'
    config.write_bytes(before)
    setup.select_kernel(tmp_path, mode)
    assert config.read_bytes() == before.replace(b'SOLEVIA_KERNEL=api', ('SOLEVIA_KERNEL=' + mode).encode()) + ("SOLEVIA_CODEX_CONSENT=" + ('1' if mode == 'codex' else '0') + '\r\n').encode()
    assert not list(config.parent.glob('.connection-*'))


def test_duplicate_exported_mode_cannot_override_explicit_choice(tmp_path):
    config = tmp_path / 'user-data/.env'
    config.parent.mkdir()
    config.write_bytes(b'export SOLEVIA_KERNEL="api"\n# note\n SOLEVIA_KERNEL=api\nOTHER=value')
    setup.select_kernel(tmp_path, 'codex')
    assert config.read_text() == 'SOLEVIA_KERNEL=codex\n# note\nOTHER=value\nSOLEVIA_CODEX_CONSENT=1\n'


def test_missing_mode_is_appended_without_merging_lines(tmp_path):
    config = tmp_path / 'user-data/.env'
    config.parent.mkdir()
    config.write_text('LLM_MODEL=existing')
    setup.select_kernel(tmp_path, 'codex')
    assert config.read_text() == 'LLM_MODEL=existing\nSOLEVIA_KERNEL=codex\nSOLEVIA_CODEX_CONSENT=1\n'


def test_failure_before_atomic_replace_preserves_original(monkeypatch, tmp_path):
    config = tmp_path / 'user-data/.env'
    config.parent.mkdir()
    config.write_bytes(b'SOLEVIA_KERNEL=api\nLLM_API_KEY=local-test-secret\n')
    before = config.read_bytes()
    def fail(*_args):
        raise PermissionError('test refused')
    monkeypatch.setattr(setup.os, 'replace', fail)
    with pytest.raises(PermissionError):
        setup.select_kernel(tmp_path, 'codex')
    assert config.read_bytes() == before
    assert not list(config.parent.glob('.connection-*'))


@pytest.mark.parametrize('mode', ['', 'Codex', 'all', 'codex\nLLM_API_KEY=bad'])
def test_invalid_mode_never_creates_config(tmp_path, mode):
    with pytest.raises(ValueError):
        setup.select_kernel(tmp_path, mode)
    assert not list(tmp_path.iterdir())


@pytest.mark.skipif(os.name != 'nt', reason='Windows junction boundary')
def test_redirected_data_directory_is_rejected(tmp_path):
    outside = tmp_path / 'outside'; outside.mkdir()
    root = tmp_path / 'product'; root.mkdir()
    result = subprocess.run(['cmd', '/c', 'mklink', '/J', str(root / 'user-data'), str(outside)], capture_output=True)
    assert result.returncode == 0
    with pytest.raises(ValueError, match='Redirected'):
        setup.select_kernel(root, 'codex')
    assert not list(outside.iterdir())


def test_plan_only_reports_choice_without_mutating_installation(tmp_path):
    import test_installer_guidance as guidance
    root = guidance.installer_fixture(tmp_path)
    plan = json.loads(guidance.run_installer(root, '-PlanOnly', '-Kernel Codex').stdout)
    assert plan['kernel'] == 'Codex'
    assert not (root / 'user-data').exists()
    assert json.loads(guidance.run_installer(root, '-PlanOnly').stdout)['kernel'] == 'Keep'


def test_cli_does_not_print_profile_values(tmp_path):
    import sys
    config = tmp_path / 'user-data/.env'; config.parent.mkdir()
    config.write_text('LLM_API_KEY=local-test-secret\n')
    result = subprocess.run([sys.executable, '-X', 'utf8', str(SCRIPTS / 'connection_setup.py'), '--root', str(tmp_path), '--kernel', 'codex'], capture_output=True, text=True, encoding='utf8')
    assert result.returncode == 0
    assert 'local-test-secret' not in result.stdout + result.stderr
    assert str(tmp_path) not in result.stdout + result.stderr


def test_skip_connection_revokes_prior_installer_consent(tmp_path):
    config = tmp_path / 'user-data/.env'
    config.parent.mkdir()
    config.write_text('SOLEVIA_KERNEL=codex\nSOLEVIA_CODEX_CONSENT=1\nLLM_API_KEY=unchanged\n')
    setup.select_kernel(tmp_path, 'api')
    assert config.read_text() == 'SOLEVIA_KERNEL=api\nLLM_API_KEY=unchanged\nSOLEVIA_CODEX_CONSENT=0\n'
