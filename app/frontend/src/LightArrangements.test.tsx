import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { Link, MemoryRouter, Route, Routes } from 'react-router-dom'
import { LightCreatePage, LightPlanPage } from './LightArrangements'
import * as lightApi from './lightApi'
import * as api from './api'

vi.mock('./lightApi', async importOriginal => ({
  ...await importOriginal<typeof import('./lightApi')>(),
  createLightDraft: vi.fn(), getLightDraft: vi.fn(), generateLightDraft: vi.fn(),
  saveLightDraft: vi.fn(), applyLightDraft: vi.fn(), addLightTask: vi.fn(),
  upgradeLightPlan: vi.fn(),
}))
vi.mock('./api', async importOriginal => ({
  ...await importOriginal<typeof import('./api')>(),
  getManualPlan: vi.fn(), getUnifiedToday: vi.fn(), updateManualTask: vi.fn(),
  getGoalLifecycle: vi.fn(),
  deleteManualTask: vi.fn(),
}))
vi.mock('./AllocationDialog', () => ({ AllocationDialog: ({ manualPlanId }: { manualPlanId: string }) => <div role="dialog" aria-label="排课">{manualPlanId}</div> }))

const task = { ...lightApi.blankLightTask(), title: '练习口语' }
const draft: lightApi.LightDraft = { id: 'draft-1', raw_input: '练习口语', timezone: 'Asia/Shanghai',
  start_date: '2026-09-15', horizon_days: 7, status: 'DRAFT', revision: 1,
  result: { title: '近期事项', tasks: [task], assumptions: ['耗时暂估'], questions: [], unavailable_windows: [], full_planning_recommended: false },
  error_message: null, quick_plan_id: null }
const day = lightApi.localDate()
const plan: api.ManualPlan = { id: 'plan-1', title: '近期事项', description: '练习口语', timezone: 'Asia/Shanghai',
  kind: 'QUICK', status: 'ACTIVE', arrangement: { start_date: day, horizon_days: 7 },
  created_at: '', updated_at: '', tasks: [{ ...task, id: 'task-1', manual_plan_id: 'plan-1', quick_plan_id: 'plan-1', order_in_plan: 1,
    scheduled_date: day, recurrence: 'DAILY', recurrence_end_date: null, status: 'NOT_STARTED',
    overrides: [], created_at: '', updated_at: '' }] }
const board: api.UnifiedToday = { date: day, timezone: 'Asia/Shanghai', fixed_tasks: [], flexible_tasks: [], conflicts: [] }

function mount(path: string) {
  return render(<MemoryRouter initialEntries={[path]}><Routes>
    <Route path="/quick/new" element={<LightCreatePage />} />
    <Route path="/quick/drafts/:draftId" element={<LightCreatePage />} />
    <Route path="/quick/:planId" element={<LightPlanPage />} />
    <Route path="/goals/:goalId/today" element={<div>完整规划执行页</div>} />
  </Routes></MemoryRouter>)
}

beforeEach(() => {
  vi.mocked(lightApi.createLightDraft).mockResolvedValue(draft)
  vi.mocked(lightApi.getLightDraft).mockResolvedValue(draft)
  vi.mocked(lightApi.saveLightDraft).mockImplementation(async (_draft, result) => ({ ...draft, result, status: 'READY', revision: 2 }))
  vi.mocked(lightApi.applyLightDraft).mockResolvedValue({ ...draft, status: 'APPLIED', quick_plan_id: 'plan-1' })
  vi.mocked(api.getManualPlan).mockResolvedValue(plan)
  vi.mocked(api.getUnifiedToday).mockResolvedValue(board)
  vi.mocked(api.updateManualTask).mockResolvedValue(plan)
  vi.mocked(api.deleteManualTask).mockResolvedValue(plan)
  vi.mocked(api.getGoalLifecycle).mockResolvedValue({ id: 'goal-1', title: '健身', status: 'ACTIVE', timezone: 'Asia/Shanghai', pause_until: null, completed_at: null, archived_at: null, completion_summary: null, remaining_required_tasks: 3 })
  vi.mocked(lightApi.upgradeLightPlan).mockResolvedValue({ goal_id: 'goal-1', status: 'ACTIVE' })
})
afterEach(() => { cleanup(); vi.clearAllMocks() })

describe('light arrangements', () => {
  it('ignores an old quick plan save after switching plans', async () => {
    let finishSave!: (value: api.ManualPlan) => void
    vi.mocked(api.updateManualTask).mockImplementationOnce(() => new Promise(resolve => { finishSave = resolve }))
    vi.mocked(api.getManualPlan).mockImplementation(async id => ({ ...plan, id, title: `安排 ${id}` }))
    render(<MemoryRouter initialEntries={['/quick/plan-1']}><Link to="/quick/plan-2">切换安排</Link><Routes><Route path="/quick/:planId" element={<LightPlanPage />} /></Routes></MemoryRouter>)
    fireEvent.click(await screen.findByRole('button', { name: '完成 练习口语' }))
    fireEvent.click(screen.getByRole('link', { name: '切换安排' }))
    await screen.findByRole('heading', { name: '安排 plan-2' })
    await act(async () => { finishSave({ ...plan, title: '旧安排的迟到写入' }) })
    expect(screen.getByRole('heading', { name: '安排 plan-2' })).toBeInTheDocument()
    expect(screen.queryByRole('heading', { name: '旧安排的迟到写入' })).not.toBeInTheDocument()
  })
  it('closes a task editor when navigating to another quick plan', async () => {
    vi.mocked(api.getManualPlan).mockImplementation(async id => ({ ...plan, id, title: `安排 ${id}` }))
    render(<MemoryRouter initialEntries={['/quick/plan-1']}><Link to="/quick/plan-2">切换安排</Link><Routes><Route path="/quick/:planId" element={<LightPlanPage />} /></Routes></MemoryRouter>)
    fireEvent.click(await screen.findByRole('button', { name: '编辑 练习口语' }))
    expect(screen.getByRole('dialog', { name: '编辑快速安排任务' })).toBeInTheDocument()
    fireEvent.click(screen.getByRole('link', { name: '切换安排' }))
    await screen.findByRole('heading', { name: '安排 plan-2' })
    expect(screen.queryByRole('dialog', { name: '编辑快速安排任务' })).not.toBeInTheDocument()
  })
  it('dismisses a removal confirmation without deleting and preserves recurring deletion scope', async () => {
    mount('/quick/plan-1')
    const remove = await screen.findByRole('button', { name: '移除 练习口语' })
    fireEvent.click(remove)
    const dialog = screen.getByRole('alertdialog')
    expect(api.deleteManualTask).not.toHaveBeenCalled()
    fireEvent.mouseDown(dialog.parentElement!)
    expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument()
    expect(api.deleteManualTask).not.toHaveBeenCalled()
    fireEvent.click(remove)
    fireEvent.click(screen.getByRole('button', { name: '确认移除' }))
    await waitFor(() => expect(api.deleteManualTask).toHaveBeenCalledWith('plan-1', 'task-1', { scope: 'TODAY', override_date: day }))
  })
  it('keeps a failed model draft editable and publishes without another model call', async () => {
    vi.mocked(lightApi.generateLightDraft).mockResolvedValue({ ...draft, revision: 3, status: 'FAILED', error_message: '模型暂时不可用，原输入已保留。' })
    mount('/quick/new')
    fireEvent.change(screen.getByLabelText('想做哪些事？'), { target: { value: '练习口语' } })
    fireEvent.click(screen.getByRole('button', { name: '整理成任务' }))
    await screen.findByText('模型暂时不可用，原输入已保留。')
    fireEvent.change(screen.getByLabelText('任务 1 名称'), { target: { value: '口语练习 20 分钟' } })
    fireEvent.click(screen.getByRole('button', { name: '确认安排' }))
    await waitFor(() => expect(lightApi.applyLightDraft).toHaveBeenCalled())
    expect(lightApi.generateLightDraft).toHaveBeenCalledTimes(1)
    expect(lightApi.saveLightDraft).toHaveBeenCalledWith(expect.objectContaining({ revision: 3 }), expect.objectContaining({ tasks: [expect.objectContaining({ title: '口语练习 20 分钟' })] }))
  })

  it('can recover a generating draft after refresh by saving manually', async () => {
    vi.mocked(lightApi.getLightDraft).mockResolvedValue({ ...draft, status: 'GENERATING', revision: 2 })
    mount('/quick/drafts/draft-1')
    await screen.findByLabelText('任务 1 名称')
    fireEvent.click(screen.getByRole('button', { name: '确认安排' }))
    await waitFor(() => expect(lightApi.saveLightDraft).toHaveBeenCalled())
    expect(lightApi.generateLightDraft).not.toHaveBeenCalled()
    expect(lightApi.applyLightDraft).toHaveBeenCalledWith(expect.objectContaining({ status: 'READY' }))
  })

  it('supports manual creation without any generation dependency', async () => {
    mount('/quick/new')
    fireEvent.change(screen.getByLabelText('想做哪些事？'), { target: { value: '练习口语' } })
    fireEvent.click(screen.getByRole('button', { name: '直接手动安排' }))
    await screen.findByLabelText('任务 1 名称')
    expect(lightApi.generateLightDraft).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: '添加任务' }))
    expect(screen.getByLabelText('任务 2 名称')).toHaveValue('')
    expect(screen.getByRole('button', { name: '确认安排' })).toBeDisabled()
  })

  it('completes or skips only the selected recurring instance', async () => {
    mount('/quick/plan-1')
    fireEvent.click(await screen.findByRole('button', { name: '完成 练习口语' }))
    await waitFor(() => expect(api.updateManualTask).toHaveBeenCalledWith('plan-1', 'task-1', expect.objectContaining({ scope: 'TODAY', scheduled_date: day, status: 'COMPLETED' })))
    await waitFor(() => expect(screen.getByRole('button', { name: '跳过 练习口语' })).toBeEnabled())
    fireEvent.click(screen.getByRole('button', { name: '跳过 练习口语' }))
    await waitFor(() => expect(api.updateManualTask).toHaveBeenCalledWith('plan-1', 'task-1', expect.objectContaining({ scope: 'TODAY', scheduled_date: day, status: 'SKIPPED' })))
  })

  it('edits one instance by default and scopes allocation to this plan', async () => {
    mount('/quick/plan-1')
    fireEvent.click(await screen.findByRole('button', { name: '编辑 练习口语' }))
    const dialog = screen.getByRole('dialog', { name: '编辑快速安排任务' })
    fireEvent.change(within(dialog).getByLabelText('分钟'), { target: { value: '20' } })
    fireEvent.click(within(dialog).getByRole('button', { name: '保存' }))
    await waitFor(() => expect(api.updateManualTask).toHaveBeenCalledWith('plan-1', 'task-1', expect.objectContaining({ scope: 'TODAY', estimate_minutes: 20 })))
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
    fireEvent.click(screen.getByRole('button', { name: '安排具体时间' }))
    expect(screen.getByRole('dialog', { name: '排课' })).toHaveTextContent('plan-1')
  })

  it('opens an upgraded plan at its actual lifecycle stage', async () => {
    vi.mocked(api.getManualPlan).mockResolvedValue({ ...plan, arrangement: { ...plan.arrangement, upgraded_goal_id: 'goal-1' } })
    mount('/quick/plan-1')
    fireEvent.click(await screen.findByRole('button', { name: '打开完整规划' }))
    expect(await screen.findByText('完整规划执行页')).toBeInTheDocument()
  })

  it('redirects a handed-off source unless the user explicitly opens history', async () => {
    vi.mocked(api.getManualPlan).mockResolvedValue({ ...plan, status: 'ARCHIVED', arrangement: { ...plan.arrangement, upgraded_goal_id: 'goal-1', handoff_at: '2026-09-16T00:00:00Z' } })
    mount('/quick/plan-1')
    expect(await screen.findByText('完整规划执行页')).toBeInTheDocument()
  })
})
