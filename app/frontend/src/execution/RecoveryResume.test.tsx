import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { Link, MemoryRouter, Route, Routes } from 'react-router-dom'
import { ProgressPage } from './ProgressPage'

afterEach(() => { cleanup(); vi.restoreAllMocks() })

it.each([false, true])('restores a saved recovery after leaving and returning (preview=%s)', async (hasPreview) => {
  const summary = { percentage: 4, completed_tasks: 1, total_tasks: 23 }
  const progress = { goal_title: '英语考试', schedule_type: 'CALENDAR', current_stage_order: 2,
    day_progress: summary, stage_progress: summary, overall_progress: summary,
    stage_summary: '原阶段记录保留', overall_summary: '已完成1项', inactivity: null,
    trend: { status: 'STABLE', window_days: 7 }, recent_days: [], feedback_timeline: [], revision_history: [] }
  const plan = { pending_adjustment: { assistant_message: '已保存的恢复方案', can_apply: hasPreview,
    needs_clarification: !hasPreview, clarifying_question: hasPreview ? null : '需要先确认顺延',
    affected_stage_orders: [2], affected_weeks: [2], proposed_changes: ['接回8项'], tradeoffs: [], total_weeks_change: 0 },
    recovery_review: { token: 'saved', decisions: hasPreview ? [] : ['ALLOW_EXTENSION', 'KEEP_DEADLINE'], extension_days: 1, extension_dates: ['2026-11-04'], is_recovery: true },
    pending_adjustment_preview: hasPreview ? { id: 'original-preview', schedule: [{ date: '2026-10-19', title: '原预览任务', start: '18:00', end: '18:30' }], unallocated_count: 0 } : null }
  const fetchMock = vi.spyOn(globalThis, 'fetch').mockImplementation(input => Promise.resolve(new Response(
    JSON.stringify(String(input).endsWith('/plan') ? plan : String(input).endsWith('/execution/progress') ? progress : []), { status: 200 })))
  render(<MemoryRouter initialEntries={['/goals/english/progress?recovery=1']}>
    <Link to="/other">切换规划</Link><Routes>
      <Route path="/goals/:goalId/progress" element={<ProgressPage />} />
      <Route path="/other" element={<Link to="/goals/english/progress?recovery=1">返回英语</Link>} />
      <Route path="/goals/:goalId/plan" element={<h1>继续原讨论</h1>} />
    </Routes></MemoryRouter>)
  await screen.findByText('已保存的恢复方案')
  expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
  if (hasPreview) expect(screen.getByText('原预览任务')).toBeInTheDocument()
  else expect(screen.getByRole('button', { name: '允许这次顺延' })).toBeEnabled()
  fireEvent.click(screen.getByRole('link', { name: '切换规划' }))
  fireEvent.click(await screen.findByRole('link', { name: '返回英语' }))
  await screen.findByText('已保存的恢复方案')
  expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
  if (hasPreview) expect(screen.getByRole('button', { name: '确认日期与时间安排' })).toBeEnabled()
  fireEvent.click(screen.getByRole('button', { name: '继续讨论 / 补充调整意见' }))
  await screen.findByRole('heading', { name: '继续原讨论' })
  expect(fetchMock.mock.calls.every(([, options]) => !options?.method || options.method === 'GET')).toBe(true)
})
