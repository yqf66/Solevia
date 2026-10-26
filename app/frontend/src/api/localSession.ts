// The credential lives only in this page's memory and is never sent to an external URL.
let pending: Promise<string> | undefined

export function localSession(signal?: AbortSignal): Promise<string> {
  if (!pending) {
    pending = fetch('/api/session', {
      headers: { 'X-Solevia-Bootstrap': '1' },
      cache: 'no-store',
      credentials: 'same-origin',
      signal,
    }).then(async response => {
      if (!response.ok) throw new Error('无法建立本地会话，请刷新工作台。')
      const data = await response.json() as { token?: unknown }
      if (typeof data.token !== 'string' || data.token.length < 32) throw new Error('本地会话无效，请刷新工作台。')
      return data.token
    }).catch(error => { pending = undefined; throw error })
  }
  return pending
}

export function clearLocalSession() { pending = undefined }
