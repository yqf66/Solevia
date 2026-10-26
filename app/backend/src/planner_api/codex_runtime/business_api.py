"""Only business model controls are exposed by the unified workbench."""

from fastapi import APIRouter, Request
from .api import preferences, update_preferences, generation_status

router = APIRouter(prefix="/api/codex")
router.add_api_route("/preferences", preferences, methods=["GET"])
router.add_api_route("/preferences", update_preferences, methods=["PUT"])
router.add_api_route("/generation-status", generation_status, methods=["GET"])


@router.get("/status")
def status(request: Request):
    runtime = request.app.state.codex_business
    return {
        "runtime": "codex-app-server",
        "connected": runtime.connected,
        "error": (runtime.connection_error or "Codex 尚未连接，请检查本机安装与登录。") if not runtime.connected else None,
    }
