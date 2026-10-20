// Stable classifications only; never expose URLs, proxy credentials or bodies.
export function searchFailure(error) {
  if (error?.code?.startsWith('SEARCH_')) return error;
  const status = error?.response?.status;
  const code = status === 429 ? 'SEARCH_RATE_LIMIT'
    : [401, 403].includes(status) ? 'SEARCH_BLOCKED'
    : status >= 500 || /TIMEOUT|ECONN|EAI_AGAIN|ENOTFOUND|ETIMEDOUT/i.test(error?.code || '')
      ? 'SEARCH_TRANSIENT' : 'SEARCH_REQUEST_FAILED';
  return Object.assign(new Error(code), { code });
}

export function assertDuckDuckGoPage(body) {
  if (/anomaly\.js|anomaly-modal|challenge-form|bots use DuckDuckGo|verify you are human/i.test(String(body))) {
    throw Object.assign(new Error('SEARCH_CHALLENGE'), { code: 'SEARCH_CHALLENGE' });
  }
}

export function assertDuckDuckGoEmpty(body) {
  assertDuckDuckGoPage(body);
  if (!/no-results|no results found|no results\s+(?:for|matching)/i.test(String(body))) {
    throw Object.assign(new Error('SEARCH_PARSE_FAILED'), { code: 'SEARCH_PARSE_FAILED' });
  }
}
