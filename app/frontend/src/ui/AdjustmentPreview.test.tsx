import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { AdjustmentPreview } from './AdjustmentPreview'
import type { PlanAdjustmentPreview } from '../api'

afterEach(cleanup)
const schedule = [
  { title: '阅读20分钟', date: '2035-09-10', minutes: 20, start: null, end: null, state: 'FLEXIBLE' },
  { title: '补回：阅读20分钟', date: '2035-09-10', minutes: 20, start: null, end: null, state: 'FLEXIBLE' },
  { title: '补回：阅读20分钟', date: '2035-09-11', minutes: 20, start: null, end: null, state: 'FLEXIBLE' },
]
const preview = { id: 'preview', schedule, comparison: {
  before: [
    { source_task_id: 'one', title: '阅读20分钟', date: '2035-09-08', minutes: 20, start: null, end: null },
    { source_task_id: 'two', title: '阅读20分钟', date: '2035-09-09', minutes: 20, start: null, end: null },
    { source_task_id: 'three', title: '阅读20分钟', date: '2035-09-10', minutes: 20, start: null, end: null },
  ], after: schedule.map((row, index) => ({ ...row, source_task_id: ['three', 'one', 'two'][index], source_date: ['2035-09-10', '2035-09-08', '2035-09-09'][index], source_minutes: 20, source_start: null, source_end: null, change_kind: index === 0 ? 'UNCHANGED' : 'RECOVERED' })),
  anchor_date: '2035-09-10', original_end_date: '2035-09-12', proposed_end_date: '2035-09-12', backlog_tasks: 2, backlog_minutes: 40,
} } as PlanAdjustmentPreview

it('explains the deadline, original dates, added workload and honest habit history without blank time inputs', () => {
  render(<div className="app-shell"><AdjustmentPreview preview={preview} schedule={schedule} onChange={vi.fn()} onRequestChange={vi.fn()} /></div>)
  expect(screen.getByText('2 项 · 40 分钟')).toBeInTheDocument()
  expect(screen.getByText('保持不变')).toBeInTheDocument()
  expect(screen.getAllByText('增加 20 分钟')).toHaveLength(2)
  expect(screen.getAllByText('2035-09-08')).toHaveLength(2)
  expect(screen.getByText(/过去漏掉的日期仍保留真实记录/)).toBeInTheDocument()
  expect(screen.queryByPlaceholderText('--:--')).not.toBeInTheDocument()
  expect(screen.getByText('变化明细 · 2 项')).toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', { name: /查看全部 3 项/ }))
  expect(screen.getByText('保持原安排')).toBeInTheDocument()
})

it('prepares specific date and daily budget requests without changing or publishing the schedule', () => {
  const onRequestChange = vi.fn(), onChange = vi.fn()
  render(<AdjustmentPreview preview={preview} schedule={schedule} onChange={onChange} onRequestChange={onRequestChange} />)
  fireEvent.click(screen.getByRole('button', { name: '分散到更多天' }))
  expect(onRequestChange).toHaveBeenLastCalledWith(expect.stringContaining('保留原结束日期、全部任务'), { action: 'SPREAD' })
  fireEvent.click(screen.getByRole('button', { name: '指定日期 / 每天用时' }))
  fireEvent.change(screen.getByLabelText('每天总用时上限（分钟）'), { target: { value: '30' } })
  fireEvent.click(screen.getByRole('button', { name: '按这个上限继续讨论' }))
  expect(onRequestChange).toHaveBeenLastCalledWith(expect.stringContaining('最多 30 分钟'), { action: 'DAILY_LIMIT', daily_limit_minutes: 30 })
  fireEvent.change(screen.getByLabelText('选择要换日期的任务'), { target: { value: '2' } })
  fireEvent.change(screen.getByLabelText('希望安排到'), { target: { value: '2035-09-12' } })
  fireEvent.click(screen.getByRole('button', { name: '按这个日期继续讨论' }))
  expect(onRequestChange).toHaveBeenLastCalledWith(expect.stringContaining('原日期 2035-09-09，草案日期 2035-09-11'), { action: 'MOVE_TASK', source_task_id: 'two', target_date: '2035-09-12' })
  expect(onChange).not.toHaveBeenCalled()
})

it('excludes preserved historical unfinished work from the future peak daily load', () => {
  const historical = { title: '未选中的历史任务', date: '2035-09-09', minutes: 120, start: null, end: null, state: 'FLEXIBLE' }
  render(<AdjustmentPreview preview={{ ...preview, schedule: [...schedule, historical], comparison: { ...preview.comparison!, after: [...preview.comparison!.after, { ...historical, source_task_id: 'deferred', source_date: historical.date, source_minutes: 120, source_start: null, source_end: null, change_kind: 'UNCHANGED' }] } }} schedule={[...schedule, historical]} onRequestChange={vi.fn()} />)
  expect(screen.getByText('最高单日用时').parentElement).toHaveTextContent('40 分钟')
})
