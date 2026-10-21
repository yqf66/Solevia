"""Desktop adapter contracts and the actual EXE archive validator on Windows."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import zipfile

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / 'scripts'))
import desktop_worker


def test_update_check_compares_stable_versions_and_handles_no_release(tmp_path, monkeypatch):
    monkeypatch.setattr(desktop_worker.updater, 'ROOT', tmp_path)
    (tmp_path / 'release.json').write_text('{"version":"0.1.2"}')
    monkeypatch.setattr(desktop_worker.updater, 'latest_release', lambda: {'tag_name': 'v0.1.3'})
    assert desktop_worker.check() == {'available': True, 'tag': 'v0.1.3'}
    monkeypatch.setattr(desktop_worker.updater, 'latest_release', lambda: {'tag_name': 'v0.1.2'})
    assert desktop_worker.check()['available'] is False
    monkeypatch.setattr(desktop_worker.updater, 'latest_release', lambda: None)
    assert desktop_worker.check()['available'] is False


def test_confirmed_update_rejects_changed_release_before_download(monkeypatch):
    monkeypatch.setattr(desktop_worker.updater, 'latest_release', lambda: {'tag_name': 'v0.2.0'})
    monkeypatch.setattr(desktop_worker.updater, 'download_release', lambda *_: pytest.fail('must not download'))
    with pytest.raises(RuntimeError, match='重新检查'):
        desktop_worker.apply('v0.1.3')


@pytest.fixture
def executable():
    release = ROOT / 'distribution/release.json'
    if not release.exists():
        release = ROOT / 'release.json'
    version = json.loads(release.read_text(encoding='utf8'))['version']
    exe = Path(os.environ.get('SOLEVIA_TEST_INSTALLER', str(ROOT / ('release-output/Solevia-Setup-' + version + '-windows-x64.exe'))))
    if os.name != 'nt' or not exe.exists():
        pytest.skip('Build the Windows installer to run native archive validation')
    return exe


def test_native_validator_accepts_exact_source_package(executable):
    package = executable.with_name(executable.name.replace('Solevia-Setup-', 'Solevia-').replace('.exe', '.zip'))
    assert subprocess.run([str(executable), '--verify-package', str(package)], timeout=30).returncode == 0


@pytest.mark.parametrize('name', ['../outside.txt', 'user-data/.env', 'desktop/extra.exe', 'app/CON.txt'])
def test_native_validator_blocks_unlisted_and_unsafe_entries(executable, tmp_path, name):
    source = executable.with_name(executable.name.replace('Solevia-Setup-', 'Solevia-').replace('.exe', '.zip'))
    package = tmp_path / 'modified.zip'
    package.write_bytes(source.read_bytes())
    with zipfile.ZipFile(package, 'a') as target:
        target.writestr('Solevia/' + name, b'not part of the product')
    assert subprocess.run([str(executable), '--verify-package', str(package)], timeout=30).returncode != 0


def test_explicit_gui_features_override_saved_defaults(tmp_path):
    import test_installer_guidance as guidance
    root = guidance.installer_fixture(tmp_path)
    runtime = root / '.runtime'
    runtime.mkdir()
    (runtime / 'installed.json').write_text(json.dumps({'features': dict(web=True, documents=True, ocr=False, semantic=False)}))
    result = guidance.run_installer(root, '-PlanOnly', '-Web 0', '-Documents 0', '-Ocr 1', '-Semantic 1')
    assert json.loads(result.stdout)['plan']['features'] == dict(web=False, documents=False, ocr=True, semantic=True)
