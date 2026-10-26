import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { StrictMode } from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { AssistantAnswer, useFreshAnswers } from './AssistantAnswer'
import { OperationProgress } from './ProcessDetails'
import { AnswerResearch } from './AnswerResearch'
import { ResearchSources } from './ResearchSources'
import type { ResearchSummary } from '../api'
import type { useResearchResponse } from './useResearchResponse'

afterEach(() => { cleanup(); vi.useRealTimers(); vi.restoreAllMocks() })

describe('assistant presentation', () => {
  it('renders Markdown, GFM tables, links and code without executing HTML or embedding remote images', () => {
    const { container } = render(<AssistantAnswer content={'**重点**\n\n1. 项目\n\n[官方](https://example.com)\n\n```python\nprint(1)\n```\n\n| A | B |\n|---|---|\n| 1 | 2 |\n\n<script>alert(1)</script>\n\n[坏链接](javascript:alert(1))\n\n![图片](https://example.com/track.png)'} />)
    expect(container.querySelector('strong')).toHaveTextContent('重点')
    expect(container.querySelector('ol li')).toHaveTextContent('项目')
    expect(container.querySelector('pre code')).toHaveTextContent('print(1)')
    expect(container.querySelector('table')).toBeInTheDocument()
    expect(screen.getByRole('link', { name: '官方' })).toHaveAttribute('href', 'https://example.com')
    expect(screen.getByText('坏链接')).not.toHaveAttribute('href')
    expect(container.querySelector('script, img')).toBeNull()
  })

  it('reveals text quickly while preserving complete bold syntax, and skip stays complete', async () => {
    vi.useFakeTimers()
    const content = `**${'证据'.repeat(120)}**`
    const view = render(<AssistantAnswer content={content} animate />)
    expect(view.container.querySelector('.assistant-answer')).toHaveAttribute('aria-busy', 'true')
    await act(() => vi.advanceTimersByTimeAsync(32))
    expect(view.container.querySelector('strong')?.textContent?.length).toBeGreaterThan(0)
    expect(view.container.textContent).not.toContain('**')
    fireEvent.click(screen.getByRole('button', { name: '显示全文' }))
    await act(() => vi.advanceTimersByTimeAsync(2000))
    expect(view.container.querySelector('strong')).toHaveTextContent('证据'.repeat(120))
    expect(view.container.querySelector('.assistant-answer')).toHaveAttribute('aria-busy', 'false')
    view.rerender(<AssistantAnswer content={content} animate />)
    expect(screen.queryByRole('button', { name: '显示全文' })).not.toBeInTheDocument()
  })

  it('finishes a long answer within two seconds and clears timers when unmounted', async () => {
    vi.useFakeTimers()
    const view = render(<AssistantAnswer content={'测试'.repeat(5000)} animate />)
    await act(() => vi.advanceTimersByTimeAsync(1850))
    expect(view.container.querySelector('.assistant-answer')).toHaveAttribute('aria-busy', 'false')
    view.unmount()
    expect(vi.getTimerCount()).toBe(0)
  })

  it('respects reduced motion', () => {
    vi.stubGlobal('matchMedia', vi.fn(() => ({ matches: true })))
    render(<AssistantAnswer content="**直接显示**" animate />)
    expect(screen.getByText('直接显示')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '显示全文' })).not.toBeInTheDocument()
    vi.unstubAllGlobals()
  })

  it('reveals a table as a complete unit, keeping headers, aligned columns, escaped pipes and links', async () => {
    vi.useFakeTimers()
    const content = '| 候选 | 预算 | 来源 |\n|:---|---:|:---:|\n| **吊兰** | 100 | [官方](https://example.com) |\n| `A\\|B` | 20 | 待核实 |\n\n后续观察建议'
    const view = render(<AssistantAnswer content={content} animate />)
    await act(() => vi.advanceTimersByTimeAsync(32))
    const table = screen.getByRole('table')
    expect(table.querySelectorAll('th')).toHaveLength(3)
    expect(table.querySelectorAll('tbody tr')).toHaveLength(2)
    expect(Array.from(table.querySelectorAll('tbody tr')).map(row => row.children.length)).toEqual([3, 3])
    expect(table.querySelector('strong')).toHaveTextContent('吊兰')
    expect(table.querySelector('code')).toHaveTextContent('A|B')
    expect(table.querySelectorAll('th')[1]).toHaveStyle({ textAlign: 'right' })
    expect(screen.getByRole('link', { name: '官方' })).toHaveAttribute('href', 'https://example.com')
    expect(screen.getByRole('region', { name: '回答表格' })).toHaveAttribute('tabindex', '0')
    expect(view.container.textContent).not.toContain('后续观察建议')
    await act(() => vi.advanceTimersByTimeAsync(2000))
    expect(view.container.textContent).toContain('后续观察建议')
    expect(view.container.querySelectorAll('table')).toHaveLength(1)
  })

  it('animates new and regenerated messages, but not initial or switched history', () => {
    type Row = { id: string; role: string; content: string; generation_status: string }
    function History({ scope, rows }: { scope: string; rows?: Row[] }) {
      const fresh = useFreshAnswers(scope, rows)
      return <>{rows?.map(row => <span key={row.id}>{row.id}:{fresh(row.id) ? 'new' : 'old'}</span>)}</>
    }
    const first = { id: 'a1', role: 'ASSISTANT', content: '原回答', generation_status: 'SUCCEEDED' }
    const view = render(<History scope="g1" />)
    view.rerender(<History scope="g1" rows={[first]} />)
    expect(screen.getByText('a1:old')).toBeInTheDocument()
    view.rerender(<History scope="g1" rows={[first, { ...first, id: 'a2' }]} />)
    expect(screen.getByText('a2:new')).toBeInTheDocument()
    view.rerender(<History scope="g1" rows={[{ ...first, content: '重新生成' }]} />)
    expect(screen.getByText('a1:new')).toBeInTheDocument()
    view.rerender(<History scope="g2" rows={[first]} />)
    expect(screen.getByText('a1:old')).toBeInTheDocument()
  })

  it.each(['complete', 'skip', 'close early'])('presents each saved answer only once across panel remounts (%s)', async mode => {
    vi.useFakeTimers()
    type Row = { id: string; role: string; content: string; generation_status: string }
    function History({ rows, open }: { rows: Row[]; open: boolean }) {
      const fresh = useFreshAnswers('g1', rows)
      return open && rows.map(row => <AssistantAnswer key={row.id} content={row.content} animate={fresh(row.id)} onPresented={() => fresh.markPresented(row.id, row.content)} />)
    }
    const row = { id: 'a', role: 'ASSISTANT', content: '刚生成的回答'.repeat(100), generation_status: 'SUCCEEDED' }
    const view = render(<History rows={[]} open />)
    view.rerender(<History rows={[row]} open />)
    expect(view.container.querySelector('.assistant-answer')).toHaveAttribute('aria-busy', 'true')
    if (mode === 'complete') await act(() => vi.advanceTimersByTimeAsync(2000))
    if (mode === 'skip') fireEvent.click(screen.getByRole('button', { name: '显示全文' }))
    view.rerender(<History rows={[row]} open={false} />)
    view.rerender(<History rows={[row]} open />)
    expect(view.container.querySelector('.assistant-answer')).toHaveAttribute('aria-busy', 'false')
    expect(view.container.textContent).toContain(row.content)
    expect(screen.queryByRole('button', { name: '显示全文' })).not.toBeInTheDocument()

    // Regenerating the same row with new content is a new presentation. The
    // previous row's cleanup cannot consume it while React replaces its props.
    view.rerender(<History rows={[{ ...row, content: '重新生成的回答'.repeat(100) }]} open />)
    expect(view.container.querySelector('.assistant-answer')).toHaveAttribute('aria-busy', 'true')
    view.rerender(<History rows={[{ ...row, content: '重新生成的回答'.repeat(100) }]} open />)
    expect(view.container.querySelector('.assistant-answer')).toHaveAttribute('aria-busy', 'true')
    await act(() => vi.advanceTimersByTimeAsync(2000))
    expect(view.container.textContent).toContain('重新生成的回答'.repeat(100))
  })

  it('keeps newly arrived answers readable after StrictMode cleanup and repeated rerenders', async () => {
    vi.useFakeTimers()
    function History({ content, open }: { content: string; open: boolean }) {
      const rows = content ? [{ id: 'strict', role: 'ASSISTANT', content, generation_status: 'SUCCEEDED' }] : []
      const fresh = useFreshAnswers('g1', rows)
      return open && rows.map(row => <AssistantAnswer key={row.id} content={row.content} animate={fresh(row.id)} onPresented={() => fresh.markPresented(row.id, row.content)} />)
    }
    const view = render(<StrictMode><History content="" open /></StrictMode>)
    view.rerender(<StrictMode><History content={'完整回答'.repeat(100)} open /></StrictMode>)
    await act(() => vi.advanceTimersByTimeAsync(2000))
    view.rerender(<StrictMode><History content={'完整回答'.repeat(100)} open={false} /></StrictMode>)
    view.rerender(<StrictMode><History content={'完整回答'.repeat(100)} open /></StrictMode>)
    expect(view.container.querySelector('.assistant-answer')).toHaveAttribute('aria-busy', 'false')
    expect(view.container.textContent).toContain('完整回答'.repeat(100))
  })
})

describe('observed processing', () => {
  it('shows readable research status and the actual evidence gaps', () => {
    const summary = { status: 'PARTIAL', question: '核对资料', stop_reason: 'CODEX_EVIDENCE_REVIEW_REQUIRED',
      sources: [{ url: 'https://example.com', title: '官方', evidence_level: 'FULL_TEXT', read_status: 'SUCCEEDED' }],
      unresolved_points: ['幼儿接触边界尚未核实'] } as unknown as ResearchSummary
    render(<ResearchSources research={summary} />)
    expect(screen.getByText('已保留收集的资料，仍有证据需要核实')).toBeInTheDocument()
    expect(screen.getByText('正文读取 · 已读取')).toBeInTheDocument()
    expect(screen.getByText('幼儿接触边界尚未核实')).toBeInTheDocument()
    expect(screen.queryByText(/CODEX_EVIDENCE/)).not.toBeInTheDocument()
  })
  it('expands while active, keeps observed steps, and folds on completion', () => {
    const view = render(<OperationProgress active scope="g1" label="生成预览" detail="已校验 1 / 4 周" />)
    expect(view.container.querySelector('details')).toHaveAttribute('open')
    view.rerender(<OperationProgress active scope="g1" label="生成预览" detail="已校验 2 / 4 周" />)
    view.rerender(<OperationProgress active={false} scope="g1" label="生成预览" />)
    expect(view.container.querySelector('details')).not.toHaveAttribute('open')
    expect(screen.getByText('已校验 1 / 4 周')).toBeInTheDocument()
    expect(screen.getByText('已校验 2 / 4 周')).toBeInTheDocument()
    expect(screen.queryByText('已校验 4 / 4 周')).not.toBeInTheDocument()
    view.rerender(<OperationProgress active={false} scope="g2" label="生成预览" />)
    expect(view.container.querySelector('details')).toBeNull()
  })

  it('keeps errors expanded and starts a clean log on retry', () => {
    const view = render(<OperationProgress active scope="g1" label="安排时间" />)
    view.rerender(<OperationProgress active={false} scope="g1" label="安排时间" error="需要重试" />)
    expect(view.container.querySelector('details')).toHaveAttribute('open')
    expect(screen.getByText('需要重试')).toBeInTheDocument()
    view.rerender(<OperationProgress active scope="g1" label="再次安排" />)
    expect(screen.queryByText('安排时间')).not.toBeInTheDocument()
  })

  it('attaches compact sources to their own assistant answer, without a full-width completed card', () => {
    const summary = { id: 'r1', status: 'PARTIAL', sources: [{ url: 'https://example.com', title: '官方网站', evidence_level: 'FULL_TEXT' }], read_pages: 1, unresolved_points: [], question: '问题' } as unknown as ResearchSummary
    const research = { current: { id: 'q1', response_message_id: 'a1', status: 'COMPLETED', fallback: false, run: { id: 'r1', status: 'PARTIAL', checkpoint: {}, result: null, evidence: [] } }, observations: [] } as unknown as ReturnType<typeof useResearchResponse>
    const view = render(<AnswerResearch message={{ id: 'a2', role: 'ASSISTANT' }} summary={summary} research={research} />)
    expect(view.container).toBeEmptyDOMElement()
    view.rerender(<AnswerResearch message={{ id: 'a1', role: 'ASSISTANT' }} summary={summary} research={research} />)
    expect(screen.getByText('来源 · 1 项 · 部分完成')).toBeInTheDocument()
    expect(screen.queryByText('回答已完成')).not.toBeInTheDocument()
    expect(view.container.querySelectorAll('details[open]')).toHaveLength(0)
    expect(view.container.querySelector('.research-progress')).toBeNull()
    view.rerender(<AnswerResearch message={{ id: 'a1', role: 'USER' }} summary={summary} research={research} />)
    expect(view.container).toBeEmptyDOMElement()
  })

  it('does not attach an empty sources panel to an ordinary answer', () => {
    const summary = { id: 'r1', status: 'SKIPPED', sources: [], unresolved_points: [], question: '给个判断' } as unknown as ResearchSummary
    const research = { current: null } as unknown as ReturnType<typeof useResearchResponse>
    const view = render(<AnswerResearch message={{ id: 'a1', role: 'ASSISTANT', metadata: { research_run_id: 'r1' } }} summary={summary} research={research} />)
    expect(view.container.querySelector('details')).toBeNull()
  })
})
