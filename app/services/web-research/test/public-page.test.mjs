import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readPublicPage } from '../src/public-page.mjs';

test('public evidence reader rejects local targets and credentials before a request', async () => {
  const credentialUrl = new URL('https://example.com/');
  credentialUrl.username = 'synthetic-user';
  credentialUrl.password = 'synthetic-password';
  for (const url of ['http://127.0.0.1:8002/', 'http://[::1]/', 'http://169.254.169.254/',
    'file:///etc/passwd', credentialUrl.href, 'http://localhost/']) {
    await assert.rejects(readPublicPage(url, 1000), /PUBLIC_NETWORK_TARGET_REQUIRED/);
  }
});
