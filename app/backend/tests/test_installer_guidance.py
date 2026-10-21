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


def test_standard_plan_explains_capabilities_without_installing(tmp_path):
    root = installer_fixture(tmp_path)
    result = run_installer(root, '-PlanOnly')
    assert result.returncode == 0, result.stderr
    plan = json.loads(result.stdout)
    assert plan['plan']['features'] == dict(web=True, documents=True, ocr=False, semantic=False)
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
