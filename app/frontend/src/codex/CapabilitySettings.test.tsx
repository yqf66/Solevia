import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { request } from '../api/client'
import { CapabilitySettings } from './CapabilitySettings'

vi.mock('../api/client', () => ({ request: vi.fn() }))
afterEach(() => { cleanup(); sessionStorage.clear(); vi.restoreAllMocks(); vi.resetAllMocks() })
const features = { web: false, documents: false, ocr: false, semantic: false }
const plan = { capabilities: [{ Key: 'documents', Name: '阅读文档', Benefit: '导入 PDF 与 Office 文档', DownloadMiB: [40, 150] }], plan: { features, downloadMiB: [150, 350], diskMiB: [700, 1500] }, space: { free: 10 * 1024 ** 3, required: 4 * 1024 ** 3, enough: true } }
function setup(enough = true) {
  vi.mocked(request).mockImplementation(async path => path.endsWith('/prepare') ? { bridge: { url: 'http://127.0.0.1:54444', token: 'temporary-token' }, permit: 'explicit-permit' } : { installed: features })
  const fetcher = vi.fn(async (_url: unknown, options: RequestInit = {}) => {
    const url = String(_url)
    return { ok: true, json: async () => url.endsWith('/plan') ? { ...plan, space: { ...plan.space, enough } } : url.endsWith('/install') ? { status: 'INSTALLING', stage: '正在安装所选依赖', step: 4, elapsed: 8, bytes: 0, speed: 0, total: -1, logs: [] } : options.method === 'GET' ? { status: 'IDLE', logs: [], elapsed: 0 } : {} } as Response
  })
  vi.stubGlobal('fetch', fetcher)
  return fetcher
}
it('selects and installs inside the website only after explicit confirmation', async () => {
  const fetcher = setup()
  render(<CapabilitySettings />)
  fireEvent.click(screen.getByText('补装与管理能力'))
  fireEvent.click(screen.getByRole('button', { name: '选择要安装的能力' }))
  const checkbox = await screen.findByRole('checkbox')
  expect(fetcher.mock.calls.some(([url]) => String(url).endsWith('/install'))).toBe(false)
  fireEvent.click(checkbox)
  fireEvent.click(screen.getByRole('button', { name: '确认并开始补装' }))
  await waitFor(() => expect(fetcher.mock.calls.some(([url]) => String(url).endsWith('/install'))).toBe(true))
  const call = fetcher.mock.calls.find(([url]) => String(url).endsWith('/install'))!
  const body = JSON.parse(String(call[1]?.body))
  expect(body.permit).toBe('explicit-permit')
  expect(body.features.documents).toBe(true)
  expect(vi.mocked(request).mock.calls.some(([path]) => path.endsWith('/manage'))).toBe(false)
})
it('prevents starting when the target drive is short of space', async () => {
  setup(false)
  render(<CapabilitySettings />)
  fireEvent.click(screen.getByText('补装与管理能力'))
  fireEvent.click(screen.getByRole('button', { name: '选择要安装的能力' }))
  expect(await screen.findByRole('button', { name: '确认并开始补装' })).toBeDisabled()
  expect(screen.getByText(/空间不足/)).toBeInTheDocument()
})
