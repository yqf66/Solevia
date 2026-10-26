import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter, useLocation } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ExecutionDiscussionDock, isExecutionDiscussionPath } from './App'
import * as api from './api'

beforeEach(() => {
  vi.spyOn(api, 'listResearchResponses').mockResolvedValue([])
  vi.spyOn(api, 'getResearchReadiness').mockResolvedValue({ ready: true, status: 'READY', components: {} })
  vi.spyOn(api, 'setResearchConnection').mockResolvedValue({ ready: true, status: 'READY', components: {} })
})

const progress = {
  review_signal: { recommended: false, trigger_type: 'NONE', reason: null },
}

const emptyWorkspace = {
  goal_id: 'goal-1',
  goal_title: '学习摄影',
  goal_status: 'ACTIVE',
  planning_mode: 'EXPLORATORY',
  schedule_type: 'RHYTHM',
  messages: [],
  insights: [],
  active_review: null,
  progress,
  latest_analysis: null,
}

afterEach(() => {
  cleanup()
  vi.useRealTimers()
  vi.restoreAllMocks()
})

describe('ExecutionDiscussionDock', () => {
  it.each([true, false])('never repeats a message or plan request when closed during or after generation (pending=%s)', async closeWhilePending => {
    let finish!: (value: api.ExecutionDiscussionWorkspace) => void
    const pending = new Promise<api.ExecutionDiscussionWorkspace>(resolve => { finish = resolve })
    const read = vi.spyOn(api, 'getExecutionDiscussion').mockResolvedValue(emptyWorkspace as unknown as api.ExecutionDiscussionWorkspace)
    const send = vi.spyOn(api, 'sendExecutionDiscussionMessage').mockReturnValue(pending)
    const revise = vi.spyOn(api, 'revisePlan')
    const retry = vi.spyOn(api, 'retryExecutionDiscussion')
    const apply = vi.spyOn(api, 'applyPlanAdjustment')
    const preview = vi.spyOn(api, 'previewPlanAdjustment')
    render(<MemoryRouter><ExecutionDiscussionDock goalId="goal-1" /></MemoryRouter>)
    await waitFor(() => expect(read).toHaveBeenCalledTimes(1))
    fireEvent.click(screen.getByRole('button', { name: '与 Solevia 讨论当前执行' }))
    fireEvent.change(screen.getByRole('textbox', { name: '执行讨论内容' }), { target: { value: '我想减轻一些强度，先讨论一下。' } })
    fireEvent.submit(screen.getByRole('textbox', { name: '执行讨论内容' }).closest('form')!)
    expect(send).toHaveBeenCalledTimes(1)
    if (closeWhilePending) fireEvent.click(screen.getByRole('button', { name: '关闭执行讨论' }))
    vi.useFakeTimers()
    await act(async () => { finish({ ...emptyWorkspace, messages: [
      { id: 'u', role: 'USER', content: '我想减轻一些强度，先讨论一下。', generation_status: 'SUCCEEDED' },
      { id: 'a', role: 'ASSISTANT', content: '可以先讨论调整建议，当前计划尚未改变。'.repeat(40), generation_status: 'SUCCEEDED' },
    ], latest_analysis: { intents: [], adjustment_signal: { recommended: true, reason: '时间变化', request: '减轻强度' } },
    } as unknown as api.ExecutionDiscussionWorkspace) })
    if (closeWhilePending) fireEvent.click(screen.getByRole('button', { name: '与 Solevia 讨论当前执行' }))
    await act(() => vi.advanceTimersByTimeAsync(2000))
    for (let index = 0; index < 3; index++) {
      fireEvent.click(screen.getByRole('button', { name: '关闭执行讨论' }))
      fireEvent.click(screen.getByRole('button', { name: '与 Solevia 讨论当前执行' }))
      expect(screen.queryByRole('button', { name: '显示全文' })).not.toBeInTheDocument()
      expect(document.querySelector('.assistant-answer')).toHaveAttribute('aria-busy', 'false')
      expect(screen.getByRole('button', { name: '生成可确认的调整建议' })).toBeVisible()
    }
    expect(send).toHaveBeenCalledTimes(1)
    expect(read).toHaveBeenCalledTimes(1)
    expect(revise).not.toHaveBeenCalled()
    expect(retry).not.toHaveBeenCalled()
    expect(preview).not.toHaveBeenCalled()
    expect(apply).not.toHaveBeenCalled()
  })

  it('preserves the draft and conversation when expanding and restoring the companion panel', async () => {
    const read = vi.spyOn(api, 'getExecutionDiscussion').mockResolvedValue({
      ...emptyWorkspace,
      messages: [{ id: 'answer-1', role: 'ASSISTANT', content: '先完成今天的观察记录。', generation_status: 'SUCCEEDED' }],
    } as unknown as api.ExecutionDiscussionWorkspace)
    render(<MemoryRouter><ExecutionDiscussionDock goalId="goal-1" /></MemoryRouter>)
    fireEvent.click(screen.getByRole('button', { name: '与 Solevia 讨论当前执行' }))
    expect(await screen.findByText('先完成今天的观察记录。')).toBeVisible()
    fireEvent.change(screen.getByRole('textbox', { name: '执行讨论内容' }), { target: { value: '还没发送的观察问题' } })
    fireEvent.click(screen.getByRole('button', { name: '展开执行讨论' }))
    expect(screen.getByLabelText('执行讨论')).toHaveClass('is-expanded')
    expect(screen.getByRole('textbox', { name: '执行讨论内容' })).toHaveValue('还没发送的观察问题')
    fireEvent.click(screen.getByRole('button', { name: '收起执行讨论' }))
    expect(screen.getByLabelText('执行讨论')).not.toHaveClass('is-expanded')
    expect(screen.getByRole('textbox', { name: '执行讨论内容' })).toHaveValue('还没发送的观察问题')
    expect(screen.getByText('先完成今天的观察记录。')).toBeVisible()
    expect(read).toHaveBeenCalledTimes(1)
  })

  it('does not navigate back to an old goal when its adjustment finishes after switching', async () => {
    let finish!: (workspace: api.PlanWorkspace) => void
    const pending = new Promise<api.PlanWorkspace>(resolve => { finish = resolve })
    vi.spyOn(api, 'getExecutionDiscussion').mockImplementation(async id => ({
      ...emptyWorkspace, goal_id: id, latest_analysis: {
        intents: [], adjustment_signal: { recommended: true, request: `调整${id}` },
      },
    }) as unknown as api.ExecutionDiscussionWorkspace)
    const revise = vi.spyOn(api, 'revisePlan').mockReturnValue(pending)
    function Location() { return <output aria-label="当前路径">{useLocation().pathname}</output> }
    const page = (id: string) => <MemoryRouter initialEntries={['/goals/goal-2/today']}>
      <ExecutionDiscussionDock key={id} goalId={id} /><Location />
    </MemoryRouter>
    const view = render(page('goal-1'))
    fireEvent.click(await screen.findByRole('button', { name: '与 Solevia 讨论当前执行' }))
    fireEvent.click(await screen.findByRole('button', { name: '生成可确认的调整建议' }))
    expect(revise).toHaveBeenCalledWith('goal-1', '调整goal-1')
    view.rerender(page('goal-2'))
    await act(async () => { finish({ messages: [{ generation_status: 'SUCCEEDED' }],
      pending_adjustment: { can_apply: true } } as unknown as api.PlanWorkspace) })
    expect(screen.getByLabelText('当前路径')).toHaveTextContent('/goals/goal-2/today')
    expect(screen.queryByRole('textbox', { name: '执行讨论内容' })).not.toBeInTheDocument()
  })
  it('keeps a discussion deep link open after initial goal state resets', async () => {
    vi.spyOn(api, 'getExecutionDiscussion').mockResolvedValue(emptyWorkspace as unknown as api.ExecutionDiscussionWorkspace)
    render(<MemoryRouter initialEntries={['/goals/goal-1/today?discussion=1']}><ExecutionDiscussionDock goalId="goal-1" /></MemoryRouter>)
    expect(await screen.findByRole('textbox', { name: '执行讨论内容' })).toBeVisible()
  })
  it('only appears on execution surfaces, not route or plan discussions', () => {
    expect(isExecutionDiscussionPath('/goals/goal-1/today')).toBe(true)
    expect(isExecutionDiscussionPath('/goals/goal-1/progress')).toBe(true)
    expect(isExecutionDiscussionPath('/goals/goal-1/route')).toBe(false)
    expect(isExecutionDiscussionPath('/goals/goal-1/plan')).toBe(false)
    expect(isExecutionDiscussionPath('/goals/goal-1/clarify')).toBe(false)
  })

  it('stores explicit discussion memory without asking for another confirmation', async () => {
    const remembered = {
      ...emptyWorkspace,
      messages: [
        { id: 'u1', sequence: 1, role: 'USER', content: '我发现早晨拍照状态更好', generation_status: 'SUCCEEDED', created_at: '2026-09-15T08:00:00Z' },
        { id: 'a1', sequence: 2, role: 'ASSISTANT', content: '这是一个值得保留的执行偏好。', generation_status: 'SUCCEEDED', created_at: '2026-09-15T08:00:01Z' },
      ],
      insights: [{ id: 'i1', insight_type: 'PREFERENCE', content: '早晨拍照状态更好', scope: 'GOAL', confidence: .9, status: 'CONFIRMED', source_message_id: 'u1', created_at: '2026-09-15T08:00:01Z', decided_at: '2026-09-15T08:00:01Z' }],
      latest_analysis: {
        assistant_message: '这是一个值得保留的执行偏好。',
        intents: [{ type: 'INSIGHT', summary: '偏好早晨练习', confidence: .9 }],
        memory_candidates: [],
        review_signal: progress.review_signal,
        adjustment_signal: { recommended: false, scope: null, reason: null, request: null },
        questions: [],
      },
    }
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockImplementation((input, init) => {
      const path = String(input)
      if (init?.method === 'POST' && path.endsWith('/discussion/messages')) {
        return Promise.resolve(new Response(JSON.stringify(remembered), { status: 200 }))
      }
      return Promise.resolve(new Response(JSON.stringify(emptyWorkspace), { status: 200 }))
    })

    render(<MemoryRouter><ExecutionDiscussionDock goalId="goal-1" /></MemoryRouter>)
    fireEvent.click(await screen.findByRole('button', { name: '与 Solevia 讨论当前执行' }))
    fireEvent.change(screen.getByRole('textbox', { name: '执行讨论内容' }), { target: { value: '我发现早晨拍照状态更好' } })
    fireEvent.click(screen.getByRole('button', { name: '发送' }))

    expect(await screen.findByText('早晨拍照状态更好')).toBeInTheDocument()
    expect(screen.queryByText('要让以后也记住吗？')).not.toBeInTheDocument()
    expect(screen.getByText('讨论中记住的内容 · 1')).toBeInTheDocument()
    expect(fetchMock).not.toHaveBeenCalledWith('/api/goals/goal-1/execution/insights/i1', expect.objectContaining({ method: 'PUT' }))
  })

  it('finishes the discussion loading state before opening a pending plan adjustment', async () => {
    const adjustmentWorkspace = {
      ...emptyWorkspace,
      latest_analysis: {
        assistant_message: '可以把训练调整为固定星期结构。',
        intents: [{ type: 'PLAN_CHANGE_REQUEST', summary: '调整训练日', confidence: .95 }],
        memory_candidates: [],
        review_signal: progress.review_signal,
        adjustment_signal: {
          recommended: true,
          scope: 'NEAR_TERM',
          reason: '训练日需要改成固定星期。',
          request: '周一胸、周二腿、周三休息、周四肩、周五背。',
        },
        questions: [],
      },
    }
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockImplementation((input, init) => {
      const path = String(input)
      if (init?.method === 'POST' && path.endsWith('/plan/revise')) {
        return Promise.resolve(new Response(JSON.stringify({
          messages: [{ generation_status: 'SUCCEEDED' }],
          pending_adjustment: { can_apply: true },
        }), { status: 200 }))
      }
      return Promise.resolve(new Response(JSON.stringify(adjustmentWorkspace), { status: 200 }))
    })

    render(<MemoryRouter><ExecutionDiscussionDock goalId="goal-1" /></MemoryRouter>)
    fireEvent.click(await screen.findByRole('button', { name: '与 Solevia 讨论当前执行' }))
    fireEvent.click(screen.getByRole('button', { name: '生成可确认的调整建议' }))

    expect(await screen.findByRole('button', { name: '与 Solevia 讨论当前执行' })).toBeInTheDocument()
    expect(screen.queryByText('正在整理…')).not.toBeInTheDocument()
    expect(fetchMock).toHaveBeenCalledWith(
      '/api/goals/goal-1/plan/revise',
      expect.objectContaining({ method: 'POST' }),
    )
  })

  it('updates an existing pending adjustment with the latest execution discussion', async () => {
    const adjustmentWorkspace = {
      ...emptyWorkspace,
      latest_analysis: {
        assistant_message: '可以调整训练结构。', intents: [], memory_candidates: [],
        review_signal: progress.review_signal,
        adjustment_signal: { recommended: true, scope: 'NEAR_TERM', reason: '需要调整', request: '调整训练结构。' },
        questions: [],
      },
    }
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockImplementation((input, init) => {
      const path = String(input)
      if (path.endsWith('/plan/revise') && init?.method === 'POST') {
        return Promise.resolve(new Response(JSON.stringify({
          pending_adjustment: { can_apply: true }, messages: [{ generation_status: 'SUCCEEDED' }],
        }), { status: 200 }))
      }
      return Promise.resolve(new Response(JSON.stringify(adjustmentWorkspace), { status: 200 }))
    })

    render(<MemoryRouter><ExecutionDiscussionDock goalId="goal-1" /></MemoryRouter>)
    fireEvent.click(await screen.findByRole('button', { name: '与 Solevia 讨论当前执行' }))
    fireEvent.click(screen.getByRole('button', { name: '生成可确认的调整建议' }))

    expect(await screen.findByRole('button', { name: '与 Solevia 讨论当前执行' })).toBeInTheDocument()
    expect(fetchMock.mock.calls.some(([input, options]) => (
      String(input).endsWith('/plan/revise') && options?.method === 'POST'
    ))).toBe(true)
    expect(fetchMock).toHaveBeenCalledWith('/api/goals/goal-1/plan/revise', expect.objectContaining({
      method: 'POST', body: JSON.stringify({ content: '调整训练结构。' }),
    }))
  })

  it('preserves a failed research request and continues an ordinary answer on explicit choice', async () => {
    const replied = {
      ...emptyWorkspace,
      messages: [{ id: 'a1', sequence: 2, role: 'ASSISTANT', content: '先结合当前计划继续推进。', generation_status: 'SUCCEEDED', created_at: '2026-09-15T08:00:01Z' }],
    }
    const request = {
      id: 'request-1', goal_id: 'goal-1', phase: 'EXECUTION', question: '今天进度比预期慢一些',
      status: 'AWAITING_DECISION', fallback: false, attempt_count: 0,
      run: { id: 'run-1', status: 'FAILED', current_stage: 'FAILED', checkpoint: {}, evidence: [], result: null },
    } as unknown as api.ResearchResponseRequest
    vi.spyOn(api, 'listResearchResponses').mockResolvedValue([])
    vi.spyOn(api, 'createResearchResponse').mockResolvedValue(request)
    const action = vi.spyOn(api, 'actResearchResponse').mockResolvedValue({ ...request, status: 'COMPLETED', response_message_id: 'a1', fallback: true, attempt_count: 1 })
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockImplementation((input, init) => {
      const path = String(input)
      if (init?.method === 'POST' && path.endsWith('/discussion/messages')) {
        return Promise.resolve(new Response(JSON.stringify(replied), { status: 200 }))
      }
      return Promise.resolve(new Response(JSON.stringify(emptyWorkspace), { status: 200 }))
    })

    render(<MemoryRouter><ExecutionDiscussionDock goalId="goal-1" /></MemoryRouter>)
    fireEvent.click(await screen.findByRole('button', { name: '与 Solevia 讨论当前执行' }))
    fireEvent.click(screen.getByRole('button', { name: '开启联网检索' }))
    fireEvent.change(screen.getByRole('textbox', { name: '执行讨论内容' }), { target: { value: '今天进度比预期慢一些' } })
    fireEvent.click(screen.getByRole('button', { name: '发送' }))
    expect(await screen.findByText('研究需要处理')).toBeInTheDocument()
    expect(action).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: '继续普通回答' }))
    vi.spyOn(api, 'getExecutionDiscussion').mockResolvedValue(replied as unknown as api.ExecutionDiscussionWorkspace)
    expect(await screen.findByText('先结合当前计划继续推进。')).toBeInTheDocument()
    expect(screen.queryByText('本次使用普通回答。')).not.toBeInTheDocument()
    expect(action).toHaveBeenCalledWith('goal-1', 'request-1', 'ordinary')
    expect(fetchMock.mock.calls.some(([path, options]) => String(path).endsWith('/discussion/messages') && options?.method === 'POST')).toBe(false)
  })

  it('regenerates a durable user turn when the assistant row is missing', async () => {
    const pending = {
      ...emptyWorkspace,
      messages: [{ id: 'u1', sequence: 1, role: 'USER', content: 'RSI 和 JEV 需要掌握吗？', generation_status: 'SUCCEEDED', created_at: '2026-09-15T08:00:00Z' }],
    }
    const replied = {
      ...pending,
      messages: [
        ...pending.messages,
        { id: 'a1', sequence: 2, role: 'ASSISTANT', content: '我会结合当前目标说明优先级。', generation_status: 'SUCCEEDED', created_at: '2026-09-15T08:00:01Z' },
      ],
    }
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockImplementation((input, init) => {
      const path = String(input)
      if (init?.method === 'POST' && path.endsWith('/discussion/messages')) {
        expect(JSON.parse(String(init.body))).toMatchObject({ content: 'RSI 和 JEV 需要掌握吗？', regenerate: true })
        return Promise.resolve(new Response(JSON.stringify(replied), { status: 200 }))
      }
      return Promise.resolve(new Response(JSON.stringify(pending), { status: 200 }))
    })

    render(<MemoryRouter><ExecutionDiscussionDock goalId="goal-1" /></MemoryRouter>)
    fireEvent.click(await screen.findByRole('button', { name: '与 Solevia 讨论当前执行' }))
    fireEvent.click(await screen.findByRole('button', { name: '继续生成回复' }))

    expect(await screen.findByText('我会结合当前目标说明优先级。')).toBeInTheDocument()
    expect(fetchMock).toHaveBeenCalledWith('/api/goals/goal-1/execution/discussion/messages', expect.objectContaining({ method: 'POST' }))
  })

  it('offers recovery when the assistant generation was interrupted', async () => {
    const pending = {
      ...emptyWorkspace,
      messages: [
        { id: 'u1', sequence: 1, role: 'USER', content: '刚才中断的问题', generation_status: 'SUCCEEDED', created_at: '2026-09-15T08:00:00Z' },
        { id: 'a1', sequence: 2, role: 'ASSISTANT', content: '', generation_status: 'GENERATING', created_at: '2026-09-15T08:00:01Z' },
      ],
    }
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response(JSON.stringify(pending), { status: 200 }))

    render(<MemoryRouter><ExecutionDiscussionDock goalId="goal-1" /></MemoryRouter>)
    fireEvent.click(await screen.findByRole('button', { name: '与 Solevia 讨论当前执行' }))

    expect(await screen.findByText('正在生成回复…')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '继续生成回复' })).toBeInTheDocument()
  })
})
