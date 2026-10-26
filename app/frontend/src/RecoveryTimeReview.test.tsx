import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { RecoveryTimeReview } from './RecoveryTimeReview'

afterEach(cleanup)
it('shows unallocated work and requires an explicit choice', () => {
  const onAccept = vi.fn()
  render(<RecoveryTimeReview accepted={false} disabled={false} onAccept={onAccept} preview={{ mode: 'BLOCKS', timezone: 'Asia/Shanghai', before: [], after: [{ title: '剩余练习', date: '2035-09-10', start: null, end: null, minutes: 45, state: 'UNALLOCATED' }], unallocated_count: 1, fingerprint: 'preview' }} />)
  expect(screen.getByText('剩余练习')).toBeInTheDocument()
  const checkbox = screen.getByRole('checkbox')
  expect(checkbox).not.toBeChecked()
  fireEvent.click(checkbox)
  expect(onAccept).toHaveBeenCalledWith(true)
})

it('distinguishes diagnostics from an applicable draft and shows source progress', () => {
  render(<RecoveryTimeReview goalId="goal-1" accepted={false} disabled={false} onAccept={vi.fn()} preview={{ mode: 'BLOCKS', timezone: 'Asia/Shanghai', before: [], after: [{ title: '阅读练习', date: '2035-09-10', source_date: '2035-09-07', source_task_id: 'source-1', start: null, end: null, minutes: 45, remaining_minutes: 30, state: 'UNALLOCATED', reason_code: 'CAPACITY_INSUFFICIENT', reason_detail: '可用时间不足以承接这项任务。', allowed_actions: ['ADJUST_AVAILABILITY', 'KEEP_PENDING'] }], diagnostics_only: true, unallocated_count: 1, fingerprint: 'diagnosis' }} />)
  expect(screen.getByText('积压任务与阻塞原因')).toBeInTheDocument()
  expect(screen.getByText(/剩余估时 30 分钟.*原日期 2035-09-07/)).toBeInTheDocument()
  expect(screen.getByText('可用时间不足以承接这项任务。')).toBeInTheDocument()
  expect(screen.queryByRole('link')).not.toBeInTheDocument()
  expect(screen.getByText(/进入恢复讨论并说明可增加的日期和分钟数/)).toBeInTheDocument()
  expect(screen.queryByRole('checkbox')).not.toBeInTheDocument()
})

it('keeps voluntary flexible scheduling separate from failure', () => {
  render(<RecoveryTimeReview accepted={false} disabled={false} onAccept={vi.fn()} preview={{ mode: 'FLEXIBLE', timezone: 'Asia/Shanghai', before: [], after: [{ title: '自由练习', date: '2035-09-10', start: null, end: null, minutes: 30, state: 'FLEXIBLE' }], unallocated_count: 0, fingerprint: 'flexible' }} />)
  expect(screen.getByText(/自行决定具体时间/)).toBeInTheDocument()
  expect(screen.queryByRole('checkbox')).not.toBeInTheDocument()
  expect(screen.queryByText(/暂时无法分配/)).not.toBeInTheDocument()
})
