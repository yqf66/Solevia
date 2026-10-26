import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { ConversationFiles, type FileCopy } from './ConversationFiles'
import { request } from '../api/client'

vi.mock('../api/client', () => ({ request: vi.fn() }))
const task: FileCopy = { id: 'copy-1', goal_id: 'goal-1', status: 'NEEDS_CONFIRMATION', source_names: ['面试准备.md'], reason: '', error: '', summary: '', files: [] }
beforeEach(() => vi.stubEnv('VITE_SOLEVIA_RUNTIME', 'codex'))
afterEach(() => { cleanup(); vi.resetAllMocks(); vi.unstubAllEnvs() })

it('asks inside the answer, keeps the reason optional, and persists a refusal', async () => {
  vi.mocked(request).mockResolvedValueOnce([task]).mockResolvedValueOnce({ ...task, status: 'DENIED', reason: '先保留这份原稿' })
  render(<ConversationFiles messageId="answer-1" />)
  await screen.findByRole('region', { name: '文件修改确认' })
  expect(screen.queryByRole('textbox')).not.toBeInTheDocument()
  expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', { name: '填写拒绝理由' }))
  fireEvent.change(screen.getByRole('textbox', { name: '拒绝理由（选填）' }), { target: { value: '先保留这份原稿' } })
  fireEvent.click(screen.getByRole('button', { name: '拒绝' }))
  await screen.findByText('已拒绝本次修改：先保留这份原稿')
  expect(request).toHaveBeenLastCalledWith('/api/codex/conversation-files/goal-1/copy-1/decision', {
    method: 'POST', body: JSON.stringify({ decision: 'DENY', reason: '先保留这份原稿' }),
  })
})

it('does not auto-allow, and exposes completed files as inert text with a download', async () => {
  vi.mocked(request).mockResolvedValueOnce([task]).mockResolvedValueOnce({ ...task, status: 'SUCCEEDED', summary: '已补充复练清单。', files: [{ index: 0, name: '面试准备-修改版.html', bytes: 100 }] })
    .mockResolvedValueOnce({ name: '面试准备-修改版.html', content: '<script>window.unsafe = true</script>' })
  render(<ConversationFiles messageId="answer-1" />)
  await screen.findByText('允许生成文件修改版吗？')
  expect(request).toHaveBeenCalledTimes(1)
  fireEvent.click(screen.getByRole('button', { name: '允许' }))
  await screen.findByText('已补充复练清单。')
  fireEvent.click(screen.getByRole('button', { name: '面试准备-修改版.html' }))
  await screen.findByText('<script>window.unsafe = true</script>')
  expect(document.querySelector('script')).toBeNull()
  expect(screen.getByRole('button', { name: '下载 面试准备-修改版.html' })).toBeEnabled()
})

it('waits for validated answers and reloads when the same answer finishes', async () => {
  vi.mocked(request).mockResolvedValue([task])
  const view = render(<ConversationFiles messageId="answer-1" ready={false} />)
  expect(request).not.toHaveBeenCalled()
  view.rerender(<ConversationFiles messageId="answer-1" ready />)
  await screen.findByText('允许生成文件修改版吗？')
  view.rerender(<ConversationFiles messageId="answer-2" ready />)
  await waitFor(() => expect(request).toHaveBeenLastCalledWith('/api/codex/conversation-files/answers/answer-2'))
})
