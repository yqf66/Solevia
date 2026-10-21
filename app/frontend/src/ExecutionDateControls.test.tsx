import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { ExecutionDayMoveDialog } from './ExecutionDateControls'
import type { ExecutionCalendar } from './api'

afterEach(() => { cleanup(); vi.restoreAllMocks() })

it('shows stage-order warnings without disabling an otherwise valid move', async () => {
  const calendar: ExecutionCalendar = {
    first_date: '2026-09-21',
    last_date: '2026-09-28',
    days: [
      {
        plan_day_id: 'today-day',
        scheduled_date: '2026-09-21',
        code: 'M1-W1-D1',
        primary_goal: '完成基础练习',
        status: 'PLANNED',
        suggested_minutes: 30,
        tasks: [{ id: 'task-1', title: '基础练习', status: 'NOT_STARTED', is_required: true, estimate_minutes: 30 }],
        changes: [],
      },
      {
        plan_day_id: 'future-day',
        scheduled_date: '2026-09-28',
        code: 'M2-W2-D1',
        primary_goal: '开始实践',
        status: 'PLANNED',
        suggested_minutes: 45,
        tasks: [{ id: 'task-2', title: '实践任务', status: 'NOT_STARTED', is_required: true, estimate_minutes: 45 }],
        changes: [],
      },
    ],
  }
  vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response(JSON.stringify({
    preview_id: 'preview-1',
    mode: 'SWAP',
    can_apply: true,
    blockers: [],
    warnings: ['这次调整会让两个阶段交错推进；如果这是有意并行，可以继续确认。'],
    completed_task_count: 0,
    changes: [
      { plan_day_id: 'today-day', code: 'M1-W1-D1', primary_goal: '完成基础练习', from_date: '2026-09-21', to_date: '2026-09-28' },
      { plan_day_id: 'future-day', code: 'M2-W2-D1', primary_goal: '开始实践', from_date: '2026-09-28', to_date: '2026-09-21' },
    ],
  }), { status: 200 }))

  render(<ExecutionDayMoveDialog goalId="goal-1" today="2026-09-21" calendar={calendar} onClose={vi.fn()} onApplied={vi.fn()} />)
  fireEvent.click(screen.getByRole('button', { name: '与今天互换' }))
  fireEvent.change(screen.getByRole('combobox', { name: '选择执行日' }), { target: { value: 'future-day' } })
  fireEvent.click(screen.getByRole('button', { name: '审核并预览' }))

  expect(await screen.findByText('可以调整，请留意顺序变化')).toBeInTheDocument()
  expect(screen.getByText(/两个阶段交错推进/)).toBeInTheDocument()
  expect(screen.getByRole('button', { name: '确认调整执行日' })).toBeEnabled()
})
