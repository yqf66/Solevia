from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import uuid
from collections.abc import Callable
from concurrent.futures import Future
from pathlib import Path
from typing import Any


EventCallback = Callable[[dict[str, Any]], None]
RequestCallback = Callable[[dict[str, Any]], None]


class CodexUnavailable(RuntimeError):
    def __init__(self, message: str, *, code: str = "CODEX_UNAVAILABLE"):
        super().__init__(message)
        self.code = code


def find_codex() -> str | None:
    configured = os.environ.get("SOLEVIA_CODEX_EXECUTABLE")
    if configured and Path(configured).is_file():
        return configured
    for name in ("codex.exe", "codex"):
        found = shutil.which(name)
        # npm's .cmd / extensionless launchers cannot be spawned with shell=False
        # on Windows. Locate their native binary below instead of invoking a shell.
        if found and (os.name != "nt" or Path(found).suffix.lower() == ".exe"):
            return found
    appdata = os.environ.get("APPDATA")
    if appdata:
        candidates = [
            Path(appdata) / "npm/node_modules/@openai/codex/node_modules/@openai/codex-win32-x64/vendor/x86_64-pc-windows-msvc/bin/codex.exe",
            Path(appdata) / "npm/node_modules/@openai/codex/node_modules/@openai/codex-win32-arm64/vendor/aarch64-pc-windows-msvc/bin/codex.exe",
        ]
        for candidate in candidates:
            if candidate.is_file():
                return str(candidate)
    local = os.environ.get("LOCALAPPDATA")
    if local:
        candidates = list((Path(local) / "OpenAI/Codex/bin").glob("*/codex.exe"))
        if candidates:
            return str(max(candidates, key=lambda path: path.stat().st_mtime))
    return None


class CodexAppServer:
    """Small JSONL client for the installed Codex app-server protocol."""

    def __init__(self, *, cwd: Path, on_event: EventCallback | None = None, on_request: RequestCallback | None = None, on_disconnect: Callable[[], None] | None = None, config_overrides: tuple[str, ...] = ()):
        self.cwd = cwd
        self.on_event = on_event
        self.on_request = on_request
        self.on_disconnect = on_disconnect
        # Only host code supplies these flags; they are never accepted from HTTP/model input.
        self.config_overrides = config_overrides
        self.process: subprocess.Popen[str] | None = None
        self._reader: threading.Thread | None = None
        self._pending: dict[str, Future[dict[str, Any]]] = {}
        self._lock = threading.RLock()
        self._write_lock = threading.Lock()
        self._closed = False
        self.initialized = False

    @property
    def executable(self) -> str | None:
        return find_codex()

    def start(self) -> None:
        executable = self.executable
        if not executable:
            raise CodexUnavailable("未找到 Codex 可执行文件；请设置 SOLEVIA_CODEX_EXECUTABLE。", code="CODEX_NOT_FOUND")
        self.cwd.mkdir(parents=True, exist_ok=True)
        command = [executable, "app-server", "--listen", "stdio://", "-c", "project_doc_max_bytes=0"]
        search = os.environ.get("SOLEVIA_CODEX_WEB_SEARCH", "live")
        if search not in {"live", "cached", "disabled"}:
            raise ValueError("SOLEVIA_CODEX_WEB_SEARCH 必须是 live、cached 或 disabled。")
        command += ["-c", f'web_search="{search}"']
        for value in self.config_overrides:
            command += ["-c", value]
        self.process = subprocess.Popen(
            command,
            cwd=self.cwd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            shell=False,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        self._closed = False
        self._reader = threading.Thread(target=self._read_loop, name="solevia-codex-reader", daemon=True)
        self._reader.start()
        self.call("initialize", {
            "clientInfo": {"name": "solevia", "title": "Solevia", "version": "0.1.0"},
            "capabilities": {"experimentalApi": True},
        }, timeout=20)
        self.notify("initialized", {})
        self.initialized = True

    def stop(self) -> None:
        with self._lock:
            self._closed = True
            process = self.process
            self.process = None
            pending = list(self._pending.values())
            self._pending.clear()
        for future in pending:
            if not future.done():
                future.set_exception(CodexUnavailable("Codex app-server 已停止。"))
        if process and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)
        if self._reader and self._reader is not threading.current_thread():
            self._reader.join(timeout=5)

    def call(self, method: str, params: dict[str, Any] | None = None, *, timeout: float = 30) -> dict[str, Any]:
        request_id = str(uuid.uuid4())
        future: Future[dict[str, Any]] = Future()
        with self._lock:
            if self._closed or not self.process or not self.process.stdin:
                raise CodexUnavailable("Codex app-server 未运行。")
            self._pending[request_id] = future
        try:
            self._send({"id": request_id, "method": method, "params": params or {}})
            response = future.result(timeout=timeout)
        except Exception:
            with self._lock:
                self._pending.pop(request_id, None)
            raise
        if "error" in response:
            error = response["error"]
            raise RuntimeError(f"Codex {method} 失败: {error}")
        return response.get("result", {})

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        self._send({"method": method, "params": params or {}})

    def respond(self, request_id: Any, result: dict[str, Any]) -> None:
        self._send({"id": request_id, "result": result})

    def reject(self, request_id: Any, message: str) -> None:
        self._send({"id": request_id, "error": {"code": -32601, "message": message}})

    def _send(self, message: dict[str, Any]) -> None:
        with self._write_lock:
            if not self.process or not self.process.stdin:
                raise CodexUnavailable("Codex app-server 未运行。")
            self.process.stdin.write(json.dumps(message, ensure_ascii=False) + "\n")
            self.process.stdin.flush()

    def _read_loop(self) -> None:
        process = self.process
        if not process or not process.stdout:
            return
        try:
            for line in process.stdout:
                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    continue
                request_id = message.get("id")
                if request_id is not None:
                    with self._lock:
                        future = self._pending.pop(str(request_id), None)
                    if future and not future.done():
                        future.set_result(message)
                    elif message.get("method") and self.on_request:
                        self.on_request(message)
                elif message.get("method") and self.on_event:
                    self.on_event(message)
        finally:
            with self._lock:
                self._closed = True
                pending = list(self._pending.values())
                self._pending.clear()
            for future in pending:
                if not future.done():
                    future.set_exception(CodexUnavailable("Codex app-server 连接已关闭。"))
            if self.on_disconnect:
                self.on_disconnect()
