import { TimePicker } from '../ui/TimePicker'
import { DialogBackdrop } from '../ui/DialogBackdrop'
import { useEffect, useId, useRef } from 'react'
import { X } from 'lucide-react'
import { ModalLayer } from '../ui/ModalLayer'
import type { TodayDashboardState } from './useTodayDashboard'

type Props = { dashboard: TodayDashboardState }

export function ScheduleTaskDialog({ dashboard }: Props) {
  const {
    scheduleTarget: task, setScheduleTarget, scheduleStart, setScheduleStart,
    scheduleEnd, setScheduleEnd, scheduleConflict, setScheduleConflict,
    parallelReason, setParallelReason, scheduleBusy, schedule, error,
  } = dashboard
  const root = useRef<HTMLFormElement>(null)
  const titleId = useId()

  useEffect(() => {
    const previous = document.activeElement as HTMLElement | null
    root.current?.querySelector<HTMLInputElement>('input')?.focus()
    return () => previous?.focus()
  }, [])

  if (!task) return null
  const close = () => { if (!scheduleBusy) setScheduleTarget(null) }

  return <ModalLayer>
    <DialogBackdrop className="inline-dialog-backdrop" onDismiss={close}> 
      <form ref={root} className="inline-dialog schedule-dialog" role="dialog" aria-modal="true" aria-labelledby={titleId}
        onSubmit={event => { event.preventDefault(); void schedule(task, scheduleConflict) }}
        onKeyDown={event => {
          if (event.key === 'Escape') close()
          if (event.key !== 'Tab') return
          const items = [...(root.current?.querySelectorAll<HTMLElement>('button:not(:disabled), input, textarea') ?? [])]
          if (event.shiftKey && document.activeElement === items[0]) { event.preventDefault(); items.at(-1)?.focus() }
          else if (!event.shiftKey && document.activeElement === items.at(-1)) { event.preventDefault(); items[0]?.focus() }
        }}>
        <header>
          <div><h2 id={titleId}>{task.title}</h2><p className="muted">安排时间 · {task.plan_title} · {task.estimate_minutes} 分钟</p></div>
          <button className="icon-button" type="button" aria-label="关闭" disabled={scheduleBusy} onClick={close}><X size={18} aria-hidden="true" /></button>
        </header>
        <div className="schedule-fields">
          <label>开始时间<TimePicker value={scheduleStart} onChange={event => { setScheduleStart(event.target.value); setScheduleConflict(false) }} required /></label>
          <label>结束时间<TimePicker value={scheduleEnd} onChange={event => { setScheduleEnd(event.target.value); setScheduleConflict(false) }} required /></label>
        </div>
        {scheduleConflict && <div className="schedule-conflict-panel">
          <strong>这个时段与已有任务重叠</strong>
          <p>换一个空闲时间，或确认这两个任务可以并行执行。</p>
          <label>并行原因<textarea value={parallelReason} onChange={event => setParallelReason(event.target.value)} placeholder="例如：已有任务是等待型工作" required /></label>
        </div>}
        {error && <p className="form-error" role="alert">{error}</p>}
        <div className="inline-dialog-actions">
          <button className="button" type="button" disabled={scheduleBusy} onClick={close}>取消</button>
          <button className="button primary" type="submit" disabled={scheduleBusy || scheduleStart >= scheduleEnd || (scheduleConflict && !parallelReason.trim())}>
            {scheduleBusy ? '正在保存…' : scheduleConflict ? '确认并行安排' : '保存时间'}
          </button>
        </div>
      </form>
    </DialogBackdrop>
  </ModalLayer>
}
