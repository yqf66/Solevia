from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .codex_runtime.api import router
from .codex_runtime.service import CodexService
from .local_security import LocalAPISecurity

ROOT = Path(__file__).resolve().parents[3]


def create_app(*, data_dir: Path | None = None, workspace: Path | None = None, service_factory=CodexService):
    runtime = data_dir or Path(os.environ.get("SOLEVIA_CODEX_RUNTIME_ROOT", ROOT / ".runtime/codex"))
    agent_workspace = workspace or Path(os.environ.get("SOLEVIA_CODEX_WORKSPACE", runtime / "workspace"))

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        service = service_factory(data_dir=runtime, workspace=agent_workspace)
        app.state.codex = service
        await asyncio.to_thread(service.start)
        try:
            yield
        finally:
            await asyncio.to_thread(service.stop)

    inner = FastAPI(title="Solevia Codex Runtime", version="0.1.0", lifespan=lifespan)

    @inner.exception_handler(KeyError)
    async def missing(_request: Request, exc: KeyError):
        return JSONResponse({"detail": str(exc.args[0])}, status_code=404)

    @inner.exception_handler(ValueError)
    async def conflict(_request: Request, exc: ValueError):
        return JSONResponse({"detail": str(exc)}, status_code=409)

    @inner.get("/api/health")
    async def health():
        return {"status": "ok", "runtime": "codex", "project_root": str(ROOT)}

    inner.include_router(router)
    origins = os.environ.get("SOLEVIA_FRONTEND_ORIGINS", "http://127.0.0.1:5175,http://localhost:5175")
    return TrustedHostMiddleware(LocalAPISecurity(inner, origins=origins.split(",")), allowed_hosts=["127.0.0.1", "localhost"])


app = create_app()
