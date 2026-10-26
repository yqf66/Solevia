"""Runtime selection shares business data and preserves separate model profiles."""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from threading import RLock
import os
import tempfile

from dotenv import set_key
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict
from typing import Literal

from planner_api.config import get_settings, settings_env_file

settings_lock = RLock()


def save_values(path: Path, values: dict[str, str]):
    """Publish a whole settings update, so readers never see half a profile."""
    with settings_lock:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=".solevia-settings-", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(path.read_text(encoding="utf-8") if path.exists() else "")
            for key, value in values.items():
                set_key(temporary, key, value, quote_mode="auto")
            os.replace(temporary, path)
        finally:
            Path(temporary).unlink(missing_ok=True)
        get_settings.cache_clear()


class KernelManager:
    def __init__(self, application, *, busy_check=None, env_path=None):
        self.application = application
        self.lock = RLock()
        self.active = 0
        self.switching = False
        self.busy_check = busy_check or self._background_busy
        self.env_path = env_path or settings_env_file()

    @contextmanager
    def business_request(self):
        with self.lock:
            if self.switching:
                raise HTTPException(409, "正在切换连接方式，请稍后再发送。")
            self.active += 1
        try:
            yield
        finally:
            with self.lock:
                self.active -= 1

    def _background_busy(self):
        from sqlalchemy import select
        from planner_api.database import SessionLocal
        from planner_api.models import (
            GenerationBatch,
            ResearchRun,
            ResearchResponseRequest,
            ClarificationAnalysisJob,
        )

        with SessionLocal() as session:
            for cls, statuses in (
                (GenerationBatch, ["QUEUED", "RUNNING"]),
                (ResearchRun, ["QUEUED", "RUNNING"]),
                (ClarificationAnalysisJob, ["PENDING", "PROCESSING"]),
                (ResearchResponseRequest, ["WAITING_RESEARCH", "QUEUED_ANSWER", "ANSWERING"]),
            ):
                if session.scalar(select(cls.id).where(cls.status.in_(statuses)).limit(1)):
                    return True
        runtime = getattr(self.application.state, "codex_business", None)
        if runtime:
            status = runtime.generation_status()
            if status["active_count"] or status["queued_count"]:
                return True
        files = getattr(self.application.state, "conversation_files", None)
        return bool(
            files
            and any(row["status"] in {"QUEUED", "RUNNING"} for row in files.store.list("file_copy"))
        )

    def snapshot(self):
        codex = getattr(self.application.state, "codex_business", None)
        mode = "codex" if get_settings().llm_provider == "codex" else "api"
        from planner_api.config import load_user_settings
        api = load_user_settings(self.env_path, kernel="api")
        return {
            "mode": mode,
            "api_configured": api.llm_provider != "fake" and api.model_runtime_configured and bool(api.llm_model.strip()),
            "switching": self.switching,
            "codex_connected": bool(codex and codex.connected),
            "codex_detected": self.codex_detected(),
            "capabilities": {
                "codex": codex is not None,
                "api": True,
                "file_copies": codex is not None,
            },
            "codex_error": (getattr(codex, "connection_error", None)
                            or "Codex 尚未连接，请先完成本机安装与登录。")
            if not codex or not codex.connected else None,
            "codex_error_code": (getattr(codex, "connection_error_code", None) or "CODEX_UNAVAILABLE")
            if not codex or not codex.connected else None,
        }

    @staticmethod
    def codex_detected():
        from planner_api.codex_runtime.protocol import find_codex
        return find_codex() is not None

    def connect_codex(self):
        """An explicit local action can connect after installation without a restart."""
        with self.lock:
            if self.switching or self.active or self.busy_check():
                raise HTTPException(409, "当前还有操作在进行，请完成后再连接 Codex。")
            runtime = getattr(self.application.state, "codex_business", None)
            if runtime is None:
                raise HTTPException(409, "请使用 Solevia 一体化工作台连接 Codex。")
            self.switching = True
        try:
            save_values(self.env_path, {"SOLEVIA_CODEX_CONSENT": "1"})
            if not runtime.connected:
                runtime.stop()
                runtime.start()
            if runtime.connected:
                from planner_api.codex_runtime.preferences import ensure_initial_model
                ensure_initial_model(runtime, self.env_path)
                save_values(self.env_path, {"SOLEVIA_KERNEL": "codex"})
        finally:
            with self.lock:
                self.switching = False
        return self.snapshot()

    def switch(self, mode):
        with self.lock, settings_lock:
            if mode == self.snapshot()["mode"]:
                return self.snapshot()
            if self.active or self.busy_check():
                raise HTTPException(
                    409, "当前还有生成或资料研究在进行，请等待完成后再切换连接方式。"
                )
            if mode == "codex" and not self.snapshot()["codex_connected"]:
                raise HTTPException(409, "Codex 尚未连接，请先完成本机安装与登录，再点击「检测并连接」。")
            if mode == "api":
                from planner_api.config import load_user_settings

                api = load_user_settings(self.env_path, kernel="api")
                if (
                    api.llm_provider == "fake"
                    or not api.model_runtime_configured
                    or not api.llm_model.strip()
                ):
                    raise HTTPException(422, "请先保存 API 地址、模型和密钥，再切换到自配置 API。")
            self.switching = True
            try:
                save_values(self.env_path, {"SOLEVIA_KERNEL": mode})
            finally:
                self.switching = False
            return self.snapshot()


class KernelUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: Literal["api", "codex"]


router = APIRouter(prefix="/api/settings/kernel", tags=["system"])


@router.get("")
def read_kernel(request: Request):
    return request.app.state.kernel.snapshot()


@router.put("")
def update_kernel(payload: KernelUpdate, request: Request):
    request.app.state.kernel.switch(payload.mode)
    return request.app.state.kernel.snapshot()


@router.post("/codex/connect")
def connect_codex(request: Request):
    return request.app.state.kernel.connect_codex()
