import os
"""Release archives and updates must preserve the private/program boundary."""
from pathlib import Path
import hashlib
import json
import sys
import zipfile

import pytest

ROOT=Path(__file__).resolve().parents[3]
sys.path.insert(0,str(ROOT/'scripts'))
from package_integrity import safe_path,scan_text,verify_zip
import launcher
import updater


@pytest.mark.parametrize('name',['../secret.txt','user-data/.env','app/backend/data/planner.db',
                                'app/.env','app/CON.txt','app/thing.','/absolute','app\\file','app/file.key'])
def test_private_or_unsafe_paths_are_rejected(name):
    with pytest.raises(ValueError):safe_path(name)


@pytest.mark.parametrize('value',['C:'+chr(92)+'Users'+chr(92)+'someone'+chr(92)+'secret',
                                  'sk-'+'s'*32,'http://localhost:'+str(7800+97)])
def test_private_content_blocks_packaging_without_echoing_it(value):
    with pytest.raises(ValueError) as error:scan_text('app/file.txt',value.encode())
    assert value not in str(error.value)


def test_launcher_does_not_inherit_provider_credentials(monkeypatch):
    monkeypatch.setenv('LLM_API_KEY','private-canary')
    monkeypatch.setenv('LLM_BASE_URL','https://private.invalid')
    monkeypatch.setenv('HTTPS_PROXY','http://private.invalid')
    env=launcher.runtime_environment({})
    assert not {'LLM_API_KEY','LLM_BASE_URL','HTTPS_PROXY'}.intersection(env)
    assert Path(env['SOLEVIA_ENV_FILE']).parent.name=='user-data'


def release_archive(folder,version,extra=None):
    config=json.loads((ROOT/'release.json').read_text())
    config['version']=version
    files={'release.json':json.dumps(config).encode(),'requirements-core.lock':b'# lock\n',
           'app/backend/pyproject.toml':b'# backend\n','app/frontend/package.json':b'{}',
           'Install.cmd':b'install','Start.cmd':b'start','Stop.cmd':b'stop','Update.cmd':b'update',
           'scripts/install.ps1':b'# install','scripts/launcher.py':b'# launch',
           'scripts/updater.py':b'# update','scripts/frontend-server.mjs':b'// server'}
    files.update(extra or {})
    manifest={'files':[{'path':name,'bytes':len(data),'sha256':hashlib.sha256(data).hexdigest()} for name,data in files.items()]}
    files['MANIFEST.json']=json.dumps(manifest).encode()
    package=folder/('release-'+version+'.zip')
    with zipfile.ZipFile(package,'w') as archive:
        for name,data in files.items():archive.writestr('Solevia/'+name,data)
    return package,files


def test_archive_cannot_hide_extra_files_outside_manifest(tmp_path):
    package,_=release_archive(tmp_path,'0.1.1')
    with zipfile.ZipFile(package,'a') as archive:archive.writestr('Solevia/app/private.txt','hidden')
    with pytest.raises(ValueError,match='exact file list'):verify_zip(package)


@pytest.mark.parametrize('succeed',[True,False])
def test_update_preserves_user_files_and_rolls_back_on_failure(tmp_path,monkeypatch,succeed):
    import sqlite3
    package,files=release_archive(tmp_path,'0.1.0')
    target=tmp_path/'installation';target.mkdir()
    for name,data in files.items():
        path=target/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(data)
    data=target/'user-data';data.mkdir();(data/'.env').write_text('PRIVATE_KEY=canary');(data/'upload.txt').write_text('private upload')
    with sqlite3.connect(data/'planner.db') as db:db.execute('create table records(value)');db.execute("insert into records values ('keep')")
    runtime=target/'.runtime';runtime.mkdir();(runtime/'installed.json').write_text('{"version":"0.1.0","features":{}}')
    (target/'Solevia.exe').write_bytes(b'previous-desktop-entry')
    for key,value in [('ROOT',target),('RUNTIME',runtime),('DATA',data),('STATE',runtime/'services.json'),('STOP',runtime/'stop.request')]:monkeypatch.setattr(launcher,key,value)
    newer,_=release_archive(tmp_path,'0.1.1',{'app/new.txt':b'new code'})
    def install():
        (target/'Solevia.exe').write_bytes(b'new-desktop-entry')
        if not succeed:
            with sqlite3.connect(data/'planner.db') as db:db.execute("update records set value='changed'")
        return 0 if succeed else 1
    if succeed:updater.install_package(newer,root=target,run_installer=install)
    else:
        with pytest.raises(RuntimeError,match='restored'):updater.install_package(newer,root=target,run_installer=install)
    assert (data/'.env').read_text()=='PRIVATE_KEY=canary'
    assert (data/'upload.txt').read_text()=='private upload'
    assert json.loads((target/'release.json').read_text())['version']==('0.1.1' if succeed else '0.1.0')
    assert (target/'app/new.txt').exists()==succeed
    with sqlite3.connect(data/'planner.db') as db:assert db.execute('select value from records').fetchone()[0]=='keep'
    assert not (runtime/'update.in-progress').exists()
    assert (target/'Solevia.exe').read_bytes()==(b'new-desktop-entry' if succeed else b'previous-desktop-entry')


def test_installed_release_data_paths_and_model_settings_are_external(tmp_path,monkeypatch):
    from planner_api.config import Settings,get_settings,settings_env_file
    from planner_api.user_settings_service import UserSettingsService
    from planner_api.schemas import ModelConnectionUpdate
    path=tmp_path/'private'/'settings.env';path.parent.mkdir()
    path.write_text('LLM_PROVIDER=fake\n')
    monkeypatch.setenv('SOLEVIA_ENV_FILE',str(path))
    get_settings.cache_clear()
    try:
        service=UserSettingsService()
        service.update_model_connection(ModelConnectionUpdate(provider='openai_compatible',base_url='https://example.test/v1',model='custom',proxy_url='',api_key='private-canary'))
        assert settings_env_file()==path
        assert get_settings().llm_model=='custom'
        assert ('private-canary' in path.read_text()) == (os.name != 'nt')
        assert 'private-canary' not in service.get_model_connection().model_dump_json()
    finally:get_settings.cache_clear()


def test_empty_browser_cache_reuses_installed_edge(tmp_path,monkeypatch):
    import importlib.util
    spec=importlib.util.spec_from_file_location('release_reader',ROOT/'app/services/web-research/src/crawl4ai-reader.py')
    reader=importlib.util.module_from_spec(spec);spec.loader.exec_module(reader)
    cache=tmp_path/'empty-cache';cache.mkdir()
    programs=tmp_path/'programs';edge=programs/'Microsoft/Edge/Application/msedge.exe'
    edge.parent.mkdir(parents=True);edge.touch()
    monkeypatch.setenv('PLAYWRIGHT_BROWSERS_PATH',str(cache))
    monkeypatch.setenv('PROGRAMFILES(X86)',str(programs))
    assert reader.browser_channel()=='msedge'


def test_update_preparation_failure_releases_lock_and_marker(tmp_path,monkeypatch):
    _,files=release_archive(tmp_path,'0.1.0')
    target=tmp_path/'install';target.mkdir()
    for name,data in files.items():
        path=target/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(data)
    runtime=target/'.runtime';runtime.mkdir();data=target/'user-data'
    for key,value in [('ROOT',target),('RUNTIME',runtime),('DATA',data),('STATE',runtime/'services.json'),('STOP',runtime/'stop.request')]:monkeypatch.setattr(launcher,key,value)
    def fail(*_args):raise OSError('backup unavailable')
    monkeypatch.setattr(updater,'backup_database',fail)
    newer,_=release_archive(tmp_path,'0.1.1')
    with pytest.raises(OSError,match='backup unavailable'):updater.install_package(newer,root=target,run_installer=lambda:0)
    assert not (runtime/'update.in-progress').exists()
    lock=launcher.acquire_lock();assert lock is not None;lock.close()
    assert json.loads((target/'release.json').read_text())['version']=='0.1.0'
