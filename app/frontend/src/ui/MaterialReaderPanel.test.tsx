import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import * as api from '../api'
import { MaterialReaderPanel } from './MaterialReaderPanel'

vi.mock('../api', () => ({ listMaterialRevisions: vi.fn(), readMaterial: vi.fn(), compareMaterialRevisions: vi.fn() }))
afterEach(() => { cleanup(); vi.resetAllMocks() })

it('reads fixed-version pages and compares historical versions without changing the plan', async () => {
  const latest = { id: 'v2', revision: 2, status: 'READY', metadata: {} }
  const older = { ...latest, id: 'v1', revision: 1 }
  vi.mocked(api.listMaterialRevisions).mockResolvedValue([latest, older])
  vi.mocked(api.readMaterial).mockResolvedValue({ source_id: 'source', title: 'report.md', revision: latest, offset: 0, next_offset: 12000, total_chars: 15000, text: 'First page text', outline: [{ title: 'Deployment', offset: 13000, sequence: 8 }], outline_truncated: false, warnings: ['第 2 页 OCR 未完成'] })
  vi.mocked(api.compareMaterialRevisions).mockResolvedValue({ added_lines: 1, removed_lines: 1, lines: [{kind:'added', text:'New deadline'}], truncated: true, compared_chars: [100000,100000], warning: '变化不会自动修改已确认事实' })
  render(<MaterialReaderPanel goalId="goal" sourceId="source" onBack={vi.fn()} />)
  expect(await screen.findByText('First page text')).toBeInTheDocument()
  expect(screen.getByText('第 2 页 OCR 未完成')).toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', { name: '下一段' }))
  await waitFor(() => expect(api.readMaterial).toHaveBeenCalledWith('goal', 'source', 'v2', 12000))
  await waitFor(() => expect(screen.getByRole('button', {name:'下一段'})).toBeEnabled())
  fireEvent.click(screen.getByText('版本对比'))
  fireEvent.click(screen.getByRole('button', {name:'查看变化'}))
  expect(await screen.findByText('+ New deadline')).toBeInTheDocument()
  expect(api.compareMaterialRevisions).toHaveBeenCalledWith('goal','source','v1','v2')
})
