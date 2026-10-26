// One bounded, credential-free public read. Reuse the existing DNS policy;
// pin the validated address for every redirect while TLS verifies the hostname.
import http from 'node:http';
import https from 'node:https';
import { resolvePublicTarget } from './network-policy.mjs';
import { pathToFileURL } from 'node:url';

export async function readPublicPage(value, timeoutMs = 15000) {
  let active;
  let stopped = false;
  let timer;
  const follow = async () => {
    let current = value;
    for (let hop = 0; hop <= 5; hop++) {
      const {url, addresses} = await resolvePublicTarget(current);
      if (stopped) throw new Error('READ_TIMEOUT');
      if (![80,443].includes(Number(url.port) || (url.protocol === 'https:' ? 443 : 80))) {
        throw new Error('WEB_PORT_REQUIRED');
      }
      const response = await new Promise((resolve, reject) => {
        const chosen = addresses.find(address => address.family === 4) || addresses[0];
        active = (url.protocol === 'https:' ? https : http).get(url, {
          agent: false,
          lookup: (_host, options, callback) => callback(null,
            options.all ? [chosen] : chosen.address, chosen.family),
          headers: {'user-agent': 'Solevia-EvidenceReader/1.0', 'accept-encoding': 'identity'},
        }, resolve);
        active.on('error', reject);
      });
      if (response.statusCode >= 300 && response.statusCode < 400 && response.headers.location) {
        response.destroy();
        current = new URL(response.headers.location, url).toString();
        continue;
      }
      if (response.statusCode !== 200) {response.destroy();throw new Error('READ_HTTP_FAILED');}
      const type = response.headers['content-type'] || '';
      if (!/^(text\/(html|plain)|application\/xhtml\+xml)\b/i.test(type) ||
          (response.headers['content-encoding'] && response.headers['content-encoding'] !== 'identity')) {
        response.destroy();throw new Error('UNSUPPORTED_BODY_TYPE');
      }
      const chunks = [];
      let bytes = 0;
      for await (const chunk of response) {
        bytes += chunk.length;
        if (bytes > 2 * 1024 * 1024) {response.destroy();throw new Error('BODY_TOO_LARGE');}
        chunks.push(chunk);
      }
      const charset = /charset\s*=\s*["']?([^;\s"']+)/i.exec(type)?.[1] || 'utf-8';
      return {url: url.toString(), contentType: type,
        body: new TextDecoder(charset).decode(Buffer.concat(chunks))};
    }
    throw new Error('REDIRECT_LIMIT');
  };
  try {
    return await Promise.race([follow(), new Promise((_, reject) => {
      timer = setTimeout(() => {stopped = true;active?.destroy();reject(new Error('READ_TIMEOUT'));}, timeoutMs);
    })]);
  } finally {clearTimeout(timer);stopped = true;active?.destroy();}
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  try {
    let input = '';
    for await (const chunk of process.stdin) {input += chunk;if (input.length > 16000) throw new Error('INPUT_TOO_LARGE');}
    const {url, timeoutMs} = JSON.parse(input);
    process.stdout.write(JSON.stringify(await readPublicPage(url, timeoutMs)));
  } catch {
    process.stderr.write('PUBLIC_BODY_READ_FAILED');
    process.exitCode = 1;
  }
}
