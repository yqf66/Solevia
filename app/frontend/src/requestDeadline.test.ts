import { afterEach, describe, expect, it, vi } from 'vitest'
import { request } from './api'

afterEach(() => { vi.unstubAllGlobals(); vi.restoreAllMocks() })

describe('request connection lifecycle', () => {
  it('ends a stalled write with an actionable warning and never resends it', async () => {
    const fetch = vi.fn((_path, options: RequestInit) => new Promise((_resolve, reject) => {
      options.signal!.addEventListener('abort', () => reject(new DOMException('Aborted', 'AbortError')))
    }))
    vi.stubGlobal('fetch', fetch)
    await expect(request('/api/example', { method: 'POST', body: '{}' }, 10)).rejects.toThrow('避免重复提交')
    expect(fetch).toHaveBeenCalledOnce()
  })

  it('retains successful results and readable server errors', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValueOnce(new Response('{"saved":true}'))
      .mockResolvedValueOnce(new Response('{"detail":"No pending plan adjustment exists"}', { status: 409 })))
    await expect(request('/api/example')).resolves.toEqual({ saved: true })
    await expect(request('/api/example')).rejects.toThrow('已经处理或失效')
  })

  it('explains a broken connection without claiming the write failed on the server', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new TypeError('Failed to fetch')))
    await expect(request('/api/example', { method: 'POST' })).rejects.toThrow('先刷新确认结果')
  })

  it('preserves dependency reasons and structured conflict codes', async () => {
    vi.stubGlobal('fetch', vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify({ detail: '需先完成前置工作项：模拟考试' }), { status: 409 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ detail: { code: 'SCHEDULE_CONFLICT', message: '所选时段与课程重叠' } }), { status: 409 })))
    await expect(request('/api/example')).rejects.toThrow('需先完成前置工作项：模拟考试')
    await expect(request('/api/example')).rejects.toMatchObject({ code: 'SCHEDULE_CONFLICT', message: '所选时段与课程重叠' })
  })
})
