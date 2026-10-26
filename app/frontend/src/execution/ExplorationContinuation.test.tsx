import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, expect, it } from 'vitest'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { ExplorationContinuation } from './ExplorationContinuation'
import type { ExplorationExecutionState } from '../api'

afterEach(cleanup)
const base: ExplorationExecutionState = { generated_through_week: 1, total_weeks: 3, next_unplanned_week: 2, current_window_completed: true, pending_window_start_week: null, pending_window_end_week: null }
function show(state: ExplorationExecutionState) {
  render(<MemoryRouter initialEntries={['/goals/a/today']}><Routes>
    <Route path="/goals/a/today" element={<ExplorationContinuation goalId="a" state={state} />} />
    <Route path="/goals/a/plan" element={<h1>已保存的计划</h1>} />
  </Routes></MemoryRouter>)
}
it('distinguishes a completed window from the unfinished route', () => {
  show(base)
  expect(screen.getByText('当前体验阶段已完成，整条路线尚未结束')).toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', { name: '继续探索下一阶段' }))
  expect(screen.getByRole('heading', { name: '已保存的计划' })).toBeInTheDocument()
})
it('opens the saved draft without generating or publishing again', () => {
  show({ ...base, pending_window_start_week: 2, pending_window_end_week: 2 })
  expect(screen.getByText('下一阶段草案已保存')).toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', { name: '查看已保存的下一阶段草案' }))
  expect(screen.getByRole('heading', { name: '已保存的计划' })).toBeInTheDocument()
})
it('offers no further expansion after the final route window', () => {
  show({ ...base, generated_through_week: 3, next_unplanned_week: null })
  expect(screen.queryByRole('button')).not.toBeInTheDocument()
})
