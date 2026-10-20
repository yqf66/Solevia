"""Explicit, checksummed release updates; user data stays outside the program."""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import urllib.request
import urllib.error

from package_integrity import safe_path, verify_zip

ROOT=Path(__file__).resolve().parents[1]


def request(url):
    return urllib.request.Request(url,headers={'User-Agent':'Solevia-release-updater','Accept':'application/vnd.github+json'})


def latest_release():
    config=json.loads((ROOT/'release.json').read_text(encoding='utf8'))
    try:
        with urllib.request.urlopen(request('https://api.github.com/repos/'+config['repository']+'/releases/latest'),timeout=8) as response:
            result=json.load(response)
        return None if result.get('draft') or result.get('prerelease') else result
    except urllib.error.HTTPError as error:
        if error.code==404:return None
        raise


def version_key(value):
    parts=value.lstrip('v').split('.')
    if len(parts)!=3 or not all(part.isdigit() for part in parts):raise ValueError('Stable version must use major.minor.patch')
    return tuple(map(int,parts))


def download_release(release,folder):
    tag=release['tag_name'].lstrip('v')
    name=f'Solevia-{tag}-windows-x64.zip'
    assets={asset['name']:asset for asset in release.get('assets',[])}
    if name not in assets or name+'.sha256' not in assets:raise RuntimeError('This version does not have a verified Windows package yet.')
    for filename in [name,name+'.sha256']:
        url=assets[filename]['browser_download_url']
        if not url.startswith('https://github.com/'):raise ValueError('Unexpected release download host')
        with urllib.request.urlopen(request(url),timeout=180) as response,(folder/filename).open('wb') as output:
            shutil.copyfileobj(response,output)
    expected=(folder/(name+'.sha256')).read_text(encoding='utf8').split()[0]
    if hashlib.sha256((folder/name).read_bytes()).hexdigest()!=expected:raise ValueError('Release archive checksum mismatch')
    return folder/name


def backup_database(source,destination):
    if not source.exists():return
    with sqlite3.connect(source) as old,sqlite3.connect(destination) as new:old.backup(new)


def install_package(package, *, root=None, run_installer=None):
    target=Path(root) if root else ROOT
    _archive_root,files=verify_zip(package)
    current=json.loads((target/'release.json').read_text(encoding='utf8'))
    incoming=json.loads(files['release.json'])
    if incoming['repository']!=current['repository'] or incoming['platform']!=current['platform']:raise ValueError('Release identity mismatch')
    if version_key(incoming['version'])<=version_key(current['version']):raise ValueError('Only a newer stable release can be installed')
    previous=json.loads((target/'MANIFEST.json').read_text(encoding='utf8'))
    names={row['path'] for row in previous['files']}|set(files)|{'MANIFEST.json'}
    for name in names:safe_path(name)
    sys.path.insert(0,str(target/'scripts'))
    import launcher
    launcher.request_stop()
    lock=launcher.acquire_lock()
    if lock is None:raise RuntimeError('Stop Solevia before updating')
    marker=target/'.runtime/update.in-progress'
    # Only source files recorded in the previous manifest may be replaced.
    # Extra local source files are retained; private data is never a program file.
    try:
        marker.write_text(str(os.getpid()),encoding='utf8')
        folder=Path(tempfile.mkdtemp(prefix='update-',dir=target/'.runtime'))
        original=folder/'original';original.mkdir()
        old_data=target/'user-data';database=old_data/'planner.db'
        backups=old_data/'backups';backups.mkdir(parents=True,exist_ok=True)
        from datetime import datetime,timezone
        stamp=datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')
        database_backup=backups/(stamp+'-before-update.db')
        backup_database(database,database_backup)
        installed_state=target/'.runtime/installed.json'
        previous_state=installed_state.read_bytes() if installed_state.exists() else None
    except Exception:
        marker.unlink(missing_ok=True);lock.close();raise
    recorded=[]
    # Do not pass PowerShell 7's module search path into Windows PowerShell 5.1.
    installer_env={key:value for key,value in os.environ.items() if key.upper()!='PSMODULEPATH'}
    installer=run_installer or (lambda:subprocess.run(['powershell.exe','-NoProfile','-ExecutionPolicy','Bypass',
        '-File',str(target/'scripts/install.ps1'),'-NonInteractive','-Repair'],cwd=target,env=installer_env).returncode)
    try:
        for name in sorted(names):
            destination=target/name
            destination.resolve().relative_to(target.resolve())
            if any(part.is_symlink() or part.is_junction() for part in [destination,*destination.parents] if part!=target and part.is_relative_to(target)):
                raise ValueError('Linked program file cannot be updated')
            existed=destination.is_file()
            if existed:
                saved=original/name;saved.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(destination,saved)
            recorded.append((name,existed))
            if name in files:
                destination.parent.mkdir(parents=True,exist_ok=True);destination.write_bytes(files[name])
            elif existed:destination.unlink()
        # Migration obtains the same lock; release ours after files are stable.
        lock.close();lock=None
        if installer()!=0:raise RuntimeError('Updated dependencies or migration failed')
    except Exception:
        for name,existed in reversed(recorded):
            destination=target/name
            if existed:shutil.copy2(original/name,destination)
            else:destination.unlink(missing_ok=True)
        if database_backup.exists():
            for suffix in ('-wal','-shm'):(old_data/('planner.db'+suffix)).unlink(missing_ok=True)
            shutil.copy2(database_backup,database)
        if previous_state is not None:installed_state.write_bytes(previous_state)
        # Reconcile the old release's dependencies after restoring its source.
        if run_installer is None:installer()
        raise RuntimeError('Update failed. Previous program and database were restored; rerun Install.cmd if dependencies need repair.')
    finally:
        if lock:lock.close()
        marker.unlink(missing_ok=True)
    print('Updated to '+incoming['version']+'. Double-click Start.cmd.')


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--package',type=Path);args=parser.parse_args()
    if args.package:
        verify_zip(args.package)
        answer=input('Install this local release package and preserve your personal data? [y/N] ')
        if answer.lower()!='y':return 0
        install_package(args.package);return 0
    release=latest_release()
    local=json.loads((ROOT/'release.json').read_text(encoding='utf8'))
    if not release or version_key(release['tag_name'])<=version_key(local['version']):print('You are using the latest available stable release.');return 0
    print('New version: '+release['tag_name'])
    if input('Download and install it? Your settings and data will be preserved. [y/N] ').lower()!='y':return 0
    folder=Path(tempfile.mkdtemp(prefix='download-',dir=ROOT/'.runtime'))
    install_package(download_release(release,folder));return 0


if __name__=='__main__':
    try:raise SystemExit(main())
    except Exception as error:print('Update: '+str(error),file=sys.stderr);raise SystemExit(1)
