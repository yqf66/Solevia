"""Noninteractive desktop adapter. Installation consent belongs to the graphical host."""
import argparse
import json
from pathlib import Path
import tempfile

import updater


def check():
    remote = updater.latest_release()
    current = json.loads((updater.ROOT / 'release.json').read_text(encoding='utf8'))
    available = bool(remote and updater.version_key(remote['tag_name']) > updater.version_key(current['version']))
    return {'available': available, 'tag': remote['tag_name'] if remote else None}


def apply(tag):
    # Resolve again to avoid using stale or caller-supplied download URLs.
    remote = updater.latest_release()
    if not remote or remote['tag_name'] != tag:
        raise RuntimeError('正式版本已变化，请重新检查更新并确认。')
    folder = Path(tempfile.mkdtemp(prefix='desktop-download-', dir=updater.ROOT / '.runtime'))
    print('正在从 GitHub 下载并校验正式安装包…', flush=True)
    package = updater.download_release(remote, folder)
    print('正在停止服务、备份个人数据库并更新程序…', flush=True)
    updater.install_package(package)


def apply_local(package):
    print('正在校验已确认的本地安装包…', flush=True)
    updater.install_package(package)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['check', 'apply', 'local'])
    parser.add_argument('--tag')
    parser.add_argument('--package', type=Path)
    args = parser.parse_args()
    try:
        if args.action == 'check':
            print(json.dumps(check()))
        elif args.action == 'local':
            if not args.package:
                raise ValueError('缺少已确认的安装包。')
            apply_local(args.package)
        else:
            if not args.tag:
                raise ValueError('缺少已确认的版本号。')
            apply(args.tag)
    except Exception as error:
        print('更新未完成：' + str(error))
        raise SystemExit(1)
