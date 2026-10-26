import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { FixedActivityMovePanel } from './planner/plan-review/FixedActivityMovePanel'

afterEach(() => { cleanup(); vi.restoreAllMocks() })

const preview = { id: 'preview', status: 'PREVIEW', title: '娱乐', plan_title: '生活安排', can_apply: true,
  before: { date: '2026-11-14', start_local: '20:00', end_local: '21:00' },
  after: { date: '2026-11-15', start_local: '20:00', end_local: '21:00' },
  capacity_changes: [{ date: '2026-11-14', before_minutes: 60, after_minutes: 120, delta_minutes: 60 },
    { date: '2026-11-15', before_minutes: 120, after_minutes: 60, delta_minutes: -60 }],
  conflicts: [], message: '只调整一次，保留时长。' }

async function openAndPreview() {
  fireEvent.click(screen.getByRole('button', { name: '查看固定活动调整' }))
  fireEvent.click(screen.getByRole('button', { name: '读取当日活动' }))
  const select = await screen.findByLabelText('选择活动')
  fireEvent.change(select, { target: { value: 'activity' } })
  fireEvent.click(screen.getByRole('button', { name: '预览活动调整' }))
  await screen.findByText(/待确认的活动调整/)
}

it('shows both dates, requires explicit confirmation and prepares the next discussion', async () => {
  const draft = vi.fn()
  const fetchMock = vi.spyOn(globalThis, 'fetch').mockImplementation(input => Promise.resolve(new Response(JSON.stringify(
    String(input).endsWith('/preview') ? preview : String(input).endsWith('/apply') ? { ...preview, status: 'APPLIED' }
      : { activities: [{ id: 'activity', title: '娱乐', start_local: '20:00', end_local: '21:00' }] }), { status: 200 })))
  render(<FixedActivityMovePanel goalId="goal" onDraft={draft} />)
  await openAndPreview()
  expect(screen.getByText(/原安排：2026-11-14/)).toBeInTheDocument()
  expect(screen.getByText(/2026-11-15：共享剩余时间.*-60/)).toBeInTheDocument()
  expect(fetchMock.mock.calls.some(([input]) => String(input).endsWith('/apply'))).toBe(false)
  fireEvent.click(screen.getByRole('button', { name: '确认这一次活动迁移' }))
  expect(await screen.findByText(/活动调整已生效/)).toBeInTheDocument()
  expect(draft).toHaveBeenCalledWith(expect.stringContaining('不能推广成每周增时'))
})

it('blocks conflicts and invalidates preview after editing', async () => {
  vi.spyOn(globalThis, 'fetch').mockImplementation(input => Promise.resolve(new Response(JSON.stringify(
    String(input).endsWith('/preview') ? { ...preview, can_apply: false, conflicts: ['与已有课程冲突'] }
      : { activities: [{ id: 'activity', title: '娱乐', start_local: '20:00', end_local: '21:00' }] }), { status: 200 })))
  render(<FixedActivityMovePanel goalId="goal" onDraft={vi.fn()} />)
  await openAndPreview()
  expect(screen.getByRole('button', { name: '确认这一次活动迁移' })).toBeDisabled()
  fireEvent.change(screen.getByLabelText('开始时间'), { target: { value: '19:00' } })
  expect(screen.queryByRole('button', { name: '确认这一次活动迁移' })).not.toBeInTheDocument()
})

it('reports failures, keeps inputs and permits retry', async () => {
  vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response(JSON.stringify({ detail: { message: '日历已变化，请重试' } }), { status: 409 }))
  render(<FixedActivityMovePanel goalId="goal" onDraft={vi.fn()} />)
  fireEvent.click(screen.getByRole('button', { name: '查看固定活动调整' }))
  fireEvent.click(screen.getByRole('button', { name: '读取当日活动' }))
  expect(await screen.findByRole('alert')).toHaveTextContent('日历已变化，请重试')
  expect(screen.getByRole('button', { name: '读取当日活动' })).toBeEnabled()
  expect(screen.getByLabelText('原活动日期')).not.toHaveValue('')
})
