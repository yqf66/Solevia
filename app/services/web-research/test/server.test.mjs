import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';

import { allocateSearchQuotas, githubReadmeRepositoryUrl, mergeSearchResponses, normalizeResults, publicHttpUrl, searchEngineWithRetry, selectReadmeContent } from '../src/server.mjs';
import { pageReaderProfile } from '../src/page-reader.mjs';

test('component files are present and package pins upstream version', async () => {
  const packageJson = JSON.parse(await readFile(new URL('../package.json', import.meta.url), 'utf8'));
  assert.equal(packageJson.dependencies['open-websearch'], '2.1.11');
  assert.equal(packageJson.scripts.postinstall, 'node scripts/patch-open-websearch.mjs');
  const patchSource = await readFile(
    new URL('../scripts/patch-open-websearch.mjs', import.meta.url),
    'utf8',
  );
  assert.match(patchSource, /requestWithSafeRedirects/);
  assert.match(patchSource, /Baidu search URL/);
});

test('fetch URL validation accepts public URLs and rejects private targets', () => {
  assert.equal(publicHttpUrl('https://example.com/doc#section'), 'https://example.com/doc#section');
  assert.equal(publicHttpUrl('http://127.0.0.1/private'), null);
  assert.equal(publicHttpUrl('http://172.24.0.1/private'), null);
  assert.equal(publicHttpUrl('http://[::1]/private'), null);
  assert.equal(publicHttpUrl('file:///etc/passwd'), null);
});

test('page reader routes known community domains to lightweight site adapters', () => {
  assert.equal(pageReaderProfile('https://blog.csdn.net/user/article/details/1'), 'csdn');
  assert.equal(pageReaderProfile('https://www.zhihu.com/question/1/answer/2'), 'zhihu');
  assert.equal(pageReaderProfile('https://www.xiaohongshu.com/explore/abc'), 'xiaohongshu');
  assert.equal(pageReaderProfile('https://example.com/article'), 'generic');
  assert.equal(pageReaderProfile('https://zhihu.com.evil.example/post'), 'generic');
  assert.equal(pageReaderProfile('https://www.sogou.com/link?url=redirect'), 'generic');
});

test('only repository roots use the GitHub README route', () => {
  assert.equal(githubReadmeRepositoryUrl('https://github.com/Aas-ee/open-webSearch'), 'https://github.com/Aas-ee/open-webSearch');
  assert.equal(githubReadmeRepositoryUrl('https://github.com/Aas-ee/open-webSearch/blob/main/README-zh.md'), null);
  assert.equal(githubReadmeRepositoryUrl('https://github.com/Aas-ee/open-webSearch/issues/1'), null);
  assert.equal(githubReadmeRepositoryUrl('https://github.com.evil.example/Aas-ee/open-webSearch'), null);
  assert.equal(githubReadmeRepositoryUrl('http://127.0.0.1/Aas-ee/open-webSearch'), null);
});

test('README excerpt keeps the introduction and features ahead of badges and sponsor copy', () => {
  const content = [
    '# Project', '[![Badge](https://img.example/badge)](https://example.com)',
    'A crawler for agents.', '## Sponsor', 'Advertisement and promo copy.',
    '## Installation', '```bash', 'pip install project', '```',
    '## Features', '- Structured extraction', '- Markdown output',
  ].join('\n');
  const selected = selectReadmeContent(content, 130);
  assert.match(selected, /A crawler for agents/);
  assert.match(selected, /Structured extraction/);
  assert.match(selected, /Markdown output/);
  assert.doesNotMatch(selected, /Badge|Advertisement|pip install/);
});

test('search result normalization deduplicates canonical URLs', () => {
  const normalized = normalizeResults('query', {
    engines: ['bing', 'sogou', 'baidu'],
    partialFailures: [{ engine: 'baidu', code: 'engine_error', message: 'network blocked' }],
    results: [
      { title: 'One', url: 'https://example.com/a?utm_source=bing#one', description: 'first', engine: 'bing' },
      { title: 'Two title', url: 'https://example.com/a#two', description: 'longer second', engine: 'sogou' },
    ],
  });

  assert.equal(normalized.totalResults, 1);
  assert.equal(normalized.results[0].url, 'https://example.com/a?utm_source=bing');
  assert.equal(normalized.results[0].snippet, 'longer second');
  assert.deepEqual(normalized.results[0].engines, ['bing', 'sogou']);
  assert.deepEqual(normalized.engines, ['bing', 'sogou', 'baidu']);
  assert.deepEqual(normalized.resultCounts, { bing: 1, sogou: 1, baidu: 0 });
  assert.deepEqual(normalized.partialFailures, [
    { engine: 'baidu', code: 'engine_error', message: 'network blocked' },
  ]);
});

test('search quotas are distributed with a minimum per engine', () => {
  assert.deepEqual(allocateSearchQuotas(12, ['bing', 'duckduckgo', 'baidu']), [4, 4, 4]);
  assert.deepEqual(allocateSearchQuotas(2, ['bing', 'duckduckgo', 'baidu']), [1, 1, 1]);
  assert.deepEqual(allocateSearchQuotas(5, ['bing', 'duckduckgo']), [3, 2]);
});

test('search diagnostics retain bounded browser retry metadata', () => {
  const normalized = normalizeResults('query', {
    engines: ['bing'], results: [],
    partialFailures: [{ engine: 'bing', code: 'engine_error', message: 'blocked' }],
    attemptedModes: ['auto', 'playwright'], retryCount: 1, retryReason: 'ALL_ENGINES_FAILED',
  });
  assert.deepEqual(normalized.attemptedModes, ['auto', 'playwright']);
  assert.equal(normalized.retryCount, 1);
  assert.equal(normalized.retryReason, 'ALL_ENGINES_FAILED');
});

test('empty upstream results retain all engine failures', () => {
  const normalized = normalizeResults('query', {
    engines: ['bing'], results: [],
    partialFailures: [{ engine: 'bing', code: 'engine_error', message: 'connect EACCES' }],
  });
  assert.equal(normalized.totalResults, 0);
  assert.deepEqual(normalized.resultCounts, { bing: 0 });
  assert.equal(normalized.partialFailures[0].message, 'connect EACCES');
});

test('ambiguous DuckDuckGo empty result is retried once and reported as failure', async () => {
  let calls = 0;
  const result = await searchEngineWithRetry(
    { query: 'q' }, 'duckduckgo', 4,
    async () => { calls += 1; return { engines: ['duckduckgo'], results: [], partialFailures: [] }; },
    async () => {},
  );
  assert.equal(calls, 2);
  assert.equal(result.partialFailures[0].code, 'AMBIGUOUS_EMPTY_RESULTS');
});

test('search response refill merges duplicate URLs before applying the limit', () => {
  const merged = mergeSearchResponses('q', [
    { engines: ['bing'], results: [{ url: 'https://example.com/a', title: 'A' }] },
    { engines: ['duckduckgo'], results: [{ url: 'https://example.com/a?utm_source=x', title: 'A2' }, { url: 'https://example.com/b', title: 'B' }] },
  ], 2);
  assert.deepEqual(merged.results.map((item) => item.url), ['https://example.com/a', 'https://example.com/b']);
});
