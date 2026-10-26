"""Authenticated local installer, kept alive while application services restart."""
import argparse
import http.server
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request

KEYS = ('web', 'documents', 'ocr', 'semantic')


def read(path, default=None):
    try:
        return json.loads(path.read_text(encoding='utf-8-sig'))
    except (OSError, ValueError):
        return default


def safe(path):
    for item in (path, *path.parents):
        if item.is_symlink() or (hasattr(item, 'is_junction') and item.is_junction()):
            raise ValueError('安装目录包含重定向，请重新选择普通目录。')
    return path


def clean(line):
    line = re.sub(r'\x1b\[[0-9;]*m', '', line)
    line = re.sub(r'(?i)(https?://)[^\s/@]+:[^\s/@]+@', r'\1[hidden]@', line)
    return re.sub(r'(?i)(?:sk-[\w-]{12,}|Bearer\s+[\w.\-]{12,})', '[hidden]', line)[:2000]


class Installer:
    def __init__(self, root):
        self.root = root
        self.runtime = safe(root / '.runtime')
        self.token = secrets.token_urlsafe(32)
        self.lock = threading.RLock()
        self.origin = None
        self.state = {'status': 'IDLE', 'stage': '选择需要补装的能力', 'step': 0, 'bytes': 0, 'speed': 0, 'total': -1, 'logs': []}
        self.started = time.monotonic()
        self.server = None

    def powershell(self, args):
        file = Path(os.environ.get('SystemRoot', 'C:/Windows')) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
        return [str(file), '-NoLogo', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-File', str(self.root / 'scripts/install.ps1'), *args]

    def environment(self):
        env = os.environ.copy()
        env['PYTHONUTF8'] = '1'
        for key in ('PSModulePath', 'OPENAI_API_KEY', 'LLM_API_KEY', 'ANTHROPIC_API_KEY', 'GH_TOKEN', 'GITHUB_TOKEN', 'NPM_TOKEN'):
            env.pop(key, None)
        return env

    def plan(self, features):
        args = ['-PlanOnly', '-Profile', 'Minimal']
        for key in KEYS:
            args += ['-' + key.capitalize(), str(int(features[key]))]
        result = subprocess.run(self.powershell(args), cwd=self.root, env=self.environment(), capture_output=True, text=True, encoding='utf-8-sig', errors='replace', timeout=30, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        if result.returncode:
            raise ValueError('无法读取安装计划，请检查安装包完整性后重试。')
        value = json.loads(result.stdout)
        required = value['plan']['diskMiB'][1] * 1024 ** 2
        target, temporary = shutil.disk_usage(self.root).free, shutil.disk_usage(tempfile.gettempdir()).free
        same = Path(tempfile.gettempdir()).anchor.lower() == self.root.anchor.lower()
        required += 2 * 1024 ** 3 if same else 0
        return {**value, 'space': {'free': target, 'required': required, 'temp_free': temporary, 'enough': target >= required and (same or temporary >= 2 * 1024 ** 3)}}

    def snapshot(self):
        with self.lock:
            return {**self.state, 'elapsed': int(time.monotonic() - self.started) if self.state['status'] != 'IDLE' else 0}

    def publish(self, **values):
        with self.lock:
            self.state.update(values)
            path = safe(self.runtime / 'installer-progress.json')
            temporary = safe(path.with_suffix('.new'))
            # A Windows UI reader may briefly hold the status file without
            # FILE_SHARE_DELETE. Progress persistence must never abort a child
            # install or close its stdout pipe when this happens.
            try:
                temporary.write_text(json.dumps(self.snapshot(), ensure_ascii=False), encoding='utf-8')
                for attempt in range(5):
                    try:
                        os.replace(temporary, path)
                        break
                    except PermissionError:
                        time.sleep(0.03 * (attempt + 1))
            except OSError:
                pass

    def output(self, line):
        line = clean(line.strip())
        match = re.match(r'^\[(DOWNLOAD|NETWORK)_PROGRESS\] bytes=(\d+) total=(-?\d+) speed=(\d+)$', line)
        if match:
            self.publish(bytes=int(match[2]), total=int(match[3]), speed=int(match[4]), traffic=match[1] == 'NETWORK')
            return
        with self.lock:
            self.state['logs'] = (self.state['logs'] + [line])[-100:]
        stage = re.match(r'^\[([1-6])/6\]', line)
        if stage:
            self.publish(stage=line, step=int(stage[1]), bytes=0, total=-1, speed=0)
        else:
            self.publish()

    def command(self, args):
        process = subprocess.Popen(args, cwd=self.root, env=self.environment(), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, text=True, encoding='utf-8-sig', errors='replace', creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        for line in process.stdout:
            self.output(line)
        return process.wait()

    def start(self, body, origin):
        features = body.get('features')
        if not isinstance(features, dict) or set(features) != set(KEYS) or any(type(features[key]) is not bool for key in KEYS):
            raise ValueError('请仅选择列表中的能力。')
        network, source = body.get('network', 'Direct'), body.get('source', 'China')
        if network not in ('Direct', 'System') or source not in ('China', 'Official'):
            raise ValueError('无效的网络或下载源选项。')
        with self.lock:
            if self.state['status'] in ('INSTALLING', 'RESTORING'):
                raise ValueError('已有补装任务在进行，请等待完成。')
            permit_path = safe(self.runtime / 'installer-permit.json')
            permit = read(permit_path, {})
            if not secrets.compare_digest(str(body.get('permit', '')), str(permit.get('token', ''))) or not permit.get('token') or permit.get('expires', 0) < time.time():
                raise ValueError('安装确认已过期，请返回工作台重新确认。')
            plan = self.plan(features)
            if not plan['space']['enough']:
                raise ValueError('磁盘空间不足，请清理安装盘和临时盘，或减少能力后重试。')
            permit_path.unlink()
            self.origin = origin
            self.started = time.monotonic()
            self.publish(status='INSTALLING', stage='正在暂停服务，资料与对话保留', step=0, logs=[], url=None, error=None, speed=0, bytes=0)
            threading.Thread(target=self.install, args=(features, network, source), daemon=True).start()

    def install(self, features, network, source):
        error = None
        try:
            launcher = [sys.executable, str(self.root / 'scripts/launcher.py')]
            if self.command([*launcher, '--stop']):
                raise ValueError('服务未能停止，请稍后重试。')
            args = ['-NonInteractive', '-Kernel', 'Keep', '-NetworkMode', network, '-DownloadSource', source]
            for key in KEYS:
                args += ['-' + key.capitalize(), str(int(features[key]))]
            if self.command(self.powershell(args)):
                raise ValueError('补装未完成。请展开日志查看失败步骤；切换下载源或网络模式后可复用缓存重试。')
        except Exception as caught:
            error = clean(str(caught))
        finally:
            self.publish(status='RESTORING', stage='正在恢复工作台服务', speed=0, error=error)
            try:
                log_path = safe(self.root / 'user-data/logs/installer-restart.log')
                with log_path.open('a', encoding='utf-8') as log:
                    subprocess.Popen([sys.executable, str(self.root / 'scripts/launcher.py'), '--no-browser', '--skip-update-check'], cwd=self.root, env=self.environment(), stdout=log, stderr=log, stdin=subprocess.DEVNULL, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                url = None
                for _ in range(120):
                    services = read(safe(self.runtime / 'services.json'), {})
                    candidate = services.get('url', '')
                    if re.fullmatch(r'http://127\.0\.0\.1:\d{1,5}', candidate):
                        try:
                            with opener.open(candidate + '/_solevia/health', timeout=1) as response:
                                if response.status == 200:
                                    url = candidate
                                    break
                        except OSError:
                            pass
                    time.sleep(1)
                if not url:
                    raise ValueError('工作台尚未恢复，请从桌面工作台启动服务并查看日志。')
                self.publish(status='FAILED' if error else 'COMPLETE', stage='补装未完成，工作台已恢复' if error else '补装完成，工作台已恢复', error=error, url=url, speed=0)
            except Exception as caught:
                self.publish(status='FAILED', stage='请检查服务恢复状态', error=(error or '') + ' ' + clean(str(caught)), speed=0)


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def allowed(self):
        app = self.server.installer
        services = read(safe(app.runtime / 'services.json'), {})
        origin = self.headers.get('Origin', '')
        return origin and origin in (services.get('url'), app.origin) and secrets.compare_digest(self.headers.get('X-Solevia-Installer', ''), app.token)

    def reply(self, code, value):
        data = json.dumps(value, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Access-Control-Allow-Origin', self.headers.get('Origin', 'null'))
        self.send_header('Vary', 'Origin')
        self.end_headers()
        self.wfile.write(data)

    def do_OPTIONS(self):
        app = self.server.installer
        origin = self.headers.get('Origin', '')
        if origin not in (read(app.runtime / 'services.json', {}).get('url'), app.origin) or not origin:
            self.send_error(403)
            return
        self.send_response(204)
        self.send_header('Access-Control-Allow-Origin', origin)
        self.send_header('Access-Control-Allow-Methods', 'GET, POST')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type, X-Solevia-Installer')
        self.end_headers()

    def do_GET(self):
        if not self.allowed():
            self.reply(403, {'message': '安装连接无效，请从 Solevia 工作台打开能力管理。'})
            return
        app = self.server.installer
        if self.path == '/status':
            self.reply(200, app.snapshot())
        elif self.path == '/plan':
            try:
                features = read(safe(app.runtime / 'installed.json'), {}).get('features', {})
                self.reply(200, app.plan({key: bool(features.get(key)) for key in KEYS}))
            except Exception as caught:
                self.reply(409, {'message': clean(str(caught))})
        else:
            self.reply(404, {})

    def do_POST(self):
        if not self.allowed() or self.path not in ('/install', '/plan'):
            self.reply(403, {'message': '无效的安装请求。'})
            return
        try:
            size = int(self.headers.get('Content-Length', '0'))
            if not 0 < size <= 8192:
                raise ValueError('安装请求大小无效。')
            body = json.loads(self.rfile.read(size))
            if self.path == '/plan':
                features = body.get('features')
                if not isinstance(features, dict) or set(features) != set(KEYS) or any(type(v) is not bool for v in features.values()):
                    raise ValueError('能力选项无效。')
                self.reply(200, self.server.installer.plan(features))
            else:
                self.server.installer.start(body, self.headers['Origin'])
                self.reply(200, self.server.installer.snapshot())
        except (ValueError, OSError, subprocess.SubprocessError) as caught:
            self.reply(409, {'message': clean(str(caught))})


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--parent', type=int, required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parent.parent
    app = Installer(root)
    server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    server.daemon_threads = True
    server.installer = app
    app.server = server
    app.publish()
    safe(app.runtime / 'installer-bridge.json').write_text(json.dumps({'url': f'http://127.0.0.1:{server.server_port}', 'token': app.token}), encoding='utf-8')

    def parent_watch():
        import ctypes
        kernel = ctypes.windll.kernel32
        kernel.OpenProcess.restype = ctypes.c_void_p
        kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        handle = kernel.OpenProcess(0x100000, False, args.parent)
        if handle:
            kernel.WaitForSingleObject(handle, 0xFFFFFFFF)
            kernel.CloseHandle(handle)
        while app.snapshot()['status'] in ('INSTALLING', 'RESTORING'):
            time.sleep(1)
        server.shutdown()
    threading.Thread(target=parent_watch, daemon=True).start()
    server.serve_forever()
    server.server_close()


if __name__ == '__main__':
    main()
