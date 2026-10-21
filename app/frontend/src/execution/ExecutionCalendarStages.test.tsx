import { cleanup, render, screen, within } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import type { ExecutionCalendar } from '../api'
import { ExecutionCalendarWidget } from './ExecutionTools'

afterEach(() => { cleanup(); vi.restoreAllMocks() })

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
  expect(within(mixed).getByText(/日历位置：M3-W2-D3/)).toBeInTheDocument()
  expect(within(mixed).getByText(/原属 M2/)).toBeInTheDocument()
  expect(within(mixed).getByText(/原属 M3/)).toBeInTheDocument()
  const carried = screen.getByRole('button', { name: /2026-10-21.*当天任务来自 M2/ })
  expect(within(carried).getByText(/日历位置：M4-W3-D1/)).toBeInTheDocument()
  expect(within(carried).getByText('当天任务来自 M2')).toBeInTheDocument()
  expect(screen.getByText(/M 编号表示日历中的阶段位置/)).toBeInTheDocument()
})
