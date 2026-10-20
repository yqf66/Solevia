import { useEffect, useState } from 'react'
import type { FormEvent, ReactNode } from 'react'
import { getWorkspaceHolidays, type ExecutionCalendar, type FocusSession, type QuickCapture, type TodayWorkspace, type HolidayCalendarDay } from '../api'
import { parseLocalDate, dateInputValue, friendlyDate } from '../ui/plannerPresentation'

const calendarWeekdays = ['一', '二', '三', '四', '五', '六', '日']

const calendarStatusLabels: Record<NonNullable<ExecutionCalendar>['days'][number]['status'], string> = {
  PLANNED: '正常安排',
  IN_PROGRESS: '正在进行',
  BLOCKED: '遇到阻塞',
  COMPLETED: '已完成',
  PARTIAL: '部分未完成',
  MISSED: '整天中断',
}

const calendarPolicyLabels: Record<string, string> = {
  EXACT_DAY: '固定执行',
  CHECKPOINT: '检查点',
  CADENCE: '节奏建议',
  DATE_WINDOW: '期限窗口',
  QUOTA: '总量要求',
  TASK_POOL: '候选任务',
  TRIGGERED: '触发后安排',
  FLOATING: '弹性建议',
}

const hardCalendarPolicies = new Set(['EXACT_DAY', 'CHECKPOINT'])
const cadenceCalendarPolicies = new Set(['CADENCE'])
const windowCalendarPolicies = new Set(['DATE_WINDOW'])

function calendarBinding(tasks: NonNullable<ExecutionCalendar>['days'][number]['tasks']) {
  if (!tasks.some((task) => task.scheduling_policy)) return 'legacy'
  const policies = new Set(tasks.map((task) => task.scheduling_policy ?? 'EXACT_DAY'))
  if (policies.size === 0 || [...policies].every((policy) => hardCalendarPolicies.has(policy))) return 'fixed'
  if ([...policies].some((policy) => hardCalendarPolicies.has(policy))) return 'mixed'
  if ([...policies].every((policy) => cadenceCalendarPolicies.has(policy))) return 'cadence'
  if ([...policies].every((policy) => windowCalendarPolicies.has(policy))) return 'window'
  return 'flexible'
}

const calendarBindingLabels: Record<string, string> = {
  legacy: '',
  fixed: '固定执行日',
  mixed: '固定任务 + 建议任务',
  cadence: '节奏建议日',
  window: '期限窗口建议日',
  flexible: '弹性建议日',
}



export function ExecutionCalendarWidget({ calendar, selectedDate, onSelectDate }: {
  calendar: ExecutionCalendar | null
  selectedDate: string
  onSelectDate: (value: string) => void
}) {
  const [visibleMonth, setVisibleMonth] = useState(() => {
    const selected = parseLocalDate(selectedDate)
    return new Date(selected.getFullYear(), selected.getMonth(), 1)
  })
  const [holidayDays, setHolidayDays] = useState<HolidayCalendarDay[]>([])

  useEffect(() => {
    const first = new Date(visibleMonth.getFullYear(), visibleMonth.getMonth(), 1)
    const last = new Date(visibleMonth.getFullYear(), visibleMonth.getMonth() + 1, 0)
    getWorkspaceHolidays(dateInputValue(first), dateInputValue(last))
      .then((result) => setHolidayDays(Array.isArray(result?.days) ? result.days : []))
      .catch(() => setHolidayDays([]))
  }, [visibleMonth])

  const scheduledByDate = new Map(calendar?.days.map((item) => [item.scheduled_date, item]) ?? [])
  const holidayByDate = new Map(holidayDays.map((item) => [item.date, item]))
  const first = new Date(visibleMonth.getFullYear(), visibleMonth.getMonth(), 1)
  const firstGridDay = new Date(first)
  firstGridDay.setDate(first.getDate() - ((first.getDay() + 6) % 7))
  const cells = Array.from({ length: 42 }, (_, index) => {
    const value = new Date(firstGridDay)
    value.setDate(firstGridDay.getDate() + index)
    return value
  })

  function moveMonth(offset: number) {
    setVisibleMonth((current) => new Date(current.getFullYear(), current.getMonth() + offset, 1))
  }

  return (
    <section className="calendar-widget" aria-label="执行日志日历">
      <header>
        <button type="button" aria-label="上个月" onClick={() => moveMonth(-1)}>‹</button>
        <strong>{visibleMonth.getFullYear()} 年 {visibleMonth.getMonth() + 1} 月</strong>
        <button type="button" aria-label="下个月" onClick={() => moveMonth(1)}>›</button>
      </header>
      <div className="calendar-weekdays" aria-hidden="true">{calendarWeekdays.map((item) => <span key={item}>{item}</span>)}</div>
      <div className="calendar-grid">
        {cells.map((cell) => {
          const value = dateInputValue(cell)
          const scheduled = scheduledByDate.get(value)
          const holiday = holidayByDate.get(value)
          const outside = cell.getMonth() !== visibleMonth.getMonth()
          const recovered = Boolean(scheduled?.tasks.some(task => task.status === 'CARRIED_OVER')
            && scheduled.tasks.every(task => ['CARRIED_OVER', 'COMPLETED', 'CANCELLED'].includes(task.status)))
          const status = recovered ? 'recovered' : scheduled?.status.toLowerCase() ?? 'empty'
          const binding = scheduled && !recovered ? calendarBinding(scheduled.tasks.filter(task => task.status !== 'CARRIED_OVER')) : 'empty'
          const bindingLabel = scheduled ? calendarBindingLabels[binding] : '留白日'
          const statusLabel = recovered ? '遗留已转移' : scheduled ? calendarStatusLabels[scheduled.status] : '无任务'
          const taskStages = scheduled?.task_stage_orders ?? []
          const taskStageLabel = taskStages.length ? `当天任务来自 ${taskStages.map(stage => `M${stage}`).join('、')}` : ''
          return (
            <button
              className={`calendar-day ${status} binding-${binding}${holiday ? ` holiday-${holiday.kind.toLowerCase()}` : ''}${outside ? ' outside' : ''}${value === selectedDate ? ' selected' : ''}${scheduled?.changes?.length ? ' has-change' : ''}`}
              type="button"
              aria-label={`${value}，${holiday ? `${holiday.local_name}，${holiday.kind === 'HOLIDAY' ? '法定休息日' : '调休工作日'}，` : ''}${scheduled ? `${bindingLabel ? `${bindingLabel}，` : ''}${statusLabel}：${scheduled.primary_goal}${taskStageLabel ? `；${taskStageLabel}` : ''}` : '无任务'}`}
              aria-pressed={value === selectedDate}
              onClick={() => onSelectDate(value)}
              key={value}
            >
              <span>{cell.getDate()}</span>
              {holiday && <em aria-hidden="true">{holiday.kind === 'HOLIDAY' ? '休' : '班'}</em>}
              {scheduled && <i aria-hidden="true" />}
              <div className="calendar-tooltip" role="tooltip">
                {holiday && <div className={`holiday-tooltip-label ${holiday.kind.toLowerCase()}`}><b>{holiday.local_name}</b><span>{holiday.kind === 'HOLIDAY' ? '法定休息日' : '调休工作日'}</span></div>}
                <strong>{scheduled ? `日历位置：${scheduled.code} · ${bindingLabel ? `${bindingLabel} · ` : ''}${statusLabel}` : '留白日'}</strong>
                {taskStageLabel && <p>{taskStageLabel}</p>}
                <p>{scheduled?.primary_goal ?? '当天没有安排任务'}</p>
                {scheduled && <ul>{scheduled.tasks.map((task) => <li key={task.id}><span>{task.status === 'COMPLETED' ? '✓' : task.status === 'CARRIED_OVER' ? '↗' : '·'}</span>{task.title}<small>{task.origin_stage_order != null && `原属 M${task.origin_stage_order} · `}{calendarPolicyLabels[task.scheduling_policy ?? 'EXACT_DAY']} · {task.status === 'CARRIED_OVER' ? `已转入后续 · ${task.estimate_minutes} 分钟` : `${task.estimate_minutes} 分钟`}</small></li>)}</ul>}
                {scheduled?.changes?.map((change, index) => {
                  const directionLabels = {
                    MOVED_FROM: '从当天移出',
                    MOVED_TO: '移入当天',
                    SPLIT_FROM: '从当天拆分',
                    SPLIT_TO: '拆分到当天',
                    ADDED: '新增到当天',
                  }
                  return <section className="calendar-change" key={`${change.direction}-${index}`}><b>{directionLabels[change.direction]}{change.counterpart_date ? ` · ${friendlyDate(change.counterpart_date)}` : ''}</b><span>{change.summary}</span><small>原因：{change.rationale}</small></section>
                })}
              </div>
            </button>
          )
        })}
      </div>
      <p className="muted">M 编号表示日历中的阶段位置；顺延或提前安排后，当天任务可能来自其他阶段，以各项任务的归属为准。</p>
      <footer><span><i className="has-task" />固定</span><span><i className="suggested-task" />建议</span><span><i className="no-task" />无任务</span><span><i className="done" />已完成</span><span><i className="partial" />部分未完成</span><span><i className="missed" />整天中断</span><span><i className="changed" />遗留已转移 / 有调整</span><span><i className="holiday-legend" />节假日</span></footer>
    </section>
  )
}

export { FocusTimerWidget } from './FocusTimerWidget'

export function QuickCaptureWidget({ captures, onAdd, onDelete }: {
  captures: QuickCapture[]
  onAdd: (content: string) => Promise<void>
  onDelete: (captureId: string) => Promise<void>
}) {
  const [content, setContent] = useState('')
  const [busy, setBusy] = useState(false)
  const [notebookOpen, setNotebookOpen] = useState(false)

  async function submit(event: FormEvent) {
    event.preventDefault()
    if (!content.trim() || busy) return
    setBusy(true)
    try {
      await onAdd(content.trim())
      setContent('')
    } finally {
      setBusy(false)
    }
  }

  const grouped = captures.reduce<Record<string, QuickCapture[]>>((result, capture) => {
    const key = dateInputValue(new Date(capture.created_at))
    result[key] = [...(result[key] ?? []), capture]
    return result
  }, {})

  return <section className="capture-widget" aria-label="点子备忘录"><header><div><strong>突然想到什么，就先记下来</strong><p>这里只保存与这个目标有关的想法，不会触发反馈，也不会自动修改计划。</p></div><span>{captures.length}</span></header><form onSubmit={(event) => void submit(event)}><textarea aria-label="点子内容" value={content} maxLength={500} onChange={(event) => setContent(event.target.value)} placeholder="例如：复习链表时想到可以画一张指针变化图…" /><button className="button primary" disabled={!content.trim() || busy} type="submit">{busy ? '保存中…' : '记下这个点子'}</button></form><button className="notebook-toggle" type="button" aria-expanded={notebookOpen} onClick={() => setNotebookOpen((current) => !current)}><span aria-hidden="true">▤</span><strong>点子笔记本</strong><small>{captures.length ? `共 ${captures.length} 条` : '还没有记录'}</small><i aria-hidden="true">{notebookOpen ? '⌃' : '⌄'}</i></button>{notebookOpen && <div className="capture-notebook">{captures.length === 0 ? <p>笔记本还是空的。之后冒出的想法都可以放在这里，不必立刻变成任务。</p> : Object.entries(grouped).map(([day, items]) => <section key={day}><time>{friendlyDate(day)}</time><div>{items.map((capture) => <article key={capture.id}><div><p>{capture.content}</p><small>{new Date(capture.created_at).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' })}</small></div><button type="button" aria-label={`删除点子：${capture.content}`} onClick={() => void onDelete(capture.id)}>删除</button></article>)}</div></section>)}</div>}</section>
}

export function CollapsibleTool({ icon, title, summary, children, defaultOpen = false }: {
  icon: ReactNode
  title: string
  summary: string
  children: ReactNode
  defaultOpen?: boolean
}) {
  return <details className="collapsible-tool" open={defaultOpen}><summary><span aria-hidden="true">{icon}</span><div><strong>{title}</strong><small>{summary}</small></div><i aria-hidden="true">⌄</i></summary><div className="collapsible-tool-body">{children}</div></details>
}

export function ExecutionInsightWidget({ workspace, sessions }: { workspace: TodayWorkspace; sessions: FocusSession[] }) {
  const todayKey = workspace.today
  const todaySessions = sessions.filter((session) => dateInputValue(new Date(session.completed_at)) === todayKey)
  const focusedMinutes = Math.round(todaySessions.reduce((total, session) => total + session.actual_seconds, 0) / 60)
  const estimatedMinutes = todaySessions.reduce((total, session) => total + session.planned_minutes, 0)
  const delta = focusedMinutes - estimatedMinutes
  const loadLabels = { REST_DAY: '缓冲日', DONE: '已完成', HEAVY: '负荷偏高', MANAGEABLE: '节奏合适' }
  const remainingTasks = workspace.day?.tasks.filter((task) => task.status !== 'COMPLETED') ?? []
  const fallbackPlannedMinutes = workspace.day?.tasks.reduce((total, task) => total + task.estimate_minutes, 0) ?? 0
  const fallbackRemainingMinutes = remainingTasks.reduce((total, task) => total + task.estimate_minutes, 0)
  const capacity = workspace.capacity ?? {
    planned_minutes: fallbackPlannedMinutes,
    remaining_minutes: fallbackRemainingMinutes,
    remaining_tasks: remainingTasks.length,
    load_status: workspace.day ? (remainingTasks.length ? 'MANAGEABLE' : 'DONE') : 'REST_DAY',
  }
  return <section className="insight-widget" aria-label="今日执行洞察"><header><div><p className="eyebrow">今日余量</p><strong>{capacity.remaining_minutes} 分钟</strong></div><span className={capacity.load_status.toLowerCase()}>{loadLabels[capacity.load_status]}</span></header><div className="capacity-bar" aria-label={`剩余 ${capacity.remaining_minutes} 分钟`}><i style={{ width: `${capacity.planned_minutes ? Math.max(0, Math.min(100, (capacity.planned_minutes - capacity.remaining_minutes) / capacity.planned_minutes * 100)) : 0}%` }} /></div><div className="insight-stats"><span>待完成<strong>{capacity.remaining_tasks} 项</strong></span><span>今日计划<strong>{capacity.planned_minutes} 分钟</strong></span><span>实际专注<strong>{focusedMinutes} 分钟</strong></span></div><section><strong>估时校准</strong>{todaySessions.length === 0 ? <p>完成专注计时后，会对比建议时长与实际投入。</p> : <p>已记录 {todaySessions.length} 轮，实际比设定{delta === 0 ? '刚好一致' : `${delta > 0 ? '多' : '少'} ${Math.abs(delta)} 分钟`}。这会帮助后续计划更贴近你的真实节奏。</p>}</section></section>
}
