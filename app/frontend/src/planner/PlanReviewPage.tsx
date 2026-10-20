import { TimePicker } from '../ui/TimePicker'
import { submitOnEnter } from '../ui/submitOnEnter'
import './ReviewPages.css'
import './PlanIdentity.css'
import { useEffect, useRef, useState } from 'react'
import type { FormEvent } from 'react'
import { Globe2 } from 'lucide-react'
import { Navigate, useLocation, useNavigate, useParams } from 'react-router-dom'
import { applyPlanAdjustment, applyExplorationWindow, confirmPlan, deferRecovery, discardPlanAdjustment, discardExplorationWindow, draftExplorationWindow, explainPlan, getPlan, previewPlanAdjustment, revisePlan, restorePlanVersion, startPlanGeneration, type PlanAdjustmentPreview, type PlanWorkspace } from '../api'
import { useResearchResponse } from '../ui/useResearchResponse'
import { ResearchProgress } from '../ui/ResearchProgress'
import { useWebResearch } from '../ui/useWebResearch'
import { ResearchConnectionStatus } from '../ui/ResearchConnectionStatus'
import { StageTransition, PlannerStepper } from '../ui/PlannerStage'
import { scheduleTypeLabels, ASSISTANT_NAME, displayMessageContent, factLabels } from '../ui/plannerPresentation'
import { AnswerResearch } from '../ui/AnswerResearch'
import { AssistantAnswer, useFreshAnswers } from '../ui/AssistantAnswer'
import { OperationProgress, ProcessDetails } from '../ui/ProcessDetails'
import { HybridPlanReview } from './plan-review/HybridPlanReview'
import { PlanDayCard } from './plan-review/PlanDayCard'
import { MaterialWorkspacePanel } from '../ui/MaterialWorkspacePanel'
import { CapacityNegotiationPanel, CapacitySummary } from './plan-review/CapacityNegotiationPanel'
import { decideRecovery } from '../api'
import { RecoveryDecisions } from '../ui/RecoveryDecisions'

export function PlanReviewPage() {
  const { goalId } = useParams()
  const location = useLocation()
  const planQuery = new URLSearchParams(location.search)
  const recoveryLevel = planQuery.get('recovery')
  const shouldShowIncomingAdjustment = planQuery.get('adjustment') === '1'
  const recoveryDraft = recoveryLevel === 'PAUSED_7_DAYS'
    ? '我已经暂停了一周左右。请先根据我现在真正能投入的时间，帮我重新确认接下来的节奏；保留已经完成的内容，在我确认前不要修改计划。'
    : recoveryLevel === 'PAUSED_3_DAYS'
      ? '我已经有几天没有推进了。请先帮我判断怎样平稳恢复，只调整尚未开始的近期安排，不要改动已完成内容，也不要立刻重做整体计划。'
      : ''
  const [workspace, setWorkspace] = useState<PlanWorkspace | null>(null)
  const [error, setError] = useState('')
  const [isGenerating, setIsGenerating] = useState(false)
  const [generationPollRun, setGenerationPollRun] = useState(0)
  const [selectedGenerationMode, setSelectedGenerationMode] = useState<'STANDARD' | 'DEEP'>(
    'STANDARD',
  )
  const [isExplorationWindowBusy, setIsExplorationWindowBusy] = useState(false)
  const [viewMode, setViewMode] = useState<'STAGE' | 'WEEK' | 'DAY'>('STAGE')
  const [planFeedbackDrafts, setPlanFeedbackDrafts] = useState({ REVISE: recoveryDraft, EXPLAIN: '' })
  const [planFeedbackIntent, setPlanFeedbackIntent] = useState<'REVISE' | 'EXPLAIN'>('REVISE')
  const webConnection = useWebResearch(goalId)
  const { enabled: webResearchEnabled, setEnabled: setWebResearchEnabled } = webConnection
  const [isPlanDiscussionOpen, setIsPlanDiscussionOpen] = useState(
    planQuery.get('discussion') === '1' || shouldShowIncomingAdjustment,
  )
  const [localResponding, setIsPlanResponding] = useState(false)
  const freshAnswer = useFreshAnswers(goalId, workspace?.goal_id === goalId ? workspace?.messages : undefined)
  const research = useResearchResponse(goalId, ['PLAN', 'RECOVERY'], getPlan, setWorkspace)
  const isPlanResponding = localResponding || research.busy
  const [isConfirmingPlan, setIsConfirmingPlan] = useState(false)
  const [pendingPlanIntent, setPendingPlanIntent] = useState<'REVISE' | 'EXPLAIN' | 'APPLY' | 'RESTORE' | null>(null)
  const [pendingPlanFeedback, setPendingPlanFeedback] = useState('')
  const [planDiscussionError, setPlanDiscussionError] = useState('')
  const [planAdjustmentPreview, setPlanAdjustmentPreview] = useState<PlanAdjustmentPreview | null>(null)
  const [editedAdjustmentSchedule, setEditedAdjustmentSchedule] = useState<PlanAdjustmentPreview['schedule']>([])
  const [acceptAdjustmentUnallocated, setAcceptAdjustmentUnallocated] = useState(false)
  const [showRecoveryGate, setShowRecoveryGate] = useState(false)
  const planFeedbackInputRef = useRef<HTMLTextAreaElement>(null)
  const planDiscussionEndRef = useRef<HTMLDivElement>(null)
  const planChangeSummaryRef = useRef<HTMLDivElement>(null)
  const pendingAdjustmentNoticeRef = useRef<HTMLElement>(null)
  const shouldFocusPlanChangeRef = useRef(false)
  const focusedIncomingAdjustmentRef = useRef(false)
  const activeGoalIdRef = useRef<string | undefined>(goalId)
  const navigate = useNavigate()
  const planFeedback = planFeedbackDrafts[planFeedbackIntent]

  useEffect(() => {
    if (!goalId || !isExplorationWindowBusy) return
    let active = true
    const timer = setInterval(() => {
      void getPlan(goalId).then(value => {
        if (active) setWorkspace(value)
      }).catch(() => { /* The original request handles terminal errors. */ })
    }, 2000)
    return () => { active = false; clearInterval(timer) }
  }, [goalId, isExplorationWindowBusy])

  useEffect(() => {
    if (!goalId || !workspace?.capacity_negotiation || (!isPlanResponding && workspace.capacity_negotiation.preview_progress?.status !== 'RUNNING')) return
    let active = true
    const timer = setInterval(() => { void getPlan(goalId).then(value => {
      if (active) setWorkspace(value)
    }).catch(() => { /* Keep the active request and its visible error handling. */ }) }, 3000)
    return () => { active = false; clearInterval(timer) }
  }, [goalId, isPlanResponding, Boolean(workspace?.capacity_negotiation), workspace?.capacity_negotiation?.preview_progress?.status])

  useEffect(() => {
    if (research.busy) setIsPlanDiscussionOpen(true)
  }, [research.busy])

  useEffect(() => {
    activeGoalIdRef.current = goalId
    setWorkspace(null)
    setError('')
    setIsGenerating(false)
    setIsExplorationWindowBusy(false)
    setWebResearchEnabled(false)
    setPendingPlanFeedback('')
    setPendingPlanIntent(null)
    setPlanDiscussionError('')
    setIsPlanResponding(false)
    setPlanAdjustmentPreview(null)
    setEditedAdjustmentSchedule([])
    setAcceptAdjustmentUnallocated(false)
    setShowRecoveryGate(false)
    setIsPlanDiscussionOpen(planQuery.get('discussion') === '1' || shouldShowIncomingAdjustment)
    return () => { activeGoalIdRef.current = undefined }
  }, [goalId])

  useEffect(() => {
    const persistedPreview = workspace?.pending_adjustment_preview
    if (!persistedPreview) {
      setPlanAdjustmentPreview(null)
      setEditedAdjustmentSchedule([])
      setAcceptAdjustmentUnallocated(false)
      return
    }
    setPlanAdjustmentPreview(persistedPreview)
    if (persistedPreview.id !== planAdjustmentPreview?.id) {
      setEditedAdjustmentSchedule(persistedPreview.schedule)
      setAcceptAdjustmentUnallocated(false)
    }
  }, [workspace])
  const planDiscussionProgress = { index: 0, message: pendingPlanIntent === 'APPLY' ? '正在生成调整预览并校验日期和时间…' : pendingPlanIntent === 'RESTORE' ? '正在恢复之前的安排…' : pendingPlanIntent === 'REVISE' ? '正在分析反馈并生成调整建议…' : '正在结合计划生成回答…' }

  function setPlanFeedbackDraft(intent: 'REVISE' | 'EXPLAIN', content: string) {
    setPlanFeedbackDrafts((current) => ({ ...current, [intent]: content }))
  }

  function preparePlanFeedback(content: string, intent: 'REVISE' | 'EXPLAIN' = 'REVISE') {
    setPlanFeedbackDraft(intent, content)
    setPlanFeedbackIntent(intent)
    setIsPlanDiscussionOpen(true)
    window.requestAnimationFrame(() => planFeedbackInputRef.current?.focus())
  }

  useEffect(() => {
    if (!isPlanDiscussionOpen) return
    if (typeof planDiscussionEndRef.current?.scrollIntoView === 'function') {
      planDiscussionEndRef.current.scrollIntoView({ behavior: 'smooth', block: 'end' })
    }
  }, [
    isPlanDiscussionOpen,
    workspace?.messages?.length,
    pendingPlanFeedback,
    planDiscussionProgress.index,
  ])

  useEffect(() => {
    if (
      !shouldShowIncomingAdjustment
      || !workspace?.pending_adjustment
      || focusedIncomingAdjustmentRef.current
    ) return
    focusedIncomingAdjustmentRef.current = true
    setIsPlanDiscussionOpen(true)
    window.requestAnimationFrame(() => {
      pendingAdjustmentNoticeRef.current?.scrollIntoView?.({ behavior: 'smooth', block: 'start' })
    })
  }, [shouldShowIncomingAdjustment, workspace?.pending_adjustment])

  async function submitPlanDiscussion(
    content: string,
    intent: 'REVISE' | 'EXPLAIN',
    regenerate = false,
  ) {
    if (!goalId || !content.trim() || isPlanResponding || workspace?.status === 'COMPLETED') return
    const cleanContent = content.trim()
    setPendingPlanFeedback(cleanContent)
    setPendingPlanIntent(intent)
    setPlanFeedbackDraft(intent, '')
    setPlanDiscussionError('')
    setIsPlanResponding(true)
    try {
      if (webResearchEnabled) {
        const recovery = Boolean(recoveryLevel || workspace?.messages.some((message) => message.metadata?.recovery_brief_id))
        await research.start(cleanContent, recovery ? 'RECOVERY' : 'PLAN', intent)
        return
      }
      const result = intent === 'EXPLAIN'
        ? await explainPlan(goalId, cleanContent, false, undefined, regenerate)
        : await revisePlan(goalId, cleanContent, false, undefined, regenerate)
      if (activeGoalIdRef.current !== goalId) return
      const latestMessage = result.messages?.at(-1)
      setWorkspace(result)
      if (intent === 'REVISE') {
        setPlanAdjustmentPreview(null)
        setEditedAdjustmentSchedule([])
        setAcceptAdjustmentUnallocated(false)
      }
      if (latestMessage?.generation_status === 'FAILED') {
        setPlanFeedbackDraft(intent, cleanContent)
      }
    } catch (caught) {
      setPlanFeedbackDraft(intent, cleanContent)
      setPlanDiscussionError(
        caught instanceof Error
          ? caught.message
          : intent === 'EXPLAIN'
            ? '暂时无法解释时间计划，请稍后重试。'
            : '暂时无法分析这次调整，当前计划没有变化。',
      )
    } finally {
      setPendingPlanFeedback('')
      setPendingPlanIntent(null)
      setIsPlanResponding(false)
    }
  }

  function handlePlanFeedback(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    void submitPlanDiscussion(planFeedback, planFeedbackIntent)
  }

  function retryPlanDiscussion(messageIndex: number, editOnly = false) {
    if (!workspace || isPlanResponding) return
    const failed = workspace.messages[messageIndex]
    const original = workspace.messages[messageIndex - 1]
    if (
      failed?.generation_status !== 'FAILED'
      || original?.role !== 'USER'
    ) return
    const intent = failed.metadata?.intent === 'REVISE' ? 'REVISE' : 'EXPLAIN'
    setPlanFeedbackIntent(intent)
    setPlanFeedbackDraft(intent, original.content)
    setIsPlanDiscussionOpen(true)
    if (editOnly) {
      window.requestAnimationFrame(() => planFeedbackInputRef.current?.focus())
      return
    }
    void submitPlanDiscussion(original.content, intent)
  }

  function regeneratePlanDiscussion(messageIndex: number) {
    if (!workspace || isPlanResponding) return
    const assistant = workspace.messages[messageIndex]
    const original = workspace.messages[messageIndex - 1]
    if (assistant?.role !== 'ASSISTANT' || assistant.generation_status !== 'SUCCEEDED' || original?.role !== 'USER') return
    const intent = assistant.metadata?.intent === 'REVISE' ? 'REVISE' : 'EXPLAIN'
    setPlanFeedbackIntent(intent)
    void submitPlanDiscussion(original.content, intent, true)
  }

  async function discardPendingAdjustment() {
    if (!goalId || isPlanResponding) return
    setIsPlanResponding(true)
    setPlanDiscussionError('')
    try {
      setWorkspace(await discardPlanAdjustment(goalId))
      setPlanAdjustmentPreview(null)
      setEditedAdjustmentSchedule([])
      setAcceptAdjustmentUnallocated(false)
    } catch (caught) {
      setPlanDiscussionError(
        caught instanceof Error ? caught.message : '暂时无法放弃这份建议。',
      )
    } finally {
      setIsPlanResponding(false)
    }
  }

  async function confirmRecoveryDecision(decision: 'ALLOW_EXTENSION' | 'KEEP_DEADLINE' | 'ACCEPT_LOAD') {
    if (!goalId || !workspace?.recovery_review || isPlanResponding) return
    setIsPlanResponding(true)
    setPendingPlanIntent('REVISE')
    setPlanDiscussionError('')
    try {
      setWorkspace(await decideRecovery(goalId, decision, workspace.recovery_review.token))
      setPlanAdjustmentPreview(null)
      setEditedAdjustmentSchedule([])
      setAcceptAdjustmentUnallocated(false)
      setPlanFeedbackDraft('REVISE', '')
    } catch (caught) {
      setPlanDiscussionError(caught instanceof Error ? caught.message : '暂时无法保存选择。')
      try { setWorkspace(await getPlan(goalId)) } catch { /* retain saved view */ }
    } finally {
      setPendingPlanIntent(null)
      setIsPlanResponding(false)
    }
  }

  async function applyPendingAdjustment() {
    if (!goalId || isPlanResponding) return
    if (!planAdjustmentPreview) {
      setIsPlanResponding(true)
      setPendingPlanIntent('APPLY')
      setPlanDiscussionError('')
      try {
        const preview = await previewPlanAdjustment(goalId)
        setPlanAdjustmentPreview(preview)
        setEditedAdjustmentSchedule(preview.schedule)
        setAcceptAdjustmentUnallocated(false)
      } catch (caught) {
        setPlanDiscussionError(caught instanceof Error ? caught.message : '暂时无法生成调整预览。')
      } finally {
        setPendingPlanIntent(null)
        setIsPlanResponding(false)
      }
      return
    }
    if (!workspace) return
    if (workspace.recovery_guard?.required && !workspace.recovery_review?.is_recovery) {
      setShowRecoveryGate(true)
      return
    }
    setPendingPlanIntent('APPLY')
    setIsPlanResponding(true)
    setPlanDiscussionError('')
    try {
      const result = await applyPlanAdjustment(
        goalId,
        planAdjustmentPreview.id,
        acceptAdjustmentUnallocated,
        editedAdjustmentSchedule.map((item) => ({
          scheduled_date: item.date,
          title: item.title,
          start_local: item.start,
          end_local: item.end,
        })),
      )
      if (activeGoalIdRef.current !== goalId) return
      setWorkspace(result)
      setPlanAdjustmentPreview(null)
      setEditedAdjustmentSchedule([])
      setAcceptAdjustmentUnallocated(false)
      const latestMessage = result.messages?.at(-1)
      if (!result.pending_adjustment && latestMessage?.generation_status !== 'FAILED') {
        sessionStorage.removeItem(`planner:recovery-view:${goalId}`)
        navigate(`/goals/${goalId}/plan`, { replace: true })
        shouldFocusPlanChangeRef.current = true
        setIsPlanDiscussionOpen(false)
      }
    } catch (caught) {
      setPlanDiscussionError(
        caught instanceof Error ? caught.message : '暂时无法应用调整，请重新读取当前状态。',
      )
      try { setWorkspace(await getPlan(goalId)) } catch { /* Keep the publish error visible. */ }
    } finally {
      setPendingPlanIntent(null)
      setIsPlanResponding(false)
    }
  }

  function openRecoveryPage() {
    if (!goalId) return
    navigate(`/goals/${goalId}/progress?recovery=1`)
  }

  function revealPendingAdjustment() {
    setIsPlanDiscussionOpen(true)
    window.requestAnimationFrame(() => {
      pendingAdjustmentNoticeRef.current?.scrollIntoView?.({ behavior: 'smooth', block: 'start' })
    })
  }

  async function deferRecoveryAndKeepPreview() {
    const recoveryGuard = workspace?.recovery_guard
    if (!goalId || !recoveryGuard?.missed_dates.length || isPlanResponding) return
    setIsPlanResponding(true)
    setPlanDiscussionError('')
    try {
      await deferRecovery(goalId, recoveryGuard.missed_dates)
      const refreshed = await getPlan(goalId)
      setWorkspace(refreshed)
      setShowRecoveryGate(false)
    } catch (caught) {
      setPlanDiscussionError(caught instanceof Error ? caught.message : '暂时无法记录这次恢复选择。')
    } finally {
      setIsPlanResponding(false)
    }
  }

  async function restoreHistoricalPlan(planId: string) {
    if (!goalId || isPlanResponding) return
    setPendingPlanIntent('RESTORE')
    setIsPlanResponding(true)
    setPlanDiscussionError('')
    try {
      const result = await restorePlanVersion(goalId, planId)
      shouldFocusPlanChangeRef.current = true
      setWorkspace(result)
    } catch (caught) {
      setPlanDiscussionError(
        caught instanceof Error ? caught.message : '暂时无法恢复这份安排。',
      )
    } finally {
      setPendingPlanIntent(null)
      setIsPlanResponding(false)
    }
  }

  async function confirmCurrentPlan() {
    if (!goalId || !workspace?.plan || isConfirmingPlan || workspace.pending_adjustment) return
    if (['ACTIVE', 'RECOVERY_REQUIRED'].includes(workspace.status)) {
      navigate(`/goals/${goalId}/today`)
      return
    }
    if (workspace.status === 'READY_TO_START') {
      navigate(`/goals/${goalId}/prepare`)
      return
    }
    setIsConfirmingPlan(true)
    setPlanDiscussionError('')
    try {
      await confirmPlan(goalId)
      if (activeGoalIdRef.current !== goalId) return
      navigate(`/goals/${goalId}/prepare`)
    } catch (caught) {
      setPlanDiscussionError(caught instanceof Error ? caught.message : '暂时无法确认计划。')
      setIsConfirmingPlan(false)
    }
  }

  useEffect(() => {
    if (!shouldFocusPlanChangeRef.current || !workspace?.plan?.change_summary?.length) return
    shouldFocusPlanChangeRef.current = false
    window.requestAnimationFrame(() => {
      if (typeof planChangeSummaryRef.current?.scrollIntoView === 'function') {
        planChangeSummaryRef.current.scrollIntoView({ behavior: 'smooth', block: 'start' })
      }
    })
  }, [workspace])

  useEffect(() => {
    if (!goalId) return
    const currentGoalId = goalId
    let active = true
    let pollTimer: ReturnType<typeof setTimeout> | undefined
    async function loadPlan() {
      try {
        const result = await getPlan(currentGoalId)
        if (!active) return
        setWorkspace(result)
        const running = result.generation_job?.status === 'QUEUED'
          || result.generation_job?.status === 'RUNNING'
        setIsGenerating(running)
        if (running) {
          pollTimer = setTimeout(() => void loadPlan(), 1500)
        }
      } catch (caught) {
        if (active) {
          setError(caught instanceof Error ? caught.message : '暂时无法生成时间计划。')
          setIsGenerating(false)
        }
      }
    }
    void loadPlan()
    return () => {
      active = false
      if (pollTimer) clearTimeout(pollTimer)
    }
  }, [goalId, generationPollRun])

  async function beginGeneration(mode: 'STANDARD' | 'DEEP') {
    if (!goalId || isGenerating) return
    setSelectedGenerationMode(mode)
    setError('')
    setIsGenerating(true)
    try {
      setWorkspace(await startPlanGeneration(goalId, mode))
      setGenerationPollRun((run) => run + 1)
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : '暂时无法开始生成。')
      setIsGenerating(false)
    }
  }

  if (error && !workspace) {
    return <section className="page narrow error-state"><h1>时间规划暂时无法打开</h1><p role="alert">{error}</p>
      <button className="button" type="button" onClick={() => { setError(''); setGenerationPollRun((run) => run + 1) }}>重新读取</button></section>
  }
  if (!workspace && !isGenerating) {
    return <StageTransition restoring current={2} title="正在读取已保存的规划"
      detail="正在同步已有任务、讨论记录和生效版本。"
      checkpoints={['读取已保存的安排', '核对当前版本', '恢复讨论记录']} />
  }
  if (!workspace || isGenerating) {
    const hasOutline = Boolean(workspace?.plan)
    const generatedWeeks = workspace?.generated_weeks ?? 0
    const totalWeeks = workspace?.generation_target_weeks ?? workspace?.total_weeks ?? 0
    const mode = workspace?.generation_job?.mode ?? selectedGenerationMode
    const repair = workspace?.generation_repair
    const completedNote = hasOutline && totalWeeks > 0 ? `已经安排好 ${generatedWeeks} / ${totalWeeks} 周` : '正在整理整体节奏'
    return <StageTransition current={2}
      title={repair ? '正在修正安排' : mode === 'DEEP' ? '正在更仔细地安排时间' : '正在安排时间'}
      detail={repair?.message ?? (mode === 'DEEP' ? 'Solevia 会多花一些时间权衡任务量和先后顺序。你可以离开页面，稍后再回来。' : 'Solevia 会先整理整体节奏，再逐周补充每天要做的事。你可以离开页面，已经完成的内容不会丢失。')}
      checkpoints={['整理整体阶段和周期', '安排每周重点', '补充每天要做的事']} activeIndex={hasOutline ? 2 : 0}
      progressNote={repair ? `${completedNote} · 第 ${repair.attempt} / ${repair.maximum_attempts} 次生成` : completedNote} />
  }
  if (!['PLAN_DRAFTING', 'PLAN_REVIEW', 'READY_TO_START', 'ACTIVE', 'RECOVERY_REQUIRED', 'COMPLETED'].includes(workspace.status)) {
    return <Navigate to={`/goals/${workspace.goal_id}/route`} replace />
  }
  if (workspace.generation_phase !== 'COMPLETE') {
    const failedMessage = workspace.generation_job?.status === 'FAILED'
      ? workspace.generation_job.error_message
      : error
    const modes = workspace.available_generation_modes ?? ['STANDARD']
    const capacityFailure = workspace.generation_job?.status === 'FAILED'
      && ['PLAN_TEMPORAL_SEGMENT_MINUTES_INVALID', 'PLAN_TEMPORAL_SEGMENT_CAPACITY_INVALID'].includes(workspace.generation_job.error_code ?? '')
    return <section className="page narrow generation-mode-page">
      <p className="eyebrow">第 3 步 · 安排时间</p>
      <h1>{workspace.capacity_negotiation ? '一起调整可行的时间安排' : workspace.plan ? '继续生成剩余计划' : '选择计划生成方式'}</h1>
      {workspace.plan && <p>已经完成 {workspace.generated_weeks} / {workspace.generation_target_weeks} 周，只会补齐缺失部分。</p>}
      {failedMessage && <p className="inline-error" role="alert">{failedMessage}</p>}
      {capacityFailure && <button className="button secondary" type="button" onClick={() => navigate(`/goals/${workspace.goal_id}/clarify`)}>复核需求与时间</button>}
      {workspace.capacity_negotiation && <CapacityNegotiationPanel workspace={workspace} draft={planFeedbackDrafts.REVISE} busy={isPlanResponding} progress={planDiscussionProgress.message} error={planDiscussionError} preview={planAdjustmentPreview} onDraft={value => setPlanFeedbackDraft('REVISE', value)} onSubmit={() => void submitPlanDiscussion(planFeedbackDrafts.REVISE, 'REVISE')} onPreview={() => void applyPendingAdjustment()} onDiscard={() => void discardPendingAdjustment()} />}
      {!workspace.plan && <div className="planning-strategy-summary"><strong>{workspace.planning_mode === 'EXPLORATORY' ? '先试试看' : '直接固定规划'} · {scheduleTypeLabels[workspace.schedule_type]}</strong><span>{workspace.schedule_type === 'OUTCOME' ? '按弹性成果窗口设置目标与检查点，不指定某天必须行动。' : workspace.schedule_type === 'RHYTHM' ? '按效果所需频率形成稳定节奏，不锁死具体钟点。' : '围绕已确认期限倒排，并在开始前映射到日历。'}</span></div>}
      <h2 className="generation-quality-heading">生成质量</h2>
      <div className="generation-mode-grid">
        <button className="generation-mode-card" type="button" disabled={isPlanResponding || Boolean(workspace.pending_adjustment) || workspace.capacity_negotiation?.classification === 'CONSTRAINT_CONFLICT'} onClick={() => void beginGeneration('STANDARD')}>
          <strong>{capacityFailure ? '继续自动修正' : '标准生成'}</strong>
          <span>{capacityFailure ? '保留已完成周，重新生成尚未通过校验的部分。' : '速度优先，适合已经确认路线后的每日任务细化。'}</span>
          <em>推荐默认</em>
        </button>
        {modes.includes('DEEP') && <button className="generation-mode-card" type="button" disabled={isPlanResponding || Boolean(workspace.pending_adjustment) || workspace.capacity_negotiation?.classification === 'CONSTRAINT_CONFLICT'} onClick={() => void beginGeneration('DEEP')}>
          <strong>深度思考</strong>
          <span>进行更充分的推理，可能需要几分钟，可以离开页面后再回来。</span>
        </button>}
      </div>
    </section>
  }
  if (!workspace.plan) {
    return <section className="page narrow error-state"><h1>计划数据暂时无法读取</h1><p>已确认需求和路线仍然保留，请刷新后重试。</p></section>
  }

  const plan = workspace.plan
  const phasedCapacity = (plan.temporal_segments?.length ?? 0) > 1
  const completedPlan = workspace.status === 'COMPLETED'
  const executionStarted = ['ACTIVE', 'RECOVERY_REQUIRED', 'COMPLETED'].includes(workspace.status)
  const explorationDraft = executionStarted && Boolean(workspace.pending_exploration_window)
  const planIsConfirmed = workspace.status === 'READY_TO_START' || executionStarted
  const isHybridPlan = plan.execution_model_version === 'HYBRID_WORK_ITEM'
  const materializedWeeks = isHybridPlan
    ? workspace.generated_weeks
    : Math.max(0, ...plan.days.map(day => day.week_index))
  const weekIndexes = Array.from({ length: plan.planning_mode === 'EXPLORATORY' ? materializedWeeks : plan.total_weeks }, (_, index) => index + 1)

  async function handleExplorationWindow(action: 'DRAFT' | 'APPLY' | 'DISCARD') {
    if (!goalId || isExplorationWindowBusy) return
    setIsExplorationWindowBusy(true)
    setError('')
    try {
      const result = action === 'DRAFT'
        ? await draftExplorationWindow(goalId, selectedGenerationMode)
        : action === 'APPLY'
          ? await applyExplorationWindow(goalId)
          : await discardExplorationWindow(goalId)
      if (activeGoalIdRef.current !== goalId) return
      setWorkspace(result)
    } catch (caught) {
      if (activeGoalIdRef.current !== goalId) return
      setError(caught instanceof Error ? caught.message : '滚动窗口暂时无法处理。')
    } finally {
      if (activeGoalIdRef.current === goalId) setIsExplorationWindowBusy(false)
    }
  }
  const latestAppliedMessage = [...(workspace.messages ?? [])].reverse().find(
    (message) => message.metadata?.plan_revision === plan.revision
      && Array.isArray(message.metadata?.change_summary),
  )
  const regeneratedWeeks = Array.isArray(latestAppliedMessage?.metadata?.regenerated_weeks)
    ? latestAppliedMessage.metadata.regenerated_weeks.filter((value): value is number => typeof value === 'number')
    : []
  return (
    <section className="page planner-stage-page plan-review-page">
      <div className="planner-primary-column">
        <header className="planner-stage-header">
          <div className="stage-heading"><p className="eyebrow">{workspace.recovery_review?.is_recovery || workspace.status === 'RECOVERY_REQUIRED' ? '执行中 · 恢复讨论' : completedPlan ? '已结束 · 规划回顾' : explorationDraft ? '执行中 · 下一阶段草案' : executionStarted ? '执行中 · 当前安排' : '第 3 步 · 时间规划'}</p><h1>{workspace.recovery_review?.is_recovery || workspace.status === 'RECOVERY_REQUIRED' ? '讨论剩余任务的恢复安排' : completedPlan ? '回看已完成的安排' : explorationDraft ? '预览下一阶段安排' : executionStarted ? '查看已生效的安排' : '查看相对时间计划'}</h1></div>
          <PlannerStepper current={planIsConfirmed ? 3 : 2} viewing={2} executing={executionStarted} onStepSelect={(index) => {
            if (index === 0) navigate(`/goals/${workspace.goal_id}/clarify`)
            if (index === 1) navigate(`/goals/${workspace.goal_id}/route`)
            if (index === 3) navigate(executionStarted ? `/goals/${workspace.goal_id}/today` : `/goals/${workspace.goal_id}/prepare`)
          }} />
        </header>
        <div className="stage-main-scroll">
          <div className="relative-plan-main">
            {executionStarted && !workspace.pending_adjustment && !explorationDraft && <section className="pending-adjustment-notice" role="status"><div><p className="eyebrow">{completedPlan ? '本轮已结束' : '当前正在使用'}</p><h3>计划 v{plan.version}.{plan.revision} {completedPlan ? '已完成' : '已生效'}</h3><p>任务日期以执行日历为准。下方讨论保留历史记录，已处理的建议不会再次应用。</p></div><button className="button primary" type="button" onClick={() => navigate(`/goals/${goalId}/today`)}>查看更新后的执行安排</button></section>}
            <div className="relative-plan-intro">
              <div><p className="eyebrow">{completedPlan ? '已完成的安排' : explorationDraft ? '下一阶段草案，尚未接入执行' : planIsConfirmed ? '当前采用的安排' : '等你确认的安排'} · {scheduleTypeLabels[plan.schedule_type]}</p><h2>{explorationDraft ? `下一阶段草案 · 拟展开到第 ${materializedWeeks} 周` : plan.schedule_type === 'OUTCOME' ? `${materializedWeeks} 个成果窗口，按完成情况推进` : plan.planning_mode === 'EXPLORATORY' ? `体验路线当前展开到第 ${materializedWeeks} 周` : executionStarted ? `当前执行安排 · 共 ${plan.total_weeks} 周` : `${plan.total_weeks} 个相对周，不绑定具体日期`}</h2>{executionStarted && !explorationDraft && plan.revision > 1 ? <><p>{completedPlan ? '已完成版本' : '当前生效版本'} v{plan.version}.{plan.revision}，覆盖 {plan.total_weeks} 周。任务、日期和完成情况请查看当前执行日历。</p><details><summary>查看生成时的规划说明</summary><p>{plan.summary}</p></details></> : <p>{plan.summary}</p>}</div>
              <div className="plan-view-switch" aria-label="计划查看方式">
                <button className={viewMode === 'STAGE' ? 'selected' : ''} type="button" onClick={() => setViewMode('STAGE')}>按阶段</button>
                <button className={viewMode === 'WEEK' ? 'selected' : ''} type="button" onClick={() => setViewMode('WEEK')}>按周</button>
                {isHybridPlan && <button className={viewMode === 'DAY' ? 'selected' : ''} type="button" onClick={() => setViewMode('DAY')}>按执行日</button>}
              </div>
            </div>
            {workspace.generation_job?.status === 'COMPLETED' && <ProcessDetails title="规划生成记录" steps={[{ label: '规划已生成并保存', detail: `已安排 ${workspace.generated_weeks} 周` }]} />}
            <div className="relative-plan-stats"><div><span>{plan.schedule_type === 'OUTCOME' ? '成果窗口' : '建议周期'}</span><strong>{plan.total_weeks} {plan.schedule_type === 'OUTCOME' ? '个' : '周'}</strong></div><div><span>{plan.schedule_type === 'OUTCOME' ? '验收方式' : plan.schedule_type === 'RHYTHM' ? '每周节奏' : '每周执行日'}</span><strong>{plan.schedule_type === 'OUTCOME' ? '每窗口 1 个检查点' : phasedCapacity ? '按各阶段容量安排' : `${plan.weekly_execution_days} 个执行日`}</strong></div><div><span>{phasedCapacity ? "分阶段容量" : "容量参考"}</span><strong>{phasedCapacity ? "各时期分别计算" : `约 ${Math.round(plan.weekly_minutes / 60 * 10) / 10} 小时`}</strong></div></div>
            {Boolean(plan.temporal_segments?.length) && <section aria-label="分阶段可用时间"><CapacitySummary segments={plan.temporal_segments ?? []} />{plan.schedule_type === "OUTCOME" && <p className="muted">D1 是窗口检查点；以上天数是窗口内真实可用时间，任务可在其中灵活完成。</p>}</section>}
            {workspace.recovery_review?.is_recovery ? <div className="planning-strategy-summary" aria-label="本次恢复状态"><strong>继续处理已保存的恢复安排</strong><span>原计划和已完成记录保留。历史讨论可回看，当前待处理事项以下方恢复建议卡为准。</span></div> : !executionStarted && workspace.latest_generation_run && workspace.latest_generation_run.reports.length > 0 && <div className="planning-strategy-summary" aria-label="本次生成依据">
              <strong>已综合 {workspace.latest_generation_run.reports.filter((report) => ['COMPLETED', 'CACHED'].includes(report.status)).length} 份专长分析</strong>
              <span>{workspace.latest_generation_run.status === 'DEGRADED' ? '部分辅助分析未完成，最终结果已继续使用原始上下文和本地校验。' : '需求、约束与执行历史已经分别整理，并通过本地规则校验。'}</span>
            </div>}
            {workspace.pending_adjustment && <section className="pending-adjustment-notice" ref={pendingAdjustmentNoticeRef}>
              <div><p className="eyebrow">来自执行讨论 · {workspace.pending_adjustment.needs_clarification ? '有待处理事项' : '等你确认'}</p><h3>调整建议已经整理好，但尚未改变当前计划</h3><p>页面下方仍展示正在执行的原版本。确认调整后的日期与时间预览并发布后，新安排才会生效；已经完成的内容会保留。</p></div>
              <button className="button primary" type="button" onClick={revealPendingAdjustment}>查看并确认调整</button>
            </section>}
            {plan.planning_mode === 'EXPLORATORY' && <section className="rolling-plan-panel">
              <header><div><p className="eyebrow">探索式规划</p><h3>{workspace.pending_exploration_window ? `第 ${workspace.pending_exploration_window.start_week}-${workspace.pending_exploration_window.end_week} 周草案待确认` : plan.next_unplanned_week > plan.total_weeks ? '体验路线已全部展开' : `当前体验安排已展开到第 ${Math.max(1, plan.next_unplanned_week - 1)} 周`}</h3></div><span>按成果阶段展开</span></header>
              <p>{workspace.pending_exploration_window ? '这份草案使用了目前的完成状态、近期反馈和固定时间约束。确认前，正在执行的版本不会变化。' : workspace.pending_adjustment ? `第 ${plan.next_unplanned_week} 周及之后尚未生成；本次确认的新规则会在后续展开时继续沿用，不代表调整要等到第 ${plan.next_unplanned_week} 周才生效。` : workspace.can_extend_exploration ? `第 ${plan.next_unplanned_week} 周及之后尚未生成。准备好后可根据当前执行效果展开到下一个可观察成果；系统不会改写已经执行的内容。` : '先完成当前体验阶段并记录真实反馈，系统会据此判断后续安排。'}</p>
              {error && <p className="inline-error">{error}</p>}
              <div className="rolling-plan-actions">
                {workspace.pending_exploration_window ? <><button className="button" type="button" disabled={isExplorationWindowBusy} onClick={() => void handleExplorationWindow('DISCARD')}>放弃草案</button><button className="button primary" type="button" disabled={isExplorationWindowBusy} onClick={() => void handleExplorationWindow('APPLY')}>{isExplorationWindowBusy ? '正在处理…' : '确认并接入执行'}</button></> : workspace.can_extend_exploration ? <button className="button primary" type="button" disabled={isExplorationWindowBusy} onClick={() => void handleExplorationWindow('DRAFT')}>{isExplorationWindowBusy ? '正在结合执行效果生成…' : '展开下一成果阶段'}</button> : null}
              </div>
              {isExplorationWindowBusy && workspace.generation_repair && <p role="status">正在自动调整第 {workspace.generation_repair.week_index} 周的安排（第 {workspace.generation_repair.attempt} / {workspace.generation_repair.maximum_attempts} 次）。已完成的内容会继续保留。</p>}
            </section>}
            {!explorationDraft && (plan.change_summary ?? []).length > 0 && (
              <div className="plan-change-summary" ref={planChangeSummaryRef}>
                <header><div><p className="eyebrow">刚刚完成的修改</p><h3>接下来的安排已经更新</h3></div>{regeneratedWeeks.length > 0 && <div>{regeneratedWeeks.map((week) => <span key={week}>第 {week} 周</span>)}</div>}</header>
                <ul>{(plan.change_summary ?? []).map((item) => <li key={item}>{item}</li>)}</ul>
                <p>没有列出的周保持原样；需要时仍可以在右侧找回之前的安排。</p>
              </div>
            )}

            {isHybridPlan ? <HybridPlanReview plan={plan} generatedWeeks={workspace.generated_weeks} viewMode={viewMode} /> : <div className="plan-outline-list">
              {viewMode === 'STAGE' ? plan.milestones.map((milestone) => {
                const days = plan.days.filter((day) => day.stage_order === milestone.stage_order)
                return (
                  <details className="plan-outline-card" key={milestone.id}>
                    <summary><span>M{milestone.stage_order}</span><div><h3>{milestone.title}</h3><p>第 {milestone.start_week}{milestone.end_week !== milestone.start_week ? `–${milestone.end_week}` : ''} {plan.schedule_type === 'OUTCOME' ? '个窗口' : '周'} · {days.length} 个{plan.schedule_type === 'OUTCOME' ? '成果检查点' : '执行日'}</p></div><small>{milestone.outputs.join(' · ')}</small></summary>
                    <div className="plan-outline-detail"><p>{milestone.purpose}</p><div className="plan-days-list">{days.map((day) => <PlanDayCard day={day} allDays={plan.days} scheduleType={plan.schedule_type} key={day.id} />)}</div></div>
                  </details>
                )
              }) : weekIndexes.map((weekIndex) => {
                const days = plan.days.filter((day) => day.week_index === weekIndex)
                const stageOrders = [...new Set(days.map((day) => day.stage_order))]
                const minutes = days.reduce((total, day) => total + day.suggested_minutes, 0)
                return (
                  <details className="plan-outline-card" key={weekIndex}>
                    <summary><span>W{weekIndex}</span><div><h3>第 {weekIndex} 周</h3><p>{days.length} 个执行日 · 建议约 {minutes} 分钟</p></div><small>{stageOrders.map((order) => `M${order}`).join(' · ')}</small></summary>
                    <div className="plan-outline-detail"><div className="plan-days-list">{days.map((day) => <PlanDayCard day={day} allDays={plan.days} scheduleType={plan.schedule_type} key={day.id} />)}</div></div>
                  </details>
                )
              })}
            </div>}

            <div className="plan-draft-boundary"><strong>{completedPlan ? '本轮规划已完成' : explorationDraft ? '这是待确认的下一阶段草案' : executionStarted ? '这是你正在执行的计划' : isHybridPlan ? '规则和每日草案采用同一套安排逻辑' : planIsConfirmed ? '这份安排已经确认' : '确认前还可以继续调整'}</strong><p>{completedPlan ? '完成记录已经保存，本轮结束后不会自动生成后续规划。' : explorationDraft ? '确认接入前，当前执行安排和完成记录保持原样。请先接入或放弃这份草案，再提出新的计划调整。' : executionStarted ? '你可以放心查看。提出修改后，Solevia 会先说明准备怎么调，只有你确认后才会改变接下来的安排。' : isHybridPlan ? '按阶段和按周用于检查规则，按执行日用于检查具体怎么做；确认后仍会根据真实开始日和日历重新映射。' : planIsConfirmed ? '回看不会改变任何内容；返回准备开始后可以继续选择具体日期。' : '确认后再选择实际开始时间。现在展开任一阶段或周，就能查看每天的详细安排。'}</p></div>
          </div>
        </div>
        <section className={`route-discussion-dock plan-discussion-dock${isPlanDiscussionOpen ? ' open' : ''}`} aria-label="计划讨论">
          <header className="route-discussion-bar">
            <button className="discussion-toggle" type="button" aria-expanded={isPlanDiscussionOpen} onClick={() => setIsPlanDiscussionOpen((current) => !current)}>
              <span>计划讨论</span>
              <small>{planFeedback ? '内容已准备，可编辑后发送' : workspace.messages?.length ? '查看最近解释与问题' : '解释安排或调整时间强度'}</small>
            </button>
            <div className="discussion-bar-actions">
              <button className="button" type="button" disabled={completedPlan} onClick={() => {
                setIsPlanDiscussionOpen(true)
                window.requestAnimationFrame(() => planFeedbackInputRef.current?.focus())
              }}>写反馈</button>
              <button
                className="button primary"
                type="button"
                disabled={isConfirmingPlan || isPlanResponding || (!planIsConfirmed && Boolean(workspace.pending_adjustment))}
                title={!planIsConfirmed && workspace.pending_adjustment ? '请先确认或放弃当前调整建议' : undefined}
                onClick={() => completedPlan ? navigate(`/goals/${goalId}/progress`) : planIsConfirmed ? navigate(`/goals/${goalId}/${executionStarted ? 'today' : 'prepare'}`) : void confirmCurrentPlan()}
              >{isConfirmingPlan ? '正在确认计划…' : completedPlan ? '查看完成记录 →' : executionStarted ? '返回今日执行 →' : planIsConfirmed ? '返回准备开始 →' : '确认计划，准备开始 →'}</button>
            </div>
          </header>
          {isPlanDiscussionOpen && (
            <div className="route-discussion-panel plan-discussion-panel">
              <div className="route-discussion-history plan-feedback-guide" aria-live="polite">
                <article className="discussion-message assistant">
                  <strong>{ASSISTANT_NAME}</strong>
                  <p>你可以调整每周强度、总周期、阶段时间或缓冲，也可以只问为什么这样安排。快捷选项只会准备一段文字，不会直接修改计划。</p>
                </article>
                {(workspace.messages ?? []).map((message, index) => message.generation_status === 'FAILED' ? (
                  <article className="discussion-message assistant failed" key={message.id}>
                    <strong>{ASSISTANT_NAME}</strong>
                    <b>这次{message.metadata?.intent === 'APPLY' ? '应用调整' : message.metadata?.intent === 'REVISE' ? '调整分析' : '解释'}没有完成，但当前计划没有变化</b>
                    <p>{displayMessageContent(message.content, true)}</p>
                    <div className="failure-actions">
                      {message.metadata?.intent === 'APPLY' ? (
                        <button type="button" onClick={() => void applyPendingAdjustment()}>重新应用</button>
                      ) : (
                        <>
                          <button type="button" onClick={() => retryPlanDiscussion(index)}>重新尝试</button>
                          <button type="button" onClick={() => retryPlanDiscussion(index, true)}>修改内容</button>
                        </>
                      )}
                    </div>
                  </article>
                ) : (
                  <article className={`discussion-message ${message.role === 'USER' ? 'user' : 'assistant'}`} key={message.id}>
                    <strong>{message.role === 'USER' ? '你' : ASSISTANT_NAME}</strong>
                    {message.role === 'ASSISTANT' ? <AssistantAnswer content={displayMessageContent(message.content)} animate={message.generation_status === 'SUCCEEDED' && freshAnswer(message.id)} /> : <p>{message.content}</p>}
                    <AnswerResearch message={message} summary={workspace.latest_research} research={research} />
                    {message.role === 'ASSISTANT' && message.generation_status === 'SUCCEEDED' && index === workspace.messages.length - 1 && <div className="message-actions"><button type="button" disabled={isPlanResponding} onClick={() => regeneratePlanDiscussion(index)}>{isPlanResponding ? '正在生成…' : '重新生成'}</button></div>}
                  </article>
                ))}
                {workspace.pending_adjustment && (
                  <article className="adjustment-proposal-card">
                    <header>
                      <div><span>{ASSISTANT_NAME} 的调整建议</span><strong>接下来的安排可以这样调整</strong></div>
                      <em>{planAdjustmentPreview ? '预览已恢复，等你确认' : workspace.pending_adjustment.needs_clarification ? '需要处理下方事项' : '等你确认'}</em>
                    </header>
                    {workspace.recovery_review?.is_recovery && <p>{workspace.pending_adjustment.assistant_message}</p>}
                    <div className="adjustment-scope">
                      <strong>本次直接调整：{workspace.pending_adjustment.scope === 'OVERALL' ? '重新安排全部剩余内容' : workspace.pending_adjustment.scope === 'STAGE' ? '调整已展开的当前阶段' : workspace.pending_adjustment.scope === 'WEEK' ? '调整选中的一周' : '调整几个相关部分'}</strong>
                      {workspace.pending_adjustment.affected_stage_orders.map((order) => <span key={`M${order}`}>当前 M{order}</span>)}
                      {workspace.pending_adjustment.affected_weeks.map((week) => <span key={`W${week}`}>重排 W{week}</span>)}
                      {workspace.pending_adjustment.downstream_affected_weeks.map((week) => <span className="downstream" key={`downstream-${week}`}>后续生成沿用 W{week}</span>)}
                    </div>
                    <section><strong>准备怎样调整</strong><ul>{workspace.pending_adjustment.proposed_changes.map((item) => <li key={item}>{item}</li>)}</ul></section>
                    {workspace.pending_adjustment.tradeoffs.length > 0 && <section><strong>需要接受的取舍</strong><ul>{workspace.pending_adjustment.tradeoffs.map((item) => <li key={item}>{item}</li>)}</ul></section>}
                    <section><strong>保持不变</strong><ul>{workspace.pending_adjustment.unchanged_guarantees.map((item) => <li key={item}>{item}</li>)}</ul></section>
                    {workspace.pending_adjustment.needs_clarification && workspace.pending_adjustment.clarifying_question && <p className="proposal-question">{workspace.pending_adjustment.clarifying_question}</p>}
                    <RecoveryDecisions review={workspace.recovery_review} busy={isPlanResponding} onDecision={decision => void confirmRecoveryDecision(decision)} />
                    {isPlanResponding && pendingPlanIntent === 'APPLY' && !planAdjustmentPreview && <section className="adjustment-time-preview adjustment-time-preview-loading" role="status" aria-live="polite"><strong>正在生成调整预览…</strong><small>正在按新的日期、任务依赖和可用时间逐日试算，当前计划不会改变。</small><span className="typing-dots"><i /><i /><i /></span></section>}
                    {planAdjustmentPreview && <section className="adjustment-time-preview" aria-label="调整后的时间预览">
                      <strong>调整后的时间安排</strong>
                      <small>这是试算结果。你可以逐项修改起止时间，确认发布时系统会重新校验窗口、冲突和前置关系。</small>
                      <AdjustmentScheduleEditor schedule={editedAdjustmentSchedule} onChange={setEditedAdjustmentSchedule} />
                      {planAdjustmentPreview.schedule.length === 0 && <small>{isHybridPlan ? '这次先确认工作项、窗口和依赖；具体日期会在开始计划前单独预览。' : '这次调整没有产生可展示的时间段；任务仍保留在日期规划中，请检查可用时间或确认保持自由安排。'}</small>}
                      {planAdjustmentPreview.schedule.length > 12 && <small>还有 {planAdjustmentPreview.schedule.length - 12} 项安排未展开。</small>}
                      {planAdjustmentPreview.unallocated_count > 0 && <label className="proposal-question"><input type="checkbox" checked={acceptAdjustmentUnallocated} onChange={(event) => setAcceptAdjustmentUnallocated(event.target.checked)} /> 有 {planAdjustmentPreview.unallocated_count} 项任务暂时没有可用时段，确认保留为待安排状态</label>}
                    </section>}
                    {showRecoveryGate && workspace.recovery_guard?.required && <section className="adjustment-recovery-gate" role="alert">
                      <strong>发布前先处理未完成任务</strong>
                      <p>{workspace.recovery_guard.message}</p>
                      <small>已发现 {workspace.recovery_guard.missed_execution_days} 个未完成执行日，其中 {workspace.recovery_guard.interrupted_execution_days} 个属于中断执行。不会自动删除这些记录。</small>
                      <div className="adjustment-recovery-actions">
                        <button className="button primary" type="button" onClick={openRecoveryPage}>先处理追赶</button>
                        <button className="button" type="button" onClick={() => void deferRecoveryAndKeepPreview()} disabled={isPlanResponding}>从今天重新开始</button>
                        <button className="button" type="button" onClick={() => setShowRecoveryGate(false)} disabled={isPlanResponding}>暂不处理</button>
                      </div>
                    </section>}
                    <footer>
                      <button className="button" type="button" disabled={isPlanResponding} onClick={() => preparePlanFeedback('', 'REVISE')}>补充具体调整意见</button>
                      <button className="button" type="button" disabled={isPlanResponding} onClick={() => void discardPendingAdjustment()}>放弃调整</button>
                      <button className="button primary" type="button" disabled={isPlanResponding || !workspace.pending_adjustment.can_apply || Boolean(planFeedbackDrafts.REVISE.trim()) || Boolean(planAdjustmentPreview?.unallocated_count && !acceptAdjustmentUnallocated)} onClick={() => void applyPendingAdjustment()}>{!workspace.pending_adjustment.can_apply ? '补充后再决定' : planFeedbackDrafts.REVISE.trim() ? '先发送补充' : planAdjustmentPreview ? '确认并发布调整' : isHybridPlan ? '生成可确认的工作项草案' : '先预览日期与时间'}</button>
                    </footer>
                  </article>
                )}
                {pendingPlanFeedback && (
                  <article className="discussion-message user pending">
                    <strong>你</strong>
                    <p>{pendingPlanFeedback}</p>
                  </article>
                )}
                <ResearchProgress research={research} />
                <OperationProgress active={(localResponding && !research.busy) || isExplorationWindowBusy} scope={`${goalId}:${research.current?.id ?? ''}`}
                  label={isExplorationWindowBusy ? '正在生成后续阶段' : pendingPlanIntent === 'APPLY' ? '正在生成调整预览并校验日期和时间' : pendingPlanIntent === 'RESTORE' ? '正在恢复之前的安排' : pendingPlanIntent === 'REVISE' ? '正在分析反馈并生成调整建议' : '正在结合计划生成回答'}
                  detail={workspace.capacity_negotiation?.preview_progress?.status === 'RUNNING' ? `已校验 ${workspace.capacity_negotiation.preview_progress.completed_weeks} / ${workspace.capacity_negotiation.preview_progress.total_weeks ?? '待确定'} 周` : undefined} error={planDiscussionError} />
                <div ref={planDiscussionEndRef} />
              </div>
              <form className="route-discussion-composer" onSubmit={handlePlanFeedback}>
                <div className="feedback-intent" aria-label="计划反馈用途">
                  <span>这次希望：</span>
                  <button className={planFeedbackIntent === 'REVISE' ? 'selected' : ''} type="button" onClick={() => setPlanFeedbackIntent('REVISE')}>调整计划</button>
                  <button className={planFeedbackIntent === 'EXPLAIN' ? 'selected' : ''} type="button" onClick={() => setPlanFeedbackIntent('EXPLAIN')}>只做解释</button>
                </div>
                {planFeedbackIntent === 'REVISE' ? (
                  <>
                    <div className="plan-feedback-shortcuts" aria-label="常用计划调整">
                      <button type="button" onClick={() => preparePlanFeedback('当前每周安排有些太密，请在不改变一级路线的前提下降低每周强度，并说明总周期会如何变化。')}>每周安排太密</button>
                      <button type="button" onClick={() => preparePlanFeedback('当前每周安排偏松，我可以投入更多时间。请适度提高每周强度，但仍保留必要缓冲。')}>每周安排太松</button>
                      <button type="button" onClick={() => preparePlanFeedback('当前总周期对我来说太长，请说明可以怎样压缩，以及需要接受什么取舍。')}>总周期太长</button>
                      <button type="button" onClick={() => preparePlanFeedback('我希望增加一些缓冲，避免偶尔有事就打乱整体计划。请尽量保持路线不变。')}>增加缓冲</button>
                    </div>
                    <p className="feedback-scope-hint">调整范围跟随上方“按阶段 / 按周”的查看方式：</p>
                    <div className="plan-feedback-shortcuts" aria-label={viewMode === 'STAGE' ? '选择要调整的计划阶段' : '选择要调整的计划周'}>
                      {viewMode === 'STAGE' ? plan.milestones.map((milestone) => (
                        <button type="button" key={milestone.id} onClick={() => preparePlanFeedback(`请调整阶段 M${milestone.stage_order}「${milestone.title}」在第 ${milestone.start_week}${milestone.end_week === milestone.start_week ? '' : `–${milestone.end_week}`} 周的安排。我的具体想法是：`)}>M{milestone.stage_order} · {milestone.title}</button>
                      )) : weekIndexes.map((weekIndex) => (
                        <button type="button" key={weekIndex} onClick={() => preparePlanFeedback(`请只调整第 ${weekIndex} 周的计划，其他周尽量保持不变。我的具体想法是：`)}>W{weekIndex} · 调整第 {weekIndex} 周</button>
                      ))}
                    </div>
                  </>
                ) : (
                  <div className="plan-feedback-shortcuts" aria-label={viewMode === 'STAGE' ? '选择要解释的计划阶段' : '选择要解释的计划周'}>
                    <button type="button" onClick={() => preparePlanFeedback('请用大白话解释这份时间计划为什么这样安排，重点说明阶段时长、每周强度和缓冲。', 'EXPLAIN')}>解释整体安排</button>
                    {viewMode === 'STAGE' ? plan.milestones.map((milestone) => (
                      <button type="button" key={milestone.id} onClick={() => preparePlanFeedback(`请解释阶段 M${milestone.stage_order}「${milestone.title}」为什么安排在第 ${milestone.start_week}${milestone.end_week === milestone.start_week ? '' : `–${milestone.end_week}`} 周，以及这个时长与前后阶段的关系。`, 'EXPLAIN')}>M{milestone.stage_order} · {milestone.title}</button>
                    )) : weekIndexes.map((weekIndex) => (
                      <button type="button" key={weekIndex} onClick={() => preparePlanFeedback(`请解释第 ${weekIndex} 周为什么这样安排：这一周的重点、任务量和它在当前阶段中的作用分别是什么？`, 'EXPLAIN')}>W{weekIndex} · 第 {weekIndex} 周</button>
                    ))}
                  </div>
                )}
                <label htmlFor="plan-feedback">{planFeedbackIntent === 'EXPLAIN' ? '想了解什么？' : '希望怎样调整？'}</label>
                <div className="composer-input-well"><textarea ref={planFeedbackInputRef} id="plan-feedback" onKeyDown={submitOnEnter} title="Enter 发送，Shift + Enter 换行" value={planFeedback} onChange={(event) => setPlanFeedbackDraft(planFeedbackIntent, event.target.value)} placeholder={planFeedbackIntent === 'EXPLAIN' ? '例如：为什么第三阶段需要三周？' : '例如：每周六天有些密，希望调整为五天……'} />
                <div className="discussion-composer-toolbar">
                  <MaterialWorkspacePanel goalId={goalId} compact />
                  <button
                    className={`research-toggle-button${webResearchEnabled ? ' active' : ''}`}
                    type="button"
                    aria-label={webResearchEnabled ? '关闭联网检索' : '开启联网检索'}
                    aria-pressed={webResearchEnabled}
                    title={webResearchEnabled ? '已允许模型判断并调用联网检索，点击关闭' : '允许模型判断是否需要联网检索'}
                    disabled={isPlanResponding}
                    onClick={() => setWebResearchEnabled((current) => !current)}
                  ><Globe2 size={16} aria-hidden="true" /></button>
                <button className="button primary" type="submit" disabled={completedPlan || !planFeedback.trim() || isPlanResponding}>{isPlanResponding ? '正在整理…' : '发送'}</button></div></div>
                <ResearchConnectionStatus connection={webConnection} />
                {planDiscussionError && <p className="inline-error">{planDiscussionError}</p>}
                <div className="review-actions">
                  <span>{planFeedbackIntent === 'EXPLAIN' ? '只回答你的问题，当前安排不会变化。' : 'Solevia 会先给出调整建议，你确认后才会改变计划。'}</span>

                </div>
              </form>
            </div>
          )}
        </section>
      </div>
      <aside className="stage-context-column memory-column" aria-label="计划依据">
        <div className="panel route-context">
          <div className="confirmed-summary-heading"><h2>这份安排基于</h2><span>已确认路线</span></div>
          {['available_capacity', 'deadline', 'success_criteria'].map((key) => {
            const value = workspace.requirement_snapshot[key]
            if (value == null || value === '') return null
            return <div className="confirmed-row" key={key}><strong>{factLabels[key]}</strong><p>{Array.isArray(value) ? value.join('、') : String(value)}</p></div>
          })}
          <div className="plan-side-notes"><h3>{executionStarted ? '原路线的缓冲约定' : '缓冲策略'}</h3>{plan.buffer_strategy.map((item) => <p key={item}>{item}</p>)}</div>
          {plan.assumptions.length > 0 && <div className="plan-side-notes"><h3>{executionStarted ? '原规划时的假设' : '当前假设'}</h3>{plan.assumptions.map((item) => <p key={item}>{item}</p>)}</div>}
          {plan.open_questions.length > 0 && <div className="plan-side-notes warning"><h3>{executionStarted ? '原规划时记录的问题' : '确认前需处理'}</h3>{plan.open_questions.map((item) => <p key={item}>{item}</p>)}</div>}
          <button className="button" type="button" onClick={() => navigate(`/goals/${workspace.goal_id}/clarify`)}>回看需求</button>
          {(workspace.revision_history ?? []).length > 1 && (
            <div className="plan-version-history">
              <h3>之前的安排</h3>
              {(workspace.revision_history ?? []).map((item) => (
                <div key={item.id}>
                  <span>{item.id === plan.id ? '当前安排' : `第 ${item.revision} 次调整前`}</span>
                  {item.id !== plan.id && <button type="button" disabled={completedPlan || isPlanResponding} onClick={() => void restoreHistoricalPlan(item.id)}>恢复这份安排</button>}
                </div>
              ))}
            </div>
          )}
        </div>
      </aside>
    </section>
  )
}

export function AdjustmentScheduleEditor({
  schedule,
  onChange,
}: {
  schedule: PlanAdjustmentPreview['schedule']
  onChange: (next: PlanAdjustmentPreview['schedule']) => void
}) {
  return <div className="adjustment-schedule-editor">
    {schedule.slice(0, 12).map((item, index) => <div className="adjustment-schedule-row" key={`${item.date}-${item.title}-${index}`}>
      <span className="adjustment-schedule-task"><strong>{item.title}</strong><small>{item.date} · {item.minutes} 分钟</small></span>
      <label>开始<TimePicker value={item.start ?? ''} onChange={(event) => {
        const start = event.target.value || null
        const end = start && item.end && item.end <= start ? null : item.end
        onChange(schedule.map((entry, entryIndex) => entryIndex === index ? { ...entry, start, end, state: start && end ? 'SCHEDULED' : 'UNALLOCATED' } : entry))
      }} /></label>
      <label>结束<TimePicker value={item.end ?? ''} onChange={(event) => {
        const end = event.target.value || null
        const start = end && item.start && item.start >= end ? null : item.start
        onChange(schedule.map((entry, entryIndex) => entryIndex === index ? { ...entry, start, end, state: start && end ? 'SCHEDULED' : 'UNALLOCATED' } : entry))
      }} /></label>
    </div>)}
  </div>
}
