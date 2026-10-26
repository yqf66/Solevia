import { ConnectionAwareError, isModelConnectionMessage } from '../ui/ConnectionAwareError'
import { usesCodexKernel } from '../api/kernelMode'
import { submitOnEnter } from '../ui/submitOnEnter'
import { useNotebookDrawer } from './useNotebookDrawer'
import { ClarificationNotebook } from './ClarificationNotebook'
import { SoleviaMark } from '../brand/SoleviaMark'
import './ClarificationPage.css'
import { useEffect, useRef, useState } from 'react'
import type { FormEvent } from 'react'
import { Globe2, FileText, X } from 'lucide-react'
import { NavLink, Navigate, useNavigate, useParams } from 'react-router-dom'
import { confirmRequirements, discardRequirementChanges, getClarification, prepareRequirementReview, regenerateClarificationMessage, selectPlanningApproach, selectScheduleType, retryClarificationMessage, retryClarificationAnalysis, sendClarificationMessage, startInitialClarification, updateRequirementFact, type ClarificationWorkspace, type RequirementFact, type GoalMaterial } from '../api'
import { useResearchResponse } from '../ui/useResearchResponse'
import { ResearchProgress } from '../ui/ResearchProgress'
import { useWebResearch } from '../ui/useWebResearch'
import { ResearchConnectionStatus } from '../ui/ResearchConnectionStatus'
import { PlannerStepper } from '../ui/PlannerStage'
import { factLabels, ASSISTANT_NAME, displayMessageContent, scheduleTypeLabels } from '../ui/plannerPresentation'
import { AnswerResearch } from '../ui/AnswerResearch'
import { AssistantAnswer, useFreshAnswers } from '../ui/AssistantAnswer'
import { OperationProgress } from '../ui/ProcessDetails'
import { MaterialWorkspacePanel } from '../ui/MaterialWorkspacePanel'
import { CodexComposerSettings, useCodexSelectionPending } from '../codex/CodexModelSettings'

const CLARIFICATION_RETRY_POLL_MS = 1500


async function waitForClarificationRetryResult(
  goalId: string,
  failedMessageId: string,
  failedMessageSequence: number,
  watch: { cancelled: boolean },
  onWorkspace: (workspace: ClarificationWorkspace) => void,
): Promise<ClarificationWorkspace> {
  const deadline = Date.now() + (usesCodexKernel() ? 21_600_000 : 120_000)
  while (Date.now() < deadline) {
    await new Promise<void>((resolve) => window.setTimeout(resolve, CLARIFICATION_RETRY_POLL_MS))
    if (watch.cancelled) throw new Error('Clarification retry polling cancelled')
    try {
      const latest = await getClarification(goalId)
      if (watch.cancelled) throw new Error('Clarification retry polling cancelled')
      onWorkspace(latest)
      const completedReply = latest.messages.find((message) => (
        message.role === 'ASSISTANT'
        && message.generation_status === 'SUCCEEDED'
        && (message.id === failedMessageId || message.sequence >= failedMessageSequence)
      ))
      if (completedReply) return latest
    } catch (caught) {
      if (watch.cancelled) throw caught
      // A transient read failure must not hide a retry that is still running server-side.
    }
  }
  throw new Error('这次生成仍在服务端处理中，请稍后再试；已经生成的结果不会丢失。')
}

export function ClarificationPage() {
  const { goalId } = useParams()
  const [workspace, setWorkspace] = useState<ClarificationWorkspace | null>(null)
  const [reply, setReply] = useState('')
  const [localSending, setIsSending] = useState(false)
  const freshAnswer = useFreshAnswers(goalId, workspace?.goal_id === goalId ? workspace?.messages : undefined)
  const research = useResearchResponse(goalId, ['CLARIFICATION'], getClarification, setWorkspace)
  const modelSelectionPending = useCodexSelectionPending()
  const isSending = localSending || research.busy || modelSelectionPending
  const webConnection = useWebResearch(goalId)
  const { enabled: webResearchEnabled, setEnabled: setWebResearchEnabled } = webConnection
  const [loadError, setLoadError] = useState('')
  const [actionError, setActionError] = useState('')
  const [editingFactId, setEditingFactId] = useState<string | null>(null)
  const [editValue, setEditValue] = useState('')
  const [clarifyingFactId, setClarifyingFactId] = useState<string | null>(null)
  const [pendingFactId, setPendingFactId] = useState<string | null>(null)
  const [isContinuing, setIsContinuing] = useState(false)
  const [isConfirming, setIsConfirming] = useState(false)
  const [isPreparingReview, setIsPreparingReview] = useState(false)
  const [isRetryingAnalysis, setIsRetryingAnalysis] = useState(false)
  const [isDiscardingChanges, setIsDiscardingChanges] = useState(false)
  const [isSelectingApproach, setIsSelectingApproach] = useState(false)
  const [pendingReply, setPendingReply] = useState('')
  const [attachedMaterials, setAttachedMaterials] = useState<GoalMaterial[]>([])
  const [isStartingInitialReply, setIsStartingInitialReply] = useState(false)
  const [initialReplyRetryKey, setInitialReplyRetryKey] = useState(0)
  const [showJumpToLatest, setShowJumpToLatest] = useState(false)
  const conversationScrollRef = useRef<HTMLDivElement>(null)
  const messageEndRef = useRef<HTMLDivElement>(null)
  const clarificationInputRef = useRef<HTMLTextAreaElement>(null)
  const shouldFollowLatestRef = useRef(true)
  const initialReplyStartedRef = useRef<string | null>(null)
  const navigate = useNavigate()
  const { open: contextOpen, setOpen: setContextOpen, drawerRef, triggerRef } = useNotebookDrawer()


  useEffect(() => {
    setWorkspace(null)
    setReply('')
    setIsSending(false)
    setWebResearchEnabled(false)
    setLoadError('')
    setActionError('')
    setPendingReply('')
    setAttachedMaterials([])
    setIsStartingInitialReply(false)
    initialReplyStartedRef.current = null
  }, [goalId])

  useEffect(() => {
    if (!goalId) return
    let active = true
    getClarification(goalId)
      .then((result) => {
        if (active) setWorkspace(result)
      })
      .catch((caught) => {
        if (active) setLoadError(caught instanceof Error ? caught.message : '暂时无法读取目标。')
      })
    return () => { active = false }
  }, [goalId])

  const pendingInitialMessage = workspace?.messages.find((message) => (
    message.role === 'ASSISTANT'
    && message.generation_status === 'PENDING'
    && message.metadata?.initial_response === true
  ))
  const pendingInitialMessageId = pendingInitialMessage?.id
  const currentAnalysisJob = workspace?.analysis_jobs?.find((job) => job.status === 'FAILED')
    ?? workspace?.analysis_jobs?.find((job) => job.status !== 'SUCCEEDED')
    ?? workspace?.analysis_jobs?.[0]
  const analysisPending = Boolean(workspace?.analysis_jobs?.some((job) => job.status !== 'SUCCEEDED'))
  const currentAnalysisJobId = currentAnalysisJob?.id
  const currentAnalysisJobStatus = currentAnalysisJob?.status
  const attachedMaterialsPending = attachedMaterials.some((material) => (
    material.latest_revision?.status !== 'READY'
  ))

  useEffect(() => {
    if (!goalId || !pendingInitialMessageId || initialReplyStartedRef.current === pendingInitialMessageId) return
    initialReplyStartedRef.current = pendingInitialMessageId
    setIsStartingInitialReply(true)
    setActionError('')
    startInitialClarification(goalId)
      .then((result) => setWorkspace(result))
      .catch((caught) => {
        initialReplyStartedRef.current = null
        setActionError(caught instanceof Error ? caught.message : 'Solevia 暂时没有连上，请重试。')
      })
      .finally(() => setIsStartingInitialReply(false))
  }, [goalId, pendingInitialMessageId, initialReplyRetryKey])

  useEffect(() => {
    if (!workspace) return
    if (shouldFollowLatestRef.current) {
      const target = messageEndRef.current
      const scroller = conversationScrollRef.current
      if (target && typeof target.scrollIntoView === 'function') {
        target.scrollIntoView({ block: 'end' })
      } else if (scroller) {
        scroller.scrollTop = scroller.scrollHeight
      }
      setShowJumpToLatest(false)
    }
  }, [workspace, pendingReply])

  useEffect(() => {
    const input = clarificationInputRef.current
    if (!input) return
    input.style.height = 'auto'
    input.style.height = `${Math.max(44, input.scrollHeight)}px`
  }, [reply])

  useEffect(() => {
    if (!goalId || !currentAnalysisJobId || !currentAnalysisJobStatus || !['PENDING', 'PROCESSING'].includes(currentAnalysisJobStatus)) return
    let active = true
    const timer = window.setInterval(() => {
      getClarification(goalId)
        .then((result) => {
          if (active) setWorkspace(result)
        })
        .catch(() => {
          // The durable backend job remains the source of truth; a missed poll is harmless.
        })
    }, 2000)
    return () => {
      active = false
      window.clearInterval(timer)
    }
  }, [goalId, currentAnalysisJobId, currentAnalysisJobStatus])

  function handleConversationScroll() {
    const scroller = conversationScrollRef.current
    if (!scroller) return
    const distanceFromBottom = scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight
    const isNearBottom = distanceFromBottom < 90
    shouldFollowLatestRef.current = isNearBottom
    setShowJumpToLatest(!isNearBottom)
  }

  function jumpToLatest() {
    shouldFollowLatestRef.current = true
    const target = messageEndRef.current
    const scroller = conversationScrollRef.current
    if (target && typeof target.scrollIntoView === 'function') {
      target.scrollIntoView({ behavior: 'smooth', block: 'end' })
    } else if (scroller) {
      scroller.scrollTop = scroller.scrollHeight
    }
    setShowJumpToLatest(false)
  }

  async function handleReply(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (!goalId || !reply.trim() || isSending) return
    const submittedReply = reply.trim()
    shouldFollowLatestRef.current = true
    setPendingReply(submittedReply)
    setReply('')
    setIsSending(true)
    setActionError('')
    try {
      if (webResearchEnabled) {
        await research.start(submittedReply, 'CLARIFICATION', 'MESSAGE', attachedMaterials.map((material) => material.source_id))
        return
      }
      const result = await sendClarificationMessage(goalId, submittedReply, { materialSourceIds: attachedMaterials.map((material) => material.source_id) })
      setWorkspace(result)
      setIsContinuing(false)
      setClarifyingFactId(null)
    } catch (caught) {
      setReply(submittedReply)
      setActionError(caught instanceof Error ? caught.message : '回答发送失败，请重试。')
    } finally {
      setPendingReply('')
      setIsSending(false)
    }
  }

  async function retryFailedClarification() {
    if (!goalId || isSending) return
    const failedMessage = [...(workspace?.messages ?? [])]
      .reverse()
      .find((message) => message.role === 'ASSISTANT' && message.generation_status === 'FAILED')
    shouldFollowLatestRef.current = true
    setIsSending(true)
    setActionError('')
    const retryWatch = { cancelled: false }
    try {
      const retryRequest = retryClarificationMessage(goalId)
      const result = failedMessage
        ? await Promise.race([
            retryRequest,
            waitForClarificationRetryResult(
              goalId,
              failedMessage.id,
              failedMessage.sequence,
              retryWatch,
              setWorkspace,
            ),
          ])
        : await retryRequest
      setWorkspace(result)
    } catch (caught) {
      setActionError(caught instanceof Error ? caught.message : '暂时无法重试，请稍后再试。')
    } finally {
      retryWatch.cancelled = true
      setIsSending(false)
    }
  }

  function editFailedClarification(messageIndex: number) {
    const original = workspace?.messages[messageIndex - 1]
    if (!original || original.role !== 'USER') return
    setReply(original.content)
    window.requestAnimationFrame(() => clarificationInputRef.current?.focus())
  }

  async function regenerateClarification() {
    if (!goalId || isSending) return
    setIsSending(true)
    setActionError('')
    try {
      setWorkspace(await regenerateClarificationMessage(goalId))
    } catch (caught) {
      setActionError(caught instanceof Error ? caught.message : '暂时无法重新生成回复。')
    } finally {
      setIsSending(false)
    }
  }

  async function handleConfirmRequirements() {
    if (!goalId || isConfirming) return
    setIsConfirming(true)
    setActionError('')
    try {
      const result = await confirmRequirements(goalId)
      setWorkspace(result)
      navigate(`/goals/${goalId}/route`)
    } catch (caught) {
      setActionError(caught instanceof Error ? caught.message : '需求确认失败，请重试。')
      setIsConfirming(false)
    }
  }

  async function handleSelectPlanningApproach(mode: 'EXPLORATORY' | 'COMMITTED') {
    if (!goalId || isSelectingApproach) return
    setIsSelectingApproach(true)
    setActionError('')
    try {
      setWorkspace(await selectPlanningApproach(goalId, mode))
    } catch (caught) {
      setActionError(caught instanceof Error ? caught.message : '暂时无法保存规划方式。')
    } finally {
      setIsSelectingApproach(false)
    }
  }

  async function handleSelectScheduleType(scheduleType: 'OUTCOME' | 'RHYTHM' | 'CALENDAR') {
    if (!goalId || isSelectingApproach) return
    setIsSelectingApproach(true)
    setActionError('')
    try {
      setWorkspace(await selectScheduleType(goalId, scheduleType))
    } catch (caught) {
      setActionError(caught instanceof Error ? caught.message : '暂时无法保存时间组织方式。')
    } finally {
      setIsSelectingApproach(false)
    }
  }

  async function handlePrepareRequirementReview() {
    if (!goalId || isPreparingReview) return
    setIsPreparingReview(true)
    setActionError('')
    try {
      const result = await prepareRequirementReview(goalId)
      setWorkspace(result)
      shouldFollowLatestRef.current = true
    } catch (caught) {
      setActionError(caught instanceof Error ? caught.message : '暂时无法整理目标，请继续聊一聊。')
    } finally {
      setIsPreparingReview(false)
    }
  }

  async function handleRetryAnalysis() {
    if (!goalId || isRetryingAnalysis) return
    setIsRetryingAnalysis(true)
    setActionError('')
    try {
      setWorkspace(await retryClarificationAnalysis(goalId))
    } catch (caught) {
      setActionError(caught instanceof Error ? caught.message : '暂时无法重新整理记忆。')
    } finally {
      setIsRetryingAnalysis(false)
    }
  }

  async function handleDiscardRequirementChanges() {
    if (!goalId || isDiscardingChanges) return
    setIsDiscardingChanges(true)
    setActionError('')
    try {
      const result = await discardRequirementChanges(goalId)
      setWorkspace(result)
      navigate(`/goals/${goalId}/route`)
    } catch (caught) {
      setActionError(caught instanceof Error ? caught.message : '暂时无法恢复原有需求。')
      setIsDiscardingChanges(false)
    }
  }

  async function handleFactAction(
    factId: string,
    action: 'CONFIRM' | 'REJECT' | 'REPLACE',
    value?: unknown,
  ) {
    if (!goalId || pendingFactId) return
    setPendingFactId(factId)
    setActionError('')
    try {
      const result = await updateRequirementFact(goalId, factId, action, value)
      setWorkspace(result)
      setEditingFactId(null)
      setEditValue('')
    } catch (caught) {
      setActionError(caught instanceof Error ? caught.message : '事实更新失败，请重试。')
    } finally {
      setPendingFactId(null)
    }
  }

  function beginFactClarification(fact: RequirementFact) {
    const label = factLabels[fact.field_key] ?? fact.field_key
    const currentValue = Array.isArray(fact.value) ? fact.value.join('、') : String(fact.value)
    const prompt = `关于“${label}”，你目前理解为：\n${currentValue}\n\n不准确的地方是：`
    setClarifyingFactId(fact.id)
    setIsContinuing(true)
    setReply(prompt)
    setActionError('')
    window.requestAnimationFrame(() => clarificationInputRef.current?.focus())
  }

  function cancelFactClarification() {
    setClarifyingFactId(null)
    setReply('')
  }

  if (loadError) {
    return <section className="page narrow error-state"><h1>目标暂时无法打开</h1><p>{loadError}</p><NavLink className="button" to="/">返回首页</NavLink></section>
  }

  if (!workspace) {
    return <section className="page narrow loading-state" aria-live="polite">正在恢复目标和对话…</section>
  }

  if (workspace.status === 'ROUTE_DRAFTING') {
    return <Navigate to={`/goals/${workspace.goal_id}/route`} replace />
  }

  const hasPreservedRoute = workspace.downstream_route_preserved
  const scheduleRecommendation = workspace.schedule_type_recommendation ?? {
    recommended_type: 'RHYTHM' as const,
    reasons: ['暂未取得模型建议，可以先按常见节奏推进'],
    confidence: 'LOW' as const,
  }
  const requirementsChanged = workspace.requirements_changed_since_route
  const showRequirementReview = workspace.status === 'REQUIREMENT_REVIEW' || hasPreservedRoute

  return (
    <section className={`page planner-stage-page clarification-page${contextOpen ? " context-open" : ""}`}>
      <div className="planner-primary-column">
        <header className="planner-stage-header">
          <div className="stage-heading">
          <p className="eyebrow">第 1 步 · {hasPreservedRoute ? '查看或修改需求' : '目标澄清'}</p>
          <h1>{hasPreservedRoute ? '回看已确认需求' : '先把目标想清楚'}</h1>
          </div>
          <button type="button" ref={triggerRef} className="context-toggle button" aria-expanded={contextOpen} aria-controls="clarification-context" onClick={() => setContextOpen(value => !value)}>目标笔记</button>
          <PlannerStepper
            current={['READY_TO_START', 'ACTIVE', 'RECOVERY_REQUIRED'].includes(workspace.status) ? 3 : ['PLAN_DRAFTING', 'PLAN_REVIEW'].includes(workspace.status) ? 2 : hasPreservedRoute ? 1 : 0}
            viewing={0}
            onStepSelect={(index) => {
              if (index === 1) navigate(`/goals/${workspace.goal_id}/route`)
              if (index === 2) navigate(`/goals/${workspace.goal_id}/plan`)
              if (index === 3) navigate(`/goals/${workspace.goal_id}/${workspace.status === 'READY_TO_START' ? 'prepare' : 'today'}`)
            }}
          />
        </header>
        <div className="conversation-column">
          <div className="conversation-scroll" ref={conversationScrollRef} onScroll={handleConversationScroll}>
            <div className="conversation" aria-live="polite">
              {workspace.messages.map((message, messageIndex) => (
                <article className={`message ${message.role.toLowerCase()}${message.generation_status === 'PENDING' ? ' processing-message' : ''}`} key={message.id}>
                  {message.role === 'ASSISTANT' && <span className="agent-identity"><SoleviaMark />{ASSISTANT_NAME}</span>}
                  {message.role === 'USER' && Array.isArray(message.metadata?.material_titles) && <div className="message-materials">{message.metadata.material_titles.filter((title): title is string => typeof title === 'string').map((title) => <span key={title}><FileText size={14} />{title}</span>)}</div>}
                  {message.generation_status === 'PENDING' && isModelConnectionMessage(actionError)
                    ? <ConnectionAwareError message={actionError} onRetry={() => { initialReplyStartedRef.current = null; setInitialReplyRetryKey(value => value + 1) }} />
                    : message.generation_status === 'PENDING'
                    ? <p><span className="typing-dots" aria-hidden="true"><i /><i /><i /></span>正在理解你的想法，准备从最有帮助的地方聊起…</p>
                    : message.role === 'ASSISTANT' ? <AssistantAnswer content={displayMessageContent(message.content, message.generation_status === 'FAILED')} animate={message.generation_status === 'SUCCEEDED' && freshAnswer(message.id)} onPresented={() => freshAnswer.markPresented(message.id, message.content)} /> : <p>{message.content}</p>}
                  <AnswerResearch message={message} summary={workspace.latest_research} research={research} />
                  {message.generation_status === 'FAILED' && messageIndex === workspace.messages.length - 1 && (
                    <div className="failed-message-actions">
                      <span>{message.metadata?.error_code === 'MODEL_TIMEOUT' ? '这次回复等待得有些久，你刚才的内容已经保存。' : message.metadata?.error_code === 'INVALID_MODEL_RESPONSE' ? '这次回复没有整理完整，你刚才的内容已经保存。' : '你刚才的内容已经保存，可以从这里继续。'}</span>
                      <div><button type="button" disabled={isSending} onClick={() => void retryFailedClarification()}>{isSending ? '正在重试…' : '直接重试'}</button><button type="button" disabled={isSending} onClick={() => editFailedClarification(messageIndex)}>修改后再试</button></div>
                    </div>
                  )}
                  {message.role === 'ASSISTANT' && message.generation_status === 'SUCCEEDED' && messageIndex === workspace.messages.length - 1 && (
                    <div className="message-actions"><button type="button" disabled={isSending} onClick={() => void regenerateClarification()}>{isSending ? '正在生成…' : '重新生成'}</button></div>
                  )}
                </article>
              ))}
              <ResearchProgress research={research} />
              {!isModelConnectionMessage(actionError) && <OperationProgress active={(localSending && !research.busy) || isStartingInitialReply || isPreparingReview || isConfirming || isRetryingAnalysis} scope={`${goalId}:${research.current?.id ?? ''}`}
                label={isPreparingReview ? '正在整理目标确认内容' : isConfirming ? '正在保存已确认的目标' : isRetryingAnalysis ? '正在更新讨论记录' : '正在结合对话生成回答'} error={actionError} />}
              {pendingInitialMessage && actionError && !isModelConnectionMessage(actionError) && (
                <div className="initial-reply-error" role="alert"><span>{actionError}</span><button type="button" disabled={isStartingInitialReply} onClick={() => { initialReplyStartedRef.current = null; setInitialReplyRetryKey((value) => value + 1) }}>{isStartingInitialReply ? '正在重试…' : '重新连接'}</button></div>
              )}
              {pendingReply && (
                <>
                  <article className="message user pending-message">
                    <p>{pendingReply}</p>
                    <span>已发送</span>
                  </article>

                </>
              )}
              {showRequirementReview && (
                <section className="requirement-review" aria-labelledby="requirement-review-title">
                  <div className="review-heading">
                    <div>
                      <p className="eyebrow">
                        {requirementsChanged ? '修改中的目标' : hasPreservedRoute ? '当前路线采用的目标' : '准备确认的目标'}
                      </p>
                      <h2 id="requirement-review-title">我目前对目标的理解</h2>
                    </div>
                    <span>{requirementsChanged ? '路线待同步' : hasPreservedRoute ? '仅查看，不影响路线' : '等待你的检查'}</span>
                  </div>
                  <p className="muted review-intro">
                    {hasPreservedRoute && !requirementsChanged
                      ? '你可以放心查看。只有实际保存了修改，Solevia 才会询问是否更新后续路线。'
                      : '逐项看一下即可。发现不准确的内容可以直接修改，不需要重新解释全部目标。'}
                  </p>
                  <div className="requirement-facts">
                    {workspace.facts.map((fact) => {
                      const label = factLabels[fact.field_key] ?? fact.field_key
                      const isEditing = editingFactId === fact.id
                      const isPending = pendingFactId === fact.id
                      return (
                        <article className="requirement-fact" key={fact.id}>
                          <div className="fact-heading">
                            <strong>{label}</strong>
                            <span className={fact.status === 'CONFIRMED' ? 'confirmed' : 'proposed'}>
                              {fact.status === 'CONFIRMED' ? '已确认' : '待你确认'}
                            </span>
                          </div>
                          {isEditing ? (
                            <div className="fact-editor">
                              <textarea
                                aria-label={`修改${label}`}
                                value={editValue}
                                onChange={(event) => setEditValue(event.target.value)}
                              />
                              <div className="text-actions">
                                <button type="button" onClick={() => { setEditingFactId(null); setEditValue('') }}>取消</button>
                                <button
                                  type="button"
                                  disabled={!editValue.trim() || isPending}
                                  onClick={() => handleFactAction(fact.id, 'REPLACE', editValue)}
                                >保存修改</button>
                              </div>
                            </div>
                          ) : (
                            <>
                              <p>{String(fact.value)}</p>
                              <div className="text-actions">
                                <button
                                  type="button"
                                  aria-label={`修改 ${label}`}
                                  onClick={() => { setEditingFactId(fact.id); setEditValue(String(fact.value)) }}
                                >修改</button>
                                {fact.status === 'PROPOSED' && (
                                  <>
                                    <button type="button" disabled={isPending} onClick={() => handleFactAction(fact.id, 'CONFIRM')}>确认正确</button>
                                    <button type="button" disabled={isPending} onClick={() => handleFactAction(fact.id, 'REJECT')}>不正确</button>
                                  </>
                                )}
                              </div>
                            </>
                          )}
                        </article>
                      )
                    })}
                  </div>
                  {!hasPreservedRoute && <section className="planning-approach-review" aria-labelledby="planning-approach-title">
                    <div className="planning-approach-heading">
                      <div><p className="eyebrow">路线策略</p><h3 id="planning-approach-title">你想先体验，还是系统推进？</h3></div>
                      <span>需要你确认</span>
                    </div>
                    <p className="planning-approach-advice"><strong>Solevia 建议：{workspace.planning_approach_recommendation.recommended_mode === 'EXPLORATORY' ? '先试试看' : '直接固定规划'}</strong>{workspace.planning_approach_recommendation.reasons.join('；')}。</p>
                    <div className="generation-mode-grid planning-mode-grid" aria-label="选择路线策略">
                      <button className={`generation-mode-card${workspace.selected_planning_mode === 'EXPLORATORY' ? ' selected' : ''}${workspace.planning_approach_recommendation.recommended_mode === 'EXPLORATORY' ? ' recommended' : ''}`} aria-pressed={workspace.selected_planning_mode === 'EXPLORATORY'} disabled={isSelectingApproach} type="button" onClick={() => void handleSelectPlanningApproach('EXPLORATORY')}>
                        <strong>先试试看</strong>
                        <span>先走通基础体验并尽早看到真实成效，再决定继续深入、调整还是停止。</span>
                        {workspace.planning_approach_recommendation.recommended_mode === 'EXPLORATORY' && <em>助手建议</em>}
                      </button>
                      <button className={`generation-mode-card${workspace.selected_planning_mode === 'COMMITTED' ? ' selected' : ''}${workspace.planning_approach_recommendation.recommended_mode === 'COMMITTED' ? ' recommended' : ''}`} aria-pressed={workspace.selected_planning_mode === 'COMMITTED'} disabled={isSelectingApproach} type="button" onClick={() => void handleSelectPlanningApproach('COMMITTED')}>
                        <strong>直接固定规划</strong>
                        <span>按完整能力链和前置依赖设计系统路线，适合已经确定要规范持续执行。</span>
                        {workspace.planning_approach_recommendation.recommended_mode === 'COMMITTED' && <em>助手建议</em>}
                      </button>
                    </div>
                    {workspace.selected_planning_mode && <p className="planning-approach-confirmed">已选择“{workspace.selected_planning_mode === 'EXPLORATORY' ? '先试试看' : '直接固定规划'}”，下一步路线会据此生成。</p>}
                  </section>}
                  {!hasPreservedRoute && <section className="planning-approach-review" aria-labelledby="schedule-type-title">
                    <div className="planning-approach-heading">
                      <div><p className="eyebrow">时间组织</p><h3 id="schedule-type-title">目标应该怎样和时间发生关系？</h3></div>
                      <span>需要你确认</span>
                    </div>
                    <p className="planning-approach-advice"><strong>Solevia 建议：{scheduleTypeLabels[scheduleRecommendation.recommended_type]}</strong>{scheduleRecommendation.reasons.join('；')}。</p>
                    <div className="generation-mode-grid schedule-type-grid" aria-label="选择时间组织方式">
                      {([
                        ['OUTCOME', '成果驱动', '在一个弹性窗口内完成目标，以可观察成果验收；具体哪天行动由你决定。'],
                        ['RHYTHM', '频率驱动', '用每周期的合理次数维持节奏，日期只是承载安排，不用截止倒计时衡量进度。'],
                        ['CALENDAR', '期限驱动', '围绕截止、考试、预约或交付日期倒排，并映射到明确日历。'],
                      ] as const).map(([type, label, description]) => <button className={`generation-mode-card${workspace.selected_schedule_type === type ? ' selected' : ''}${scheduleRecommendation.recommended_type === type ? ' recommended' : ''}`} aria-pressed={workspace.selected_schedule_type === type} disabled={isSelectingApproach} type="button" onClick={() => void handleSelectScheduleType(type)} key={type}>
                        <strong>{label}</strong><span>{description}</span>
                        {scheduleRecommendation.recommended_type === type && <em>助手建议</em>}
                      </button>)}
                    </div>
                    {workspace.selected_schedule_type && <p className="planning-approach-confirmed">已选择“{scheduleTypeLabels[workspace.selected_schedule_type]}”，路线与后续计划都会采用对应逻辑。</p>}
                  </section>}
                  {actionError && !(pendingInitialMessage && isModelConnectionMessage(actionError)) && <ConnectionAwareError message={actionError} />}
                  {requirementsChanged ? (
                    <div className="requirements-changed-notice" role="status">
                      <div>
                        <strong>需求已经更新，现有路线仍完整保留</strong>
                        <p>原来的路线仍然保留。你可以继续修改或先回去查看；确认采用新内容后，Solevia 才会更新路线。</p>
                      </div>
                      <div className="actions end">
                        <button className="button" type="button" onClick={() => navigate(`/goals/${workspace.goal_id}/route`)}>先查看现有路线</button>
                        <button className="button" type="button" disabled={isDiscardingChanges} onClick={handleDiscardRequirementChanges}>{isDiscardingChanges ? '正在恢复…' : '撤销需求修改，继续原路线'}</button>
                        <button
                          className="button primary"
                          type="button"
                          disabled={isConfirming || analysisPending || pendingFactId !== null || !workspace.selected_planning_mode || !workspace.selected_schedule_type}
                          onClick={handleConfirmRequirements}
                        >{isConfirming ? '正在确认…' : '确认新需求并更新路线 →'}</button>
                      </div>
                    </div>
                  ) : (
                    <div className="review-actions">
                      <button className="button" type="button" onClick={() => setIsContinuing(true)}>我还想补充</button>
                      {hasPreservedRoute ? (
                        <button className="button primary" type="button" onClick={() => navigate(`/goals/${workspace.goal_id}/route`)}>返回现有路线 →</button>
                      ) : (
                        <button
                          className="button primary"
                          type="button"
                          disabled={isConfirming || analysisPending || pendingFactId !== null || !workspace.selected_planning_mode || !workspace.selected_schedule_type}
                          onClick={handleConfirmRequirements}
                        >{isConfirming ? '正在确认…' : '确认需求，开始讨论路线 →'}</button>
                      )}
                    </div>
                  )}
                </section>
              )}
              <div ref={messageEndRef} />
            </div>
          </div>
          {showJumpToLatest && (
            <button className="jump-latest" type="button" onClick={jumpToLatest}>↓ 回到最新消息</button>
          )}
          {!pendingInitialMessage && (!showRequirementReview || isContinuing) && (
            <div className="composer-shell">
              <p className="composer-hint">说说你的想法、疑问或刚刚改变的主意</p>
              <form className="reply-form" onSubmit={handleReply}>
                {attachedMaterials.length > 0 && <div className="material-attachment-strip" aria-label="本次对话附件">{attachedMaterials.map((material) => <span key={material.source_id}><FileText size={15} /><span title={material.relative_path ?? material.title}>{material.title}</span>{material.latest_revision?.status !== 'READY' ? <small>解析中</small> : material.latest_revision.metadata.cache_reused ? <small>已复用</small> : null}<button type="button" disabled={isSending} aria-label={`移除附件 ${material.title}`} title="移除附件" onClick={() => setAttachedMaterials((current) => current.filter((item) => item.source_id !== material.source_id))}><X size={13} /></button></span>)}</div>}
                {attachedMaterialsPending && <p className="material-attachment-hint" role="status">资料正在解析，完成后才能用于回答；你可以先继续编辑消息。</p>}
                <div className="composer-input-well"><textarea
                  ref={clarificationInputRef}
                  id="clarification-reply" onKeyDown={submitOnEnter} title="Enter 发送，Shift + Enter 换行"
                  className="reply-input"
                  rows={1}
                  value={reply}
                  onChange={(event) => setReply(event.target.value)}
                  aria-label="给 Solevia 发送消息"
                  placeholder="输入消息"
                />
                <div className="discussion-composer-toolbar">
                  <MaterialWorkspacePanel goalId={workspace.goal_id} compact selectedSourceIds={attachedMaterials.map((material) => material.source_id)} onSelectionChange={setAttachedMaterials} disabled={isSending} />
                  <CodexComposerSettings disabled={isSending} />
                  {webConnection.available && <button
                    className={`research-toggle-button${webResearchEnabled ? ' active' : ''}`}
                    type="button"
                    aria-label={webResearchEnabled ? '关闭联网检索' : '开启联网检索'}
                    aria-pressed={webResearchEnabled}
                    title={webResearchEnabled ? '已允许模型判断并调用联网检索，点击关闭' : '允许模型判断是否需要联网检索'}
                    disabled={isSending}
                    onClick={() => setWebResearchEnabled((current) => !current)}
                  ><Globe2 size={16} aria-hidden="true" /></button>}
                <button className="button primary" type="submit" disabled={!reply.trim() || isSending || attachedMaterialsPending}>
                    {isSending ? '正在回应…' : '发送'}
                  </button></div></div>
                <ResearchConnectionStatus connection={webConnection} />
                {actionError && !(pendingInitialMessage && isModelConnectionMessage(actionError)) && <ConnectionAwareError message={actionError} />}
                
              </form>
            </div>
          )}
        </div>
      </div>
      {contextOpen && <button className="context-backdrop" type="button" aria-label="关闭目标笔记" onClick={() => setContextOpen(false)} />}
      <aside ref={drawerRef} role={contextOpen ? 'dialog' : undefined} aria-modal={contextOpen || undefined} id="clarification-context" className="stage-context-column memory-column" aria-label="目标已知信息">
          <button className="context-close button" type="button" onClick={() => setContextOpen(false)}>收起目标笔记 <X size={16} /></button>
          <ClarificationNotebook workspace={workspace} latestAnalysisJob={currentAnalysisJob}
            showRequirementReview={showRequirementReview} isPreparingReview={isPreparingReview} analysisPending={analysisPending}
            clarifyingFactId={clarifyingFactId} pendingFactId={pendingFactId} isRetryingAnalysis={isRetryingAnalysis}
            onPrepareReview={handlePrepareRequirementReview} onConfirmFact={id => void handleFactAction(id, 'CONFIRM')}
            onClarifyFact={beginFactClarification} onCancelClarification={cancelFactClarification} onRetryAnalysis={handleRetryAnalysis} />
      </aside>
    </section>
  )
}
