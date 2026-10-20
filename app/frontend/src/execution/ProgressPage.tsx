import './ExecutionRefinement.css'
import './ExecutionIdentity.css'
import { ProgressCompletion } from './ProgressCompletion'
import { useEffect, useRef, useState } from 'react'
import { useLocation, useNavigate, useParams } from 'react-router-dom'
import { applyPlanAdjustment, deferRecovery, discardPlanAdjustment, getExecutionProgress, getGenerationBatches, getPlan, previewPlanAdjustment, reviseRecoveryPlan, revisePlan, retryGenerationBatch, type ExecutionProgress, type PlanAdjustmentProposal, type PlanAdjustmentPreview, type RecoveryCatchupPreview, type GenerationBatch } from '../api'
import { AssistantAnswer } from '../ui/AssistantAnswer'
import { OperationProgress } from '../ui/ProcessDetails'
import { StageTransition } from '../ui/PlannerStage'
import { previewInactivity } from './recoveryPreview'
import { InactivityNotice, RecoveryPlanChoiceDialog, RetrospectiveCompletionDialog, RecoveryShiftDialog, RecoveryCatchupDialog } from './RecoveryControls'
import { ASSISTANT_NAME, friendlyDate } from '../ui/plannerPresentation'
import { feedbackLabel } from './feedbackOptions'
import { decideRecovery, type PlanWorkspace } from '../api'
import { RecoveryDecisions } from '../ui/RecoveryDecisions'
import { ExplorationContinuation } from './ExplorationContinuation'

export function RecoveryBatchResult({ batch }: { batch: GenerationBatch }) {
  const completed = batch.status === 'COMPLETED' && Boolean(batch.staged_result)
  const result = batch.staged_result ?? {}
  const days = Array.isArray(result.replacement_days) ? result.replacement_days : []
  const status = completed ? '已生成，可展开查看' : batch.status === 'FAILED' ? '生成失败' : batch.status === 'RUNNING' ? '生成中' : '等待生成'
  if (!completed) return <div className={`recovery-week ${batch.status.toLowerCase()}`}><span>第 {String(batch.scope.week ?? '—')} 周</span><span className="recovery-week-status">{status}</span></div>
  return <details className="recovery-week completed"><summary><span>第 {String(batch.scope.week ?? '—')} 周</span><span className="recovery-week-status">{status}<i aria-hidden="true">⌄</i></span></summary><div className="recovery-week-content"><p>{typeof result.summary === 'string' ? result.summary : '本周调整已生成，请核对下方任务。'}</p>{days.map((raw, index) => { const day = raw as Record<string, unknown>; const tasks = Array.isArray(day.tasks) ? day.tasks : []; return <section key={`${String(day.week_index ?? '')}-${String(day.day_index ?? index)}`}><header><strong>{day.scheduled_date ? String(day.scheduled_date) : `第 ${String(day.day_index ?? index + 1)} 个执行日`}</strong><small>{day.suggested_minutes ? `${String(day.suggested_minutes)} 分钟` : ''}</small></header><ul>{tasks.map((task, taskIndex) => { const item = task as Record<string, unknown>; return <li key={taskIndex}><span>{String(item.title ?? item.description ?? '未命名任务')}</span>{Boolean(item.estimate_minutes) && <small>{String(item.estimate_minutes)} 分钟</small>}</li> })}</ul></section> })}</div></details>
}

export function ProgressPage() {
  const { goalId } = useParams()
  const location = useLocation()
  const navigate = useNavigate()
  const [progress, setProgress] = useState<ExecutionProgress | null>(null)
  const [planProposal, setPlanProposal] = useState<PlanAdjustmentProposal | null>(null)
  const [recoveryReview, setRecoveryReview] = useState<PlanWorkspace['recovery_review']>(null)
  const [proposalLoaded, setProposalLoaded] = useState(false)
  const [planAdjustmentPreview, setPlanAdjustmentPreview] = useState<PlanAdjustmentPreview | null>(null)
  const [editedAdjustmentSchedule, setEditedAdjustmentSchedule] = useState<PlanAdjustmentPreview['schedule']>([])
  const [acceptAdjustmentUnallocated, setAcceptAdjustmentUnallocated] = useState(false)
  const [recoveryBatches, setRecoveryBatches] = useState<GenerationBatch[]>([])
  const [analyzingTrend, setAnalyzingTrend] = useState(false)
  const [error, setError] = useState('')
  const [loadingSlow, setLoadingSlow] = useState(false)
  const [loadingTimedOut, setLoadingTimedOut] = useState(false)
  const [reloadKey, setReloadKey] = useState(0)
  const [recoveryMode, setRecoveryMode] = useState<'SELECT' | 'RECORD_COMPLETED' | 'SHIFT' | 'CATCHUP' | null>(null)
  const [retrospectiveResult, setRetrospectiveResult] = useState('')
  const recoverySectionRef = useRef<HTMLDivElement | null>(null)
  const activeGoalRef = useRef(goalId)
  useEffect(() => {
    activeGoalRef.current = goalId
    return () => { activeGoalRef.current = undefined }
  }, [goalId])

  useEffect(() => {
    if (!goalId) return
    let active = true
    setProgress(null)
    setPlanProposal(null)
    setRecoveryReview(null)
    setPlanAdjustmentPreview(null)
    setEditedAdjustmentSchedule([])
    setRecoveryMode(null)
    setProposalLoaded(false)
    setError('')
    setLoadingSlow(false)
    setLoadingTimedOut(false)
    const slowTimer = window.setTimeout(() => {
      if (active) setLoadingSlow(true)
    }, 8000)
    const timeoutTimer = window.setTimeout(() => {
      if (active) setLoadingTimedOut(true)
    }, 30000)
    getExecutionProgress(goalId).then((result) => {
      if (active) {
        setProgress(result)
        setLoadingSlow(false)
        setLoadingTimedOut(false)
        setError('')
        // Progress is the first-paint data. The full plan and compiler batches
        // can be large, so load them after the page is usable instead of
        // competing with the progress query during initial recovery.
        void getPlan(goalId).then((planResult) => {
          if (active) {
            setPlanProposal(planResult.pending_adjustment)
            setRecoveryReview(planResult.recovery_review)
            setPlanAdjustmentPreview(planResult.pending_adjustment_preview ?? null)
            setEditedAdjustmentSchedule(planResult.pending_adjustment_preview?.schedule ?? [])
            setAcceptAdjustmentUnallocated(false)
            setProposalLoaded(true)
            if (new URLSearchParams(location.search).get('recovery') === '1' && !planResult.pending_adjustment && result.inactivity?.missed_execution_days) {
              setRecoveryMode('SELECT')
            }
          }
        }).catch(() => {
          if (active) setError('暂时无法读取已保存的调整建议，请重新读取后继续。')
        })
        void getGenerationBatches(goalId).then((batchesResult) => {
          if (active) {
            setRecoveryBatches(batchesResult.filter((item) => item.scope_type === 'RECOVERY_WEEK'))
          }
        }).catch(() => undefined)
      }
    }).catch((caught) => {
      if (active) setError(caught instanceof Error ? caught.message : '暂时无法读取进度。')
    })
    return () => {
      active = false
      window.clearTimeout(slowTimer)
      window.clearTimeout(timeoutTimer)
    }
  }, [goalId, location.search, reloadKey])

  useEffect(() => {
    if (!goalId || !recoveryBatches.some((item) => item.status === 'QUEUED' || item.status === 'RUNNING')) return
    const timer = window.setInterval(() => {
      void getGenerationBatches(goalId).then((items) => {
        setRecoveryBatches(items.filter((item) => item.scope_type === 'RECOVERY_WEEK'))
      }).catch(() => undefined)
    }, 2000)
    return () => window.clearInterval(timer)
  }, [goalId, recoveryBatches])

  async function analyzeTrend() {
    if (!goalId || !progress?.trend.recommendation_request || analyzingTrend) return
    setAnalyzingTrend(true)
    setError('')
    try {
      const result = await revisePlan(goalId, progress.trend.recommendation_request)
      setPlanProposal(result.pending_adjustment)
      setPlanAdjustmentPreview(null)
      setAcceptAdjustmentUnallocated(false)
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : '最近的进展已经保留，但暂时无法整理调整建议。')
    } finally {
      setAnalyzingTrend(false)
    }
  }

  function continueRecovery() {
    if (!proposalLoaded) return
    if (planProposal) navigate(`/goals/${goalId}/plan?recovery=1&discussion=1&adjustment=1`)
    else setRecoveryMode('SELECT')
  }

  async function confirmRecoveryDecision(decision: 'ALLOW_EXTENSION' | 'KEEP_DEADLINE' | 'ACCEPT_LOAD') {
    if (!goalId || !recoveryReview || analyzingTrend) return
    setAnalyzingTrend(true)
    setError('')
    try {
      const result = await decideRecovery(goalId, decision, recoveryReview.token)
      setPlanProposal(result.pending_adjustment)
      setRecoveryReview(result.recovery_review)
      setPlanAdjustmentPreview(null)
      setEditedAdjustmentSchedule([])
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : '暂时无法保存选择。')
      try {
        const result = await getPlan(goalId)
        setPlanProposal(result.pending_adjustment)
        setRecoveryReview(result.recovery_review)
      } catch { /* retain the original error */ }
    } finally {
      setAnalyzingTrend(false)
    }
  }

  async function deferAndContinue() {
    if (!goalId || !progress || analyzingTrend) return
    const previewDate = import.meta.env.DEV
      ? new URLSearchParams(location.search).get('preview_recovery_date')
      : null
    if (previewDate) {
      navigate(`/goals/${goalId}/today?resume=1&preview_inactivity=3&preview_recovery_date=${previewDate}&preview_deferred=1`)
      return
    }
    setAnalyzingTrend(true)
    setError('')
    try {
      await deferRecovery(goalId, progress.inactivity?.missed_dates ?? [])
      if (activeGoalRef.current !== goalId) return
      navigate(`/goals/${goalId}/today`)
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : '暂时无法保存稍后处理状态。')
    } finally {
      setAnalyzingTrend(false)
    }
  }

  async function requestRecoveryAI(preview: RecoveryCatchupPreview) {
    if (!goalId || analyzingTrend) return
    setAnalyzingTrend(true)
    setError('')
    try {
      const result = await reviseRecoveryPlan(goalId, {
        missed_dates: preview.missed_dates,
        backlog_minutes: preview.backlog_minutes,
        original_end_date: preview.original_end_date,
        include_non_execution_days: preview.include_non_execution_days,
        preview_id: preview.id,
        instruction: '请根据所选范围和阻塞明细，讨论拆分、目标或期限取舍；确认前不要修改正式计划。',
      })
      if (activeGoalRef.current !== goalId) return
      setPlanProposal(result.pending_adjustment)
      setRecoveryMode(null)
      navigate(`/goals/${goalId}/plan?recovery=1&discussion=1&adjustment=1`)
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : '暂时无法生成剩余计划调整建议。')
      throw caught
    } finally {
      setAnalyzingTrend(false)
    }
  }

  async function decideRecoveryProposal(action: 'APPLY' | 'DISCARD') {
    if (!goalId || !planProposal || analyzingTrend) return
    setAnalyzingTrend(true)
    setError('')
    try {
      if (action === 'APPLY' && !planAdjustmentPreview) {
        const preview = await previewPlanAdjustment(goalId)
        setPlanAdjustmentPreview(preview)
        setEditedAdjustmentSchedule(preview.schedule)
        setAcceptAdjustmentUnallocated(false)
        return
      }
      const result = action === 'APPLY'
        ? await applyPlanAdjustment(
          goalId,
          planAdjustmentPreview?.id,
          acceptAdjustmentUnallocated,
          editedAdjustmentSchedule.map((item) => ({
            scheduled_date: item.date,
            title: item.title,
            start_local: item.start,
            end_local: item.end,
          })),
        )
        : await discardPlanAdjustment(goalId)
      if (activeGoalRef.current !== goalId) return
      if (action === 'APPLY' && result.pending_adjustment) throw new Error('调整尚未发布，请查看建议或重试；当前安排保持不变。')
      setPlanProposal(result.pending_adjustment)
      setRecoveryReview(result.recovery_review)
      setPlanAdjustmentPreview(null)
      setAcceptAdjustmentUnallocated(false)
      if (action === 'APPLY') navigate(`/goals/${goalId}/today`, { replace: true })
    } catch (caught) {
      // Re-read server state so stale proposals never leave the page looking inert.
      if (goalId) {
        try {
          const latest = await getPlan(goalId)
          setPlanProposal(latest.pending_adjustment)
          setRecoveryReview(latest.recovery_review)
          setPlanAdjustmentPreview(latest.pending_adjustment_preview ?? null)
          setEditedAdjustmentSchedule(latest.pending_adjustment_preview?.schedule ?? [])
          setAcceptAdjustmentUnallocated(false)
          const batches = await getGenerationBatches(goalId)
          setRecoveryBatches(batches.filter((item) => item.scope_type === 'RECOVERY_WEEK'))
        } catch { /* keep the original error visible */ }
      }
      setError(caught instanceof Error ? caught.message : '暂时无法处理调整建议。')
    } finally {
      setAnalyzingTrend(false)
    }
  }

  async function retryRecoveryWeek(batch: GenerationBatch) {
    if (!goalId || analyzingTrend) return
    setAnalyzingTrend(true)
    setError('')
    try {
      const updated = await retryGenerationBatch(goalId, batch.id)
      setRecoveryBatches((items) => items.map((item) => item.id === updated.id ? updated : item))
    } catch (caught) {
      setRecoveryBatches(await getGenerationBatches(goalId).then((items) => items.filter((item) => item.scope_type === 'RECOVERY_WEEK')))
      setError(caught instanceof Error ? caught.message : '这一周暂时无法重新生成。')
    } finally {
      setAnalyzingTrend(false)
    }
  }

  if (!progress && !error && !loadingTimedOut) return <StageTransition executing current={3} title={loadingSlow ? '正在读取最近进展' : '正在整理最近进展'} detail={loadingSlow ? '服务响应较慢，数据仍在读取中；请稍候，或稍后点击重新读取。' : '马上就好，Solevia 正在把最近的完成情况和反馈整理到一起。'} checkpoints={['查看最近完成情况', '整理你的反馈', loadingSlow ? '等待服务返回' : '准备进展建议']} />
  if (!progress) return <section className="page narrow error-state"><h1>暂时无法读取进度</h1><p>{error || '读取进度超过 30 秒，服务响应较慢。你可以重新读取，已保存的数据不会丢失。'}</p><button className="button primary" type="button" onClick={() => setReloadKey((value) => value + 1)}>重新读取</button></section>

  const trendLabels = { LEARNING: '稳步推进', STABLE: '进展稳定', NEEDS_REVIEW: '最近有些吃力', EXPECTED_RAMP: '开局顺利', AHEAD: '最近状态很好' }
  const trendSummaries = {
    LEARNING: '目前进展顺利，先保持现在的节奏。',
    STABLE: '最近的完成情况比较稳定，暂时不需要调整。',
    NEEDS_REVIEW: '最近几次执行有些吃力，可以看看接下来几天是否需要放松一点。',
    EXPECTED_RAMP: '现在还在阶段前段，进行得轻松是正常的。先保持节奏，后面的内容会逐渐加深。',
    AHEAD: '最近进行得很顺利。如果你愿意，可以学得更深入或适当加快。',
  }
  const trendRecommendation = progress.trend.status === 'NEEDS_REVIEW'
    ? '看看接下来几天怎么调'
    : '想学深一点或加快一些'
  const inactivity = previewInactivity(progress.inactivity, location.search)
  const recoveryPreviewDate = import.meta.env.DEV
    ? new URLSearchParams(location.search).get('preview_recovery_date') ?? undefined
    : undefined
  const proposalCanApply = Boolean(
    planProposal?.can_apply
    && (recoveryBatches.length === 0
      || recoveryBatches.every((item) => item.status === 'COMPLETED' && Boolean(item.staged_result))),
  )
  const previewReadyToApply = Boolean(
    proposalCanApply
    && planAdjustmentPreview
    && (!planAdjustmentPreview.unallocated_count || acceptAdjustmentUnallocated),
  )
  return (
    <section className="page progress-page">
      {goalId && <ExplorationContinuation goalId={goalId} state={progress.exploration} />}
      <header className="progress-page-header"><div><p className="eyebrow">近期进展</p><h1>进度与调整</h1><p>{progress.goal_title}</p></div><div className="progress-header-actions">{(!inactivity || inactivity.level === 'NONE' || inactivity.recovery_deferred) && <button className="button primary" type="button" onClick={() => navigate(`/goals/${goalId}/today`)}>← 返回今日任务</button>}</div></header>
      {progress.schedule_type === 'RHYTHM' && progress.rhythm ? <>
        <div className="progress-overview rhythm-overview"><article><span>当前日历位置</span><strong>{progress.current_stage_order ? `M${progress.current_stage_order}` : '—'}</strong><small>{progress.current_stage_title ?? '尚未开始'}</small></article><article><span>本周期完成</span><strong>{progress.rhythm.completed_sessions} / {progress.rhythm.target_sessions}</strong><small>还可安排 {progress.rhythm.remaining_sessions} 次</small></article><article><span>近期平均</span><strong>{progress.rhythm.average_completed_sessions}</strong><small>每周期完成次数</small></article><article><span>节奏稳定率</span><strong>{progress.rhythm.adherence_rate}%</strong><small>已观察 {progress.rhythm.observed_cycles} 个周期</small></article></div>
        <section className={`rhythm-progress-band ${progress.rhythm.pace_status.toLowerCase()}`}><div><p className="eyebrow">频率驱动 · {progress.rhythm.cycle_start} 至 {progress.rhythm.cycle_end}</p><h2>{progress.rhythm.pace_status === 'LEARNING' ? '正在建立基准' : progress.rhythm.pace_status === 'BELOW' ? '当前节奏偏低' : progress.rhythm.pace_status === 'ABOVE' ? '当前节奏高于目标' : '当前节奏可持续'}</h2></div><p>{progress.rhythm.forecast_summary}</p><span>计划 {progress.rhythm.planned_minutes} 分钟 · 已记录专注 {progress.rhythm.focus_minutes} 分钟</span></section>
      </> : <div className="progress-overview"><article><span>当前日历位置</span><strong>{progress.current_stage_order ? `M${progress.current_stage_order}` : '—'}</strong><small>{progress.current_stage_title ?? '尚未开始'}</small></article><article><span>今日进度</span><strong>{progress.day_progress.percentage}%</strong><small>{progress.day_progress.completed_tasks} / {progress.day_progress.total_tasks} 项</small></article><article><span>{progress.current_stage_order ? `M${progress.current_stage_order} 任务进度` : '阶段任务进度'}</span><strong>{progress.stage_progress.percentage}%</strong><small>{progress.stage_progress.completed_tasks} / {progress.stage_progress.total_tasks} 项</small></article><article><span>{progress.exploration?.next_unplanned_week ? '已展开任务进度' : '整体进度'}</span><strong>{progress.overall_progress.percentage}%</strong><small>{progress.overall_progress.completed_tasks} / {progress.overall_progress.total_tasks} 项</small></article></div>}
      <ProgressCompletion progress={progress} />
      <p className="muted">日历位置按日期显示；阶段任务进度按任务原属阶段统计，包含顺延到其他日期的任务。</p>
      <section className="progress-summary-band" aria-label="进度摘要"><p><strong>{progress.schedule_type === 'OUTCOME' ? '当前成果' : '阶段摘要'}</strong>{progress.stage_summary}</p><p><strong>{progress.exploration?.next_unplanned_week ? '已展开进展' : progress.schedule_type === 'CALENDAR' ? '期限进展' : progress.schedule_type === 'RHYTHM' ? '长期进展' : '目标摘要'}</strong>{progress.overall_summary}</p></section>
      {inactivity && inactivity.missed_execution_days > 0 && <InactivityNotice compact inactivity={inactivity} continueLabel={analyzingTrend ? '正在保存…' : '暂不处理'} reviewLabel={planProposal ? "继续已保存的恢复讨论" : proposalLoaded ? "调整计划" : "正在读取恢复状态…"} onContinue={() => void deferAndContinue()} onReview={continueRecovery} />}
      {retrospectiveResult && <p className="recovery-success" role="status">{retrospectiveResult}</p>}
      {(!inactivity || inactivity.missed_execution_days === 0) && <section className={`trend-card ${progress.trend.status.toLowerCase()}`}><header><div><p className="eyebrow">最近 {progress.trend.window_days} 个执行日</p><h2>{trendLabels[progress.trend.status]}</h2></div></header><p>{trendSummaries[progress.trend.status]}</p><div><span>完成情况<strong>{progress.trend.completed_days} / {progress.trend.window_days} 个执行日</strong></span><span>计划时间<strong>{progress.trend.planned_minutes} 分钟</strong></span><span>专注记录<strong>{progress.trend.focus_minutes} 分钟</strong></span></div>{progress.trend.should_offer_adjustment && !planProposal && <footer><div><strong>{trendRecommendation}</strong><small>Solevia 会先给出建议，只有你确认后才会调整。</small></div><button className="button primary" type="button" disabled={analyzingTrend} onClick={() => void analyzeTrend()}>{analyzingTrend ? '正在整理建议…' : '看看怎么调整'}</button></footer>}</section>}
      <OperationProgress active={analyzingTrend} scope={goalId} label="正在结合执行记录处理计划调整" detail={recoveryBatches.some(batch => batch.status === 'RUNNING') ? `已生成 ${recoveryBatches.filter(batch => batch.status === 'COMPLETED').length} / ${recoveryBatches.length} 周` : undefined} error={error} />
      {planProposal && <section className="trend-proposal"><header><div><p className="eyebrow">{ASSISTANT_NAME} 的调整建议</p><h2>接下来的安排可以这样调整</h2></div><span>{planAdjustmentPreview ? '等待发布' : planProposal.needs_clarification ? '需要处理下方事项' : '等你确认'}</span></header><AssistantAnswer content={planProposal.assistant_message} />{planProposal.clarifying_question && <p className="proposal-question">{planProposal.clarifying_question}</p>}<RecoveryDecisions review={recoveryReview} busy={analyzingTrend} onDecision={decision => void confirmRecoveryDecision(decision)} /><section className="proposal-comparison"><article><span>保持当前安排</span><strong>什么都不会立刻改变</strong><p>在你确认前，任务、日期和进度都会保持原样。</p></article><article><span>采用这份建议</span><strong>{planProposal.affected_stage_orders.map((item) => `M${item}`).join('、') || '保持阶段结构'} · {planProposal.affected_weeks.map((item) => `W${item}`).join('、') || '具体周次还需确认'}</strong><p>{recoveryReview?.extension_dates.length ? `顺延后结束日期：${recoveryReview.extension_dates.at(-1)}（待预览确认）` : planProposal.total_weeks_change ? `总周期变化 ${planProposal.total_weeks_change > 0 ? '+' : ''}${planProposal.total_weeks_change} 周` : '总周期暂不改变'}</p></article></section><div><strong>准备怎样调整</strong><ul>{planProposal.proposed_changes.map((item) => <li key={item}>{item}</li>)}</ul></div>{planProposal.tradeoffs.length > 0 && <div><strong>需要接受的取舍</strong><ul>{planProposal.tradeoffs.map((item) => <li key={item}>{item}</li>)}</ul></div>}{planAdjustmentPreview && <section className="adjustment-time-preview" aria-label="调整后的时间预览"><strong>调整后的时间安排</strong><small>这是试算结果，确认后才会正式替换当前计划。</small><ul>{planAdjustmentPreview.schedule.slice(0, 12).map((item, index) => <li key={`${item.date}-${item.title}-${index}`}><span>{item.date} {item.start && item.end ? `${item.start}–${item.end}` : '待安排'}</span><strong>{item.title}</strong></li>)}</ul>{planAdjustmentPreview.schedule.length > 12 && <small>还有 {planAdjustmentPreview.schedule.length - 12} 项安排未展开。</small>}{planAdjustmentPreview.unallocated_count > 0 && <label className="proposal-question"><input type="checkbox" checked={acceptAdjustmentUnallocated} onChange={(event) => setAcceptAdjustmentUnallocated(event.target.checked)} /> 有 {planAdjustmentPreview.unallocated_count} 项任务暂时没有可用时段，确认保留为待安排状态</label>}</section>}{recoveryBatches.length > 0 && <section className="recovery-batches"><header><strong>分周生成状态</strong><small>失败时只需重新生成对应周</small></header>{recoveryBatches.map((batch) => <article key={batch.id}><span>第 {String(batch.scope.week ?? '—')} 周</span><strong>{batch.status === 'COMPLETED' ? '已完成' : batch.status === 'FAILED' ? '生成失败' : batch.status === 'RUNNING' ? '生成中' : '等待生成'}</strong><small>已尝试 {batch.attempt_count} 次</small>{batch.status !== 'COMPLETED' && <button className="button" type="button" disabled={analyzingTrend} onClick={() => void retryRecoveryWeek(batch)}>仅重试本周</button>}</article>)}</section>}<footer><button className="recovery-quiet-action" type="button" disabled={analyzingTrend} onClick={() => void decideRecoveryProposal('DISCARD')}>暂不采用</button><button className="button" type="button" disabled={analyzingTrend} onClick={continueRecovery}>继续讨论 / 补充调整意见</button><button className="button primary" type="button" disabled={analyzingTrend || (planAdjustmentPreview ? !previewReadyToApply : !proposalCanApply)} onClick={() => void decideRecoveryProposal('APPLY')}>{planAdjustmentPreview ? '确认日期与时间安排' : '先预览日期与时间'}</button></footer></section>}
      {error && <div role="alert"><p className="form-error">{error}</p><button className="button" type="button" disabled={analyzingTrend} onClick={() => setReloadKey(value => value + 1)}>重新读取当前状态</button></div>}
      <div ref={recoverySectionRef} className="progress-columns">
        {planProposal && recoveryBatches.length > 0 && <section className="adjusted-plan-list"><header><h2>调整后规划</h2><small>点击每周展开查看具体任务</small></header>{recoveryBatches.map((batch) => <div key={`adjusted-${batch.id}`}><RecoveryBatchResult batch={batch} />{batch.status !== 'COMPLETED' && <button className="button" type="button" disabled={analyzingTrend} onClick={() => void retryRecoveryWeek(batch)}>仅重试本周</button>}</div>)}</section>}
        <section className="progress-section"><header><div><p className="eyebrow">近况</p><h2>最近执行日</h2></div><small>最近 7 个执行日</small></header><div className="execution-day-timeline">{progress.recent_days.length === 0 ? <p className="empty-copy">还没有可汇总的执行日。</p> : progress.recent_days.map((day) => <article key={day.scheduled_date}><time>{friendlyDate(day.scheduled_date)}</time><i className={day.status.toLowerCase()} /><div><header><strong>{day.code} · {day.primary_goal}</strong><span>{day.completed_tasks}/{day.total_tasks} 项</span></header><p>{day.carried_over_tasks ? `已将 ${day.carried_over_tasks} 项任务移至后续日期；当前日期剩余 ${day.total_tasks} 项。` : day.summary ?? `${day.stage_title} · 等待形成当日日志`}</p><small>{day.focus_minutes ? `专注 ${day.focus_minutes} 分钟 · ` : ''}{day.feedback_count ? `${day.feedback_count} 条反馈` : '没有反馈'}</small></div></article>)}</div></section>
        <section className="progress-section"><header><div><p className="eyebrow">你的反馈</p><h2>反馈与处理结果</h2></div><small>{progress.feedback_timeline.length} 条</small></header><div className="progress-feedback-list">{progress.feedback_timeline.length === 0 ? <p className="empty-copy">还没有反馈记录。</p> : progress.feedback_timeline.map((item) => <article key={item.id}><header><div><time>{friendlyDate(item.scheduled_date)}</time><strong>{feedbackLabel(item.reason_code)}</strong></div><span>{item.task_title ?? '当天整体反馈'}</span></header>{item.detail_text && <p>{item.detail_text}</p>}<div className="feedback-treatment"><strong>{item.adjustment ? item.adjustment.title : '已记录，继续观察'}</strong><span>{item.adjustment ? item.adjustment.summary : '偶尔一次不会改变整体安排；Solevia 会结合接下来的情况继续判断。'}</span><small>{item.adjustment?.status === 'APPLIED' ? '已采用' : item.adjustment?.status === 'DISMISSED' ? '保持原计划' : item.adjustment ? '等你确认' : '继续观察'}</small></div></article>)}</div></section>
      </div>
      <section className="progress-section revisions-section"><header><div><p className="eyebrow">调整记录</p><h2>之前的安排</h2></div><small>{progress.revision_history.length} 份</small></header><div className="revision-list">{progress.revision_history.map((item) => <article key={item.id}><time>{new Date(item.created_at).toLocaleDateString('zh-CN')}</time><div><header><strong>{item.status === 'CONFIRMED' ? '当前安排' : item.revision === 1 ? '最初的安排' : `第 ${item.revision} 次调整前`}</strong><span>{item.status === 'CONFIRMED' ? '正在使用' : item.status === 'SUPERSEDED' ? '已经替换' : '尚未采用'}</span></header><p>{item.feedback ?? (item.revision === 1 ? '第一次确认的时间安排' : '这次调整时保存')}</p>{item.changes.length > 0 && <ul>{item.changes.map((change) => <li key={change}>{change}</li>)}</ul>}</div></article>)}</div></section>
      {recoveryMode === 'SELECT' && <RecoveryPlanChoiceDialog onRecordCompleted={() => setRecoveryMode('RECORD_COMPLETED')} onShift={() => setRecoveryMode('SHIFT')} onKeepDeadline={() => setRecoveryMode('CATCHUP')} onClose={() => setRecoveryMode(null)} />}
      {goalId && recoveryMode === 'RECORD_COMPLETED' && <RetrospectiveCompletionDialog goalId={goalId} missedDates={inactivity?.missed_dates ?? []} onClose={() => setRecoveryMode('SELECT')} onCompleted={(updated, count) => { setProgress(updated); setRecoveryMode(null); setRetrospectiveResult(`已补记 ${count} 项任务。${updated.inactivity?.missed_execution_days ? '其余遗留内容仍可继续处理。' : '遗留状态已清除，可以继续今天的安排。'}`) }} />}
      {goalId && recoveryMode === 'SHIFT' && <RecoveryShiftDialog goalId={goalId} anchorDate={recoveryPreviewDate} previewOnly={Boolean(recoveryPreviewDate)} onClose={() => setRecoveryMode('SELECT')} onApplied={async () => {
        const result = await getExecutionProgress(goalId)
        if (activeGoalRef.current !== goalId) return
        setProgress(result)
        navigate(`/goals/${goalId}/today`, { replace: true })
      }} />}
      {goalId && recoveryMode === 'CATCHUP' && <RecoveryCatchupDialog goalId={goalId} anchorDate={recoveryPreviewDate} previewOnly={Boolean(recoveryPreviewDate)} onClose={() => setRecoveryMode('SELECT')} onApplied={async () => {
        const result = await getExecutionProgress(goalId)
        if (activeGoalRef.current !== goalId) return
        setProgress(result)
        navigate(`/goals/${goalId}/today`, { replace: true })
      }} onRequestAI={requestRecoveryAI} />}
    </section>
  )
}
