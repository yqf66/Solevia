import { DialogBackdrop } from './ui/DialogBackdrop'
import { useState } from 'react'
import { ArrowRightLeft, CalendarClock, X } from 'lucide-react'
import { applyExecutionDayMove, previewExecutionDayMove, previewSmartCalendar } from './api'
import type { ExecutionCalendar, ExecutionDayMove, SmartCalendar, TodayWorkspace } from './api'
import { OperationProgress } from './ui/ProcessDetails'
import './ExecutionDateControls.css'

export function SmartCalendarControl({ goalId, startDate, result, onResult, onBusy, onStartDate, onManual }: {
  goalId: string
  startDate: string
  result: SmartCalendar | null
  onResult: (result: SmartCalendar | null) => void
  onBusy: (busy: boolean) => void
  onStartDate: (date: string) => void
  onManual: () => void
}) {
  const [instructions, setInstructions] = useState('')
  const [useSelectedStart, setUseSelectedStart] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  async function generate() {
    if (busy) return
    setBusy(true); onBusy(true); setError(''); onResult(null)
    try {
      onResult(await previewSmartCalendar(goalId, {
        instructions, preferred_start_date: useSelectedStart ? startDate : null,
      }))
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : '智能日期草案暂时没有完成，仍可手动安排。')
    } finally { setBusy(false); onBusy(false) }
  }
  return <section className="setup-section smart-calendar-control" aria-label="智能日期排布">
    <header><CalendarClock size={22} /><div><h3>智能排布日期</h3></div></header>
    <label>补充日期安排（可选）<textarea aria-label="补充日期安排" value={instructions} disabled={busy} onChange={(e) => { setInstructions(e.target.value); onResult(null) }} placeholder="例如：周一到周六跟课，周日做套卷；国庆只休一天，其余照常。" /></label>
    <label className="calendar-start-choice"><input type="checkbox" checked={useSelectedStart} disabled={busy} onChange={(e) => { setUseSelectedStart(e.target.checked); onResult(null) }} />使用我选择的开始日期：{startDate}</label>
    {useSelectedStart && <input aria-label="智能安排指定开始日期" type="date" value={startDate} disabled={busy} onChange={(e) => { onStartDate(e.target.value); onResult(null) }} />}
    <button className="button primary" type="button" disabled={busy} onClick={() => void generate()}><CalendarClock size={18} />{busy ? '正在结合讨论排布日期…' : result ? '重新排布日期' : '结合讨论排布日期'}</button>
    {error && <p className="form-error" role="alert">{error}</p>}
    <OperationProgress active={busy} label="正在结合讨论排布日期并校验可用时间" scope={goalId} error={error} />
    {result && <div className="smart-calendar-result"><p>{result.rationale}</p>{result.rules.length > 0 && <ul>{result.rules.map((r) => <li key={r}>{r}</li>)}</ul>}{result.assumptions.length > 0 && <details><summary>采用的建议与假设</summary><ul>{result.assumptions.map((a) => <li key={a}>{a}</li>)}</ul></details>}{result.questions.length > 0 && <div role="alert"><strong>确认前需要补充</strong><ul>{result.questions.map((q) => <li key={q}>{q}</li>)}</ul></div>}<details><summary>查看全部 {result.days.length} 个日期</summary><div className="execution-date-list">{result.days.map((d) => <div key={d.plan_day_id}><time>{d.scheduled_date}</time><span>{d.code} · {d.primary_goal}</span></div>)}</div></details><button className="button" type="button" onClick={onManual}>改用手动日期</button></div>}
  </section>
}

export function ExecutionDayMoveDialog({ goalId, today, calendar, onClose, onApplied }: {
  goalId: string
  today: string
  calendar: ExecutionCalendar
  onClose: () => void
  onApplied: (result: TodayWorkspace) => void
}) {
  const [mode, setMode] = useState<'ADVANCE' | 'SWAP'>('ADVANCE')
  const [selected, setSelected] = useState('')
  const [preview, setPreview] = useState<ExecutionDayMove | null>(null)
  const [acknowledged, setAcknowledged] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const candidates = calendar.days.filter((day) => day.scheduled_date > today && day.tasks.some((t) => t.status === 'NOT_STARTED' || t.status === 'IN_PROGRESS'))
  function reset() { setPreview(null); setAcknowledged(false); setError('') }
  async function review() {
    setBusy(true); setError('')
    try { setPreview(await previewExecutionDayMove(goalId, mode, selected)) }
    catch (caught) { setError(caught instanceof Error ? caught.message : '暂时无法审核调整。') }
    finally { setBusy(false) }
  }
  async function apply() {
    if (!preview || !preview.can_apply || (mode === 'ADVANCE' && !acknowledged)) return
    setBusy(true); setError('')
    try { onApplied(await applyExecutionDayMove(goalId, preview.preview_id)); onClose() }
    catch (caught) { setError(caught instanceof Error ? caught.message : '暂时无法应用调整。'); setPreview(null) }
    finally { setBusy(false) }
  }
  return <DialogBackdrop className="inline-dialog-backdrop" onDismiss={onClose}> <section className="inline-dialog execution-day-move-dialog" role="dialog" aria-modal="true" aria-labelledby="day-move-title"><header><div><h2 id="day-move-title">调整执行日</h2></div><button className="icon-button" title="关闭" aria-label="关闭执行日调整" disabled={busy} onClick={onClose}><X size={20} /></button></header>
    <div className="date-mode-control" role="group" aria-label="执行日调整方式"><button type="button" className={mode === 'ADVANCE' ? 'selected' : ''} disabled={busy} onClick={() => { setMode('ADVANCE'); reset() }}><CalendarClock size={18} />从指定日开始</button><button type="button" className={mode === 'SWAP' ? 'selected' : ''} disabled={busy} onClick={() => { setMode('SWAP'); reset() }}><ArrowRightLeft size={18} />与今天互换</button></div>
    <label>{mode === 'ADVANCE' ? '选择今天开始执行的内容' : '选择今天替换执行的内容'}<select aria-label="选择执行日" value={selected} disabled={busy} onChange={(e) => { setSelected(e.target.value); reset() }}><option value="">请选择执行日</option>{candidates.map((d) => <option key={d.plan_day_id} value={d.plan_day_id}>{d.scheduled_date} · {d.code} · {d.primary_goal}</option>)}</select></label>
    {preview && <div className="execution-day-move-result">{preview.blockers.length > 0 ? <div className="execution-move-blockers" role="alert"><strong>当前不能这样调整</strong><ul>{preview.blockers.map((b) => <li key={b}>{b}</li>)}</ul></div> : preview.warnings.length > 0 ? <div className="execution-move-warnings"><strong>可以调整，请留意顺序变化</strong><ul>{preview.warnings.map((warning) => <li key={warning}>{warning}</li>)}</ul></div> : <strong>必要前置条件审核通过</strong>}<div className="execution-date-list">{preview.changes.map((c) => <div key={c.plan_day_id}><span>{c.code} · {c.primary_goal}</span><time>{c.from_date} → {c.to_date}</time></div>)}</div>{mode === 'ADVANCE' && <label className="calendar-start-choice"><input type="checkbox" checked={acknowledged} onChange={(e) => setAcknowledged(e.target.checked)} disabled={busy} />确认此前内容已完成：将补记 {preview.completed_task_count} 项完成，后续安排按原间隔前移至今天。</label>}{mode === 'SWAP' && <p>今天的任务移到选中日期，不会被标记完成；其他日期不变。</p>}</div>}
    <OperationProgress active={busy} label={preview ? "正在应用确认后的执行日调整" : "正在校验执行日调整并生成预览"} scope={goalId} error={error} />
    {error && <p className="form-error" role="alert">{error}</p>}<footer className="inline-dialog-actions"><button className="button" type="button" disabled={busy} onClick={onClose}>取消</button>{!preview ? <button className="button primary" type="button" disabled={busy || !selected} onClick={() => void review()}>{busy ? '正在审核…' : '审核并预览'}</button> : <button className="button primary" type="button" disabled={busy || !preview.can_apply || (mode === 'ADVANCE' && !acknowledged)} onClick={() => void apply()}>{busy ? '正在应用…' : '确认调整执行日'}</button>}</footer>
  </section></DialogBackdrop>
}
