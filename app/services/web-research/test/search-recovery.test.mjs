import test from 'node:test';
import assert from 'node:assert/strict';
import { assertDuckDuckGoEmpty, assertDuckDuckGoPage, searchFailure } from '../src/search-failure.mjs';
import { searchEngineWithRetry, searchWithQuotaCompensation } from '../src/server.mjs';

test('verification pages and malformed pages are distinct from confirmed empty results', () => {
  assert.throws(() => assertDuckDuckGoPage('<form action="anomaly.js">'), { code: 'SEARCH_CHALLENGE' });
  assert.throws(() => assertDuckDuckGoEmpty('<html>changed layout</html>'), { code: 'SEARCH_PARSE_FAILED' });
  assert.doesNotThrow(() => assertDuckDuckGoEmpty('<div class="no-results">No results found</div>'));
  assert.equal(searchFailure({ response: { status: 429 } }).code, 'SEARCH_RATE_LIMIT');
  assert.equal(searchFailure({ code: 'ETIMEDOUT' }).code, 'SEARCH_TRANSIENT');
});

test('temporary engine failure recovers once and retains retry diagnostics', async () => {
  let calls = 0;
  const result = await searchEngineWithRetry({ query: 'q' }, 'duckduckgo', 4, async () => {
    calls += 1;
    return calls === 1
      ? { results: [], partialFailures: [{ engine: 'duckduckgo', code: 'SEARCH_TRANSIENT' }] }
      : { results: [{ url: 'https://example.org/rule' }], partialFailures: [] };
  }, async () => {});
  assert.equal(calls, 2);
  assert.equal(result.results.length, 1);
  assert.equal(result.retryCount, 1);
  assert.equal(result.retryReason, 'SEARCH_TRANSIENT');
});

test('challenge and confirmed empty results do not trigger repeated requests', async () => {
  for (const data of [
    { results: [], partialFailures: [{ engine: 'duckduckgo', code: 'SEARCH_CHALLENGE' }] },
    { results: [], partialFailures: [], emptyConfirmedEngines: ['duckduckgo'] },
  ]) {
    let calls = 0;
    await searchEngineWithRetry({ query: 'q' }, 'duckduckgo', 4, async () => { calls += 1; return data; }, async () => {});
    assert.equal(calls, 1);
  }
});

test('expired search deadline cannot start another network request', async () => {
  let calls = 0;
  const result = await searchEngineWithRetry({ query: 'q' }, 'duckduckgo', 4,
    async () => { calls += 1; }, async () => {}, Date.now() - 1);
  assert.equal(calls, 0);
  assert.equal(result.partialFailures[0].code, 'SEARCH_TIMEOUT');
});

test('slow engine receives a bounded budget while the successful engine can fill results', async () => {
  const budgets = [];
  const data = await searchWithQuotaCompensation({ query: 'rule', limit: 4, engines: ['bing', 'duckduckgo'] },
    Date.now() + 60000, async (_path, request, timeout) => {
      const engine = request.engines[0];
      budgets.push({ engine, timeout });
      if (engine === 'duckduckgo') throw Object.assign(new Error('timeout'), { code: 'SEARCH_TIMEOUT' });
      return { engines: [engine], results: Array.from({ length: request.limit }, (_, i) => ({
        url: `https://example.org/rule/${i}`, title: `Rule ${i}`, engine,
      })), partialFailures: [] };
    });
  assert.equal(data.results.length, 4);
  assert.ok(budgets.filter(row => row.engine === 'duckduckgo').every(row => row.timeout <= 12000));
  assert.ok(data.partialFailures.some(row => row.engine === 'duckduckgo'));
});

test('fetch aborts retry once and still expose failure after exhaustion', async () => {
  let calls = 0;
  const result = await searchEngineWithRetry({ query: 'q' }, 'bing', 4, async () => {
    calls += 1;
    throw Object.assign(new Error('aborted'), { name: 'AbortError' });
  }, async () => {});
  assert.equal(calls, 2);
  assert.equal(result.retryCount, 1);
  assert.equal(result.partialFailures[0].code, 'SEARCH_TIMEOUT');
});

test('installed DuckDuckGo adapter exposes challenges and confirmed no-results separately', async () => {
  const { default: axios } = await import('../node_modules/axios/index.js');
  const { searchDuckDuckGo } = await import('../node_modules/open-websearch/build/engines/duckduckgo/searchDuckDuckGo.js');
  const get = axios.get;
  const post = axios.post;
  try {
    let calls = 0;
    axios.get = async () => { calls += 1; return { data: '<form action="anomaly.js">' }; };
    axios.post = async () => { throw new Error('must not repeat a challenge'); };
    await assert.rejects(searchDuckDuckGo('q', 2), { code: 'SEARCH_CHALLENGE' });
    assert.equal(calls, 1);
    axios.get = async () => ({ data: '<html>no preload</html>' });
    axios.post = async () => ({ data: '<div class="no-results">No results found</div>' });
    const empty = await searchDuckDuckGo('q', 2);
    assert.equal(empty.length, 0);
    assert.equal(empty.emptyConfirmed, true);
    axios.post = async () => ({ data: '<html>unexpected layout</html>' });
    await assert.rejects(searchDuckDuckGo('q', 2), { code: 'SEARCH_PARSE_FAILED' });
    axios.post = async () => { throw Object.assign(new Error('timeout'), { code: 'ETIMEDOUT' }); };
    await assert.rejects(searchDuckDuckGo('q', 2), { code: 'SEARCH_TRANSIENT' });
  } finally {
    axios.get = get;
    axios.post = post;
  }
});
