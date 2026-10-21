import pytest

from planner_api.config import Settings
from planner_api.research.providers import OpenWebSearchProvider, ResearchProviderError
from planner_api.research.schemas import PageReadRequest, SearchRequest
from planner_api.research.service import WebResearchService


def _settings() -> Settings:
    return Settings(
        _env_file=None,
        web_search_provider="open_websearch",
        web_search_base_url="http://127.0.0.1:3211",
        web_search_timeout_seconds=3,
    )


def test_open_websearch_provider_decodes_search_contract() -> None:
    calls: list[tuple[str, dict[str, object], float]] = []

    def transport(url: str, payload: dict[str, object], timeout: float) -> dict[str, object]:
        calls.append((url, payload, timeout))
        return {
            "status": "ok",
            "data": {
                "query": "本地部署",
                "results": [
                    {
                        "title": "示例结果",
                        "url": "https://example.com/article",
                        "snippet": "摘要",
                        "source": "example",
                        "engine": "sogou",
                    }
                ],
                "totalResults": 1,
                "engines": ["sogou", "bing"],
                "resultCounts": {"sogou": 1, "bing": 0},
                "partialFailures": [
                    {"engine": "bing", "code": "engine_error", "message": "blocked"}
                ],
                "searchedAt": "2026-09-23T00:00:00.000Z",
            },
        }

    provider = OpenWebSearchProvider(_settings(), transport=transport)
    response = provider.search(SearchRequest(query="本地部署", limit=5))

    assert response.query == "本地部署"
    assert response.total_results == 1
    assert response.searched_at.endswith("Z")
    assert response.results[0].engine == "sogou"
    assert response.result_counts == {"sogou": 1, "bing": 0}
    assert response.partial_failures[0].engine == "bing"
    assert calls == [
        (
            "http://127.0.0.1:3211/v1/search",
            {"query": "本地部署", "limit": 5, "engines": [], "searchMode": "auto"},
            3,
        )
    ]


def test_open_websearch_provider_decodes_page_contract() -> None:
    def transport(url: str, payload: dict[str, object], timeout: float) -> dict[str, object]:
        assert url.endswith("/v1/fetch")
        assert payload["renderMode"] == "browser"
        return {
            "status": "ok",
            "data": {
                "url": "https://example.com",
                "finalUrl": "https://example.com/",
                "title": "Example",
                "content": "正文内容",
                "retrievalMethod": "request",
                "contentType": "text/html",
                "readabilityApplied": True,
                "excerpt": "短摘要",
                "byline": "Author",
                "siteName": "Example",
                "truncated": False,
                "links": [],
                "qualityScore": 88,
                "qualityFlags": ["NO_READABILITY"],
            },
        }

    provider = OpenWebSearchProvider(_settings(), transport=transport)
    document = provider.read(
        PageReadRequest(url="https://example.com", render_mode="browser", max_chars=2000)
    )

    assert str(document.final_url).rstrip("/") == "https://example.com"
    assert document.retrieval_method == "request"
    assert document.content == "正文内容"
    assert document.content_type == "text/html"
    assert document.readability_applied is True
    assert document.excerpt == "短摘要"
    assert document.site_name == "Example"
    assert document.quality_score == 88
    assert document.quality_flags == ["NO_READABILITY"]


def test_provider_errors_are_structured() -> None:
    def transport(url: str, payload: dict[str, object], timeout: float) -> dict[str, object]:
        return {"status": "error", "error": {"message": "upstream unavailable"}}

    provider = OpenWebSearchProvider(_settings(), transport=transport)

    with pytest.raises(ResearchProviderError, match="upstream unavailable"):
        provider.search(SearchRequest(query="test"))


def test_provider_preserves_wrapper_error_code() -> None:
    def transport(url: str, payload: dict[str, object], timeout: float) -> dict[str, object]:
        return {
            "status": "error",
            "error": {"code": "UPSTREAM_TIMEOUT", "message": "search timed out"},
        }

    with pytest.raises(ResearchProviderError) as captured:
        OpenWebSearchProvider(_settings(), transport=transport).search(SearchRequest(query="test"))
    assert captured.value.code == "UPSTREAM_TIMEOUT"


def test_agent_service_exposes_explicit_tool_names() -> None:
    class StubProvider:
        def search(self, request: SearchRequest):
            return "search-result"

        def read(self, request: PageReadRequest):
            return "page-result"

    service = WebResearchService(StubProvider())

    assert service.search_tool.name == "web_search"
    assert service.read_tool.name == "web_read"
    assert service.search(SearchRequest(query="test")) == "search-result"
