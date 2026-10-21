import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { ClarificationPage } from './planner/ClarificationPage'
import { RouteReviewPage } from './planner/RouteReviewPage'
import { PlanReviewPage } from './planner/PlanReviewPage'

afterEach(() => { cleanup(); vi.restoreAllMocks() })

it.each(['RECOVERY_REQUIRED', 'ACTIVE', 'READY_TO_START'])('keeps the existing route, plan and execution reachable while %s', async (status) => {
  const pending = {
    assistant_message: '已保存所选英语任务的恢复建议，等待确认。', user_intent_summary: '恢复遗留任务',
    scope: 'MULTI_SCOPE', affected_stage_orders: [1], affected_weeks: [1], downstream_affected_weeks: [],
    proposed_changes: ['先讨论过期窗口的处理方式'], tradeoffs: [], unchanged_guarantees: ['完成记录保留'],
    total_weeks_change: 0, weekly_intensity_change: 'UNCHANGED', buffer_change: 'UNCHANGED',
    needs_clarification: true, clarifying_question: '是否调整执行窗口？', can_apply: false,
  }
  const base = { goal_id: 'english', goal_title: '英语考试', status, requirement_version: 1,
    requirement_snapshot: { objective: '准备英语考试' }, planning_mode: 'COMMITTED', schedule_type: 'RHYTHM' }
  const plan = {
    ...base, route_version: 1, route_revision: 1, generation_phase: 'COMPLETE', generated_weeks: 1,
    total_weeks: 1, messages: [{ id: 'recovery-answer', sequence: 1, role: 'ASSISTANT',
      content: pending.assistant_message, generation_status: 'SUCCEEDED', created_at: '2026-10-16T12:00:00Z' }],
    pending_adjustment: pending, revision_history: [],
    recovery_guard: { required: true, message: '先处理恢复建议', missed_dates: [] },
    recovery_review: status === 'RECOVERY_REQUIRED' ? { token: 'saved-decision', decisions: ['ALLOW_EXTENSION', 'KEEP_DEADLINE'], extension_dates: ['2026-11-04'], extension_days: 1, is_recovery: true } : null,
    plan: { id: 'original-plan', route_version_id: 'original-route', version: 1, revision: 1,
      status: 'CONFIRMED', summary: '原执行计划仍然保留', total_weeks: 1, weekly_execution_days: 3,
      weekly_minutes: 90, total_estimated_minutes: 90, planning_mode: 'COMMITTED', schedule_type: 'RHYTHM',
      buffer_strategy: [], assumptions: [], open_questions: [], change_summary: [], milestones: [], days: [] },
  }
  const route = { ...base, messages: [], requirements_changed_since_route: false,
    route: { id: 'original-route', version: 1, revision: 1, status: 'CONFIRMED', summary: '原英语路线',
      stages: [], tradeoffs: [], assumptions: [], open_questions: [], change_summary: [] } }
  const clarification = { ...base, raw_input: '准备英语考试', messages: [], facts: [], conflicts: [], open_questions: [],
    downstream_route_preserved: true, requirements_changed_since_route: false,
    readiness: { ready_for_review: true, blocking_fields: [], reason: '需求已确认' },
    requirement_draft: { id: 'requirement', version: 1, status: 'CONFIRMED', snapshot: base.requirement_snapshot } }
  const fetchMock = vi.spyOn(globalThis, 'fetch').mockImplementation((input) => {
    const path = String(input)
    if (path.endsWith('/plan/recovery-decision')) {
      plan.pending_adjustment = { ...pending, can_apply: true, needs_clarification: false, clarifying_question: '' }
      plan.recovery_review = { ...plan.recovery_review!, decisions: [] }
      return Promise.resolve(new Response(JSON.stringify(plan), { status: 200 }))
    }
    if (path.endsWith('/plan/adjustment/preview')) return Promise.resolve(new Response(JSON.stringify({ id: 'saved-preview', schedule: [], unallocated_count: 0 }), { status: 200 }))
    if (path.endsWith('/plan/adjustment/apply')) return Promise.resolve(new Response(JSON.stringify(plan), { status: 200 }))
    const data = path.endsWith('/plan') ? plan : path.endsWith('/route') ? route
      : path.endsWith('/clarification') ? clarification : []
    return Promise.resolve(new Response(JSON.stringify(data), { status: 200 }))
  })
  render(<MemoryRouter initialEntries={['/goals/english/plan?recovery=1&discussion=1&adjustment=1']}><Routes>
    <Route path="/goals/:goalId/plan" element={<PlanReviewPage />} />
    <Route path="/goals/:goalId/route" element={<RouteReviewPage />} />
    <Route path="/goals/:goalId/clarify" element={<ClarificationPage />} />
    <Route path="/goals/:goalId/today" element={<h1>原执行工作区</h1>} />
    <Route path="/goals/:goalId/prepare" element={<h1>原准备工作区</h1>} />
  </Routes></MemoryRouter>)

  expect((await screen.findAllByText('已保存所选英语任务的恢复建议，等待确认。')).length).toBeGreaterThan(0)
  if (status === 'RECOVERY_REQUIRED') expect(screen.getByRole('heading', { name: '讨论剩余任务的恢复安排' })).toBeInTheDocument()
  const returnLabel = status === 'READY_TO_START' ? '返回准备开始 →' : '返回今日执行 →'
  expect(screen.getByRole('button', { name: returnLabel })).toBeEnabled()
  fireEvent.click(screen.getByRole('button', { name: '前往“明确需求”阶段' }))
  await screen.findByRole('heading', { name: '回看已确认需求' })
  fireEvent.click(screen.getByRole('button', { name: '返回现有路线 →' }))
  await screen.findByRole('heading', { name: '第一版整体路线' })
  expect(screen.queryByRole('button', { name: /确认路线/ })).not.toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', { name: '返回时间计划' }))
  await screen.findByText('原执行计划仍然保留')
  if (status === 'RECOVERY_REQUIRED') {
    fireEvent.click(screen.getByRole('button', { name: '查看并确认调整' }))
    fireEvent.click(screen.getByRole('button', { name: '允许这次顺延' }))
    await waitFor(() => expect(screen.getByRole('button', { name: '先预览日期与时间' })).toBeEnabled())
    fireEvent.click(screen.getByRole('button', { name: '先预览日期与时间' }))
    await screen.findByText('调整后的时间安排')
    fireEvent.click(screen.getByRole('button', { name: '确认并发布调整' }))
    await waitFor(() => expect(fetchMock.mock.calls.some(([input]) => String(input).endsWith('/plan/adjustment/apply'))).toBe(true))
    expect(screen.queryByRole('button', { name: '先处理追赶' })).not.toBeInTheDocument()
  }
  fireEvent.click(screen.getByRole('button', { name: returnLabel }))
  await screen.findByRole('heading', { name: status === 'READY_TO_START' ? '原准备工作区' : '原执行工作区' })
  expect(fetchMock.mock.calls.some(([input]) => /\/(draft|revise|recovery\/ai|generation)$/.test(String(input)))).toBe(false)
})
