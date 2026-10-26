import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { previewTaskRecommendations, type TaskRecommendationPreview } from '../api'
import { TaskRecommendations } from './TaskRecommendations'

vi.mock('../api', () => ({ previewTaskRecommendations: vi.fn() }))
afterEach(() => { cleanup(); vi.resetAllMocks() })
const result: TaskRecommendationPreview = {
  id: 'preview', fingerprint: 'snapshot', date: '2026-10-08', strategy: 'RULES',
  capacity: { available_minutes: 60, current_task_minutes: 0, spare_minutes: 60, recommended_minutes: 30, remaining_minutes: 20 },
  items: [{ task_id: 'reading', title: '阅读第1次', scheduling_policy: 'FLOATING', scheduled_date: '2026-10-10', estimate_minutes: 30, group_key: 'reading', eligible_from: null, eligible_to: null, remaining_value: 2, target_unit: 'COUNT', reason: '前置条件已完成。', allowed_actions: ['ARRANGE_TODAY', 'RESCHEDULE', 'VIEW_DATE'] }],
  blocked_summary: [{ task_id: 'review', title: '真题复盘', scheduled_date: '2026-10-11', reason_code: 'DEPENDENCY_BLOCKED', reason: '需先完成真题' }],
  blocked_count: 1, warnings: [],
}
function props() { return { goalId: 'goal', refreshKey: 1, disabled: false, onArrange: vi.fn(), onReschedule: vi.fn(), onViewDate: vi.fn() } }

it('shows one actionable list, collapses blocked items and only delegates confirmed actions', async () => {
  vi.mocked(previewTaskRecommendations).mockResolvedValue(result)
  const callbacks = props()
  const view = render(<TaskRecommendations {...callbacks} />)
  await screen.findByText('阅读第1次')
  expect(screen.getByText('暂不推荐 · 1 项').closest('details')).not.toHaveAttribute('open')
  expect(screen.getAllByRole('button', { name: '安排到今天', hidden: true })).toHaveLength(1)
  fireEvent.click(screen.getByRole('button', { name: '安排到今天', hidden: true }))
  expect(callbacks.onArrange).toHaveBeenCalledWith(result.items[0])
  fireEvent.click(screen.getByRole('button', { name: '改到某日', hidden: true }))
  expect(callbacks.onReschedule).toHaveBeenCalledWith(result.items[0])
  expect(previewTaskRecommendations).toHaveBeenCalledTimes(1)
  view.rerender(<TaskRecommendations {...callbacks} refreshKey={2} />)
  await waitFor(() => expect(previewTaskRecommendations).toHaveBeenCalledTimes(2))
})

it('keeps read failures local and refresh allows retry', async () => {
  vi.mocked(previewTaskRecommendations).mockRejectedValueOnce(new Error('请求失败')).mockResolvedValue({ ...result, items: [] })
  render(<TaskRecommendations {...props()} />)
  await screen.findByRole('alert', { hidden: true })
  fireEvent.click(screen.getByRole('button', { name: '刷新今日建议', hidden: true }))
  await screen.findByText('当前没有可加入今天的建议任务。')
  expect(screen.queryByRole('alert', { hidden: true })).not.toBeInTheDocument()
})

it('automatically requests intelligent recommendations and applies optional preferences', async () => {
  vi.mocked(previewTaskRecommendations).mockResolvedValueOnce(result).mockResolvedValueOnce({
    ...result, strategy: 'AI', summary: '优先处理阅读', items: [{ ...result.items[0], reason: '符合你今天练习阅读的偏好。' }],
  }).mockResolvedValueOnce({ ...result, strategy: 'AI_FALLBACK', warnings: ['智能推荐未完成，已显示规则建议。'] })
  render(<TaskRecommendations {...props()} />)
  await screen.findByText('阅读第1次')
  expect(previewTaskRecommendations).toHaveBeenCalledTimes(1)
  expect(previewTaskRecommendations).toHaveBeenLastCalledWith('goal', { use_ai: true, instruction: '' })
  expect(screen.getByText('调整偏好').closest('details')).not.toHaveAttribute('open')
  expect(screen.queryByRole('button', { name: '智能推荐', hidden: true })).not.toBeInTheDocument()
  fireEvent.change(screen.getByRole('textbox', { name: '今日推荐偏好', hidden: true }), { target: { value: '优先阅读' } })
  fireEvent.click(screen.getByRole('button', { name: '按偏好重新推荐', hidden: true }))
  await screen.findByText('符合你今天练习阅读的偏好。')
  expect(previewTaskRecommendations).toHaveBeenLastCalledWith('goal', { use_ai: true, instruction: '优先阅读' })
  fireEvent.click(screen.getByRole('button', { name: '刷新今日建议', hidden: true }))
  await screen.findByText('规则建议 · 智能推荐未完成')
  expect(screen.getByText('智能推荐未完成，已显示规则建议。')).toBeInTheDocument()
})

it('ignores old model result when execution state has changed', async () => {
  let resolveModel!: (result: TaskRecommendationPreview) => void
  vi.mocked(previewTaskRecommendations).mockResolvedValueOnce(result)
    .mockImplementationOnce(() => new Promise((resolve) => { resolveModel = resolve }))
    .mockResolvedValueOnce({ ...result, items: [] })
  const callbacks = props()
  const view = render(<TaskRecommendations {...callbacks} />)
  await screen.findByText('阅读第1次')
  fireEvent.click(screen.getByRole('button', { name: '刷新今日建议', hidden: true }))
  expect(screen.queryByRole('button', { name: '安排到今天', hidden: true })).not.toBeInTheDocument()
  view.rerender(<TaskRecommendations {...callbacks} refreshKey={2} />)
  await screen.findByText('当前没有可加入今天的建议任务。')
  resolveModel({ ...result, strategy: 'AI' })
  await waitFor(() => expect(screen.queryByText('阅读第1次')).not.toBeInTheDocument())
})

it('does not repeat model request for identical execution data', async () => {
  vi.mocked(previewTaskRecommendations).mockResolvedValue(result)
  const callbacks = props()
  const view = render(<TaskRecommendations {...callbacks} refreshKey={{ revision: 1 }} />)
  await screen.findByText('阅读第1次')
  view.rerender(<TaskRecommendations {...callbacks} refreshKey={{ revision: 1 }} />)
  expect(previewTaskRecommendations).toHaveBeenCalledTimes(1)
  view.rerender(<TaskRecommendations {...callbacks} refreshKey={{ revision: 2 }} />)
  await waitFor(() => expect(previewTaskRecommendations).toHaveBeenCalledTimes(2))
  expect(previewTaskRecommendations).toHaveBeenLastCalledWith('goal', { use_ai: true, instruction: '' })
})
