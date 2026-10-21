import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { MemoryRouter, Route, Routes, Link } from 'react-router-dom'
import { PlanReviewPage } from './planner/PlanReviewPage'

afterEach(() => { cleanup(); vi.restoreAllMocks() })
const response = (data: unknown) => new Response(JSON.stringify(data), { status: 200 })
function workspace(id: string, pending = false) {
  return { goal_id: id, goal_title: `探索${id}`, status: 'ACTIVE', requirement_snapshot: {},
    generation_phase: 'COMPLETE', generated_weeks: pending ? 2 : 1, total_weeks: 3,
    messages: [], revision_history: [], pending_adjustment: null, recovery_review: null,
    planning_mode: 'EXPLORATORY', schedule_type: 'RHYTHM', exploration_windows: [],
    can_extend_exploration: !pending,
    pending_exploration_window: pending ? { id: `window-${id}`, start_week: 2, end_week: 2 } : null,
    plan: { id: `plan-${id}`, status: pending ? 'DRAFT' : 'CONFIRMED', version: 1, revision: pending ? 2 : 1,
      summary: `探索${id}的安排`, planning_mode: 'EXPLORATORY', schedule_type: 'RHYTHM',
      total_weeks: 3, next_unplanned_week: pending ? 3 : 2, weekly_execution_days: 1, weekly_minutes: 30,
      milestones: [], days: [], temporal_segments: [], buffer_strategy: [], assumptions: [], open_questions: [],
      change_summary: pending ? ['新增下一阶段任务'] : [] } }
}
function show() {
  render(<MemoryRouter initialEntries={['/goals/a/plan']}>
    <Link to="/goals/b/plan">切换到探索b</Link><Link to="/goals/a/plan">回到探索a</Link>
    <Routes><Route path="/goals/:goalId/plan" element={<PlanReviewPage />} /></Routes>
  </MemoryRouter>)
}

it('restores a pending exploration window as an unpublished draft', async () => {
  vi.spyOn(globalThis, 'fetch').mockImplementation(input => Promise.resolve(response(
    String(input) === '/api/goals/a/plan' ? workspace('a', true) : [])))
  show()
  expect(await screen.findByRole('heading', { name: '预览下一阶段安排' })).toBeInTheDocument()
  expect(screen.getByText(/下一阶段草案，尚未接入执行/)).toBeInTheDocument()
  expect(screen.getByRole('button', { name: '确认并接入执行' })).toBeInTheDocument()
  expect(screen.queryByText('计划 v1.2 已生效')).not.toBeInTheDocument()
  expect(screen.queryByText('接下来的安排已经更新')).not.toBeInTheDocument()
  expect(screen.getByText('这是待确认的下一阶段草案')).toBeInTheDocument()
  expect(screen.queryByText('这是你正在执行的计划')).not.toBeInTheDocument()
})

it('keeps the completed route readable without returning to initial planning', async () => {
  const completed = workspace('a')
  completed.status = 'COMPLETED'
  completed.can_extend_exploration = false
  completed.plan.next_unplanned_week = 4
  vi.spyOn(globalThis, 'fetch').mockImplementation(input => Promise.resolve(response(
    String(input) === '/api/goals/a/plan' ? completed : [])))
  show()
  expect(await screen.findByRole('heading', { name: '回看已完成的安排' })).toBeInTheDocument()
  expect(screen.getByText('本轮规划已完成')).toBeInTheDocument()
  expect(screen.getByRole('button', { name: '写反馈' })).toBeDisabled()
  expect(screen.queryByRole('button', { name: '展开下一成果阶段' })).not.toBeInTheDocument()
})

it('keeps late exploration generation out of another goal and restores the saved draft on return', async () => {
  let finish!: (value: Response) => void
  const generation = new Promise<Response>(resolve => { finish = resolve })
  let pending = false
  vi.spyOn(globalThis, 'fetch').mockImplementation(input => {
    const path = String(input)
    if (path === '/api/goals/a/plan/exploration/draft') return generation
    if (path === '/api/goals/a/plan') return Promise.resolve(response(workspace('a', pending)))
    if (path === '/api/goals/b/plan') return Promise.resolve(response(workspace('b')))
    return Promise.resolve(response([]))
  })
  show()
  fireEvent.click(await screen.findByRole('button', { name: '展开下一成果阶段' }))
  expect(screen.getByRole('button', { name: '正在结合执行效果生成…' })).toBeDisabled()
  fireEvent.click(screen.getByText('切换到探索b'))
  expect(await screen.findByText('探索b的安排')).toBeInTheDocument()
  expect(screen.getByRole('button', { name: '展开下一成果阶段' })).toBeEnabled()
  pending = true
  await act(async () => { finish(response(workspace('a', true))) })
  expect(screen.getByText('探索b的安排')).toBeInTheDocument()
  expect(screen.queryByText(/第 2-2 周草案待确认/)).not.toBeInTheDocument()
  fireEvent.click(screen.getByText('回到探索a'))
  expect(await screen.findByRole('button', { name: '确认并接入执行' })).toBeInTheDocument()
})

it('shows server-side automatic repair while the original exploration request continues', async () => {
  let finish!: (value: Response) => void
  const generation = new Promise<Response>(resolve => { finish = resolve })
  let generating = false
  vi.spyOn(globalThis, 'fetch').mockImplementation(input => {
    const path = String(input)
    if (path.endsWith('/exploration/draft')) { generating = true; return generation }
    if (path === '/api/goals/a/plan') return Promise.resolve(response({
      ...workspace('a'),
      generation_repair: generating ? { week_index: 2, attempt: 2, maximum_attempts: 3 } : null,
    }))
    return Promise.resolve(response([]))
  })
  show()
  fireEvent.click(await screen.findByRole('button', { name: '展开下一成果阶段' }))
  expect(await screen.findByText(/正在自动调整第 2 周/, {}, { timeout: 4000 })).toHaveAttribute('role', 'status')
  expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  await act(async () => { finish(response(workspace('a', true))) })
  expect(await screen.findByRole('button', { name: '确认并接入执行' })).toBeEnabled()
})
