import { readFileSync, writeFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

const baiduPath = fileURLToPath(
  new URL('../node_modules/open-websearch/build/engines/baidu/baidu.js', import.meta.url),
);
const httpPath = fileURLToPath(
  new URL('../node_modules/open-websearch/build/utils/httpRequest.js', import.meta.url),
);

let httpSource = readFileSync(httpPath, 'utf8');
httpSource = httpSource.replace(
  /const effectiveMaxRedirects = .*?;/,
  'const effectiveMaxRedirects = trustedStaticHost ? 0 : maxRedirects;',
);
writeFileSync(httpPath, httpSource);

let source = readFileSync(baiduPath, 'utf8');
source = source.replace("import axios from 'axios';\n", '');
source = source.replace(
  "import { buildAxiosRequestOptions } from '../../utils/httpRequest.js';",
  "import { buildAxiosRequestOptions, requestWithSafeRedirects } from '../../utils/httpRequest.js';",
);
source = source.replace(/^\s*maxRedirects: 5,\r?\n/m, '');
source = source.replace(
  /const response = await axios\.get\('https:\/\/www\.baidu\.com\/s', buildAxiosRequestOptions\(\{([\s\S]*?)\n        \}\)\);/,
  `const options = buildAxiosRequestOptions({$1
        });
        const response = await requestWithSafeRedirects(
            'GET',
            'https://www.baidu.com/s',
            { ...options, maxRedirects: 5 },
            'Baidu search URL'
        );`,
);
if (!source.includes('requestWithSafeRedirects(')) {
  throw new Error('Open-WebSearch Baidu patch did not match version 2.1.11');
}
writeFileSync(baiduPath, source);

// The search browser must use our owned egress proxy and a fresh isolation
// domain per daemon. Never attach to browser profiles from unrelated tasks.
const browserPath = fileURLToPath(new URL('../node_modules/open-websearch/build/utils/playwrightClient.js', import.meta.url));
let browser = readFileSync(browserPath, 'utf8');
if (!browser.includes('PLAN_A_BROWSER_SANDBOX')) {
  browser = '// PLAN_A_BROWSER_SANDBOX\n' + browser;
  browser = browser.replace("path.join(tmpdir(), 'open-websearch-page-pool-locks')", "path.join(tmpdir(), `plan-a-search-${process.pid}`, 'page-locks')");
  browser = browser.replace("path.join(tmpdir(), 'open-websearch-browser-session-locks')", "path.join(tmpdir(), `plan-a-search-${process.pid}`, 'browser-locks')");
  browser = browser.replace('preferExistingContext: options?.preferExistingContext !== false,', 'preferExistingContext: false,');
  browser = browser.replace('browser.newContext(pool.contextOptions)', "browser.newContext({ ...pool.contextOptions, serviceWorkers: 'block', acceptDownloads: false, ignoreHTTPSErrors: false })");
  browser = browser.replace('export async function openPlaywrightBrowser(headless, launchArgs = [], options) {', `export async function openPlaywrightBrowser(headless, launchArgs = [], options) {
    launchArgs = launchArgs.filter(arg => !['--no-sandbox', '--disable-setuid-sandbox', '--ignore-certificate-errors'].includes(arg));
    launchArgs.push('--proxy-bypass-list=<-loopback>', '--force-webrtc-ip-handling-policy=disable_non_proxied_udp');`);
  browser = browser.replace('const browser = await playwright.chromium.launch({', 'const browser = await playwright.chromium.launch({ chromiumSandbox: true,');
  if (!browser.includes('preferExistingContext: false,') || !browser.includes('chromiumSandbox: true')) {
    throw new Error('Search browser sandbox patch did not match pinned upstream');
  }
  writeFileSync(browserPath, browser);
}
// Patch revision 2: guard the browser engine itself against re-reading backend
// credentials after a library's implicit dotenv load.
browser = readFileSync(browserPath, 'utf8');
if (!browser.includes('PLAN_A_BROWSER_CLEAN_ENV')) {
  browser = "import { workerEnvironment as planAWorkerEnvironment } from '../../../../src/worker-environment.mjs';\n// PLAN_A_BROWSER_CLEAN_ENV\n" + browser;
  browser = browser.replace('chromium.launch({ chromiumSandbox: true,',
    'chromium.launch({ chromiumSandbox: true, env: planAWorkerEnvironment(),');
  browser = browser.replaceAll("detached: true\n", "detached: true, env: planAWorkerEnvironment()\n");
  writeFileSync(browserPath, browser);
}
browser = readFileSync(browserPath, 'utf8');
browser = browser.replace("from '../../../../../src/worker-environment.mjs'", "from '../../../../src/worker-environment.mjs'");
writeFileSync(browserPath, browser);

// Preserve DDG failures instead of treating connection/parser/challenge errors
// as a successful empty search. Keep this patch repeatable on pinned installs.
const ddgPath = fileURLToPath(new URL('../node_modules/open-websearch/build/engines/duckduckgo/searchDuckDuckGo.js', import.meta.url));
let ddg = readFileSync(ddgPath, 'utf8');
if (!ddg.includes('assertDuckDuckGoEmpty')) {
  ddg = "import { assertDuckDuckGoPage, assertDuckDuckGoEmpty, searchFailure } from '../../../../../src/search-failure.mjs';\n" + ddg;
  ddg = ddg.replace('let basePreloadUrl = \x27\x27;', "assertDuckDuckGoPage(response.data);\n        let basePreloadUrl = ''; ");
  ddg = ddg.replace("console.warn('无法找到预加载的d.js URL');\n            return [];", "throw Object.assign(new Error('SEARCH_PARSE_FAILED'), { code: 'SEARCH_PARSE_FAILED' });");
  ddg = ddg.replace('const jsonpMatch = dataResponse.data.match(', 'assertDuckDuckGoPage(dataResponse.data);\n            const jsonpMatch = dataResponse.data.match(');
  ddg = ddg.replace('// If unable to extract data from response, assume no more results\n                hasMoreResults = false;', "throw Object.assign(new Error('SEARCH_PARSE_FAILED'), { code: 'SEARCH_PARSE_FAILED' });");
  ddg = ddg.replace("console.error('DuckDuckGo预加载URL搜索失败:', error);\n        return [];", 'throw searchFailure(error);');
  ddg = ddg.replace('let $ = cheerio.load(response.data);', 'assertDuckDuckGoPage(response.data);\n        let $ = cheerio.load(response.data);');
  ddg = ddg.replace('if (items.length === 0) {\n            return results;', 'if (items.length === 0) {\n            assertDuckDuckGoEmpty(response.data);\n            results.emptyConfirmed = true;\n            return results;');
  ddg = ddg.replace("console.error('DuckDuckGo HTML search failed:', error);\n        return [];", 'throw searchFailure(error);');
  ddg = ddg.replace("console.warn('预加载URL方法失败，尝试HTML方法:', error);", "if (['SEARCH_RATE_LIMIT', 'SEARCH_CHALLENGE', 'SEARCH_BLOCKED'].includes(error.code)) throw error;");
  if (!ddg.includes('results.emptyConfirmed = true') || ddg.includes("console.error('DuckDuckGo HTML search failed:")) {
    throw new Error('DuckDuckGo diagnostics patch did not match pinned upstream');
  }
  writeFileSync(ddgPath, ddg);
}
const searchPath = fileURLToPath(new URL('../node_modules/open-websearch/build/core/search/searchService.js', import.meta.url));
let search = readFileSync(searchPath, 'utf8');
if (!search.includes('emptyConfirmedEngines')) {
  search = search.replace('const partialFailures = [];', 'const partialFailures = [];\n            const emptyConfirmedEngines = [];');
  search = search.replace('return await executor(cleanQuery, engineLimit, { searchMode: effectiveSearchMode });', 'const hits = await executor(cleanQuery, engineLimit, { searchMode: effectiveSearchMode });\n                    if (hits.emptyConfirmed) emptyConfirmedEngines.push(engine);\n                    return hits;');
  search = search.replace("code: 'engine_error',", "code: error?.code || 'engine_error',");
  search = search.replace('partialFailures\n            };', 'partialFailures, emptyConfirmedEngines\n            };');
  if (!search.includes('hits.emptyConfirmed') || !search.includes('partialFailures, emptyConfirmedEngines')) {
    throw new Error('Search failure metadata patch did not match pinned upstream');
  }
  writeFileSync(searchPath, search);
}

// Protect the underlying loopback daemon as well as our public adapter.
const daemonPath = fileURLToPath(new URL('../node_modules/open-websearch/build/adapters/http/localDaemon.js', import.meta.url));
let daemon = readFileSync(daemonPath, 'utf8');
if (!daemon.includes('SOLEVIA_LOCAL_HTTP_BOUNDARY')) {
  daemon = daemon.replace('const app = express();', `const app = express();
    // SOLEVIA_LOCAL_HTTP_BOUNDARY
    app.use((req, res, next) => {
        const port = req.socket.localPort;
        if (!['127.0.0.1:' + port, 'localhost:' + port].includes(req.headers.host)) {
            return res.status(400).json({ error: 'Invalid local Host' });
        }
        if (req.headers.origin !== undefined || req.headers['sec-fetch-site'] !== undefined) {
            return res.status(403).json({ error: 'Browser access is not allowed' });
        }
        next();
    });`);
  if (!daemon.includes('SOLEVIA_LOCAL_HTTP_BOUNDARY')) throw new Error('Local daemon boundary patch did not match');
  writeFileSync(daemonPath, daemon);
}
