import { DialogBackdrop } from '../ui/DialogBackdrop'
import { useEffect, useRef, useState } from 'react'
import { X } from 'lucide-react'
import { applyFlexibleTaskDecision, previewFlexibleTaskDecision, type FlexibleTaskDecisionPreview, type TodayWorkspace } from '../api'
import { friendlyDate } from '../ui/plannerPresentation'
import { ModalLayer } from '../ui/ModalLayer'

export type FlexibleDecisionTarget = {
  id: string; title: string; scheduled_date: string; estimate_minutes: number
  action: 'ARRANGE_TODAY' | 'RESCHEDULE'
}

export function FlexibleTaskDecisionDialog({ goalId, target, today, disabled, onClose, onApplied }: {
  goalId: string; target: FlexibleDecisionTarget; today: string; disabled: boolean
  onClose: () => void; onApplied: (workspace: TodayWorkspace, notice: string) => void
}) {
  const [date, setDate] = useState(target.scheduled_date >= today ? target.scheduled_date : today)
  const [reason, setReason] = useState('')
  const [preview, setPreview] = useState<FlexibleTaskDecisionPreview | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const sequence = useRef(0)
  const pending = useRef(false)
  const arranging = target.action === 'ARRANGE_TODAY'
  useEffect(() => () => { sequence.current += 1 }, [])
  function invalidate() { sequence.current += 1; setPreview(null); setError('') }
  async function createPreview() {
    if (pending.current || disabled || (!arranging && (!date || !reason.trim()))) return
    const requestId = ++sequence.current
    pending.current = true; setBusy(true); setError(''); setPreview(null)
    try {
      const result = await previewFlexibleTaskDecision(goalId, {
        action: target.action, task_id: target.id,
        ...(!arranging ? { target_date: date, reason: reason.trim() } : {}),
      })
      if (sequence.current === requestId) setPreview(result)
    } catch (caught) {
      if (sequence.current === requestId) setError(caught instanceof Error ? caught.message : '暂时无法生成预览，请重试。')
    } finally { pending.current = false; setBusy(false) }
  }
  async function confirm() {
    if (pending.current || disabled || !preview?.can_apply) return
    pending.current = true; setBusy(true); setError('')
    try {
      const result = await applyFlexibleTaskDecision(goalId, preview.id)
      onApplied(result, arranging ? `已将“${target.title}”加入今天，可以开始或记录完成。` : `已将“${target.title}”改到 ${friendlyDate(preview.target_date ?? date)}。`)
    } catch (caught) {
      setPreview(null)
      setError(caught instanceof Error ? caught.message : '确认未完成，请重新生成预览后再确认。')
    } finally { pending.current = false; setBusy(false) }
  }
  return <ModalLayer><DialogBackdrop className="inline-dialog-backdrop" onDismiss={onClose}> <form className="inline-dialog flexible-decision-dialog" role="dialog" aria-modal="true" aria-labelledby="flexible-decision-title" onSubmit={(event) => { event.preventDefault(); void createPreview() }}>
    <header><h2 id="flexible-decision-title">{arranging ? '安排到今天' : '改到某个日期'}</h2><button className="icon-button" type="button" aria-label="关闭任务安排" disabled={busy} onClick={onClose}><X size={18} /></button></header>
    <strong>{target.title}</strong><p>当前建议日：{friendlyDate(target.scheduled_date)} · {target.estimate_minutes} 分钟</p>
    {arranging ? <p>先检查今日余量和前置条件，再确认加入今日任务。具体时间可稍后安排。</p> : <>
      <label htmlFor="flexible-decision-date">改到日期</label><input id="flexible-decision-date" type="date" value={date} min={today} required disabled={busy} onChange={(event) => { setDate(event.target.value); invalidate() }} />
      <label htmlFor="flexible-decision-reason">改期原因</label><textarea id="flexible-decision-reason" value={reason} maxLength={500} required disabled={busy} placeholder="例如：今天临时加班，改到周四完成" onChange={(event) => { setReason(event.target.value); invalidate() }} />
    </>}
    {preview && <section className="flexible-decision-preview" aria-label="任务安排影响预览"><strong>影响预览</strong>
      <p>{preview.source_date && preview.target_date ? `${friendlyDate(preview.source_date)} → ${friendlyDate(preview.target_date)}` : friendlyDate(preview.target_date ?? today)}</p>
      <p>本规划目标日预计 {preview.planned_minutes} / {preview.capacity_minutes} 分钟{preview.over_capacity_minutes > 0 ? ` · 超出建议 ${preview.over_capacity_minutes} 分钟` : ''}</p>
      {preview.shared_available_minutes != null && <p>共享日程空闲 {preview.shared_available_minutes} 分钟{preview.proposed_start_local && preview.proposed_end_local ? ` · 可安排 ${preview.proposed_start_local}–${preview.proposed_end_local}` : ''}</p>}
      {preview.competing_tasks.length > 0 && <small>同日安排：{preview.competing_tasks.join('、')}</small>}
      {preview.warnings.map((item) => <small key={item}>{item}</small>)}
      {preview.blockers.map((item) => <small className="form-error" key={item}>{item}</small>)}
      {preview.remaining_commitments.filter((item) => item.remaining_occurrences > 0).slice(0, 3).map((item) => <small key={`${item.label}-${item.cadence}`}>{item.label}：还需 {item.remaining_occurrences} 次{item.remaining_minutes > 0 ? `，约 ${item.remaining_minutes} 分钟` : ''}</small>)}
      <small>日期调整后，完成记录与已投入进度继续保留；预览中的时段只是建议。</small>
    </section>}
    {error && <p className="form-error" role="alert">{error}</p>}
    <footer className="inline-dialog-actions"><button className="button" type="button" disabled={busy} onClick={onClose}>取消</button>
      {preview ? <><button className="button quiet" type="button" disabled={busy || disabled} onClick={() => void createPreview()}>重新预览</button><button className="button primary" type="button" disabled={busy || disabled || !preview.can_apply} onClick={() => void confirm()}>{busy ? '正在确认…' : arranging ? '确认加入今天' : '确认改期'}</button></> : <button className="button primary" type="submit" disabled={busy || disabled || (!arranging && (!date || !reason.trim()))}>{busy ? '正在生成预览…' : '生成影响预览'}</button>}
    </footer>
  </form></DialogBackdrop></ModalLayer>
}
