"""Verify native search evidence without requiring the API search service."""
from __future__ import annotations

from html.parser import HTMLParser
import json
from pathlib import Path
import re
import shutil
import subprocess
from urllib.parse import unquote, urlsplit

from planner_api.research.providers import ResearchProviderError
from planner_api.research.schemas import PageDocument


class BodyText(HTMLParser):
    def __init__(self, fragment=''):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.stack = []
        self.fragment_offset = None
        self.fragment = unquote(fragment)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        hidden = tag in {'script', 'style', 'nav', 'header', 'footer', 'aside', 'noscript'} or 'hidden' in attrs
        hidden = hidden or 'headerlink' in attrs.get('class', '').split()
        self.stack.append((tag, hidden or bool(self.stack and self.stack[-1][1])))
        if not self.stack[-1][1]:
            if tag in {'p', 'div', 'section', 'dt', 'dd', 'li', 'h1', 'h2', 'h3', 'pre', 'br'}:
                self.parts.append('\n')
            if self.fragment and attrs.get('id') == self.fragment and self.fragment_offset is None:
                self.fragment_offset = sum(map(len, self.parts))
        if tag in {'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link', 'meta', 'param', 'source', 'track', 'wbr'}:
            self.stack.pop()

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                self.stack = self.stack[:i]
                break
        if tag in {'p', 'div', 'section', 'dt', 'dd', 'li', 'pre'}:
            self.parts.append('\n')

    def handle_data(self, data):
        if not self.stack or not self.stack[-1][1]:
            self.parts.append(data)

    def excerpt(self, limit, focus=''):
        raw = ''.join(self.parts)
        start = self.fragment_offset
        if start is None and focus:
            # A verbatim native quote locates evidence in a long page; the text
            # still comes exclusively from the fetched body.
            compact = re.sub(r'\s+', ' ', raw)
            needle = re.sub(r'\s+', ' ', focus).strip()
            found = compact.find(needle)
            if found >= 0:
                raw, start = compact, max(0, found - 300)
        text = re.sub(r'[^\S\n]+', ' ', raw[start or 0:]).strip()
        text = re.sub(r'\n\s*\n+', '\n\n', text)
        return text[:limit], len(text) > limit or bool(start)


class PublicPageReader:
    def __init__(self, timeout=15):
        self.timeout = timeout
        self.script = Path(__file__).resolve().parents[4] / 'services/web-research/src/public-page.mjs'

    def read(self, request):
        try:
            node = shutil.which('node')
            if not node:
                raise OSError('Node runtime unavailable')
            result = subprocess.run([node, str(self.script)], input=json.dumps({
                'url': str(request.url), 'timeoutMs': max(1, int(self.timeout * 1000)),
            }), text=True, encoding='utf-8', capture_output=True, timeout=self.timeout + 1,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0), check=True)
            page = json.loads(result.stdout)
            parser = BodyText(urlsplit(str(request.url)).fragment)
            if 'html' in page['contentType']:
                parser.feed(page['body'])
            else:
                parser.parts.append(page['body'])
            content, truncated = parser.excerpt(request.max_chars, request.focus)
            if not content:
                raise ValueError('No readable body')
            return PageDocument(url=request.url, finalUrl=page['url'], content=content,
                contentType=page['contentType'], retrievalMethod='public-http-verified',
                truncated=truncated, qualityFlags=['STATIC_BODY_ONLY'] + (['TRUNCATED'] if truncated else []))
        except (OSError, subprocess.SubprocessError, ValueError, KeyError) as error:
            raise ResearchProviderError('公开网页正文读取未完成；保留证据缺口。') from error
