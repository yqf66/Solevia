from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from pydantic import ValidationError

from planner_api.config import Settings, get_settings

from .schemas import PageDocument, PageReadRequest, SearchRequest, SearchResponse

JSONTransport = Callable[[str, dict[str, object], float], dict[str, object]]


class ResearchProviderError(RuntimeError):
    def __init__(self, message: str, *, code: str = "RESEARCH_PROVIDER_ERROR") -> None:
        super().__init__(message)
        self.code = code


class SearchProvider(Protocol):
    def search(self, request: SearchRequest) -> SearchResponse: ...


class PageReaderProvider(Protocol):
    def read(self, request: PageReadRequest) -> PageDocument: ...


class ResearchProvider(SearchProvider, PageReaderProvider, Protocol):
    """Capability contract required by the Agent's web research tools."""


def _urllib_json_transport(
    url: str, payload: dict[str, object], timeout: float
) -> dict[str, object]:
    request = Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as error:
        try:
            envelope = json.loads(error.read().decode("utf-8"))
            detail = envelope.get("error") if isinstance(envelope, dict) else None
        except (ValueError, UnicodeError):
            detail = None
        raise ResearchProviderError(
            str(detail.get("message") or "本地联网搜索服务返回了错误。")[:500]
            if isinstance(detail, dict) else "本地联网搜索服务返回了错误。",
            code=str(detail.get("code") or f"RESEARCH_HTTP_{error.code}")[:80]
            if isinstance(detail, dict) else f"RESEARCH_HTTP_{error.code}",
        ) from error
    except (TimeoutError, URLError) as error:
        raise ResearchProviderError(
            "本地联网搜索服务暂时不可用，请稍后重试。",
            code="RESEARCH_UNAVAILABLE",
        ) from error


class OpenWebSearchProvider(SearchProvider, PageReaderProvider):
    """Agent-side adapter for the local Open-WebSearch wrapper."""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        transport: JSONTransport = _urllib_json_transport,
    ) -> None:
        self.settings = settings or get_settings()
        self.transport = transport
        self.base_url = self.settings.web_search_base_url.rstrip("/")
        self.timeout = self.settings.web_search_timeout_seconds

    def search(self, request: SearchRequest) -> SearchResponse:
        payload = {
            "query": request.query,
            "limit": min(request.limit, self.settings.web_search_max_results),
            "engines": request.engines,
            "searchMode": request.search_mode,
        }
        return self._decode(
            self.transport(f"{self.base_url}/v1/search", payload, self.timeout),
            SearchResponse,
        )

    def read(self, request: PageReadRequest) -> PageDocument:
        payload = {
            "url": str(request.url),
            "maxChars": request.max_chars,
            "renderMode": request.render_mode,
            "includeLinks": request.include_links,
            "focus": request.focus,
        }
        return self._decode(
            self.transport(f"{self.base_url}/v1/fetch", payload, self.timeout),
            PageDocument,
        )

    @staticmethod
    def _decode(payload: dict[str, object], model: type[Any]) -> Any:
        if payload.get("status") != "ok" or not isinstance(payload.get("data"), dict):
            error = payload.get("error")
            message = error.get("message") if isinstance(error, dict) else None
            code = error.get("code") if isinstance(error, dict) else None
            raise ResearchProviderError(
                message or "本地联网搜索服务返回了无效结果。",
                code=code or "INVALID_RESULT",
            )
        try:
            return model.model_validate(payload["data"])
        except ValidationError as error:
            raise ResearchProviderError(
                "本地联网搜索结果无法通过数据校验。", code="INVALID_RESULT"
            ) from error
