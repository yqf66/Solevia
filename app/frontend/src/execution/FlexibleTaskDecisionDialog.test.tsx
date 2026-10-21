import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { applyFlexibleTaskDecision, previewFlexibleTaskDecision, type FlexibleTaskDecisionPreview, type TodayWorkspace } from '../api'
import { FlexibleTaskDecisionDialog, type FlexibleDecisionTarget } from './FlexibleTaskDecisionDialog'

vi.mock('../api', () => ({ applyFlexibleTaskDecision: vi.fn(), previewFlexibleTaskDecision: vi.fn() }))
afterEach(() => { cleanup(); vi.resetAllMocks() })
const target: FlexibleDecisionTarget = { id: 'task', title: '阅读第1次', scheduled_date: '2026-10-10', estimate_minutes: 30, action: 'ARRANGE_TODAY' }
const preview: FlexibleTaskDecisionPreview = {
  id: 'preview', status: 'PREVIEW', action: 'ARRANGE_TODAY', task_id: 'task', title: target.title,
  scheduling_policy: 'FLOATING', source_date: target.scheduled_date, target_date: '2026-10-08',
  planned_minutes: 30, capacity_minutes: 60, over_capacity_minutes: 0, competing_tasks: [],
  shared_available_minutes: 120, proposed_start_local: '19:00', proposed_end_local: '19:30',
  blockers: [], warnings: [], affected_task_ids: ['task'], remaining_commitments: [], can_apply: true, fingerprint: 'state',
}
function props(selected = target) { return { goalId: 'goal', target: selected, today: '2026-10-08', disabled: false, onClose: vi.fn(), onApplied: vi.fn() } }

it('requires reviewed preview and explicit confirmation before arranging; cancelling never applies', async () => {
  vi.mocked(previewFlexibleTaskDecision).mockResolvedValue(preview)
  vi.mocked(applyFlexibleTaskDecision).mockResolvedValue({ today: '2026-10-08' } as TodayWorkspace)
  const callbacks = props()
  render(<FlexibleTaskDecisionDialog {...callbacks} />)
  expect(screen.queryByRole('button', { name: '确认加入今天' })).not.toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', { name: '生成影响预览' }))
  await screen.findByText('共享日程空闲 120 分钟 · 可安排 19:00–19:30')
  expect(applyFlexibleTaskDecision).not.toHaveBeenCalled()
  fireEvent.click(screen.getByRole('button', { name: '取消' }))
  expect(callbacks.onClose).toHaveBeenCalledOnce()
  expect(applyFlexibleTaskDecision).not.toHaveBeenCalled()
  fireEvent.click(screen.getByRole('button', { name: '确认加入今天' }))
  await waitFor(() => expect(callbacks.onApplied).toHaveBeenCalled())
  expect(previewFlexibleTaskDecision).toHaveBeenCalledWith('goal', { action: 'ARRANGE_TODAY', task_id: 'task' })
  expect(applyFlexibleTaskDecision).toHaveBeenCalledWith('goal', 'preview')
})

it('invalidates reschedule preview when inputs change and disables a blocked confirmation', async () => {
  vi.mocked(previewFlexibleTaskDecision).mockResolvedValue({ ...preview, action: 'RESCHEDULE', can_apply: false, blockers: ['需先完成真题'] })
  render(<FlexibleTaskDecisionDialog {...props({ ...target, action: 'RESCHEDULE' })} />)
  expect(screen.getByRole('button', { name: '生成影响预览' })).toBeDisabled()
  fireEvent.change(screen.getByLabelText('改期原因'), { target: { value: '临时会议' } })
  fireEvent.click(screen.getByRole('button', { name: '生成影响预览' }))
  await screen.findByText('需先完成真题')
  expect(screen.getByRole('button', { name: '确认改期' })).toBeDisabled()
  fireEvent.change(screen.getByLabelText('改到日期'), { target: { value: '2026-10-12' } })
  expect(screen.queryByLabelText('任务安排影响预览')).not.toBeInTheDocument()
  expect(screen.queryByRole('button', { name: '确认改期' })).not.toBeInTheDocument()
  expect(applyFlexibleTaskDecision).not.toHaveBeenCalled()
})

it('clears rejected stale preview and allows regenerating without repeated apply', async () => {
  vi.mocked(previewFlexibleTaskDecision).mockResolvedValue(preview)
  vi.mocked(applyFlexibleTaskDecision).mockRejectedValue(new Error('预览已过期'))
  render(<FlexibleTaskDecisionDialog {...props()} />)
  fireEvent.click(screen.getByRole('button', { name: '生成影响预览' }))
  fireEvent.click(await screen.findByRole('button', { name: '确认加入今天' }))
  await screen.findByRole('alert')
  expect(screen.queryByRole('button', { name: '确认加入今天' })).not.toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', { name: '生成影响预览' }))
  await screen.findByRole('button', { name: '确认加入今天' })
  expect(applyFlexibleTaskDecision).toHaveBeenCalledTimes(1)
  expect(previewFlexibleTaskDecision).toHaveBeenCalledTimes(2)
})
