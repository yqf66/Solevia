import { executionHeading, executionDayLabel } from './executionLabels'
import { TimePicker } from '../ui/TimePicker'
import { DialogBackdrop } from '../ui/DialogBackdrop'
import './ExecutionRefinement.css'
import './ExecutionIdentity.css'
import { TodayBrief } from './TodayBrief'
import { useEffect, useRef, useState } from 'react'
import { CalendarClock } from 'lucide-react'
import { ExecutionDayMoveDialog } from '../ExecutionDateControls'
import { useLocation, useNavigate, useParams } from 'react-router-dom'
import {
  applyFeedbackAdjustment,
  allocateEntirePlan,
  carryForwardTask,
  completePlanTask,
  createQuickCapture,
  deleteFeedback,
  deleteQuickCapture,
  discardPlanAdjustment,
  discardFeedbackAdjustment,
  getExecutionCalendar,
  getScheduleFoundation,
  updateCommitmentReminder,
  getUnifiedToday,
  scheduleTodayTask,
  getFocusSessions,
  getPlan,
  getToday,
  getQuickCaptures,
  revisePlan,
  recordFocusSession,
  resumeGoal,
  startPlanTask,
  submitDayPaceFeedback,
  submitTaskFeedback,
  updateReminderEvent,
  type ExecutionCalendar,
  type FocusSession,
  type PlanAdjustmentProposal,
  type QuickCapture,
  type PlanScheduleAllocation,
  type ScheduleFoundation,
  type TodayWorkspace,
  type UnifiedToday,
} from '../api'
import { type FeedbackTarget, feedbackLabel, intentLabels, dayPaceChoices, feedbackStatusLabels } from './feedbackOptions'
import { FeedbackDialog } from './FeedbackDialog'
import { AssistantAnswer } from '../ui/AssistantAnswer'
import { OperationProgress } from '../ui/ProcessDetails'
import { StageTransition } from '../ui/PlannerStage'
import { previewInactivity } from './recoveryPreview'
import { RecoveryShiftDialog, PauseGoalDialog } from './RecoveryControls'
import { dateInputValue, friendlyDate, ASSISTANT_NAME } from '../ui/plannerPresentation'
import { ExecutionCalendarWidget, CollapsibleTool, FocusTimerWidget, QuickCaptureWidget, ExecutionInsightWidget } from './ExecutionTools'
import { ModalLayer } from '../ui/ModalLayer'
import { ExecutionObligations } from './ExecutionObligations'
import { TaskRecommendations } from './TaskRecommendations'
import { FlexibleTaskDecisionDialog, type FlexibleDecisionTarget } from './FlexibleTaskDecisionDialog'
import { TaskProgressDialog, type TaskProgressTarget } from './TaskProgressDialog'
import { ExplorationContinuation } from './ExplorationContinuation'
import './TodayPage.css'
import { ExecutionTaskCard } from './ExecutionTaskCard'
import { ExecutionNoticeBar } from './ExecutionNoticeBar'
import './ExecutionTaskCard.css'

export function TodayPage() {
  const { goalId } = useParams()
  const location = useLocation()
  const navigate = useNavigate()
  const [todayToolsOpen, setTodayToolsOpen] = useState(() => {
    if (typeof window === 'undefined') return true
    try {
      return window.localStorage.getItem('plan-a.today-tools-collapsed') !== '1'
    } catch {
      return true
    }
  })
  const activeGoalRef = useRef(goalId)
  useEffect(() => {
    activeGoalRef.current = goalId
    return () => { activeGoalRef.current = undefined }
  }, [goalId])
  const [workspace, setWorkspace] = useState<TodayWorkspace | null>(null)
  const [error, setError] = useState('')
  const [busyTaskId, setBusyTaskId] = useState('')
  const [reminderBusyLabel, setReminderBusyLabel] = useState('')
  const [reminderEventBusyId, setReminderEventBusyId] = useState('')
  const [feedbackTarget, setFeedbackTarget] = useState<FeedbackTarget | null>(null)
  const [planProposal, setPlanProposal] = useState<PlanAdjustmentProposal | null>(null)
  const [proposalFollowup, setProposalFollowup] = useState('')
  const [planAdjustmentResult, setPlanAdjustmentResult] = useState('')
  const [calendar, setCalendar] = useState<ExecutionCalendar | null>(null)
  const [flexibleDecisionTarget, setFlexibleDecisionTarget] = useState<FlexibleDecisionTarget | null>(null)
  const [flexibleArrangeNotice, setFlexibleArrangeNotice] = useState('')
  const [taskProgressTarget, setTaskProgressTarget] = useState<TaskProgressTarget | null>(null)
  const [focusSessions, setFocusSessions] = useState<FocusSession[]>([])
  const [quickCaptures, setQuickCaptures] = useState<QuickCapture[]>([])
  const [recoveryOpen, setRecoveryOpen] = useState(false)
  const [pauseOpen, setPauseOpen] = useState(false)
  const [feedbackDeleteTarget, setFeedbackDeleteTarget] = useState<{ id: string; label: string } | null>(null)
  const [unifiedToday, setUnifiedToday] = useState<UnifiedToday | null>(null)
  const [scheduleTarget, setScheduleTarget] = useState<UnifiedToday['fixed_tasks'][number] | null>(null)
  const [scheduleStart, setScheduleStart] = useState('19:00')
  const [scheduleEnd, setScheduleEnd] = useState('19:30')
  const [scheduleConflict, setScheduleConflict] = useState(false)
  const [parallelReason, setParallelReason] = useState('')
  const [scheduleFoundation, setScheduleFoundation] = useState<ScheduleFoundation | null>(null)
  const [planScheduleOpen, setPlanScheduleOpen] = useState(false)
  const [planScheduleBusy, setPlanScheduleBusy] = useState(false)
  const [planScheduleResult, setPlanScheduleResult] = useState<PlanScheduleAllocation | null>(null)
  const [carryForwardOpen, setCarryForwardOpen] = useState(false)
  const [dayMoveOpen, setDayMoveOpen] = useState(false)
  const [carryForwardMinutes, setCarryForwardMinutes] = useState(30)
  const [carryForwardTaskId, setCarryForwardTaskId] = useState('')
  const [carryForwardMode, setCarryForwardMode] = useState<'FULL' | 'PARTIAL'>('FULL')


  const toggleTodayTools = () => {
    setTodayToolsOpen((current) => {
      const next = !current
      try {
        window.localStorage.setItem('plan-a.today-tools-collapsed', next ? '0' : '1')
      } catch {
        // The sidebar still works when storage is unavailable (for example in private mode).
      }
      return next
    })
  }

  useEffect(() => {
    if (!goalId) return
    let active = true
    getToday(goalId).then((result) => {
      if (!active) return
      if (result.status === 'COMPLETED') {
        navigate(`/goals/${goalId}/complete`, { replace: true })
        return
      }
      setWorkspace(result)
    }).catch((caught) => {
      if (active) setError(caught instanceof Error ? caught.message : '暂时无法读取今日计划。')
    })
    getPlan(goalId).then((result) => {
      if (active) setPlanProposal(result.pending_adjustment)
    }).catch(() => {
      // Today can still open if the optional adjustment draft cannot be read.
    })
    getExecutionCalendar(goalId).then((result) => {
      if (active) setCalendar(result)
    }).catch(() => {
      // Today remains usable while calendar details are unavailable.
    })
    getFocusSessions(goalId).then((result) => {
      if (active) setFocusSessions(result)
    }).catch(() => {
      // Focus history is supplementary to today's task list.
    })
    getQuickCaptures(goalId).then((result) => {
      if (active) setQuickCaptures(result)
    }).catch(() => {
      // The capture inbox can recover independently on the next page load.
    })
    getUnifiedToday().then((result) => {
      if (active) setUnifiedToday(result)
    }).catch(() => {
      // Execution remains usable if the shared schedule cannot be loaded.
    })
    getScheduleFoundation(goalId).then((result) => {
      if (active) setScheduleFoundation(result)
    }).catch(() => {
      // Scheduling remains available; suggested free windows are optional.
    })
    return () => { active = false }
  }, [goalId, navigate])

  useEffect(() => {
    if (!goalId || !workspace) return
    const params = new URLSearchParams(location.search)
    const detected = previewInactivity(workspace.inactivity, location.search)
    if (
      workspace.status !== 'PAUSED'
      && (detected?.level === 'PAUSED_3_DAYS' || detected?.level === 'PAUSED_7_DAYS')
      && (!detected.recovery_deferred || detected.has_new_missed_work)
      && params.get('resume') !== '1'
    ) {
      navigate(`/goals/${goalId}/progress${location.search}`, { replace: true })
    }
  }, [goalId, location.search, navigate, workspace])

  function refreshCalendar() {
    if (!goalId) return
    void getExecutionCalendar(goalId).then(setCalendar).catch(() => undefined)
  }

  async function refreshAfterRecovery() {
    if (!goalId) return
    const [today, updatedCalendar] = await Promise.all([
      getToday(goalId),
      getExecutionCalendar(goalId),
    ])
    setWorkspace(today)
    setCalendar(updatedCalendar)
  }

  async function saveFocusSession(input: { task_id: string | null; planned_minutes: number; actual_seconds: number; interruptions: number }) {
    if (!goalId) return
    const saved = await recordFocusSession(goalId, input)
    setFocusSessions((current) => [saved, ...current].slice(0, 30))
    setWorkspace(await getToday(goalId, workspace?.today))
  }

  async function stopCommitmentReminder(label: string) {
    if (!goalId) return
    setReminderBusyLabel(label)
    setError('')
    try {
      await updateCommitmentReminder(goalId, label, false)
      setWorkspace(await getToday(goalId, workspace?.today))
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : '暂时无法停止这项提醒。')
    } finally {
      setReminderBusyLabel('')
    }
  }

  async function acknowledgeReminder(eventId: string, action: 'READ' | 'DISMISS') {
    if (!goalId) return
    setReminderEventBusyId(eventId)
    setError('')
    try {
      await updateReminderEvent(goalId, eventId, action)
      setWorkspace(await getToday(goalId, workspace?.today))
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : '暂时无法更新这条提醒。')
    } finally {
      setReminderEventBusyId('')
    }
  }

  async function addQuickCapture(content: string) {
    if (!goalId) return
    const saved = await createQuickCapture(goalId, content)
    setQuickCaptures((current) => [saved, ...current])
  }

  async function removeQuickCapture(captureId: string) {
    if (!goalId) return
    setQuickCaptures(await deleteQuickCapture(goalId, captureId))
  }

  async function selectCalendarDate(value: string) {
    if (!goalId || busyTaskId) return
    setBusyTaskId('DATE')
    setError('')
    try {
      const [nextWorkspace, nextUnified] = await Promise.all([
        getToday(goalId, value),
        getUnifiedToday(value),
      ])
      setWorkspace(nextWorkspace)
      setUnifiedToday(nextUnified)
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : '暂时无法读取这一天的日志。')
    } finally {
      setBusyTaskId('')
    }
  }

  async function updateTask(taskId: string, action: 'START' | 'COMPLETE') {
    if (!goalId || busyTaskId) return
    setBusyTaskId(taskId)
    setError('')
    try {
      const updated = action === 'START' ? await startPlanTask(goalId, taskId) : await completePlanTask(goalId, taskId)
      if (activeGoalRef.current !== goalId) return
      if (updated.status === 'COMPLETED') {
        navigate(`/goals/${goalId}/complete`, { replace: true })
      } else {
        setWorkspace(updated)
      }
      refreshCalendar()
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : '暂时无法更新任务。')
    } finally {
      setBusyTaskId('')
    }
  }

  function taskProgressSaved(updated: TodayWorkspace, notice: string) {
    setTaskProgressTarget(null)
    setWorkspace(updated)
    setFlexibleArrangeNotice(notice)
    refreshCalendar()
    void getUnifiedToday(updated.today).then(setUnifiedToday).catch(() => undefined)
  }

  async function carryForward() {
    if (!goalId || !workspace || busyTaskId || !carryForwardTaskId) return
    setBusyTaskId('CARRY_FORWARD')
    setError('')
    try {
      const updated = await carryForwardTask(goalId, {
        target_date: workspace.today,
        task_id: carryForwardTaskId,
        minutes: carryForwardMode === 'PARTIAL' ? carryForwardMinutes : null,
      })
      setWorkspace(updated)
      setCarryForwardOpen(false)
      refreshCalendar()
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : '暂时无法承接后续任务。')
    } finally {
      setBusyTaskId('')
    }
  }

  function openFlexibleReschedule(task: { id: string; title: string; scheduled_date: string; estimate_minutes: number }) {
    setFlexibleDecisionTarget({ ...task, action: 'RESCHEDULE' })
  }

  function flexibleDecisionApplied(updated: TodayWorkspace, notice: string) {
    setFlexibleDecisionTarget(null)
    setFlexibleArrangeNotice(notice)
    if (updated.today === actualToday) setWorkspace(updated)
    else if (goalId) void getToday(goalId).then(setWorkspace).catch(() => setError('安排已保存，请刷新今日任务。'))
    refreshCalendar()
    void getUnifiedToday(actualToday).then(setUnifiedToday).catch(() => undefined)
  }

  function openCarryForward() {
    const first = workspace?.carry_forward_tasks?.find((item) => item.can_carry)
    setCarryForwardTaskId(first?.task_id ?? '')
    setCarryForwardMode('FULL')
    setCarryForwardMinutes(Math.min(30, first?.estimate_minutes ?? 30))
    setCarryForwardOpen(true)
  }

  async function resumeExecution() {
    if (!goalId || busyTaskId) return
    setBusyTaskId('RESUME')
    setError('')
    try {
      await resumeGoal(goalId)
      setWorkspace(await getToday(goalId))
      refreshCalendar()
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : '暂时无法恢复目标。')
    } finally {
      setBusyTaskId('')
    }
  }

  async function saveFeedback(reason: string, intent: string, detail: string, remainingMinutes: number | null) {
    if (!goalId || !feedbackTarget) return
    const updated = feedbackTarget.kind === 'TASK'
      ? await submitTaskFeedback(goalId, feedbackTarget.taskId, { reason_code: reason, intent_code: intent, detail_text: detail || null, remaining_minutes: remainingMinutes })
      : await submitDayPaceFeedback(goalId, { reason_code: reason, intent_code: intent, detail_text: detail || null })
    setWorkspace(updated)
    refreshCalendar()
    setFeedbackTarget(null)
    if (reason === 'CUSTOM_ADJUSTMENT' && detail) {
      setBusyTaskId('PLAN_ANALYSIS')
      try {
        const result = await revisePlan(goalId, detail)
        setPlanProposal(result.pending_adjustment)
        setPlanAdjustmentResult('')
      } catch (caught) {
        setError(caught instanceof Error ? caught.message : '反馈已保存，但暂时无法生成调整方案。')
      } finally {
        setBusyTaskId('')
      }
    }
  }

  async function analyzeAsPlanAdjustment(feedbackId: string) {
    if (!goalId || busyTaskId) return
    const feedback = workspace?.feedbacks.find((item) => item.id === feedbackId)
    if (!feedback) return
    setBusyTaskId(feedbackId)
    setError('')
    try {
      const request = feedback.detail_text || `${feedbackLabel(feedback.reason_code)}，希望${feedback.intent_code ? intentLabels[feedback.intent_code] : '调整当前计划'}。`
      const result = await revisePlan(goalId, request)
      setPlanProposal(result.pending_adjustment)
      setProposalFollowup('')
      setPlanAdjustmentResult('')
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : '暂时无法生成阶段调整方案。')
      try {
        setWorkspace(await getToday(goalId))
      } catch {
        // Keep the currently visible workspace if refreshing also fails.
      }
    } finally {
      setBusyTaskId('')
    }
  }

  async function continuePlanProposal() {
    if (!goalId || !proposalFollowup.trim() || busyTaskId) return
    setBusyTaskId('PLAN_ANALYSIS')
    setError('')
    try {
      const result = await revisePlan(goalId, proposalFollowup.trim())
      setPlanProposal(result.pending_adjustment)
      setProposalFollowup('')
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : '暂时无法补充调整说明。')
    } finally {
      setBusyTaskId('')
    }
  }

  async function decidePlanProposal(action: 'APPLY' | 'DISCARD') {
    if (!goalId || !planProposal || busyTaskId) return
    if (action === 'APPLY') {
      navigate(`/goals/${goalId}/plan?discussion=1&adjustment=1`)
      return
    }
    setBusyTaskId('PLAN_DECISION')
    setError('')
    try {
      const result = await discardPlanAdjustment(goalId)
      setPlanProposal(result.pending_adjustment)
      setProposalFollowup('')
      setPlanAdjustmentResult('已放弃这份调整建议，当前安排没有变化。')
      setWorkspace(await getToday(goalId))
      refreshCalendar()
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : '暂时无法处理这份调整建议。')
      try {
        setWorkspace(await getToday(goalId))
      } catch {
        // Keep the currently visible workspace if refreshing also fails.
      }
    } finally {
      setBusyTaskId('')
    }
  }

  async function decideAdjustment(adjustmentId: string, action: 'APPLY' | 'DISCARD') {
    if (!goalId || busyTaskId) return
    setBusyTaskId(adjustmentId)
    setError('')
    try {
      const updated = action === 'APPLY'
        ? await applyFeedbackAdjustment(goalId, adjustmentId)
        : await discardFeedbackAdjustment(goalId, adjustmentId)
      setWorkspace(updated)
      refreshCalendar()
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : '暂时无法处理调整方案。')
    } finally {
      setBusyTaskId('')
    }
  }

  async function deleteFeedbackRecord(feedbackId: string) {
    if (!goalId || busyTaskId) return
    setBusyTaskId(feedbackId)
    setError('')
    try {
      setWorkspace(await deleteFeedback(goalId, feedbackId))
      refreshCalendar()
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : '暂时无法删除反馈记录。')
    } finally {
      setBusyTaskId('')
    }
  }

  async function scheduleExecutionTask(task: UnifiedToday['fixed_tasks'][number], allowParallel = false) {
    if (busyTaskId) return
    setBusyTaskId('SCHEDULE')
    setError('')
    try {
      await scheduleTodayTask({ source: task.source, task_id: task.id, scheduled_date: task.scheduled_date, start_local: scheduleStart, end_local: scheduleEnd, allow_parallel: allowParallel, parallel_reason: allowParallel ? parallelReason : null })
      setUnifiedToday(await getUnifiedToday(task.scheduled_date))
      setScheduleTarget(null)
      setScheduleConflict(false)
      setParallelReason('')
    } catch (caught) {
      if ((caught as Error & { code?: string }).code === 'SCHEDULE_CONFLICT') setScheduleConflict(true)
      else setError(caught instanceof Error ? caught.message : '暂时无法安排时间。')
    } finally {
      setBusyTaskId('')
    }
  }

  async function scheduleEntirePlan() {
    if (!goalId || planScheduleBusy) return
    setPlanScheduleBusy(true)
    setError('')
    try {
      const result = await allocateEntirePlan(goalId)
      setPlanScheduleResult(result)
      setUnifiedToday(await getUnifiedToday(workspace?.today))
      setScheduleFoundation(await getScheduleFoundation(goalId))
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : '暂时无法安排整个规划。')
      setPlanScheduleOpen(false)
    } finally {
      setPlanScheduleBusy(false)
    }
  }

  function freeTimeWindows(target: UnifiedToday['fixed_tasks'][number]) {
    if (!scheduleFoundation || !unifiedToday) return []
    const toMinutes = (value: string) => Number(value.slice(0, 2)) * 60 + Number(value.slice(3, 5))
    const toTime = (value: number) => `${String(Math.floor(value / 60)).padStart(2, '0')}:${String(value % 60).padStart(2, '0')}`
    const weekday = (new Date(`${target.scheduled_date}T12:00:00`).getDay() + 6) % 7
    const exception = scheduleFoundation.availability_exceptions.find((item) => item.exception_date === target.scheduled_date)
    if (exception?.kind === 'UNAVAILABLE') return []
    const sources = exception?.kind === 'OVERRIDE' && exception.start_local && exception.end_local
      ? [{ start_local: exception.start_local, end_local: exception.end_local }]
      : scheduleFoundation.availability_windows.filter((item) => item.weekday === weekday && (!item.valid_from || item.valid_from <= target.scheduled_date) && (!item.valid_to || item.valid_to >= target.scheduled_date))
    const occupied = unifiedToday.fixed_tasks.filter((item) => item.id !== target.id && item.start_local && item.end_local).map((item) => [toMinutes(item.start_local!), toMinutes(item.end_local!)] as const).sort((a, b) => a[0] - b[0])
    return sources.flatMap((source) => {
      let cursor = toMinutes(source.start_local)
      const end = toMinutes(source.end_local)
      const free: Array<{ start: string; end: string }> = []
      occupied.forEach(([busyStart, busyEnd]) => {
        if (busyEnd <= cursor || busyStart >= end) return
        if (busyStart > cursor) free.push({ start: toTime(cursor), end: toTime(Math.min(busyStart, end)) })
        cursor = Math.max(cursor, busyEnd)
      })
      if (cursor < end) free.push({ start: toTime(cursor), end: toTime(end) })
      return free.filter((item) => toMinutes(item.end) - toMinutes(item.start) >= 15)
    })
  }

  if (!workspace && !error) return <StageTransition executing current={3} title="正在读取今天的任务" detail="Solevia 正在查看今天该做什么，马上就好。" checkpoints={['查看今天是否是执行日', '找到今天的主目标', '准备任务清单']} />
  if (!workspace) return <section className="page narrow error-state"><h1>暂时无法打开今日计划</h1><p>{error}</p><button className="button" type="button" onClick={() => navigate(`/goals/${goalId}/progress`)}>查看已保存的执行进度</button></section>

  const day = workspace.day
  const actualToday = dateInputValue()
  const viewingToday = workspace.today === actualToday
  const canEditToday = viewingToday && workspace.status === 'ACTIVE'
  const latestDayFeedback = workspace.feedbacks?.filter((item) => item.feedback_kind === 'DAY_PACE').at(-1)
  const latestPastExecutionDate = calendar?.days
    .filter((item) => item.scheduled_date < actualToday)
    .at(-1)?.scheduled_date
  const inactivity = previewInactivity(workspace.inactivity, location.search, latestPastExecutionDate)
  const recoveryPreviewDate = import.meta.env.DEV
    ? new URLSearchParams(location.search).get('preview_recovery_date') ?? undefined
    : undefined
  const scheduledAgentTasks = new Map(
    [...(unifiedToday?.fixed_tasks ?? []), ...(unifiedToday?.flexible_tasks ?? [])]
      .filter((item) => item.source === 'AGENT')
      .map((item) => [item.id, item]),
  )
  const orderedDayTasks = [...(day?.tasks ?? [])].sort((left, right) => {
    const leftStart = scheduledAgentTasks.get(left.id)?.start_local
    const rightStart = scheduledAgentTasks.get(right.id)?.start_local
    if (leftStart && rightStart && leftStart !== rightStart) return leftStart.localeCompare(rightStart)
    if (leftStart && !rightStart) return -1
    if (!leftStart && rightStart) return 1
    return left.order - right.order
  })
  const taskTitles = new Map([
    ...(calendar?.days.flatMap((item) => item.tasks.map((task) => [task.id, task.title] as const)) ?? []),
    ...orderedDayTasks.map((task) => [task.id, task.title] as const),
  ])
  function renderTask(task: NonNullable<TodayWorkspace['day']>['tasks'][number]) {
    const position = orderedDayTasks.findIndex((item) => item.id === task.id) + 1
    return <ExecutionTaskCard key={task.id} task={task} position={position} tone={(position - 1) % 4}
      taskTitles={taskTitles} latestFeedback={workspace?.feedbacks?.filter((item) => item.task_id === task.id).at(-1)}
      scheduleTask={scheduledAgentTasks.get(task.id)} viewingToday={viewingToday} canEditToday={canEditToday} busyTaskId={busyTaskId}
      onSchedule={(item) => { setScheduleTarget(item); setScheduleStart(item.start_local ?? '19:00'); setScheduleEnd(item.end_local ?? '19:30'); setScheduleConflict(false) }}
      onUpdate={(id, action) => { void updateTask(id, action) }} onProgress={setTaskProgressTarget} onFeedback={setFeedbackTarget}
      onReschedule={() => openFlexibleReschedule({ id: task.id, title: task.title, scheduled_date: workspace?.today ?? actualToday, estimate_minutes: task.estimate_minutes })} />
  }

  return (
    <>
      <ModalLayer>
        {goalId && taskProgressTarget && <TaskProgressDialog key={`${goalId}:${taskProgressTarget.id}:${taskProgressTarget.action}`} goalId={goalId} target={taskProgressTarget} disabled={Boolean(busyTaskId)} onClose={() => setTaskProgressTarget(null)} onSaved={taskProgressSaved} />}
        {goalId && flexibleDecisionTarget && <FlexibleTaskDecisionDialog key={`${goalId}:${flexibleDecisionTarget.id}:${flexibleDecisionTarget.action}`} goalId={goalId} target={flexibleDecisionTarget} today={actualToday} disabled={Boolean(busyTaskId)} onClose={() => setFlexibleDecisionTarget(null)} onApplied={flexibleDecisionApplied} />}
        {dayMoveOpen && goalId && calendar && <ExecutionDayMoveDialog goalId={goalId} today={workspace.today} calendar={calendar} onClose={() => setDayMoveOpen(false)} onApplied={(result) => { setWorkspace(result); refreshCalendar(); void getUnifiedToday().then(setUnifiedToday).catch(() => undefined) }} />}
        {carryForwardOpen && <DialogBackdrop className="inline-dialog-backdrop" onDismiss={() => setCarryForwardOpen(false)}> <form className="inline-dialog carry-forward-dialog" role="dialog" aria-modal="true" onSubmit={(event) => { event.preventDefault(); void carryForward() }}><header><div><p className="eyebrow">提前承接 · 下一执行日</p><h2>选择今天想提前完成的任务</h2><p>先选择具体任务；只有可以自然拆分的任务才能选择“先做一部分”。</p></div><button className="icon-button" type="button" aria-label="关闭" onClick={() => setCarryForwardOpen(false)}>×</button></header><div className="carry-forward-task-list">{(workspace.carry_forward_tasks ?? []).map((item) => <button className={`carry-forward-task-card${carryForwardTaskId === item.task_id ? ' selected' : ''}`} type="button" key={item.task_id} disabled={!item.can_carry} onClick={() => { setCarryForwardTaskId(item.task_id); setCarryForwardMode('FULL'); setCarryForwardMinutes(Math.min(30, item.estimate_minutes)) }}><span><strong>{item.title}</strong><small>{item.scheduled_date} · {executionDayLabel(item.day_code)} · {item.estimate_minutes} 分钟</small></span><p>{item.description}</p>{!item.can_carry && <em>{item.blocked_reason}</em>}{item.can_carry && !item.can_split && <em>{item.split_reason ?? '必须完整执行'}</em>}</button>)}{(workspace.carry_forward_tasks ?? []).length === 0 && <p className="muted">下一执行日暂时没有可承接任务。</p>}</div>{carryForwardTaskId && (() => { const selected = workspace.carry_forward_tasks.find((item) => item.task_id === carryForwardTaskId); if (!selected) return null; return <div className="carry-forward-choice"><label><input type="radio" name="carry-mode" checked={carryForwardMode === 'FULL'} onChange={() => setCarryForwardMode('FULL')} />完整执行这项任务</label>{selected.can_split && <label><input type="radio" name="carry-mode" checked={carryForwardMode === 'PARTIAL'} onChange={() => { setCarryForwardMode('PARTIAL'); setCarryForwardMinutes(Math.min(30, selected.estimate_minutes - 1)) }} />先做一部分</label>}{carryForwardMode === 'PARTIAL' && selected.can_split && <label className="carry-forward-minutes">本次执行时长<div className="input-suffix"><input type="number" min="1" max={selected.estimate_minutes - 1} value={carryForwardMinutes} onChange={(event) => setCarryForwardMinutes(Number(event.target.value))} /><span>分钟</span></div><small>剩余 {Math.max(0, selected.estimate_minutes - carryForwardMinutes)} 分钟仍留在原执行日。</small></label>}</div> })()}<div className="inline-dialog-actions"><button className="button" type="button" onClick={() => setCarryForwardOpen(false)}>取消</button><button className="button primary" type="submit" disabled={busyTaskId === 'CARRY_FORWARD' || !carryForwardTaskId || (carryForwardMode === 'PARTIAL' && carryForwardMinutes < 1)}>{busyTaskId === 'CARRY_FORWARD' ? '正在承接…' : '加入今天'}</button></div></form></DialogBackdrop>}
        {planScheduleOpen && <DialogBackdrop className="inline-dialog-backdrop" onDismiss={() => { setPlanScheduleOpen(false); setPlanScheduleResult(null) }}> <section className="inline-dialog plan-schedule-dialog" role="dialog" aria-modal="true" aria-labelledby="plan-schedule-title"><header><div><p className="eyebrow">安排整个规划</p><h2 id="plan-schedule-title">为后续每个执行日生成时间安排</h2></div><button className="icon-button" type="button" aria-label="关闭" disabled={planScheduleBusy} onClick={() => { setPlanScheduleOpen(false); setPlanScheduleResult(null) }}>×</button></header>{planScheduleResult ? <div className="plan-schedule-result"><strong>整个规划的时间已经更新</strong><p>处理 {planScheduleResult.scheduled_days} 个执行日，安排 {planScheduleResult.assigned_tasks} 项任务。</p>{planScheduleResult.unallocated_tasks > 0 && <p>{planScheduleResult.unallocated_tasks} 项暂时没有完整空闲时段，已保留为自由任务。</p>}</div> : <><p>Solevia 会按天读取这个规划的任务、可用时间和其他规划占用，再并行生成每天的时间安排。已有固定任务优先保留。</p><p className="muted">确认后，这个规划以后调整或中断恢复时也会继续沿用自动排时策略。</p></>}<OperationProgress active={planScheduleBusy} scope={goalId} label="正在按执行日生成并保存时间安排" error={error} /><div className="inline-dialog-actions"><button className="button" type="button" disabled={planScheduleBusy} onClick={() => { setPlanScheduleOpen(false); setPlanScheduleResult(null) }}>{planScheduleResult ? '完成' : '取消'}</button>{!planScheduleResult && <button className="button primary" type="button" disabled={planScheduleBusy} onClick={() => void scheduleEntirePlan()}>{planScheduleBusy ? '正在逐日安排…' : '确认并安排整个规划'}</button>}</div></section></DialogBackdrop>}
        {scheduleTarget && <DialogBackdrop className="inline-dialog-backdrop" onDismiss={() => setScheduleTarget(null)}> <form className="inline-dialog schedule-dialog" role="dialog" aria-modal="true" onSubmit={(event) => { event.preventDefault(); void scheduleExecutionTask(scheduleTarget, scheduleConflict) }}><header><div><p className="eyebrow">{scheduleTarget.is_fixed ? '调整当日时间' : '为这项任务安排时间'}</p><h2>{scheduleTarget.title}</h2><p>{scheduleTarget.estimate_minutes} 分钟 · 将与所有规划的今日安排共同检查</p></div><button className="icon-button" type="button" aria-label="关闭" onClick={() => setScheduleTarget(null)}>×</button></header>{freeTimeWindows(scheduleTarget).length > 0 && <div className="free-time-suggestions"><strong>当天空闲时间</strong><div>{freeTimeWindows(scheduleTarget).map((item) => <button type="button" key={`${item.start}-${item.end}`} onClick={() => { setScheduleStart(item.start); setScheduleEnd(item.end); setScheduleConflict(false) }}>{item.start}–{item.end}</button>)}</div></div>}<div className="schedule-fields"><label>开始时间<TimePicker value={scheduleStart} onChange={(event) => { setScheduleStart(event.target.value); setScheduleConflict(false) }} required /></label><label>结束时间<TimePicker value={scheduleEnd} onChange={(event) => { setScheduleEnd(event.target.value); setScheduleConflict(false) }} required /></label></div>{scheduleConflict && <div className="schedule-conflict-panel"><strong>这个时段与已有任务重叠</strong><p>可以换一个时间；确实能并行时，需要说明原因后再确认。</p><label>并行原因<textarea value={parallelReason} onChange={(event) => setParallelReason(event.target.value)} placeholder="例如：已有任务主要在等待，可同时处理" required /></label></div>}<div className="inline-dialog-actions"><button className="button" type="button" onClick={() => setScheduleTarget(null)}>取消</button><button className="button primary" type="submit" disabled={busyTaskId === 'SCHEDULE' || scheduleStart >= scheduleEnd || (scheduleConflict && !parallelReason.trim())}>{busyTaskId === 'SCHEDULE' ? '正在保存…' : scheduleConflict ? '确认并行安排' : '保存时间'}</button></div></form></DialogBackdrop>}
        {feedbackTarget && <FeedbackDialog target={feedbackTarget} onClose={() => setFeedbackTarget(null)} onSubmit={saveFeedback} />}
        {goalId && recoveryOpen && <RecoveryShiftDialog goalId={goalId} anchorDate={recoveryPreviewDate} previewOnly={Boolean(recoveryPreviewDate)} onClose={() => setRecoveryOpen(false)} onApplied={refreshAfterRecovery} />}
        {goalId && pauseOpen && <PauseGoalDialog goalId={goalId} onClose={() => setPauseOpen(false)} onPaused={async () => { setPauseOpen(false); setWorkspace(await getToday(goalId)) }} />}
        {feedbackDeleteTarget && <DialogBackdrop className="inline-dialog-backdrop" onDismiss={() => setFeedbackDeleteTarget(null)}> <div className="inline-dialog" role="dialog" aria-modal="true"><h2>删除反馈记录</h2><p>确定删除“{feedbackDeleteTarget.label}”这条反馈吗？此操作无法撤销。</p><div className="inline-dialog-actions"><button className="button" type="button" onClick={() => setFeedbackDeleteTarget(null)}>取消</button><button className="button danger-button" type="button" onClick={() => { const target = feedbackDeleteTarget; setFeedbackDeleteTarget(null); void deleteFeedbackRecord(target.id) }}>确认删除</button></div></div></DialogBackdrop>}
      </ModalLayer>
      <section className={`page planner-stage-page today-page${todayToolsOpen ? '' : ' today-tools-collapsed'}`}>
        <div className="planner-primary-column">
          <header className="planner-stage-header execution-header"><div className="stage-heading"><p className="eyebrow">{workspace.status === 'PAUSED' ? '执行已暂停' : workspace.schedule_type === 'OUTCOME' ? '当前成果窗口' : viewingToday ? '正在执行' : '日志回看'}</p><h1>{executionHeading(workspace, viewingToday)}</h1></div><div className="execution-header-actions">
            {canEditToday && calendar && <button className="button execution-day-move-entry" type="button" disabled={Boolean(busyTaskId)} onClick={() => setDayMoveOpen(true)}><CalendarClock size={18} />调整执行日</button>}
{canEditToday && workspace.schedule_type !== 'OUTCOME' && <button className="button" type="button" onClick={() => { setPlanScheduleResult(null); setPlanScheduleOpen(true) }}>安排整个规划</button>}{!viewingToday && <button className="button return-today" type="button" disabled={busyTaskId === 'DATE'} onClick={() => void selectCalendarDate(actualToday)}>回到今天</button>}{workspace.status === 'PAUSED' ? <button className="button primary" type="button" disabled={Boolean(busyTaskId)} onClick={() => void resumeExecution()}>{busyTaskId === 'RESUME' ? '正在恢复…' : '立即恢复'}</button> : <button className="button" type="button" onClick={() => setPauseOpen(true)}>暂停</button>}</div></header>
        <div className="stage-main-scroll"><div className="today-main">
          {day && <TodayBrief goal={day.primary_goal} minutes={day.suggested_minutes} date={friendlyDate(workspace.today)} milestone={workspace.milestone_title} outcome={workspace.schedule_type === 'OUTCOME'} />}
          {goalId && viewingToday && <ExplorationContinuation goalId={goalId} state={workspace.exploration} />}
          {viewingToday && flexibleArrangeNotice && <p className="flexible-arrange-notice" role="status">{flexibleArrangeNotice}</p>}
          {workspace.status === 'PAUSED' && <section className="paused-notice"><div><p className="eyebrow">计划已暂停</p><h2>这段时间不会累积任务</h2><p>你可以随时恢复；未完成安排会从恢复当天重新接续。</p></div><button className="button primary" type="button" disabled={Boolean(busyTaskId)} onClick={() => void resumeExecution()}>{busyTaskId === 'RESUME' ? '正在恢复…' : '现在恢复'}</button></section>}
          <ExecutionNoticeBar key={goalId} workspace={workspace} viewingToday={viewingToday} inactivity={inactivity}
            reminderBusyLabel={reminderBusyLabel} reminderEventBusyId={reminderEventBusyId}
            onRecovery={() => navigate(`/goals/${goalId}/progress?recovery=1`)}
            onDiscuss={(label) => navigate(`/goals/${goalId}/progress?discussion=1&commitment=${encodeURIComponent(label)}`)}
            onAcknowledge={(id, action) => { void acknowledgeReminder(id, action) }} onStopReminder={(label) => { void stopCommitmentReminder(label) }} />
          {!day ? <section className="rest-day-card"><p className="eyebrow">{friendlyDate(workspace.today)}</p><h2>今天没有安排必做任务</h2><p>{workspace.next_execution_date ? `下一个执行日是 ${friendlyDate(workspace.next_execution_date)}。` : '当前计划的执行日已经全部走完。'} 今天想继续时，可以从下一执行日选择具体任务。</p>{canEditToday && workspace.carry_forward_tasks?.length > 0 && <button className="button primary" type="button" onClick={openCarryForward}>今天不休息了，选择后续任务</button>}</section> : <>
            <div className="task-groups">
              <section><h2>{workspace.schedule_type === 'OUTCOME' ? '本窗口需要完成' : '今天需要完成'}</h2><div className="task-list">{orderedDayTasks.filter((task) => task.is_required).map(renderTask)}</div></section>
              {orderedDayTasks.some((task) => !task.is_required) && <section className="optional-tasks"><h2>有余力再做</h2><div className="task-list">{orderedDayTasks.filter((task) => !task.is_required).map(renderTask)}</div></section>}
            </div>
            {day.evidence.length > 0 && <div className="evidence"><strong>{workspace.schedule_type === 'OUTCOME' ? '本窗口的产出或收获：' : '今天可能获得的产出或收获：'}</strong>{day.evidence.join('；')}</div>}
            {day.do_not.length > 0 && <div className="do-not"><strong>今天暂时不要做：</strong>{day.do_not.join('；')}</div>}
            {workspace.day_completed && <div className="day-complete-card"><strong>今天已经完成</strong><p>所有子任务完成后已自动记录，不需要再次确认。</p></div>}
            {canEditToday && workspace.day_completed && workspace.carry_forward_tasks?.length > 0 && <div className="carry-forward-entry"><div><strong>今天还有余力？</strong><p>查看下一执行日的任务卡，选择完整完成或只做可拆分任务的一部分。</p></div><button className="button" type="button" onClick={openCarryForward}>选择后续任务</button></div>}
            {canEditToday && <section className="day-pace-card compact-feedback"><div><p className="eyebrow">轻量反馈</p><h2>今天整体感觉如何？</h2></div><div className="day-pace-actions">{dayPaceChoices.map((choice) => <button type="button" key={choice.code} onClick={() => setFeedbackTarget({ kind: 'DAY', title: choice.label, reason: choice.code })}>{choice.label}</button>)}</div>{latestDayFeedback && <div className="day-feedback-saved">最近反馈：{feedbackLabel(latestDayFeedback.reason_code)} · {latestDayFeedback.intent_code ? intentLabels[latestDayFeedback.intent_code] : '已记录'}</div>}</section>}
            {workspace.feedbacks?.length > 0 && <details className="feedback-history compact-feedback-history"><summary><span><strong>反馈与处理</strong><small>{workspace.feedbacks.length} 条记录，点击展开</small></span><i aria-hidden="true">⌄</i></summary><div className="feedback-history-content">{workspace.feedbacks.map((item) => {
              const candidate = workspace.adjustments?.find((adjustment) => adjustment.feedback_id === item.id)
              const result = candidate
                ? candidate.status === 'PENDING_REVIEW' ? '已生成调整方案，请查看下方' : candidate.status === 'APPLIED' ? '对应方案已经应用' : '对应方案已放弃，保留原计划'
                : feedbackStatusLabels[item.status] ?? item.status
              const label = feedbackLabel(item.reason_code)
              return <article key={item.id}><header><strong>{label}</strong><span>{item.intent_code ? intentLabels[item.intent_code] : '暂时只记录'}</span><button className="feedback-delete" type="button" aria-label={`删除反馈：${label}`} disabled={Boolean(busyTaskId)} onClick={() => setFeedbackDeleteTarget({ id: item.id, label })}>删除</button></header>{item.detail_text && <p>{item.detail_text}</p>}<small>{result}</small>{item.detail_text && <button className="feedback-escalate" type="button" disabled={Boolean(busyTaskId)} onClick={() => void analyzeAsPlanAdjustment(item.id)}>{busyTaskId === item.id ? `${ASSISTANT_NAME} 正在整理建议…` : '看看阶段或整体怎么调'}</button>}</article>
            })}</div></details>}
            {planProposal && <section className="adjustment-proposal-card execution-plan-proposal" aria-label="阶段或整体调整建议">
              <header><div><span>{ASSISTANT_NAME} 的调整建议</span><strong>接下来的安排可以这样调整</strong></div><em>等你确认</em></header>
              <AssistantAnswer content={planProposal.assistant_message} />
              <div className="adjustment-scope"><strong>准备影响</strong>{planProposal.affected_stage_orders.map((order) => <span key={`today-M${order}`}>M{order}</span>)}{planProposal.affected_weeks.map((week) => <span key={`today-W${week}`}>W{week}</span>)}{planProposal.downstream_affected_weeks.map((week) => <span className="downstream" key={`today-downstream-${week}`}>承接 W{week}</span>)}</div>
              <section><strong>准备怎样调整</strong><ul>{planProposal.proposed_changes.map((item) => <li key={item}>{item}</li>)}</ul></section>
              {planProposal.tradeoffs.length > 0 && <section><strong>需要接受的取舍</strong><ul>{planProposal.tradeoffs.map((item) => <li key={item}>{item}</li>)}</ul></section>}
              <section><strong>不会改变</strong><ul><li>已经完成的内容会完整保留。</li>{planProposal.unchanged_guarantees.map((item) => <li key={item}>{item}</li>)}</ul></section>
              {planProposal.clarifying_question && <p className="proposal-question">{planProposal.clarifying_question}</p>}
              <label htmlFor="today-plan-followup">还想补充什么？（可选）</label>
              <textarea id="today-plan-followup" value={proposalFollowup} onChange={(event) => setProposalFollowup(event.target.value)} placeholder="可以补充希望保留的内容、可接受的取舍或更具体的时间要求。" />
              <footer><button className="button" type="button" disabled={busyTaskId === 'PLAN_DECISION'} onClick={() => void decidePlanProposal('DISCARD')}>保留当前计划</button><button className="button" type="button" disabled={!proposalFollowup.trim() || Boolean(busyTaskId)} onClick={() => void continuePlanProposal()}>发送补充说明</button><button className="button primary" type="button" disabled={Boolean(busyTaskId) || Boolean(proposalFollowup.trim())} onClick={() => void decidePlanProposal('APPLY')}>{planProposal.can_apply ? '查看预览并确认' : '继续处理调整建议'}</button></footer>
            </section>}
            {planAdjustmentResult && <div className="plan-adjustment-result"><strong>计划处理完成</strong><p>{planAdjustmentResult}</p></div>}
            {workspace.adjustments?.filter((item) => item.status === 'PENDING_REVIEW').map((item) => <section className="adjustment-candidate" key={item.id}>
              <div><p className="eyebrow">{ASSISTANT_NAME} 的建议 · 等你确认</p><h2>{item.title}</h2><p>{item.summary}</p></div>
              <div className="adjustment-reason"><strong>为什么这样调</strong><span>{item.rationale}</span></div>
              <ul>{item.impact.map((impact) => <li key={impact}>{impact}</li>)}</ul>
              <footer><button className="button" type="button" disabled={busyTaskId === item.id} onClick={() => void decideAdjustment(item.id, 'DISCARD')}>保留原计划</button><button className="button primary" type="button" disabled={busyTaskId === item.id} onClick={() => void decideAdjustment(item.id, 'APPLY')}>{busyTaskId === item.id ? '正在应用…' : '应用这次局部调整'}</button></footer>
            </section>)}
            {error && <p className="form-error" role="alert">{error}</p>}
          </>}
        </div></div>
      </div>
        <aside className="stage-context-column today-context" aria-label="每日工具">
          {!todayToolsOpen && <button className="today-context-mini-progress" type="button" aria-label="查看简要进度与调整" onClick={() => navigate(`/goals/${goalId}/progress`)}><span><small>今日</small><strong>{workspace.day_progress.percentage}%</strong></span><span><small>阶段</small><strong>{workspace.stage_progress.percentage}%</strong></span><span><small>{workspace.exploration?.next_unplanned_week ? '已展开' : '整体'}</small><strong>{workspace.overall_progress.percentage}%</strong></span></button>}
          <button className="today-context-toggle" type="button" aria-label={todayToolsOpen ? '收起每日工具' : '展开每日工具'} aria-expanded={todayToolsOpen} onClick={toggleTodayTools}>
            <span aria-hidden="true">{todayToolsOpen ? '›' : '‹'}</span>
            <strong>{todayToolsOpen ? '收起辅助栏' : '辅助工具'}</strong>
            {!todayToolsOpen && <small>进度 · 日历 · 专注</small>}
          </button>
          <div className="today-context-content">
            <button className="compact-progress progress-entry" type="button" aria-label="查看进度与调整" onClick={() => navigate(`/goals/${goalId}/progress`)}><span>今日 <strong>{workspace.day_progress.percentage}%</strong></span><span>阶段 <strong>{workspace.stage_progress.percentage}%</strong></span><span>{workspace.exploration?.next_unplanned_week ? '已展开任务' : '整体'} <strong>{workspace.overall_progress.percentage}%</strong></span><em>查看进度与调整 →</em></button>
            <div className="today-tools-scroll">
              {goalId && <ExecutionObligations goalId={goalId} workspace={workspace} onActivated={(result) => { setWorkspace(result); refreshCalendar(); void getUnifiedToday().then(setUnifiedToday).catch(() => undefined) }} />}
              <section className="fixed-calendar-tool"><header><span aria-hidden="true">▦</span><div><strong>执行日历</strong><small>固定日期与建议执行日分开标记</small></div></header><ExecutionCalendarWidget key={workspace.today.slice(0, 7)} calendar={calendar} selectedDate={workspace.today} onSelectDate={(value) => void selectCalendarDate(value)} /></section>
              {goalId && workspace.status === 'ACTIVE' && <TaskRecommendations goalId={goalId} refreshKey={calendar} disabled={Boolean(busyTaskId)} onViewDate={(value) => void selectCalendarDate(value)} onArrange={(task) => setFlexibleDecisionTarget({ ...task, id: task.task_id, action: 'ARRANGE_TODAY' })} onReschedule={(task) => openFlexibleReschedule({ ...task, id: task.task_id })} />}
              <CollapsibleTool icon="◷" title="番茄钟" summary="专注计时与实际用时"><FocusTimerWidget tasks={day?.tasks ?? []} sessions={focusSessions} onSave={saveFocusSession} /></CollapsibleTool>
              <CollapsibleTool icon="＋" title="点子备忘" summary={`${quickCaptures.length} 条与目标有关的想法`}><QuickCaptureWidget captures={quickCaptures} onAdd={addQuickCapture} onDelete={removeQuickCapture} /></CollapsibleTool>
              <CollapsibleTool icon="⌁" title="今日洞察" summary="余量与估时校准"><ExecutionInsightWidget workspace={workspace} sessions={focusSessions} /></CollapsibleTool>
            </div>
          </div>
        </aside>
      </section>
    </>
  )
}
