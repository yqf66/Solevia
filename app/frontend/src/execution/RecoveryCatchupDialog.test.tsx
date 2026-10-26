import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { applyRecoveryCatchup, applyRecoverySplit, previewRecoverySplit, previewRecoveryCatchup, type RecoveryCatchupPreview } from '../api'
import { RecoveryCatchupDialog } from './RecoveryControls'

vi.mock('../api', async importOriginal => ({
  ...await importOriginal<typeof import('../api')>(),
  previewRecoveryCatchup: vi.fn(), applyRecoveryCatchup: vi.fn(),
  previewRecoverySplit: vi.fn(), applyRecoverySplit: vi.fn(),
}))
afterEach(cleanup)
beforeEach(() => vi.resetAllMocks())

function preview(selected = ['first', 'second'], feasible = true): RecoveryCatchupPreview {
  return {
    id: `preview-${selected.join('-')}`, status: 'PREVIEW', mode: 'KEEP_DEADLINE',
    source_plan_version_id: 'plan', result_plan_version_id: null,
    anchor_date: '2035-09-10', original_end_date: '2035-09-20', missed_dates: ['2035-09-07'],
    backlog_tasks: selected.length, backlog_minutes: selected.length * 10,
    remaining_execution_days: 4, available_non_execution_days: 2,
    required_extra_minutes_per_day: 5, include_non_execution_days: false,
    programmatic_feasible: feasible, model_required: !feasible, backlog_cleared_date: null,
    allocation: [], reason_codes: [],
    recovery_scope: {
      source_plan_version_id: 'plan', fingerprint: 'scope', selected_task_ids: selected,
      deferred_task_ids: ['first', 'second'].filter(id => !selected.includes(id)),
      selected_minutes: selected.length * 10, deferred_minutes: (2 - selected.length) * 10,
      items: ['first', 'second'].map(id => ({ source_task_id: id, lineage_task_id: id,
        title: id === 'first' ? '整理材料' : '阅读论文', source_date: '2035-09-07',
        minutes: 10, remaining_minutes: 10, selected: selected.includes(id),
        prerequisite_task_ids: id === 'second' ? ['first'] : [],
        can_split: id === 'first', split_reason: id === 'first' ? null : '存在前置任务关系',
      })),
    },
  }
}

function mount(onRequestAI = vi.fn(), onClose = vi.fn()) {
  render(<RecoveryCatchupDialog goalId="goal" onClose={onClose} onApplied={vi.fn()} onRequestAI={onRequestAI} />)
  return { onClose }
}

it('requires a new scoped preview after editing and submits only the selected source IDs', async () => {
  vi.mocked(previewRecoveryCatchup).mockResolvedValueOnce(preview()).mockResolvedValueOnce(preview(['first']))
  vi.mocked(applyRecoveryCatchup).mockResolvedValue(preview(['first']))
  mount()
  await screen.findByText('本次所选范围可直接调度')
  fireEvent.click(screen.getByLabelText(/阅读论文/))
  expect(screen.getByRole('button', { name: '应用这份追赶安排' })).toBeDisabled()
  expect(screen.queryByText('本次所选范围可直接调度')).not.toBeInTheDocument()
  expect(screen.getByText(/保留待处理 1 项/)).toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', { name: '按所选范围重新预览' }))
  await waitFor(() => expect(previewRecoveryCatchup).toHaveBeenLastCalledWith('goal', undefined, false, {
    source_plan_version_id: 'plan', selected_task_ids: ['first'],
  }))
  await waitFor(() => expect(screen.getByRole('button', { name: '应用这份追赶安排' })).toBeEnabled())
  fireEvent.click(screen.getByRole('button', { name: '应用这份追赶安排' }))
  await waitFor(() => expect(applyRecoveryCatchup).toHaveBeenCalledWith('goal', 'preview-first', false))
})

it('collects a bounded extra-time offer inside the dialog without navigating to preparation', async () => {
  const sample = { ...preview(undefined, false), original_end_date: '2035-09-20', time_preview: {
    mode: 'FLEXIBLE' as const, timezone: 'Asia/Shanghai', before: [], after: [{ title: '阅读论文', date: '2035-09-10', minutes: 10,
      start: null, end: null, state: 'UNALLOCATED' as const, allowed_actions: ['ADJUST_AVAILABILITY' as const] }],
    diagnostics_only: true, unallocated_count: 1, fingerprint: 'time',
  } }
  vi.mocked(previewRecoveryCatchup).mockResolvedValue(sample)
  const onRequestAI = vi.fn()
  mount(onRequestAI)
  fireEvent.click(await screen.findByRole('button', { name: '补充我能多投入的时间' }))
  expect(screen.queryByRole('link', { name: '查看并补充可用时间' })).not.toBeInTheDocument()
  fireEvent.change(screen.getByLabelText('从哪天起'), { target: { value: '2035-09-10' } })
  fireEvent.change(screen.getByLabelText('每个执行日额外分钟'), { target: { value: '15' } })
  fireEvent.click(screen.getByRole('button', { name: '按这个投入讨论补回方案' }))
  await waitFor(() => expect(onRequestAI).toHaveBeenCalledWith(sample, expect.stringContaining('最多额外投入 15 分钟'), {
    from_date: '2035-09-10', extra_minutes_per_execution_day: 15,
  }))
  expect(applyRecoveryCatchup).not.toHaveBeenCalled()
})

it('empty selection cannot preview or apply and cancel does not publish', async () => {
  vi.mocked(previewRecoveryCatchup).mockResolvedValue(preview())
  const { onClose } = mount()
  await screen.findByText('本次恢复范围')
  fireEvent.click(screen.getByLabelText('全部纳入本次恢复'))
  expect(screen.getByRole('button', { name: '按所选范围重新预览' })).toBeDisabled()
  expect(screen.getByRole('button', { name: '应用这份追赶安排' })).toBeDisabled()
  fireEvent.click(screen.getByRole('button', { name: '返回调整方式' }))
  expect(onClose).toHaveBeenCalledOnce()
  expect(applyRecoveryCatchup).not.toHaveBeenCalled()
})

it('failed repreview retains selection and rejects confirmation until a successful retry', async () => {
  vi.mocked(previewRecoveryCatchup).mockResolvedValueOnce(preview())
    .mockRejectedValueOnce(new Error('范围发生变化')).mockResolvedValueOnce(preview(['second'], false))
  mount()
  await screen.findByText('本次恢复范围')
  fireEvent.click(screen.getByLabelText(/整理材料/))
  expect(screen.getByText(/前置积压尚未纳入/)).toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', { name: '按所选范围重新预览' }))
  await screen.findByRole('alert')
  expect(screen.getByLabelText(/整理材料/)).not.toBeChecked()
  expect(screen.getByLabelText(/阅读论文/)).toBeChecked()
  expect(screen.getByRole('button', { name: '应用这份追赶安排' })).toBeDisabled()
  fireEvent.click(screen.getByRole('button', { name: '按当前选择重试' }))
  await screen.findByText('所选内容仍需进一步处理')
  expect(screen.getByRole('button', { name: '讨论所选任务的恢复方案' })).toBeEnabled()
  expect(applyRecoveryCatchup).not.toHaveBeenCalled()
})

it('AI failure keeps the scope selector available for narrowing', async () => {
  vi.mocked(previewRecoveryCatchup).mockResolvedValue(preview(undefined, false))
  const onRequestAI = vi.fn().mockRejectedValue(new Error('服务暂不可用'))
  mount(onRequestAI)
  fireEvent.click(await screen.findByRole('button', { name: '讨论所选任务的恢复方案' }))
  await screen.findByText('服务暂不可用')
  expect(screen.getByLabelText(/整理材料/)).toBeEnabled()
  fireEvent.click(screen.getByLabelText(/阅读论文/))
  expect(screen.getByText(/恢复范围已修改/)).toBeInTheDocument()
  expect(onRequestAI).toHaveBeenCalledOnce()
})

it('explains the pending recovery discussion and prevents duplicate requests until it finishes', async () => {
  vi.mocked(previewRecoveryCatchup).mockResolvedValue(preview(undefined, false))
  let rejectRequest: (error: Error) => void = () => {}
  const onRequestAI = vi.fn(() => new Promise<void>((_resolve, reject) => { rejectRequest = reject }))
  mount(onRequestAI)
  fireEvent.click(await screen.findByRole('button', { name: '讨论所选任务的恢复方案' }))
  const pendingButton = screen.getByRole('button', { name: '正在整理恢复建议…' })
  expect(pendingButton).toBeDisabled()
  expect(await screen.findByRole('status')).toHaveTextContent('完成后会打开恢复讨论')
  expect(screen.getByText(/正在分析所选任务并生成恢复建议 · 0 秒/)).toBeInTheDocument()
  expect(screen.getByRole('button', { name: '关闭追赶方案' })).toBeDisabled()
  fireEvent.click(pendingButton)
  expect(onRequestAI).toHaveBeenCalledOnce()
  await act(async () => rejectRequest(new Error('恢复建议生成超时')))
  expect(await screen.findByRole('button', { name: '讨论所选任务的恢复方案' })).toBeEnabled()
  expect(screen.getByText('处理未完成')).toBeInTheDocument()
  expect(screen.getByLabelText(/整理材料/)).toBeChecked()
})

it('marks selected recovery rows visibly and exposes splitting for eligible tasks', async () => {
  vi.mocked(previewRecoveryCatchup).mockResolvedValue(preview())
  mount()
  const selectedInput = await screen.findByLabelText(/整理材料/)
  const selectedRow = selectedInput.closest('label')
  expect(selectedRow).not.toBeNull()
  expect(selectedRow).toHaveClass('selected')
  expect(screen.getByRole('button', { name: '拆分' })).toBeInTheDocument()
})

it('previews a split before applying and refreshes exactly the new selected IDs', async () => {
  const refreshed = { ...preview(['part-1', 'part-2']), source_plan_version_id: 'new-plan' }
  vi.mocked(previewRecoveryCatchup).mockResolvedValueOnce(preview()).mockResolvedValueOnce(refreshed)
  const split = { id: 'split-id', status: 'PREVIEW' as const, source_plan_version_id: 'plan', result_plan_version_id: null, source_task_id: 'first', title: '整理材料', original_minutes: 10, part_minutes: [5, 5], result_selected_task_ids: [] }
  vi.mocked(previewRecoverySplit).mockResolvedValue(split)
  vi.mocked(applyRecoverySplit).mockResolvedValue({ ...split, status: 'APPLIED', result_plan_version_id: 'new-plan', result_selected_task_ids: ['part-1', 'part-2', 'second'] })
  mount()
  fireEvent.click(await screen.findByRole('button', { name: '拆分' }))
  expect(screen.getByLabelText('各分片时长')).toHaveValue('5, 5')
  expect(applyRecoverySplit).not.toHaveBeenCalled()
  fireEvent.click(screen.getByRole('button', { name: '预览拆分' }))
  await screen.findByRole('dialog', { name: '确认任务拆分' })
  expect(previewRecoverySplit).toHaveBeenCalledWith('goal', { preview_id: 'preview-first-second', source_task_id: 'first', part_minutes: [5, 5] })
  fireEvent.click(screen.getByRole('button', { name: '确认拆分' }))
  await waitFor(() => expect(previewRecoveryCatchup).toHaveBeenLastCalledWith('goal', undefined, false, { source_plan_version_id: 'new-plan', selected_task_ids: ['part-1', 'part-2', 'second'] }))
  await waitFor(() => expect(screen.queryByRole('dialog', { name: '确认任务拆分' })).not.toBeInTheDocument())
})

it('apply conflict plus failed retry retains the original selection', async () => {
  vi.mocked(previewRecoveryCatchup).mockResolvedValueOnce(preview(['first']))
    .mockRejectedValueOnce(new Error('读取失败'))
  vi.mocked(applyRecoveryCatchup).mockRejectedValue(new Error('预览已过期'))
  mount()
  fireEvent.click(await screen.findByRole('button', { name: '应用这份追赶安排' }))
  await screen.findByText('预览已过期')
  await waitFor(() => expect(screen.getByLabelText(/整理材料/)).toBeEnabled())
  expect(screen.getByLabelText(/阅读论文/)).not.toBeChecked()
  expect(screen.getByRole('button', { name: '应用这份追赶安排' })).toBeDisabled()
  expect(screen.getByRole('button', { name: '重新读取全部积压' })).toBeEnabled()
})
