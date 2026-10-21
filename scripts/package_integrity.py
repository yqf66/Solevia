"""Validate exact release contents before extraction or installation."""
import hashlib
import json
from pathlib import PurePosixPath
import re
import zipfile

FORBIDDEN_PARTS = {'.git','.env','node_modules','.venv','.runtime','user-data','__pycache__',
                   '.dev-logs','.pnpm-store','uploads','data','models','checkpoints','.pytest_cache'}
PRIVATE_PATTERNS = {
    'machine_path': re.compile(r'(?i)[A-Z]:[\\/](?:Users|Documents and Settings|DATASET)[\\/]|/' + r'Users/[^/\s]+/|/' + r'home/[^/\s]+/'),
    'private_key': re.compile(r'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----'),
    'provider_key': re.compile(r'\b(?:sk-[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b'),
    'personal_proxy': re.compile(r'(?<!\d)78(?:97|90)(?!\d)'),
    'credential_url': re.compile(r'https?://[^\s/:]+:[^\s/@]+@'),
    'email': re.compile(r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b'),
}
MAX_FILE = 12 * 1024 * 1024
MAX_TOTAL = 200 * 1024 * 1024


def safe_path(name):
    path=PurePosixPath(name)
    reserved={'CON','PRN','AUX','NUL',*[f'COM{i}' for i in range(1,10)],*[f'LPT{i}' for i in range(1,10)]}
    if (not name or '\\' in name or path.is_absolute() or ':' in name or
            any(part in {'..','.'} or part.lower() in FORBIDDEN_PARTS or
                part.endswith((' ','.')) or part.split('.')[0].upper() in reserved
                or any(ord(char)<32 for char in part) for part in path.parts)):
        raise ValueError('Forbidden release path: '+name)
    if path.suffix.lower() in {'.db','.sqlite','.sqlite3','.log','.pem','.key','.pyc','.zip','.7z','.exe','.dll','.bin','.safetensors','.pt','.pth','.dpapi'}:
        raise ValueError('Forbidden release file type: '+name)
    return path


def scan_text(name,data,private_values=()):
    if PurePosixPath(name).suffix.lower() in {'.png','.webp','.woff2','.ttf','.ico'}:return
    text=data.decode('utf-8-sig')
    for kind,pattern in PRIVATE_PATTERNS.items():
        if pattern.search(text):raise ValueError('Privacy scan blocked '+kind+' in '+name)
    for value in private_values:
        if value and value in text:raise ValueError('Privacy scan blocked a local-only value in '+name)


def verify_zip(filename):
    with zipfile.ZipFile(filename) as archive:
        entries=archive.infolist()
        if len(entries)>5000 or sum(item.file_size for item in entries)>MAX_TOTAL:raise ValueError('Release size limit exceeded')
        if not entries:raise ValueError('Empty release')
        root=entries[0].filename.split('/')[0]
        prefix=root+'/'
        files={};seen=set()
        for info in entries:
            if info.is_dir():continue
            if not info.filename.startswith(prefix):raise ValueError('Multiple archive roots')
            relative=info.filename[len(prefix):]
            safe_path(relative)
            identity=relative.casefold()
            if identity in seen:raise ValueError('Duplicate archive entry')
            seen.add(identity)
            if info.file_size>MAX_FILE or info.flag_bits&1 or (info.external_attr>>16)&0o170000==0o120000:raise ValueError('Unsafe archive entry')
            data=archive.read(info);scan_text(relative,data);files[relative]=data
        manifest=json.loads(files['MANIFEST.json'])
        expected={row['path']:row for row in manifest['files']}
        if len(expected)!=len(manifest['files']) or set(files)!=set(expected)|{'MANIFEST.json'}:raise ValueError('Archive manifest does not match exact file list')
        for relative,row in expected.items():
            data=files[relative]
            if len(data)!=row['bytes'] or hashlib.sha256(data).hexdigest()!=row['sha256']:raise ValueError('File checksum mismatch: '+relative)
        required={'release.json','Install.cmd','Start.cmd','Stop.cmd','Update.cmd','scripts/install.ps1',
                  'scripts/launcher.py','scripts/updater.py','scripts/frontend-server.mjs',
                  'requirements-core.lock','app/backend/pyproject.toml','app/frontend/package.json'}
        if not required<=set(files):raise ValueError('Incomplete release')
        return root,files
