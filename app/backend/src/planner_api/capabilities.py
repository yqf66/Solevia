"""Fixed desktop handoff for optional capabilities; never execute web-supplied commands."""
import json
import os
import time
import re
import secrets
from pathlib import Path
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request

router = APIRouter(prefix="/api/settings/capabilities", tags=["system"])
KEYS = ("web", "documents", "ocr", "semantic")


def desktop_root():
    value = os.environ.get("SOLEVIA_DESKTOP_ROOT")
    return Path(value).resolve() if value else None


def checked(path):
    for item in (path, *path.parents):
        if item.is_symlink() or (hasattr(item, "is_junction") and item.is_junction()):
            raise HTTPException(409, "安装目录存在重定向，请从桌面工作台管理能力。")
    return path


def desktop_available(root):
    if root is None:
        return False
    heartbeat = checked(root / ".runtime/desktop-host.json")
    try:
        return 0 <= time.time() - heartbeat.stat().st_mtime < 15
    except OSError:
        return False


@router.get("")
def snapshot():
    root = desktop_root()
    features = {}
    if root:
        try:
            features = json.loads(checked(root / ".runtime/installed.json").read_text(encoding="utf-8-sig")).get("features", {})
        except (OSError, ValueError):
            pass
    return {"installed": {key: bool(features.get(key)) for key in KEYS},
            "desktop_available": desktop_available(root)}


@router.post('/prepare')
def prepare(request: Request):
    """Grant one short-lived fixed installation, after checking ongoing work."""
    root = desktop_root()
    if not desktop_available(root):
        raise HTTPException(409, '桌面安装服务未运行。请打开 Solevia 桌面工作台，再回到此处重试。')
    try:
        bridge = json.loads(checked(root / '.runtime/installer-bridge.json').read_text(encoding='utf-8'))
        if not re.fullmatch(r'http://127\.0\.0\.1:[0-9]{1,5}', bridge.get('url', '')) or not re.fullmatch(r'[A-Za-z0-9_-]{40,64}', bridge.get('token', '')):
            raise ValueError()
    except (OSError, ValueError):
        raise HTTPException(409, '当前安装服务尚未就绪。请重新打开桌面工作台后重试。') from None
    manager = request.app.state.kernel
    with manager.lock:
        if manager.switching or manager.active or manager.busy_check():
            raise HTTPException(409, '当前还有生成或资料处理在进行，请完成后再补装能力。')
        token = secrets.token_urlsafe(32)
        target = checked(root / '.runtime/installer-permit.json')
        target.write_text(json.dumps({'token': token, 'expires': time.time() + 60}), encoding='utf-8')
    return {'bridge': bridge, 'permit': token}


@router.post("/manage")
def manage(request: Request):
    root = desktop_root()
    if not desktop_available(root):
        raise HTTPException(409, "请先打开 Solevia 桌面工作台，再点击「补装与管理能力」。")
    manager = request.app.state.kernel
    with manager.lock:
        if manager.switching or manager.active or manager.busy_check():
            raise HTTPException(409, "当前还有生成或资料处理在进行，请完成后再补装能力。")
        target = checked(root / ".runtime/desktop-request.json")
        # Exclusive create coalesces double clicks; the desktop consumes the request.
        try:
            with target.open("x", encoding="utf-8") as handle:
                json.dump({"action": "manage-capabilities", "id": str(uuid4()), "expires": time.time() + 30}, handle)
        except FileExistsError:
            raise HTTPException(409, "能力管理已请求打开，请查看桌面工作台。") from None
    return {"status": "REQUESTED", "message": "请在桌面工作台确认补装；安装前会检查磁盘空间，完成后自动恢复服务。"}
