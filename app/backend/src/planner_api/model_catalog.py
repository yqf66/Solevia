"""Discover the configured provider's model IDs without exposing its credentials."""
from __future__ import annotations

import hashlib
import json
from urllib.parse import urlsplit, urlunsplit

import httpx
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator

from planner_api.config import get_settings
from planner_api.kernel import save_values, settings_lock
from planner_api.schemas import ModelConnectionUpdate
from planner_api.user_settings_service import UserSettingsService


class ModelCatalogRequest(ModelConnectionUpdate):
    model: str = ""


class ModelSelection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model: str = Field(min_length=1, max_length=160)
    connection_id: str = Field(min_length=64, max_length=64)

    @field_validator("model")
    @classmethod
    def clean_model(cls, value):
        value = value.strip()
        if not value or any(ord(char) < 32 for char in value):
            raise ValueError("Invalid model ID")
        return value


def connection_id(settings):
    values = [settings.llm_provider, settings.llm_base_url, settings.llm_proxy_url,
              settings.llm_api_key.get_secret_value()]
    return hashlib.sha256(json.dumps(values).encode()).hexdigest()


def models_url(base_url):
    parsed = urlsplit(base_url)
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname
            or parsed.username or parsed.password or parsed.query or parsed.fragment):
        raise ValueError("Invalid provider URL")
    path = parsed.path.rstrip("/")
    for suffix in ("/chat/completions", "/responses", "/models"):
        if path.endswith(suffix):
            path = path[:-len(suffix)]
            break
    return urlunsplit((parsed.scheme, parsed.netloc, path + "/models", "", ""))


def discover_models(settings, *, client_factory=httpx.Client):
    result = {"model": settings.llm_model, "models": [], "available": False,
              "connection_id": connection_id(settings), "message": ""}
    if settings.llm_provider == "fake":
        return {**result, "message": "请先配置 API 地址与密钥，也可以手动填写模型 ID。"}
    key = settings.llm_api_key.get_secret_value().strip()
    if not key:
        return {**result, "message": "请先填写 API Key，再读取模型列表。"}
    try:
        url = models_url(settings.llm_base_url)
        models = set()
        after = None
        truncated = False
        # Never follow redirects with the provider's credentials.
        with client_factory(timeout=15, follow_redirects=False, trust_env=False,
                            proxy=settings.llm_proxy_url or None) as client:
            for page in range(10):
                with client.stream("GET", url, headers={"Authorization": f"Bearer {key}"},
                                   params={"after": after} if after else None) as response:
                    hints = {
                        401: "服务商拒绝了 API Key（401）。请检查密钥是否正确、过期或已停用；模型 ID 也可手动填写。",
                        403: "服务商未授权读取模型列表（403）。请检查 Key 权限或账号限制，也可手动填写模型 ID。",
                        404: "服务商未提供此地址的模型列表（404）。请核对 Base URL；地址正确时可手动填写模型 ID。",
                        429: "模型列表请求受限（429）。请检查服务商的额度或频率限制，稍后重试或手动填写模型 ID。",
                    }
                    if response.status_code in hints:
                        return {**result, "message": hints[response.status_code]}
                    if response.status_code >= 500:
                        return {**result, "message": "服务商暂时无法提供模型列表（5xx）。请稍后重试，也可手动填写模型 ID。"}
                    if 300 <= response.status_code < 400:
                        return {**result, "message": "模型列表地址发生跳转。为保护密钥未跟随跳转，请核对服务商的最终 API 地址，或手动填写模型 ID。"}
                    response.raise_for_status()
                    chunks = []
                    size = 0
                    for chunk in response.iter_bytes():
                        size += len(chunk)
                        if size > 1_000_000:
                            raise ValueError("Model catalog too large")
                        chunks.append(chunk)
                    payload = json.loads(b"".join(chunks))
                rows = payload if isinstance(payload, list) else payload.get("data", payload.get("models"))
                if not isinstance(rows, list):
                    raise ValueError("Unsupported model catalog")
                for row in rows:
                    model = row if isinstance(row, str) else row.get("id") if isinstance(row, dict) else None
                    if isinstance(model, str) and model.strip() and len(model) <= 160 and not any(ord(c) < 32 for c in model):
                        models.add(model.strip())
                if not isinstance(payload, dict) or not payload.get("has_more"):
                    break
                last = payload.get("last_id") or (rows[-1].get("id") if rows and isinstance(rows[-1], dict) else None)
                if not isinstance(last, str) or last == after or page == 9:
                    truncated = True
                    break
                after = last
        message = "列表由服务商返回，实际调用仍取决于账号权限与当前对话协议。"
        if truncated:
            message = "已读取部分模型；服务商列表未完整返回，可刷新或手动填写模型 ID。"
        return {**result, "models": [{"id": value} for value in sorted(models, key=str.casefold)],
                "available": bool(models), "message": message if models else "服务商没有返回模型列表，可以手动填写模型 ID。"}
    except httpx.TimeoutException:
        return {**result, "message": "读取模型列表超时。请检查网络或代理后重试，也可手动填写模型 ID；未修改已保存配置。"}
    except httpx.TransportError:
        return {**result, "message": "无法连接服务商。请检查 API 地址、网络、代理与 HTTPS 证书配置后重试；也可手动填写模型 ID。"}
    except (httpx.HTTPError, ValueError, TypeError, AttributeError):
        # Provider bodies/errors may contain keys or URLs. Do not relay them.
        return {**result, "message": "暂时无法读取服务商模型列表，可以重试或手动填写模型 ID。"}


router = APIRouter(prefix="/api/settings/model", tags=["system"])


@router.get("/catalog")
def read_catalog():
    return discover_models(UserSettingsService()._settings())


@router.post("/catalog")
def preview_catalog(payload: ModelCatalogRequest):
    saved = UserSettingsService()._settings()
    # A blank key must never forward saved credentials to a newly typed host.
    reuse = (saved.llm_provider == payload.provider and saved.llm_base_url == payload.base_url
             and saved.llm_proxy_url == payload.proxy_url and not payload.clear_api_key)
    key = "" if payload.clear_api_key else payload.api_key if payload.api_key and payload.api_key.get_secret_value().strip() else saved.llm_api_key if reuse else ""
    settings = saved.model_copy(update={"llm_provider": payload.provider, "llm_base_url": payload.base_url,
                                       "llm_proxy_url": payload.proxy_url, "llm_model": payload.model,
                                       "llm_api_key": key})
    # model_copy does not coerce values into SecretStr.
    from pydantic import SecretStr
    if isinstance(settings.llm_api_key, str):
        settings = settings.model_copy(update={"llm_api_key": SecretStr(settings.llm_api_key)})
    return discover_models(settings)


@router.put("/selection")
def select_model(payload: ModelSelection, request: Request):
    service = UserSettingsService()
    manager = request.app.state.kernel
    with manager.lock, settings_lock:
        if get_settings().llm_provider != "codex" and (manager.active or manager.busy_check()):
            raise HTTPException(409, detail={"message": "当前还有生成在进行，请完成后再切换模型。"})
        settings = service._settings()
        if connection_id(settings) != payload.connection_id:
            raise HTTPException(409, detail={"message": "连接配置已更新，请重新读取模型列表后再选择。"})
        if settings.llm_provider == "fake" or not settings.model_runtime_configured:
            raise HTTPException(422, detail={"message": "请先保存完整的模型连接配置。"})
        save_values(service.env_path, {"LLM_MODEL": payload.model})
        return {"model": payload.model, "connection_id": payload.connection_id}
