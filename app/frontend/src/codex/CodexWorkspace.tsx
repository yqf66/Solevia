import { type FormEvent, useCallback, useEffect, useRef, useState } from 'react'
import { ArrowUp, Check, CircleStop, FileSearch, LoaderCircle, Menu, Plus, RefreshCw, ShieldAlert, X } from 'lucide-react'
import { request } from '../api/client'
import { clearLocalSession, localSession } from '../api/localSession'
import { SoleviaMark, SoleviaWordmark } from '../brand/SoleviaMark'
import { AssistantAnswer } from '../ui/AssistantAnswer'
import './CodexWorkspace.css'

type Message = { id: string; role: 'user' | 'assistant'; content: string; status: string; created_at: number }
type Session = { id: string; title: string; thread_id: string | null }
type Run = { id: string; session_id: string; status: string; error?: string | null; created_at: number; completed_at?: number }
type Question = { id: string; question: string; isSecret?: boolean; options?: { label: string; description: string }[] }
type PendingRequest = { request_id: string; method: string; params: { command?: string; reason?: string; questions?: Question[]; changes?: unknown; permissions?: unknown } }
type Snapshot = { session: Session; messages: Message[]; active_run: Run | null; last_run: Run | null; last_event_id: number; requests: PendingRequest[] }
type RuntimeStatus = { connected: boolean; probe: { local_skills?: string[]; models?: { displayName: string; isDefault: boolean }[] }; error: string | null }
type StreamEvent = { id: number; type: string; payload: Record<string, unknown> }

export function decodeEvent(block: string): StreamEvent | null {
  const lines = block.replaceAll('\r', '').split('\n')
  const data = lines.filter(line => line.startsWith('data:')).map(line => line.slice(5).trimStart()).join('\n')
  if (!data) return null
  return { id: Number(lines.find(line => line.startsWith('id:'))?.slice(3) || 0), type: lines.find(line => line.startsWith('event:'))?.slice(6).trim() || 'message', payload: JSON.parse(data) as Record<string, unknown> }
}

let initial: Promise<string> | null = null
function initialSession() {
  initial ??= request<{ sessions: Session[] }>('/api/codex/sessions').then(async result => {
    const saved = localStorage.getItem('solevia-codex-session')
    return result.sessions.find(session => session.id === saved)?.id || result.sessions[0]?.id || (await request<Session>('/api/codex/sessions', { method: 'POST', body: '{}' })).id
  }).catch(error => { initial = null; throw error })
  return initial
}

function RequestCard({ item, onAnswer }: { item: PendingRequest; onAnswer: (answer: object) => Promise<void> }) {
  const [answers, setAnswers] = useState<Record<string, string>>({})
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const questions = item.params.questions || []
  const input = item.method === 'item/tool/requestUserInput'
  const permissions = item.method === 'item/permissions/requestApproval'
  async function respond(answer: object) {
    setBusy(true); setError('')
    try { await onAnswer(answer) } catch (cause) { setError(cause instanceof Error ? cause.message : '请求处理失败。') }
    finally { setBusy(false) }
  }
  return <div className="codex-approval"><ShieldAlert size={18} aria-hidden="true" /><div>
    <strong>{input ? '需要补充信息' : '需要你的授权'}</strong>
    {item.params.reason && <p>{item.params.reason}</p>}
    {item.params.command && <pre>{item.params.command}</pre>}
    {item.params.changes != null && <pre>{JSON.stringify(item.params.changes, null, 2)}</pre>}
    {permissions && <p>Codex 申请额外权限：{JSON.stringify(item.params.permissions)}。当前版本支持拒绝此类申请。</p>}
    {questions.map(question => <label className="codex-question" key={question.id}>{question.question}
      {question.options?.length ? <select value={answers[question.id] || ''} onChange={event => setAnswers(previous => ({ ...previous, [question.id]: event.target.value }))}><option value="">请选择</option>{question.options.map(option => <option key={option.label} value={option.label}>{option.label} — {option.description}</option>)}</select> : null}
      <input type={question.isSecret ? 'password' : 'text'} value={answers[question.id] || ''} onChange={event => setAnswers(previous => ({ ...previous, [question.id]: event.target.value }))} placeholder="输入你的回答" />
    </label>)}
    {error && <p role="alert">{error}</p>}
    <div className="codex-approval-actions">{input ? <button disabled={busy || questions.some(question => !answers[question.id]?.trim())} className="primary-button" onClick={() => respond({ answers: Object.fromEntries(questions.map(question => [question.id, { answers: [answers[question.id]] }])) })}>提交回答</button> : <>
      {!permissions && <button disabled={busy} className="primary-button" onClick={() => respond({ decision: 'accept' })}><Check size={15} />允许本次</button>}
      <button disabled={busy} className="secondary-button" onClick={() => respond({ decision: 'decline' })}>拒绝</button>
    </>}</div>
  </div></div>
}

export function CodexWorkspace() {
  const [snapshot, setSnapshot] = useState<Snapshot | null>(null)
  const [sessions, setSessions] = useState<Session[]>([])
  const [runtime, setRuntime] = useState<RuntimeStatus | null>(null)
  const [draft, setDraft] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [menuOpen, setMenuOpen] = useState(false)
  const [sending, setSending] = useState(false)
  const [streamState, setStreamState] = useState('')
  const snapshotRef = useRef(snapshot)
  const selectedSession = useRef<string | null>(null)
  const scroller = useRef<HTMLElement | null>(null)
  const followBottom = useRef(true)
  const submission = useRef<{ content: string; id: string } | null>(null)
  snapshotRef.current = snapshot

  const load = useCallback(async (id: string, select = false) => {
    if (select || !selectedSession.current) selectedSession.current = id
    const [next, list, state] = await Promise.all([
      request<Snapshot>(`/api/codex/sessions/${encodeURIComponent(id)}`),
      request<{ sessions: Session[] }>('/api/codex/sessions'),
      request<RuntimeStatus>('/api/codex/status'),
    ])
    if (selectedSession.current !== id) return next
    localStorage.setItem('solevia-codex-session', id)
    snapshotRef.current = next
    setSnapshot(next); setSessions(list.sessions); setRuntime(state); setMenuOpen(false)
    return next
  }, [])

  useEffect(() => {
    let alive = true
    initialSession().then(id => { if (alive) return load(id, true) }).catch((cause: unknown) => { if (alive) setError(cause instanceof Error ? cause.message : '无法连接 Solevia。') })
    return () => { alive = false }
  }, [load])

  useEffect(() => {
    if (followBottom.current && scroller.current) scroller.current.scrollTop = scroller.current.scrollHeight
  }, [snapshot?.messages, snapshot?.requests])

  const runId = snapshot?.active_run?.id
  useEffect(() => {
    if (!runId) { setStreamState(''); return }
    const controller = new AbortController()
    let alive = true
    const current = snapshotRef.current
    if (!current) return
    let cursor = current.last_event_id
    const sessionId = current.session.id
    async function follow() {
      for (let attempt = 0; alive && attempt < 4; attempt += 1) {
        try {
          setStreamState(attempt ? '正在恢复连接…' : 'Codex 正在处理…')
          const token = await localSession(controller.signal)
          const response = await fetch(`/api/codex/runs/${runId}/events?after=${cursor}`, { headers: { 'X-Solevia-Token': token, Accept: 'text/event-stream' }, signal: controller.signal, cache: 'no-store', credentials: 'same-origin', redirect: 'error' })
          if (response.status === 401) { clearLocalSession(); throw new Error('本地服务已重启，请刷新查看保留的回答。') }
          if (!response.ok || !response.body) throw new Error('无法接收实时回答。')
          const reader = response.body.getReader()
          const decoder = new TextDecoder()
          let buffer = ''
          let terminal = false
          while (alive) {
            const chunk = await reader.read()
            if (chunk.done) break
            buffer += decoder.decode(chunk.value, { stream: true }).replaceAll('\r\n', '\n')
            const blocks = buffer.split('\n\n')
            buffer = blocks.pop() || ''
            for (const block of blocks) {
              const event = decodeEvent(block)
              if (!event || event.id <= cursor) continue
              cursor = event.id
              const payload = event.payload
              if (event.type === 'assistant_content' || event.type === 'assistant_delta') {
                setSnapshot(previous => previous && previous.session.id === sessionId ? { ...previous, last_event_id: cursor, messages: previous.messages.map(message => message.id === payload.message_id ? { ...message, content: event.type === 'assistant_content' ? String(payload.content) : message.content + String(payload.delta || '') } : message) } : previous)
              } else if (event.type === 'request') {
                setSnapshot(previous => previous ? { ...previous, requests: [...previous.requests.filter(item => item.request_id !== payload.request_id), payload as unknown as PendingRequest] } : previous)
              } else if (event.type === 'request_answered') {
                setSnapshot(previous => previous ? { ...previous, requests: previous.requests.filter(item => item.request_id !== payload.request_id) } : previous)
              } else if (event.type === 'notice') {
                setStreamState(String(payload.text))
              } else if (event.type === 'run_completed') {
                terminal = true
              }
            }
          }
          if (!alive) return
          const next = await load(sessionId)
          cursor = next.last_event_id
          if (terminal || !next.active_run) return
        } catch (cause: unknown) {
          if (!alive) return
          if (attempt === 3) {
            setError(cause instanceof Error ? cause.message : '实时连接已中断，回答仍保留在本地。')
            setStreamState('连接已中断')
            return
          }
        }
        await new Promise(resolve => setTimeout(resolve, 1000))
        if (alive) {
          try {
            const next = await load(sessionId)
            cursor = next.last_event_id
            if (!next.active_run) return
          } catch { /* Retry reads only; never replay the user message. */ }
        }
      }
    }
    void follow()
    return () => { alive = false; controller.abort() }
  }, [runId, load])

  async function submit(event: FormEvent) {
    event.preventDefault()
    const content = draft.trim()
    if (!content || !snapshot || snapshot.active_run || sending) return
    setSending(true); setError(null)
    submission.current = submission.current?.content === content ? submission.current : { content, id: crypto.randomUUID() }
    try {
      await request<Run>(`/api/codex/sessions/${snapshot.session.id}/messages`, { method: 'POST', body: JSON.stringify({ content, client_request_id: submission.current.id }) })
      submission.current = null; setDraft('')
      await load(snapshot.session.id)
    } catch (cause: unknown) {
      setError(cause instanceof Error ? cause.message : '消息发送失败。')
      await load(snapshot.session.id).catch(() => undefined)
    } finally { setSending(false) }
  }

  async function newSession() {
    try { const session = await request<Session>('/api/codex/sessions', { method: 'POST', body: '{}' }); followBottom.current = true; await load(session.id, true); setDraft(''); setError(null) }
    catch (cause) { setError(cause instanceof Error ? cause.message : '无法新建对话。') }
  }

  async function stop() {
    if (!snapshot?.active_run) return
    try {
      await request(`/api/codex/runs/${snapshot.active_run.id}/cancel`, { method: 'POST', body: '{}' })
      setStreamState('正在停止…')
    } catch (cause) { setError(cause instanceof Error ? cause.message : '停止请求失败。') }
  }

  if (!snapshot) return <main className="codex-loading">{error ? <div role="alert"><p>{error}</p><button className="primary-button" onClick={() => window.location.reload()}>重新连接</button></div> : <><LoaderCircle className="spin" size={20} />正在打开 Solevia 对话…</>}</main>
  const busy = Boolean(snapshot.active_run) || sending
  const connected = runtime?.connected
  const runtimeLabel = connected ? 'Codex 已连接' : 'Codex 未连接'
  return <div className="codex-workspace">
    <aside className={`codex-sidebar ${menuOpen ? 'open' : ''}`}>
      <div className="codex-sidebar-head"><SoleviaWordmark /><button className="icon-button mobile-only" onClick={() => setMenuOpen(false)} aria-label="关闭菜单"><X size={18} /></button></div>
      <p className="codex-eyebrow">工作台</p><div className="codex-nav active"><FileSearch size={16} />Codex 对话</div>
      <button className="codex-new" onClick={newSession}><Plus size={16} />新建对话</button>
      <nav className="codex-session-list" aria-label="最近对话"><span>最近对话</span>{sessions.map(session => <button key={session.id} className={`codex-session ${session.id === snapshot.session.id ? 'current' : ''}`} onClick={() => { followBottom.current = true; void load(session.id, true).catch(cause => setError(String(cause))) }} title={session.title}>{session.title}</button>)}</nav>
      <div className="codex-sidebar-foot"><span className={`status-dot ${connected ? 'online' : ''}`} />{runtimeLabel}<button className="icon-button" onClick={() => load(snapshot.session.id).catch(cause => setError(String(cause)))} aria-label="刷新状态"><RefreshCw size={15} /></button></div>
    </aside>
    {menuOpen && <button className="codex-scrim mobile-only" onClick={() => setMenuOpen(false)} aria-label="关闭菜单" />}
    <main className="codex-main">
      <header className="codex-header"><button className="icon-button mobile-only" onClick={() => setMenuOpen(true)} aria-label="打开菜单"><Menu size={19} /></button><div><p className="codex-kicker">SOLEViA / CODEX</p><h1>和 Solevia 一起完成下一步</h1></div><div className="codex-runtime-pill"><span className={`status-dot ${connected ? 'online' : ''}`} />{runtimeLabel}</div></header>
      <section className="codex-conversation" aria-label="对话记录" ref={scroller} onScroll={event => { const element = event.currentTarget; followBottom.current = element.scrollHeight - element.scrollTop - element.clientHeight < 90 }}>
        {snapshot.messages.length === 0 && <div className="codex-empty"><SoleviaMark className="codex-empty-mark" /><h2>从一个真实目标开始</h2><p>告诉我你想完成什么，Codex 会在 Solevia 内选择合适的技能，帮你回答、检索或拆解计划。</p><div className="codex-suggestions"><button onClick={() => setDraft('帮我把这个目标拆成一份可执行计划：')}>拆解一个目标</button><button onClick={() => setDraft('请检索并比较以下问题的可靠资料：')}>检索可靠资料</button></div></div>}
        {snapshot.messages.map(message => <article className={`codex-message ${message.role}`} key={message.id}><div className="codex-message-label">{message.role === 'user' ? '你' : 'Solevia'}{message.role === 'assistant' && message.status !== 'streaming' && message.status !== 'succeeded' ? <span> · {message.status === 'interrupted' ? '已中断' : '未完成'}</span> : null}</div>{message.role === 'assistant' ? <AssistantAnswer content={message.content || (message.status === 'streaming' ? '正在整理回答…' : '这次没有收到回答内容。')} /> : <p>{message.content}</p>}</article>)}
        {snapshot.requests.map(item => <RequestCard key={item.request_id} item={item} onAnswer={async answer => {
          await request(`/api/codex/requests/${item.request_id}/respond`, { method: 'POST', body: JSON.stringify(answer) })
          await load(snapshot.session.id)
        }} />)}
        {snapshot.last_run?.error && <p className="codex-run-error" role="alert">{snapshot.last_run.error}</p>}
      </section>
      {(error || runtime?.error) && <div className="codex-error" role="alert">{error || runtime?.error}</div>}
      <form className="codex-composer" onSubmit={submit}><textarea value={draft} onChange={event => setDraft(event.target.value)} onKeyDown={event => { if (event.key === 'Enter' && (event.ctrlKey || event.metaKey)) event.currentTarget.form?.requestSubmit() }} placeholder="描述你要完成的事情…" rows={2} aria-label="发送给 Solevia" /><div className="codex-composer-foot"><span role="status">{busy ? streamState || '正在提交…' : `${runtime?.probe.local_skills?.length || 0} 个 Solevia 技能 · Ctrl + Enter 发送`}</span>{snapshot.active_run ? <button className="stop-button" type="button" onClick={stop}><CircleStop size={17} />停止回答</button> : <button className="send-button" type="submit" disabled={busy || !draft.trim() || !connected} aria-label="发送"><ArrowUp size={18} /></button>}</div></form>
    </main>
  </div>
}
