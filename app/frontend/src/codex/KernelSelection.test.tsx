import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { act } from 'react'
import { KernelConnectionSettings, KernelSetupPrompt, selectKernel } from './KernelSelection'
import { CodexComposerSettings } from './CodexModelSettings'
import { request } from '../api/client'

vi.mock('../api/client', () => ({ request: vi.fn() }))
afterEach(() => { cleanup(); vi.unstubAllEnvs(); vi.resetAllMocks() })

it('switches the shared workspace and composer without discarding either configuration', async () => {
  vi.stubEnv('VITE_SOLEVIA_RUNTIME', 'unified')
  const base = { api_configured: true, codex_connected: true, capabilities: { codex: true, api: true, file_copies: true } }
  vi.mocked(request).mockImplementation(async (path, options) => {
    if (path === '/api/codex/preferences') return { model: 'codex-model', reasoning_effort: 'high', models: [], wait_seconds_by_effort: {} }
    if (options?.method === 'PUT') return { ...base, mode: JSON.parse(options.body as string).mode }
    return { ...base, mode: 'codex' }
  })
  render(<><KernelConnectionSettings><p>已保存的 API 配置</p><button onClick={() => void selectKernel('api')}>保存并使用 API</button></KernelConnectionSettings><CodexComposerSettings /></>)
  await screen.findByRole('button', { name: '选择 Codex 模型与思考强度' })
  fireEvent.change(screen.getByRole('combobox', { name: '内核连接方式' }), { target: { value: 'api' } })
  expect(screen.getByText('已保存的 API 配置')).toBeInTheDocument()
  expect(screen.getByText('正在配置：自配置 API')).toBeInTheDocument()
  expect(screen.getByText(/目前仍由 Codex 内核处理对话/)).toBeInTheDocument()
  expect(screen.queryByText(/当前使用：Codex 内核/)).not.toBeInTheDocument()
  fireEvent.click(screen.getByText('保存并使用 API'))
  await waitFor(() => expect(screen.queryByRole('button', { name: '选择 Codex 模型与思考强度' })).not.toBeInTheDocument())
  expect(screen.getByText(/当前使用：自配置 API/)).toBeInTheDocument()
  fireEvent.change(screen.getByRole('combobox', { name: '内核连接方式' }), { target: { value: 'codex' } })
  fireEvent.click(screen.getByText('使用 Codex 内核'))
  await screen.findByRole('button', { name: '选择 Codex 模型与思考强度' })
  expect(screen.getByText(/当前使用：Codex 内核/)).toBeInTheDocument()
})


it('keeps the last working connection when a response has an invalid shape', async () => {
  vi.mocked(request).mockResolvedValue({})
  render(<CodexComposerSettings />)
  expect(screen.getByRole('button', { name: '选择 Codex 模型与思考强度' })).toBeInTheDocument()
  await expect(selectKernel('api')).rejects.toThrow('连接状态返回不完整')
  expect(screen.getByRole('button', { name: '选择 Codex 模型与思考强度' })).toBeInTheDocument()
})

it('keeps the real active kernel and explains an unsuccessful API switch', async () => {
  vi.mocked(request).mockRejectedValue(new Error('请先保存完整的 API 配置。'))
  render(<KernelConnectionSettings><p>填写 API 配置</p></KernelConnectionSettings>)
  fireEvent.change(screen.getByRole('combobox', { name: '内核连接方式' }), { target: { value: 'api' } })
  fireEvent.click(screen.getByRole('button', { name: '使用已保存的 API' }))
  expect(await screen.findByRole('alert')).toHaveTextContent('请先保存完整的 API 配置。')
  expect(screen.getByText('正在配置：自配置 API')).toBeInTheDocument()
  expect(screen.getByText(/目前仍由 Codex 内核处理对话/)).toBeInTheDocument()
})

it('guides a disconnected Codex user and refreshes the actual state without claiming a reconnect', async () => {
  const missing = { mode: 'codex', codex_connected: false, codex_error_code: 'CODEX_NOT_FOUND',
    codex_error: '尚未找到本机 Codex。请先安装 Codex。', capabilities: { codex: true, api: true, file_copies: true } }
  vi.mocked(request).mockResolvedValue(missing)
  await act(async () => { await selectKernel('codex') })
  const configure = vi.fn()
  render(<><KernelSetupPrompt onConfigure={configure} /><KernelConnectionSettings><p>API 配置</p></KernelConnectionSettings></>)
  expect(screen.queryByText('对话、目标和资料已连接到当前内核。')).not.toBeInTheDocument()
  expect(screen.getByText(/状态编号：CODEX_NOT_FOUND/)).toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', { name: /Codex 需要完成连接/ }))
  expect(configure).toHaveBeenCalledOnce()
  expect(screen.getByRole('link', { name: /官方安装指南/ })).toHaveAttribute('href', 'https://learn.chatgpt.com/docs/cli')
  vi.mocked(request).mockResolvedValue({ ...missing, codex_connected: true, codex_error: null, codex_error_code: null })
  fireEvent.click(screen.getByRole('button', { name: '刷新状态' }))
  await waitFor(() => expect(screen.queryByText('还差一步，连接本机 Codex')).not.toBeInTheDocument())
  expect(screen.getByText(/登录状态检查已通过/)).toBeInTheDocument()
  expect(vi.mocked(request)).toHaveBeenLastCalledWith('/api/settings/kernel')
})

it('shows first-run API setup instead of a success indicator before credentials are configured', async () => {
  vi.mocked(request).mockResolvedValue({ mode: 'api', api_configured: false, codex_connected: false,
    capabilities: { codex: true, api: true, file_copies: true } })
  await act(async () => { await selectKernel('api') })
  render(<><KernelSetupPrompt onConfigure={() => {}} /><KernelConnectionSettings><p>API 配置</p></KernelConnectionSettings></>)
  expect(screen.getByRole('button', { name: /先连接一个模型/ })).toBeInTheDocument()
  expect(screen.getByText(/补齐服务地址、密钥和模型/)).toBeInTheDocument()
  expect(screen.queryByText(/API 配置已保存/)).not.toBeInTheDocument()
})
