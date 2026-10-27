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
    release = ROOT / 'release.json'
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


def test_installer_can_extract_beside_itself_without_overwriting_other_files(executable, tmp_path):
    import hashlib
    import shutil
    target = tmp_path / '中文 安装器所在目录'
    target.mkdir()
    local_exe = target / executable.name
    shutil.copyfile(executable, local_exe)
    marker = tmp_path / 'done.txt'
    # First execution and hundreds of extracted files can trigger Windows scans.
    result = subprocess.run([str(local_exe), '--extract-package', str(target), str(marker)], timeout=120)
    assert result.returncode == 0 and marker.exists()
    cache = target / '.runtime/downloads/uv.zip'
    assert hashlib.sha256(cache.read_bytes()).hexdigest() == 'bb701bd35b586c45966654ea2703c9bd116a6a4fc0a8ca755bd644c2c41c5ebe'
    assert local_exe.read_bytes() == executable.read_bytes()
    blocked = tmp_path / 'other'; blocked.mkdir()
    (blocked / 'unrelated.txt').write_text('keep')
    assert subprocess.run([str(executable), '--extract-package', str(blocked), str(tmp_path / 'blocked.txt')], timeout=120).returncode != 0
    assert (blocked / 'unrelated.txt').read_text() == 'keep'
    assert not (blocked / 'release.json').exists()


def test_embedded_tool_bootstraps_without_network(executable, tmp_path):
    import hashlib
    import shutil
    import test_installer_guidance as guidance
    target = tmp_path / 'offline-tool';marker = tmp_path / 'extracted'
    assert subprocess.run([str(executable), '--extract-package', str(target), str(marker)], timeout=120).returncode == 0
    # No runtime has been installed. A matching embedded archive is the only tool source.
    result = guidance.run_installer(target, '-NonInteractive', '-Profile Minimal', '-BootstrapOnly')
    assert result.returncode == 0, result.stdout + result.stderr
    assert '[缓存]' in result.stdout and '诊断模式到此结束' in result.stdout
    assert (target / '.runtime/uv/uv.exe').is_file()
    assert not (target / '.runtime/python').exists()


@pytest.fixture
def native_process_driver(tmp_path):
    if os.name != 'nt':
        pytest.skip('Windows PowerShell runner')
    compiler = Path(os.environ['WINDIR']) / 'Microsoft.NET/Framework64/v4.0.30319/csc.exe'
    source_root = ROOT / 'desktop'
    if not source_root.exists():
        source_root = ROOT / 'desktop'
    driver = tmp_path / 'Driver.cs'
    driver.write_text('''using System;using System.Threading.Tasks;using Solevia.Desktop;
class Driver { static int Main(string[] args) { return ProcessRunner.Run("powershell.exe",ProcessRunner.PowerShell(args[0],""),args[1],s=>Console.WriteLine(InstallDiagnostics.Clean(s))).GetAwaiter().GetResult(); } }''')
    target = tmp_path / 'Driver.exe'
    subprocess.run([str(compiler), '/nologo', '/target:exe', '/out:' + str(target), str(driver),
                    str(source_root / 'ProcessRunner.cs'), str(source_root / 'InstallDiagnostics.cs')], check=True, capture_output=True)
    return target


@pytest.mark.parametrize('fail', [False, True])
def test_native_runner_outputs_plain_text_preserves_exit_and_redacts_identity(native_process_driver, tmp_path, fail):
    script = tmp_path / 'output.ps1'
    script.write_text("Write-Host 'hello installer'; Write-Host $env:USERPROFILE; Write-Host $env:COMPUTERNAME; " + ("throw 'Connection timed out'" if fail else ''), encoding='utf-8-sig')
    result = subprocess.run([str(native_process_driver), str(script), str(tmp_path)], text=True, encoding='utf8', capture_output=True, timeout=20)
    assert (result.returncode != 0) == fail, result.stdout + result.stderr
    assert 'hello installer' in result.stdout
    assert 'CLIXML' not in result.stdout + result.stderr
    assert 'PSComputerName' not in result.stdout + result.stderr
    assert os.environ['USERPROFILE'] not in result.stdout
    assert os.environ['COMPUTERNAME'] not in result.stdout
    if fail:
        assert 'Connection timed out' in result.stdout
