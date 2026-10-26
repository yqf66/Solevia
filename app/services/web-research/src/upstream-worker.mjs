// Keep the pinned upstream daemon unchanged, but give its owner an IPC stop
// path so Windows can close browser sessions before the child process exits.
import { createOpenWebSearchRuntime } from '../node_modules/open-websearch/build/runtime/createRuntime.js';
import { startLocalDaemon } from '../node_modules/open-websearch/build/adapters/http/localDaemon.js';

const daemon = await startLocalDaemon(createOpenWebSearchRuntime(), {
  host: process.env.HOST || '127.0.0.1', port: Number(process.env.PORT || 3210),
});
let closing = false;
async function stop() {
  if (closing) return;
  closing = true;
  try { await daemon.close(); } finally { process.exit(0); }
}
process.on('message', (message) => { if (message?.action === 'stop') void stop(); });
process.on('disconnect', () => void stop());
process.on('SIGTERM', () => void stop());
process.on('SIGINT', () => void stop());
