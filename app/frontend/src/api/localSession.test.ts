import { afterEach, describe, expect, it, vi } from 'vitest'
vi.unmock('./localSession')
import { clearLocalSession, localSession } from './localSession'

afterEach(() => { clearLocalSession(); vi.unstubAllGlobals() })
describe('local browser session', () => {
  it('uses a same-origin bootstrap header and shares a single in-memory session', async () => {
    const token = 'a'.repeat(43)
    const fetch = vi.fn().mockImplementation(async () => new Response(JSON.stringify({ token })))
    vi.stubGlobal('fetch', fetch)
    await expect(Promise.all([localSession(), localSession()])).resolves.toEqual([token, token])
    expect(fetch).toHaveBeenCalledOnce()
    expect(fetch).toHaveBeenCalledWith('/api/session', expect.objectContaining({
      headers: { 'X-Solevia-Bootstrap': '1' }, cache: 'no-store', credentials: 'same-origin',
    }))
    clearLocalSession()
    await localSession()
    expect(fetch).toHaveBeenCalledTimes(2)
  })
  it('rejects invalid sessions and permits a fresh bootstrap after failure', async () => {
    const fetch = vi.fn().mockResolvedValueOnce(new Response('{}')).mockResolvedValueOnce(new Response(JSON.stringify({ token: 'b'.repeat(43) })))
    vi.stubGlobal('fetch', fetch)
    await expect(localSession()).rejects.toThrow('无效')
    await expect(localSession()).resolves.toBe('b'.repeat(43))
  })
})
