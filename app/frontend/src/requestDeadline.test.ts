import { afterEach, describe, expect, it, vi } from 'vitest'
import { request } from './api'

afterEach(() => { vi.unstubAllGlobals(); vi.restoreAllMocks() })

describe('request connection lifecycle', () => {
  it('shows actionable kernel setup and busy errors without masking them as generation failures', async () => {
    const missing = '请先保存 API 地址、模型和密钥，再切换到自配置 API。'
    const busy = '当前还有生成或资料研究在进行，请等待完成后再切换连接方式。'
    vi.stubGlobal('fetch', vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify({ detail: missing }), { status: 422 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ detail: busy }), { status: 409 })))
    await expect(request('/api/settings/kernel', { method: 'PUT', body: '{"mode":"api"}' })).rejects.toThrow(missing)
    await expect(request('/api/settings/kernel', { method: 'PUT', body: '{"mode":"api"}' })).rejects.toThrow(busy)
  })
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
