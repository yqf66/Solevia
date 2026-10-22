import { submitOnEnter } from '../ui/submitOnEnter'
import './ReviewPages.css'
import { RouteStageMap } from './RouteStageMap'
import { useEffect, useRef, useState } from 'react'
import type { FormEvent } from 'react'
import { Globe2 } from 'lucide-react'
import { Navigate, useNavigate, useParams } from 'react-router-dom'
import { confirmRoute, draftRoute, explainRoute, getRoute, reviseRoute, type RouteWorkspace } from '../api'
import { useResearchResponse } from '../ui/useResearchResponse'
import { ResearchProgress } from '../ui/ResearchProgress'
import { useWebResearch } from '../ui/useWebResearch'
import { ResearchConnectionStatus } from '../ui/ResearchConnectionStatus'
import { StageTransition, PlannerStepper } from '../ui/PlannerStage'
import { ASSISTANT_NAME, displayMessageContent } from '../ui/plannerPresentation'
import { RequirementSummary } from '../ui/RequirementSummary'
import { AnswerResearch } from '../ui/AnswerResearch'
import { AssistantAnswer, useFreshAnswers } from '../ui/AssistantAnswer'
import { OperationProgress } from '../ui/ProcessDetails'
import { MaterialWorkspacePanel } from '../ui/MaterialWorkspacePanel'

export function RouteReviewPage() {
  const { goalId } = useParams()
  const [workspace, setWorkspace] = useState<RouteWorkspace | null>(null)
  const [error, setError] = useState('')
  const [routeFeedbackDrafts, setRouteFeedbackDrafts] = useState({ REVISE: '', EXPLAIN: '' })
  const [isGenerating, setIsGenerating] = useState(false)
  const [localRevising, setIsRevising] = useState(false)
  const freshAnswer = useFreshAnswers(goalId, workspace?.goal_id === goalId ? workspace?.messages : undefined)
  const research = useResearchResponse(goalId, ['ROUTE'], getRoute, setWorkspace)
  const isRevising = localRevising || research.busy
  const [isConfirming, setIsConfirming] = useState(false)
  const [isDiscussionOpen, setIsDiscussionOpen] = useState(false)
  const [feedbackIntent, setFeedbackIntent] = useState<'REVISE' | 'EXPLAIN'>('REVISE')
  const [pendingRouteFeedback, setPendingRouteFeedback] = useState('')
  const [highlightLatestChange, setHighlightLatestChange] = useState(false)
  const webConnection = useWebResearch(goalId)
  const { enabled: webResearchEnabled, setEnabled: setWebResearchEnabled } = webConnection
  const generationStartedRef = useRef<string | null>(null)
  const feedbackInputRef = useRef<HTMLTextAreaElement>(null)
  const discussionEndRef = useRef<HTMLDivElement>(null)
  const routeResultRef = useRef<HTMLDivElement>(null)
  const changeSummaryRef = useRef<HTMLDivElement>(null)
  const shouldFocusChangeRef = useRef(false)
  const navigate = useNavigate()
  const routeProgress = { index: 0, message: feedbackIntent === 'EXPLAIN' ? '正在结合路线生成回答…' : '正在分析反馈并调整路线…' }

  const feedback = routeFeedbackDrafts[feedbackIntent]

  useEffect(() => {
    if (research.busy) setIsDiscussionOpen(true)
  }, [research.busy])

  useEffect(() => {
    setWorkspace(null)
    setError('')
    setRouteFeedbackDrafts({ REVISE: '', EXPLAIN: '' })
    setIsGenerating(false)
    setIsRevising(false)
    setIsConfirming(false)
    setIsDiscussionOpen(false)
    setPendingRouteFeedback('')
    setHighlightLatestChange(false)
    setWebResearchEnabled(false)
    generationStartedRef.current = null
  }, [goalId])

  function setRouteFeedbackDraft(intent: 'REVISE' | 'EXPLAIN', content: string) {
    setRouteFeedbackDrafts((current) => ({ ...current, [intent]: content }))
  }

  function prepareRouteFeedback(content: string, intent: 'REVISE' | 'EXPLAIN' = 'REVISE') {
    setRouteFeedbackDraft(intent, content)
    setFeedbackIntent(intent)
    setIsDiscussionOpen(true)
  }

  useEffect(() => {
    if (!isDiscussionOpen) return
    window.requestAnimationFrame(() => feedbackInputRef.current?.focus({ preventScroll: true }))
  }, [isDiscussionOpen])

  useEffect(() => {
    if (!isDiscussionOpen) return
    if (typeof discussionEndRef.current?.scrollIntoView === 'function') {
      discussionEndRef.current.scrollIntoView({ block: 'end' })
    }
  }, [isDiscussionOpen, workspace?.messages.length, pendingRouteFeedback, routeProgress.index])

  useEffect(() => {
    if (!shouldFocusChangeRef.current || !workspace?.route) return
    shouldFocusChangeRef.current = false
    setHighlightLatestChange(true)
    window.requestAnimationFrame(() => {
      const target = changeSummaryRef.current ?? routeResultRef.current
      if (typeof target?.scrollIntoView === 'function') {
        target.scrollIntoView({ behavior: 'smooth', block: 'start' })
      }
    })
    const timer = window.setTimeout(() => setHighlightLatestChange(false), 2200)
    return () => window.clearTimeout(timer)
  }, [workspace])

  useEffect(() => {
    if (!goalId) return
    let active = true
    getRoute(goalId)
      .then(async (result) => {
        if (!active) return
        setWorkspace(result)
        if (!result.route && result.status === 'ROUTE_DRAFTING' && generationStartedRef.current !== goalId) {
          generationStartedRef.current = goalId
          setIsGenerating(true)
          const generated = await draftRoute(goalId)
          if (active) setWorkspace(generated)
        }
      })
      .catch((caught) => {
        if (active) setError(caught instanceof Error ? caught.message : '暂时无法读取规划。')
      })
      .finally(() => { if (active) setIsGenerating(false) })
    return () => { active = false }
  }, [goalId])

  async function submitRouteFeedback(
    submittedFeedback: string,
    submittedIntent: 'REVISE' | 'EXPLAIN',
    regenerate = false,
  ) {
    if (!goalId || !submittedFeedback.trim() || isRevising) return
    const cleanFeedback = submittedFeedback.trim()
    setPendingRouteFeedback(cleanFeedback)
    setRouteFeedbackDraft(submittedIntent, '')
    setIsRevising(true)
    setError('')
    try {
      if (webResearchEnabled) {
        await research.start(cleanFeedback, 'ROUTE', submittedIntent)
        return
      }
      const revised = submittedIntent === 'EXPLAIN'
        ? await explainRoute(goalId, cleanFeedback, { regenerate })
        : await reviseRoute(goalId, cleanFeedback, { regenerate })
      const latestMessage = revised.messages.at(-1)
      if (latestMessage?.generation_status === 'FAILED') {
        setWorkspace(revised)
        setRouteFeedbackDraft(submittedIntent, cleanFeedback)
        return
      }
      if (submittedIntent === 'REVISE') shouldFocusChangeRef.current = true
      setWorkspace(revised)
      if (submittedIntent === 'REVISE') setIsDiscussionOpen(false)
    } catch (caught) {
      setRouteFeedbackDraft(submittedIntent, cleanFeedback)
      setError(caught instanceof Error ? caught.message : submittedIntent === 'EXPLAIN' ? '暂时无法解释路线。' : '暂时无法修改路线。')
    } finally {
      setPendingRouteFeedback('')
      setIsRevising(false)
    }
  }

  function handleRouteFeedback(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    void submitRouteFeedback(feedback, feedbackIntent)
  }

  function retryFailedMessage(messageIndex: number, editOnly = false) {
    if (!workspace || isRevising) return
    const failed = workspace.messages[messageIndex]
    const original = workspace.messages[messageIndex - 1]
    if (failed?.generation_status !== 'FAILED' || original?.role !== 'USER') return
    const intent = failed.metadata?.intent === 'EXPLAIN' ? 'EXPLAIN' : 'REVISE'
    setFeedbackIntent(intent)
    setRouteFeedbackDraft(intent, original.content)
    setIsDiscussionOpen(true)
    if (editOnly) {
      window.requestAnimationFrame(() => feedbackInputRef.current?.focus())
      return
    }
    void submitRouteFeedback(original.content, intent)
  }

  function regenerateMessage(messageIndex: number) {
    if (!workspace || isRevising) return
    const assistant = workspace.messages[messageIndex]
    const original = workspace.messages[messageIndex - 1]
    if (assistant?.role !== 'ASSISTANT' || assistant.generation_status !== 'SUCCEEDED' || original?.role !== 'USER') return
    const intent = assistant.metadata?.intent === 'EXPLAIN' ? 'EXPLAIN' : 'REVISE'
    setFeedbackIntent(intent)
    void submitRouteFeedback(original.content, intent, true)
  }

  async function handleConfirm() {
    if (!goalId || isConfirming) return
    setIsConfirming(true)
    setError('')
    try {
      await confirmRoute(goalId)
      navigate(`/goals/${goalId}/plan`)
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : '暂时无法确认路线。')
      setIsConfirming(false)
    }
  }

  if (error && !workspace) {
    return <section className="page narrow error-state"><h1>规划暂时无法打开</h1><p>{error}</p></section>
  }
  if (!workspace && !isGenerating) {
    return <StageTransition restoring current={1} title="正在读取已保存的路线"
      detail="正在同步已确认的路线和讨论记录。"
      checkpoints={['读取已保存的路线', '核对确认状态', '恢复讨论记录']} />
  }
  if (!workspace || isGenerating) {
    return (
      <StageTransition
        current={1}
        title="正在组织你的一级路线"
        detail="Solevia 正在把我们谈好的目标整理成清晰的阶段。这里暂时不会安排每天的任务。"
        checkpoints={['回顾目标和实际情况', '梳理先做什么、后做什么', '准备可供讨论的路线']}
      />
    )
  }
  if (isConfirming) {
    return (
      <StageTransition
        current={2}
        title="路线已确认，正在进入时间规划"
        detail="接下来会结合容量、期限和缓冲安排阶段时间，但不会把建议时间变成硬性倒计时。"
        checkpoints={['回顾已经确认的路线', '结合你的时间和期限', '准备安排每周节奏']}
      />
    )
  }
  if (!['ROUTE_DRAFTING', 'ROUTE_REVIEW', 'REQUIREMENT_REVIEW', 'PLAN_DRAFTING', 'PLAN_REVIEW', 'READY_TO_START', 'ACTIVE', 'RECOVERY_REQUIRED'].includes(workspace.status)) {
    return <Navigate to={`/goals/${workspace.goal_id}/clarify`} replace />
  }

  if (!workspace.route) {
    const failedMessage = workspace.messages.at(-1)?.generation_status === 'FAILED'
      ? workspace.messages.at(-1)?.content
      : null
    return (
      <section className="page narrow error-state">
        <PlannerStepper current={1} />
        <h1>路线还没有生成</h1>
        <p>{failedMessage ?? '需求已经安全保存，可以重新尝试生成。'}</p>
        <button className="button primary" type="button" onClick={() => window.location.reload()}>重新生成路线</button>
      </section>
    )
  }

  const route = workspace.route
  const planIsConfirmed = ['READY_TO_START', 'ACTIVE', 'RECOVERY_REQUIRED'].includes(workspace.status)
  const hasDownstreamPlan = ['PLAN_DRAFTING', 'PLAN_REVIEW'].includes(workspace.status) || planIsConfirmed
  return (
    <section className="page planner-stage-page route-review-page">
      <div className="planner-primary-column">
        <header className="planner-stage-header">
          <div className="stage-heading">
            <p className="eyebrow">第 2 步 · 路线规划</p>
            <h1>讨论一级路线</h1>
          </div>
          <PlannerStepper current={planIsConfirmed ? 3 : hasDownstreamPlan ? 2 : 1} viewing={1} onStepSelect={(index) => {
            if (index === 0) navigate(`/goals/${workspace.goal_id}/clarify`)
            if (index === 2) navigate(`/goals/${workspace.goal_id}/plan`)
            if (index === 3) navigate(`/goals/${workspace.goal_id}/${workspace.status === 'READY_TO_START' ? 'prepare' : 'today'}`)
          }} />
        </header>
        <div className="stage-main-scroll route-scroll">
          <div className="route-main">
          {hasDownstreamPlan && (
            <div className="requirements-stale-notice" role="status">
              <div><strong>正在回看时间计划采用的路线</strong><p>这里展示之前确认的路线，不会改变现在的时间安排。</p></div>
              <button className="button" type="button" onClick={() => navigate(`/goals/${workspace.goal_id}/plan`)}>返回时间计划</button>
            </div>
          )}
          {workspace.requirements_changed_since_route && (
            <div className="requirements-stale-notice" role="status">
              <div>
                <strong>这条路线仍保留，但需求已有未确认的修改</strong>
                <p>这里仍展示原来的路线；你尚未确认的修改还没有影响它。</p>
              </div>
              <button className="button" type="button" onClick={() => navigate(`/goals/${workspace.goal_id}/clarify`)}>查看并处理需求修改</button>
            </div>
          )}
          <div className="route-title-row" ref={routeResultRef}>
            <div><h2>第一版整体路线</h2><p className="muted">{hasDownstreamPlan ? '这是当前时间计划采用的已确认路线。' : '先确认阶段和顺序，此时还没有每日安排。'}</p></div>
            <span>{route.revision > 1 ? `第 ${route.revision} 次调整` : '初始路线'}</span>
          </div>

          {workspace.messages[0] && (
            <div className="route-agent-note"><AssistantAnswer content={workspace.messages[0].content} /></div>
          )}
          <p className="route-summary">{route.summary}</p>

          {route.change_summary.length > 0 && (
            <div className={`route-changes${highlightLatestChange ? ' highlight' : ''}`} ref={changeSummaryRef} tabIndex={-1}>
              <strong>这次修改</strong>
              <ul>{route.change_summary.map((item) => <li key={item}>{item}</li>)}</ul>
            </div>
          )}

          <RouteStageMap stages={route.stages} />
          <div className="route-stage-list">
            {route.stages.map((stage) => (
              <article className="panel route-stage-card" id={`route-stage-${stage.id}`} tabIndex={-1} key={stage.id}>
                <span className="route-number">{stage.order}</span>
                <div>
                  <h2>{stage.title}</h2>
                  <p>{stage.purpose}</p>
                  <h3>主要内容</h3>
                  <div className="chips">{stage.topics.map((topic) => <span key={topic}>{topic}</span>)}</div>
                  <h3>阶段产出</h3>
                  <ul>{stage.outputs.map((output) => <li key={output}>{output}</li>)}</ul>
                  {stage.options.length > 0 && (
                    <div className="route-options">
                      <h3>可以这样选</h3>
                      <div>
                        {stage.options.map((option) => (
                          <button
                            className={option.recommended ? 'recommended' : ''}
                            type="button"
                            key={option.label}
                            disabled={workspace.requirements_changed_since_route || hasDownstreamPlan}
                            onClick={() => prepareRouteFeedback(`关于阶段 ${stage.order}「${stage.title}」：我倾向选择「${option.label}」。请按这个方向调整路线，其他部分尽量保持不变。`)}
                          >
                            <span>{option.label}{option.recommended && <em>建议默认</em>}</span>
                            <p>{option.description}</p>
                            <small>{option.tradeoff}</small>
                          </button>
                        ))}
                      </div>
                    </div>
                  )}
                  {stage.depends_on.length > 0 && <small>依赖阶段：{stage.depends_on.join('、')}</small>}
                  <div className="text-actions">
                    <button type="button" disabled={workspace.requirements_changed_since_route || hasDownstreamPlan} onClick={() => prepareRouteFeedback(`关于阶段 ${stage.order}「${stage.title}」：我已经具备这部分基础，请压缩。`)}>我已经会了</button>
                    <button type="button" disabled={workspace.requirements_changed_since_route || hasDownstreamPlan} onClick={() => prepareRouteFeedback(`关于阶段 ${stage.order}「${stage.title}」：当前内容太多或太难，请缩小范围。`)}>太多 / 太难</button>
                    <button type="button" disabled={workspace.requirements_changed_since_route || hasDownstreamPlan} onClick={() => prepareRouteFeedback(`请解释阶段 ${stage.order}「${stage.title}」为什么这样安排。`, 'EXPLAIN')}>需要解释</button>
                  </div>
                  <details className="route-more-actions">
                    <summary>更多调整</summary>
                    <div className="route-structure-actions">
                      <button
                        type="button"
                        disabled={workspace.requirements_changed_since_route || hasDownstreamPlan || stage.order === 1}
                        onClick={() => prepareRouteFeedback(`请将阶段 ${stage.order}「${stage.title}」适当前移，并重新检查所有阶段的先后依赖；其他阶段内容尽量保持不变。`)}
                      >提前这一阶段</button>
                      <button
                        type="button"
                        disabled={workspace.requirements_changed_since_route || hasDownstreamPlan || stage.order === route.stages.length}
                        onClick={() => prepareRouteFeedback(`请将阶段 ${stage.order}「${stage.title}」适当延后，并重新检查所有阶段的先后依赖；其他阶段内容尽量保持不变。`)}
                      >延后这一阶段</button>
                      <button
                        type="button"
                        disabled={workspace.requirements_changed_since_route || hasDownstreamPlan}
                        onClick={() => prepareRouteFeedback(`请删除阶段 ${stage.order}「${stage.title}」的独立安排；如果直接删除会破坏路线完整性，请只把必要内容合并进相邻阶段，并说明取舍。`)}
                      >删除这一阶段</button>
                      <button
                        type="button"
                        disabled={workspace.requirements_changed_since_route || hasDownstreamPlan}
                        onClick={() => prepareRouteFeedback(`我不想按阶段 ${stage.order}「${stage.title}」的当前方案推进。请提供并采用一个更适合已确认需求的替代阶段，其他阶段尽量保持不变。`)}
                      >替换这一阶段</button>
                    </div>
                    <p>点击对应操作，会在下方生成一段大致的调整需求并自动带你前往。你可以直接确认，也可以在此基础上继续补充和细化想法。</p>
                  </details>
                </div>
              </article>
            ))}
          </div>

          {error && <p className="form-error route-action-error" role="alert">{error}</p>}

          {(route.tradeoffs.length > 0 || route.assumptions.length > 0) && (
            <div className="route-notes">
              {route.tradeoffs.length > 0 && <div><h2>这版路线的取舍</h2><ul>{route.tradeoffs.map((item) => <li key={item}>{item}</li>)}</ul></div>}
              {route.assumptions.length > 0 && <div><h2>当前假设</h2><ul>{route.assumptions.map((item) => <li key={item}>{item}</li>)}</ul></div>}
            </div>
          )}

          {workspace.requirements_changed_since_route && (
            <div className="route-feedback route-readonly-note">
              <strong>现有路线暂时只读</strong>
              <p>先确认是否采用新的需求，再继续调整或确认路线，可以避免两套内容互相冲突。</p>
              <div className="actions end"><button className="button primary" type="button" onClick={() => navigate(`/goals/${workspace.goal_id}/clarify`)}>返回处理需求 →</button></div>
            </div>
          )}
          </div>
        </div>
        {!workspace.requirements_changed_since_route && !hasDownstreamPlan && (
          <section className={`route-discussion-dock${isDiscussionOpen ? ' open' : ''}`} aria-label="路线讨论">
            <header className="route-discussion-bar">
              <button
                className="discussion-toggle"
                type="button"
                aria-expanded={isDiscussionOpen}
                onClick={() => setIsDiscussionOpen((current) => !current)}
              >
                <span>路线讨论</span>
                <small>{isRevising ? routeProgress.message : workspace.messages.length > 1 ? '查看最近反馈与回答' : '解释或调整这条路线'}</small>
              </button>
              <div className="discussion-bar-actions">
                <button className="button" type="button" onClick={() => {
                  setIsDiscussionOpen(true)
                  window.requestAnimationFrame(() => feedbackInputRef.current?.focus())
                }}>写反馈</button>
                <button className="button primary" type="button" disabled={isConfirming || isRevising} onClick={handleConfirm}>{isConfirming ? '正在确认…' : route.open_questions.length > 0 ? '按当前假设继续 →' : '确认路线并安排时间 →'}</button>
              </div>
            </header>
            {isDiscussionOpen && (
              <div className="route-discussion-panel">
                <div className="route-discussion-history" aria-live="polite">
                  {workspace.messages.slice(-6).map((message, visibleIndex) => {
                    const messageIndex = Math.max(0, workspace.messages.length - 6) + visibleIndex
                    const failed = message.generation_status === 'FAILED'
                    return (
                    <article className={`discussion-message ${message.role.toLowerCase()}${failed ? ' failed' : ''}`} key={message.id}>
                      <strong>{message.role === 'USER' ? '你' : ASSISTANT_NAME}</strong>
                      {failed && <b>这次没有完成，但当前路线没有变化</b>}
                      {message.role === 'ASSISTANT' ? <AssistantAnswer content={displayMessageContent(message.content, failed)} animate={!failed && freshAnswer(message.id)} /> : <p>{message.content}</p>}
                      <AnswerResearch message={message} summary={workspace.latest_research} research={research} />
                      {failed && (
                        <div className="failure-actions">
                          <button type="button" disabled={isRevising} onClick={() => retryFailedMessage(messageIndex)}>重新尝试</button>
                          <button type="button" disabled={isRevising} onClick={() => retryFailedMessage(messageIndex, true)}>修改后再试</button>
                        </div>
                      )}
                      {!failed && message.role === 'ASSISTANT' && message.generation_status === 'SUCCEEDED' && messageIndex === workspace.messages.length - 1 && (
                        <div className="message-actions"><button type="button" disabled={isRevising} onClick={() => regenerateMessage(messageIndex)}>{isRevising ? '正在生成…' : '重新生成'}</button></div>
                      )}
                    </article>
                  )})}
                  {pendingRouteFeedback && (
                    <>
                      <article className="discussion-message user pending"><strong>你</strong><p>{pendingRouteFeedback}</p><small>已发送</small></article>

                    </>
                  )}
                  <ResearchProgress research={research} />
                  <OperationProgress active={localRevising && !research.busy} scope={`${goalId}:${research.current?.id ?? ''}`} label={feedbackIntent === 'REVISE' ? '正在分析反馈并调整路线' : '正在结合路线生成回答'} error={error} />
                  <div ref={discussionEndRef} />
                </div>
                <form className="route-discussion-composer" onSubmit={handleRouteFeedback}>
                  <div className="feedback-intent" aria-label="反馈用途">
                    <span>这次希望：</span>
                    <button className={feedbackIntent === 'REVISE' ? 'selected' : ''} type="button" onClick={() => setFeedbackIntent('REVISE')}>修改路线</button>
                    <button className={feedbackIntent === 'EXPLAIN' ? 'selected' : ''} type="button" onClick={() => setFeedbackIntent('EXPLAIN')}>只做解释</button>
                  </div>
                  {feedbackIntent === 'EXPLAIN' && (
                    <div className="plan-feedback-shortcuts route-explanation-shortcuts" aria-label="选择要解释的路线阶段">
                      <button type="button" onClick={() => prepareRouteFeedback('请用大白话解释这条一级路线为什么这样安排，重点说明阶段顺序和主要取舍。', 'EXPLAIN')}>解释整体路线</button>
                      {route.stages.map((stage) => (
                        <button type="button" key={stage.id} onClick={() => prepareRouteFeedback(`请解释阶段 ${stage.order}「${stage.title}」为什么这样安排，它与前后阶段有什么关系。`, 'EXPLAIN')}>M{stage.order} · {stage.title}</button>
                      ))}
                    </div>
                  )}
                  <label htmlFor="route-feedback">{feedbackIntent === 'EXPLAIN' ? '想了解什么？' : '哪里需要调整？'}</label>
                  <div className="composer-input-well"><textarea ref={feedbackInputRef} id="route-feedback" onKeyDown={submitOnEnter} title="Enter 发送，Shift + Enter 换行" value={feedback} onChange={(event) => setRouteFeedbackDraft(feedbackIntent, event.target.value)} placeholder={feedbackIntent === 'EXPLAIN' ? '例如：为什么必须先完成第二阶段？' : '例如：第二阶段我已经会了；整体太难；希望先做一个小成果……'} />
                  <div className="discussion-composer-toolbar">
                    <MaterialWorkspacePanel goalId={workspace.goal_id} compact />
                    <button
                      className={`research-toggle-button${webResearchEnabled ? ' active' : ''}`}
                      type="button"
                      aria-label={webResearchEnabled ? '关闭联网检索' : '开启联网检索'}
                      aria-pressed={webResearchEnabled}
                      title={webResearchEnabled ? '已允许模型判断并调用联网检索，点击关闭' : '允许模型判断是否需要联网检索'}
                      disabled={isRevising}
                      onClick={() => setWebResearchEnabled((current) => !current)}
                    ><Globe2 size={16} aria-hidden="true" /></button>
                  <button className="button primary" type="submit" disabled={!feedback.trim() || isRevising}>{isRevising ? routeProgress.message : feedbackIntent === 'EXPLAIN' ? '发送问题' : '提交调整需求'}</button></div></div>
                  <ResearchConnectionStatus connection={webConnection} />
                  {error && <p className="form-error" role="alert">{error}</p>}
                  <div className="review-actions">
                    <span>{feedbackIntent === 'EXPLAIN' ? '只回答你的问题，路线不会变化。' : 'Solevia 会先给出建议，你确认后路线才会变化。'}</span>

                  </div>
                </form>
              </div>
            )}
          </section>
        )}
      </div>
      <aside className="stage-context-column memory-column route-context-column" aria-label="路线依据">
        <div className="panel route-context">
          <div className="confirmed-summary-heading"><h2>已经谈好的目标</h2><span>已确认</span></div>
          <RequirementSummary snapshot={workspace.requirement_snapshot} />
          {(route.open_questions.length > 0 || (route.optional_questions ?? []).length > 0) && <div className="route-open-questions"><h2>可以稍后补充</h2><p className="muted">不回答也可以按当前假设继续，补充后路线会更贴身。</p><ul>{[...route.open_questions, ...(route.optional_questions ?? [])].map((question) => <li key={question}>{question}</li>)}</ul></div>}
        </div>
      </aside>
    </section>
  )
}
