import test from 'node:test';
import assert from 'node:assert/strict';
import { fork } from 'node:child_process';
import { createServer } from 'node:net';
import { once } from 'node:events';
import { fileURLToPath } from 'node:url';

test('owned upstream exits cleanly over IPC and can restart on the same port', { timeout: 30000 }, async () => {
  const probe = createServer().listen(0, '127.0.0.1');
  await once(probe, 'listening');
  const port = probe.address().port;
  await new Promise((resolve) => probe.close(resolve));
  for (let attempt = 0; attempt < 2; attempt += 1) {
    const child = fork('src/upstream-worker.mjs', [], {
      cwd: fileURLToPath(new URL('../', import.meta.url)), windowsHide: true,
      env: { ...process.env, HOST: '127.0.0.1', PORT: String(port) },
      stdio: ['ignore', 'ignore', 'ignore', 'ipc'],
    });
    try {
      let ready = false;
      for (let retry = 0; retry < 200 && !ready; retry += 1) {
        try {
          const response = await fetch(`http://127.0.0.1:${port}/health`);
          ready = response.ok && (await response.json()).data.daemon === 'running';
        } catch { /* The child has not bound its port yet. */ }
        if (!ready) await new Promise((resolve) => setTimeout(resolve, 50));
      }
      assert.ok(ready, 'upstream started');
      assert.equal((await fetch(`http://127.0.0.1:${port}/health`, { headers: { Origin: 'https://attacker.example' } })).status, 403);
      const exited = once(child, 'exit');
      child.send({ action: 'stop' });
      const [code] = await exited;
      assert.equal(code, 0);
      await assert.rejects(fetch(`http://127.0.0.1:${port}/health`));
    } finally {
      if (child.exitCode === null) child.kill();
    }
  }
});
