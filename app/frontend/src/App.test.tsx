import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import App from './App'

const workspace = {
  goal_id: 'goal-1',
  goal_title: '学习 Agent 开发',
  raw_input: '我想学习 Agent 开发',
  status: 'CLARIFYING',
  messages: [
    { id: 'm1', sequence: 1, role: 'USER', content: '我想学习 Agent 开发', generation_status: 'SUCCEEDED', created_at: '2026-09-01T00:00:00Z' },
    { id: 'm2', sequence: 2, role: 'ASSISTANT', content: '先了解你的当前基础。', generation_status: 'SUCCEEDED', created_at: '2026-09-01T00:00:01Z' },
  ],
  facts: [
    { id: 'f1', category: 'OBJECTIVE', field_key: 'objective', value: '我想学习 Agent 开发', source: 'USER', status: 'CONFIRMED', confidence: 1 },
  ],
  open_questions: ['你目前具备哪些基础？'],
  conflicts: [],
  readiness: { ready_for_review: false, blocking_fields: ['current_state'], reason: '信息不足' },
  requirement_draft: null,
  downstream_route_preserved: false,
  requirements_changed_since_route: false,
  planning_approach_recommendation: {
    recommended_mode: 'COMMITTED',
    reasons: ['目标和验收标准已经明确'],
    confidence: 'MEDIUM',
    explicit_trial_weeks: null,
  },
  selected_planning_mode: 'COMMITTED',
  schedule_type_recommendation: {
    recommended_type: 'RHYTHM',
    reasons: ['当前目标适合按固定频率持续推进'],
    confidence: 'MEDIUM',
  },
  selected_schedule_type: 'RHYTHM',
}

const routeWorkspace = {
  goal_id: 'goal-1',
  goal_title: '学习 Agent 开发',
  status: 'ROUTE_REVIEW',
  requirement_version: 1,
  requirement_snapshot: { objective: '我想学习 Agent 开发', available_capacity: '每周 10 小时' },
  route: {
    id: 'route-1',
    version: 1,
    revision: 1,
    status: 'DRAFT',
    summary: '通过基础、核心实践和成果验证三个阶段完成目标。',
    stages: [
      { id: 's1', order: 1, title: '建立基础', purpose: '补齐后续实践需要的基础。', topics: ['API 调用'], outputs: ['最小 API 示例'], depends_on: [], options: [] },
      {
        id: 's2', order: 2, title: '完成核心实践', purpose: '实现一个可以运行的 Agent。', topics: ['工具调用', '记忆'], outputs: ['可运行 Agent'], depends_on: [1],
        options: [
          { label: '先做最小版本', description: '只实现一次工具调用和简单记忆。', tradeoff: '覆盖面小，但更容易快速跑通。', recommended: true },
          { label: '一次覆盖完整能力', description: '同时实现多个工具和长期记忆。', tradeoff: '成果更完整，但复杂度更高。', recommended: false },
        ],
      },
      { id: 's3', order: 3, title: '验证并展示', purpose: '按照成功标准验证最终成果。', topics: ['测试', '演示'], outputs: ['公开项目'], depends_on: [2], options: [] },
    ],
    tradeoffs: ['暂不加入模型微调。'],
    assumptions: [],
    open_questions: [],
    optional_questions: [],
    change_summary: [],
    planning_mode: 'COMMITTED',
    schedule_type: 'RHYTHM',
  },
  messages: [
    { id: 'rm1', sequence: 1, role: 'ASSISTANT', content: '我根据需求整理了三个一级阶段。', generation_status: 'SUCCEEDED', created_at: '2026-09-01T00:01:00Z' },
  ],
  pending_requirement_version: null,
  requirements_changed_since_route: false,
  planning_mode: 'COMMITTED',
  schedule_type: 'RHYTHM',
}

afterEach(() => {
  cleanup()
  vi.useRealTimers()
  vi.restoreAllMocks()
  window.history.pushState({}, '', '/')
})

describe('App', () => {
  it('starts with the cross-plan Today dashboard', () => {
    render(<App />)

    expect(screen.getByRole('heading', { name: '今天要做什么' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '＋ 创建规划' })).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: '定时安排' })).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: '灵活推进' })).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: '＋ 创建规划' }))
    expect(screen.getByRole('menuitem', { name: /快速安排/ })).toBeInTheDocument()
    expect(screen.getByRole('menuitem', { name: /和 Solevia 一起规划/ })).toBeInTheDocument()
  })

  it('separates paused goals and shows their local-day countdown', async () => {
    const now = new Date()
    const resumeDate = new Date(now.getFullYear(), now.getMonth(), now.getDate() + 2)
    const resumeValue = `${resumeDate.getFullYear()}-${String(resumeDate.getMonth() + 1).padStart(2, '0')}-${String(resumeDate.getDate()).padStart(2, '0')}`
    const todayValue = `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, '0')}-${String(now.getDate()).padStart(2, '0')}`
    const timezone = Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC'
    vi.spyOn(globalThis, 'fetch').mockImplementation((input) => {
      if (['/api/goals', '/api/goals/status-check'].includes(String(input))) return Promise.resolve(new Response(JSON.stringify([
        { id: 'active-1', title: '正在学习', status: 'ACTIVE', timezone, pause_until: null, last_active_at: '2026-09-01T00:00:00Z', completed_at: null, archived_at: null },
        { id: 'paused-1', title: '两天后继续', status: 'PAUSED', timezone, pause_until: resumeValue, last_active_at: '2026-09-01T00:00:00Z', completed_at: null, archived_at: null },
        { id: 'paused-2', title: '暂未决定时间', status: 'PAUSED', timezone, pause_until: null, last_active_at: '2026-09-01T00:00:00Z', completed_at: null, archived_at: null },
        { id: 'resume-today', title: '今天自动恢复', status: 'PAUSED', timezone, pause_until: todayValue, last_active_at: '2026-09-01T00:00:00Z', completed_at: null, archived_at: null },
      ]), { status: 200 }))
      if (String(input) === '/api/today') return Promise.resolve(new Response(JSON.stringify({ date: '2026-09-11', timezone, fixed_tasks: [], flexible_tasks: [], conflicts: [] }), { status: 200 }))
      return Promise.resolve(new Response(JSON.stringify([]), { status: 200 }))
    })
    render(<App />)

    expect(await screen.findByText('两天后继续')).toBeInTheDocument()
    expect(screen.getByText('距离恢复还剩 2 天')).toBeInTheDocument()
    expect(screen.getByText('待你恢复')).toBeInTheDocument()
    const runningSection = screen.getByLabelText('正在进行')
    expect(within(runningSection).getByText('正在学习')).toBeInTheDocument()
    expect(within(runningSection).getByText('今天自动恢复')).toBeInTheDocument()
    expect(within(runningSection).getAllByText('正在执行')).toHaveLength(2)
    expect(within(runningSection).queryByText('两天后继续')).not.toBeInTheDocument()
    expect(document.querySelector('.paused-goals')).not.toHaveTextContent('今天自动恢复')
    expect(screen.queryByText('今天恢复')).not.toBeInTheDocument()
  })

  it('uses a starting point as an editable draft without creating a goal', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockImplementation(() =>
      Promise.resolve(new Response(JSON.stringify([]), { status: 200 })))
    window.history.pushState({}, '', '/new-agent')
    render(<App />)

    fireEvent.click(screen.getByRole('button', { name: '学习与成长 ↗' }))
    const input = screen.getByRole('textbox', { name: '目标描述' })
    expect(input).toHaveValue('我想学会一项新技能，但不知道从哪里开始。')
    expect(input).toHaveFocus()
    fireEvent.change(input, { target: { value: '我想学习烘焙，每周练习一次。' } })
    expect(input).toHaveValue('我想学习烘焙，每周练习一次。')
    expect(screen.getByRole('button', { name: '开始规划 →' })).toBeEnabled()
    expect(window.location.pathname).toBe('/new-agent')
    await waitFor(() => expect(fetchMock).toHaveBeenCalled())
    expect(fetchMock.mock.calls.some(([url]) => String(url) === '/api/goals/initialize')).toBe(false)
  })

  it('opens the discussion immediately while the first agent response is generated', async () => {
    const pendingWorkspace = {
      ...workspace,
      messages: [
        workspace.messages[0],
        { ...workspace.messages[1], content: 'Solevia 正在理解你的想法。', generation_status: 'PENDING', metadata: { initial_response: true } },
      ],
    }
    let resolveInitialReply!: (response: Response) => void
    const initialReply = new Promise<Response>((resolve) => { resolveInitialReply = resolve })
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockImplementation((input, options) => {
      const path = String(input)
      if (path === '/api/goals/initialize' && options?.method === 'POST') {
        return Promise.resolve(new Response(JSON.stringify(pendingWorkspace), { status: 201 }))
      }
      if (path.endsWith('/clarification/start') && options?.method === 'POST') {
        return initialReply
      }
      if (path.endsWith('/clarification')) {
        return Promise.resolve(new Response(JSON.stringify(pendingWorkspace), { status: 200 }))
      }
      if (path === '/api/goals') {
        return Promise.resolve(new Response(JSON.stringify([]), { status: 200 }))
      }
      return Promise.resolve(new Response(JSON.stringify(workspace), { status: 200 }))
    })
    window.history.pushState({}, '', '/new-agent')
    render(<App />)

    fireEvent.change(screen.getByRole('textbox', { name: '目标描述' }), {
      target: { value: '我想学习 Agent 开发' },
    })
    fireEvent.click(screen.getByRole('button', { name: '开始规划 →' }))

    await waitFor(() => expect(window.location.pathname).toBe('/goals/goal-1/clarify'))
    expect(await screen.findByText(/正在理解你的想法/)).toBeInTheDocument()
    await act(async () => { resolveInitialReply(new Response(JSON.stringify(workspace), { status: 200 })) })
    await waitFor(() => expect(screen.getByText('先了解你的当前基础。')).toBeInTheDocument())
    expect(screen.getByText('我想学习 Agent 开发', { selector: '.facts-preview p' })).toBeInTheDocument()
    expect(fetchMock).toHaveBeenCalledWith('/api/goals/initialize', expect.objectContaining({ method: 'POST' }))
    expect(fetchMock).toHaveBeenCalledWith('/api/goals/goal-1/clarification/start', expect.objectContaining({ method: 'POST' }))
  })

  it('sends a blocked task to recovery handling instead of scheduling it', async () => {
    const blockedTask = {
      id: 'task-2', source: 'AGENT', plan_id: 'goal-1', plan_title: '学习 Agent 开发',
      title: '编写 Provider 实现', description: '', estimate_minutes: 40, priority: 3,
      status: 'PENDING', scheduled_date: '2026-09-10', start_local: null, end_local: null,
      recurrence: 'ONCE', recurrence_weekdays: [], recurrence_end_date: null,
      timezone: 'Asia/Shanghai', is_fixed: false, parallel_confirmed: false,
      schedule_status: 'PLANNED', schedule_review_required: false, schedule_change_reason: null,
      dependencies: [{ reference: 'task-1', task_id: 'task-1', title: '设计 Provider 抽象接口', status: 'PENDING' }],
    }
    vi.spyOn(globalThis, 'fetch').mockImplementation((input) => {
      const path = String(input)
      if (path === '/api/today') return Promise.resolve(new Response(JSON.stringify({ date: '2026-09-10', timezone: 'Asia/Shanghai', fixed_tasks: [], flexible_tasks: [blockedTask], conflicts: [] }), { status: 200 }))
      return Promise.resolve(new Response(JSON.stringify([]), { status: 200 }))
    })
    render(<App />)

    const handleButton = await screen.findByRole('button', { name: '去处理' })
    expect(screen.queryByRole('button', { name: '安排时间' })).not.toBeInTheDocument()
    fireEvent.click(handleButton)
    expect(window.location.pathname).toBe('/goals/goal-1/progress')
    expect(window.location.search).toContain('recovery=1')
    expect(window.location.search).toContain('blocked_task=task-2')
  })

  it('opens the unified quick arrangement flow from the legacy URL', async () => {
    window.history.pushState({}, '', '/manual-plans/new')
    vi.spyOn(globalThis, 'fetch').mockImplementation(() => Promise.resolve(new Response(JSON.stringify([]), { status: 200 })))
    render(<App />)

    expect(screen.getByRole('heading', { name: '安排今天，或接下来几天' })).toBeInTheDocument()
    expect(screen.getByRole('textbox', { name: '想做哪些事？' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '一天' })).toBeInTheDocument()
    expect(window.location.pathname).toBe('/quick/new')
  })

  it('sends a clarification reply and refreshes remembered facts', async () => {
    const scrollToLatest = vi.fn()
    Object.defineProperty(HTMLElement.prototype, 'scrollIntoView', {
      configurable: true,
      value: scrollToLatest,
    })
    const updatedWorkspace = {
      ...workspace,
      messages: [
        ...workspace.messages,
        { id: 'm3', sequence: 3, role: 'USER', content: '会 Python 基础', generation_status: 'SUCCEEDED', created_at: '2026-09-01T00:00:02Z' },
        { id: 'm4', sequence: 4, role: 'ASSISTANT', content: '已经记下当前基础。', generation_status: 'SUCCEEDED', created_at: '2026-09-01T00:00:03Z' },
      ],
      facts: [
        ...workspace.facts,
        { id: 'f2', category: 'CURRENT_STATE', field_key: 'current_state', value: '会 Python 基础', source: 'USER', status: 'CONFIRMED', confidence: 1 },
      ],
    }
    window.history.pushState({}, '', '/goals/goal-1/clarify')
    vi.spyOn(globalThis, 'fetch').mockImplementation((input) => {
      const path = String(input)
      if (path.endsWith('/messages')) {
        return Promise.resolve(new Response(JSON.stringify(updatedWorkspace), { status: 200 }))
      }
      if (path === '/api/goals') {
        return Promise.resolve(new Response(JSON.stringify([]), { status: 200 }))
      }
      return Promise.resolve(new Response(JSON.stringify(workspace), { status: 200 }))
    })
    render(<App />)

    const reply = await screen.findByRole('textbox', { name: '给 Solevia 发送消息' })
    Object.defineProperty(reply, 'scrollHeight', { configurable: true, value: 128 })
    fireEvent.change(reply, { target: { value: '会 Python 基础' } })
    await waitFor(() => expect(reply).toHaveStyle({ height: '128px' }))
    expect(screen.queryByText('继续和 Solevia 聊聊')).not.toBeInTheDocument()
    fireEvent.keyDown(reply, { key: 'Enter' })

    await waitFor(() => expect(screen.getByText('已经记下当前基础。')).toBeInTheDocument())
    expect(screen.getByText('当前基础')).toBeInTheDocument()
    expect(screen.getByRole('complementary', { name: '目标已知信息' })).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: '先把目标想清楚' }).closest('.planner-stage-header')).not.toBeNull()
    expect(scrollToLatest).toHaveBeenCalled()
  })

  it('prefills a clarification instead of silently rejecting a proposed fact', async () => {
    const proposedWorkspace = {
      ...workspace,
      facts: [
        ...workspace.facts,
        {
          id: 'f-proposed', category: 'CONSTRAINT', field_key: 'constraints',
          value: '每周 4 小时不足以同时覆盖所有内容', source: 'ASSISTANT',
          status: 'PROPOSED', confidence: 0.7,
        },
      ],
    }
    window.history.pushState({}, '', '/goals/goal-1/clarify')
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockImplementation((input) => {
      const path = String(input)
      if (path === '/api/goals') {
        return Promise.resolve(new Response(JSON.stringify([]), { status: 200 }))
      }
      return Promise.resolve(new Response(JSON.stringify(proposedWorkspace), { status: 200 }))
    })
    render(<App />)

    const proposedText = await screen.findByText('每周 4 小时不足以同时覆盖所有内容')
    fireEvent.click(screen.getByRole('button', { name: '修改这条理解' }))

    expect(proposedText).toBeInTheDocument()
    expect(screen.getByText('原内容已带入对话框，补充后发送即可。')).toBeInTheDocument()
    expect(screen.getByRole('textbox', { name: '给 Solevia 发送消息' })).toHaveValue(
      '关于“现实限制”，你目前理解为：\n每周 4 小时不足以同时覆盖所有内容\n\n不准确的地方是：',
    )
    expect(fetchMock.mock.calls.some(([input, options]) =>
      String(input).includes('/facts/f-proposed') && options?.method === 'PATCH')).toBe(false)

    fireEvent.click(screen.getByRole('button', { name: '取消修改' }))
    expect(screen.getByRole('textbox', { name: '给 Solevia 发送消息' })).toHaveValue('')
    expect(proposedText).toBeInTheDocument()
  })

  it('retries a timed-out clarification without asking the user to resubmit', async () => {
    const failedWorkspace = {
      ...workspace,
      messages: [
        workspace.messages[0],
        { id: 'm2', sequence: 2, role: 'ASSISTANT', content: '模型服务响应超时。你的输入已经保存，可以直接重试，不需要重新填写。', generation_status: 'FAILED', metadata: { error_code: 'MODEL_TIMEOUT' }, created_at: '2026-09-03T02:32:34Z' },
      ],
    }
    const recoveredWorkspace = {
      ...workspace,
      messages: [
        workspace.messages[0],
        { ...workspace.messages[1], content: '已经恢复，我们继续确认当前基础。' },
      ],
    }
    window.history.pushState({}, '', '/goals/goal-1/clarify')
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockImplementation((input, options) => {
      const path = String(input)
      if (path.endsWith('/messages/retry') && options?.method === 'POST') {
        return Promise.resolve(new Response(JSON.stringify(recoveredWorkspace), { status: 200 }))
      }
      if (path === '/api/goals') return Promise.resolve(new Response(JSON.stringify([]), { status: 200 }))
      return Promise.resolve(new Response(JSON.stringify(failedWorkspace), { status: 200 }))
    })
    render(<App />)

    expect(await screen.findByText('Solevia 暂时没有完成这次回复，你刚才的内容已经保存，可以直接重试。')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '直接重试' }))

    expect(await screen.findByText('已经恢复，我们继续确认当前基础。')).toBeInTheDocument()
    expect(fetchMock).toHaveBeenCalledWith(
      '/api/goals/goal-1/messages/retry',
      expect.objectContaining({ method: 'POST' }),
    )
  })

  it('reconciles a completed clarification retry when the post response never returns', async () => {
    const failedWorkspace = {
      ...workspace,
      messages: [
        workspace.messages[0],
        { id: 'm2', sequence: 2, role: 'ASSISTANT', content: '模型服务响应超时。', generation_status: 'FAILED', metadata: { error_code: 'MODEL_TIMEOUT' }, created_at: '2026-09-03T02:32:34Z' },
      ],
    }
    const recoveredWorkspace = {
      ...workspace,
      messages: [
        workspace.messages[0],
        { ...workspace.messages[1], id: 'm2', content: '结果已经保存，页面自动恢复显示。' },
      ],
    }
    let retryStarted = false
    window.history.pushState({}, '', '/goals/goal-1/clarify')
    vi.spyOn(globalThis, 'fetch').mockImplementation((input, options) => {
      const path = String(input)
      if (path.endsWith('/messages/retry') && options?.method === 'POST') {
        retryStarted = true
        return new Promise<Response>(() => {})
      }
      if (path === '/api/goals') return Promise.resolve(new Response(JSON.stringify([]), { status: 200 }))
      return Promise.resolve(new Response(JSON.stringify(retryStarted ? recoveredWorkspace : failedWorkspace), { status: 200 }))
    })
    render(<App />)

    expect(await screen.findByRole('button', { name: '直接重试' })).toBeInTheDocument()
    vi.useFakeTimers()
    fireEvent.click(screen.getByRole('button', { name: '直接重试' }))
    expect(screen.getByRole('button', { name: '正在重试…' })).toBeDisabled()

    await act(async () => {
      await vi.advanceTimersByTimeAsync(1600)
    })

    fireEvent.click(screen.getByRole('button', { name: '显示全文' }))
    expect(screen.getByText('结果已经保存，页面自动恢复显示。')).toBeInTheDocument()
    expect(screen.getByRole('textbox', { name: '给 Solevia 发送消息' })).toBeEnabled()
    expect(screen.queryByRole('button', { name: '正在回应…' })).not.toBeInTheDocument()
  })

  it('reviews and edits a remembered requirement fact', async () => {
    const reviewWorkspace = {
      ...workspace,
      status: 'REQUIREMENT_REVIEW',
      requirement_draft: {
        id: 'r1',
        version: 1,
        status: 'DRAFT',
        snapshot: { objective: '我想学习 Agent 开发' },
      },
      readiness: { ready_for_review: true, blocking_fields: [], reason: '可以审核' },
    }
    const editedWorkspace = {
      ...reviewWorkspace,
      facts: [{ ...workspace.facts[0], value: '准备 Agent 应用开发实习' }],
    }
    window.history.pushState({}, '', '/goals/goal-1/clarify')
    vi.spyOn(globalThis, 'fetch').mockImplementation((input, options) => {
      const path = String(input)
      if (path.includes('/facts/') && options?.method === 'PATCH') {
        return Promise.resolve(new Response(JSON.stringify(editedWorkspace), { status: 200 }))
      }
      if (path === '/api/goals') {
        return Promise.resolve(new Response(JSON.stringify([]), { status: 200 }))
      }
      return Promise.resolve(new Response(JSON.stringify(reviewWorkspace), { status: 200 }))
    })
    render(<App />)

    await screen.findByRole('heading', { name: '我目前对目标的理解' })
    fireEvent.click(screen.getByRole('button', { name: '修改 目标' }))
    fireEvent.change(screen.getByRole('textbox', { name: '修改目标' }), {
      target: { value: '准备 Agent 应用开发实习' },
    })
    fireEvent.click(screen.getByRole('button', { name: '保存修改' }))

    await waitFor(() => {
      expect(screen.getAllByText('准备 Agent 应用开发实习').length).toBeGreaterThan(0)
    })
  })

  it('requires an explicit planning approach before confirming requirements', async () => {
    const reviewWorkspace = {
      ...workspace,
      status: 'REQUIREMENT_REVIEW',
      selected_planning_mode: null,
      requirement_draft: {
        id: 'r1',
        version: 1,
        status: 'DRAFT',
        snapshot: { objective: '我想学习 Agent 开发' },
      },
      readiness: { ready_for_review: true, blocking_fields: [], reason: '可以审核' },
    }
    window.history.pushState({}, '', '/goals/goal-1/clarify')
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockImplementation((input, options) => {
      const path = String(input)
      if (path.endsWith('/planning-approach') && options?.method === 'PUT') {
        return Promise.resolve(new Response(JSON.stringify({
          ...reviewWorkspace,
          selected_planning_mode: 'EXPLORATORY',
        }), { status: 200 }))
      }
      if (path === '/api/goals') {
        return Promise.resolve(new Response(JSON.stringify([]), { status: 200 }))
      }
      return Promise.resolve(new Response(JSON.stringify(reviewWorkspace), { status: 200 }))
    })
    render(<App />)

    const confirm = await screen.findByRole('button', {
      name: '确认需求，开始讨论路线 →',
    })
    expect(confirm).toBeDisabled()
    fireEvent.click(screen.getByRole('button', { name: /先试试看/ }))

    await waitFor(() => expect(confirm).toBeEnabled())
    expect(fetchMock).toHaveBeenCalledWith(
      '/api/goals/goal-1/planning-approach',
      expect.objectContaining({ method: 'PUT' }),
    )
  })

  it('confirms requirements and advances to route discussion', async () => {
    const reviewWorkspace = {
      ...workspace,
      status: 'REQUIREMENT_REVIEW',
      requirement_draft: {
        id: 'r1',
        version: 1,
        status: 'DRAFT',
        snapshot: { objective: '我想学习 Agent 开发' },
      },
      readiness: { ready_for_review: true, blocking_fields: [], reason: '可以审核' },
    }
    const confirmedWorkspace = {
      ...reviewWorkspace,
      status: 'ROUTE_DRAFTING',
      requirement_draft: { ...reviewWorkspace.requirement_draft, status: 'CONFIRMED' },
    }
    window.history.pushState({}, '', '/goals/goal-1/clarify')
    vi.spyOn(globalThis, 'fetch').mockImplementation((input, options) => {
      const path = String(input)
      if (path.endsWith('/requirements/confirm') && options?.method === 'POST') {
        return Promise.resolve(new Response(JSON.stringify(confirmedWorkspace), { status: 200 }))
      }
      if (path === '/api/goals') {
        return Promise.resolve(new Response(JSON.stringify([]), { status: 200 }))
      }
      if (path.endsWith('/route')) {
        return Promise.resolve(new Response(JSON.stringify(routeWorkspace), { status: 200 }))
      }
      return Promise.resolve(new Response(JSON.stringify(reviewWorkspace), { status: 200 }))
    })
    render(<App />)

    const confirmButton = await screen.findByRole('button', {
      name: '确认需求，开始讨论路线 →',
    })
    fireEvent.click(confirmButton)

    await screen.findByRole('heading', { name: '第一版整体路线' })
    expect(screen.getByRole('heading', { name: '完成核心实践' })).toBeInTheDocument()
    expect(window.location.pathname).toBe('/goals/goal-1/route')
  })

  it('shows a specific recoverable message when requirement confirmation conflicts', async () => {
    const reviewWorkspace = {
      ...workspace,
      status: 'REQUIREMENT_REVIEW',
      requirement_draft: {
        id: 'r1',
        version: 1,
        status: 'DRAFT',
        snapshot: { objective: '我想学习 Agent 开发' },
      },
      readiness: { ready_for_review: true, blocking_fields: [], reason: '可以审核' },
    }
    window.history.pushState({}, '', '/goals/goal-1/clarify')
    vi.spyOn(globalThis, 'fetch').mockImplementation((input, options) => {
      const path = String(input)
      if (path.endsWith('/requirements/confirm') && options?.method === 'POST') {
        return Promise.resolve(new Response(
          JSON.stringify({ detail: 'No requirement draft exists' }),
          { status: 409 },
        ))
      }
      if (path === '/api/goals') {
        return Promise.resolve(new Response(JSON.stringify([]), { status: 200 }))
      }
      return Promise.resolve(new Response(JSON.stringify(reviewWorkspace), { status: 200 }))
    })
    render(<App />)

    fireEvent.click(await screen.findByRole('button', { name: '确认需求，开始讨论路线 →' }))

    expect(await screen.findByRole('alert')).toHaveTextContent(
      '暂时没有找到整理好的目标，请刷新页面后重试。',
    )
  })

  it('revises a route from lightweight stage feedback', async () => {
    const revised = {
      ...routeWorkspace,
      route: {
        ...routeWorkspace.route,
        id: 'route-2',
        revision: 2,
        change_summary: ['压缩第二阶段，只保留最小 Agent 实现。'],
      },
      messages: [
        ...routeWorkspace.messages,
        { id: 'rm2', sequence: 2, role: 'USER', content: '第二阶段太难', generation_status: 'SUCCEEDED', created_at: '2026-09-01T00:02:00Z' },
        { id: 'rm3', sequence: 3, role: 'ASSISTANT', content: '我已经压缩第二阶段。', generation_status: 'SUCCEEDED', created_at: '2026-09-01T00:03:00Z' },
      ],
    }
    window.history.pushState({}, '', '/goals/goal-1/route')
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockImplementation((input, options) => {
      const path = String(input)
      if (path.endsWith('/route/revise') && options?.method === 'POST') {
        return Promise.resolve(new Response(JSON.stringify(revised), { status: 200 }))
      }
      if (path === '/api/goals') {
        return Promise.resolve(new Response(JSON.stringify([]), { status: 200 }))
      }
      return Promise.resolve(new Response(JSON.stringify(routeWorkspace), { status: 200 }))
    })
    render(<App />)

    await screen.findByRole('heading', { name: '完成核心实践' })
    expect(screen.getByRole('complementary', { name: '路线依据' })).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: '讨论一级路线' }).closest('.planner-stage-header')).not.toBeNull()
    fireEvent.click(screen.getAllByRole('button', { name: '太多 / 太难' })[1])
    expect(
      (screen.getByRole('textbox', { name: '哪里需要调整？' }) as HTMLTextAreaElement).value,
    ).toContain('阶段 2')
    fireEvent.click(screen.getByRole('button', { name: '提交调整需求' }))

    await screen.findByText('压缩第二阶段，只保留最小 Agent 实现。')
    expect(screen.getByText('第 2 次调整')).toBeInTheDocument()
    expect(fetchMock).toHaveBeenCalledWith(
      '/api/goals/goal-1/route/revise',
      expect.objectContaining({ method: 'POST' }),
    )
  })

  it('turns a route option into editable feedback without forcing a choice', async () => {
    window.history.pushState({}, '', '/goals/goal-1/route')
    vi.spyOn(globalThis, 'fetch').mockImplementation((input) => {
      if (String(input) === '/api/goals') {
        return Promise.resolve(new Response(JSON.stringify([]), { status: 200 }))
      }
      return Promise.resolve(new Response(JSON.stringify(routeWorkspace), { status: 200 }))
    })
    render(<App />)

    const option = await screen.findByRole('button', { name: /先做最小版本/ })
    expect(screen.getByText('建议默认')).toBeInTheDocument()
    fireEvent.click(option)

    expect(
      (screen.getByRole('textbox', { name: '哪里需要调整？' }) as HTMLTextAreaElement).value,
    ).toContain('我倾向选择「先做最小版本」')
    fireEvent.click(screen.getByRole('button', { name: '只做解释' }))
    expect(screen.getByRole('textbox', { name: '想了解什么？' })).toHaveValue('')
    fireEvent.click(screen.getByRole('button', { name: /M2 · 完成核心实践/ }))
    expect((screen.getByRole('textbox', { name: '想了解什么？' }) as HTMLTextAreaElement).value).toContain('阶段 2')
    fireEvent.click(screen.getByRole('button', { name: '修改路线' }))
    expect((screen.getByRole('textbox', { name: '哪里需要调整？' }) as HTMLTextAreaElement).value).toContain('我倾向选择「先做最小版本」')
  })

  it('offers editable route ordering and replacement feedback without applying it immediately', async () => {
    window.history.pushState({}, '', '/goals/goal-1/route')
    vi.spyOn(globalThis, 'fetch').mockImplementation((input) => {
      if (String(input) === '/api/goals') {
        return Promise.resolve(new Response(JSON.stringify([]), { status: 200 }))
      }
      return Promise.resolve(new Response(JSON.stringify(routeWorkspace), { status: 200 }))
    })
    render(<App />)

    const stageHeading = await screen.findByRole('heading', { name: '完成核心实践' })
    const stageCard = stageHeading.closest('article')
    expect(stageCard).not.toBeNull()
    const stage = within(stageCard as HTMLElement)
    fireEvent.click(stage.getByText('更多调整'))

    expect(stage.getByRole('button', { name: '提前这一阶段' })).toBeEnabled()
    expect(stage.getByRole('button', { name: '延后这一阶段' })).toBeEnabled()
    expect(stage.getByRole('button', { name: '删除这一阶段' })).toBeEnabled()
    expect(stage.getByRole('button', { name: '替换这一阶段' })).toBeEnabled()
    expect(stage.getByText(/会在下方生成一段大致的调整需求/)).toBeInTheDocument()
    fireEvent.click(stage.getByRole('button', { name: '提前这一阶段' }))

    const feedbackInput = screen.getByRole('textbox', { name: '哪里需要调整？' }) as HTMLTextAreaElement
    expect(feedbackInput.value).toContain('适当前移')
    await waitFor(() => expect(feedbackInput).toHaveFocus())
    expect(screen.getByText('初始路线')).toBeInTheDocument()
  })

  it('answers a route explanation without creating a new revision', async () => {
    const explainedWorkspace = {
      ...routeWorkspace,
      messages: [
        ...routeWorkspace.messages,
        { id: 'rm2', sequence: 2, role: 'USER', content: '为什么第二阶段这样安排？', generation_status: 'SUCCEEDED', created_at: '2026-09-01T00:02:00Z' },
        { id: 'rm3', sequence: 3, role: 'ASSISTANT', content: '因为它依赖第一阶段，并为最终验证提供输入。路线没有修改。', generation_status: 'SUCCEEDED', created_at: '2026-09-01T00:03:00Z' },
      ],
    }
    window.history.pushState({}, '', '/goals/goal-1/route')
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockImplementation((input, options) => {
      const path = String(input)
      if (path.endsWith('/route/explain') && options?.method === 'POST') {
        return Promise.resolve(new Response(JSON.stringify(explainedWorkspace), { status: 200 }))
      }
      if (path === '/api/goals') {
        return Promise.resolve(new Response(JSON.stringify([]), { status: 200 }))
      }
      return Promise.resolve(new Response(JSON.stringify(routeWorkspace), { status: 200 }))
    })
    render(<App />)

    await screen.findByRole('heading', { name: '完成核心实践' })
    fireEvent.click(screen.getAllByRole('button', { name: '需要解释' })[1])
    expect(screen.getByRole('button', { name: '只做解释' })).toHaveClass('selected')
    fireEvent.click(screen.getByRole('button', { name: '发送问题' }))

    await screen.findByText('因为它依赖第一阶段，并为最终验证提供输入。路线没有修改。')
    expect(screen.getByText('初始路线')).toBeInTheDocument()
    expect(fetchMock).toHaveBeenCalledWith(
      '/api/goals/goal-1/route/explain',
      expect.objectContaining({ method: 'POST' }),
    )
  })

  it('retries a failed route response without making the user retype it', async () => {
    const failedWorkspace = {
      ...routeWorkspace,
      messages: [
        ...routeWorkspace.messages,
        { id: 'rm2', sequence: 2, role: 'USER', content: '请缩小第二阶段', generation_status: 'SUCCEEDED', metadata: { intent: 'REVISE' }, created_at: '2026-09-01T00:02:00Z' },
        { id: 'rm3', sequence: 3, role: 'ASSISTANT', content: '模型暂时没有返回可用路线，请稍后重试。', generation_status: 'FAILED', metadata: { intent: 'REVISE' }, created_at: '2026-09-01T00:03:00Z' },
      ],
    }
    const recoveredWorkspace = {
      ...failedWorkspace,
      route: { ...failedWorkspace.route, id: 'route-2', revision: 2, change_summary: ['第二阶段已缩小。'] },
      messages: [
        ...failedWorkspace.messages.slice(0, -1),
        { ...failedWorkspace.messages.at(-1), content: '已经缩小第二阶段。', generation_status: 'SUCCEEDED' },
      ],
    }
    window.history.pushState({}, '', '/goals/goal-1/route')
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockImplementation((input, options) => {
      const path = String(input)
      if (path.endsWith('/route/revise') && options?.method === 'POST') {
        return Promise.resolve(new Response(JSON.stringify(recoveredWorkspace), { status: 200 }))
      }
      if (path === '/api/goals') return Promise.resolve(new Response(JSON.stringify([]), { status: 200 }))
      return Promise.resolve(new Response(JSON.stringify(failedWorkspace), { status: 200 }))
    })
    render(<App />)

    await screen.findByRole('heading', { name: '完成核心实践' })
    fireEvent.click(screen.getByRole('button', { name: /路线讨论/ }))
    expect(screen.getByText('这次没有完成，但当前路线没有变化')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '重新尝试' }))

    await screen.findByText('第二阶段已缩小。')
    expect(fetchMock).toHaveBeenCalledWith(
      '/api/goals/goal-1/route/revise',
      expect.objectContaining({ method: 'POST', body: JSON.stringify({ content: '请缩小第二阶段' }) }),
    )
  })

  it('shows a relative plan by stage and expands into clear daily work', async () => {
    const planWorkspace = {
      goal_id: 'goal-1',
      goal_title: '学习 Agent 开发',
      status: 'PLAN_REVIEW',
      requirement_version: 1,
      requirement_snapshot: { available_capacity: '每周 10 小时', success_criteria: '完成可运行项目' },
      route_version: 1,
      route_revision: 1,
      generation_phase: 'COMPLETE',
      generated_weeks: 3,
      total_weeks: 3,
      plan: {
        id: 'plan-1', route_version_id: 'route-1', version: 1, revision: 1, status: 'DRAFT',
        summary: '先打好基础，再完成项目，建议按三个相对周推进。',
        total_weeks: 3, weekly_execution_days: 2, weekly_minutes: 180, total_estimated_minutes: 120,
        buffer_strategy: ['每周保留未排满容量。'], assumptions: ['启动时再绑定日期。'], open_questions: [],
        milestones: [
          { id: 'pm1', route_stage_id: 's1', stage_order: 1, title: '建立基础', purpose: '准备核心实践需要的基础。', start_week: 1, end_week: 1, outputs: ['基础清单'] },
        ],
        days: [
          { id: 'pd1', stage_order: 1, week_index: 1, day_index: 1, primary_goal: '完成一份可以检查的基础清单', suggested_minutes: 90, evidence: ['基础清单'], do_not: ['不要扩大范围'], tasks: [
            { id: 'pt1', order: 1, title: '整理当前基础', description: '列出已经会和还不会的内容。', estimate_minutes: 60, priority: 1, task_kind: 'PREPARE', is_required: true, evidence: ['基础清单'], depends_on: [], status: 'NOT_STARTED' },
          ] },
        ],
      },
    }
    window.history.pushState({}, '', '/goals/goal-1/plan')
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockImplementation((input) => {
      const path = String(input)
      if (path === '/api/goals') return Promise.resolve(new Response(JSON.stringify([]), { status: 200 }))
      if (path.endsWith('/route')) return Promise.resolve(new Response(JSON.stringify({ ...routeWorkspace, status: 'PLAN_REVIEW', route: { ...routeWorkspace.route, status: 'CONFIRMED' } }), { status: 200 }))
      if (path.endsWith('/plan/revise')) return Promise.resolve(new Response(JSON.stringify({
        ...planWorkspace,
        messages: [
          { id: 'pm1', sequence: 1, role: 'USER', content: '请只调整第 1 周', generation_status: 'SUCCEEDED', created_at: '2026-09-02T00:00:00Z' },
          { id: 'pm2', sequence: 2, role: 'ASSISTANT', content: '建议局部调轻第一周。', generation_status: 'SUCCEEDED', metadata: { intent: 'REVISE' }, created_at: '2026-09-02T00:00:01Z' },
        ],
        pending_adjustment: {
          assistant_message: '建议局部调轻第一周。',
          user_intent_summary: '第 1 周变轻',
          scope: 'WEEK',
          affected_stage_orders: [1],
          affected_weeks: [1],
          downstream_affected_weeks: [],
          proposed_changes: ['保留必做内容，移动可选任务。'],
          tradeoffs: ['当周覆盖面减少。'],
          unchanged_guarantees: ['其他周和一级路线保持不变。'],
          total_weeks_change: 0,
          weekly_intensity_change: 'LOWER',
          buffer_change: 'UNCHANGED',
          needs_clarification: false,
          clarifying_question: null,
          can_apply: true,
        },
      }), { status: 200 }))
      if (path.endsWith('/plan/adjustment/preview')) return Promise.resolve(new Response(JSON.stringify({
        id: 'preview-1', goal_id: 'goal-1', source_plan_version_id: 'plan-1', status: 'PREVIEW', fingerprint: 'fp',
        plan: planWorkspace.plan, schedule: [{ title: '整理当前基础', date: '2026-09-10', start: '19:00', end: '20:00', minutes: 60, state: 'SCHEDULED' }],
        unallocated_count: 0, created_at: '2026-09-02T00:00:01Z',
      }), { status: 200 }))
      if (path.endsWith('/plan/adjustment/apply')) return Promise.resolve(new Response(JSON.stringify({
        ...planWorkspace,
        messages: [
          { id: 'pm3', sequence: 3, role: 'ASSISTANT', content: '第一周已经调整。', generation_status: 'SUCCEEDED', metadata: { intent: 'APPLY', plan_revision: 2, regenerated_weeks: [1], change_summary: ['第一周减少可选任务。'] }, created_at: '2026-09-02T00:00:02Z' },
        ],
        pending_adjustment: null,
        revision_history: [
          { id: 'plan-2', version: 1, revision: 2, status: 'DRAFT', change_summary: ['第一周减少可选任务。'], feedback: '第一周变轻', created_at: '2026-09-02T00:00:02Z' },
          { id: 'plan-1', version: 1, revision: 1, status: 'SUPERSEDED', change_summary: [], feedback: null, created_at: '2026-09-01T00:00:00Z' },
        ],
        plan: { ...planWorkspace.plan, id: 'plan-2', revision: 2, change_summary: ['第一周减少可选任务。'] },
      }), { status: 200 }))
      if (path.includes('/plan/versions/plan-1/restore')) return Promise.resolve(new Response(JSON.stringify({
        ...planWorkspace,
        messages: [],
        pending_adjustment: null,
        revision_history: [
          { id: 'plan-3', version: 1, revision: 3, status: 'DRAFT', change_summary: ['恢复了计划 v1.1 的完整内容。'], feedback: '恢复计划 v1.1', created_at: '2026-09-02T00:00:03Z' },
          { id: 'plan-2', version: 1, revision: 2, status: 'SUPERSEDED', change_summary: ['第一周减少可选任务。'], feedback: '第一周变轻', created_at: '2026-09-02T00:00:02Z' },
          { id: 'plan-1', version: 1, revision: 1, status: 'SUPERSEDED', change_summary: [], feedback: null, created_at: '2026-09-01T00:00:00Z' },
        ],
        plan: { ...planWorkspace.plan, id: 'plan-3', revision: 3, change_summary: ['恢复了计划 v1.1 的完整内容。'] },
      }), { status: 200 }))
      if (path.endsWith('/plan/adjustment/discard')) return Promise.resolve(new Response(JSON.stringify({ ...planWorkspace, messages: [], pending_adjustment: null }), { status: 200 }))
      return Promise.resolve(new Response(JSON.stringify(planWorkspace), { status: 200 }))
    })
    render(<App />)

    expect(await screen.findByRole('heading', { name: '查看相对时间计划' })).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: '3 个相对周，不绑定具体日期' })).toBeInTheDocument()
    const stageSummary = screen.getByRole('heading', { name: '建立基础' }).closest('summary')
    expect(stageSummary).not.toBeNull()
    fireEvent.click(stageSummary as HTMLElement)
    expect(stageSummary?.parentElement).toHaveAttribute('open')
    expect(screen.getByText('完成一份可以检查的基础清单')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '按周' }))
    expect(screen.getByRole('heading', { name: '第 1 周' })).toBeInTheDocument()
    expect(screen.queryByText(/2026年|9月/)).not.toBeInTheDocument()
    expect(screen.queryByRole('link', { name: /开始执行/ })).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '前往“讨论路线”阶段' }))
    expect(await screen.findByText('正在回看时间计划采用的路线')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '前往“安排时间”阶段' }))
    expect(await screen.findByRole('heading', { name: '查看相对时间计划' })).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '写反馈' }))
    fireEvent.click(screen.getByRole('button', { name: '每周安排太密' }))
    const planFeedback = screen.getByRole('textbox', { name: '希望怎样调整？' }) as HTMLTextAreaElement
    expect(planFeedback.value).toContain('降低每周强度')
    await waitFor(() => expect(planFeedback).toHaveFocus())
    fireEvent.click(screen.getByRole('button', { name: '只做解释' }))
    expect(screen.queryByRole('button', { name: '每周安排太密' })).not.toBeInTheDocument()
    expect(screen.getByRole('textbox', { name: '想了解什么？' })).toHaveValue('')
    fireEvent.click(screen.getByRole('button', { name: /M1 · 建立基础/ }))
    expect((screen.getByRole('textbox', { name: '想了解什么？' }) as HTMLTextAreaElement).value).toContain('阶段 M1')
    expect(screen.getByRole('button', { name: '发送' })).toBeEnabled()
    fireEvent.click(screen.getByRole('button', { name: '发送' }))
    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith(
      '/api/goals/goal-1/plan/explain',
      expect.objectContaining({ method: 'POST' }),
    ))
    fireEvent.click(screen.getByRole('button', { name: '调整计划' }))
    expect((screen.getByRole('textbox', { name: '希望怎样调整？' }) as HTMLTextAreaElement).value).toContain('降低每周强度')
    expect(screen.getByRole('button', { name: '发送' })).toBeEnabled()
    fireEvent.click(screen.getByRole('button', { name: '按周' }))
    fireEvent.click(screen.getByRole('button', { name: 'W1 · 调整第 1 周' }))
    expect((screen.getByRole('textbox', { name: '希望怎样调整？' }) as HTMLTextAreaElement).value).toContain('只调整第 1 周')
    fireEvent.click(screen.getByRole('button', { name: '发送' }))
    expect(await screen.findByText('Solevia 的调整建议')).toBeInTheDocument()
    expect(screen.getByText('保留必做内容，移动可选任务。')).toBeInTheDocument()
    expect(screen.getByText('本次直接调整：调整选中的一周')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '先预览日期与时间' })).toBeEnabled()
    fireEvent.click(screen.getByRole('button', { name: '补充具体调整意见' }))
    expect(screen.getByRole('textbox', { name: '希望怎样调整？' })).toHaveValue('')
    fireEvent.change(screen.getByRole('textbox', { name: '希望怎样调整？' }), { target: { value: '' } })
    fireEvent.click(screen.getByRole('button', { name: '先预览日期与时间' }))
    expect(await screen.findByText('调整后的时间安排')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '确认并发布调整' }))
    expect(await screen.findByRole('heading', { name: '这份安排包含的更新' })).toBeInTheDocument()
    expect(screen.getByText('第一周减少可选任务。')).toBeInTheDocument()
    expect(screen.getByText('第 1 次调整前')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '恢复这份安排' }))
    expect(await screen.findByRole('heading', { name: '这份安排包含的更新' })).toBeInTheDocument()
    expect(fetchMock).toHaveBeenCalledWith(
      '/api/goals/goal-1/plan/versions/plan-1/restore',
      expect.objectContaining({ method: 'POST' }),
    )
  })

  it('separates a pending execution adjustment from the next exploratory window', async () => {
    const pending = {
      assistant_message: '固定星期结构已经整理成待确认建议。',
      user_intent_summary: '周一胸、周二腿、周三休息、周四肩、周五背。',
      scope: 'STAGE',
      affected_stage_orders: [1],
      affected_weeks: [1, 2],
      downstream_affected_weeks: [3, 4, 5, 6, 7, 8],
      proposed_changes: ['训练改成固定星期结构。'],
      tradeoffs: [], unchanged_guarantees: ['已完成内容保持不变。'],
      total_weeks_change: 0, weekly_intensity_change: 'UNCHANGED', buffer_change: 'UNCHANGED',
      target_schedule_type: 'RHYTHM', needs_clarification: false, clarifying_question: null, can_apply: true,
    }
    const days = [1, 2].map((week) => ({
      id: `day-${week}`, stage_order: 1, week_index: week, day_index: 1,
      primary_goal: `第 ${week} 周训练`, suggested_minutes: 45, evidence: [], do_not: [], tasks: [],
    }))
    const exploratoryWorkspace = {
      goal_id: 'goal-1', goal_title: '健身', status: 'ACTIVE',
      requirement_version: 1, requirement_snapshot: {}, route_version: 1, route_revision: 1,
      generation_phase: 'COMPLETE', generated_weeks: 2, total_weeks: 8, generation_target_weeks: 2,
      planning_mode: 'EXPLORATORY', schedule_type: 'RHYTHM', exploration_windows: [],
      pending_exploration_window: null, can_extend_exploration: false,
      available_generation_modes: ['STANDARD'], generation_job: null,
      messages: [], revision_history: [], pending_adjustment: pending,
      plan: {
        id: 'plan-1', route_version_id: 'route-1', version: 1, revision: 1, status: 'CONFIRMED',
        summary: '先体验固定训练节奏。', total_weeks: 8, weekly_execution_days: 4,
        weekly_minutes: 180, total_estimated_minutes: 360, planning_mode: 'EXPLORATORY',
        schedule_type: 'RHYTHM', exploration_window_weeks: 2, next_unplanned_week: 3,
        buffer_strategy: [], assumptions: [], open_questions: [], change_summary: [],
        milestones: [{ id: 'm1', route_stage_id: 's1', stage_order: 1, title: '建立训练节奏', purpose: '先试两周。', start_week: 1, end_week: 2, outputs: [] }],
        days,
      },
    }
    window.history.pushState({}, '', '/goals/goal-1/plan?discussion=1&adjustment=1')
    vi.spyOn(globalThis, 'fetch').mockImplementation((input) => {
      if (String(input) === '/api/goals') return Promise.resolve(new Response(JSON.stringify([]), { status: 200 }))
      return Promise.resolve(new Response(JSON.stringify(exploratoryWorkspace), { status: 200 }))
    })

    render(<App />)

    expect(await screen.findByRole('heading', { name: '调整建议已经整理好，但尚未改变当前计划' })).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: '当前体验安排已展开到第 2 周' })).toBeInTheDocument()
    expect(screen.getByText(/不代表调整要等到第 3 周才生效/)).toBeInTheDocument()
    expect(screen.getByText('Solevia 的调整建议')).toBeInTheDocument()
    expect(screen.queryByText('下一阶段从第 3 周开始')).not.toBeInTheDocument()
  })

  it('starts standard generation and polls until all weekly details are available', async () => {
    const base = {
      goal_id: 'goal-1', goal_title: '学习 Agent 开发', status: 'PLAN_DRAFTING',
      requirement_version: 1, requirement_snapshot: { available_capacity: '每周 10 小时' },
      route_version: 1, route_revision: 1,
    }
    const outline = {
      ...base, generation_phase: 'DETAILS', generated_weeks: 0, total_weeks: 3,
      plan: {
        id: 'plan-1', route_version_id: 'route-1', version: 1, revision: 1, status: 'GENERATING',
        summary: '三周相对计划。', total_weeks: 3, weekly_execution_days: 2,
        weekly_minutes: 180, total_estimated_minutes: 0, buffer_strategy: ['保留缓冲'],
        assumptions: [], open_questions: [],
        milestones: [{ id: 'pm1', route_stage_id: 's1', stage_order: 1, title: '建立基础', purpose: '完成基础准备。', start_week: 1, end_week: 3, outputs: ['基础清单'] }],
        days: [],
      },
    }
    const complete = {
      ...outline, status: 'PLAN_REVIEW', generation_phase: 'COMPLETE', generated_weeks: 3,
      plan: { ...outline.plan, status: 'DRAFT' }, generation_job: {
        id: 'job-1', mode: 'STANDARD', status: 'COMPLETED', phase: 'COMPLETE',
        generated_weeks: 3, total_weeks: 3, error_code: null, error_message: null,
      },
    }
    const initial = {
      ...base, generation_phase: 'OUTLINE', generated_weeks: 0, total_weeks: 0,
      plan: null, available_generation_modes: ['STANDARD', 'DEEP'], generation_job: null,
    }
    const queued = {
      ...initial, generation_job: {
        id: 'job-1', mode: 'STANDARD', status: 'QUEUED', phase: 'OUTLINE',
        generated_weeks: 0, total_weeks: 0, error_code: null, error_message: null,
      },
    }
    window.history.pushState({}, '', '/goals/goal-1/plan')
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockImplementation((input, options) => {
      const path = String(input)
      if (path === '/api/goals') return Promise.resolve(new Response(JSON.stringify([]), { status: 200 }))
      if (path.endsWith('/plan/generation/start') && options?.method === 'POST') {
        return Promise.resolve(new Response(JSON.stringify(queued), { status: 200 }))
      }
      const planReads = fetchMock.mock.calls.filter(([request, requestOptions]) => String(request).endsWith('/plan') && !requestOptions?.method).length
      return Promise.resolve(new Response(JSON.stringify(planReads <= 1 ? initial : complete), { status: 200 }))
    })
    render(<App />)

    expect(await screen.findByRole('heading', { name: '选择计划生成方式' })).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: /标准生成/ }))
    expect(await screen.findByRole('heading', { name: '查看相对时间计划' })).toBeInTheDocument()
    expect(fetchMock).toHaveBeenCalledWith(
      '/api/goals/goal-1/plan/generation/start',
      expect.objectContaining({ method: 'POST', body: JSON.stringify({ mode: 'STANDARD' }) }),
    )
  })

  it('shows initial plan read failure and allows an explicit retry', async () => {
    window.history.pushState({}, '', '/goals/goal-1/plan')
    let planReads = 0
    vi.spyOn(globalThis, 'fetch').mockImplementation((input) => {
      if (String(input).endsWith('/plan')) {
        planReads += 1
        if (planReads === 1) return Promise.reject(new TypeError('Failed to fetch'))
        return Promise.resolve(new Response(JSON.stringify({ goal_id: 'goal-1', goal_title: '测试规划',
          status: 'PLAN_DRAFTING', generation_phase: 'OUTLINE', plan: null,
          generation_job: null, messages: [], available_generation_modes: ['STANDARD'],
        }), { status: 200 }))
      }
      return Promise.resolve(new Response('[]', { status: 200 }))
    })
    render(<App />)
    expect(await screen.findByRole('heading', { name: '时间规划暂时无法打开' })).toBeInTheDocument()
    expect(screen.getByRole('alert')).toHaveTextContent('连接暂时中断')
    fireEvent.click(screen.getByRole('button', { name: '重新读取' }))
    expect(await screen.findByRole('heading', { name: '选择计划生成方式' })).toBeInTheDocument()
    expect(planReads).toBe(2)
  })

  it('shows a saved background-generation failure without implying that plan data was lost', async () => {
    const base = {
      goal_id: 'goal-1', goal_title: '准备考试', status: 'PLAN_DRAFTING',
      requirement_version: 1, requirement_snapshot: { available_capacity: '每周 10 小时' },
      route_version: 1, route_revision: 1, generation_phase: 'OUTLINE',
      generated_weeks: 0, total_weeks: 0, plan: null,
      available_generation_modes: ['STANDARD'],
      generation_job: {
        id: 'job-1', mode: 'STANDARD', status: 'FAILED', phase: 'OUTLINE',
        generated_weeks: 0, total_weeks: 0,
        error_code: 'MODEL_GATEWAY_TIMEOUT',
        error_message: '模型网关等待超时。需求和路线已经保存，可以稍后重试。',
      },
    }
    window.history.pushState({}, '', '/goals/goal-1/plan')
    vi.spyOn(globalThis, 'fetch').mockImplementation((input) => {
      const path = String(input)
      if (path === '/api/goals') return Promise.resolve(new Response(JSON.stringify([]), { status: 200 }))
      return Promise.resolve(new Response(JSON.stringify(base), { status: 200 }))
    })
    render(<App />)

    expect(await screen.findByRole('heading', { name: '选择计划生成方式' })).toBeInTheDocument()
    expect(screen.getByText('模型网关等待超时。需求和路线已经保存，可以稍后重试。')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /标准生成/ })).toBeEnabled()
  })

  it('shows automatic correction progress while background generation is running', async () => {
    const correction = {
      goal_id: 'goal-1', goal_title: '准备考试', status: 'PLAN_DRAFTING',
      generation_phase: 'DETAILS', generated_weeks: 1, generation_target_weeks: 4,
      plan: { id: 'plan-1' }, messages: [],
      generation_job: { id: 'job-1', mode: 'STANDARD', status: 'RUNNING' },
      generation_repair: { week_index: 2, attempt: 2, maximum_attempts: 3,
        message: '第 2 周的安排未通过约束校验，正在让模型重新安排。已完成内容保留。' },
    }
    window.history.pushState({}, '', '/goals/goal-1/plan')
    vi.spyOn(globalThis, 'fetch').mockImplementation((input) => Promise.resolve(
      new Response(JSON.stringify(String(input) === '/api/goals' ? [] : correction), { status: 200 }),
    ))
    render(<App />)
    expect(await screen.findByRole('heading', { name: '正在修正安排' })).toBeInTheDocument()
    expect(screen.getByText(correction.generation_repair.message)).toBeInTheDocument()
    expect(screen.getByText('已经安排好 1 / 4 周 · 第 2 / 3 次生成')).toBeInTheDocument()
  })

  it('offers bounded capacity retry and a non-mutating requirements review', async () => {
    const failure = {
      goal_id: 'goal-1', goal_title: '准备考试', status: 'PLAN_DRAFTING',
      generation_phase: 'DETAILS', generated_weeks: 1, generation_target_weeks: 4,
      plan: { id: 'plan-1' }, messages: [], available_generation_modes: ['STANDARD'],
      generation_job: { id: 'job-1', mode: 'STANDARD', status: 'FAILED',
        error_code: 'PLAN_TEMPORAL_SEGMENT_MINUTES_INVALID',
        error_message: '自动调整后，第 2 周的草稿预计需要 135 分钟，可用时间为 85 分钟。已完成周保留。' },
    }
    const viewingWorkspace = {
      ...workspace, status: 'PLAN_DRAFTING',
      requirement_draft: { id: 'r1', version: 1, status: 'CONFIRMED',
        snapshot: { objective: '准备考试', available_capacity: '每周 85 分钟' } },
      downstream_route_preserved: true, requirements_changed_since_route: false,
    }
    window.history.pushState({}, '', '/goals/goal-1/plan')
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockImplementation((input) => {
      const path = String(input)
      return Promise.resolve(new Response(JSON.stringify(path === '/api/goals' ? []
        : path.endsWith('/clarification') ? viewingWorkspace : failure), { status: 200 }))
    })
    render(<App />)
    expect(await screen.findByRole('heading', { name: '继续生成剩余计划' })).toBeInTheDocument()
    expect(screen.getByRole('alert')).toHaveTextContent('135 分钟')
    expect(screen.getByRole('button', { name: /继续自动修正/ })).toBeEnabled()
    fireEvent.click(screen.getByRole('button', { name: '复核需求与时间' }))
    expect(await screen.findByText('仅查看，不影响路线')).toBeInTheDocument()
    expect(window.location.pathname).toBe('/goals/goal-1/clarify')
    expect(fetchMock.mock.calls.some(([input, options]) => options?.method === 'POST'
      && /\/(requirements\/(reopen|confirm)|plan\/(confirm|generation\/start))$/.test(String(input)))).toBe(false)
  })

  it('freely switches between route and confirmed requirements without changing progress', async () => {
    const viewingWorkspace = {
      ...workspace,
      status: 'ROUTE_REVIEW',
      requirement_draft: {
        id: 'r1',
        version: 1,
        status: 'CONFIRMED',
        snapshot: { objective: '我想学习 Agent 开发', available_capacity: '每周 10 小时' },
      },
      readiness: { ready_for_review: true, blocking_fields: [], reason: '需求已确认' },
      downstream_route_preserved: true,
      requirements_changed_since_route: false,
    }
    window.history.pushState({}, '', '/goals/goal-1/route')
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockImplementation((input) => {
      const path = String(input)
      if (path === '/api/goals') {
        return Promise.resolve(new Response(JSON.stringify([]), { status: 200 }))
      }
      if (path.endsWith('/clarification')) {
        return Promise.resolve(new Response(JSON.stringify(viewingWorkspace), { status: 200 }))
      }
      return Promise.resolve(new Response(JSON.stringify(routeWorkspace), { status: 200 }))
    })
    render(<App />)

    fireEvent.click(await screen.findByRole('button', { name: '前往“明确需求”阶段' }))

    await screen.findByText(/当前路线采用的目标/)
    expect(screen.getByText('仅查看，不影响路线')).toBeInTheDocument()
    expect(window.location.pathname).toBe('/goals/goal-1/clarify')
    expect(fetchMock).not.toHaveBeenCalledWith(
      expect.stringContaining('/requirements/reopen'),
      expect.anything(),
    )

    fireEvent.click(screen.getByRole('button', { name: '前往“讨论路线”阶段' }))
    await screen.findByRole('heading', { name: '第一版整体路线' })
    expect(window.location.pathname).toBe('/goals/goal-1/route')
  })

  it('asks about regenerating only after a previous-stage value is changed', async () => {
    const viewingWorkspace = {
      ...workspace,
      status: 'ROUTE_REVIEW',
      requirement_draft: { id: 'r1', version: 1, status: 'CONFIRMED', snapshot: { objective: '我想学习 Agent 开发' } },
      readiness: { ready_for_review: true, blocking_fields: [], reason: '需求已确认' },
      downstream_route_preserved: true,
      requirements_changed_since_route: false,
    }
    const changedWorkspace = {
      ...viewingWorkspace,
      status: 'REQUIREMENT_REVIEW',
      facts: [{ ...workspace.facts[0], value: '完成一个更聚焦的 Agent 项目' }],
      requirement_draft: { id: 'r2', version: 2, status: 'DRAFT', snapshot: { objective: '完成一个更聚焦的 Agent 项目' } },
      requirements_changed_since_route: true,
    }
    window.history.pushState({}, '', '/goals/goal-1/clarify')
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockImplementation((input, options) => {
      const path = String(input)
      if (path.includes('/facts/') && options?.method === 'PATCH') {
        return Promise.resolve(new Response(JSON.stringify(changedWorkspace), { status: 200 }))
      }
      if (path.endsWith('/requirements/changes/discard') && options?.method === 'POST') {
        return Promise.resolve(new Response(JSON.stringify(viewingWorkspace), { status: 200 }))
      }
      if (path === '/api/goals') {
        return Promise.resolve(new Response(JSON.stringify([]), { status: 200 }))
      }
      if (path.endsWith('/route')) {
        return Promise.resolve(new Response(JSON.stringify(routeWorkspace), { status: 200 }))
      }
      return Promise.resolve(new Response(JSON.stringify(viewingWorkspace), { status: 200 }))
    })
    render(<App />)

    await screen.findByText('仅查看，不影响路线')
    expect(screen.queryByText('需求已经更新，现有路线仍完整保留')).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '修改 目标' }))
    fireEvent.change(screen.getByRole('textbox', { name: '修改目标' }), { target: { value: '完成一个更聚焦的 Agent 项目' } })
    fireEvent.click(screen.getByRole('button', { name: '保存修改' }))

    await screen.findByText('需求已经更新，现有路线仍完整保留')
    expect(screen.getByRole('button', { name: '确认新需求并更新路线 →' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '先查看现有路线' })).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '撤销需求修改，继续原路线' }))
    await screen.findByRole('heading', { name: '第一版整体路线' })
    expect(fetchMock).toHaveBeenCalledWith(
      '/api/goals/goal-1/requirements/changes/discard',
      expect.objectContaining({ method: 'POST' }),
    )
  })

  it('shows a polished transition while the first route is being generated', async () => {
    const draftingWorkspace = { ...routeWorkspace, status: 'ROUTE_DRAFTING', route: null }
    window.history.pushState({}, '', '/goals/goal-1/route')
    vi.spyOn(globalThis, 'fetch').mockImplementation((input, options) => {
      const path = String(input)
      if (path.endsWith('/route/draft') && options?.method === 'POST') {
        return new Promise(() => undefined)
      }
      if (path === '/api/goals') {
        return Promise.resolve(new Response(JSON.stringify([]), { status: 200 }))
      }
      return Promise.resolve(new Response(JSON.stringify(draftingWorkspace), { status: 200 }))
    })
    render(<App />)

    expect(await screen.findByRole('heading', { name: '正在组织你的一级路线' })).toBeInTheDocument()
    expect(screen.getByText('回顾目标和实际情况')).toBeInTheDocument()
    expect(screen.getByText('你的已确认内容已经保存，可以放心等待。')).toBeInTheDocument()
    expect(screen.getByRole('complementary', { name: '已保存的阶段信息' })).toBeInTheDocument()
  })

  it('confirms a plan, maps dates and persists real task progress', async () => {
    const now = new Date()
    const today = `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, '0')}-${String(now.getDate()).padStart(2, '0')}`
    const missed = new Date(now)
    missed.setDate(now.getDate() - 1)
    const missedDate = `${missed.getFullYear()}-${String(missed.getMonth() + 1).padStart(2, '0')}-${String(missed.getDate()).padStart(2, '0')}`
    const plan = {
      id: 'plan-1', route_version_id: 'route-1', version: 1, revision: 1, status: 'DRAFT',
      summary: '先建立基础，再完成一次实践。', total_weeks: 1,
      weekly_execution_days: 1, weekly_minutes: 90, total_estimated_minutes: 60,
      buffer_strategy: ['保留休息日'], assumptions: [], open_questions: [], change_summary: [],
      milestones: [{ id: 'milestone-1', route_stage_id: 'stage-1', stage_order: 1, title: '建立基础', purpose: '完成基础准备', start_week: 1, end_week: 1, outputs: ['基础清单'] }],
      days: [{ id: 'day-1', stage_order: 1, week_index: 1, day_index: 1, primary_goal: '完成今天的基础准备', suggested_minutes: 60, evidence: ['能够说明核心概念'], do_not: ['不要扩大范围'], tasks: [{ id: 'task-1', order: 1, title: '整理核心概念', description: '用自己的话说明三个核心概念。', estimate_minutes: 60, priority: 1, task_kind: 'LEARN', is_required: true, evidence: ['能够独立说明'], depends_on: [], status: 'NOT_STARTED' }] }],
    }
    const planWorkspace = {
      goal_id: 'goal-1', goal_title: '学习 Agent 开发', status: 'PLAN_REVIEW',
      requirement_version: 1, requirement_snapshot: {}, route_version: 1, route_revision: 1,
      generation_phase: 'COMPLETE', generated_weeks: 1, total_weeks: 1,
      messages: [], revision_history: [], pending_adjustment: null, plan,
    }
    const confirmedWorkspace = { ...planWorkspace, status: 'READY_TO_START', plan: { ...plan, status: 'CONFIRMED' } }
    const preview = {
      requested_start_date: today, first_execution_date: today, projected_end_date: today,
      execution_weekdays: [0], excluded_dates: [], total_execution_days: 1,
      upcoming_days: [{ plan_day_id: 'day-1', stage_order: 1, week_index: 1, day_index: 1, code: 'M1-W1-D1', scheduled_date: today, primary_goal: '完成今天的基础准备' }],
    }
    const setup = {
      goal_id: 'goal-1', goal_title: '学习 Agent 开发', status: 'READY_TO_START',
      plan_id: 'plan-1', plan_revision: 1, total_weeks: 1,
      weekly_execution_days: 1, suggested_weekdays: [0], activation: null,
    }
    const executionCalendar = {
      first_date: missedDate,
      last_date: today,
      days: [
        {
          plan_day_id: 'missed-day', stage_order: 1, week_index: 1, day_index: 0,
          code: 'M1-W1-D0', scheduled_date: missedDate, primary_goal: '此前中断的任务',
          status: 'MISSED', suggested_minutes: 45,
          tasks: [{ id: 'missed-task', title: '此前未完成内容', status: 'CARRIED_OVER', is_required: true, estimate_minutes: 45 }],
          changes: [],
        },
        {
          ...preview.upcoming_days[0],
          status: 'PLANNED',
          suggested_minutes: 60,
          tasks: [{ id: 'task-1', title: '整理核心概念', status: 'NOT_STARTED', is_required: true, estimate_minutes: 60 }],
          changes: [],
        },
      ],
    }
    let dayCompleted = false
    const feedbacks: Array<Record<string, string | null>> = [{
      id: 'feedback-stage', task_id: null, feedback_kind: 'DAY_PACE',
      reason_code: 'JUST_RIGHT', intent_code: 'OBSERVE',
      detail_text: '希望给前面的学习阶段更多时间，减少最后的整理任务。',
      status: 'RECORDED', created_at: '2026-09-03T07:00:00Z',
    }]
    let inactivity = { level: 'NONE', inactive_days: 0, missed_execution_days: 0, missed_dates: [] as string[], partial_execution_days: 0, partial_dates: [] as string[], interrupted_execution_days: 0, interrupted_dates: [] as string[], last_activity_date: null as string | null, latest_missed_date: null as string | null, recovery_deferred: false, has_new_missed_work: false }
    const todayWorkspace = (status: string) => ({
      goal_id: 'goal-1', goal_title: '学习 Agent 开发', status: 'ACTIVE', today,
      is_execution_day: true, next_execution_date: today, milestone_title: '建立基础',
      scheduled_day: preview.upcoming_days[0],
      day: { ...plan.days[0], tasks: [{ ...plan.days[0].tasks[0], status }] },
      day_completed: dayCompleted,
      day_progress: { completed_tasks: status === 'COMPLETED' ? 1 : 0, total_tasks: 1, completed_required_tasks: status === 'COMPLETED' ? 1 : 0, total_required_tasks: 1, percentage: status === 'COMPLETED' ? 100 : 0 },
      stage_progress: { completed_tasks: status === 'COMPLETED' ? 1 : 0, total_tasks: 1, completed_required_tasks: status === 'COMPLETED' ? 1 : 0, total_required_tasks: 1, percentage: status === 'COMPLETED' ? 100 : 0 },
      overall_progress: { completed_tasks: status === 'COMPLETED' ? 1 : 0, total_tasks: 1, completed_required_tasks: status === 'COMPLETED' ? 1 : 0, total_required_tasks: 1, percentage: status === 'COMPLETED' ? 100 : 0 },
      feedbacks,
      adjustments: [],
      day_log: null,
      inactivity,
    })
    const pendingPlanProposal = {
      assistant_message: '建议只重新分配学习阶段及必要的承接周，已经完成的执行日保持不变。',
      user_intent_summary: '给学习阶段更多时间，减少最后整理任务',
      scope: 'STAGE',
      affected_stage_orders: [1], affected_weeks: [1], downstream_affected_weeks: [],
      proposed_changes: ['延长学习内容的消化时间。'], tradeoffs: ['最后整理内容会精简。'],
      unchanged_guarantees: ['已经完成的执行记录保持不变。'], total_weeks_change: 0,
      weekly_intensity_change: 'MIXED', buffer_change: 'UNCHANGED',
      needs_clarification: false, clarifying_question: null, can_apply: true,
    }
    let currentTaskStatus = 'NOT_STARTED'
    const focusSessions: Array<Record<string, unknown>> = []
    const quickCaptures: Array<Record<string, unknown>> = []
    const executionProgress = () => ({
      goal_id: 'goal-1', goal_title: '学习 Agent 开发', plan_revision: 2,
      current_stage_order: 1, current_stage_title: '建立基础',
      day_progress: todayWorkspace(currentTaskStatus).day_progress,
      stage_progress: todayWorkspace(currentTaskStatus).stage_progress,
      overall_progress: todayWorkspace(currentTaskStatus).overall_progress,
      stage_summary: '当前阶段已完成 0/1 项，还有 1 项，其中 1 项为必做。',
      overall_summary: '整个目标已完成 0/1 项，还有 1 项，其中 1 项为必做。',
      trend: { window_days: 3, completed_days: dayCompleted ? 1 : 0, completion_rate: dayCompleted ? 33 : 0, consecutive_struggle_days: 3, consecutive_easy_days: 0, planned_minutes: 180, focus_minutes: 1, status: 'NEEDS_REVIEW', summary: '最近连续多个执行日遇到困难，建议查看记录并讨论后续节奏。', should_offer_adjustment: true, recommendation_title: '复核当前阶段近期强度', recommendation_request: '最近连续三天偏难，只调整近期安排。' },
      inactivity,
      recent_days: [{ scheduled_date: today, code: 'M1-W1-D1', stage_order: 1, stage_title: '建立基础', primary_goal: '完成今天的基础准备', status: dayCompleted ? 'COMPLETED' : 'IN_PROGRESS', completed_tasks: currentTaskStatus === 'COMPLETED' ? 1 : 0, total_tasks: 1, focus_minutes: 1, feedback_count: feedbacks.length, summary: '完成真实任务并留下反馈。' }],
      feedback_timeline: feedbacks.map((item) => ({ ...item, scheduled_date: today, task_title: null, adjustment: null })),
      revision_history: [{ id: 'plan-2', revision: 2, status: 'CONFIRMED', feedback: '调整学习节奏', changes: ['延长学习阶段'], created_at: `${today}T11:00:00Z` }],
    })
    const recoveryPreview = {
      id: 'recovery-1', status: 'PREVIEW', mode: 'SHIFT_FORWARD',
      source_plan_version_id: 'plan-2', result_plan_version_id: null,
      anchor_date: today, original_end_date: today, projected_end_date: '2026-09-08',
      missed_execution_days: 2, locked_day_ids: ['day-1'], locked_task_ids: ['task-completed'],
      carryover_task_ids: ['task-1'], content_changed: false, model_called: false,
      changes: [{ plan_day_id: 'day-1', code: 'M1-W1-D1', primary_goal: '完成今天的基础准备', from_date: '2026-09-03', to_date: '2026-09-08' }],
    }
    const catchupPreview = {
      id: 'catchup-1', status: 'PREVIEW', mode: 'KEEP_DEADLINE',
      source_plan_version_id: 'plan-2', result_plan_version_id: null,
      anchor_date: today, original_end_date: '2026-09-20', missed_dates: ['2026-09-02', '2026-09-03'],
      backlog_tasks: 2, backlog_minutes: 60, remaining_execution_days: 6,
      available_non_execution_days: 4, required_extra_minutes_per_day: 10,
      include_non_execution_days: false, programmatic_feasible: true, model_required: false,
      backlog_cleared_date: '2026-09-08',
      allocation: [{ scheduled_date: '2026-09-08', day_type: 'EXECUTION', added_tasks: 2, added_minutes: 60, original_minutes: 60, total_minutes: 120 }],
    }
    const unifiedToday = {
      date: today,
      timezone: 'Asia/Shanghai',
      fixed_tasks: [],
      flexible_tasks: [{
        id: 'task-1', source: 'AGENT', plan_id: 'goal-1', plan_title: '学习 Agent 开发',
        title: '整理核心概念', description: '完成今天的基础准备', estimate_minutes: 60,
        status: 'NOT_STARTED', scheduled_date: today, start_local: null, end_local: null,
        is_fixed: false, schedule_status: 'PLANNED', schedule_review_required: false,
        schedule_change_reason: null, dependencies: [], ai_schedule_requested: false,
      }],
      conflicts: [],
    }
    window.history.pushState({}, '', '/goals/goal-1/plan')
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockImplementation((input, options) => {
      const path = String(input)
      if (path === '/api/goals') return Promise.resolve(new Response(JSON.stringify([]), { status: 200 }))
      if (path === '/api/today') return Promise.resolve(new Response(JSON.stringify(unifiedToday), { status: 200 }))
      if (path.endsWith('/plan/confirm')) return Promise.resolve(new Response(JSON.stringify(confirmedWorkspace), { status: 200 }))
      if (path.endsWith('/execution/setup')) return Promise.resolve(new Response(JSON.stringify(setup), { status: 200 }))
      if (path.endsWith('/execution/schedule')) return Promise.resolve(new Response(JSON.stringify({ preference: { mode: 'FLEXIBLE', timezone: 'Asia/Shanghai' }, availability_windows: [{ id: 'window-1', goal_id: 'goal-1', weekday: (now.getDay() + 6) % 7, start_local: '09:00', end_local: '12:00', valid_from: null, valid_to: null, timezone: 'Asia/Shanghai', source: 'USER', created_at: '', updated_at: '' }], availability_exceptions: [] }), { status: 200 }))
      if (path.endsWith('/execution/calendar')) return Promise.resolve(new Response(JSON.stringify(executionCalendar), { status: 200 }))
      if (path.endsWith('/execution/progress')) return Promise.resolve(new Response(JSON.stringify(executionProgress()), { status: 200 }))
      if (path.endsWith('/execution/recovery/shift-preview')) return Promise.resolve(new Response(JSON.stringify(recoveryPreview), { status: 200 }))
      if (path.includes('/execution/recovery/catchup-preview')) return Promise.resolve(new Response(JSON.stringify(catchupPreview), { status: 200 }))
      if (path.endsWith('/execution/recovery/defer') && options?.method === 'POST') {
        inactivity = { ...inactivity, recovery_deferred: true }
        return Promise.resolve(new Response(JSON.stringify(inactivity), { status: 200 }))
      }
      if (path.endsWith('/execution/recovery/shift') && options?.method === 'POST') {
        inactivity = { level: 'NONE', inactive_days: 0, missed_execution_days: 0, missed_dates: [], partial_execution_days: 0, partial_dates: [], interrupted_execution_days: 0, interrupted_dates: [], last_activity_date: today, latest_missed_date: null, recovery_deferred: false, has_new_missed_work: false }
        return Promise.resolve(new Response(JSON.stringify({ ...recoveryPreview, status: 'APPLIED', result_plan_version_id: 'plan-3' }), { status: 200 }))
      }
      if (path.endsWith('/focus-sessions') && options?.method === 'POST') { const input = JSON.parse(String(options.body)); const saved = { id: 'focus-1', task_id: input.task_id, task_title: '整理核心概念', planned_minutes: input.planned_minutes, actual_seconds: input.actual_seconds, interruptions: input.interruptions, status: 'ENDED_EARLY', completed_at: `${today}T10:00:00Z` }; focusSessions.unshift(saved); return Promise.resolve(new Response(JSON.stringify(saved), { status: 201 })) }
      if (path.endsWith('/focus-sessions')) return Promise.resolve(new Response(JSON.stringify(focusSessions), { status: 200 }))
      if (path.endsWith('/quick-captures') && options?.method === 'POST') { const input = JSON.parse(String(options.body)); const saved = { id: 'capture-1', content: input.content, status: 'INBOX', created_at: `${today}T10:00:00Z` }; quickCaptures.unshift(saved); return Promise.resolve(new Response(JSON.stringify(saved), { status: 201 })) }
      if (path.includes('/quick-captures/') && options?.method === 'DELETE') { quickCaptures.splice(0); return Promise.resolve(new Response(JSON.stringify(quickCaptures), { status: 200 })) }
      if (path.endsWith('/quick-captures')) return Promise.resolve(new Response(JSON.stringify(quickCaptures), { status: 200 }))
      if (path.endsWith('/execution/preview')) return Promise.resolve(new Response(JSON.stringify(preview), { status: 200 }))
      if (path.endsWith('/execution/start')) return Promise.resolve(new Response(JSON.stringify(todayWorkspace(currentTaskStatus)), { status: 200 }))
      if (path.endsWith('/tasks/task-1/start')) { currentTaskStatus = 'IN_PROGRESS'; return Promise.resolve(new Response(JSON.stringify(todayWorkspace(currentTaskStatus)), { status: 200 })) }
      if (path.endsWith('/tasks/task-1/complete')) { currentTaskStatus = 'COMPLETED'; dayCompleted = true; return Promise.resolve(new Response(JSON.stringify(todayWorkspace(currentTaskStatus)), { status: 200 })) }
      if (path.endsWith('/tasks/task-1/feedback')) { feedbacks.push({ id: 'feedback-task', task_id: 'task-1', feedback_kind: 'TASK_BLOCKED', reason_code: 'TOO_HARD', intent_code: 'SPLIT_TASK', detail_text: null, status: 'PENDING_ADJUSTMENT', created_at: '2026-09-03T08:00:00Z' }); return Promise.resolve(new Response(JSON.stringify(todayWorkspace(currentTaskStatus)), { status: 200 })) }
      if (path.endsWith('/today/feedback')) { feedbacks.push({ id: 'feedback-day', task_id: null, feedback_kind: 'DAY_PACE', reason_code: 'TOO_EASY', intent_code: 'OBSERVE', detail_text: null, status: 'RECORDED', created_at: '2026-09-03T09:00:00Z' }); return Promise.resolve(new Response(JSON.stringify(todayWorkspace(currentTaskStatus)), { status: 200 })) }
      if (path.includes('/feedback/') && options?.method === 'DELETE') { const feedbackId = path.split('/').at(-1); const index = feedbacks.findIndex((item) => item.id === feedbackId); if (index >= 0) feedbacks.splice(index, 1); return Promise.resolve(new Response(JSON.stringify(todayWorkspace(currentTaskStatus)), { status: 200 })) }
      if (path.endsWith('/today/complete')) { dayCompleted = true; return Promise.resolve(new Response(JSON.stringify(todayWorkspace(currentTaskStatus)), { status: 200 })) }
      if (path.endsWith('/today')) return Promise.resolve(new Response(JSON.stringify(todayWorkspace(currentTaskStatus)), { status: 200 }))
      if (path.includes('/today?on_date=')) return Promise.resolve(new Response(JSON.stringify(todayWorkspace(currentTaskStatus)), { status: 200 }))
      if (path.endsWith('/plan/revise')) return Promise.resolve(new Response(JSON.stringify({ ...planWorkspace, status: 'ACTIVE', pending_adjustment: pendingPlanProposal, plan: { ...plan, status: 'CONFIRMED' } }), { status: 200 }))
      if (path.endsWith('/plan/adjustment/apply')) return Promise.resolve(new Response(JSON.stringify({ ...planWorkspace, status: 'ACTIVE', pending_adjustment: null, messages: [{ id: 'applied', sequence: 1, role: 'ASSISTANT', content: '新版计划已经发布，历史执行记录保持不变。', generation_status: 'SUCCEEDED', created_at: '2026-09-03T10:00:00Z' }], plan: { ...plan, revision: 2, status: 'CONFIRMED' } }), { status: 200 }))
      if (path.endsWith('/plan') && !options?.method) return Promise.resolve(new Response(JSON.stringify(planWorkspace), { status: 200 }))
      return Promise.resolve(new Response(JSON.stringify(planWorkspace), { status: 200 }))
    })
    render(<App />)

    fireEvent.click(await screen.findByRole('button', { name: '确认计划，准备开始 →' }))
    expect(await screen.findByRole('heading', { name: '把相对计划放进日历' })).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '手动日期' }))
    expect(await screen.findByText('日期预览')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '确认日期安排 →' }))
    expect(await screen.findByRole('heading', { name: '要现在为每天分配具体时间吗？' })).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '暂时保持自由安排' }))
    expect(await screen.findByRole('heading', { name: '今天的重点' })).toBeInTheDocument()
    expect(screen.getByText('完成今天的基础准备', { selector: '.today-primary-goal' })).toBeInTheDocument()
    expect(await screen.findByRole('button', { name: '安排整个规划' })).toBeInTheDocument()
    const executionPage = screen.getByRole('complementary', { name: '每日工具' }).closest('.today-page') as HTMLElement
    const expectExecutionColumns = () => {
      expect(Array.from(executionPage.children).map((child) => child.className)).toEqual([
        'planner-primary-column', 'stage-context-column today-context',
      ])
      expect(screen.getByRole('button', { name: '调整执行日' }).closest('.execution-header-actions')).not.toBeNull()
      expect(executionPage.querySelector('[role="dialog"]')).toBeNull()
    }
    expectExecutionColumns()
    fireEvent.click(screen.getByRole('button', { name: '收起每日工具' }))
    expect(executionPage).toHaveClass('today-tools-collapsed')
    expect(screen.getByRole('button', { name: '展开每日工具' })).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '展开每日工具' }))
    expect(executionPage).not.toHaveClass('today-tools-collapsed')
    fireEvent.click(screen.getByRole('button', { name: '调整执行日' }))
    expect(screen.getByRole('dialog', { name: '调整执行日' })).toBeInTheDocument()
    expectExecutionColumns()
    fireEvent.click(screen.getByRole('button', { name: '关闭执行日调整' }))
    expect(screen.getByRole('button', { name: '安排时间' })).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '安排整个规划' }))
    expect(screen.getByRole('heading', { name: '为后续每个执行日生成时间安排' })).toBeInTheDocument()
    expectExecutionColumns()
    fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: '取消' }))
    fireEvent.click(screen.getByRole('button', { name: '安排时间' }))
    expect(screen.getByText('当天空闲时间')).toBeInTheDocument()
    expectExecutionColumns()
    expect(screen.getByRole('button', { name: '09:00–12:00' })).toBeInTheDocument()
    fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: '取消' }))
    expect(screen.getByRole('button', { name: `${missedDate}，遗留已转移：此前中断的任务` })).toHaveClass('recovered')
    expect(screen.getByText('遗留已转移 / 有调整')).toBeInTheDocument()
    expect(screen.queryByText('明确需求')).not.toBeInTheDocument()
    expect(await screen.findByRole('button', { name: /完成今天的基础准备/ })).toBeInTheDocument()
    fireEvent.click(screen.getByText('番茄钟'))
    expect(screen.getByText('25:00')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: /整理核心概念/ }))
    expect(screen.getByText('60:00')).toBeInTheDocument()
    fireEvent.change(screen.getByRole('spinbutton', { name: '自定义专注分钟' }), { target: { value: '40' } })
    expect(screen.getByText('40:00')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '开始专注' }))
    fireEvent.click(screen.getByRole('button', { name: '结束并记录' }))
    expect(await screen.findByText('本轮专注已保存')).toBeInTheDocument()
    expect(screen.getByText('1 / 40 分钟')).toBeInTheDocument()
    fireEvent.click(screen.getByText('点子备忘'))
    fireEvent.change(screen.getByRole('textbox', { name: '点子内容' }), { target: { value: '明天确认接口限流策略' } })
    fireEvent.click(screen.getByRole('button', { name: '记下这个点子' }))
    fireEvent.click(screen.getByRole('button', { name: /点子笔记本/ }))
    expect(await screen.findByText('明天确认接口限流策略')).toBeInTheDocument()
    fireEvent.click(screen.getByText('今日洞察'))
    expect(screen.getByText('今日余量')).toBeInTheDocument()
    expect(screen.getByText(/实际比设定少 40 分钟/)).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '看看阶段或整体怎么调' }))
    expect(await screen.findByRole('region', { name: '阶段或整体调整建议' })).toBeInTheDocument()
    expect(screen.getByText('延长学习内容的消化时间。')).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: '今天的重点' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '查看预览并确认' })).toBeEnabled()
    expect(screen.queryByText('计划处理完成')).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '暂时做不完' }))
    fireEvent.click(screen.getByRole('button', { name: /比预计更难/ }))
    fireEvent.click(screen.getByRole('button', { name: '保存并分析反馈' }))
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
    expect(screen.getAllByText('比预计更难', { exact: false }).length).toBeGreaterThan(0)
    expect(screen.getAllByText('把任务拆小', { exact: false }).length).toBeGreaterThan(0)
    fireEvent.click(screen.getByRole('button', { name: '标记完成' }))
    expect(await screen.findByText('今天已经完成')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '内容太简单' }))
    fireEvent.click(screen.getByRole('button', { name: '保存并分析反馈' }))
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
    expect(screen.getByText('最近反馈：内容太简单', { exact: false })).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '删除反馈：内容太简单' }))
    fireEvent.click(screen.getByRole('button', { name: '确认删除' }))
    await waitFor(() => expect(screen.queryByRole('button', { name: '删除反馈：内容太简单' })).not.toBeInTheDocument())
    expect(screen.queryByRole('button', { name: '结束今天' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '进度与调整' })).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '查看进度与调整' }))
    expect(await screen.findByRole('heading', { name: '最近执行日' })).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: '最近有些吃力' })).toBeInTheDocument()
    expect(screen.getByText('最近几次执行有些吃力，可以看看接下来几天是否需要放松一点。')).toBeInTheDocument()
    expect(document.querySelector('.trend-card > header > strong')).toBeNull()
    expect(screen.getByText('调整学习节奏')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '看看怎么调整' }))
    expect(await screen.findByText('Solevia 的调整建议')).toBeInTheDocument()
    expect(screen.getByText('在你确认前，任务、日期和进度都会保持原样。')).toBeInTheDocument()
    expect(fetchMock).toHaveBeenCalledWith('/api/goals/goal-1/tasks/task-1/complete', expect.objectContaining({ method: 'POST' }))
    expect(fetchMock).toHaveBeenCalledWith('/api/goals/goal-1/tasks/task-1/feedback', expect.objectContaining({ method: 'POST' }))
    expect(fetchMock).toHaveBeenCalledWith('/api/goals/goal-1/today/feedback', expect.objectContaining({ method: 'POST' }))
    inactivity = { level: 'PAUSED_3_DAYS', inactive_days: 3, missed_execution_days: 2, missed_dates: ['2026-09-02', '2026-09-03'], partial_execution_days: 1, partial_dates: ['2026-09-02'], interrupted_execution_days: 1, interrupted_dates: ['2026-09-03'], last_activity_date: '2026-09-01', latest_missed_date: '2026-09-03', recovery_deferred: false, has_new_missed_work: false }
    fireEvent.click(screen.getByRole('button', { name: '← 返回今日任务' }))
    expect(await screen.findByRole('heading', { name: '进度与调整' })).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: '欢迎回来，不用急着一次补完' })).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '调整计划' }))
    expect(await screen.findByRole('heading', { name: '希望怎样处理这次中断？' })).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: /尽量保持完成日期不变/ }))
    expect(await screen.findByRole('heading', { name: '把遗留工作接回后续安排' })).toBeInTheDocument()
    expect(screen.getByText('2 项 · 60 分钟')).toBeInTheDocument()
    expect(screen.getByText('可以考虑使用非执行日补任务')).toBeInTheDocument()
    expect(screen.getByText('当前只把积压任务分摊到后续执行日。')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '应用这份追赶安排' })).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '返回调整方式' }))
    fireEvent.click(screen.getByRole('button', { name: '返回欢迎页' }))
    fireEvent.click(screen.getByRole('button', { name: '暂不处理' }))
    expect(await screen.findByRole('heading', { name: '1 个执行日部分未完成，1 个执行日整天中断' })).toBeInTheDocument()
    expect(screen.getByText(/部分未完成：9月2日周三；整天中断：9月3日周四/)).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '查看并处理' }))
    expect(await screen.findByRole('heading', { name: '进度与调整' })).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '调整计划' }))
    fireEvent.click(screen.getByRole('button', { name: /直接整体顺延/ }))
    expect(await screen.findByRole('heading', { name: '从今天接上原来的安排' })).toBeInTheDocument()
    expect(within(screen.getByRole('dialog')).getByText('9月8日周二')).toBeInTheDocument()
    expect(screen.getByText('不会调用模型')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '确认整体顺延' }))
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
    expect(await screen.findByRole('heading', { name: '今天的重点' })).toBeInTheDocument()
    expect(fetchMock).toHaveBeenCalledWith('/api/goals/goal-1/execution/recovery/shift', expect.objectContaining({ method: 'POST' }))
  }, 15_000)

  it('pauses indefinitely from Today and can resume later', async () => {
    const today = '2026-09-08'
    let status = 'ACTIVE'
    const summary = { completed_tasks: 0, total_tasks: 0, completed_required_tasks: 0, total_required_tasks: 0, percentage: 0 }
    const lifecycle = () => ({ id: 'goal-1', title: '学习 Agent 开发', status, timezone: 'UTC', pause_until: null, completed_at: null, archived_at: null, completion_summary: null, remaining_required_tasks: 0 })
    const todayWorkspace = () => ({
      goal_id: 'goal-1', goal_title: '学习 Agent 开发', status, today,
      is_execution_day: false, next_execution_date: null, milestone_title: null,
      scheduled_day: null, day: null, day_completed: false,
      day_progress: summary, stage_progress: summary, overall_progress: summary,
      capacity: { planned_minutes: 0, remaining_minutes: 0, remaining_tasks: 0, load_status: 'REST_DAY' },
      inactivity: { level: 'NONE', inactive_days: 0, missed_execution_days: 0, missed_dates: [], last_activity_date: null, latest_missed_date: null, recovery_deferred: false, has_new_missed_work: false },
      feedbacks: [], adjustments: [], day_log: null,
    })
    window.history.pushState({}, '', '/goals/goal-1/manage')
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockImplementation((input, options) => {
      const path = String(input)
      if (path === '/api/goals/status-check') return Promise.resolve(new Response(JSON.stringify([{ ...lifecycle(), last_active_at: `${today}T00:00:00Z` }]), { status: 200 }))
      if (path.endsWith('/pause') && options?.method === 'POST') { status = 'PAUSED'; return Promise.resolve(new Response(JSON.stringify(lifecycle()), { status: 200 })) }
      if (path.endsWith('/today')) return Promise.resolve(new Response(JSON.stringify(todayWorkspace()), { status: 200 }))
      if (path.endsWith('/execution/calendar')) return Promise.resolve(new Response(JSON.stringify({ first_date: today, last_date: today, days: [] }), { status: 200 }))
      if (path.endsWith('/focus-sessions') || path.endsWith('/quick-captures')) return Promise.resolve(new Response(JSON.stringify([]), { status: 200 }))
      if (path.endsWith('/plan')) return Promise.resolve(new Response(JSON.stringify({ pending_adjustment: null }), { status: 200 }))
      return Promise.resolve(new Response(JSON.stringify(lifecycle()), { status: 200 }))
    })
    render(<App />)

    fireEvent.click(await screen.findByRole('button', { name: '暂停' }))
    expect(window.location.pathname).toBe('/goals/goal-1/today')
    fireEvent.click(screen.getByRole('button', { name: '暂不设日期' }))
    fireEvent.click(screen.getByRole('button', { name: '确认暂停' }))

    expect(await screen.findByRole('heading', { name: '这段时间不会累积任务' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '现在恢复' })).toBeInTheDocument()
    expect(fetchMock).toHaveBeenCalledWith('/api/goals/goal-1/pause', expect.objectContaining({
      method: 'POST',
      body: JSON.stringify({ pause_until: null }),
    }))
  })

  it('shows the completion review and archives it with 收官', async () => {
    const completedAt = '2026-09-08T10:00:00Z'
    let status = 'COMPLETED'
    const lifecycle = () => ({ id: 'goal-1', title: '学习 Agent 开发', status, timezone: 'UTC', pause_until: null, completed_at: completedAt, archived_at: status === 'ARCHIVED' ? '2026-09-08T11:00:00Z' : null, completion_summary: '计划中的 6 项任务已全部完成。期间记录 1 条反馈，采用 1 次调整。', remaining_required_tasks: 0 })
    window.history.pushState({}, '', '/goals/goal-1/complete')
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockImplementation((input, options) => {
      const path = String(input)
      if (path === '/api/goals/status-check') return Promise.resolve(new Response(JSON.stringify([{ ...lifecycle(), last_active_at: completedAt }]), { status: 200 }))
      if (path.endsWith('/lifecycle')) return Promise.resolve(new Response(JSON.stringify(lifecycle()), { status: 200 }))
      if (path.endsWith('/execution/calendar')) return Promise.resolve(new Response(JSON.stringify({ first_date: '2026-09-01', last_date: '2026-09-08', days: [] }), { status: 200 }))
      if (path.endsWith('/execution/progress')) return Promise.resolve(new Response(JSON.stringify({
        goal_id: 'goal-1', goal_title: '学习 Agent 开发', plan_revision: 2,
        overall_progress: { completed_tasks: 6, total_tasks: 6 }, trend: { focus_minutes: 0 },
        total_focus_minutes: 145, applied_adjustments: 2,
        feedback_timeline: [{ id: 'feedback-1', scheduled_date: '2026-09-04', task_title: '完成接口', reason_code: 'TOO_HARD', detail_text: '需要拆小处理。', adjustment: { title: '拆分任务', summary: '拆为两个小步骤。', status: 'APPLIED' } }],
        revision_history: [{ id: 'plan-2', revision: 2, created_at: '2026-09-04T12:00:00Z', feedback: '降低近期强度', changes: ['拆分接口任务'] }],
      }), { status: 200 }))
      if (path.endsWith('/archive') && options?.method === 'POST') { status = 'ARCHIVED'; return Promise.resolve(new Response(JSON.stringify(lifecycle()), { status: 200 })) }
      return Promise.resolve(new Response(JSON.stringify({}), { status: 200 }))
    })
    render(<App />)

    expect(await screen.findByRole('heading', { name: '学习 Agent 开发' })).toBeInTheDocument()
    expect(screen.getByText('8 天')).toBeInTheDocument()
    expect(screen.getByText('6 / 6')).toBeInTheDocument()
    expect(screen.getByText('145 分钟')).toBeInTheDocument()
    expect(screen.getByText('1 条反馈 · 2 次已采用调整')).toBeInTheDocument()
    expect(screen.getByText('需要拆小处理。')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '收官' }))

    expect(await screen.findByRole('button', { name: '返回首页' })).toBeInTheDocument()
    expect(fetchMock).toHaveBeenCalledWith('/api/goals/goal-1/archive', expect.objectContaining({ method: 'POST' }))
  })
})
