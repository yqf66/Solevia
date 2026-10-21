import { existsSync, readFileSync } from 'node:fs';
import { spawn } from 'node:child_process';
import { createServer } from 'node:http';
import { publicHttpUrl } from './network-policy.mjs';
export { publicHttpUrl } from './network-policy.mjs';
import { createEgressProxy, resolvePublicRedirect } from './egress-proxy.mjs';
import { workerEnvironment } from './worker-environment.mjs';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { URL } from 'node:url';

import { closeBrowserReader, readRenderedPage, probeBrowserReader } from './page-reader.mjs';
import { readCrawl4AI } from './crawl4ai-reader.mjs';

loadLocalEnv();

function loadLocalEnv() {
  const path = new URL('../.env', import.meta.url);
  if (!existsSync(path)) return;
  for (const rawLine of readFileSync(path, 'utf8').split(/\r?\n/)) {
    const line = rawLine.trim();
    if (!line || line.startsWith('#')) continue;
    const separator = line.indexOf('=');
    if (separator <= 0) continue;
    const key = line.slice(0, separator).trim();
    const value = line.slice(separator + 1).trim().replace(/^(['"])(.*)\1$/, '$2');
    if (key && process.env[key] === undefined) process.env[key] = value;
  }
}

const config = {
  host: process.env.WEB_RESEARCH_HOST || '127.0.0.1',
  port: numberEnv('WEB_RESEARCH_PORT', 3211, 1024, 65535),
  upstreamHost: process.env.OPEN_WEBSEARCH_HOST || '127.0.0.1',
  upstreamPort: numberEnv('OPEN_WEBSEARCH_PORT', 3210, 1024, 65535),
  // The Agent normally sends a purpose-aware subset. This is only the
  // direct-HTTP fallback, so keep it diverse without including every engine.
  engines: csvEnv('WEB_RESEARCH_ENGINES', ['bing', 'duckduckgo', 'baidu']),
  searchTimeoutMs: numberEnv('WEB_RESEARCH_SEARCH_TIMEOUT_MS', 60000, 1000, 120000),
  fetchTimeoutMs: numberEnv('WEB_RESEARCH_FETCH_TIMEOUT_MS', 30000, 1000, 120000),
  maxQueryChars: numberEnv('WEB_RESEARCH_MAX_QUERY_CHARS', 1000, 20, 5000),
  maxResults: numberEnv('WEB_RESEARCH_MAX_RESULTS', 20, 1, 50),
  maxFetchChars: numberEnv('WEB_RESEARCH_MAX_FETCH_CHARS', 30000, 1000, 200000),
  retryBrowserOnAllFailure: process.env.WEB_RESEARCH_RETRY_BROWSER_ON_FAILURE !== 'false',
  proxyUrl: process.env.WEB_RESEARCH_PROXY_URL || process.env.PROXY_URL || '',
  useProxy: process.env.WEB_RESEARCH_USE_PROXY === 'true' || process.env.USE_PROXY === 'true',
  manageUpstream: process.env.WEB_RESEARCH_MANAGE_UPSTREAM !== 'false',
  reader: process.env.WEB_RESEARCH_READER || 'crawl4ai',
};
if (!['crawl4ai', 'legacy'].includes(config.reader)) {
  throw new Error('WEB_RESEARCH_READER must be crawl4ai or legacy');
}

const upstreamBaseUrl = `http://${config.upstreamHost}:${config.upstreamPort}`;
let upstreamProcess;
let shuttingDown = false;
let suspended = false;
let activeWork = 0;
let suspending;
const readinessClients = new Map();
let readerReadyAt = 0;
let readerProbe;

async function readerReadiness() {
  if (Date.now() - readerReadyAt < 300000) return;
  if (!readerProbe) {
    readerProbe = (config.reader === 'crawl4ai'
      ? readCrawl4AI({ probe: true }, { timeoutMs: 30000 })
      : probeBrowserReader({ executablePath: resolveBrowserPath(), proxyServer: config.useProxy ? config.proxyUrl : '' }))
      .then(() => { readerReadyAt = Date.now(); })
      .finally(() => { readerProbe = undefined; });
  }
  await readerProbe;
}

function numberEnv(name, fallback, min, max) {
  const value = Number.parseInt(process.env[name] || '', 10);
  return Number.isInteger(value) && value >= min && value <= max ? value : fallback;
}

function csvEnv(name, fallback) {
  const values = (process.env[name] || '').split(',').map((value) => value.trim()).filter(Boolean);
  return values.length ? values.slice(0, 10) : fallback;
}

export function allocateSearchQuotas(limit, engines) {
  const count = engines.length;
  if (!count) return [];
  // A small overall result limit must not silently disable selected engines.
  // Ask every selected engine for one candidate, then let mergeSearchResponses
  // enforce the caller's final result limit. When there is room, distribute
  // the remaining quota as evenly as possible.
  if (limit < count) return engines.map(() => 1);
  const base = Math.floor(limit / count);
  const remainder = limit % count;
  return engines.map((_, index) => base + (index < remainder ? 1 : 0));
}

function json(res, status, body) {
  const payload = JSON.stringify(body);
  res.writeHead(status, { 'content-type': 'application/json; charset=utf-8' });
  res.end(payload);
}

function errorBody(code, message) {
  return { status: 'error', data: null, error: { code, message } };
}

async function readBody(req) {
  let raw = '';
  for await (const chunk of req) {
    raw += chunk;
    if (raw.length > 100_000) throw new Error('request body too large');
  }
  if (!raw) return {};
  try {
    return JSON.parse(raw);
  } catch {
    throw new Error('request body must be valid JSON');
  }
}

async function upstreamRequest(path, body, timeoutMs) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await fetch(`${upstreamBaseUrl}${path}`, {
      method: body === undefined ? 'GET' : 'POST',
      headers: body === undefined ? undefined : { 'content-type': 'application/json' },
      body: body === undefined ? undefined : JSON.stringify(body),
      signal: controller.signal,
    });
    const payload = await response.json();
    if (!response.ok || payload.status === 'error') {
      const message = payload?.error?.message || `upstream returned HTTP ${response.status}`;
      const error = new Error(message);
      error.code = payload?.error?.code || `UPSTREAM_HTTP_${response.status}`;
      throw error;
    }
    return payload.data;
  } finally {
    clearTimeout(timer);
  }
}

function normalizeUrl(value) {
  try {
    const url = new URL(String(value));
    url.hash = '';
    return url.toString();
  } catch {
    return String(value || '');
  }
}

function searchIdentity(value) {
  try {
    const url = new URL(String(value));
    url.hash = '';
    for (const key of [...url.searchParams.keys()]) {
      const normalized = key.toLowerCase();
      if (normalized.startsWith('utm_') || ['fbclid', 'gclid', 'ref', 'ref_src', 'source', 'spm', 'yclid'].includes(normalized)) {
        url.searchParams.delete(key);
      }
    }
    url.searchParams.sort();
    url.pathname = url.pathname.replace(/\/{2,}/g, '/').replace(/\/$/, '') || '/';
    return url.toString();
  } catch {
    return String(value || '');
  }
}

async function resolveKnownRedirectUrls(data) {
  const results = await Promise.all((data?.results || []).map(async (item) => {
    const raw = String(item?.url || "");
    try {
      const parsed = new URL(raw);
      const host = parsed.hostname.toLowerCase();
      const knownRedirect =
        (host === "sogou.com" || host.endsWith(".sogou.com")) && parsed.pathname === "/link" ||
        (host === "baidu.com" || host.endsWith(".baidu.com")) && parsed.pathname === "/link" ||
        (host === "bing.com" || host.endsWith(".bing.com")) && parsed.pathname.startsWith("/ck/");
      if (!knownRedirect) return item;
      try {
        const finalUrl = await resolvePublicRedirect(raw, {
          proxyServer: config.useProxy ? config.proxyUrl : '', timeoutMs: 5000,
        });
        if (finalUrl && finalUrl !== raw && /^https?:/i.test(finalUrl)) {
          return { ...item, url: finalUrl };
        }
      } catch { /* Preserve original search URL when expansion is unavailable. */ }
    } catch {
      // Keep the original result; the Agent will discard unresolved redirects.
    }
    return item;
  }));
  return { ...data, results };
}
export function normalizeResults(query, data) {
  const seen = new Map();
  const results = [];
  const engines = Array.isArray(data?.engines) ? data.engines.map(String) : [];
  const partialFailures = Array.isArray(data?.partialFailures) ? data.partialFailures.map((failure) => ({
    engine: String(failure.engine || '').slice(0, 40),
    code: String(failure.code || 'engine_error').slice(0, 80),
    message: String(failure.message || '').slice(0, 500),
  })) : [];
  const resultCounts = Object.fromEntries(engines.map((engine) => [engine, 0]));
  for (const item of data?.results || []) {
    const url = normalizeUrl(item.url);
    const identity = searchIdentity(url);
    if (!url || !identity) continue;
    const engines = [...new Set([
      ...(Array.isArray(item.engines) ? item.engines : []),
      ...(item.engine ? [item.engine] : []),
    ].map((value) => String(value).trim()).filter(Boolean))];
    for (const engine of engines) resultCounts[engine] = (resultCounts[engine] || 0) + 1;
    const candidate = {
      title: String(item.title || '').trim(),
      url,
      snippet: String(item.description || item.snippet || '').trim(),
      source: String(item.source || '').trim(),
      engine: engines[0] || '',
      engines,
      publishedAt: typeof (item.publishedAt || item.publishedDate || item.date) === 'string'
        ? String(item.publishedAt || item.publishedDate || item.date) : null,
    };
    const existing = seen.get(identity);
    if (existing) {
      existing.engines = [...new Set([...existing.engines, ...candidate.engines])];
      if (candidate.snippet.length > existing.snippet.length) existing.snippet = candidate.snippet;
      if (candidate.title.length > existing.title.length) existing.title = candidate.title;
      continue;
    }
    seen.set(identity, candidate);
    results.push(candidate);
  }
  return {
    query,
    results,
    totalResults: results.length,
    engines,
    resultCounts,
    partialFailures,
    attemptedModes: Array.isArray(data?.attemptedModes) ? data.attemptedModes : [],
    retryCount: Number.isInteger(data?.retryCount) ? data.retryCount : 0,
    retryReason: data?.retryReason || null,
    searchedAt: new Date().toISOString(),
  };
}

export function githubReadmeRepositoryUrl(value) {
  const publicUrl = publicHttpUrl(value);
  if (!publicUrl) return null;
  const parsed = new URL(publicUrl);
  if (parsed.hostname.toLowerCase() !== 'github.com' && parsed.hostname.toLowerCase() !== 'www.github.com') return null;
  const parts = parsed.pathname.split('/').filter(Boolean);
  if (parts.length < 2 || !parts.slice(0, 2).every((part) => /^[\w.-]+$/.test(part))) return null;
  // The upstream helper reads the default README, not the blob URL's exact file.
  // Restrict this route to repository roots so citations never misattribute a file.
  if (parts.length !== 2) return null;
  return `https://github.com/${parts[0]}/${parts[1]}`;
}

export function selectReadmeContent(content, maxChars) {
  const sections = String(content).split(/(?=^##\s+)/m);
  const clean = (section) => {
    let inCodeBlock = false;
    return section.split(/\r?\n/).filter((line) => {
      if (/^\s*```/.test(line)) { inCodeBlock = !inCodeBlock; return false; }
      if (inCodeBlock || /^\s*(?:!\[|\[!\[)/.test(line)) return false;
      return true;
    }).map((line) => line.replace(/<[^>]*>/g, '').trimEnd())
      .filter((line) => line.trim()).join('\n');
  };
  const heading = (section) => section.split(/\r?\n/, 1)[0];
  const featureSections = sections.slice(1).filter((section) =>
    /feature|capabilit|核心功能|功能特点|特性/i.test(heading(section)));
  const otherSections = sections.slice(1).filter((section) =>
    !featureSections.includes(section) && !/sponsor|赞助/i.test(heading(section)));
  return [sections[0], ...featureSections, ...otherSections]
    .map(clean).filter(Boolean).join('\n\n').slice(0, maxChars);
}

async function handleSearch(body) {
  const deadline = Date.now() + config.searchTimeoutMs;
  const query = typeof body.query === 'string' ? body.query.trim() : '';
  if (!query || query.length > config.maxQueryChars) {
    throw Object.assign(new Error(`query must be 1-${config.maxQueryChars} characters`), { code: 'INVALID_QUERY' });
  }
  const limit = Math.min(Math.max(Number(body.limit) || 10, 1), config.maxResults);
  const requested = Array.isArray(body.engines) && body.engines.length
    ? body.engines.slice(0, 10) : config.engines;
  const searchMode = ['request', 'playwright'].includes(body.searchMode) ? body.searchMode : 'auto';
  const request = { query, limit, engines: requested, searchMode };
  const attemptedModes = [searchMode];
  let data = await searchWithQuotaCompensation(request, deadline);
  const allFailed = requested.length > 0 &&
    Array.isArray(data?.partialFailures) &&
    data.partialFailures.length >= requested.length &&
    !(data?.results || []).length;
  if (allFailed && requested.includes('bing') && config.retryBrowserOnAllFailure && searchMode !== 'playwright' && deadline - Date.now() > 1000) {
    attemptedModes.push('playwright');
    try {
      const retryData = await upstreamRequest(
        '/search', { ...request, engines: ['bing'], searchMode: 'playwright' }, deadline - Date.now()
      );
      if ((retryData?.results || []).length > (data?.results || []).length) {
        data = retryData;
      } else {
        data = {
          ...data,
          partialFailures: [...(data.partialFailures || []), ...(retryData.partialFailures || [])],
        };
      }
      data.retryReason = 'ALL_ENGINES_FAILED';
    } catch (error) {
      data = {
        ...data,
        partialFailures: [
          ...(data.partialFailures || []),
          {
            engine: '__browser_retry__',
            code: error.code || 'BROWSER_RETRY_FAILED',
            message: error.message || 'browser retry failed',
          },
        ],
      };
      data.retryReason = 'BROWSER_RETRY_FAILED';
    }
    data.attemptedModes = attemptedModes;
    data.retryCount = 1;
  }
  return normalizeResults(query, await resolveKnownRedirectUrls(data));
}

export async function searchWithQuotaCompensation(request, deadline, requester = upstreamRequest) {
  const quotas = allocateSearchQuotas(request.limit, request.engines);
  // Each engine gets a bounded first pass. One slow engine must not occupy
  // the entire research budget while another has already returned evidence.
  // Reserve the rest for a deeper read of the successful engine's results.
  const engineDeadline = Math.min(deadline, Date.now() + 12000);
  const responses = await Promise.all(request.engines.map(async (engine, index) => {
    if (quotas[index] === 0) return null;
    return { engine, data: await searchEngineWithRetry(request, engine, quotas[index], requester, undefined, engineDeadline) };
  }));

  const activeResponses = responses.filter(Boolean);
  const firstPass = mergeSearchResponses(request.query, activeResponses.map((item) => item.data), request.limit);
  const firstUniqueCount = new Set((firstPass.results || []).map((item) => searchIdentity(item.url))).size;
  const remaining = Math.max(0, request.limit - firstUniqueCount);
  if (!remaining) return firstPass;

  const refillCandidates = activeResponses.filter(({ engine, data }) => {
    const failed = (data.partialFailures || []).some((failure) => failure.engine === engine);
    return !failed && (data.results || []).length >= 1;
  });
  if (!refillCandidates.length) return firstPass;
  if (deadline - Date.now() < 1000) return firstPass;

  const refillQuotas = allocateSearchQuotas(remaining, refillCandidates.map((item) => item.engine));
  const refillResponses = await Promise.all(refillCandidates.map(async ({ engine }, index) => {
    try {
      return await requester('/search', {
        ...request,
        // Ask for a deeper prefix, not the same first few hits again.
        limit: Math.min(config.maxResults, quotas[request.engines.indexOf(engine)] + refillQuotas[index]),
        engines: [engine],
      }, Math.min(8000, deadline - Date.now()));
    } catch (error) {
      return {
        query: request.query,
        engines: [engine],
        results: [],
        partialFailures: [{
          engine,
          code: error.code || 'engine_error',
          message: error.message || 'engine refill failed',
        }],
      };
    }
  }));
  return mergeSearchResponses(
    request.query,
    [...activeResponses.map((item) => item.data), ...refillResponses],
    request.limit,
  );
}

export async function searchEngineWithRetry(request, engine, limit, requester = upstreamRequest, pause = (ms) => new Promise((resolve) => setTimeout(resolve, ms)), deadline = Date.now() + config.searchTimeoutMs) {
  let failure;
  let attempts = 0;
  for (let attempt = 0; attempt < 2; attempt += 1) {
    const remaining = deadline - Date.now();
    if (remaining < 500) break;
    attempts += 1;
    try {
      const data = await requester('/search', {
        ...request, limit, engines: [engine],
      }, attempt === 0 ? Math.max(500, Math.floor(remaining / 2)) : remaining);
      if ((data.results || []).length || data.emptyConfirmedEngines?.includes(engine)) {
        return { ...data, retryCount: attempt, retryReason: attempt ? failure?.code : null };
      }
      const reported = (data.partialFailures || []).find((item) => item.engine === engine);
      if (reported) {
        failure = reported;
        if (!/SEARCH_TRANSIENT|TIMEOUT|UNAVAILABLE|ECONN|EAI_AGAIN|HTTP_50[234]/i.test(failure.code)) return data;
      } else {
        // Older upstream processes may still swallow DDG errors. Never certify
        // their empty response as proof that no source exists.
        if (engine !== 'duckduckgo') return data;
        failure = { engine, code: 'AMBIGUOUS_EMPTY_RESULTS', message: 'DuckDuckGo returned no verifiable results' };
      }
    } catch (error) {
      failure = {
        engine, code: error.name === 'AbortError' ? 'SEARCH_TIMEOUT' : error.code || 'engine_error',
        message: error.message || 'engine request failed',
      };
      if (!/SEARCH_TRANSIENT|TIMEOUT|UNAVAILABLE|ECONN|EAI_AGAIN|HTTP_50[234]/i.test(failure.code)) break;
    }
    if (attempt === 0) await pause(250);
  }
  return {
    query: request.query, engines: [engine], results: [],
    partialFailures: [failure || { engine, code: 'SEARCH_TIMEOUT', message: 'Search time budget exhausted' }],
    retryCount: Math.max(0, attempts - 1),
    retryReason: attempts > 1 ? failure?.code : null,
  };
}

export function mergeSearchResponses(query, responses, limit) {
  const engines = [...new Set(responses.flatMap((data) => data.engines || []).map(String))];
  const partialFailures = responses.flatMap((data) => data.partialFailures || []);
  const normalized = normalizeResults(query, {
    engines, partialFailures, results: responses.flatMap((data) => data.results || []),
    retryCount: Math.max(0, ...responses.map((data) => data.retryCount || 0)),
    retryReason: responses.find((data) => data.retryReason)?.retryReason,
  });
  normalized.results = normalized.results.slice(0, limit);
  normalized.totalResults = normalized.results.length;
  return normalized;
}

async function handleFetch(body) {
  const url = publicHttpUrl(body.url);
  if (!url) throw Object.assign(new Error('only public HTTP(S) URLs are allowed'), { code: 'INVALID_URL' });
  const maxChars = Math.min(Math.max(Number(body.maxChars) || 12000, 1000), config.maxFetchChars);
  if (config.reader === 'crawl4ai') {
    return readCrawl4AI({ url, maxChars, focus: String(body.focus || '').slice(0, 500),
      proxyServer: config.useProxy ? config.proxyUrl : '' }, { timeoutMs: config.fetchTimeoutMs });
  }
  if (body.renderMode === 'browser') {
    const browserPath = resolveBrowserPath();
    try {
      return await readRenderedPage({
        url,
        maxChars,
        executablePath: browserPath,
        focus: String(body.focus || '').slice(0, 500),
        proxyServer: config.useProxy ? config.proxyUrl : '',
      });
    } catch (error) {
      error.code = error.code || (browserPath ? 'BROWSER_READ_FAILED' : 'BROWSER_UNAVAILABLE');
      throw error;
    }
  }
  const repositoryUrl = githubReadmeRepositoryUrl(url);
  if (repositoryUrl) {
    try {
      const readme = await upstreamRequest('/fetch-github-readme', { url: repositoryUrl }, config.fetchTimeoutMs);
      const content = String(readme.content || '');
      if (content.trim()) {
        const selected = selectReadmeContent(content, maxChars);
        return {
          url, finalUrl: url, title: `${new URL(repositoryUrl).pathname.slice(1)} README`,
          content: selected, retrievalMethod: 'github-readme',
          contentType: 'text/markdown', readabilityApplied: true, excerpt: selected.slice(0, 1000),
          byline: '', siteName: 'GitHub', truncated: content.length > maxChars,
          links: [], qualityScore: 0, qualityFlags: [],
        };
      }
    } catch { /* Try the ordinary reader when the repository has no accessible README. */ }
  }
  const data = await upstreamRequest('/fetch-web', {
    url,
    maxChars,
    renderMode: body.renderMode === 'browser' ? 'browser' : 'request',
    readability: body.readability !== false,
    includeLinks: body.includeLinks === true,
  }, config.fetchTimeoutMs);
  return {
    url,
    finalUrl: data.finalUrl || url,
    title: String(data.title || '').trim(),
    content: String(data.content || '').slice(0, maxChars),
    retrievalMethod: data.retrievalMethod || 'request',
    contentType: data.contentType || '',
    readabilityApplied: Boolean(data.readabilityApplied),
    excerpt: String(data.excerpt || '').slice(0, 1000),
    byline: String(data.byline || '').slice(0, 200),
    siteName: String(data.siteName || '').slice(0, 200),
    truncated: Boolean(data.truncated),
    links: Array.isArray(data.links) ? data.links.slice(0, 100) : [],
    qualityScore: Number.isFinite(Number(data.qualityScore))
      ? Math.max(0, Math.min(100, Number(data.qualityScore))) : 0,
    qualityFlags: Array.isArray(data.qualityFlags)
      ? data.qualityFlags.map((flag) => String(flag).slice(0, 80)).slice(0, 20) : [],
  };
}

async function handle(req, res) {
  const host = req.headers.host;
  if (![`${config.host}:${config.port}`, `127.0.0.1:${config.port}`, `localhost:${config.port}`].includes(host)) {
    return json(res, 400, { error: { message: 'Invalid local Host' } });
  }
  // This sidecar is server-to-server only, never a browser API.
  if (req.headers.origin !== undefined || req.headers['sec-fetch-site'] !== undefined) {
    return json(res, 403, { error: { message: 'Browser access is not allowed' } });
  }
  const url = new URL(req.url, `http://${config.host}:${config.port}`);
  const work = req.method === 'POST' && ['/v1/search', '/v1/fetch'].includes(url.pathname);
  if (work) activeWork += 1;
  try {
    if (req.method === 'POST' && url.pathname === '/v1/suspend') {
      const owner = req.headers['x-research-client'];
      if (typeof owner === 'string') readinessClients.delete(owner);
      for (const [key, expiry] of readinessClients) if (expiry <= Date.now()) readinessClients.delete(key);
      if (readinessClients.size) return json(res, 409, { ready: false, status: 'IN_USE' });
      if (activeWork || readerProbe) return json(res, 409, { ready: false, status: 'BUSY' });
      if (!suspending) suspending = suspendResources().finally(() => { suspending = undefined; });
      await suspending;
      return json(res, 200, { status: 'SUSPENDED', upstream: config.manageUpstream ? 'STOPPED' : 'SHARED' });
    }
    if ((req.method === 'GET' && url.pathname === '/v1/ready') || work) {
      if (suspending) await suspending;
      suspended = false;
      if (config.manageUpstream) await startUpstream();
      const owner = req.headers['x-research-client'];
      if (typeof owner === 'string' && owner.length <= 80) readinessClients.set(owner, Date.now() + 90000);
    }
    if (req.method === 'GET' && url.pathname === '/v1/ready') {
      const started = Date.now();
      const checks = await Promise.allSettled([
        upstreamRequest('/health', undefined, 1500).then((health) => {
          // upstreamRequest already validates the HTTP/envelope status and
          // returns its data, not the outer { status, data } envelope.
          if (health?.daemon !== 'running') throw new Error('Search upstream not ready');
        }), readerReadiness(),
      ]);
      const components = { search: checks[0].status === 'fulfilled', reader: checks[1].status === 'fulfilled' };
      const ready = Object.values(components).every(Boolean);
      return json(res, ready ? 200 : 503, { ready, components, elapsed_ms: Date.now() - started });
    }
    if (req.method === 'GET' && url.pathname === '/health') {
      let upstream = 'unavailable';
      try { await upstreamRequest('/health', undefined, 1500); upstream = 'ready'; } catch { /* report below */ }
      return json(res, 200, { status: 'ok', component: 'web-research', upstream, suspended });
    }
    if (req.method === 'GET' && url.pathname === '/v1/status') {
      let upstreamStatus;
      try { upstreamStatus = await upstreamRequest('/status', undefined, 2500); } catch (error) {
        upstreamStatus = { error: error.message, code: error.code || 'UPSTREAM_UNAVAILABLE' };
      }
      return json(res, 200, {
        status: 'ok', component: 'web-research', upstream: upstreamStatus,
        config: {
          engines: config.engines,
          manageUpstream: config.manageUpstream,
          retryBrowserOnAllFailure: config.retryBrowserOnAllFailure,
          proxyConfigured: config.useProxy && Boolean(config.proxyUrl),
          browserExecutableAvailable: Boolean(resolveBrowserPath()),
          pageReaderProfiles: ['generic', 'csdn', 'zhihu', 'xiaohongshu'],
          reader: config.reader,
        },
      });
    }
    if (req.method !== 'POST' || !['/v1/search', '/v1/fetch'].includes(url.pathname)) {
      return json(res, 404, errorBody('NOT_FOUND', 'route not found'));
    }
    const body = await readBody(req);
    const data = url.pathname === '/v1/search' ? await handleSearch(body) : await handleFetch(body);
    return json(res, 200, { status: 'ok', data, error: null });
  } catch (error) {
    const code = error.name === 'AbortError' ? 'UPSTREAM_TIMEOUT' : (error.code || 'REQUEST_FAILED');
    return json(res, code === 'INVALID_QUERY' || code === 'INVALID_URL' ? 400 : 502,
      errorBody(code, error.message || 'request failed'));
  } finally {
    if (work) activeWork -= 1;
  }
}

async function suspendResources() {
  // Every Crawl4AI request already closes its own worker/browser on completion.
  // The legacy reader and the owned search daemon can hold browser sessions.
  await closeBrowserReader();
  readerReadyAt = 0;
  const child = upstreamProcess;
  if (child && child.exitCode === null && !child.killed) {
    await new Promise((resolve, reject) => {
      const timer = setTimeout(() => reject(new Error('Upstream suspension timed out')), 10000);
      child.once('exit', () => { clearTimeout(timer); resolve(); });
      child.send({ action: 'stop' }, (error) => { if (error) { clearTimeout(timer); reject(error); } });
    });
  }
  await upstreamEgress?.close(); upstreamEgress = undefined;
  suspended = true;
}

let upstreamEgress;
let upstreamStarting;
async function startUpstream() {
  if (upstreamStarting) return upstreamStarting;
  upstreamStarting = launchUpstream().finally(() => { upstreamStarting = undefined; });
  return upstreamStarting;
}
async function launchUpstream() {
  if (!config.manageUpstream) return;
  if (upstreamProcess && upstreamProcess.exitCode === null && !upstreamProcess.killed) return;
  if (!upstreamEgress) upstreamEgress = await createEgressProxy({ proxyServer: config.useProxy ? config.proxyUrl : '' });
  const command = process.platform === 'win32' ? 'node.exe' : 'node';
  const browserPath = resolveBrowserPath();
  upstreamProcess = spawn(command, ['src/upstream-worker.mjs'], {
    cwd: fileURLToPath(new URL('../', import.meta.url)),
    stdio: ['ignore', 'ignore', 'pipe', 'ipc'],
    windowsHide: true,
    env: {
      ...workerEnvironment(process.env, ['DEFAULT_SEARCH_ENGINE','ALLOWED_SEARCH_ENGINES','SEARCH_MODE','PLAYWRIGHT_HEADLESS','PLAYWRIGHT_NAVIGATION_TIMEOUT_MS','OPEN_WEBSEARCH_QUIET_STARTUP']),
      HOST: config.upstreamHost,
      PORT: String(config.upstreamPort),
      PLAYWRIGHT_PACKAGE: process.env.PLAYWRIGHT_PACKAGE || 'playwright-core',
      ...(browserPath ? { PLAYWRIGHT_EXECUTABLE_PATH: browserPath } : {}),
      PROXY_URL: upstreamEgress.url, USE_PROXY: 'true',
    },
  });
  upstreamProcess.stderr.on('data', (chunk) => process.stderr.write(`[open-websearch] ${chunk}`));
  upstreamProcess.on('exit', (code) => {
    if (!shuttingDown && code !== 0) console.error(`Open-WebSearch exited with code ${code}`);
  });
}

function resolveBrowserPath() {
  if (process.env.PLAYWRIGHT_EXECUTABLE_PATH) return process.env.PLAYWRIGHT_EXECUTABLE_PATH;
  if (process.platform !== 'win32') return '';
  const candidates = [
    `${process.env.PROGRAMFILES || ''}\\Google\\Chrome\\Application\\chrome.exe`,
    `${process.env['PROGRAMFILES(X86)'] || ''}\\Google\\Chrome\\Application\\chrome.exe`,
    `${process.env.LOCALAPPDATA || ''}\\Google\\Chrome\\Application\\chrome.exe`,
    `${process.env.PROGRAMFILES || ''}\\Microsoft\\Edge\\Application\\msedge.exe`,
    `${process.env['PROGRAMFILES(X86)'] || ''}\\Microsoft\\Edge\\Application\\msedge.exe`,
  ];
  return candidates.find((candidate) => candidate && existsSync(candidate)) || '';
}

export function startServer() {
  const server = createServer(handle);
  void startUpstream().catch(error => console.error('Upstream start failed:', error.code || error.name));
  server.listen(config.port, config.host, () => {
    console.log(`Solevia web research listening at http://${config.host}:${config.port}`);
  });
  return server;
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  const server = startServer();
  async function shutdown() {
    if (shuttingDown) return;
    shuttingDown = true;
    await suspendResources().catch(() => { if (upstreamProcess && !upstreamProcess.killed) upstreamProcess.kill(); });
    await new Promise((resolve) => server.close(resolve));
    process.exit(0);
  }
  process.on('SIGINT', shutdown);
  process.on('SIGTERM', shutdown);
}
