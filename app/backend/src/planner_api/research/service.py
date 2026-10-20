from __future__ import annotations

from .providers import (
    OpenWebSearchProvider,
    PageReaderProvider,
    ResearchProvider,
    SearchProvider,
)
from .schemas import PageDocument, PageReadRequest, SearchRequest, SearchResponse


class WebSearchTool:
    name = "web_search"

    def __init__(self, provider: SearchProvider) -> None:
        self.provider = provider

    def invoke(self, request: SearchRequest) -> SearchResponse:
        return self.provider.search(request)


class WebReadTool:
    name = "web_read"

    def __init__(self, provider: PageReaderProvider) -> None:
        self.provider = provider

    def invoke(self, request: PageReadRequest) -> PageDocument:
        return self.provider.read(request)


class WebResearchService:
    """Agent-owned tool facade; orchestration is intentionally added later."""

    def __init__(self, provider: ResearchProvider | None = None) -> None:
        provider = provider or OpenWebSearchProvider()
        self.search_tool = WebSearchTool(provider)
        self.read_tool = WebReadTool(provider)

    def search(self, request: SearchRequest) -> SearchResponse:
        return self.search_tool.invoke(request)

    def read(self, request: PageReadRequest) -> PageDocument:
        return self.read_tool.invoke(request)
