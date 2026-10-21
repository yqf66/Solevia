import test from 'node:test';
import assert from 'node:assert/strict';
import { createReaderGate } from '../src/crawl4ai-reader.mjs';

test('concurrent callers wait in FIFO order without increasing reader capacity', async () => {
  const acquire = createReaderGate({ concurrency: 1, maxQueued: 2 });
  const first = await acquire(1000);
  const order = [];
  const second = acquire(1000).then(release => { order.push(2); return release; });
  const third = acquire(1000).then(release => { order.push(3); return release; });
  await assert.rejects(acquire(1000), { code: 'CRAWL4AI_BUSY' });
  assert.deepEqual(order, []);
  first(); first(); // Duplicate cleanup cannot grant two slots.
  const releaseSecond = await second;
  assert.deepEqual(order, [2]);
  releaseSecond();
  (await third)();
  assert.deepEqual(order, [2, 3]);
});

test('expired queued requests are removed and never consume the next slot', async () => {
  const acquire = createReaderGate({ concurrency: 1, maxQueued: 1 });
  const release = await acquire(1000);
  await assert.rejects(acquire(5), { code: 'CRAWL4AI_BUSY' });
  const next = acquire(1000);
  release();
  (await next)();
});
