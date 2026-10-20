import { publicHttpUrl } from './network-policy.mjs';
import { createEgressProxy } from './egress-proxy.mjs';
import { workerEnvironment } from './worker-environment.mjs';

import { chromium } from 'playwright-core';

let browserPromise;
let browserEgress;

const SITE_SELECTORS = {
  csdn: [
    '#content_views',
    '.article_content',
    '.blog-content-box article',
    'article',
  ],
  zhihu: [
    '.RichContent-inner',
    '.Post-RichTextContainer',
    '.QuestionAnswer-content',
    '.ArticleItem-content',
    'article',
    'main',
  ],
  xiaohongshu: [
    '#detail-desc',
    '.note-content',
    '.desc',
    '[class*="note-content"]',
    '[class*="noteContent"]',
    'main',
  ],
  generic: ['article', 'main', '[role="main"]'],
};

export function pageReaderProfile(value) {
  try {
    const host = new URL(String(value)).hostname.toLowerCase();
    if (host === 'csdn.net' || host.endsWith('.csdn.net')) return 'csdn';
    if (host === 'zhihu.com' || host.endsWith('.zhihu.com')) return 'zhihu';
    if (host === 'xiaohongshu.com' || host.endsWith('.xiaohongshu.com')) return 'xiaohongshu';
  } catch {
    return 'generic';
  }
  return 'generic';
}

async function assertPublicNavigation(value) {
  if (!publicHttpUrl(value)) throw new Error('PUBLIC_NETWORK_TARGET_REQUIRED');
}

async function getBrowser(executablePath, proxyServer) {
  if (!browserPromise) {
    browserPromise = (async () => {
      browserEgress = await createEgressProxy({ proxyServer });
      return chromium.launch({
        headless: true, chromiumSandbox: true, env: workerEnvironment(),
        ...(executablePath ? { executablePath } : {}),
        proxy: { server: browserEgress.url, bypass: '<-loopback>' },
        args: ['--disable-background-networking', '--disable-component-update', '--force-webrtc-ip-handling-policy=disable_non_proxied_udp'],
      });
    })().catch(async (error) => {
      await browserEgress?.close(); browserEgress = undefined;
      browserPromise = undefined;
      throw error;
    });
  }
  return browserPromise;
}

export async function closeBrowserReader() {
  if (!browserPromise) return;
  try {
    const browser = await browserPromise;
    await browser.close();
  } finally {
    browserPromise = undefined;
    await browserEgress?.close(); browserEgress = undefined;
  }
}

export async function probeBrowserReader(options) {
  const browser = await getBrowser(options.executablePath, options.proxyServer || '');
  const page = await browser.newPage();
  try {
    await page.setContent('<html><body>Reader readiness check.</body></html>');
    if (!(await page.textContent('body')).includes('Reader readiness check.')) throw new Error('Reader readiness failed');
  } finally { await page.close(); }
}

export async function readRenderedPage({ url, maxChars, executablePath, proxyServer = '', focus = '' }) {
  await assertPublicNavigation(url);
  const browser = await getBrowser(executablePath, proxyServer);
  const context = await browser.newContext({
    serviceWorkers: 'block', acceptDownloads: false,
    locale: 'zh-CN',
    userAgent: 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/145.0.0.0 Safari/537.36',
    viewport: { width: 1365, height: 900 },
  });
  await context.routeWebSocket('**/*', socket => socket.close());
  const page = await context.newPage();
  try {
    await context.route('**/*', async (route) => {
      const request = route.request();
      if (['image', 'media', 'font'].includes(request.resourceType())) return route.abort();
      try { await assertPublicNavigation(request.url()); }
      catch { return route.abort(); }
      return route.continue();
    });
    await page.goto(url, { waitUntil: 'domcontentloaded', timeout: 20_000 });
    await page.waitForLoadState('networkidle', { timeout: 2_500 }).catch(() => {});
    await page.waitForTimeout(500);

    const finalProfile = pageReaderProfile(page.url());

    const extracted = await page.evaluate(({ profileName, selectors, focus }) => {
      const normalize = (value) => String(value || '')
        .replace(/\u00a0/g, ' ')
        .replace(/[ \t]+\n/g, '\n')
        .replace(/\n{3,}/g, '\n\n')
        .trim();
      const textFrom = (node) => {
        if (!node) return '';
        const clone = node.cloneNode(true);
        clone.querySelectorAll('script,style,noscript,template,iframe,svg,canvas,nav,footer,aside')
          .forEach((item) => item.remove());
        return normalize(clone.innerText || clone.textContent || '');
      };
      const candidates = selectors.map((selector) => textFrom(document.querySelector(selector)))
        .filter((value) => value.length >= 120)
        .sort((a, b) => b.length - a.length);
      let content = candidates[0] || '';
      let sourceKind = candidates.length ? `selector:${profileName}` : '';
      // Select an actual matching API section before applying the length cap.
      const names = focus.match(/(?<![\w-])[A-Z][a-z]+(?:[A-Z][A-Za-z0-9]*)+\b/g) || [];
      const headingKey = (value) => value.toLowerCase().replace(/[^a-z0-9]/g, '').replace(/s$/, '');
      for (const name of names) {
        const heading = [...document.querySelectorAll('h1,h2,h3,h4,h5,h6')]
          .find((node) => headingKey(node.textContent || '') === headingKey(name));
        const section = heading?.closest('section');
        const focused = section ? textFrom(section) : '';
        if (focused.length >= 120) {
          content = focused;
          sourceKind = 'focused-section';
          break;
        }
      }

      if (content.length < 240) {
        const stateRoots = [];
        for (const key of ['__INITIAL_STATE__', '__INITIAL_STATE', '__NUXT__']) {
          if (window[key] && typeof window[key] === 'object') stateRoots.push(window[key]);
        }
        const nextData = document.querySelector('#__NEXT_DATA__');
        if (nextData?.textContent) {
          try { stateRoots.push(JSON.parse(nextData.textContent)); } catch { /* ignore malformed state */ }
        }
        const found = [];
        const seen = new Set();
        let visited = 0;
        const walk = (value, path = '', depth = 0) => {
          if (depth > 7 || visited++ > 10_000 || value == null) return;
          if (typeof value === 'string') {
            const clean = normalize(value);
            if (clean.length >= 40 && clean.length <= 20_000 &&
                /(?:title|desc|content|note|text|body|article)/i.test(path) && !seen.has(clean)) {
              seen.add(clean);
              found.push(clean);
            }
            return;
          }
          if (Array.isArray(value)) {
            value.slice(0, 100).forEach((item, index) => walk(item, `${path}.${index}`, depth + 1));
          } else if (typeof value === 'object') {
            Object.entries(value).slice(0, 200)
              .forEach(([key, item]) => walk(item, `${path}.${key}`, depth + 1));
          }
        };
        stateRoots.forEach((root) => walk(root));
        found.sort((a, b) => b.length - a.length);
        if (found[0]?.length > content.length) {
          content = found.slice(0, 6).filter((value, index, all) =>
            !all.some((other, otherIndex) => otherIndex < index && other.includes(value)))
            .join('\n\n');
          sourceKind = 'structured-state';
        }
      }

      if (content.length < 240) {
        content = textFrom(document.body);
        sourceKind = sourceKind || 'rendered-body';
      }
      const description = document.querySelector('meta[name="description"],meta[property="og:description"]')
        ?.getAttribute('content') || '';
      if (content.length < 120 && description) content = normalize(`${document.title}\n\n${description}`);
      return { content, sourceKind, title: document.title || '' };
    }, { profileName: finalProfile, selectors: SITE_SELECTORS[finalProfile], focus });

    const finalUrl = page.url();
    await assertPublicNavigation(finalUrl);
    const rawContent = String(extracted.content || '');
    if (!rawContent.trim()) throw new Error('browser reader extracted no content');
    const flags = ['BROWSER_RENDERED'];
    if (extracted.sourceKind === 'focused-section') flags.push('SECTION_FOCUSED');
    if (finalProfile !== 'generic') flags.push(`SITE_ADAPTER_${finalProfile.toUpperCase()}`);
    if (extracted.sourceKind === 'structured-state') flags.push('STRUCTURED_STATE');
    const finalPath = new URL(finalUrl).pathname.toLowerCase();
    if (/(?:^|\/)(?:login|signin|passport)(?:\/|$)/.test(finalPath)) flags.push('AUTH_REQUIRED');
    return {
      url,
      finalUrl,
      title: String(extracted.title || ''),
      content: rawContent.slice(0, maxChars),
      retrievalMethod: extracted.sourceKind === 'structured-state'
        ? `browser-state:${finalProfile}` : `browser-dom:${finalProfile}`,
      contentType: 'text/html; rendered=1',
      readabilityApplied: false,
      excerpt: rawContent.slice(0, 1000),
      byline: '',
      siteName: finalProfile === 'generic' ? '' : finalProfile,
      truncated: rawContent.length > maxChars,
      links: [],
      qualityScore: 0,
      qualityFlags: flags,
    };
  } finally {
    await context.close();
  }
}
