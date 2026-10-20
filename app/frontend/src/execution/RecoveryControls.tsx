import { DialogBackdrop } from '../ui/DialogBackdrop'
import { useEffect, useRef, useState } from 'react'
import type { FormEvent } from 'react'
import { RecoveryTimeReview } from '../RecoveryTimeReview'
import { applyRecoveryCatchup, applyRecoveryShift, applyRecoverySplit, completeMissedTasks, getToday, pauseGoal, previewRecoveryCatchup, previewRecoverySplit, previewRecoveryShift, type GoalLifecycle, type ExecutionInactivity, type ExecutionProgress, type RecoveryCatchupPreview, type RecoveryShiftPreview, type TodayWorkspace } from '../api'
import { friendlyDate, dateInputValue } from '../ui/plannerPresentation'
import { RecoveryScopeSelector } from './RecoveryScopeSelector'
import { OperationProgress } from '../ui/ProcessDetails'

const adjustmentReasonLabels: Record<string, string> = {
  MISSED_EXECUTION: '存在未完成执行日',
  DEADLINE_PROTECTION: '需要守住原结束日期',
  CAPACITY_INSUFFICIENT: '当前容量不足以直接承接',
  HISTORY_LOCKED: '已完成历史需要保持锁定',
  CONSTRAINT_CHANGE: '使用了新的时间约束',
  USER_REQUESTED: '用户主动提出调整',
}

export function InactivityNotice({ inactivity, onViewMissed, onContinue, onReview, onKeepDeadline, continueLabel = '先做今天的任务', reviewLabel = '重新看看接下来的节奏', compact = false }: {
  inactivity: ExecutionInactivity
  onViewMissed?: () => void
  onContinue?: () => void
  onReview: () => void
  onKeepDeadline?: () => void
  continueLabel?: string
  reviewLabel?: string
  compact?: boolean
}) {
  if (inactivity.level === 'NONE' && inactivity.missed_execution_days === 0) return null
  const longPause = inactivity.level === 'PAUSED_7_DAYS'
  const severalDays = inactivity.level === 'PAUSED_3_DAYS'
  const partialDays = inactivity.partial_execution_days ?? 0
  const interruptedDays = inactivity.interrupted_execution_days ?? Math.max(0, inactivity.missed_execution_days - partialDays)
  const missedSummary = [partialDays ? `${partialDays} 天部分未完成` : '', interruptedDays ? `${interruptedDays} 天整天中断` : ''].filter(Boolean).join('，')
  const title = longPause
    ? '欢迎回来，先重新找回合适的节奏'
    : severalDays ? '欢迎回来，不用急着一次补完' : interruptedDays ? '上个执行日中断了' : '上个执行日还有部分任务没完成'
  const detail = longPause
    ? `大约 ${inactivity.inactive_days} 天没有推进了；遗留记录中${missedSummary || '有未完成工作'}。可以先确认现在真正能投入的时间，再决定是否调整接下来的安排。`
    : severalDays
      ? `已经有${missedSummary || ` ${inactivity.missed_execution_days} 个执行日留下未完成内容`}。先从能做的部分恢复，计划不会自动改变。`
      : interruptedDays ? '当天没有任务完成。你可以进入追赶处理，也可以先按今天的安排继续。' : '当天完成了一部分，只需处理剩余任务；已经完成的内容会保留。'
  return <section className={`inactivity-notice ${compact ? 'compact' : ''}`} aria-label="中断恢复提示">
    <div><p className="eyebrow">继续前先看一眼</p><h2>{title}</h2><p>{detail}</p></div>
    <footer>
      {inactivity.missed_execution_days > 0 && <button className="button primary" type="button" onClick={onReview}>{reviewLabel}</button>}
      {onKeepDeadline && (severalDays || longPause || inactivity.recovery_deferred) && <button className="button recovery-deadline-action" type="button" onClick={onKeepDeadline}>保持最终日期</button>}
      {onViewMissed && inactivity.latest_missed_date && <button className="button recovery-quiet-action" type="button" onClick={onViewMissed}>查看遗留内容</button>}
      {onContinue && <button className="recovery-quiet-action" type="button" onClick={onContinue}>{continueLabel}</button>}
    </footer>
  </section>
}

export function RecoveryShiftDialog({ goalId, anchorDate, previewOnly = false, onClose, onApplied }: {
  goalId: string
  anchorDate?: string
  previewOnly?: boolean
  onClose: () => void
  onApplied: () => Promise<void> | void
}) {
  const [preview, setPreview] = useState<RecoveryShiftPreview | null>(null)
  const [error, setError] = useState('')
  const [applying, setApplying] = useState(false)
  const [acceptUnallocated, setAcceptUnallocated] = useState(false)

  useEffect(() => {
    let active = true
    previewRecoveryShift(goalId, anchorDate).then((result) => {
      if (active) setPreview(result)
    }).catch((caught) => {
      if (active) setError(caught instanceof Error ? caught.message : '暂时无法计算新的日期安排。')
    })
    return () => { active = false }
  }, [anchorDate, goalId])

  useEffect(() => {
    function closeOnEscape(event: KeyboardEvent) {
      if (event.key === 'Escape' && !applying) onClose()
    }
    window.addEventListener('keydown', closeOnEscape)
    return () => window.removeEventListener('keydown', closeOnEscape)
  }, [applying, onClose])

  async function applyShift() {
    if (!preview || applying) return
    setApplying(true)
    setError('')
    try {
      await applyRecoveryShift(goalId, preview.id, acceptUnallocated)
      await onApplied()
      onClose()
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : '暂时无法应用新的日期安排。')
      setAcceptUnallocated(false)
      try { setPreview(await previewRecoveryShift(goalId, anchorDate)) }
      catch { setPreview(null) }
    } finally {
      setApplying(false)
    }
  }

  return <DialogBackdrop className="recovery-overlay" onDismiss={onClose}> 
    <section className="recovery-dialog" role="dialog" aria-modal="true" aria-labelledby="recovery-dialog-title">
      <header><div><p className="eyebrow">{previewOnly ? '压力测试预览' : '中断恢复'}</p><h2 id="recovery-dialog-title">从今天接上原来的安排</h2></div><button type="button" aria-label="关闭顺延预览" disabled={applying} onClick={onClose}>×</button></header>
      <div className="recovery-dialog-body">
        {!preview && !error && <div className="recovery-loading"><i aria-hidden="true" /><p>正在计算新的执行日期…</p></div>}
        {error && <p className="form-error" role="alert">{error}</p>}
        {preview && <>
          <div className="recovery-summary">
            <span>遗留执行日<strong>{preview.missed_execution_days} 天</strong></span>
            <span>保留历史记录<strong>{preview.locked_day_ids.length} 天</strong></span>
            <span>新的结束日期<strong>{friendlyDate(preview.projected_end_date)}</strong></span>
          </div>
          {(preview.reason_codes ?? []).length > 0 && <p className="recovery-reason-codes">调整依据：{(preview.reason_codes ?? []).map((code) => adjustmentReasonLabels[code] ?? code).join('、')}</p>}
          {preview.carryover_task_ids.length > 0 && <p className="recovery-carryover">有 {preview.carryover_task_ids.length} 项未完成内容会接到新的执行日，原日期上的完成记录保持不变。</p>}
          <div className="recovery-change-list" aria-label="日期变化">
            {preview.changes.length === 0 ? <p>当前没有需要顺延的执行日。</p> : preview.changes.map((change) => <article key={change.plan_day_id}><div><strong>{change.code}</strong><span>{change.primary_goal}</span></div><time>{friendlyDate(change.from_date)} → {friendlyDate(change.to_date)}</time></article>)}
          </div>
          <div className="recovery-guarantees"><span>任务内容不变</span><span>不会调用模型</span><span>确认前计划不变</span></div>
          <RecoveryTimeReview goalId={goalId} preview={preview.time_preview} accepted={acceptUnallocated} onAccept={setAcceptUnallocated} disabled={applying} />
        </>}
      </div>
      <footer><button className="button" type="button" disabled={applying} onClick={onClose}>保持当前安排</button><button className="button primary" type="button" disabled={previewOnly || !preview || preview.changes.length === 0 || applying || Boolean(preview.time_preview?.unallocated_count && !acceptUnallocated)} onClick={() => void applyShift()}>{previewOnly ? '测试预览不可应用' : applying ? '正在顺延…' : '确认整体顺延'}</button></footer>
    </section>
  </DialogBackdrop>
}

export function RetrospectiveCompletionDialog({ goalId, missedDates, onClose, onCompleted }: {
  goalId: string
  missedDates: string[]
  onClose: () => void
  onCompleted: (progress: ExecutionProgress, count: number) => void
}) {
  const [days, setDays] = useState<TodayWorkspace[]>([])
  const [selected, setSelected] = useState<string[]>([])
  const [busy, setBusy] = useState(true)
  const [error, setError] = useState('')

  useEffect(() => {
    let active = true
    Promise.all(missedDates.map((value) => getToday(goalId, value))).then((items) => {
      if (!active) return
      setDays(items)
      setBusy(false)
    }).catch((caught) => {
      if (!active) return
      setError(caught instanceof Error ? caught.message : '暂时无法读取遗留任务。')
      setBusy(false)
    })
    return () => { active = false }
  }, [goalId, missedDates])

  const candidates = days.flatMap((workspace) => (workspace.day?.tasks ?? [])
    .filter((task) => task.status !== 'COMPLETED' && task.status !== 'CARRIED_OVER')
    .map((task) => ({ ...task, scheduledDate: workspace.today })))

  async function submit() {
    if (!selected.length || busy) return
    setBusy(true)
    setError('')
    try {
      const progress = await completeMissedTasks(goalId, selected)
      onCompleted(progress, selected.length)
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : '暂时无法补记完成状态。')
      setBusy(false)
    }
  }

  return <DialogBackdrop className="recovery-overlay" onDismiss={onClose}> <section className="recovery-dialog retrospective-dialog" role="dialog" aria-modal="true" aria-labelledby="retrospective-title">
    <header><div><p className="eyebrow">补记完成</p><h2 id="retrospective-title">哪些任务其实已经做完了？</h2><p>只补登记真实完成的任务。未选择的内容仍会留在追赶处理中。</p></div><button type="button" aria-label="关闭补记" disabled={busy} onClick={onClose}>×</button></header>
    {busy && days.length === 0 ? <p className="muted">正在读取遗留任务…</p> : <div className="retrospective-task-list">{candidates.map((task) => <label key={task.id} className={selected.includes(task.id) ? 'selected' : ''}><input type="checkbox" checked={selected.includes(task.id)} disabled={busy} onChange={(event) => setSelected((items) => event.target.checked ? [...items, task.id] : items.filter((id) => id !== task.id))} /><span><time>{friendlyDate(task.scheduledDate)}</time><strong>{task.title}</strong><small>{task.estimate_minutes} 分钟 · 原计划 {task.order} / {days.find((day) => day.today === task.scheduledDate)?.day?.tasks.length ?? 0}</small></span></label>)}{candidates.length === 0 && <p className="empty-copy">遗留日期中已没有可补记的任务。</p>}</div>}
    {error && <p className="form-error" role="alert">{error}</p>}
    <footer><button className="button" type="button" disabled={busy} onClick={onClose}>取消</button><button className="button primary" type="button" disabled={busy || selected.length === 0} onClick={() => void submit()}>{busy && days.length > 0 ? '正在补记…' : `确认补记${selected.length ? ` ${selected.length} 项` : ''}`}</button></footer>
  </section></DialogBackdrop>
}

export function RecoveryPlanChoiceDialog({ onRecordCompleted, onShift, onKeepDeadline, onClose }: {
  onRecordCompleted: () => void
  onShift: () => void
  onKeepDeadline: () => void
  onClose: () => void
}) {
  return <DialogBackdrop className="recovery-overlay" onDismiss={onClose}> <section className="recovery-dialog recovery-choice-dialog" role="dialog" aria-modal="true" aria-labelledby="recovery-choice-title">
    <header><div><p className="eyebrow">调整计划</p><h2 id="recovery-choice-title">希望怎样处理这次中断？</h2></div><button type="button" aria-label="关闭调整选择" onClick={onClose}>×</button></header>
    <div className="recovery-plan-choices">
      <button type="button" onClick={onRecordCompleted}><strong>其实已经完成，只是忘记确认</strong><p>选择过去已经做完的任务补记完成；剩余遗留内容仍可继续追赶。</p><span>不会改动计划</span></button>
      <button type="button" onClick={onShift}><strong>直接整体顺延</strong><p>保持原来的任务结构和每日强度，从现在继续，最终完成日期相应后移。</p><span>不调用模型</span></button>
      <button type="button" onClick={onKeepDeadline}><strong>尽量保持完成日期不变</strong><p>把中断任务分摊到后续日期；可选择是否考虑非执行日。</p><span>先计算负载</span></button>
    </div>
    <footer><button className="recovery-quiet-action" type="button" onClick={onClose}>返回欢迎页</button></footer>
  </section></DialogBackdrop>
}

export function RecoveryCatchupDialog({ goalId, anchorDate, previewOnly = false, onClose, onApplied, onRequestAI }: {
  goalId: string
  anchorDate?: string
  previewOnly?: boolean
  onClose: () => void
  onApplied: () => Promise<void> | void
  onRequestAI: (preview: RecoveryCatchupPreview) => Promise<void> | void
}) {
  const [preview, setPreview] = useState<RecoveryCatchupPreview | null>(null)
  const [includeNonExecutionDays, setIncludeNonExecutionDays] = useState(false)
  const [selectedTaskIds, setSelectedTaskIds] = useState<string[]>([])
  const [scopeDirty, setScopeDirty] = useState(false)
  const scopeRequest = useRef(0)
  const applyingRef = useRef(false)
  const [acceptUnallocated, setAcceptUnallocated] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [splitPreview, setSplitPreview] = useState<Awaited<ReturnType<typeof previewRecoverySplit>> | null>(null)
  const [splitTaskId, setSplitTaskId] = useState<string | null>(null)
  const [splitMinutes, setSplitMinutes] = useState('')
  const [requestingDiscussion, setRequestingDiscussion] = useState(false)

  function nextScopeRequest() { return ++scopeRequest.current }

  async function reloadPreview(includeNonExecution = includeNonExecutionDays, rereadScope = false) {
    if (busy) return
    const requestId = nextScopeRequest()
    setAcceptUnallocated(false)
    setBusy(true)
    setError('')
    try {
      const result = await previewRecoveryCatchup(goalId, anchorDate, includeNonExecution, !rereadScope && preview?.recovery_scope ? { source_plan_version_id: preview.recovery_scope.source_plan_version_id, selected_task_ids: selectedTaskIds } : undefined)
      if (requestId !== scopeRequest.current) return
      setPreview(result)
      setSelectedTaskIds(result.recovery_scope?.selected_task_ids ?? [])
      setScopeDirty(false)
    } catch (caught) {
      if (requestId !== scopeRequest.current) return
      setScopeDirty(true)
      setError(caught instanceof Error ? caught.message : '暂时无法重新计算追赶成本。')
    } finally {
      if (requestId === scopeRequest.current) setBusy(false)
    }
  }

  useEffect(() => {
    let active = true
    const requestId = nextScopeRequest()
    previewRecoveryCatchup(goalId, anchorDate, false).then((result) => {
      if (active && requestId === scopeRequest.current) {
        setPreview(result)
        setSelectedTaskIds(result.recovery_scope?.selected_task_ids ?? [])
      }
    }).catch((caught) => {
      if (active) setError(caught instanceof Error ? caught.message : '暂时无法计算追赶成本。')
    })
    return () => { active = false; nextScopeRequest() }
  }, [anchorDate, goalId])

  async function changeNonExecutionDays(checked: boolean) {
    if (busy) return
    setAcceptUnallocated(false)
    setScopeDirty(true)
    setIncludeNonExecutionDays(checked)
    if (!preview?.recovery_scope || selectedTaskIds.length) await reloadPreview(checked)
  }

  async function applyCatchup() {
    if (!preview || busy || scopeDirty || applyingRef.current) return
    applyingRef.current = true
    setBusy(true)
    setError('')
    try {
      await applyRecoveryCatchup(goalId, preview.id, acceptUnallocated)
      await onApplied()
      onClose()
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : '暂时无法应用追赶安排。')
      setAcceptUnallocated(false)
      setScopeDirty(true)
      try {
        const result = await previewRecoveryCatchup(goalId, anchorDate, includeNonExecutionDays, preview.recovery_scope ? { source_plan_version_id: preview.recovery_scope.source_plan_version_id, selected_task_ids: selectedTaskIds } : undefined)
        setPreview(result)
        setSelectedTaskIds(result.recovery_scope?.selected_task_ids ?? [])
        setScopeDirty(false)
      }
      catch { /* Retain the selected scope for retry or an explicit source refresh. */ }
    } finally {
      applyingRef.current = false
      setBusy(false)
    }
  }

  async function requestSplit(taskId: string) {
    if (!preview?.recovery_scope || busy || scopeDirty || previewOnly) return
    const item = preview.recovery_scope.items.find(candidate => candidate.source_task_id === taskId)
    if (!item) return
    setSplitTaskId(taskId)
    setSplitMinutes(`${Math.ceil(item.minutes / 2)}, ${Math.floor(item.minutes / 2)}`)
    setSplitPreview(null)
    setError('')
  }

  async function requestDiscussion() {
    if (!preview || busy || scopeDirty || previewOnly || splitTaskId) return
    setBusy(true)
    setRequestingDiscussion(true)
    setError('')
    try {
      await onRequestAI(preview)
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : '生成失败，可以缩小范围后重新预览。')
    } finally {
      setRequestingDiscussion(false)
      setBusy(false)
    }
  }

  async function calculateSplit() {
    if (!preview || !splitTaskId || busy || scopeDirty || previewOnly) return
    const parts = splitMinutes.split(/[,，]/).map(value => Number(value.trim()))
    if (parts.length < 2 || parts.length > 8 || parts.some(value => !Number.isInteger(value) || value < 5 || value > 1440)) {
      setError('请输入 2–8 个分片，每个分片至少 5 分钟，以逗号分隔。'); return
    }
    setBusy(true); setError('')
    try { setSplitPreview(await previewRecoverySplit(goalId, { preview_id: preview.id, source_task_id: splitTaskId, part_minutes: parts })) }
    catch (caught) { setError(caught instanceof Error ? caught.message : '暂时无法预览任务拆分。') }
    finally { setBusy(false) }
  }

  async function confirmSplit() {
    if (!splitPreview || busy || applyingRef.current || previewOnly) return
    applyingRef.current = true
    setBusy(true); setError(''); setScopeDirty(true); setAcceptUnallocated(false)
    try {
      const result = await applyRecoverySplit(goalId, splitPreview.id)
      setSplitPreview(null); setSplitTaskId(null)
      const refreshed = await previewRecoveryCatchup(goalId, anchorDate, includeNonExecutionDays, {
        source_plan_version_id: result.result_plan_version_id!, selected_task_ids: result.result_selected_task_ids,
      })
      setPreview(refreshed); setSelectedTaskIds(refreshed.recovery_scope?.selected_task_ids ?? []); setScopeDirty(false)
    }
    catch (caught) { setError(caught instanceof Error ? caught.message : '暂时无法应用任务拆分。') }
    finally { applyingRef.current = false; setBusy(false) }
  }

  return <DialogBackdrop className="recovery-overlay" onDismiss={onClose}> <section className="recovery-dialog catchup-dialog" role="dialog" aria-modal="true" aria-labelledby="catchup-dialog-title">
    <header><div><p className="eyebrow">{previewOnly ? '压力测试预览' : '保持最终日期'}</p><h2 id="catchup-dialog-title">把遗留工作接回后续安排</h2></div><button type="button" aria-label="关闭追赶方案" disabled={busy} onClick={onClose}>×</button></header>
    <div className="catchup-dialog-body">
      {!preview && !error && <div className="recovery-loading"><i aria-hidden="true" /><p>正在计算追赶成本…</p></div>}
      {error && <div className="recovery-error"><p className="form-error" role="alert">{error}</p><button className="button" type="button" disabled={busy || Boolean(preview?.recovery_scope && !selectedTaskIds.length)} onClick={() => void reloadPreview()}>按当前选择重试</button>{preview?.recovery_scope && <button className="button" type="button" disabled={busy} onClick={() => void reloadPreview(includeNonExecutionDays, true)}>重新读取全部积压</button>}</div>}
      {preview && <>
        {preview.recovery_scope && <RecoveryScopeSelector scope={preview.recovery_scope} selected={selectedTaskIds} disabled={busy || Boolean(splitTaskId)} dirty={scopeDirty} onChange={ids => { setSelectedTaskIds(ids); setScopeDirty(true); setAcceptUnallocated(false) }} onPreview={() => void reloadPreview()} onSplit={!scopeDirty && !previewOnly ? taskId => void requestSplit(taskId) : undefined} />}
        {splitTaskId && !splitPreview && <form className="recovery-split-confirm" aria-label="任务拆分预览" onSubmit={event => { event.preventDefault(); void calculateSplit() }}>
          <strong>拆分“{preview.recovery_scope?.items.find(item => item.source_task_id === splitTaskId)?.title}”</strong>
          <label>各分片时长（分钟）<input aria-label="各分片时长" value={splitMinutes} disabled={busy} onChange={event => setSplitMinutes(event.target.value)} placeholder="例如：20, 20" /></label>
          <p>每片至少 5 分钟，总时长不变。分片按顺序完成，仍计为同一任务。</p>
          <button className="button" type="button" disabled={busy} onClick={() => setSplitTaskId(null)}>取消拆分</button>
          <button className="button primary" type="submit" disabled={busy}>预览拆分</button>
        </form>}
        {splitPreview && <div className="recovery-split-confirm" role="dialog" aria-label="确认任务拆分"><strong>确认拆分“{splitPreview.title}”</strong><p>{splitPreview.original_minutes} 分钟 → {splitPreview.part_minutes.join(' + ')} 分钟。拆分后仍是同一项任务的连续分片，不会增加完成次数；日期安排需要重新预览。</p><button className="button" type="button" disabled={busy} onClick={() => { setSplitPreview(null); setSplitTaskId(null) }}>取消</button><button className="button primary" type="button" disabled={busy} onClick={() => void confirmSplit()}>确认拆分</button></div>}
        {!scopeDirty && <div className="catchup-facts"><span>本次纳入<strong>{preview.backlog_tasks} 项 · {preview.backlog_minutes} 分钟</strong></span><span>剩余执行日<strong>{preview.remaining_execution_days} 天</strong></span><span>原结束日期<strong>{friendlyDate(preview.original_end_date)}</strong></span></div>}
        {(preview.reason_codes ?? []).length > 0 && <p className="recovery-reason-codes">调整依据：{(preview.reason_codes ?? []).map((code) => adjustmentReasonLabels[code] ?? code).join('、')}</p>}
        {!scopeDirty && <RecoveryTimeReview goalId={goalId} preview={preview.time_preview} accepted={acceptUnallocated} onAccept={setAcceptUnallocated} disabled={busy} />}
        <label className="rest-day-consent"><input type="checkbox" checked={includeNonExecutionDays} disabled={busy} onChange={(event) => void changeNonExecutionDays(event.target.checked)} />可以考虑使用非执行日补任务</label>
        <p className="catchup-consent-note">{includeNonExecutionDays ? '程序会同时考虑后续执行日和非执行日，按日期与可用容量尽早分摊，不会只使用非执行日。' : '当前只把积压任务分摊到后续执行日。'}</p>
        {!scopeDirty && (preview.programmatic_feasible ? <>
          <div className="catchup-result"><strong>本次所选范围可直接调度</strong><p>预计在 {preview.backlog_cleared_date ? friendlyDate(preview.backlog_cleared_date) : '原完成日期前'} 补完本次所选内容，任务内容无需重新拆解。</p></div>
          <div className="catchup-allocation" aria-label="追赶任务分配预览">{preview.allocation.map((item) => <article key={`${item.scheduled_date}-${item.day_type}`}><time>{friendlyDate(item.scheduled_date)}</time><span>{item.day_type === 'EXECUTION' ? '执行日' : '非执行日'}</span><strong>增加 {item.added_tasks} 项 · {item.added_minutes} 分钟</strong><small>当日合计约 {item.total_minutes} 分钟</small></article>)}</div>
        </> : <div className="catchup-ai"><strong>所选内容仍需进一步处理</strong><p>可以进一步缩小范围、拆分允许拆分的任务，或带着所选内容进入规划讨论。未选任务和已完成记录保持保留。</p><button className="button primary" type="button" disabled={busy || previewOnly || Boolean(splitTaskId)} onClick={() => void requestDiscussion()}>{requestingDiscussion ? '正在整理恢复建议…' : '讨论所选任务的恢复方案'}</button><OperationProgress active={requestingDiscussion} scope={preview.id} label="正在分析所选任务并生成恢复建议" detail="正在结合遗留任务、日期限制和可用时间整理建议；完成后会打开恢复讨论，确认前当前执行计划保持不变。" error={error} /></div>)}
      </>}
    </div>
    <footer><button className="recovery-quiet-action" type="button" disabled={busy} onClick={onClose}>返回调整方式</button>{preview?.programmatic_feasible && <button className="button primary" type="button" disabled={previewOnly || busy || scopeDirty || Boolean(splitTaskId) || Boolean(preview.recovery_scope && selectedTaskIds.length === 0) || Boolean(preview.time_preview?.unallocated_count && !acceptUnallocated)} onClick={() => void applyCatchup()}>{previewOnly ? '测试预览不可应用' : busy ? '正在应用…' : '应用这份追赶安排'}</button>}</footer>
  </section></DialogBackdrop>
}

export function DeferredRecoveryReminder({ inactivity, onReview }: { inactivity: ExecutionInactivity; onReview: () => void }) {
  const partialDays = inactivity.partial_execution_days ?? 0
  const interruptedDays = inactivity.interrupted_execution_days ?? Math.max(0, inactivity.missed_execution_days - partialDays)
  const title = partialDays && interruptedDays
    ? `${partialDays} 个执行日部分未完成，${interruptedDays} 个执行日整天中断`
    : partialDays
      ? `${partialDays} 个执行日留下了部分任务`
      : `${interruptedDays} 个执行日整天中断`
  const detail = [
    partialDays ? `部分未完成：${(inactivity.partial_dates ?? []).map(friendlyDate).join('、')}` : '',
    interruptedDays ? `整天中断：${(inactivity.interrupted_dates ?? inactivity.missed_dates).map(friendlyDate).join('、')}` : '',
  ].filter(Boolean).join('；')
  return <section className="deferred-recovery-reminder" aria-label="待处理的中断工作"><div><p className="eyebrow">仍需处理 · 共 {inactivity.missed_execution_days} 个执行日</p><h2>{title}</h2><p>{detail}。两类遗留都从同一入口追赶，已经完成的任务不会重新安排。</p></div><button className="button primary" type="button" onClick={onReview}>查看并处理</button></section>
}



export function PauseGoalDialog({ goalId, onClose, onPaused }: { goalId: string; onClose: () => void; onPaused: (goal: GoalLifecycle) => void }) {
  const tomorrow = new Date()
  tomorrow.setDate(tomorrow.getDate() + 1)
  const [mode, setMode] = useState<'DATE' | 'INDEFINITE'>('DATE')
  const [pauseUntil, setPauseUntil] = useState(dateInputValue(tomorrow))
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (busy || (mode === 'DATE' && !pauseUntil)) return
    setBusy(true)
    setError('')
    try {
      onPaused(await pauseGoal(goalId, mode === 'DATE' ? pauseUntil : null))
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : '暂时无法暂停目标。')
      setBusy(false)
    }
  }

  return <DialogBackdrop className="feedback-overlay" onDismiss={onClose}> <form className="feedback-dialog pause-dialog" role="dialog" aria-modal="true" aria-labelledby="pause-dialog-title" onSubmit={submit}>
    <header><div><p className="eyebrow">暂停执行</p><h2 id="pause-dialog-title">准备什么时候继续？</h2></div><button className="icon-button" type="button" aria-label="关闭" onClick={onClose}>×</button></header>
    <div className="pause-mode" role="group" aria-label="暂停方式">
      <button className={mode === 'DATE' ? 'selected' : ''} type="button" onClick={() => setMode('DATE')}>指定恢复日期</button>
      <button className={mode === 'INDEFINITE' ? 'selected' : ''} type="button" onClick={() => setMode('INDEFINITE')}>暂不设日期</button>
    </div>
    {mode === 'DATE' && <label>恢复日期<input type="date" min={dateInputValue(tomorrow)} value={pauseUntil} onChange={(event) => setPauseUntil(event.target.value)} /></label>}
    <p>暂停期间不会把任务累积到当天。到期或手动恢复时，未完成安排会从恢复日继续。</p>
    {error && <p className="form-error" role="alert">{error}</p>}
    <footer><button className="button" type="button" onClick={onClose}>取消</button><button className="button primary" type="submit" disabled={busy || (mode === 'DATE' && !pauseUntil)}>{busy ? '正在暂停…' : '确认暂停'}</button></footer>
  </form></DialogBackdrop>
}
