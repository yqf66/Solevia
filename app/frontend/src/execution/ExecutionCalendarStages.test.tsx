import { act, cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import type { ExecutionCalendar } from '../api'
import { ExecutionCalendarWidget } from './ExecutionTools'

afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.useRealTimers() })

it('distinguishes calendar position from mixed and carried-over task stages', async () => {
  vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response(JSON.stringify({ days: [] })))
  const task = (id: string, stage: number) => ({ id, title: `任务${id}`, status: 'NOT_STARTED',
    is_required: true, estimate_minutes: 30, origin_stage_order: stage })
  const calendar: ExecutionCalendar = { first_date: '2026-10-20', last_date: '2026-10-21', days: [
    { plan_day_id: 'a', scheduled_date: '2026-10-20', code: 'M3-W2-D3',
      task_stage_orders: [2, 3], primary_goal: '阅读练习', status: 'PLANNED', suggested_minutes: 60,
      tasks: [task('a', 2), task('b', 3)], changes: [] },
    { plan_day_id: 'b', scheduled_date: '2026-10-21', code: 'M4-W3-D1',
      task_stage_orders: [2], primary_goal: '补回练习', status: 'PLANNED', suggested_minutes: 30,
      tasks: [task('c', 2)], changes: [] },
  ] }
  render(<ExecutionCalendarWidget calendar={calendar} selectedDate="2026-10-20" onSelectDate={vi.fn()} />)
  const mixed = screen.getByRole('button', { name: /2026-10-20.*当天任务来自 M2、M3/ })
  fireEvent.focus(mixed)
  const mixedPreview = screen.getByRole('tooltip')
  expect(within(mixedPreview).getByText(/日历位置：M3-W2-D3/)).toBeInTheDocument()
  expect(within(mixedPreview).getByText(/原属 M2/)).toBeInTheDocument()
  expect(within(mixedPreview).getByText(/原属 M3/)).toBeInTheDocument()
  const carried = screen.getByRole('button', { name: /2026-10-21.*当天任务来自 M2/ })
  fireEvent.focus(carried)
  expect(within(screen.getByRole('tooltip')).getByText(/日历位置：M4-W3-D1/)).toBeInTheDocument()
  expect(within(screen.getByRole('tooltip')).getByText('当天任务来自 M2')).toBeInTheDocument()
  expect(screen.getByText(/M 编号表示日历中的阶段位置/)).toBeInTheDocument()
})

it('keeps a busy day open when the pointer moves into its card and dismisses with Escape', () => {
  vi.useFakeTimers()
  vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response(JSON.stringify({ days: [] })))
  const calendar: ExecutionCalendar = { first_date: '2026-10-20', last_date: '2026-10-20', days: [
    { plan_day_id: 'busy', scheduled_date: '2026-10-20', code: 'M1-W1-D1', primary_goal: '多项安排',
      status: 'PLANNED', suggested_minutes: 120, changes: [],
      tasks: Array.from({ length: 12 }, (_, index) => ({ id: String(index), title: `可核验任务 ${index + 1}`,
        status: 'NOT_STARTED', is_required: true, estimate_minutes: 10 })) },
  ] }
  const onSelect = vi.fn()
  render(<ExecutionCalendarWidget calendar={calendar} selectedDate="2026-10-20" onSelectDate={onSelect} />)
  const day = screen.getByRole('button', { name: /2026-10-20/ })
  fireEvent.mouseEnter(day)
  const card = screen.getByRole('tooltip')
  expect(within(card).getAllByRole('listitem')).toHaveLength(12)
  expect(day).toHaveAttribute('aria-describedby', card.id)
  fireEvent.mouseLeave(day)
  act(() => vi.advanceTimersByTime(100))
  fireEvent.mouseEnter(card)
  act(() => vi.advanceTimersByTime(300))
  expect(card).toBeInTheDocument()
  fireEvent.click(card)
  expect(onSelect).not.toHaveBeenCalled()
  fireEvent.keyDown(document, { key: 'Escape' })
  expect(screen.queryByRole('tooltip')).not.toBeInTheDocument()
  fireEvent.focus(day)
  fireEvent.pointerDown(document.body)
  expect(screen.queryByRole('tooltip')).not.toBeInTheDocument()
})
