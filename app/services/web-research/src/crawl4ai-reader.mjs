import { spawn } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { workerEnvironment } from './worker-environment.mjs';
import { createEgressProxy } from './egress-proxy.mjs';

const backend = fileURLToPath(new URL('../../../backend/', import.meta.url));
const python = fileURLToPath(new URL(process.platform === 'win32'
  ? '../../../backend/.venv/Scripts/python.exe' : '../../../backend/.venv/bin/python', import.meta.url));
const worker = fileURLToPath(new URL('./crawl4ai-reader.py', import.meta.url));
// One shared, bounded FIFO for all callers of this service. Waiting uses the
// caller's existing timeout, so overload cannot grow the queue indefinitely.
export function createReaderGate({ concurrency = 3, maxQueued = 12 } = {}) {
  let active = 0;
  const waiting = [];
  const busy = () => Object.assign(new Error('Crawl4AI reader is busy'), { code: 'CRAWL4AI_BUSY' });
  const grant = (resolve) => {
    active += 1;
    let released = false;
    resolve(() => {
      if (released) return;
      released = true;
      active -= 1;
      const next = waiting.shift();
      if (next) { clearTimeout(next.timer); grant(next.resolve); }
    });
  };
  return (timeoutMs) => new Promise((resolve, reject) => {
    if (active < concurrency) return grant(resolve);
    if (waiting.length >= maxQueued) return reject(busy());
    const item = { resolve, timer: null };
    item.timer = setTimeout(() => {
      const index = waiting.indexOf(item);
      if (index >= 0) waiting.splice(index, 1);
      reject(busy());
    }, timeoutMs);
    waiting.push(item);
  });
}
const acquireReader = createReaderGate();

export async function readCrawl4AI(request, { timeoutMs = 30000 } = {}) {
  const started = Date.now();
  const release = await acquireReader(timeoutMs);
  let egress;
  try {
    egress = await createEgressProxy({ proxyServer: request.proxyServer || '' });
    return await runReader({ ...request, proxyServer: egress.url }, Math.max(1, timeoutMs - (Date.now() - started)));
  } finally { await egress?.close(); release(); }
}

function runReader(request, timeoutMs) {
  return new Promise((resolve, reject) => {
    const processHandle = spawn(process.env.CRAWL4AI_PYTHON || python, ['-I', worker], {
      cwd: backend, windowsHide: true, detached: process.platform !== 'win32', stdio: ['pipe', 'pipe', 'ignore'],
      env: { ...workerEnvironment(process.env, ['PLAYWRIGHT_BROWSERS_PATH','SOLEVIA_WEB_ARCHIVES']), PYTHONIOENCODING: 'utf-8', PYTHONNOUSERSITE: '1' },
    });
    let output = '';
    processHandle.stdout.setEncoding('utf8');
    let finished = false;
    const finish = (error, data) => {
      if (finished) return;
      finished = true;
      clearTimeout(timer);
      if (error) reject(error); else resolve(data);
    };
    const fail = (code) => Object.assign(new Error(`Crawl4AI reader failed: ${code}`), { code });
    let stoppedError;
    const stopTree = () => {
      if (process.platform === 'win32' && processHandle.pid) {
        const killer = spawn('taskkill.exe', ['/PID', String(processHandle.pid), '/T', '/F'], { windowsHide:true, stdio:'ignore' });
        killer.on('error',()=>processHandle.kill());
      } else if (processHandle.pid) {
        try { process.kill(-processHandle.pid, 'SIGKILL'); } catch { processHandle.kill(); }
      }
    };
    const timer = setTimeout(() => {
      stoppedError = fail('CRAWL4AI_TIMEOUT');
      stopTree();
    }, timeoutMs);
    processHandle.on('error', () => finish(fail('CRAWL4AI_UNAVAILABLE')));
    processHandle.stdout.on('data', (chunk) => {
      output += chunk;
      if (output.length > 250000) {
        stoppedError = fail('CRAWL4AI_OUTPUT_BUDGET');
        stopTree();
      }
    });
    processHandle.on('close', (code) => {
      if (stoppedError) return finish(stoppedError);
      try {
        const data = JSON.parse(output);
        if (code !== 0 || data.error) return finish(fail(data.code || 'CRAWL4AI_READ_FAILED'));
        if (request.probe === true) {
          if (data.ready !== true || data.component !== 'crawl4ai') return finish(fail('CRAWL4AI_INVALID_RESULT'));
          return finish(null, data);
        }
        if (data.retrievalMethod !== 'crawl4ai:raw-markdown' || !data.evidenceArchive ||
            typeof data.content !== 'string' || data.content.length > request.maxChars) {
          return finish(fail('CRAWL4AI_INVALID_RESULT'));
        }
        finish(null, data);
      } catch { finish(fail('CRAWL4AI_INVALID_RESULT')); }
    });
    processHandle.stdin.on('error', () => finish(fail('CRAWL4AI_UNAVAILABLE')));
    processHandle.stdin.end(JSON.stringify(request));
  });
}
