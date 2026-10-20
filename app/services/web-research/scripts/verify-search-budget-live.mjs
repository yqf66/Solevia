import { mkdir, writeFile } from 'node:fs/promises';
import { resolve } from 'node:path';
import { searchWithQuotaCompensation } from '../src/server.mjs';

const directory = resolve(process.argv[2]);
const upstream = new URL(process.argv[3]);
if (!['127.0.0.1', 'localhost'].includes(upstream.hostname)) throw new Error('Use the existing local search adapter');
await mkdir(directory, { recursive: true });
const report = { started_at: new Date().toISOString(), requests: [] };
const start = performance.now();
try {
  report.result = await searchWithQuotaCompensation({
    query: process.argv[4] || 'site:docs.python.org/3.14/library/asyncio-task.html TaskGroup exception cancellation',
    engines: ['bing', 'duckduckgo'], limit: 4, searchMode: 'playwright',
  }, Date.now() + 30000, async (path, body, timeout) => {
    const began = performance.now();
    const record = { path, request: body, timeout_ms: timeout };
    report.requests.push(record);
    try {
      const response = await fetch(new URL(path, upstream), {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body), signal: AbortSignal.timeout(Math.max(1, Math.floor(timeout))),
      });
      record.http_status = response.status;
      record.response = await response.json();
      if (!response.ok || record.response.status === 'error') {
        throw Object.assign(new Error(record.response.error?.message || `HTTP ${response.status}`),
          { code: record.response.error?.code || `UPSTREAM_HTTP_${response.status}` });
      }
      return record.response.data;
    } catch (error) {
      record.error = error.message;
      if (error.name === 'TimeoutError') {
        throw Object.assign(new Error(error.message, { cause: error }), { code: 'SEARCH_TIMEOUT' });
      }
      throw error;
    } finally {
      record.elapsed_ms = Math.round(performance.now() - began);
    }
  });
  report.relevant_results = report.result.results.filter(item => {
    try {
      const url = new URL(item.url);
      return url.hostname === 'docs.python.org' && url.pathname.startsWith('/3.14/') && /asyncio|TaskGroup/i.test(item.url + item.title);
    }
    catch { return false; }
  }).length;
  report.status = report.relevant_results ? 'PASSED' : 'FAILED';
} catch (error) {
  report.status = 'FAILED';
  report.error = error.message;
}
report.elapsed_ms = Math.round(performance.now() - start);
report.scope = 'Fresh real searches only; no model answer or page reads in this diagnostic';
await writeFile(resolve(directory, 'report.json'), JSON.stringify(report, null, 2));
console.log(JSON.stringify({ status: report.status, elapsed_ms: report.elapsed_ms,
  results: report.result?.results?.length, partialFailures: report.result?.partialFailures }));
process.exitCode = report.status === 'PASSED' ? 0 : 1;
