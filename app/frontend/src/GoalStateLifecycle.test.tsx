import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import App from './App'
import { request } from './api'
import { StageTransition } from './ui/PlannerStage'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { TodayPage } from './execution/TodayPage'

afterEach(() => {
  cleanup(); vi.restoreAllMocks(); sessionStorage.clear()
  window.history.pushState({}, '', '/')
})

const response = (data: unknown, status = 200) => new Response(JSON.stringify(data), { status })
const goal = (id: string, status = 'PLAN_REVIEW') => ({ id, title: `规划${id}`, status,
  timezone: Intl.DateTimeFormat().resolvedOptions().timeZone, pause_until: null })
const workspace = (id: string) => ({ goal_id: id, goal_title: `规划${id}`, status: 'PLAN_REVIEW',
  requirement_snapshot: {}, generation_phase: 'COMPLETE', generated_weeks: 1, total_weeks: 1,
  messages: [], revision_history: [], pending_adjustment: null, recovery_review: null,
  plan: { id: `plan-${id}`, status: 'DRAFT', version: 1, revision: 1, summary: `${id}的已保存安排`,
    schedule_type: 'CALENDAR', planning_mode: 'COMMITTED', total_weeks: 1,
    weekly_execution_days: 3, weekly_minutes: 90, milestones: [], days: [],
    buffer_strategy: [], assumptions: [], open_questions: [], change_summary: [] } })

it('never presents execution loading as preparation or calendar regeneration', () => {
  render(<StageTransition executing current={3} title="正在读取任务" detail="读取已有安排" checkpoints={[]} />)
  expect(screen.getByText('今日执行')).toBeInTheDocument()
  expect(screen.queryByText('准备开始')).not.toBeInTheDocument()
  expect(screen.queryByText('建立执行日历')).not.toBeInTheDocument()
})

it('reads a saved plan without announcing a new generation while the API is pending', async () => {
  let finishRead!: (value: Response) => void
  const read = new Promise<Response>(resolve => { finishRead = resolve })
  window.history.pushState({}, '', '/goals/a/plan')
  const fetchMock = vi.spyOn(globalThis, 'fetch').mockImplementation(input => {
    const path = String(input)
    if (path === '/api/goals/a/plan') return read
    if (path === '/api/goals' || path === '/api/goals/status-check') return Promise.resolve(response([goal('a', 'ACTIVE')]))
    return Promise.resolve(response([]))
  })
  render(<App />)
  expect(await screen.findByRole('heading', { name: '正在读取已保存的规划' })).toBeInTheDocument()
  expect(screen.queryByRole('heading', { name: '正在安排时间' })).not.toBeInTheDocument()
  expect(screen.queryByLabelText('创建规划进度')).not.toBeInTheDocument()
  await act(async () => { finishRead(response({ ...workspace('a'), status: 'ACTIVE',
    plan: { ...workspace('a').plan, status: 'CONFIRMED', revision: 2,
      total_weeks: 5, summary: '原先只有四周的规划说明' } })) })
  expect(await screen.findByText('计划 v1.2 已生效')).toBeInTheDocument()
  expect(screen.getByRole('heading', { name: '当前执行安排 · 共 5 周' })).toBeInTheDocument()
  const originalSummary = screen.getByText('原先只有四周的规划说明').closest('details')
  expect(originalSummary).not.toHaveAttribute('open')
  expect(screen.getByText('查看生成时的规划说明')).toBeInTheDocument()
  expect(screen.getByText('每周执行日')).toBeInTheDocument()
  expect(screen.queryByText(/个相对周，不绑定具体日期/)).not.toBeInTheDocument()
  expect(fetchMock.mock.calls.some(([input]) => /\/(draft|generation)$/.test(String(input)))).toBe(false)
})

it('notifies successful goal mutations but not reads, failed writes or status-check', async () => {
  const changed = vi.fn()
  window.addEventListener('planner:goals-changed', changed)
  try {
    vi.spyOn(globalThis, 'fetch').mockResolvedValueOnce(response({ status: 'ACTIVE' }))
      .mockResolvedValueOnce(response([])).mockResolvedValueOnce(response([]))
      .mockResolvedValueOnce(response({ detail: 'changed' }, 409))
    await request('/api/goals/a/plan/adjustment/apply', { method: 'POST' })
    await request('/api/goals')
    await request('/api/goals/status-check', { method: 'POST' })
    await expect(request('/api/goals/a/plan/adjustment/apply', { method: 'POST' })).rejects.toThrow()
    expect(changed).toHaveBeenCalledOnce()
  } finally { window.removeEventListener('planner:goals-changed', changed) }
})

it('updates the sidebar after publication and ignores a late startup status list', async () => {
  let finishStartup!: (value: Response) => void
  const startup = new Promise<Response>(resolve => { finishStartup = resolve })
  vi.spyOn(globalThis, 'fetch').mockImplementation(input => {
    if (String(input) === '/api/goals/status-check') return startup
    if (String(input) === '/api/goals') return Promise.resolve(response([goal('a', 'ACTIVE')]))
    return Promise.resolve(response([]))
  })
  window.history.pushState({}, '', '/quick')
  render(<App />)
  await act(async () => { window.dispatchEvent(new Event('planner:goals-changed')) })
  expect(await screen.findByRole('link', { name: /规划a\s*正在执行/ })).toHaveAttribute('href', '/goals/a/today')
  await act(async () => { finishStartup(response([goal('a', 'RECOVERY_REQUIRED')])) })
  expect(screen.queryByRole('link', { name: /规划a\s*等待恢复/ })).not.toBeInTheDocument()
})

it('isolates discussion drafts and delayed replies when switching between two plan pages', async () => {
  let finishRevision!: (value: Response) => void
  const revision = new Promise<Response>(resolve => { finishRevision = resolve })
  window.history.pushState({}, '', '/goals/a/plan?discussion=1')
  vi.spyOn(globalThis, 'fetch').mockImplementation((input, options) => {
    const path = String(input)
    if (path === '/api/goals' || path === '/api/goals/status-check') return Promise.resolve(response([goal('a'), goal('b')]))
    if (path === '/api/goals/a/plan/revise' && options?.method === 'POST') return revision
    if (path === '/api/goals/a/plan') return Promise.resolve(response(workspace('a')))
    if (path === '/api/goals/b/plan') return Promise.resolve(response(workspace('b')))
    return Promise.resolve(response([]))
  })
  render(<App />)
  const editor = await screen.findByLabelText('希望怎样调整？')
  fireEvent.change(editor, { target: { value: '只属于规划a的意见' } })
  fireEvent.click(screen.getByRole('button', { name: '发送' }))
  fireEvent.click(screen.getByRole('link', { name: /规划b\s*等你确认时间/ }))
  await screen.findByText('b的已保存安排')
  fireEvent.click(screen.getByRole('button', { name: '写反馈' }))
  expect(await screen.findByLabelText('希望怎样调整？')).toHaveValue('')
  await act(async () => { finishRevision(response({ ...workspace('a'), plan: { ...workspace('a').plan, summary: 'a的迟到回复' } })) })
  await waitFor(() => expect(screen.getByText('b的已保存安排')).toBeInTheDocument())
  expect(screen.queryByText('a的迟到回复')).not.toBeInTheDocument()
  expect(window.location.pathname).toBe('/goals/b/plan')
})

it('finishes publication with the active version, cleared recovery URL and updated sidebar', async () => {
  let published = false
  const pending = { assistant_message: '当前恢复草案', can_apply: true, needs_clarification: false,
    scope: 'WEEK', affected_stage_orders: [], affected_weeks: [1], downstream_affected_weeks: [],
    proposed_changes: ['补回任务'], tradeoffs: [], unchanged_guarantees: [], total_weeks_change: 0 }
  const saved = () => ({ ...workspace('a'), status: published ? 'ACTIVE' : 'RECOVERY_REQUIRED',
    plan: { ...workspace('a').plan, status: 'CONFIRMED', revision: published ? 2 : 1 },
    recovery_review: published ? null : { token: 'confirmed', decisions: [], extension_dates: [], extension_days: 0, is_recovery: true },
    pending_adjustment: published ? null : pending })
  window.history.pushState({}, '', '/goals/a/plan?recovery=1&discussion=1&adjustment=1')
  const fetchMock = vi.spyOn(globalThis, 'fetch').mockImplementation(input => {
    const path = String(input)
    if (path === '/api/goals' || path === '/api/goals/status-check') return Promise.resolve(response([goal('a', published ? 'ACTIVE' : 'RECOVERY_REQUIRED')]))
    if (path.endsWith('/plan/adjustment/preview')) return Promise.resolve(response({ id: 'preview', schedule: [], unallocated_count: 0 }))
    if (path.endsWith('/plan/adjustment/apply')) { published = true; return Promise.resolve(response(saved())) }
    if (path.endsWith('/plan')) return Promise.resolve(response(saved()))
    return Promise.resolve(response([]))
  })
  render(<App />)
  fireEvent.click(await screen.findByRole('button', { name: '先预览日期与时间' }))
  await screen.findByRole('button', { name: '确认并发布调整' })
  for (let index = 0; index < 2; index++) {
    fireEvent.click(screen.getByRole('button', { name: /^计划讨论/ }))
    fireEvent.click(screen.getByRole('button', { name: /^计划讨论/ }))
    expect(screen.getByRole('button', { name: '确认并发布调整' })).toBeVisible()
  }
  expect(fetchMock.mock.calls.filter(([input]) => String(input).endsWith('/plan/adjustment/preview'))).toHaveLength(1)
  expect(fetchMock.mock.calls.filter(([input]) => String(input).endsWith('/plan/adjustment/apply'))).toHaveLength(0)
  fireEvent.click(await screen.findByRole('button', { name: '确认并发布调整' }))
  expect(await screen.findByText('计划 v1.2 已生效')).toBeInTheDocument()
  expect(screen.queryByText('当前恢复草案')).not.toBeInTheDocument()
  expect(await screen.findByRole('link', { name: /规划a\s*正在执行/ })).toHaveAttribute('href', '/goals/a/today')
  expect(window.location.search).toBe('')
  expect(sessionStorage.getItem('planner:recovery-view:a')).toBeNull()
  fireEvent.click(screen.getByRole('button', { name: '写反馈' }))
  fireEvent.click(screen.getByRole('button', { name: /^计划讨论/ }))
  fireEvent.click(screen.getByRole('button', { name: /^计划讨论/ }))
  expect(screen.queryByRole('button', { name: '确认并发布调整' })).not.toBeInTheDocument()
  expect(fetchMock.mock.calls.filter(([input]) => String(input).endsWith('/plan/adjustment/preview'))).toHaveLength(1)
  expect(fetchMock.mock.calls.filter(([input]) => String(input).endsWith('/plan/adjustment/apply'))).toHaveLength(1)
})

it.each(['EXPLAIN', 'REVISE'])('does not repeat %s or publish a plan when discussion is closed before its response and opened again', async intent => {
  let finish!: (value: Response) => void
  const pendingRequest = new Promise<Response>(resolve => { finish = resolve })
  const pending = { assistant_message: '已保存的调整建议，等待确认。', can_apply: true, needs_clarification: false,
    scope: 'WEEK', affected_stage_orders: [], affected_weeks: [1], downstream_affected_weeks: [],
    proposed_changes: ['降低本周强度'], tradeoffs: [], unchanged_guarantees: [], total_weeks_change: 0 }
  window.history.pushState({}, '', '/goals/a/plan?discussion=1')
  const fetchMock = vi.spyOn(globalThis, 'fetch').mockImplementation((input, options) => {
    const path = String(input)
    if (path === '/api/goals' || path === '/api/goals/status-check') return Promise.resolve(response([goal('a')]))
    if (path.endsWith(intent === 'EXPLAIN' ? '/plan/explain' : '/plan/revise') && options?.method === 'POST') return pendingRequest
    if (path.endsWith('/plan')) return Promise.resolve(response(workspace('a')))
    return Promise.resolve(response([]))
  })
  render(<App />)
  const editor = await screen.findByLabelText('希望怎样调整？')
  if (intent === 'EXPLAIN') fireEvent.click(screen.getByRole('button', { name: '只做解释' }))
  fireEvent.change(editor, { target: { value: '想了解降低强度会怎样安排' } })
  fireEvent.click(screen.getByRole('button', { name: '发送' }))
  const toggle = () => screen.getByRole('button', { name: /^计划讨论/ })
  fireEvent.click(toggle())
  expect(screen.queryByLabelText('希望怎样调整？')).not.toBeInTheDocument()
  const content = '已保存的回复正文。'.repeat(80)
  await act(async () => { finish(response({ ...workspace('a'),
    pending_adjustment: intent === 'REVISE' ? pending : null,
    messages: [{ id: 'a1', role: 'ASSISTANT', content, generation_status: 'SUCCEEDED' }],
  })) })
  fireEvent.click(toggle())
  expect(screen.getByRole('button', { name: '显示全文' })).toBeInTheDocument()
  // Closing during presentation must consume only the display animation.
  fireEvent.click(toggle())
  fireEvent.click(toggle())
  expect(document.querySelector('.assistant-answer')).toHaveAttribute('aria-busy', 'false')
  expect(document.querySelector('.assistant-answer')).toHaveTextContent(content)
  if (intent === 'REVISE') {
    expect(screen.getByRole('button', { name: '先预览日期与时间' })).toBeVisible()
    expect(screen.getByText('降低本周强度')).toBeVisible()
  }
  expect(screen.getByText('a的已保存安排')).toBeInTheDocument()
  const mutationPaths = fetchMock.mock.calls.filter(([, options]) => options?.method === 'POST')
    .map(([input]) => String(input)).filter(path => /\/plan\//.test(path))
  expect(mutationPaths).toEqual([`/api/goals/a/plan/${intent === 'EXPLAIN' ? 'explain' : 'revise'}`])
})

it.each([true, false])('takes Today proposals to saved preview or decisions without publishing (can_apply=%s)', async canApply => {
  const pending = { assistant_message: '已保存的调整草案', can_apply: canApply, needs_clarification: !canApply,
    scope: 'WEEK', affected_stage_orders: [], affected_weeks: [1], downstream_affected_weeks: [],
    proposed_changes: ['调整未完成任务'], tradeoffs: [], unchanged_guarantees: [] }
  const count = { completed_tasks: 0, total_tasks: 1, percentage: 0 }
  const fetchMock = vi.spyOn(globalThis, 'fetch').mockImplementation(input => {
    const path = String(input)
    if (path.endsWith('/plan')) return Promise.resolve(response({ ...workspace('a'), pending_adjustment: pending }))
    if (path.endsWith('/execution/calendar')) return Promise.resolve(response({ days: [] }))
    if (path.endsWith('/today')) return Promise.resolve(response({ goal_id: 'a', status: 'ACTIVE', today: '2026-10-16',
      day: { id: 'day-a', tasks: [], evidence: [], do_not: [], primary_goal: '今日任务', suggested_minutes: 60 },
      day_progress: count, stage_progress: count, overall_progress: count, feedbacks: [], adjustments: [] }))
    return Promise.resolve(response([]))
  })
  render(<MemoryRouter initialEntries={['/goals/a/today']}><Routes>
    <Route path="/goals/:goalId/today" element={<TodayPage />} />
    <Route path="/goals/:goalId/plan" element={<h1>读取已保存草案并预览</h1>} />
  </Routes></MemoryRouter>)
  fireEvent.click(await screen.findByRole('button', { name: canApply ? '查看预览并确认' : '继续处理调整建议' }))
  expect(await screen.findByRole('heading', { name: '读取已保存草案并预览' })).toBeInTheDocument()
  expect(fetchMock.mock.calls.some(([input]) => /adjustment\/(apply|preview)$/.test(String(input)))).toBe(false)
})
