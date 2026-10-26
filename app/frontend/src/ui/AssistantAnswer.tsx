import { useEffect, useId, useRef, useState, type ReactNode } from 'react'
import Markdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { normalizeAssistantName } from '../brand/identity'
import './ConversationPresentation.css'

type Message = { id: string; role: string; content: string; generation_status: string }

// An initial history is already readable; only answers arriving afterwards animate.
export function useFreshAnswers(scope: string | undefined, messages: Message[] | undefined) {
  const history = useRef<{ scope: string | undefined; loaded: boolean; seen: Map<string, string>; fresh: Set<string> }>({ scope, loaded: false, seen: new Map(), fresh: new Set() })
  if (history.current.scope !== scope) history.current = { scope, loaded: false, seen: new Map(), fresh: new Set() }
  if (messages) {
    for (const message of messages) {
      if (message.role !== 'ASSISTANT' || message.generation_status !== 'SUCCEEDED') continue
      if (history.current.loaded && history.current.seen.get(message.id) !== message.content) history.current.fresh.add(message.id)
      history.current.seen.set(message.id, message.content)
    }
    history.current.loaded = true
  }
  return Object.assign((id: string) => history.current.fresh.has(id), {
    markPresented(id: string, content: string) {
      // An old animation's cleanup must not consume a regenerated answer with
      // the same message id and different content.
      if (history.current.scope === scope && history.current.seen.get(id) === content) history.current.fresh.delete(id)
    },
  })
}

type MarkdownNode = { type: string; value?: string; children?: MarkdownNode[] }

// Parse complete Markdown first, then reveal text nodes. Bold/link/code markup
// therefore stays intact while characters appear, even across a delimiter.
function revealText({ limit }: { limit: number }) {
  return (tree: MarkdownNode) => {
    let remaining = limit
    function visit(node: MarkdownNode) {
      // Tables are one readable unit: removing cells while revealing changes
      // column widths and can associate a value with the wrong header.
      if (node.type === 'table') {
        const length = (item: MarkdownNode): number => Array.from(item.value ?? '').length + (item.children ?? []).reduce((sum, child) => sum + length(child), 0)
        remaining -= length(node)
        return
      }
      if (node.value != null) {
        const characters = Array.from(node.value)
        node.value = characters.slice(0, Math.max(0, remaining)).join('')
        remaining -= characters.length
      }
      if (node.children) node.children = node.children.filter((child) => {
        if (remaining <= 0) return false
        visit(child)
        return true
      })
    }
    visit(tree)
  }
}

function AnswerTable({ children }: { children: ReactNode }) {
  const frame = useRef<HTMLDivElement>(null)
  const hint = useId()
  const [overflow, setOverflow] = useState(false)
  useEffect(() => {
    const element = frame.current
    if (!element) return
    const measure = () => setOverflow(element.scrollWidth > element.clientWidth + 1)
    measure()
    const observer = typeof ResizeObserver === 'function' ? new ResizeObserver(measure) : undefined
    observer?.observe(element)
    window.addEventListener('resize', measure)
    return () => { observer?.disconnect(); window.removeEventListener('resize', measure) }
  }, [children])
  return <div className="answer-table-block">
    {overflow && <p id={hint} className="answer-table-hint">可左右滚动查看完整表格</p>}
    <div ref={frame} className="answer-table-scroll" role="region" aria-label="回答表格" aria-describedby={overflow ? hint : undefined} tabIndex={0}>
      <table>{children}</table>
    </div>
  </div>
}

export function AssistantAnswer({ content, animate = false, onPresented }: { content: string; animate?: boolean; onPresented?: (content: string) => void }) {
  const sourceContent = content
  content = normalizeAssistantName(content)
  const reducedMotion = typeof window.matchMedia === 'function' && window.matchMedia('(prefers-reduced-motion: reduce)').matches
  const [reveal, setReveal] = useState({ content, count: animate && !reducedMotion ? 0 : Infinity })
  const root = useRef<HTMLDivElement>(null)
  const timerRef = useRef<ReturnType<typeof window.setInterval> | undefined>(undefined)
  const onPresentedRef = useRef(onPresented)
  useEffect(() => { onPresentedRef.current = onPresented }, [onPresented])
  const count = reveal.content === content ? reveal.count : animate && !reducedMotion ? 0 : Infinity
  const complete = count >= Array.from(content).length
  useEffect(() => {
    // Keep the acknowledgement paired with the answer that started this
    // presentation, even if a regenerated answer replaces its props.
    const markPresented = onPresentedRef.current
    if (!animate || reducedMotion) {
      setReveal({ content, count: Infinity })
      markPresented?.(sourceContent)
      return
    }
    let shown = 0
    const length = Array.from(content).length
    // At most ~1.8 seconds of presentation delay, independent of answer length.
    const chunk = Math.max(8, Math.ceil(length / 55))
    setReveal({ content, count: 0 })
    const timer = window.setInterval(() => {
      shown += chunk
      const scroller = root.current?.closest<HTMLElement>('.conversation-scroll, .route-discussion-history, .execution-discussion-scroll')
      const follow = scroller && scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight < 100
      setReveal({ content, count: shown })
      if (follow) requestAnimationFrame(() => { if (scroller.isConnected) scroller.scrollTop = scroller.scrollHeight })
      if (shown >= length) {
        window.clearInterval(timer)
        markPresented?.(sourceContent)
      }
    }, 32)
    timerRef.current = timer
    return () => {
      window.clearInterval(timer)
      // Closing a panel ends presentation only. On reopening, show its saved
      // answer in full, including when the user closed it during the reveal.
      markPresented?.(sourceContent)
    }
  }, [content, sourceContent, animate, reducedMotion])
  return <div className="assistant-answer" ref={root} aria-busy={!complete}>
    <Markdown skipHtml remarkPlugins={[remarkGfm, [revealText, { limit: count }]]}
      components={{ table: ({ children }) => <AnswerTable>{children}</AnswerTable>, a: ({ children, href }) => <a href={href || undefined} target="_blank" rel="noreferrer">{children}</a>, img: ({ alt }) => <span>{alt}</span> }}>{content}</Markdown>
    {!complete && <button className="answer-reveal-skip" type="button" onClick={() => { window.clearInterval(timerRef.current); setReveal({ content, count: Infinity }); onPresentedRef.current?.(sourceContent) }}>显示全文</button>}
  </div>
}
