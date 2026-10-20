import { useEffect, useRef, useState } from 'react'
import { Check } from 'lucide-react'
import type { FocusSession, PlanDay } from '../api'
import { FocusFullscreen } from './FocusFullscreen'
import './FocusFullscreen.css'

export function FocusTimerWidget({ tasks, sessions, onSave }: {
  tasks: PlanDay['tasks']
  sessions: FocusSession[]
  onSave: (input: { task_id: string | null; planned_minutes: number; actual_seconds: number; interruptions: number }) => Promise<void>
}) {
  const [durationMinutes, setDurationMinutes] = useState(25)
  const [secondsLeft, setSecondsLeft] = useState(25 * 60)
  const [running, setRunning] = useState(false)
  const [selectedTaskId, setSelectedTaskId] = useState<string | null>(null)
  const [interruptions, setInterruptions] = useState(0)
  const [started, setStarted] = useState(false)
  const [recorded, setRecorded] = useState(false)
  const [notice, setNotice] = useState('')
  const autoSavingRef = useRef(false)
  const deadline = useRef(0)
  const [fullscreen, setFullscreen] = useState(false)
  const fullscreenOpen = useRef(false)
  const ownsFullscreen = useRef(false)

  function enterFullscreen() {
    if (fullscreenOpen.current) return
    fullscreenOpen.current = true
    setFullscreen(true)
    if (!document.fullscreenElement && document.documentElement.requestFullscreen) {
      ownsFullscreen.current = true
      void document.documentElement.requestFullscreen().then(() => {
        if (!fullscreenOpen.current && document.fullscreenElement) void document.exitFullscreen().catch(() => {})
      }).catch(() => { ownsFullscreen.current = false })
    }
  }

  function leaveFullscreen() {
    fullscreenOpen.current = false
    setFullscreen(false)
    if (ownsFullscreen.current && document.fullscreenElement) void document.exitFullscreen().catch(() => {})
    ownsFullscreen.current = false
  }

  useEffect(() => () => {
    fullscreenOpen.current = false
    if (ownsFullscreen.current && document.fullscreenElement) void document.exitFullscreen().catch(() => {})
  }, [])

  useEffect(() => {
    if (!running) return
    const timer = window.setInterval(() => {
      const remaining = Math.max(0, Math.ceil((deadline.current - Date.now()) / 1000))
      setSecondsLeft(remaining)
      if (!remaining) setRunning(false)
    }, 250)
    return () => window.clearInterval(timer)
  }, [running])

  useEffect(() => {
    if (secondsLeft !== 0 || !started || recorded || autoSavingRef.current) return
    autoSavingRef.current = true
    void onSave({
      task_id: selectedTaskId,
      planned_minutes: durationMinutes,
      actual_seconds: durationMinutes * 60,
      interruptions,
    }).then(() => {
      setRecorded(true)
      setNotice('本轮专注已自动保存')
    }).catch(() => {
      autoSavingRef.current = false
      setNotice('保存失败，请点击“结束并记录”重试')
    })
  }, [durationMinutes, interruptions, onSave, recorded, secondsLeft, selectedTaskId, started])

  function chooseDuration(minutes: number, taskId: string | null = null) {
    const normalized = Math.min(180, Math.max(1, Math.round(minutes)))
    setDurationMinutes(normalized)
    setSecondsLeft(normalized * 60)
    setRunning(false)
    setSelectedTaskId(taskId)
    setInterruptions(0)
    setStarted(false)
    setRecorded(false)
    autoSavingRef.current = false
    setNotice('')
  }

  function toggleTimer() {
    if (running) {
      setSecondsLeft(Math.max(0, Math.ceil((deadline.current - Date.now()) / 1000)))
      setRunning(false)
      setInterruptions((current) => current + 1)
      return
    }
    if (secondsLeft === 0) {
      setSecondsLeft(durationMinutes * 60)
      setInterruptions(0)
      setRecorded(false)
    }
    setStarted(true)
    setNotice('')
    setRunning(true)
    deadline.current = Date.now() + (secondsLeft === 0 ? durationMinutes * 60 : secondsLeft) * 1000
    enterFullscreen()
  }

  async function finishAndSave() {
    const remaining = running ? Math.max(0, Math.ceil((deadline.current - Date.now()) / 1000)) : secondsLeft
    const actualSeconds = Math.max(1, durationMinutes * 60 - remaining)
    setSecondsLeft(remaining)
    setRunning(false)
    setRecorded(true)
    autoSavingRef.current = true
    try {
      await onSave({ task_id: selectedTaskId, planned_minutes: durationMinutes, actual_seconds: actualSeconds, interruptions })
      setNotice('本轮专注已保存')
      leaveFullscreen()
    } catch {
      setRecorded(false)
      autoSavingRef.current = false
      setNotice('保存失败，请稍后重试')
    }
  }

  const minutes = Math.floor(secondsLeft / 60).toString().padStart(2, '0')
  const seconds = (secondsLeft % 60).toString().padStart(2, '0')
  const progress = 1 - secondsLeft / (durationMinutes * 60)
  const clock = <div className="focus-clock">
    <svg className="focus-clock-ring" viewBox="0 0 180 180" aria-hidden="true"><circle cx="90" cy="90" r="80" /><circle cx="90" cy="90" r="80" strokeDasharray={`${progress * 502.655} 502.655`} /></svg>
    <div><time>{minutes}:{seconds}</time><span>{running ? '保持专注' : secondsLeft === 0 ? '本轮完成' : started ? '已暂停' : '准备开始'}</span></div>
  </div>

  return (
    <section className="focus-widget" aria-label="番茄专注计时器" aria-hidden={fullscreen || undefined} inert={fullscreen}>
      <div className="focus-presets"><button className={durationMinutes === 25 && !selectedTaskId ? 'selected' : ''} type="button" onClick={() => chooseDuration(25)}>专注 25</button><button className={durationMinutes === 5 && !selectedTaskId ? 'selected' : ''} type="button" onClick={() => chooseDuration(5)}>休息 5</button><label><span>自定</span><input aria-label="自定义专注分钟" type="number" min="1" max="180" value={durationMinutes} onChange={(event) => { const value = Number(event.target.value); if (Number.isFinite(value) && value > 0) chooseDuration(value) }} /></label></div>
      {tasks.length > 0 && <div className="focus-task-options"><span>按今日任务计时</span>{tasks.map((task) => <button className={selectedTaskId === task.id ? 'selected' : ''} type="button" onClick={() => chooseDuration(task.estimate_minutes, task.id)} key={task.id}><span>{task.status === 'COMPLETED' ? <Check size={14} aria-label="已完成" /> : task.order}</span><strong>{task.title}</strong><small>{task.estimate_minutes} 分钟</small></button>)}</div>}
      {clock}
      <div className="focus-actions"><button className="button primary" type="button" onClick={toggleTimer}>{running ? '暂停' : secondsLeft === 0 ? '重新开始' : started ? '继续专注' : '开始专注'}</button>{started && !recorded && <button className="button" type="button" onClick={() => void finishAndSave()}>结束并记录</button>}<button className="button" type="button" onClick={() => chooseDuration(durationMinutes, selectedTaskId)}>重置</button></div>
      {started && <button className="button focus-view-button" type="button" onClick={enterFullscreen}>进入全屏专注</button>}
      {fullscreen && <FocusFullscreen clock={clock} title={tasks.find(task => task.id === selectedTaskId)?.title ?? (durationMinutes === 5 ? '短暂休息，留一点余地' : '专注于眼前的这一小步')} running={running} finished={secondsLeft === 0} notice={notice} onToggle={toggleTimer} onFinish={started && !recorded ? () => void finishAndSave() : undefined} onClose={leaveFullscreen} />}
      {notice && <p className="focus-notice" role="status">{notice}</p>}
      <div className="focus-history"><header><strong>最近专注</strong><span>{sessions.length} 轮</span></header>{sessions.length === 0 ? <p>完成第一轮后，这里会保留实际用时。</p> : sessions.slice(0, 3).map((session) => { const actual = Math.max(1, Math.round(session.actual_seconds / 60)); const delta = actual - session.planned_minutes; return <article key={session.id}><div><strong>{session.task_title ?? '自由专注'}</strong><small>{new Date(session.completed_at).toLocaleString('zh-CN', { month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit' })}{session.interruptions ? ` · 中断 ${session.interruptions} 次` : ''}</small></div><span>{actual} / {session.planned_minutes} 分钟</span><em className={delta > 0 ? 'over' : ''}>{delta === 0 ? '刚好' : `${delta > 0 ? '+' : ''}${delta} 分钟`}</em></article> })}</div>
    </section>
  )
}

