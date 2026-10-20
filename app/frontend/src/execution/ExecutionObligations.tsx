import { DialogBackdrop } from '../ui/DialogBackdrop'
import { useEffect, useState } from 'react'
import { Check, CircleAlert, X } from 'lucide-react'
import {
  applyCandidateReplacement,
  applyTriggeredTaskActivation,
  getCandidatePools,
  getTriggeredTasks,
  previewCandidateReplacement,
  previewTriggeredTaskActivation,
  type CandidatePool,
  type CandidateReplacementPreview,
  type TodayWorkspace,
  type TriggeredTask,
  type TriggeredTaskActivationPreview,
} from '../api'
import { ModalLayer } from '../ui/ModalLayer'
import { dateInputValue, friendlyDate } from '../ui/plannerPresentation'

type Props = {
  goalId: string
  workspace: TodayWorkspace
  onActivated: (workspace: TodayWorkspace) => void
}

export function ExecutionObligations({ goalId, workspace, onActivated }: Props) {
  const [pools, setPools] = useState<CandidatePool[]>([])
  const [triggers, setTriggers] = useState<TriggeredTask[]>([])
  const [target, setTarget] = useState<TriggeredTask | null>(null)
  const [replacement, setReplacement] = useState<{ pool: CandidatePool; candidateId: string; candidateTitle: string; periodKey?: string } | null>(null)
  const [replacementDate, setReplacementDate] = useState('')
  const [replacementReason, setReplacementReason] = useState('')
  const [replacementPreview, setReplacementPreview] = useState<CandidateReplacementPreview | null>(null)
  const [targetDate, setTargetDate] = useState('')
  const [evidence, setEvidence] = useState('')
  const [preview, setPreview] = useState<TriggeredTaskActivationPreview | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')

  useEffect(() => {
    let active = true
    Promise.all([getCandidatePools(goalId), getTriggeredTasks(goalId)])
      .then(([nextPools, nextTriggers]) => {
        if (!active) return
        setPools(nextPools)
        setTriggers(nextTriggers)
        setError('')
      }).catch((caught) => {
        if (active) setError(caught instanceof Error ? caught.message : '暂时无法读取候选与触发任务')
      })
    return () => { active = false }
  }, [goalId, workspace])

  function openTrigger(task: TriggeredTask) {
    setTarget(task)
    setTargetDate([dateInputValue(), task.eligible_from].sort().at(-1) ?? task.eligible_from)
    setEvidence('')
    setPreview(null)
    setError('')
  }

  function openReplacement(pool: CandidatePool, candidateId: string, candidateTitle: string, periodKey?: string) {
    setReplacement({ pool, candidateId, candidateTitle, periodKey })
    setReplacementDate(dateInputValue())
    setReplacementReason('')
    setReplacementPreview(null)
    setError('')
  }

  async function createReplacementPreview() {
    if (!replacement || !replacementReason.trim() || !replacementDate || busy) return
    setBusy(true)
    setError('')
    try {
      setReplacementPreview(await previewCandidateReplacement(goalId, {
        group_id: replacement.pool.id,
        candidate_item_id: replacement.candidateId,
        target_date: replacementDate,
        reason: replacementReason.trim(),
        period_key: replacement.periodKey,
      }))
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : '暂时无法生成候选替换预览')
    } finally {
      setBusy(false)
    }
  }

  async function confirmReplacement() {
    if (!replacementPreview?.can_apply || busy) return
    setBusy(true)
    setError('')
    try {
      const updated = await applyCandidateReplacement(goalId, replacementPreview.id)
      setReplacement(null)
      onActivated(updated)
    } catch (caught) {
      setReplacementPreview(null)
      setError(caught instanceof Error ? caught.message : '暂时无法替换候选任务')
    } finally {
      setBusy(false)
    }
  }

  async function createPreview() {
    if (!target || !evidence.trim() || !targetDate || busy) return
    setBusy(true)
    setError('')
    try {
      setPreview(await previewTriggeredTaskActivation(goalId, {
        window_id: target.window_id, target_date: targetDate, evidence: evidence.trim(),
      }))
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : '暂时无法生成触发预览')
    } finally {
      setBusy(false)
    }
  }

  async function confirmTrigger() {
    if (!preview?.can_apply || busy) return
    setBusy(true)
    setError('')
    try {
      const updated = await applyTriggeredTaskActivation(goalId, preview.id)
      setTarget(null)
      onActivated(updated)
    } catch (caught) {
      setPreview(null)
      setError(caught instanceof Error ? caught.message : '暂时无法安排触发任务')
    } finally {
      setBusy(false)
    }
  }

  if (!pools.length && !triggers.length && !error) return null
  return <>
    <details className="execution-obligations">
      <summary><span>候选与触发任务</span><small>{pools.length} 个候选组 · {triggers.length} 项待触发</small></summary>
      {pools.map((pool) => <section key={pool.id} aria-label={pool.title}>
        <header><strong>{pool.title}</strong>{pool.period_status === 'COMPLETE' && <Check size={16} aria-label="已达标" />}</header>
        <progress value={pool.completed_selected} max={Math.max(1, pool.minimum_selected)} aria-label={`${pool.title}完成进度`} />
        <small>完成 {pool.completed_selected} / 至少 {pool.minimum_selected} 项 · 还需 {pool.remaining_required} 项</small>
        {(pool.periods ?? []).length > 1 && <div className="candidate-period-list">{pool.periods?.map((period) => <small key={period.period_key}>{period.period_key}：{period.completed_selected}/{pool.minimum_selected} · {period.period_status === 'COMPLETE' ? '已达标' : `还需 ${period.remaining_required}`}</small>)}</div>}
        <ul>{pool.candidates.filter((item) => pool.selected_item_ids.includes(item.id)).map((item) => <li key={item.id}>{item.title}</li>)}</ul>
        {pool.period_status === 'IN_PROGRESS' && pool.candidates.some((item) => !pool.selected_item_ids.includes(item.id)) && <div className="candidate-replacement-list"><small>{pool.selected_item_ids.length < pool.minimum_selected ? '待补选候选' : '可替换候选'}</small>{pool.candidates.filter((item) => !pool.selected_item_ids.includes(item.id)).map((item) => <button className="button quiet" type="button" key={item.id} onClick={() => openReplacement(pool, item.id, item.title, pool.periods?.find((period) => period.period_status !== 'COMPLETE')?.period_key)}>{pool.selected_item_ids.length < pool.minimum_selected ? `补选“${item.title}”` : `用“${item.title}”替换`}</button>)}</div>}
      </section>)}
      {triggers.map((task) => <section key={task.window_id}>
        <header><strong>{task.title}</strong><CircleAlert size={16} /></header>
        <p>{task.condition}</p>
        <small>{friendlyDate(task.eligible_from)} 至 {friendlyDate(task.eligible_to)} · {task.estimate_minutes} 分钟 · 已完成 {task.completed_value ?? 0}/{task.target_value ?? 1}{task.target_unit === 'MINUTES' ? ' 分钟' : ' 次'}</small>
        {task.explanation && <small>{task.explanation}</small>}
        {task.blocked_reason && <small className="form-error">{task.blocked_reason}</small>}
        <button className="button quiet" type="button" disabled={!task.can_activate || workspace.status !== 'ACTIVE'} onClick={() => openTrigger(task)}>确认已触发</button>
      </section>)}
      {!target && error && <p className="form-error" role="alert">{error}</p>}
    </details>
    {target && <ModalLayer><DialogBackdrop className="inline-dialog-backdrop" onDismiss={() => setTarget(null)}> <form className="inline-dialog" role="dialog" aria-modal="true" aria-labelledby="trigger-task-title" onSubmit={(event) => { event.preventDefault(); void createPreview() }}>
      <header><h2 id="trigger-task-title">确认触发任务</h2><button className="icon-button" type="button" title="关闭" aria-label="关闭触发确认" disabled={busy} onClick={() => setTarget(null)}><X size={18} /></button></header>
      <strong>{target.title}</strong><p>触发条件：{target.condition}</p>
      <label htmlFor="trigger-evidence">触发依据</label>
      <textarea id="trigger-evidence" required maxLength={1000} value={evidence} onChange={(event) => { setEvidence(event.target.value); setPreview(null) }} />
      <label htmlFor="trigger-target-date">执行日期</label>
      <input id="trigger-target-date" type="date" required min={[dateInputValue(), target.eligible_from].sort().at(-1)} max={target.eligible_to} value={targetDate} onChange={(event) => { setTargetDate(event.target.value); setPreview(null) }} />
      {preview && <div className="flexible-decision-preview"><strong>安排预览</strong><p>{friendlyDate(preview.target_date ?? targetDate)} · {preview.fulfillment_mode === 'DURATION' ? `${preview.target_unit === 'MINUTES' ? preview.remaining_value ?? 0 : target.estimate_minutes} 分钟` : target.estimate_minutes + ' 分钟'}</p><small>触发后预计还需 {preview.remaining_value ?? 0}{preview.target_unit === 'MINUTES' ? ' 分钟' : ' 次'}</small>{preview.warnings.map((item) => <small key={item}>{item}</small>)}{preview.blockers.map((item) => <small className="form-error" key={item}>{item}</small>)}</div>}
      {error && <p className="form-error" role="alert">{error}</p>}
      <footer className="inline-dialog-actions"><button className="button" type="button" disabled={busy} onClick={() => setTarget(null)}>取消</button>{preview ? <button className="button primary" type="button" disabled={busy || !preview.can_apply} onClick={() => void confirmTrigger()}>{busy ? '正在安排…' : '确认生成执行任务'}</button> : <button className="button primary" type="submit" disabled={busy || !evidence.trim() || !targetDate}>{busy ? '正在预览…' : '预览安排'}</button>}</footer>
    </form></DialogBackdrop></ModalLayer>}
    {replacement && <ModalLayer><DialogBackdrop className="inline-dialog-backdrop" onDismiss={() => setReplacement(null)}> <form className="inline-dialog" role="dialog" aria-modal="true" aria-labelledby="candidate-replacement-title" onSubmit={(event) => { event.preventDefault(); void createReplacementPreview() }}>
      <header><h2 id="candidate-replacement-title">{replacement.pool.selected_item_ids.length < replacement.pool.minimum_selected ? '补选候选任务' : '替换候选任务'}</h2><button className="icon-button" type="button" title="关闭" aria-label="关闭候选替换" disabled={busy} onClick={() => setReplacement(null)}><X size={18} /></button></header>
      <strong>{replacement.candidateTitle}</strong><p>会停用当前候选的未完成执行卡，并保留原履约记录。</p>
      <label htmlFor="candidate-replacement-date">执行日期</label>
      <input id="candidate-replacement-date" type="date" min={dateInputValue()} value={replacementDate} onChange={(event) => { setReplacementDate(event.target.value); setReplacementPreview(null) }} required />
      <label htmlFor="candidate-replacement-reason">{replacement.pool.selected_item_ids.length < replacement.pool.minimum_selected ? '补选原因' : '替换原因'}</label>
      <textarea id="candidate-replacement-reason" required maxLength={1000} value={replacementReason} onChange={(event) => { setReplacementReason(event.target.value); setReplacementPreview(null) }} placeholder="例如：原候选所需材料暂时无法获得，改做这个候选。" />
      {replacementPreview && <div className="flexible-decision-preview"><strong>{replacementPreview.replaced_item_ids.length ? '替换预览' : '补选预览'}</strong><p>预计 {replacementPreview.candidate_minutes} 分钟 · 目标日已有 {replacementPreview.planned_minutes} 分钟</p>{replacementPreview.over_capacity_minutes > 0 && <small>超出建议容量 {replacementPreview.over_capacity_minutes} 分钟</small>}{replacementPreview.warnings.map((item) => <small key={item}>{item}</small>)}{replacementPreview.blockers.map((item) => <small className="form-error" key={item}>{item}</small>)}</div>}
      {error && <p className="form-error" role="alert">{error}</p>}
      <footer className="inline-dialog-actions"><button className="button" type="button" disabled={busy} onClick={() => setReplacement(null)}>取消</button>{replacementPreview ? <button className="button primary" type="button" disabled={busy || !replacementPreview.can_apply} onClick={() => void confirmReplacement()}>{busy ? '正在安排…' : replacementPreview.replaced_item_ids.length ? '确认替换候选' : '确认补选候选'}</button> : <button className="button primary" type="submit" disabled={busy || !replacementReason.trim() || !replacementDate}>{busy ? '正在预览…' : replacement.pool.selected_item_ids.length < replacement.pool.minimum_selected ? '预览补选' : '预览替换'}</button>}</footer>
    </form></DialogBackdrop></ModalLayer>}
  </>
}
