import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { CodexComposerSettings } from './CodexModelSettings'
import { CodexGenerationStatus } from './CodexGenerationStatus'
import { request } from '../api/client'

vi.mock('../api/client', () => ({ request: vi.fn() }))
afterEach(() => { cleanup(); vi.resetAllMocks(); vi.unstubAllEnvs() })

it('offers supported effort choices and saves the choice with its long wait policy', async () => {
  vi.stubEnv('VITE_SOLEVIA_RUNTIME', 'codex')
  const preferences = { model: 'gpt-6.1-sol', reasoning_effort: 'low', supported_efforts: ['low', 'high'], wait_seconds: 600,
    wait_seconds_by_effort: { low: 600, high: 2400, max: 5400 }, models: [
      { id: 'gpt-6.1-sol', display_name: 'Sol', supported_efforts: ['low', 'high'], default_effort: 'low' },
      { id: 'other', display_name: 'Other', supported_efforts: ['max'], default_effort: 'max' }] }
  vi.mocked(request).mockResolvedValueOnce(preferences).mockRejectedValueOnce(new Error('保存失败')).mockResolvedValueOnce({ ...preferences, model: 'other', reasoning_effort: 'max', wait_seconds: 5400 })
  render(<CodexComposerSettings />)
  fireEvent.click(screen.getByRole('button', { name: '选择 Codex 模型与思考强度' }))
  const selector = await screen.findByRole('combobox', { name: 'Codex 思考强度' })
  fireEvent.change(selector, { target: { value: 'high' } })
  expect(screen.getByText(/最多等待 40 分钟/)).toBeInTheDocument()
  fireEvent.change(screen.getByRole('combobox', { name: 'Codex 模型' }), { target: { value: 'other' } })
  expect(selector).toHaveValue('max')
  fireEvent.click(screen.getByRole('button', { name: '应用' }))
  await screen.findByText('保存失败')
  expect(screen.getByRole('button', { name: '选择 Codex 模型与思考强度' })).toHaveTextContent('gpt-6.1-sol · 低')
  fireEvent.click(screen.getByRole('button', { name: '应用' }))
  await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
  expect(screen.getByRole('button', { name: '选择 Codex 模型与思考强度' })).toHaveTextContent('other · 最高')
  expect(request).toHaveBeenLastCalledWith('/api/codex/preferences', { method: 'PUT', body: '{"model":"other","reasoning_effort":"max"}' })
})

it('reports backend activity and real checkpoints without exposing candidate text', async () => {
  vi.stubEnv('VITE_SOLEVIA_RUNTIME', 'codex')
  vi.mocked(request).mockResolvedValue({ connected: true, active_count: 1, queued_count: 0,
    requests: [{ stage: 'THINKING', last_activity_seconds: 100 }],
    plan_checkpoint: { status: 'RUNNING', completed_weeks: 2, total_weeks: 4 } })
  render(<CodexGenerationStatus active scope="033f1c89-8570-45f0-b8df-9c318e4bb7fc" />)
  await screen.findByText(/正在处理 1 个生成请求/)
  expect(screen.getByText(/已校验 2 \/ 4 周/)).toBeInTheDocument()
  expect(screen.getByText(/暂未收到新的阶段事件/)).toBeInTheDocument()
})

it('does not call a disconnected or unobservable backend normal generation', async () => {
  vi.stubEnv('VITE_SOLEVIA_RUNTIME', 'codex')
  vi.mocked(request).mockResolvedValueOnce({ connected: false, requests: [], active_count: 0, queued_count: 0 })
  const view = render(<CodexGenerationStatus active scope="first" />)
  await screen.findByText(/连接已断开/)
  vi.mocked(request).mockRejectedValueOnce(new Error('offline'))
  view.rerender(<CodexGenerationStatus active scope="second" />)
  await waitFor(() => expect(screen.getByText(/暂时无法取得后台生成状态/)).toBeInTheDocument())
  expect(screen.queryByText(/正在处理 1/)).not.toBeInTheDocument()
})

it('shows a bounded retry wait and does not send another generation request', async () => {
  vi.stubEnv('VITE_SOLEVIA_RUNTIME', 'codex')
  vi.mocked(request).mockResolvedValue({ connected: true, active_count: 0, queued_count: 0, requests: [],
    retries: [{ attempt: 2, maximum_attempts: 3, wait_seconds: 4 }] })
  render(<CodexGenerationStatus active scope="retry" />)
  await screen.findByText(/4 秒后自动进行第 2 \/ 3 次尝试/)
  expect(request).toHaveBeenCalledOnce()
  expect(request).toHaveBeenCalledWith('/api/codex/generation-status')
})
