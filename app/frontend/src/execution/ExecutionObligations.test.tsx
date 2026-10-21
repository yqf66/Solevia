import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { ExecutionObligations } from './ExecutionObligations'
import * as api from '../api'

vi.mock('../api', () => ({
  previewCandidateReplacement: vi.fn(),
  applyCandidateReplacement: vi.fn(),
  getCandidatePools: vi.fn(),
  getTriggeredTasks: vi.fn(),
  previewTriggeredTaskActivation: vi.fn(),
  applyTriggeredTaskActivation: vi.fn(),
}))

afterEach(() => { cleanup(); vi.clearAllMocks() })

const workspace = { goal_id: 'goal-1', status: 'ACTIVE' } as api.TodayWorkspace
const task: api.TriggeredTask = {
  window_id: 'window-1', work_item_id: 'item-1', title: '连续失败后复盘',
  description: '检查学习方法', estimate_minutes: 20, condition: '连续两次未完成',
  eligible_from: '2026-01-01', eligible_to: '2027-12-31', status: 'PENDING',
  can_activate: true, blocked_reason: null,
}
const preview: api.TriggeredTaskActivationPreview = {
  id: 'preview-1', status: 'PREVIEW', window_id: 'window-1', work_item_id: 'item-1',
  title: task.title, condition: task.condition, eligible_from: task.eligible_from,
  eligible_to: task.eligible_to, target_date: '2026-10-07',
  blockers: [], warnings: ['任务会加入目标日期现有执行日'], can_apply: true, fingerprint: 'fingerprint',
}

describe('ExecutionObligations', () => {
  it('shows remaining candidates and requires evidence, preview, and confirmation before activation', async () => {
    vi.mocked(api.getCandidatePools).mockResolvedValue([{
      id: 'pool-1', group_key: 'pool', title: '二选一实践', selection_mode: 'SELECT_K',
      minimum_selected: 1, maximum_selected: 1, selected_item_ids: ['choice-1'],
      completed_selected: 0, remaining_required: 1, period_status: 'IN_PROGRESS',
      candidates: [{ id: 'choice-1', item_key: 'choice', title: '实践 A', description: '', estimate_minutes: 30, priority: 1, status: 'ACTIVE' }],
    }])
    vi.mocked(api.getTriggeredTasks).mockResolvedValue([task])
    vi.mocked(api.previewTriggeredTaskActivation).mockResolvedValue(preview)
    vi.mocked(api.applyTriggeredTaskActivation).mockResolvedValue(workspace)
    const onActivated = vi.fn()
    render(<ExecutionObligations goalId="goal-1" workspace={workspace} onActivated={onActivated} />)
    await screen.findByText('完成 0 / 至少 1 项 · 还需 1 项')
    fireEvent.click(screen.getByText('候选与触发任务'))
    fireEvent.click(screen.getByRole('button', { name: '确认已触发' }))
    expect(screen.getByRole('button', { name: '预览安排' })).toBeDisabled()
    fireEvent.change(screen.getByLabelText('触发依据'), { target: { value: '周一和周二均未完成练习' } })
    const targetDateInput = screen.getByLabelText('执行日期') as HTMLInputElement
    const targetDate = targetDateInput.min
    fireEvent.change(targetDateInput, { target: { value: targetDate } })
    fireEvent.click(screen.getByRole('button', { name: '预览安排' }))
    await screen.findByText('安排预览')
    expect(api.applyTriggeredTaskActivation).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: '确认生成执行任务' }))
    await waitFor(() => expect(onActivated).toHaveBeenCalledWith(workspace))
    expect(api.previewTriggeredTaskActivation).toHaveBeenCalledWith('goal-1', {
      window_id: 'window-1', target_date: targetDate, evidence: '周一和周二均未完成练习',
    })
    expect(api.applyTriggeredTaskActivation).toHaveBeenCalledWith('goal-1', 'preview-1')
  })

  it('disables confirmation for a blocked preview and invalidates preview after editing evidence', async () => {
    vi.mocked(api.getCandidatePools).mockResolvedValue([])
    vi.mocked(api.getTriggeredTasks).mockResolvedValue([task])
    vi.mocked(api.previewTriggeredTaskActivation).mockResolvedValue({ ...preview, can_apply: false, blockers: ['需先完成：基础阅读'] })
    render(<ExecutionObligations goalId="goal-1" workspace={workspace} onActivated={vi.fn()} />)
    fireEvent.click(await screen.findByText('候选与触发任务'))
    fireEvent.click(screen.getByRole('button', { name: '确认已触发' }))
    fireEvent.change(screen.getByLabelText('触发依据'), { target: { value: '两次失败' } })
    fireEvent.click(screen.getByRole('button', { name: '预览安排' }))
    await screen.findByText('需先完成：基础阅读')
    expect(screen.getByRole('button', { name: '确认生成执行任务' })).toBeDisabled()
    fireEvent.change(screen.getByLabelText('触发依据'), { target: { value: '新依据' } })
    expect(screen.queryByText('安排预览')).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: '预览安排' })).toBeEnabled()
    expect(api.applyTriggeredTaskActivation).not.toHaveBeenCalled()
  })

  it('previews a candidate replacement and displays capacity tradeoffs', async () => {
    vi.mocked(api.getCandidatePools).mockResolvedValue([{
      id: 'pool-1', group_key: 'pool', title: '二选一实践', selection_mode: 'SELECT_K',
      minimum_selected: 1, maximum_selected: 1, selected_item_ids: ['choice-1'],
      completed_selected: 0, remaining_required: 1, period_status: 'IN_PROGRESS',
      candidates: [
        { id: 'choice-1', item_key: 'choice-a', title: '实践 A', description: '', estimate_minutes: 30, priority: 1, status: 'ACTIVE' },
        { id: 'choice-2', item_key: 'choice-b', title: '实践 B', description: '', estimate_minutes: 45, priority: 2, status: 'DRAFT' },
      ],
    }])
    vi.mocked(api.getTriggeredTasks).mockResolvedValue([])
    vi.mocked(api.previewCandidateReplacement).mockResolvedValue({
      id: 'replacement-preview', status: 'PREVIEW', group_id: 'pool-1', group_title: '二选一实践',
      candidate_item_id: 'choice-2', candidate_title: '实践 B', replaced_item_ids: ['choice-1'],
      target_date: '2026-10-07', planned_minutes: 120, candidate_minutes: 45,
      capacity_minutes: 120, over_capacity_minutes: 45,
      blockers: [], warnings: ['目标日期预计超出建议容量 45 分钟'], can_apply: true, fingerprint: 'replacement-fingerprint',
    })
    vi.mocked(api.applyCandidateReplacement).mockResolvedValue(workspace)
    const onActivated = vi.fn()
    render(<ExecutionObligations goalId="goal-1" workspace={workspace} onActivated={onActivated} />)
    fireEvent.click(await screen.findByText('候选与触发任务'))
    fireEvent.click(screen.getByRole('button', { name: '用“实践 B”替换' }))
    fireEvent.change(screen.getByLabelText('替换原因'), { target: { value: '备用材料更容易获得' } })
    fireEvent.click(screen.getByRole('button', { name: '预览替换' }))
    await screen.findByText('超出建议容量 45 分钟')
    expect(screen.getByRole('button', { name: '确认替换候选' })).toBeEnabled()
    fireEvent.click(screen.getByRole('button', { name: '确认替换候选' }))
    await waitFor(() => expect(onActivated).toHaveBeenCalledWith(workspace))
    expect(api.previewCandidateReplacement).toHaveBeenCalled()
    expect(api.applyCandidateReplacement).toHaveBeenCalledWith('goal-1', 'replacement-preview')
  })

  it('offers supplement after a skipped candidate without replacing history', async () => {
    vi.mocked(api.getCandidatePools).mockResolvedValue([{
      id: 'pool-1', group_key: 'pool', title: '实践候选池', selection_mode: 'SELECT_K',
      minimum_selected: 1, maximum_selected: 1, selected_item_ids: [],
      completed_selected: 0, remaining_required: 1, period_status: 'IN_PROGRESS',
      candidates: [{ id: 'choice-2', item_key: 'choice-b', title: '实践 B',
        description: '', estimate_minutes: 30, priority: 2, status: 'DRAFT' }],
    }])
    vi.mocked(api.getTriggeredTasks).mockResolvedValue([])
    vi.mocked(api.previewCandidateReplacement).mockResolvedValue({
      id: 'supplement-preview', status: 'PREVIEW', group_id: 'pool-1', group_title: '实践候选池',
      candidate_item_id: 'choice-2', candidate_title: '实践 B', replaced_item_ids: [],
      target_date: '2026-10-07', planned_minutes: 0, candidate_minutes: 30,
      capacity_minutes: 60, over_capacity_minutes: 0, blockers: [],
      warnings: ['已有候选及履约记录会保留'], can_apply: true, fingerprint: 'supplement',
    })
    vi.mocked(api.applyCandidateReplacement).mockResolvedValue(workspace)
    const onActivated = vi.fn()
    render(<ExecutionObligations goalId="goal-1" workspace={workspace} onActivated={onActivated} />)
    fireEvent.click(await screen.findByText('候选与触发任务'))
    fireEvent.click(screen.getByRole('button', { name: '补选“实践 B”' }))
    expect(screen.getByRole('heading', { name: '补选候选任务' })).toBeInTheDocument()
    fireEvent.change(screen.getByLabelText('补选原因'), { target: { value: '原候选已跳过' } })
    fireEvent.click(screen.getByRole('button', { name: '预览补选' }))
    await screen.findByText('补选预览')
    expect(api.applyCandidateReplacement).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: '确认补选候选' }))
    await waitFor(() => expect(onActivated).toHaveBeenCalledWith(workspace))
    expect(api.applyCandidateReplacement).toHaveBeenCalledWith('goal-1', 'supplement-preview')
  })
})
