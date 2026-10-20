import { DialogBackdrop } from '../ui/DialogBackdrop'
import { useRef, useState } from 'react'
import { X } from 'lucide-react'
import { recordPlanTaskProgress, type TodayWorkspace } from '../api'
import { ModalLayer } from '../ui/ModalLayer'

export type TaskProgressTarget = {
  id: string; title: string; estimate_minutes: number
  action: 'PARTIAL' | 'DEFER' | 'SKIP'; skip_guidance?: string[]
}

const titles = { PARTIAL: '记录部分完成', DEFER: '今天不做', SKIP: '本周期不做' }

export function TaskProgressDialog({ goalId, target, disabled, onClose, onSaved }: {
  goalId: string; target: TaskProgressTarget; disabled: boolean
  onClose: () => void; onSaved: (workspace: TodayWorkspace, notice: string) => void
}) {
  const [minutes, setMinutes] = useState(String(Math.max(1, Math.min(30, target.estimate_minutes))))
  const [reason, setReason] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const pending = useRef(false)
  const partial = target.action === 'PARTIAL'
  const amount = Number(minutes)
  const valid = partial ? minutes !== '' && Number.isInteger(amount) && amount >= 1 && amount <= 1440 : Boolean(reason.trim())
  async function save() {
    if (pending.current || disabled || !valid) return
    pending.current = true; setBusy(true); setError('')
    try {
      const result = await recordPlanTaskProgress(goalId, target.id, {
        action: target.action,
        ...(partial ? { amount, unit: 'MINUTES' as const } : {}),
        ...(reason.trim() ? { reason: reason.trim() } : {}),
      })
      onSaved(result, partial ? `已为“${target.title}”记录本次投入 ${amount} 分钟。` : `已为“${target.title}”记录${titles[target.action]}。`)
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : '暂时无法保存，请重试。')
    } finally { pending.current = false; setBusy(false) }
  }
  return <ModalLayer><DialogBackdrop className="inline-dialog-backdrop" onDismiss={onClose}> <form className="inline-dialog task-progress-dialog" role="dialog" aria-modal="true" aria-labelledby="task-progress-title" onSubmit={(event) => { event.preventDefault(); void save() }}>
    <header><h2 id="task-progress-title">{titles[target.action]}</h2><button className="icon-button" type="button" aria-label="关闭进度记录" title="关闭" disabled={busy} onClick={onClose}><X size={18} /></button></header>
    <strong>{target.title}</strong>
    <p>{partial ? '记录本次投入，保留剩余任务；不会直接标记整项完成。' : target.action === 'DEFER' ? '今天先不做，任务与周期要求会保留；不会自动改期。' : '跳过当前执行卡，保留已有进度和后续调整依据。'}</p>
    {partial && <><label htmlFor="task-progress-minutes">本次投入（分钟）</label><input id="task-progress-minutes" type="number" min={1} max={1440} step={1} required autoFocus value={minutes} disabled={busy} onChange={(event) => setMinutes(event.target.value)} /></>}
    <label htmlFor="task-progress-reason">{partial ? '进展说明（可选）' : '原因'}</label><textarea id="task-progress-reason" maxLength={500} required={!partial} autoFocus={!partial} value={reason} disabled={busy} onChange={(event) => setReason(event.target.value)} placeholder={partial ? '例如：完成了前半部分，剩余内容下次继续' : '例如：今天临时加班'} />
    {target.action === 'SKIP' && Boolean(target.skip_guidance?.length) && <p>{target.skip_guidance?.join('；')}</p>}
    {error && <p className="form-error" role="alert">{error}</p>}
    <footer className="inline-dialog-actions"><button className="button" type="button" disabled={busy} onClick={onClose}>取消</button><button className="button primary" type="submit" disabled={busy || disabled || !valid}>{busy ? '正在保存…' : partial ? '保存进度' : '确认记录'}</button></footer>
  </form></DialogBackdrop></ModalLayer>
}
