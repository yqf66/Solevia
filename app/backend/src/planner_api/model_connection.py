"""Keep offline test providers out of production conversations and generation."""
from fastapi import HTTPException, Request

from planner_api.config import get_settings


def model_required(endpoint):
    """Mark a model operation; storage, manual tasks and settings remain available."""
    endpoint.solevia_model_required = True
    return endpoint


async def require_model_connection(request: Request):
    endpoint = request.scope.get("endpoint")
    if not getattr(endpoint, "solevia_model_required", False):
        return
    # Cancelling an asynchronous answer never needs a model.
    if request.path_params.get("action") in {"cancel", "discard"}:
        return
    if getattr(endpoint, "__name__", "") in {"preview_day_allocation", "preview_many_day_allocations", "allocate_entire_plan"}:
        try:
            payload = await request.json()
            rows = payload.get("requests", [payload])
            if rows and all(isinstance(row, dict) and row.get("use_ai") is False for row in rows):
                return
        except (ValueError, AttributeError):
            pass
    settings = get_settings()
    if settings.llm_provider == "codex":
        runtime = getattr(request.app.state, "codex_business", None)
        if runtime and runtime.connected:
            return
        message = "Codex 尚未连接。请先连接本机 Codex，或配置 API 后再发送。"
    elif (settings.llm_provider != "fake" and settings.model_runtime_configured
          and settings.llm_model.strip() and settings.llm_base_url.strip()):
        return
    else:
        message = "还没有连接模型。请先配置 API 或连接本机 Codex，再开始对话。"
    raise HTTPException(503, detail={"code": "MODEL_CONNECTION_REQUIRED", "message": message})
