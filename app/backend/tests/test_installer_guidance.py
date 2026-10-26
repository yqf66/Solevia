"""Exercise the Windows installer before it downloads or changes a runtime."""
import json
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[3]
POWERSHELL = shutil.which('powershell.exe')
pytestmark = pytest.mark.skipif(not POWERSHELL, reason='Windows installer test')


def installer_fixture(tmp_path):
    root = tmp_path / '中文安装目录'
    shutil.copytree(ROOT, root, ignore=shutil.ignore_patterns('.git', '.runtime', 'user-data', 'node_modules', '.venv', '__pycache__', '.pytest_cache', '.dev-logs'))
    (root / 'MANIFEST.json').write_text('{"files": []}', encoding='utf8')
    return root


def run_installer(root, *args, answers=None):
    # Force a known console encoding when stdout is captured by pytest.
    script = root / 'scripts/install.ps1'
    command = "[Console]::OutputEncoding=[Text.UTF8Encoding]::new(); "
    if answers is not None:
        entries = answers.splitlines()
        values = ','.join("'" + value.replace("'", "''") + "'" for value in entries)
        command += "$global:testAnswers=[Collections.Generic.Queue[string]]::new(); @(" + values + ") | ForEach-Object {$global:testAnswers.Enqueue($_)}; function Read-Host {param($Prompt) Write-Host $Prompt; if($global:testAnswers.Count -eq 0){throw 'Test input exhausted'}; return $global:testAnswers.Dequeue()}; "
    command += "& '" + str(script).replace("'", "''") + "' " + ' '.join(args)
    return subprocess.run([POWERSHELL, '-NoProfile', '-ExecutionPolicy', 'Bypass', '-Command', command],
                          input=answers, text=True, encoding='utf8', capture_output=True, timeout=30)


def test_minimal_plan_explains_capabilities_without_installing(tmp_path):
    root = installer_fixture(tmp_path)
    result = run_installer(root, '-PlanOnly')
    assert result.returncode == 0, result.stderr
    plan = json.loads(result.stdout)
    assert plan['plan']['features'] == dict(web=False, documents=False, ocr=False, semantic=False)
    assert plan['plan']['estimateOnly'] and plan['plan']['includesCaches']
    assert len(plan['capabilities']) == 4
    assert all(row['Benefit'] and row['Skip'] and row['Memory'] for row in plan['capabilities'])
    assert not (root / '.runtime').exists()


def test_cancel_validates_answer_and_never_downloads(tmp_path):
    root = installer_fixture(tmp_path)
    result = run_installer(root, answers='what\n是\n\n否\n\nn\n')
    assert result.returncode == 0, result.stderr
    assert '请输入 Y 或 N' in result.stdout
    assert '本次安装内容' in result.stdout and '已取消' in result.stdout
    assert '首次下载粗估' in result.stdout and '磁盘粗估' in result.stdout
    assert not (root / '.runtime').exists()
    assert not (root / 'user-data').exists()


def test_reselect_keeps_previous_capabilities_and_cancellation_preserves_state(tmp_path):
    root = installer_fixture(tmp_path)
    runtime = root / '.runtime'
    runtime.mkdir()
    state = runtime / 'installed.json'
    previous = json.dumps({'features': dict(web=False, documents=True, ocr=True, semantic=False)})
    state.write_text(previous)
    result = run_installer(root, '-PlanOnly', '-ChooseFeatures')
    assert json.loads(result.stdout)['plan']['features']['ocr'] is True
    result = run_installer(root, '-ChooseFeatures', answers='\n\n\n\nn\n')
    assert result.returncode == 0, result.stderr
    assert '默认：否' in result.stdout and '默认：是' in result.stdout
    assert state.read_text() == previous


def test_browser_increment_and_full_profile_totals(tmp_path):
    root = installer_fixture(tmp_path)
    command = "[Console]::OutputEncoding=[Text.UTF8Encoding]::new(); . '" + str(root / 'scripts/install-options.ps1').replace("'", "''") + "'; $f=@{web=$true;documents=$false;ocr=$false;semantic=$false}; @((Get-SoleviaInstallPlan $f $false),(Get-SoleviaInstallPlan $f $true)) | ConvertTo-Json -Depth 4"
    result = subprocess.run([POWERSHELL, '-NoProfile', '-Command', command], capture_output=True, text=True, encoding='utf8', timeout=30)
    plans = json.loads(result.stdout)
    assert plans[1]['downloadMiB'][0] - plans[0]['downloadMiB'][0] == 150
    assert plans[1]['diskMiB'][1] - plans[0]['diskMiB'][1] == 700
    full = json.loads(run_installer(root, '-PlanOnly', '-Profile Full').stdout)
    assert all(full['plan']['features'].values())
    assert full['plan']['downloadMiB'][0] >= 1020


def network_script(root, command):
    prefix = "[Console]::OutputEncoding=[Text.UTF8Encoding]::new(); . '" + str(root / 'scripts/common.ps1').replace("'", "''") + "'; "
    return subprocess.run([POWERSHELL, '-NoProfile', '-ExecutionPolicy', 'Bypass', '-Command', prefix + command],
                          capture_output=True, text=True, encoding='utf8', timeout=30)


def test_direct_mode_ignores_stale_proxy_and_sets_selected_mirrors(tmp_path):
    root = installer_fixture(tmp_path)
    result = network_script(root, "$env:HTTPS_PROXY='http://invalid.test'; Set-SoleviaInstallNetwork Direct China; [pscustomobject]@{proxy=$env:HTTPS_PROXY;index=$env:UV_DEFAULT_INDEX;npm=$env:npm_config_registry;models=$env:HF_ENDPOINT;direct=([Net.WebRequest]::DefaultWebProxy.GetProxy([Uri]'https://example.test').Host -eq 'example.test')} | ConvertTo-Json -Compress")
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout.splitlines()[-1])
    assert not data['proxy'] and data['direct']
    assert data['index'] == 'https://pypi.tuna.tsinghua.edu.cn/simple'
    assert data['npm'] == 'https://registry.npmmirror.com'
    assert data['models'] == 'https://hf-mirror.com'


def test_official_source_overrides_inherited_mirror(tmp_path):
    root = installer_fixture(tmp_path)
    result = network_script(root, "$env:HF_ENDPOINT='https://old.invalid'; Set-SoleviaInstallNetwork Direct Official; Write-Output $env:HF_ENDPOINT; Write-Output $env:UV_DEFAULT_INDEX")
    assert result.returncode == 0
    assert result.stdout.splitlines()[-2:] == ['https://huggingface.co', 'https://pypi.org/simple']


def test_system_mode_applies_environment_proxy_without_printing_credentials(tmp_path):
    root = installer_fixture(tmp_path)
    proxy = 'http://' + 'test-account:test-secret' + '@127.0.0.1:18080'
    result = network_script(root, "$env:HTTPS_PROXY='" + proxy + "'; Set-SoleviaInstallNetwork System Official; Write-Output ([Net.WebRequest]::DefaultWebProxy.GetProxy([Uri]'https://example.test').Port)")
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines()[-1] == '18080'
    assert 'test-secret' not in result.stdout + result.stderr


def test_unavailable_proxy_reports_connection_failure_and_keeps_file_uninstalled(tmp_path):
    import socket
    root = installer_fixture(tmp_path)
    with socket.socket() as bound:
        bound.bind(('127.0.0.1', 0)); port = bound.getsockname()[1]
    command = "$env:HTTPS_PROXY='http://127.0.0.1:" + str(port) + "'; Set-SoleviaInstallNetwork System Official; Get-VerifiedDownload 'https://example.test/file' '" + str(root / 'not-installed').replace("'", "''") + "' '" + '0' * 64 + "' 'test component'"
    result = network_script(root, command)
    assert result.returncode != 0
    assert '[重试]' in result.stdout
    assert 'NETWORK_DOWNLOAD' in result.stdout + result.stderr or 'NETWORK_PROXY' in result.stdout + result.stderr
    assert '代理' in result.stdout + result.stderr
    assert not (root / 'not-installed').exists()


@pytest.mark.parametrize(('message', 'code'), [('Connection timed out', 'NETWORK_DOWNLOAD'), ('proxy unavailable', 'NETWORK_PROXY'), ('certificate invalid', 'NETWORK_TLS'), ('No space left on device', 'DISK_SPACE'), ('Access is denied', 'PERMISSION'), ('out of memory', 'MEMORY'), ('checksum mismatch', 'HASH_MISMATCH')])
def test_failure_classification_keeps_actionable_advice(tmp_path, message, code):
    root = installer_fixture(tmp_path)
    result = network_script(root, "Get-SoleviaFailure '" + message + "' | ConvertTo-Json -Compress")
    failure = json.loads(result.stdout)
    assert failure['code'] == code and len(failure['advice']) > 20


def test_native_tool_failure_preserves_original_cause(tmp_path):
    root = installer_fixture(tmp_path)
    result = network_script(root, "Invoke-Checked $env:ComSpec @('/c','echo Connection timed out & exit /b 7')")
    assert result.returncode != 0
    assert 'Connection timed out' in result.stdout + result.stderr
    assert 'NETWORK_DOWNLOAD' in result.stdout + result.stderr


@pytest.mark.parametrize('mode', ['slow', 'corrupt', 'retry', 'unknown-size'])
def test_stream_download_reports_real_bytes_speed_slow_connection_and_checks_hash(tmp_path, mode):
    """Exercise production HTTPS streaming against a local, throttled TLS server."""
    import datetime
    import hashlib
    import http.server
    import ipaddress
    import ssl
    import threading
    import time
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    root = installer_fixture(tmp_path)
    corrupt = mode == 'corrupt'
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'localhost')])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - datetime.timedelta(minutes=1))
            .not_valid_after(now + datetime.timedelta(days=1))
            .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address('127.0.0.1'))]), critical=False)
            .sign(key, hashes.SHA256()))
    cert_file = tmp_path / 'server.pem'; key_file = tmp_path / 'server.key'
    cert_file.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_file.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    payload = b'Solevia-download-test-' * 4096
    class Handler(http.server.BaseHTTPRequestHandler):
        attempts = 0
        def do_GET(self):
            Handler.attempts += 1
            if mode == 'retry' and Handler.attempts == 1:
                self.send_response(503); self.end_headers(); return
            self.send_response(200)
            if mode != 'unknown-size':
                self.send_header('Content-Length', str(len(payload)))
            self.end_headers()
            for index in range(0, len(payload), 4096):
                self.wfile.write(payload[index:index + 4096]); self.wfile.flush()
                if mode == 'slow':
                    time.sleep(.55)
        def log_message(self, *_args):
            pass
    server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER); context.load_cert_chain(cert_file, key_file)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    # Trust exactly this ephemeral local test certificate, inside this test child only.
    thumbprint = cert.fingerprint(hashes.SHA1()).hex().upper()
    destination = root / 'download.test'
    digest = hashlib.sha256(payload if not corrupt else b'wrong').hexdigest()
    trust = "Add-Type 'using System;using System.Net;public static class TestTls {public static void Trust(){ServicePointManager.ServerCertificateValidationCallback=(s,c,ch,e)=>c.GetCertHashString()==\"" + thumbprint + "\";}}'; [TestTls]::Trust(); "
    command = "Set-SoleviaInstallNetwork Direct Official; " + trust + "Get-VerifiedDownload 'https://127.0.0.1:" + str(server.server_port) + "/file' '" + str(destination).replace("'", "''") + "' '" + digest + "' 'test component'"
    try:
        result = network_script(root, command)
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=3)
    if corrupt:
        assert result.returncode != 0
        assert 'HASH_MISMATCH' in result.stdout + result.stderr
        assert not destination.exists()
    else:
        assert result.returncode == 0, result.stdout + result.stderr
        assert destination.read_bytes() == payload
        assert '[DOWNLOAD_PROGRESS]' in result.stdout and 'speed=' in result.stdout
        if mode == 'slow':
            assert '[网速提示]' in result.stdout
        if mode == 'retry':
            assert '[重试]' in result.stdout and Handler.attempts == 2
        assert 'total=' + str(len(payload)) in result.stdout


def test_insufficient_target_disk_space_reports_actual_drive_and_required_capacity(tmp_path):
    root = installer_fixture(tmp_path)
    command = "$d=[IO.DriveInfo]::new([IO.Path]::GetPathRoot($SoleviaRoot)); Assert-SoleviaInstallSpace (($d.AvailableFreeSpace/1MB)+10240)"
    result = network_script(root, command)
    assert result.returncode != 0
    assert '[DISK_SPACE]' in result.stdout + result.stderr
    assert 'GiB' in result.stdout + result.stderr
    assert not (root / '.runtime').exists()
