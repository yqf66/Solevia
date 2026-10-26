import { ConnectionAwareError } from '../ui/ConnectionAwareError'
import { submitOnEnter } from '../ui/submitOnEnter'
import { useEffect, useRef, useState } from 'react'
import type { FormEvent } from 'react'
import { Globe2, Maximize2, MessageCircle, Minimize2, Send, X } from 'lucide-react'
import { useLocation, useNavigate } from 'react-router-dom'
import { getExecutionDiscussion, revisePlan, retryExecutionDiscussion, sendExecutionDiscussionMessage, dismissExecutionReview, type ExecutionDiscussionWorkspace, type GoalMaterial } from '../api'
import { useResearchResponse } from '../ui/useResearchResponse'
import { ResearchProgress } from '../ui/ResearchProgress'
import { useWebResearch } from '../ui/useWebResearch'
import { ResearchConnectionStatus } from '../ui/ResearchConnectionStatus'
import { ASSISTANT_NAME, scheduleTypeLabels, displayMessageContent } from '../ui/plannerPresentation'
import { AnswerResearch } from '../ui/AnswerResearch'
import { AssistantAnswer, useFreshAnswers } from '../ui/AssistantAnswer'
import { AutoResizeTextarea } from '../ui/AutoResizeTextarea'
import { OperationProgress } from '../ui/ProcessDetails'
import { MaterialWorkspacePanel } from '../ui/MaterialWorkspacePanel'
import { CodexComposerSettings, useCodexSelectionPending } from '../codex/CodexModelSettings'
import './ExecutionDiscussionDock.css'

const executionIntentLabels: Record<string, string> = {
  QUESTION: '问题',
  INSIGHT: '心得',
  CONSTRAINT_CHANGE: '条件变化',
  PROGRESS_REPORT: '进展',
  DIFFICULTY: '困难',
  PLAN_CHANGE_REQUEST: '调整想法',
  ROUTE_CHANGE_SIGNAL: '方向变化',
  REVIEW_REQUEST: '复盘请求',
  NO_ACTION: '仅讨论',
}

export function ExecutionDiscussionDock({ goalId }: { goalId: string }) {
  const navigate = useNavigate()
  const location = useLocation()
  const [open, setOpen] = useState(false)
  const [expanded, setExpanded] = useState(false)
  const [workspace, setWorkspace] = useState<ExecutionDiscussionWorkspace | null>(null)
  const [draft, setDraft] = useState('')
  const [localBusy, setBusy] = useState(false)
  const [operationLabel, setOperationLabel] = useState('正在结合执行进度生成回答')
  const freshAnswer = useFreshAnswers(goalId, workspace?.goal_id === goalId ? workspace?.messages : undefined)
  const research = useResearchResponse(goalId, ['EXECUTION'], getExecutionDiscussion, setWorkspace)
  const modelSelectionPending = useCodexSelectionPending()
  const busy = localBusy || research.busy || modelSelectionPending
  const [error, setError] = useState('')
  const webConnection = useWebResearch(goalId)
  const { enabled: webResearchEnabled, setEnabled: setWebResearchEnabled } = webConnection
  const [attachedMaterials, setAttachedMaterials] = useState<GoalMaterial[]>([])
  const attachmentsPending = attachedMaterials.some((m) => m.latest_revision?.status !== 'READY')
  const messagesEndRef = useRef<HTMLDivElement | null>(null)
  const activeGoalIdRef = useRef<string | null>(goalId)

  useEffect(() => {
    if (research.busy) setOpen(true)
  }, [research.busy])

  useEffect(() => {
    let active = true
    getExecutionDiscussion(goalId).then((result) => {
      if (active) setWorkspace(result)
    }).catch((caught) => {
      if (active) setError(caught instanceof Error ? caught.message : '暂时无法读取执行讨论。')
    })
    return () => { active = false }
  }, [goalId])

  useEffect(() => {
    activeGoalIdRef.current = goalId
    setWorkspace(null)
    setDraft('')
    setBusy(false)
    setError('')
    setWebResearchEnabled(false)
    setAttachedMaterials([])
    setOpen(false)
    setExpanded(false)
    return () => { activeGoalIdRef.current = null }
  }, [goalId])

  useEffect(() => {
    const params = new URLSearchParams(location.search)
    if (params.get('discussion') !== '1') return
    setOpen(true)
    const commitment = params.get('commitment')
    if (commitment && !draft) {
      setDraft(`请结合“${commitment}”这项完成要求，看看当前进度是否需要调整安排。`)
    }
  }, [draft, goalId, location.search])

  useEffect(() => {
    if (open) messagesEndRef.current?.scrollIntoView?.({ block: 'nearest' })
  }, [open, workspace?.messages?.length])

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    const content = draft.trim()
    if (!content || busy || attachmentsPending) return
    setBusy(true)
    setOperationLabel('正在结合执行进度生成回答')
    setError('')
    setDraft('')
    try {
      if (webResearchEnabled) {
        await research.start(content, 'EXECUTION', 'MESSAGE', attachedMaterials.map((material) => material.source_id))
        return
      }
      if (activeGoalIdRef.current !== goalId) return
      const result = await sendExecutionDiscussionMessage(goalId, content, {
        materialSourceIds: attachedMaterials.map((material) => material.source_id),
      })
      if (activeGoalIdRef.current !== goalId) return
      setWorkspace(result)
    } catch (caught) {
      if (activeGoalIdRef.current !== goalId) return
      setDraft(content)
      setError(caught instanceof Error ? caught.message : '这次讨论暂时没有完成。')
    } finally {
      if (activeGoalIdRef.current === goalId) setBusy(false)
    }
  }

  async function retry() {
    if (busy) return
    setBusy(true)
    setError('')
    try {
      const messages = (workspace?.messages ?? []).filter((item) => item.role !== 'SYSTEM')
      const pendingUser = [...messages].reverse().find((item) => item.role === 'USER')
      if (pendingUser) {
        // Use the same full generation path as a normal message. This also
        // recovers a durable user turn that has no assistant row yet.
        const result = await sendExecutionDiscussionMessage(goalId, pendingUser.content, { regenerate: true })
        if (activeGoalIdRef.current !== goalId) return
        setWorkspace(result)
        const latest = result.messages.filter((item) => item.role !== 'SYSTEM').at(-1)
        if (!latest || latest.role !== 'ASSISTANT' || latest.generation_status !== 'SUCCEEDED') {
          setError('这次重新生成没有完成，原问题仍已保留，请再次点击重试。')
        }
      } else {
        const result = await retryExecutionDiscussion(goalId)
        if (activeGoalIdRef.current !== goalId) return
        setWorkspace(result)
      }
    } catch (caught) {
      if (activeGoalIdRef.current !== goalId) return
      setError(caught instanceof Error ? caught.message : '暂时无法重试。')
    } finally {
      if (activeGoalIdRef.current === goalId) setBusy(false)
    }
  }

  async function regenerate() {
    if (busy || !workspace) return
    const messages = workspace.messages.filter((item) => item.role !== 'SYSTEM')
    const assistant = messages.at(-1)
    const user = messages.at(-2)
    if (!assistant || assistant.role !== 'ASSISTANT' || assistant.generation_status !== 'SUCCEEDED' || !user || user.role !== 'USER') return
    setBusy(true)
    setError('')
    try {
      const result = await sendExecutionDiscussionMessage(goalId, user.content, { regenerate: true })
      if (activeGoalIdRef.current !== goalId) return
      setWorkspace(result)
    } catch (caught) {
      if (activeGoalIdRef.current !== goalId) return
      setError(caught instanceof Error ? caught.message : '暂时无法重新生成。')
    } finally {
      if (activeGoalIdRef.current === goalId) setBusy(false)
    }
  }

  async function dismissReview() {
    if (!workspace?.active_review || busy) return
    setBusy(true)
    try {
      const result = await dismissExecutionReview(goalId, workspace.active_review.id)
      if (activeGoalIdRef.current !== goalId) return
      setWorkspace(result)
    } catch (caught) {
      if (activeGoalIdRef.current !== goalId) return
      setError(caught instanceof Error ? caught.message : '暂时无法忽略这次复盘。')
    } finally {
      if (activeGoalIdRef.current === goalId) setBusy(false)
    }
  }

  async function prepareAdjustment() {
    const request = workspace?.latest_analysis?.adjustment_signal.request
    if (!request || busy) return
    setBusy(true)
    setOperationLabel('正在整理可确认的调整建议')
    setError('')
    try {
      const result = await revisePlan(goalId, request)
      if (activeGoalIdRef.current !== goalId) return
      const latestMessage = result.messages?.at(-1)
      if (latestMessage?.generation_status === 'FAILED' || !result.pending_adjustment) {
        setError('调整建议暂时没有整理完成，当前计划没有变化。你可以留在这里继续补充或稍后重试。')
        return
      }
      setOpen(false)
      navigate(`/goals/${goalId}/plan?discussion=1&adjustment=1`)
    } catch (caught) {
      if (activeGoalIdRef.current !== goalId) return
      setError(caught instanceof Error ? caught.message : '暂时无法生成调整建议。')
    } finally {
      if (activeGoalIdRef.current === goalId) setBusy(false)
    }
  }

  const confirmedInsights = workspace?.insights?.filter((item) => item.status === 'CONFIRMED') ?? []
  const latestAssistant = [...(workspace?.messages ?? [])].reverse().find((item) => item.role === 'ASSISTANT')
  const hasFailedReply = latestAssistant?.generation_status === 'FAILED'
  const hasPendingReply = latestAssistant?.generation_status === 'GENERATING' || latestAssistant?.generation_status === 'PENDING'
  const visibleMessages = (workspace?.messages ?? []).filter((item) => item.role !== 'SYSTEM')
  const hasUnansweredReply = visibleMessages.at(-1)?.role === 'USER'
  const reviewSignal = workspace?.active_review
    ? { recommended: true, reason: workspace.active_review.reason }
    : workspace?.progress?.review_signal
  const needsAttention = Boolean(reviewSignal?.recommended) || hasFailedReply || hasPendingReply || hasUnansweredReply

  return <>
    <button
      className={`execution-discussion-trigger${needsAttention ? ' needs-attention' : ''}`}
      type="button"
      aria-label="与 Solevia 讨论当前执行"
      title="与 Solevia 讨论当前执行"
      onClick={() => setOpen(true)}
    >
      <MessageCircle size={21} aria-hidden="true" />
      <span className="execution-discussion-trigger-label">和 {ASSISTANT_NAME} 讨论</span>
      {needsAttention && <span className="execution-discussion-attention" aria-hidden="true" />}
    </button>
    {open && <aside className={`execution-discussion-panel${expanded ? ' is-expanded' : ''}`} aria-label="执行讨论">
      <header>
        <div><p className="eyebrow">执行中的讨论</p><h2>和 {ASSISTANT_NAME} 说说近况</h2></div>
        <div className="execution-discussion-actions">
          <button className="icon-button execution-discussion-expand" type="button" aria-label={expanded ? '收起执行讨论' : '展开执行讨论'} title={expanded ? '恢复小窗' : '展开阅读'} onClick={() => setExpanded((value) => !value)}>{expanded ? <Minimize2 size={16} /> : <Maximize2 size={16} />}</button>
          <button className="icon-button" type="button" aria-label="关闭执行讨论" title="关闭" onClick={() => setOpen(false)}><X size={18} /></button>
        </div>
      </header>
      <p className="execution-discussion-context">{workspace ? `延续前期讨论 · ${workspace.planning_mode === 'EXPLORATORY' ? '先试试看' : '直接固定规划'} · ${scheduleTypeLabels[workspace.schedule_type]}` : '正在读取当前计划…'}</p>
      <div className="execution-discussion-scroll">
        {reviewSignal?.recommended && <section className="execution-review-prompt">
          <strong>适合做一次阶段复盘</strong>
          <p>{reviewSignal.reason}</p>
          <div><button type="button" onClick={() => setDraft(`请结合我目前的完成情况和之前聊过的内容，带我复盘一下当前阶段。`)}>开始复盘</button>{workspace?.active_review && <button type="button" onClick={() => void dismissReview()}>稍后再说</button>}</div>
        </section>}
        {(workspace?.messages?.length ?? 0) === 0 && <div className="execution-discussion-empty"><strong>接着之前的讨论说就可以</strong><p>进展、卡点、心得或新想法都可以。Solevia 会结合前期讨论和当前执行继续理解；明确的信息会自动记住，真正修改安排前才需要你确认。</p></div>}
        <div className="execution-discussion-messages">
          {workspace?.messages?.filter((item) => item.role !== 'SYSTEM').map((message) => <article className={message.role.toLowerCase()} key={message.id}>
            <span>{message.role === 'USER' ? '你' : ASSISTANT_NAME}</span>
            {Array.isArray(message.metadata?.material_titles) && <small className="message-materials">{message.metadata.material_titles.map(String).join(' · ')}</small>}
            {message.generation_status === 'GENERATING' || message.generation_status === 'PENDING' ? <p>正在生成回复…</p>
              : message.role === 'ASSISTANT' ? <AssistantAnswer content={displayMessageContent(message.content, message.generation_status === 'FAILED')} animate={message.generation_status === 'SUCCEEDED' && freshAnswer(message.id)} onPresented={() => freshAnswer.markPresented(message.id, message.content)} /> : <p>{message.content}</p>}
            <AnswerResearch message={message} summary={workspace?.latest_research ?? null} research={research} />
          </article>)}
          <ResearchProgress research={research} />
          <OperationProgress active={localBusy && !research.busy} scope={`${goalId}:${research.current?.id ?? ''}`} label={operationLabel} error={error} />
          <div ref={messagesEndRef} />
        </div>
        {hasFailedReply && <button className="button execution-retry" type="button" disabled={busy} onClick={() => void retry()}>{busy ? '正在重试…' : '重试刚才的回复'}</button>}
        {(hasPendingReply || hasUnansweredReply) && <button className="button execution-retry" type="button" disabled={busy} onClick={() => void retry()}>{busy ? '正在生成…' : '继续生成回复'}</button>}
        {!hasFailedReply && !hasPendingReply && !hasUnansweredReply && latestAssistant?.generation_status === 'SUCCEEDED' && <button className="button execution-retry" type="button" disabled={busy} onClick={() => void regenerate()}>{busy ? '正在生成…' : '重新生成回复'}</button>}
        {workspace?.latest_analysis && workspace.latest_analysis.intents.some((item) => item.type !== 'NO_ACTION') && <div className="execution-intents" aria-label="识别到的讨论内容">{workspace.latest_analysis.intents.filter((item) => item.type !== 'NO_ACTION').map((item, index) => <span key={`${item.type}-${index}`}>{executionIntentLabels[item.type] ?? item.type}</span>)}</div>}
        {confirmedInsights.length > 0 && <details className="execution-confirmed-memory"><summary>讨论中记住的内容 · {confirmedInsights.length}</summary><ul>{confirmedInsights.map((insight) => <li key={insight.id}>{insight.content}</li>)}</ul></details>}
        {workspace?.latest_analysis?.adjustment_signal.recommended && workspace.latest_analysis.adjustment_signal.request && <section className="execution-adjustment-signal"><strong>这可能需要调整计划</strong><p>{workspace.latest_analysis.adjustment_signal.reason}</p><button className="button primary" type="button" disabled={busy} onClick={() => void prepareAdjustment()}>{busy ? '正在整理…' : '生成可确认的调整建议'}</button><small>只会生成预览，不会直接修改当前安排。</small></section>}
        {workspace?.planning_mode === 'EXPLORATORY' && workspace.active_review && !workspace.latest_analysis?.adjustment_signal.recommended && <button className="button exploration-next-entry" type="button" onClick={() => navigate(`/goals/${goalId}/plan`)}>查看并决定下一阶段</button>}
        {error && <ConnectionAwareError message={error} />}
      </div>
      <form className="execution-discussion-compose" onSubmit={submit}>
        {attachedMaterials.length > 0 && <div className="material-attachment-strip">{attachedMaterials.map((m) => <span key={m.source_id}>{m.title}{m.latest_revision?.status !== 'READY' ? ' · 解析中' : m.latest_revision.metadata.cache_reused ? ' · 已复用' : ''}<button type="button" disabled={busy} aria-label={`移除附件 ${m.title}`} onClick={() => setAttachedMaterials((current) => current.filter((item) => item.source_id !== m.source_id))}>×</button></span>)}</div>}
        {attachmentsPending && <small className="material-attachment-hint">解析完成后可发送；失败时可重试或移除附件。</small>}
        <AutoResizeTextarea onKeyDown={submitOnEnter} title="Enter 发送，Shift + Enter 换行" value={draft} onChange={(event) => setDraft(event.target.value)} placeholder="说说进展或卡点…" aria-label="执行讨论内容" />
        <div className="execution-discussion-toolbar">
          <MaterialWorkspacePanel goalId={goalId} compact selectedSourceIds={attachedMaterials.map((m) => m.source_id)} onSelectionChange={setAttachedMaterials} disabled={busy} />
          <CodexComposerSettings disabled={busy} />
          {webConnection.available && <button
            className={`research-toggle-button${webResearchEnabled ? ' active' : ''}`}
            type="button"
            aria-label={webResearchEnabled ? '关闭联网检索' : '开启联网检索'}
            aria-pressed={webResearchEnabled}
            title={webResearchEnabled ? '已允许模型判断并调用联网检索，点击关闭' : '允许模型判断是否需要联网检索'}
            disabled={busy}
            onClick={() => setWebResearchEnabled((current) => !current)}
          ><Globe2 size={16} aria-hidden="true" /></button>}
          <button type="submit" disabled={!draft.trim() || busy || attachmentsPending} aria-label="发送" title="发送"><Send size={18} /></button>
        </div>
      </form>
      <ResearchConnectionStatus connection={webConnection} />
    </aside>}
  </>
}
