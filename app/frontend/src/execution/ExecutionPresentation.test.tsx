import { cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import type { PlanTask, TodayWorkspace } from '../api'
import { ExecutionTaskCard } from './ExecutionTaskCard'
import { ExecutionNoticeBar } from './ExecutionNoticeBar'
import { TodayBrief } from './TodayBrief'

afterEach(cleanup)

it('keeps a long daily objective folded without removing its complete text', () => {
  const goal = '完成阅读与错题复盘，并把今天发现的知识点记录下来。'.repeat(5)
  render(<TodayBrief goal={goal} minutes={35} date="10月21日" milestone="诊断与基础建立" />)
  expect(screen.getByText(goal)).toBeInTheDocument()
  const disclosure = screen.getByLabelText('展开或收起今日重点').closest('details')!
  expect(disclosure).not.toHaveAttribute('open')
  expect(screen.getByText('35')).toBeVisible()
})
const task: PlanTask = { id: 'reading', order: 1, title: '完成阅读专项练习', description: '记录正确率与错题原因。', estimate_minutes: 35, priority: 1, task_kind: 'PRACTICE', is_required: true, evidence: ['一份错题记录'], depends_on: ['previous'], scheduling_policy: 'CADENCE', status: 'NOT_STARTED' }
function props(overrides: Partial<PlanTask> = {}) {
  return { task: { ...task, ...overrides }, position: 2, tone: 1, taskTitles: new Map([['previous', '确定本周练习题型']]), viewingToday: true, canEditToday: true, busyTaskId: '', onSchedule: vi.fn(), onUpdate: vi.fn(), onProgress: vi.fn(), onReschedule: vi.fn(), onFeedback: vi.fn() }
}

it('keeps completion and partial progress separate and exposes secondary actions on demand', () => {
  const callbacks = props()
  render(<ExecutionTaskCard {...callbacks} />)
  expect(screen.getByText('确定本周练习题型')).toBeVisible()
  expect(screen.getByText('今天不做')).not.toBeVisible()
  fireEvent.click(screen.getByRole('button', { name: '标记完成' }))
  expect(callbacks.onUpdate).toHaveBeenCalledWith('reading', 'COMPLETE')
  fireEvent.click(screen.getByRole('button', { name: '记录部分完成' }))
  expect(callbacks.onProgress).toHaveBeenCalledWith(expect.objectContaining({ id: 'reading', action: 'PARTIAL' }))
  const more = screen.getByText('更多安排').closest('details')!
  more.setAttribute('open', '')
  fireEvent.click(screen.getByRole('button', { name: '今天不做' }))
  expect(callbacks.onProgress).toHaveBeenCalledWith(expect.objectContaining({ action: 'DEFER' }))
  expect(more).not.toHaveAttribute('open')
  more.setAttribute('open', '')
  fireEvent.pointerDown(document.body)
  expect(more).not.toHaveAttribute('open')
  more.setAttribute('open', '')
  fireEvent.keyDown(document, { key: 'Escape' })
  expect(more).not.toHaveAttribute('open')
  expect(screen.getByText('更多安排')).toHaveFocus()
})

it('preserves blocked, completed, historical and exact-day restrictions', () => {
  const view = render(<ExecutionTaskCard {...props({ blocked_reason: '前置任务未完成' })} />)
  expect(screen.getByRole('button', { name: '开始' })).toBeDisabled()
  expect(screen.getByRole('button', { name: '标记完成' })).toBeDisabled()
  view.rerender(<ExecutionTaskCard {...props({ status: 'COMPLETED' })} />)
  expect(screen.queryByRole('button', { name: '标记完成' })).not.toBeInTheDocument()
  expect(screen.queryByText('更多安排')).not.toBeInTheDocument()
  view.rerender(<ExecutionTaskCard {...props()} viewingToday={false} canEditToday={false} />)
  expect(screen.getByText(/日志回看/)).toBeVisible()
  expect(screen.queryByRole('button')).not.toBeInTheDocument()
  view.rerender(<ExecutionTaskCard {...props({ scheduling_policy: 'EXACT_DAY' })} />)
  expect(screen.queryByText('今天不做')).not.toBeInTheDocument()
  expect(screen.queryByText('改到某日')).not.toBeInTheDocument()
})

it('shows accurate summary counts while keeping reminders collapsed and all controls available', () => {
  const workspace: Pick<TodayWorkspace, 'commitment_progress' | 'commitment_alerts'> = { commitment_progress: [{ label: '阅读练习', cadence: 'PER_WEEK', risk: 'DUE_SOON', target_occurrences: 3, completed_occurrences: 1, remaining_occurrences: 2, completed_minutes: 35, partial_minutes: 10, remaining_minutes: 60, period_start: '2026-10-19', period_end: '2026-10-25', source_text: '每周练习三次' }], commitment_alerts: [{ event_id: 'alert', label: '阅读练习', title: '本周阅读练习待完成', message: '本周还需完成两次。', severity: 'WARNING', risk: 'DUE_SOON', status: 'OPEN', action: 'REVIEW_PLAN', period_end: '2026-10-25', remaining_occurrences: 2 }] }
  const onAcknowledge = vi.fn()
  render(<ExecutionNoticeBar workspace={workspace} viewingToday reminderBusyLabel="" reminderEventBusyId="" onRecovery={vi.fn()} onDiscuss={vi.fn()} onAcknowledge={onAcknowledge} onStopReminder={vi.fn()} />)
  expect(screen.getByText('周期进度 1/3 次')).toBeVisible()
  expect(screen.getByText('1 项提醒')).toBeVisible()
  const bar = screen.getByText('提醒与进度').closest('details')!
  expect(bar).not.toHaveAttribute('open')
  bar.setAttribute('open', '')
  fireEvent.click(within(bar).getByRole('button', { name: '标记已读' }))
  expect(onAcknowledge).toHaveBeenCalledWith('alert', 'READ')
  fireEvent.click(screen.getByRole('button', { name: '展开本周期完成要求' }))
  expect(screen.getByText('阅读练习')).toBeVisible()
})
