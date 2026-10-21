import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import * as api from '../api'
import type { ResearchResponseRequest } from '../api'
import { ResearchProgress } from './ResearchProgress'
import { useResearchResponse } from './useResearchResponse'

const request = {
  id: 'request-1', goal_id: 'goal-1', conversation_id: 'conversation-1', phase: 'PLAN', intent: 'EXPLAIN',
  question: '查阅官方资料解释安排', status: 'WAITING_RESEARCH', fallback: false, attempt_count: 0,
  response_message_id: null, error_message: null,
  run: { id: 'run-1', status: 'RUNNING', current_stage: 'READING', question: '查阅官方资料解释安排',
    checkpoint: { coverage: { searched_queries: 2, read_pages: 1 } }, result: null, evidence: [],
  },
} as unknown as ResearchResponseRequest

function Harness({ goalId = 'goal-1', onWorkspace = vi.fn() }) {
  const research = useResearchResponse(goalId, ['PLAN'], api.getPlan, onWorkspace)
  return <><button onClick={() => void research.start('新的问题', 'PLAN', 'EXPLAIN')}>开始</button>
    <span>{research.busy ? '处理中' : '空闲'}</span><ResearchProgress research={research} /></>
}

afterEach(() => { cleanup(); vi.restoreAllMocks() })
beforeEach(() => {
  vi.spyOn(api, 'getResearchReadiness').mockResolvedValue({ ready: true, status: 'READY', components: {} })
})

describe('durable research response', () => {
  it('shows context processing without search counters before retrieval begins', async () => {
    vi.spyOn(api, 'listResearchResponses').mockResolvedValue([{ ...request,
      run: { ...request.run, current_stage: 'ANALYZING', checkpoint: {}, evidence: [] } }])
    render(<Harness />)
    expect(await screen.findByText('正在结合上下文', { selector: 'summary' })).toBeInTheDocument()
    expect(screen.queryByText(/已完成查询|已查询/)).not.toBeInTheDocument()
  })

  it.each(['SKIPPED', 'FAILED'])('does not leave a search report for a completed %s gate without retrieval', async (status) => {
    const research = { current: { ...request, status: 'COMPLETED',
      run: { ...request.run, status, checkpoint: { coverage: { searched_queries: 0, read_pages: 0 } }, evidence: [] } },
      observations: [], preparing: false } as unknown as ReturnType<typeof useResearchResponse>
    const view = render(<ResearchProgress research={research} completedOnly />)
    expect(view.container).toBeEmptyDOMElement()
  })

  it('shows loading and does not submit until dependencies are ready', async () => {
    vi.spyOn(api, 'listResearchResponses').mockResolvedValue([])
    let finish: (value: api.ResearchReadiness) => void = () => undefined
    vi.mocked(api.getResearchReadiness).mockImplementationOnce(() => new Promise((resolve) => { finish = resolve }))
    const create = vi.spyOn(api, 'createResearchResponse').mockResolvedValue(request)
    vi.spyOn(api, 'getResearchResponse').mockResolvedValue(request)
    render(<Harness />)
    await waitFor(() => expect(api.listResearchResponses).toHaveBeenCalled())
    fireEvent.click(screen.getByText('开始'))
    expect(await screen.findByText('正在准备联网服务…')).toBeInTheDocument()
    expect(create).not.toHaveBeenCalled()
    finish({ ready: true, status: 'READY', components: { bge: 'READY' } })
    await waitFor(() => expect(create).toHaveBeenCalledOnce())
  })

  it('does not submit a pending readiness result to a different goal', async () => {
    vi.spyOn(api, 'listResearchResponses').mockResolvedValue([])
    let finish: (value: api.ResearchReadiness) => void = () => undefined
    vi.mocked(api.getResearchReadiness).mockImplementationOnce(() => new Promise((resolve) => { finish = resolve }))
    const create = vi.spyOn(api, 'createResearchResponse')
    const view = render(<Harness />)
    fireEvent.click(screen.getByText('开始'))
    await screen.findByText('正在准备联网服务…')
    view.rerender(<Harness goalId="goal-2" />)
    finish({ ready: true, status: 'READY', components: {} })
    await waitFor(() => expect(screen.getByText('空闲')).toBeInTheDocument())
    expect(create).not.toHaveBeenCalled()
  })
  it('restores an in-flight request and reloads its completed answer without enqueueing again', async () => {
    vi.spyOn(api, 'listResearchResponses').mockResolvedValue([request])
    vi.spyOn(api, 'getResearchResponse').mockResolvedValue({ ...request, status: 'COMPLETED', attempt_count: 1 })
    vi.spyOn(api, 'getPlan').mockResolvedValue({ messages: [] } as unknown as api.PlanWorkspace)
    const create = vi.spyOn(api, 'createResearchResponse')
    const loaded = vi.fn()
    render(<Harness onWorkspace={loaded} />)
    expect(await screen.findByText('读取来源')).toBeInTheDocument()
    expect(screen.getByText('已完成查询 2 · 已读取 1 页 · 收集 0 个来源')).toBeInTheDocument()
    await waitFor(() => expect(loaded).toHaveBeenCalledOnce(), { timeout: 2500 })
    expect(create).not.toHaveBeenCalled()
  })

  it('cancel remains cancelled after remount and does not produce a response', async () => {
    vi.spyOn(api, 'listResearchResponses').mockResolvedValue([request])
    const action = vi.spyOn(api, 'actResearchResponse').mockResolvedValue({ ...request, status: 'CANCELLED', run: { ...request.run, status: 'CANCELLED', current_stage: 'CANCELLED' } })
    const get = vi.spyOn(api, 'getPlan')
    const view = render(<Harness />)
    fireEvent.click(await screen.findByRole('button', { name: '取消研究' }))
    expect(await screen.findByText('研究已取消')).toBeInTheDocument()
    expect(action).toHaveBeenCalledWith('goal-1', 'request-1', 'cancel')
    view.unmount()
    vi.mocked(api.listResearchResponses).mockResolvedValue([{ ...request, status: 'CANCELLED', run: { ...request.run, status: 'CANCELLED', current_stage: 'CANCELLED' } }])
    render(<Harness />)
    expect(await screen.findByText('研究已取消')).toBeInTheDocument()
    expect(get).not.toHaveBeenCalled()
  })

  it('retries only the answer and retains the existing request and run', async () => {
    vi.spyOn(api, 'listResearchResponses').mockResolvedValue([{ ...request, status: 'ANSWER_FAILED' }])
    const create = vi.spyOn(api, 'createResearchResponse')
    const action = vi.spyOn(api, 'actResearchResponse').mockResolvedValue({ ...request, status: 'QUEUED_ANSWER' })
    render(<Harness />)
    fireEvent.click(await screen.findByRole('button', { name: '重试回答' }))
    await waitFor(() => expect(action).toHaveBeenCalledWith('goal-1', 'request-1', 'retry-answer'))
    expect(create).not.toHaveBeenCalled()
  })

  it('shows cancellation even while the worker retains its previous stage', async () => {
    vi.spyOn(api, 'listResearchResponses').mockResolvedValue([{ ...request, status: 'CANCELLED',
      run: { ...request.run, status: 'CANCEL_REQUESTED', current_stage: 'ANALYZING' } }])
    render(<Harness />)
    expect(await screen.findByText('研究已取消')).toBeInTheDocument()
    expect(screen.queryByText('分析检索需求')).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: '重试研究' })).toBeEnabled()
  })

  it('requires an explicit choice when research failed', async () => {
    vi.spyOn(api, 'listResearchResponses').mockResolvedValue([{ ...request, status: 'AWAITING_DECISION' }])
    const action = vi.spyOn(api, 'actResearchResponse').mockResolvedValue({ ...request, status: 'QUEUED_ANSWER', fallback: true })
    render(<Harness />)
    expect(await screen.findByText('研究需要处理')).toBeInTheDocument()
    expect(action).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: '继续普通回答' }))
    expect(await screen.findByText('正在整理普通回答')).toBeInTheDocument()
    expect(action).toHaveBeenCalledWith('goal-1', 'request-1', 'ordinary')
  })

  it('ignores a late workspace result after switching goals', async () => {
    vi.spyOn(api, 'listResearchResponses').mockImplementation(async (id) => id === 'goal-1' ? [{ ...request, status: 'COMPLETED' }] : [])
    let resolve: (value: api.PlanWorkspace) => void = () => undefined
    vi.spyOn(api, 'getPlan').mockImplementation(() => new Promise((done) => { resolve = done }))
    const loaded = vi.fn()
    const view = render(<Harness onWorkspace={loaded} />)
    await waitFor(() => expect(api.getPlan).toHaveBeenCalled())
    view.rerender(<Harness goalId="goal-2" onWorkspace={loaded} />)
    resolve({ goal_id: 'goal-1' } as api.PlanWorkspace)
    await waitFor(() => expect(screen.getByText('空闲')).toBeInTheDocument())
    expect(loaded).not.toHaveBeenCalled()
  })

  it('continues polling after a failed cancellation request', async () => {
    vi.spyOn(api, 'listResearchResponses').mockResolvedValue([request])
    vi.spyOn(api, 'actResearchResponse').mockRejectedValue(new Error('连接中断'))
    const get = vi.spyOn(api, 'getResearchResponse').mockResolvedValue(request)
    render(<Harness />)
    fireEvent.click(await screen.findByRole('button', { name: '取消研究' }))
    expect(await screen.findByText('连接中断')).toBeInTheDocument()
    await waitFor(() => expect(get).toHaveBeenCalled(), { timeout: 2500 })
    expect(screen.getByText('处理中')).toBeInTheDocument()
  })

  it('keeps observed research steps when the answer attempt counter increments', async () => {
    vi.spyOn(api, 'listResearchResponses').mockResolvedValue([request])
    vi.spyOn(api, 'getResearchResponse').mockResolvedValue({ ...request, status: 'ANSWERING', attempt_count: 1 })
    render(<Harness />)
    await screen.findByText('读取来源', { selector: 'li span' })
    await screen.findByText('正在结合资料回答', { selector: 'summary' }, { timeout: 2500 })
    expect(screen.getByText('读取来源', { selector: 'li span' })).toBeInTheDocument()
    expect(await screen.findByText('正在结合资料回答', { selector: 'li span' })).toBeInTheDocument()
  })
})
