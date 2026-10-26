import { DialogBackdrop } from '../ui/DialogBackdrop'
import { type FeedbackTarget, taskFeedbackChoices, dayPaceChoices, intentsByReason, intentLabels } from './feedbackOptions'
import { useState } from 'react'
import type { FormEvent } from 'react'

export function FeedbackDialog({ target, onClose, onSubmit }: {
  target: FeedbackTarget
  onClose: () => void
  onSubmit: (reason: string, intent: string, detail: string, remainingMinutes: number | null) => Promise<void>
}) {
  const choices = target.kind === 'TASK' ? taskFeedbackChoices : dayPaceChoices
  const initialReason = target.kind === 'DAY' ? target.reason : ''
  const [reason, setReason] = useState(initialReason)
  const [intent, setIntent] = useState(initialReason ? (intentsByReason[initialReason] ?? ['OBSERVE'])[0] : '')
  const [detail, setDetail] = useState('')
  const [rescheduleMode, setRescheduleMode] = useState<'NORMAL_EXECUTION' | 'REST_DAY'>('NORMAL_EXECUTION')
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')
  const [remainingMinutes, setRemainingMinutes] = useState(
    target.kind === 'TASK' ? Math.max(1, Math.round(target.estimateMinutes / 2)) : 1,
  )
  const availableIntents = reason ? intentsByReason[reason] ?? ['OBSERVE'] : []
  const detailRequired = reason === 'OTHER' || reason === 'CUSTOM_ADJUSTMENT'

  function chooseReason(code: string) {
    setReason(code)
    setIntent((intentsByReason[code] ?? ['OBSERVE'])[0])
    setRescheduleMode('NORMAL_EXECUTION')
  }

  async function handleSubmit(event: FormEvent) {
    event.preventDefault()
    if (!reason || !intent || (detailRequired && !detail.trim())) return
    setSaving(true)
    setError('')
    try {
      const submittedIntent = reason === 'TEMPORARY_CONFLICT' && intent === 'RESCHEDULE_ONLY' && rescheduleMode === 'REST_DAY'
        ? 'RECOVER_ON_REST_DAY'
        : intent
      await onSubmit(
        reason,
        submittedIntent,
        detail.trim(),
        target.kind === 'TASK' && target.status === 'IN_PROGRESS' && intent === 'RESCHEDULE_ONLY'
          ? remainingMinutes
          : null,
      )
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : '暂时无法保存反馈。')
      setSaving(false)
    }
  }

  return (
    <DialogBackdrop className="feedback-overlay" onDismiss={onClose}> 
      <section className="feedback-dialog" role="dialog" aria-modal="true" aria-labelledby="feedback-title">
        <header><div><p className="eyebrow">{target.kind === 'TASK' ? '任务反馈' : '今日节奏反馈'}</p><h2 id="feedback-title">{target.title}</h2></div><button type="button" aria-label="关闭反馈" onClick={onClose}>×</button></header>
        <form onSubmit={(event) => void handleSubmit(event)}>
          <fieldset><legend>{target.kind === 'TASK' ? '今天为什么暂时做不完？' : '今天整体感觉如何？'}</legend><div className="feedback-choice-grid">{choices.map((choice) => <button className={reason === choice.code ? 'selected' : ''} type="button" key={choice.code} onClick={() => chooseReason(choice.code)}><strong>{choice.label}</strong><span>{choice.detail}</span></button>)}</div></fieldset>
          {reason && <fieldset><legend>你更希望怎样处理？</legend><div className="feedback-intents">{availableIntents.map((code) => <button className={intent === code ? 'selected' : ''} type="button" key={code} onClick={() => setIntent(code)}>{intentLabels[code]}</button>)}</div>{reason === 'TEMPORARY_CONFLICT' && intent === 'RESCHEDULE_ONLY' && <div className="reschedule-mode"><strong>如果中间有非执行日：</strong><div><button className={rescheduleMode === 'REST_DAY' ? 'selected' : ''} type="button" onClick={() => setRescheduleMode('REST_DAY')}>在最近的非执行日补做</button><button className={rescheduleMode === 'NORMAL_EXECUTION' ? 'selected' : ''} type="button" onClick={() => setRescheduleMode('NORMAL_EXECUTION')}>留到最近的正常执行日</button></div><p>两种方式都只移动未完成部分，并且不会安排到未来 7 天以外。</p></div>}</fieldset>}
          {target.kind === 'TASK' && target.status === 'IN_PROGRESS' && intent === 'RESCHEDULE_ONLY' && <label className="remaining-work"><span>这项任务大约还剩多少分钟？</span><div><button type="button" onClick={() => setRemainingMinutes(Math.max(1, Math.round(target.estimateMinutes * .25)))}>剩少量</button><button type="button" onClick={() => setRemainingMinutes(Math.max(1, Math.round(target.estimateMinutes * .5)))}>约一半</button><input aria-label="剩余分钟" type="number" min="1" max={target.estimateMinutes} value={remainingMinutes} onChange={(event) => setRemainingMinutes(Math.min(target.estimateMinutes, Math.max(1, Number(event.target.value) || 1)))} /><small>分钟</small></div><p>今天已完成的部分会保留，只移动剩余量。</p></label>}
          {reason && <label className="feedback-detail"><span>{detailRequired ? '请说明你的情况或调整想法（必填）' : '还有什么想告诉 Solevia？（可选）'}</span><textarea value={detail} maxLength={2000} rows={3} onChange={(event) => setDetail(event.target.value)} placeholder="例如：今天临时加班一小时；希望基础学习阶段多留一些消化时间。" /></label>}
          <p className="feedback-safety-note">反馈会先保存；如果适合小范围调整，Solevia 会先给你看建议，只有你确认后才会改变安排。</p>
          {error && <p className="form-error" role="alert">{error}</p>}
          <footer><button className="button" type="button" onClick={onClose}>先不反馈</button><button className="button primary" type="submit" disabled={saving || !reason || !intent || (detailRequired && !detail.trim())}>{saving ? '正在分析…' : '保存并分析反馈'}</button></footer>
        </form>
      </section>
    </DialogBackdrop>
  )
}
