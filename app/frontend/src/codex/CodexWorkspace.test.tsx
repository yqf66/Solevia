import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { CodexWorkspace, decodeEvent } from './CodexWorkspace'

afterEach(() => { cleanup(); vi.unstubAllGlobals(); localStorage.clear() })

describe('Codex event decoding', () => {
  it('reads Unicode, multiline payload and durable event ids', () => {
    expect(decodeEvent('id: 14\r\nevent: assistant_content\r\ndata: {"content":\r\ndata: "你好"}\r\n')).toEqual({ id: 14, type: 'assistant_content', payload: { content: '你好' } })
    expect(decodeEvent(': keepalive')).toBeNull()
  })
})

it('loads saved conversation and disables mutations while the runtime is offline', async () => {
  vi.stubGlobal('fetch', vi.fn(async (path: string) => {
    if (path === '/api/session') return new Response(JSON.stringify({ token: 'test-token-with-at-least-thirty-two-characters' }))
    if (path === '/api/codex/sessions') return new Response(JSON.stringify({ sessions: [{ id: 'saved', title: '阅读计划' }] }))
    if (path === '/api/codex/status') return new Response(JSON.stringify({ connected: false, probe: { local_skills: [] }, error: '未连接运行时' }))
    return new Response(JSON.stringify({ session: { id: 'saved', title: '阅读计划' }, messages: [{ id: 'm1', role: 'assistant', content: '已保留的回答', status: 'succeeded' }], active_run: null, last_run: null, last_event_id: 0, requests: [] }))
  }))
  render(<CodexWorkspace />)
  expect(await screen.findByText('已保留的回答')).toBeVisible()
  fireEvent.change(screen.getByLabelText('发送给 Solevia'), { target: { value: '继续' } })
  await waitFor(() => expect(screen.getByRole('button', { name: /^发送$/ })).toBeDisabled())
  expect(screen.getByRole('alert')).toHaveTextContent('未连接运行时')
})
