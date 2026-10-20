"""Bounded Crawl4AI reader worker; stdin request, stdout JSON, no LLM extraction."""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import socket
import sys
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[3]
ARCHIVES = Path(os.environ.get("SOLEVIA_WEB_ARCHIVES", str(ROOT / ".dev-logs" / "web-page-originals")))
READ_STAGE = "URL_VALIDATION"
sys.path.insert(0, str(ROOT / "backend" / "src"))


def assert_public_url(value: str) -> None:
    parsed = urlsplit(value)
    host = parsed.hostname or ""
    if parsed.scheme not in {"http", "https"} or not host or parsed.username or parsed.password:
        raise ValueError("Only public HTTP(S) URLs without credentials are allowed")
    if host == "localhost" or host.endswith(".localhost"):
        raise ValueError("Private navigation is not allowed")
    addresses = socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == "https" else 80), type=socket.SOCK_STREAM)
    if not addresses or any(not ipaddress.ip_address(row[4][0]).is_global for row in addresses):
        raise ValueError("Navigation resolved to a private target")


def browser_channel() -> str:
    """Keep Crawl4AI, using an installed browser when the service cannot
    access Playwright's user-cache download. No permissions are changed.
    """
    if sys.platform != "win32":
        return "chromium"
    cache = Path(os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or
                 str(Path(os.environ.get("LOCALAPPDATA", "")) / "ms-playwright"))
    if any(cache.glob("chromium-*/chrome-win*/chrome.exe")):
        return "chromium"
    installed = [
        (Path(os.environ.get("PROGRAMFILES(X86)", "C:/Program Files (x86)")) /
         "Microsoft/Edge/Application/msedge.exe", "msedge"),
        (Path(os.environ.get("PROGRAMFILES", "C:/Program Files")) /
         "Google/Chrome/Application/chrome.exe", "chrome"),
        (Path(os.environ.get("PROGRAMFILES", "C:/Program Files")) /
         "Microsoft/Edge/Application/msedge.exe", "msedge"),
        (Path(os.environ.get("PROGRAMFILES(X86)", "C:/Program Files (x86)")) /
         "Google/Chrome/Application/chrome.exe", "chrome"),
        (Path(os.environ.get("LOCALAPPDATA", "")) /
         "Google/Chrome/Application/chrome.exe", "chrome"),
    ]
    return next((channel for path, channel in installed if path.is_file()), "chromium")


def focused_html(html: str, url: str, focus: str) -> tuple[str, bool]:
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "lxml")
    target = soup.find(id=unquote(urlsplit(url).fragment)) if urlsplit(url).fragment else None
    if target is None:
        names = re.findall(r"(?<![A-Za-z0-9_-])[A-Z][a-z]+(?:[A-Z][A-Za-z0-9]*)+(?![A-Za-z0-9_])", focus)
        def key(text):
            return re.sub(r"[^a-z0-9]", "", text.casefold()).rstrip("s")
        for name in names:
            target = next((heading for heading in soup.find_all(re.compile(r"^h[1-6]$"))
                           if key(heading.get_text()) == key(name)), None)
            if target is not None:
                break
    if target is None:
        return html, False
    # Python API definitions use dt/dd; general docs and RFC use sections.
    if target.name == "dt":
        description = target.find_next_sibling("dd")
        if description is not None:
            # Shared version/exception notes can precede all definitions in
            # this section. Keep that introductory context without unrelated APIs.
            section = target.find_parent("section")
            introduction = []
            if section is not None:
                for child in section.children:
                    if getattr(child, "name", None) and (child.name == "dt" or child.find("dt")):
                        break
                    introduction.append(str(child))
            return "".join(introduction) + str(target) + str(description), True
    section = target if target.name == "section" else target.find_parent("section")
    return (str(section), True) if section is not None else (html, False)


def body_html(html: str, url: str) -> str:
    """Remove structural navigation only; never prune prose by relevance score."""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "lxml")
    for navigation in soup.select("nav, [role=navigation], [role=doc-toc]"):
        navigation.decompose()
    if (urlsplit(url).hostname or "").removeprefix("www.") == "sqlite.org":
        # SQLite explicitly marks its menu/TOC as nosearch. Keep article prose.
        for navigation in soup.select("div.nosearch"):
            if not navigation.find("p"):
                navigation.decompose()
    main = soup.find("main") or soup.find("article") or soup.find(attrs={"role": "main"})
    return str(main or soup.body or soup)


def make_document(html: str, url: str, final_url: str, title: str, max_chars: int, focus: str,
                  archive_root: Path = ARCHIVES) -> dict:
    from planner_api.evidence_windows import focused_excerpt_window
    from crawl4ai.markdown_generation_strategy import DefaultMarkdownGenerator
    from crawl4ai.content_filter_strategy import PruningContentFilter

    if len(html.encode("utf-8")) > 2_000_000:
        raise ValueError("Original HTML exceeds reader archive budget")
    generator = DefaultMarkdownGenerator(content_source="raw_html", options={"body_width": 0, "inline_links": False},
        content_filter=PruningContentFilter(threshold=0.4))
    original = generator.generate_markdown(html, base_url=final_url)
    selected, focused = focused_html(html, url, focus)
    if not focused:
        selected = body_html(html, final_url)
    # fit_markdown is archived for discovery only. It is never the evidence.
    markdown = generator.generate_markdown(selected, base_url=final_url).raw_markdown
    if not markdown.strip():
        raise ValueError("Crawl4AI extracted no markdown")
    # Formatting changes must not overwrite an earlier immutable original.
    digest = hashlib.sha256(json.dumps([final_url, html, original.raw_markdown,
        original.fit_markdown or "", selected, markdown], ensure_ascii=False).encode("utf-8")).hexdigest()
    folder = archive_root / digest
    folder.mkdir(parents=True, exist_ok=True)
    artifacts = {"html": html, "raw_markdown": original.raw_markdown,
                 "fit_markdown": original.fit_markdown or "", "section_html": selected,
                 "section_markdown": markdown}
    extensions = {"html": "original.html", "raw_markdown": "original.md", "fit_markdown": "retrieval.md",
                  "section_html": "section.html", "section_markdown": "section.md"}
    window = focused_excerpt_window(markdown, focus, max_chars)
    archive = {"archive_id": digest, "reader": "crawl4ai", "evidence_basis": "section_markdown",
               "reader_window": window.locator(),
               "url": url, "final_url": final_url, "artifacts": {}}
    for kind, value in artifacts.items():
        data = value.encode("utf-8")
        path = folder / extensions[kind]
        path.write_bytes(data)
        archive["artifacts"][kind] = {"path": str(path.relative_to(ROOT)) if path.is_relative_to(ROOT) else str(path),
                                      "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}
    (folder / "manifest.json").write_text(json.dumps(archive, ensure_ascii=False, indent=2), encoding="utf-8")
    content = window.text
    return {"url": url, "finalUrl": final_url, "title": title, "content": content,
            "retrievalMethod": "crawl4ai:raw-markdown", "contentType": "text/markdown; source=text/html",
            "readabilityApplied": False, "excerpt": content[:1000], "byline": "", "siteName": "",
            "truncated": len(markdown) > max_chars, "links": [], "qualityScore": 0,
            "qualityFlags": ["CRAWL4AI", "RAW_MARKDOWN_EVIDENCE", "ORIGINAL_ARCHIVED"]
                            + (["SECTION_FOCUSED"] if focused else []),
            "evidenceArchive": archive}


async def read(request: dict) -> dict:
    global READ_STAGE
    probe = request.get("probe") is True
    url = "raw:<html><body><p>Reader readiness check.</p></body></html>" if probe else str(request["url"])
    if not probe:
        assert_public_url(url)
    base = (ARCHIVES.parent / "crawl4ai-runtime" if os.environ.get("SOLEVIA_WEB_ARCHIVES")
            else ROOT / ".dev-logs" / "crawl4ai-runtime")
    base.mkdir(parents=True, exist_ok=True)
    # A managed Python runtime has no inherited user profile. Keep library
    # home/cache discovery in this workspace rather than the OS user folder.
    os.environ["USERPROFILE"] = str(base)
    os.environ["HOME"] = str(base)
    # Set before importing Crawl4AI: its database singleton resolves this at import.
    os.environ["CRAWL4_AI_BASE_DIRECTORY"] = str(base)
    # Crawl4AI's base directory is intentionally project-local for its cache,
    # while Playwright's installed browsers live in the user cache. Without
    # this explicit path a fresh service process reports a misleading
    # “browser missing” error even though the managed Chromium is installed.
    browser_cache = Path(os.environ.get("LOCALAPPDATA", "")) / "ms-playwright"
    if browser_cache.is_dir():
        os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(browser_cache))
    READ_STAGE = "IMPORT"
    from crawl4ai import AsyncWebCrawler, BrowserConfig, CacheMode, CrawlerRunConfig
    from planner_api.browser_sandbox import contain_crawler

    browser = BrowserConfig(headless=True, verbose=False, ignore_https_errors=False,
                            chrome_channel=browser_channel(),
                            proxy_config=request.get("proxyServer") or None)
    config = CrawlerRunConfig(cache_mode=CacheMode.BYPASS, page_timeout=20_000, verbose=False)
    READ_STAGE = "BROWSER_START"
    crawler_instance = AsyncWebCrawler(config=browser, base_directory=str(base))
    contain_crawler(crawler_instance)
    async with crawler_instance as crawler:
        if probe:
            result = await crawler.arun(url=url, config=config)
            if not result.success or "Reader readiness check." not in result.markdown.raw_markdown:
                raise RuntimeError("Reader readiness failed")
            return {"ready": True, "component": "crawl4ai"}
        async def guard(page, **_kwargs):
            async def route_guard(route):
                if route.request.resource_type in {"image", "media", "font"}:
                    return await route.abort()
                try:
                    parsed = urlsplit(route.request.url)
                    if parsed.scheme not in {"http", "https"} or parsed.username or parsed.password:
                        raise ValueError("Invalid request scheme")
                except (ValueError, OSError):
                    return await route.abort()
                return await route.continue_()
            await page.route("**/*", route_guard)
            return page
        crawler.crawler_strategy.set_hook("on_page_context_created", guard)
        READ_STAGE = "NAVIGATION"
        result = await crawler.arun(url=url, config=config)
        if not result.success or (result.status_code and result.status_code >= 400):
            raise RuntimeError("Crawl4AI page read failed")
        final_url = result.redirected_url or url
        assert_public_url(final_url)
        READ_STAGE = "ARCHIVE"
        return make_document(result.html, url, final_url, (result.metadata or {}).get("title", ""),
                             min(max(int(request.get("maxChars", 12000)), 1000), 30000),
                             str(request.get("focus", ""))[:500])


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    try:
        from planner_api.worker_sandbox import limit_current_process
        limit_current_process(4096)
        request = json.loads(sys.stdin.buffer.read(100_001))
        # Library diagnostics must not corrupt the stdout JSON protocol.
        with contextlib.redirect_stdout(sys.stderr):
            result = asyncio.run(asyncio.wait_for(read(request), timeout=25))
        print(json.dumps(result, ensure_ascii=False))
    except Exception as error:
        # Stable diagnostics only: no URLs, proxy credentials or exception text.
        category = type(error).__name__.upper()
        if READ_STAGE == "BROWSER_START":
            message = str(error)
            missing = re.search(r"Executable doesn't exist at ([^\r\n]+)", message)
            if missing:
                diagnostic = ROOT / ".dev-logs" / "crawl4ai-browser-missing.json"
                diagnostic.write_text(json.dumps({
                    "python": sys.executable,
                    "expected_browser": missing.group(1)[:1000],
                    "expected_exists": Path(missing.group(1).strip()).exists(),
                    "local_cache_exists": (Path(os.environ.get("LOCALAPPDATA", "")) / "ms-playwright").is_dir(),
                    "local_appdata": os.environ.get("LOCALAPPDATA", ""),
                    "browser_cache": os.environ.get("PLAYWRIGHT_BROWSERS_PATH", ""),
                }), encoding="utf-8")
            for marker, label in [("Executable doesn't exist", "BROWSER_MISSING"),
                                  ("Target page, context or browser has been closed", "BROWSER_CLOSED"),
                                  ("EACCES", "ACCESS_DENIED"), ("Access is denied", "ACCESS_DENIED")]:
                if marker in message:
                    category = label
                    break
        print(json.dumps({"error": type(error).__name__,
                          "code": "CRAWL4AI_" + READ_STAGE + "_" + category,
                          "errno": getattr(error, "errno", None),
                          "winerror": getattr(error, "winerror", None)}))
        sys.exit(1)
