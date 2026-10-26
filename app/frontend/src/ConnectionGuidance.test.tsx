import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { UserSettingsPanel } from './UserSettingsPanel'
import { getModelConnection, getWorkspaceScheduleProfile, updateModelConnection } from './api'
import { request } from './api/client'

vi.mock('./api', () => ({ getModelConnection: vi.fn(), getWorkspaceScheduleProfile: vi.fn(), updateModelConnection: vi.fn() }))
vi.mock('./api/client', () => ({ request: vi.fn() }))
afterEach(() => { cleanup(); vi.resetAllMocks() })

async function openConnection(base = 'https://provider.test/v1') {
  vi.mocked(getModelConnection).mockResolvedValue({ provider: 'openai_compatible', base_url: base,
    model: 'example-model', proxy_url: '', api_key_configured: false, api_key_hint: null })
  vi.mocked(getWorkspaceScheduleProfile).mockResolvedValue({} as Awaited<ReturnType<typeof getWorkspaceScheduleProfile>>)
  vi.mocked(request).mockResolvedValue({ mode: 'api', api_configured: false, codex_connected: false,
    capabilities: { api: true, codex: true, file_copies: true } })
  render(<UserSettingsPanel initialTab="MODEL" onClose={() => {}} />)
  await screen.findByRole('button', { name: '读取可用模型' })
}

it('catches an incomplete API URL before sending a credential-bearing request', async () => {
  await openConnection('provider.test/v1')
  fireEvent.change(screen.getByLabelText('API Key'), { target: { value: 'synthetic-test-key' } })
  fireEvent.click(screen.getByRole('button', { name: '读取可用模型' }))
  expect(await screen.findByRole('alert')).toHaveTextContent('以 https:// 或 http:// 开头')
  expect(vi.mocked(request).mock.calls.some(([path]) => path.includes('/catalog'))).toBe(false)
})

it('requires a key before saving a first-time API profile', async () => {
  await openConnection()
  fireEvent.click(screen.getByRole('button', { name: '保存并使用 API' }))
  expect(await screen.findByRole('alert')).toHaveTextContent('请先填写服务商提供的 API Key')
  expect(updateModelConnection).not.toHaveBeenCalled()
})

it('keeps manual model entry available when a provider denies its catalog', async () => {
  await openConnection()
  vi.mocked(request).mockResolvedValue({ models: [], available: false, message: '服务商未授权读取模型列表（403）。请检查 Key 权限，也可手动填写模型 ID。' })
  fireEvent.click(screen.getByRole('button', { name: '读取可用模型' }))
  expect(await screen.findByText(/服务商未授权读取模型列表/)).toBeInTheDocument()
  expect(screen.getByLabelText('模型名称')).toBeEnabled()
  expect(screen.getByRole('button', { name: '保存并使用 API' })).toBeEnabled()
})
