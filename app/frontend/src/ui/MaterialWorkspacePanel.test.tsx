import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import * as api from '../api'
import { MaterialWorkspacePanel } from './MaterialWorkspacePanel'

vi.mock('../api', () => ({
  listMaterialRoots: vi.fn(), listGoalMaterials: vi.fn(), listMaterialAnalyses: vi.fn(),
  scanMaterialRoot: vi.fn(), importMaterialBatch: vi.fn(), analyzeGoalMaterials: vi.fn(),
  createMaterialRoot: vi.fn(), confirmMaterialFindings: vi.fn(), processLocalMaterial: vi.fn(),
  refreshLocalMaterial: vi.fn(), unlinkGoalMaterial: vi.fn(), uploadMaterialFile: vi.fn(),
  indexMaterialSemantics: vi.fn(), listMaterialRevisions: vi.fn(), readMaterial: vi.fn(), compareMaterialRevisions: vi.fn(),
}))

const material: api.GoalMaterial = {
  link_id: 'link', source_id: 'source', kind: 'LOCAL_FILE', title: 'spec.md', mime_type: null,
  root_id: 'root', relative_path: 'spec.md', canonical_uri: '', source_status: 'ACTIVE',
  scope: 'ALL', usage_policy: 'MANUAL_ONLY', pinned: false, link_status: 'ACTIVE',
  latest_revision: { id: 'revision', revision: 1, status: 'READY', metadata: { chunk_count: 1 } },
}

beforeEach(() => {
  vi.resetAllMocks()
  vi.mocked(api.listMaterialRoots).mockResolvedValue([{ id: 'root', display_name: '资料', path: 'C:/materials', status: 'ACTIVE', created_at: '', updated_at: '' }])
  vi.mocked(api.listGoalMaterials).mockResolvedValue([material])
  vi.mocked(api.listMaterialAnalyses).mockResolvedValue([])
})
afterEach(cleanup)

it('keeps optional PDF reparse in management and explicitly requests MinerU', async () => {
  const pdf = { ...material, title: 'complex.pdf', relative_path: 'complex.pdf' }
  vi.mocked(api.listGoalMaterials).mockResolvedValue([pdf])
  vi.mocked(api.processLocalMaterial).mockResolvedValue(pdf)
  render(<MaterialWorkspacePanel goalId="goal" />)
  fireEvent.click(screen.getByRole('button', { name: '添加或阅读本地资料' }))
  await screen.findByText('complex.pdf', { selector: 'strong' })
  fireEvent.click(screen.getByText(/已关联资料 · 1 份/))
  fireEvent.click(screen.getByRole('button', { name: '用 MinerU 重解析' }))
  await waitFor(() => expect(api.processLocalMaterial).toHaveBeenCalledWith('goal', 'source', 'MINERU'))
})

it('scans, selects supported files, preserves per-file import errors and restores focus', async () => {
  const files = ['a.md', 'b.md', 'image.png'].map((relative_path) => ({ relative_path, title: relative_path, extension: '', file_size: 1024, supported: relative_path !== 'image.png', reason: relative_path === 'image.png' ? '不支持' : null, linked: false, revision_status: null }))
  vi.mocked(api.scanMaterialRoot).mockResolvedValue({ root_id: 'root', scanned_at: '', truncated: false, files })
  vi.mocked(api.importMaterialBatch).mockResolvedValue({ imported: [material], errors: [{ relative_path: 'b.md', message: '文件已移走' }] })
  render(<MaterialWorkspacePanel goalId="goal" />)
  const trigger = screen.getByRole('button', { name: '添加或阅读本地资料' })
  fireEvent.click(trigger)
  await waitFor(() => expect(screen.getByRole('button', { name: '扫描目录' })).toBeEnabled())
  fireEvent.click(screen.getByRole('button', { name: '扫描目录' }))
  fireEvent.click(await screen.findByText('扫描结果 · 3 个文件 · 已选 0 个'))
  await screen.findByRole('checkbox', { name: '选择 a.md' })
  expect(screen.getByRole('checkbox', { name: '选择 image.png' })).toBeDisabled()
  fireEvent.click(screen.getByRole('button', { name: '选择可导入文件' }))
  fireEvent.click(screen.getByRole('button', { name: '导入并解析 2 份' }))
  await waitFor(() => expect(api.importMaterialBatch).toHaveBeenCalledWith('goal', 'root', ['a.md', 'b.md']))
  expect(await screen.findByText('b.md：文件已移走')).toBeInTheDocument()
  fireEvent.keyDown(document, { key: 'Escape' })
  expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
  expect(document.activeElement).toBe(trigger)
})

it('uses selected-file scope and prevents confirmation of blocked facts', async () => {
  vi.mocked(api.listMaterialAnalyses).mockResolvedValue([{
    id: 'analysis', goal_id: 'goal', query: '期限', status: 'COMPLETED', summary: '文件期限冲突',
    key_points: [], findings: [{ text: '六月完成', category: 'DEADLINE', confidence: 1, status: 'BLOCKED', citations: [] }],
    open_questions: [], source_count: 2, file_count: 2, created_at: '2026-10-07', updated_at: '',
    conflicts: [{ description: '六月与七月不一致', citations: [] }],
  }])
  vi.mocked(api.analyzeGoalMaterials).mockResolvedValue({} as api.MaterialAnalysis)
  render(<MaterialWorkspacePanel goalId="goal" />)
  fireEvent.click(screen.getByRole('button', { name: '添加或阅读本地资料' }))
  fireEvent.click(screen.getByRole('tab', { name: '阅读分析' }))
  expect(await screen.findByText('六月与七月不一致')).toBeInTheDocument()
  fireEvent.click(screen.getByText('查看分析详情 · 0 个要点 · 1 条事实'))
  expect(screen.getByRole('checkbox')).toBeDisabled()
  fireEvent.change(screen.getByLabelText('分析范围'), { target: { value: 'selected' } })
  fireEvent.click(screen.getByRole('checkbox', { name: 'spec.md' }))
  fireEvent.change(screen.getByLabelText('资料分析问题'), { target: { value: '交付期限是什么？' } })
  fireEvent.click(screen.getByRole('button', { name: '分析资料' }))
  await waitFor(() => expect(api.analyzeGoalMaterials).toHaveBeenCalledWith('goal', '交付期限是什么？', { sourceIds: ['source'] }))
})

it('shows only two upload commands without loading the library and supports dismissal', () => {
  render(<MaterialWorkspacePanel goalId="goal" compact onSelectionChange={vi.fn()} />)
  const trigger = screen.getByRole('button', { name: '添加或阅读本地资料' })
  fireEvent.click(trigger)
  expect(screen.getAllByRole('menuitem')).toHaveLength(2)
  expect(screen.getByRole('menuitem', { name: '选择文件' })).toHaveFocus()
  expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
  expect(screen.queryByText('管理目录和资料')).not.toBeInTheDocument()
  expect(api.listMaterialRoots).not.toHaveBeenCalled()
  expect(api.listGoalMaterials).not.toHaveBeenCalled()
  fireEvent.keyDown(screen.getByRole('menu'), { key: 'ArrowDown' })
  expect(screen.getByRole('menuitem', { name: '选择文件夹' })).toHaveFocus()
  fireEvent.keyDown(screen.getByRole('menu'), { key: 'Escape' })
  expect(screen.queryByRole('menu')).not.toBeInTheDocument()
  expect(trigger).toHaveFocus()
  fireEvent.click(trigger)
  fireEvent.pointerDown(document.body)
  expect(screen.queryByRole('menu')).not.toBeInTheDocument()
})

it('uploads a selected file and attaches it to the current conversation', async () => {
  const onSelectionChange = vi.fn()
  const uploaded = { ...material, source_id: 'uploaded-source', title: 'direct.md', relative_path: 'notes/direct.md', latest_revision: { id: 'uploaded-revision', revision: 1, status: 'QUEUED' as const, metadata: {} } }
  vi.mocked(api.uploadMaterialFile).mockResolvedValue(uploaded)
  render(<MaterialWorkspacePanel goalId="goal" compact selectedSourceIds={[]} onSelectionChange={onSelectionChange} />)
  fireEvent.click(screen.getByRole('button', { name: '添加或阅读本地资料' }))
  const input = screen.getByLabelText('选择附件文件') as HTMLInputElement
  const choose = vi.spyOn(input, 'click')
  fireEvent.click(screen.getByRole('menuitem', { name: '选择文件' }))
  expect(choose).toHaveBeenCalledOnce()
  expect(screen.queryByRole('menu')).not.toBeInTheDocument()
  const file = new File(['# Direct notes\n'], 'direct.md', { type: 'text/markdown' })
  fireEvent.change(input, { target: { files: [file] } })
  await waitFor(() => expect(api.uploadMaterialFile).toHaveBeenCalledWith('goal', file, 'direct.md'))
  await waitFor(() => expect(onSelectionChange).toHaveBeenCalledWith([uploaded]))
})

it('uploads folder files independently, preserves existing attachments and reports failures', async () => {
  const onSelectionChange = vi.fn()
  const added = { ...material, source_id: 'new', title: 'notes.md' }
  vi.mocked(api.uploadMaterialFile).mockResolvedValueOnce(added).mockRejectedValueOnce(new Error('不支持此文件'))
  render(<MaterialWorkspacePanel goalId="goal" compact selectedSourceIds={['source']} onSelectionChange={onSelectionChange} />)
  await waitFor(() => expect(onSelectionChange).toHaveBeenCalledWith([material]))
  fireEvent.click(screen.getByRole('button', { name: '添加或阅读本地资料' }))
  const folder = screen.getByLabelText('选择附件文件夹') as HTMLInputElement
  const choose = vi.spyOn(folder, 'click')
  fireEvent.click(screen.getByRole('menuitem', { name: '选择文件夹' }))
  expect(choose).toHaveBeenCalledOnce()
  expect(folder).toHaveAttribute('webkitdirectory')
  const file = new File(['notes'], 'notes.md')
  Object.defineProperty(file, 'webkitRelativePath', { value: 'folder/notes.md' })
  fireEvent.change(folder, { target: { files: [file, new File(['bad'], 'image.bin')] } })
  await waitFor(() => expect(api.uploadMaterialFile).toHaveBeenCalledWith('goal', file, 'folder/notes.md'))
  await waitFor(() => expect(onSelectionChange).toHaveBeenLastCalledWith([material, added]))
  expect(await screen.findByRole('alert')).toHaveTextContent('image.bin：不支持此文件')
})
