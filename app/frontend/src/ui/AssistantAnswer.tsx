import { useEffect, useRef, useState } from 'react'
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
  return (id: string) => history.current.fresh.has(id)
}

type MarkdownNode = { type: string; value?: string; children?: MarkdownNode[] }

// Parse complete Markdown first, then reveal text nodes. Bold/link/code markup
// therefore stays intact while characters appear, even across a delimiter.
function revealText({ limit }: { limit: number }) {
  return (tree: MarkdownNode) => {
    let remaining = limit
    function visit(node: MarkdownNode) {
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

export function AssistantAnswer({ content, animate = false }: { content: string; animate?: boolean }) {
  content = normalizeAssistantName(content)
  const reducedMotion = typeof window.matchMedia === 'function' && window.matchMedia('(prefers-reduced-motion: reduce)').matches
  const [reveal, setReveal] = useState({ content, count: animate && !reducedMotion ? 0 : Infinity })
  const root = useRef<HTMLDivElement>(null)
  const timerRef = useRef<ReturnType<typeof window.setInterval> | undefined>(undefined)
  const count = reveal.content === content ? reveal.count : animate && !reducedMotion ? 0 : Infinity
  const complete = count >= Array.from(content).length
  useEffect(() => {
    if (!animate || reducedMotion) { setReveal({ content, count: Infinity }); return }
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
      if (shown >= length) window.clearInterval(timer)
    }, 32)
    timerRef.current = timer
    return () => window.clearInterval(timer)
  }, [content, animate, reducedMotion])
  return <div className="assistant-answer" ref={root} aria-busy={!complete}>
    <Markdown skipHtml remarkPlugins={[remarkGfm, [revealText, { limit: count }]]}
      components={{ a: ({ children, href }) => <a href={href || undefined} target="_blank" rel="noreferrer">{children}</a>, img: ({ alt }) => <span>{alt}</span> }}>{content}</Markdown>
    {!complete && <button className="answer-reveal-skip" type="button" onClick={() => { window.clearInterval(timerRef.current); setReveal({ content, count: Infinity }) }}>显示全文</button>}
  </div>
}
