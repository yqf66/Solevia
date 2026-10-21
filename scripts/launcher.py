"""Local release supervisor. No dependency on the developer's environment."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time
import urllib.request
import uuid
import webbrowser

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / '.runtime'
DATA = ROOT / 'user-data'
STATE = RUNTIME / 'services.json'
STOP = RUNTIME / 'stop.request'


def runtime_environment(features, ports=None):
    allowed = {'SYSTEMROOT','WINDIR','PATH','PATHEXT','COMSPEC','SYSTEMDRIVE',
               'PROGRAMFILES','PROGRAMFILES(X86)','LOCALAPPDATA','APPDATA','TEMP','TMP','LANG'}
    env = {key:value for key,value in os.environ.items() if key.upper() in allowed}
    env.update(SOLEVIA_DATA_DIR=str(DATA), SOLEVIA_ENV_FILE=str(DATA/'.env'),
               PYTHONUTF8='1', PYTHONNOUSERSITE='1', PYTHONUNBUFFERED='1',
               PLAYWRIGHT_BROWSERS_PATH=str(RUNTIME/'browsers'), HF_HOME=str(RUNTIME/'models'))
    env['PATH'] = str(RUNTIME/'node') + os.pathsep + env.get('PATH','')
    if features.get('web'):
        env.update(WEB_SEARCH_PROVIDER='open_websearch', WEB_RESEARCH_HOST='127.0.0.1',
                   OPEN_WEBSEARCH_HOST='127.0.0.1', WEB_RESEARCH_READER='crawl4ai')
        if ports:
            env.update(WEB_SEARCH_BASE_URL=f"http://127.0.0.1:{ports['web']}",
                       WEB_RESEARCH_PORT=str(ports['web']), OPEN_WEBSEARCH_PORT=str(ports['search']),
                       CRAWL4AI_PYTHON=sys.executable, SOLEVIA_WEB_ARCHIVES=str(DATA/'research-archives'))
            browser=next(iter(sorted((RUNTIME/'browsers').glob('chromium-*/chrome-win*/chrome.exe'),reverse=True)),None)
            if browser is None:
                candidates=[Path(os.environ.get(key,''))/suffix for key,suffix in [
                    ('PROGRAMFILES(X86)','Microsoft/Edge/Application/msedge.exe'),
                    ('PROGRAMFILES','Microsoft/Edge/Application/msedge.exe'),
                    ('PROGRAMFILES','Google/Chrome/Application/chrome.exe'),
                    ('PROGRAMFILES(X86)','Google/Chrome/Application/chrome.exe'),
                    ('LOCALAPPDATA','Google/Chrome/Application/chrome.exe')]]
                browser=next((path for path in candidates if path.is_file()),None)
            if browser:env['PLAYWRIGHT_EXECUTABLE_PATH']=str(browser)
    env['MATERIAL_OCR_ENABLED'] = str(bool(features.get('ocr'))).lower()
    env['MATERIAL_SEMANTIC_ENABLED'] = str(bool(features.get('semantic'))).lower()
    return env


def load_features(state_file=None):
    state = Path(state_file) if state_file else RUNTIME/'installed.json'
    return json.loads(state.read_text(encoding='utf-8-sig'))['features']


def available_ports(count):
    sockets=[]
    try:
        for _ in range(count):
            item=socket.socket();item.bind(('127.0.0.1',0));sockets.append(item)
        return [item.getsockname()[1] for item in sockets]
    finally:
        for item in sockets:item.close()


def read_state():
    try:return json.loads(STATE.read_text(encoding='utf8'))
    except (OSError,ValueError):return None


def ping(url):
    try:
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(url,timeout=3) as response:
            return response.status==200
    except Exception:return False


def request_stop():
    state=read_state()
    if state:
        unlocked=acquire_lock()
        if unlocked is not None:
            STATE.unlink(missing_ok=True);STOP.unlink(missing_ok=True);unlocked.close();return True
        STOP.write_text(state['token'],encoding='utf8')
        for _ in range(100):
            if not STATE.exists():return True
            time.sleep(.1)
        raise RuntimeError('Services are still stopping. Close the Start window and retry.')
    return True


def acquire_lock():
    RUNTIME.mkdir(parents=True,exist_ok=True)
    handle=(RUNTIME/'supervisor.lock').open('a+b')
    handle.seek(0);handle.write(b'0');handle.flush();handle.seek(0)
    try:
        if os.name=='nt':
            import msvcrt
            msvcrt.locking(handle.fileno(),msvcrt.LK_NBLCK,1)
        else:
            import fcntl
            fcntl.flock(handle,fcntl.LOCK_EX|fcntl.LOCK_NB)
    except OSError:
        handle.close();return None
    return handle


def migrate(features):
    DATA.mkdir(parents=True,exist_ok=True)
    result=subprocess.run([sys.executable,'-m','alembic','upgrade','head'],
                          cwd=ROOT/'app/backend',env=runtime_environment(features),
                          creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
    if result.returncode:raise RuntimeError('Database preparation failed. Your data was retained.')


def check_update():
    try:
        from updater import latest_release
        remote=latest_release()
        local=json.loads((ROOT/'release.json').read_text(encoding='utf8'))
        from updater import version_key
        if remote and version_key(remote['tag_name']) > version_key(local['version']):
            print(f"New version {remote['tag_name']} is available. Use Update.cmd when convenient.",flush=True)
    except Exception:
        print('Update check unavailable; starting your installed version.',flush=True)


def main():
    parser=argparse.ArgumentParser()
    for flag in ['stop','no-browser','skip-update-check','migrate-only','check-only']:
        parser.add_argument('--'+flag,action='store_true')
    parser.add_argument('--state-file')
    args=parser.parse_args()
    if args.stop:request_stop();return 0
    if not args.migrate_only and any((RUNTIME / name).exists() for name in ['update.in-progress', 'install.in-progress']):
        raise RuntimeError('Installation or update is in progress. Wait for it to finish before starting.')
    features=load_features(args.state_file)
    if args.check_only:
        required=[ROOT/'app/frontend/dist/index.html',RUNTIME/'node/node.exe',DATA/'.env']
        if features.get('web'):required.append(ROOT/'app/services/web-research/node_modules/open-websearch/package.json')
        missing=[str(item.relative_to(ROOT)) for item in required if not item.is_file()]
        print(json.dumps({'ready':not missing,'missing':missing,'features':features}))
        return int(bool(missing))
    lock=acquire_lock()
    if lock is None:
        existing=read_state()
        if not args.migrate_only and existing and ping(existing['url']+'/_solevia/health'):
            if not args.no_browser:webbrowser.open(existing['url'])
            print('Solevia is already running.');return 0
        raise RuntimeError('Another Solevia process owns this folder. Close it before installing or updating.')
    if args.migrate_only:
        try:migrate(features)
        finally:lock.close()
        return 0
    services=[];token=uuid.uuid4().hex;halt=False;job=None
    try:
        STOP.unlink(missing_ok=True)
        if not args.skip_update_check:
            from threading import Thread
            Thread(target=check_update,daemon=True).start()
        migrate(features)
        names=['backend','frontend','web','search'];ports=dict(zip(names,available_ports(4)))
        env=runtime_environment(features,ports)
        logs=DATA/'logs';logs.mkdir(parents=True,exist_ok=True)
        # The job closes descendants even if the console window is closed.
        if os.name=='nt':
            sys.path.insert(0,str(ROOT/'app/backend/src'))
            from planner_api.worker_sandbox import WorkerJob
            job=WorkerJob(memory_mb=12288,max_processes=48)
        def spawn(service):
            service['output']=open(logs/(service['name']+'.log'),'ab',buffering=0)
            service['process']=subprocess.Popen(service['command'],cwd=service['cwd'],env=env,
                stdout=service['output'],stderr=subprocess.STDOUT,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
            if job:job.attach(service['process'].pid)
            service['started']=time.monotonic();service['misses']=0
        def stop_service(service):
            process=service.get('process')
            if process and process.poll() is None:
                if os.name=='nt':subprocess.run(['taskkill','/PID',str(process.pid),'/T','/F'],capture_output=True,
                                               creationflags=subprocess.CREATE_NO_WINDOW)
                else:process.terminate()
                try:process.wait(timeout=5)
                except subprocess.TimeoutExpired:process.kill();process.wait()
            if service.get('output'):service['output'].close()
        node=str(RUNTIME/'node/node.exe')
        services=[{'name':'backend','cwd':ROOT/'app/backend',
                   'command':[sys.executable,'-m','uvicorn','planner_api.main:app','--host','127.0.0.1','--port',str(ports['backend'])],
                   'health':f"http://127.0.0.1:{ports['backend']}/api/health"},
                  {'name':'frontend','cwd':ROOT,
                   'command':[node,str(ROOT/'scripts/frontend-server.mjs'),str(ROOT/'app/frontend/dist'),str(ports['frontend']),str(ports['backend'])],
                   'health':f"http://127.0.0.1:{ports['frontend']}/_solevia/health"}]
        if features.get('web'):services.append({'name':'web','cwd':ROOT/'app/services/web-research',
            'command':[node,'src/server.mjs'],'health':f"http://127.0.0.1:{ports['web']}/health"})
        for service in services:service['restarts']=[];spawn(service)
        url=f"http://127.0.0.1:{ports['frontend']}"
        STATE.write_text(json.dumps({'token':token,'url':url,'ports':ports,'pid':os.getpid()}),encoding='utf8')
        def shutdown(*_):
            nonlocal halt
            halt=True
        signal.signal(signal.SIGINT,shutdown);signal.signal(signal.SIGTERM,shutdown)
        opened=False;begin=time.monotonic()
        print('正在准备 Solevia 本地服务。使用桌面入口时可关闭管理窗口留在托盘；使用 Start.cmd 时请保留终端。',flush=True)
        while not halt:
            if STOP.exists() and STOP.read_text(encoding='utf8')==token:break
            now=time.monotonic()
            for service in services:
                dead=service['process'].poll() is not None
                if not dead and now-service['started']>45:
                    service['misses']=0 if ping(service['health']) else service['misses']+1
                if dead or service['misses']>=5:
                    service['restarts']=[stamp for stamp in service['restarts'] if now-stamp<120]
                    if len(service['restarts'])>=3:raise RuntimeError(service['name']+' stopped repeatedly. See user-data/logs and run Install.cmd to repair.')
                    stop_service(service);service['restarts'].append(now);spawn(service)
                    print('正在重新启动服务：'+service['name']+'…',flush=True)
            if not opened and all(ping(service['health']) for service in services[:2]):
                print('服务已就绪：'+url,flush=True)
                if not args.no_browser:webbrowser.open(url)
                opened=True
            if not opened and now-begin>180:raise RuntimeError('Startup timed out. See user-data/logs.')
            time.sleep(2)
    finally:
        if job:job.close()
        for service in reversed(services):
            if 'stop_service' in locals():stop_service(service)
        if (read_state() or {}).get('token')==token:STATE.unlink(missing_ok=True)
        STOP.unlink(missing_ok=True);lock.close()
    return 0


if __name__=='__main__':
    try:raise SystemExit(main())
    except Exception as error:print('Solevia: '+str(error),file=sys.stderr);raise SystemExit(1)
