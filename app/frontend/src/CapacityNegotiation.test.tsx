import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import App from './App'
import { CapacitySummary } from './planner/plan-review/CapacityNegotiationPanel'

const conflict = {
  goal_id: 'capacity-goal', goal_title: '分阶段报告', status: 'PLAN_DRAFTING',
  generation_phase: 'DETAILS', generated_weeks: 0, generation_target_weeks: 4,
  available_generation_modes: ['STANDARD'], messages: [], plan: { id: 'original-plan' },
  generation_job: { status: 'FAILED', error_code: 'PLAN_COMMITMENT_CAPACITY_INVALID',
    error_message: '最低工作量超过窗口容量，需要讨论调整。' },
  capacity_negotiation: {
    state: 'AWAITING_USER', classification: 'CONSTRAINT_CONFLICT',
    message: '工作量1400分钟，可用1080分钟，先讨论边界。',
    verified_conflicts: [{ label: '两份报告', weeks: [1, 2, 3, 4], minimum_minutes: 1400, available_minutes: 1080, deficit_minutes: 320 }],
    capacity_segments: [{ label: '前期', start_week: 1, end_week: 2, available_days_per_week: 2, suggested_daily_minutes: 30 },
      { label: '后期', start_week: 3, end_week: 4, available_days_per_week: 4, suggested_daily_minutes: 120 }],
    options: [{ title: '讨论是否可以延长期限', instruction: '请先核对期限是否可协商，不修改固定考试日。' }],
    rules: [],
  },
}

afterEach(() => { cleanup(); vi.restoreAllMocks() })

describe('capacity conflict negotiation', () => {
  it('shows short Saturday allowances and a single-week exception separately', () => {
    render(<CapacitySummary segments={[{ label: '后期', start_week: 3, end_week: 4,
      available_days_per_week: 5, suggested_daily_minutes: 120,
      exception_days_per_week: 1, exception_daily_minutes: 60, exception_weekdays: [5],
      weekly_capacity_overrides: [{ week_index: 4, weekly_execution_days: 1,
        available_days_per_week: 5, suggested_daily_minutes: 120,
        exception_days_per_week: 1, exception_daily_minutes: 90, exception_weekdays: [5] }] }]} />)
    expect(screen.getByText(/普通日 120 分钟；周六各 60 分钟/)).toBeInTheDocument()
    expect(screen.getByText(/第 4 周例外.*周六各 90 分钟/)).toBeInTheDocument()
    expect(screen.queryByText(/每天 120 分钟/)).not.toBeInTheDocument()
  })
  it('shows separate stage budgets and makes quick choices non-mutating', async () => {
    window.history.pushState({}, '', '/goals/capacity-goal/plan')
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockImplementation(input => Promise.resolve(
      new Response(JSON.stringify(String(input) === '/api/goals' ? [] : conflict), { status: 200 })))
    render(<App />)
    expect(await screen.findByRole('heading', { name: '一起找到可行的安排' })).toBeInTheDocument()
    expect(screen.getByText(/还差 320 分钟/)).toBeInTheDocument()
    expect(screen.getByText(/每周可用 2 天/)).toBeInTheDocument()
    expect(screen.getByText(/每周可用 4 天/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /标准生成/ })).toBeDisabled()
    fireEvent.click(screen.getByRole('button', { name: '讨论是否可以延长期限' }))
    expect(screen.getByLabelText('你的选择或补充条件')).toHaveValue('请先核对期限是否可协商，不修改固定考试日。')
    expect(fetchMock.mock.calls.some(([input, init]) => init?.method === 'POST' && /plan\/(revise|adjustment|generation)/.test(String(input)))).toBe(false)
  })

  it('shows request progress and retains user input when discussion fails', async () => {
    window.history.pushState({}, '', '/goals/capacity-goal/plan')
    let finish: ((response: Response) => void) | undefined
    vi.spyOn(globalThis, 'fetch').mockImplementation((input, init) => {
      if (String(input).endsWith('/plan/revise') && init?.method === 'POST') {
        return new Promise<Response>(resolve => { finish = resolve })
      }
      return Promise.resolve(new Response(JSON.stringify(String(input) === '/api/goals' ? [] : conflict), { status: 200 }))
    })
    render(<App />)
    const editor = await screen.findByLabelText('你的选择或补充条件')
    fireEvent.change(editor, { target: { value: '期限可协商，前两周时间不增加。' } })
    fireEvent.click(screen.getByRole('button', { name: '发送并核算方案' }))
    expect(await screen.findByRole('status')).toHaveTextContent('已有计划保持不变')
    expect(screen.getByRole('button', { name: '发送并核算方案' })).toBeDisabled()
    finish?.(new Response(JSON.stringify({ detail: { message: '模型暂时超时，请重试，原计划未改变。' } }), { status: 502 }))
    expect(await screen.findByText('模型暂时超时，请重试，原计划未改变。')).toBeInTheDocument()
    await waitFor(() => expect(editor).toHaveValue('期限可协商，前两周时间不增加。'))
    await waitFor(() => expect(screen.getByRole('button', { name: '发送并核算方案' })).toBeEnabled())
  })
})
