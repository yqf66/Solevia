import { TimePicker } from '../ui/TimePicker'
import { DialogBackdrop } from '../ui/DialogBackdrop'
import { useEffect, useState } from 'react'
import type { CSSProperties } from 'react'
import { useNavigate } from 'react-router-dom'
import { listManualPlans, getUnifiedToday, updateManualTask, deleteManualTask, type ManualPlan, type ManualTask, type UnifiedToday, type FocusSession, type QuickCapture, type ExecutionCalendar, type PlanDay } from '../api'
import { dateInputValue, friendlyDate, parseLocalDate } from '../ui/plannerPresentation'
import { ExecutionCalendarWidget, CollapsibleTool, FocusTimerWidget, QuickCaptureWidget } from './ExecutionTools'
import './ExecutionRefinement.css'
import './ExecutionIdentity.css'

const weekdays = ['周一', '周二', '周三', '周四', '周五', '周六', '周日']

export function FixedTasksPage() {
  const navigate = useNavigate()
  const actualToday = dateInputValue(new Date())
  const [selectedDate, setSelectedDate] = useState(actualToday)
  const [plans, setPlans] = useState<ManualPlan[]>([])
  const [board, setBoard] = useState<UnifiedToday | null>(null)
  const [editing, setEditing] = useState<{ plan: ManualPlan; task: ManualTask } | null>(null)
  const [editTitle, setEditTitle] = useState('')
  const [editStart, setEditStart] = useState('')
  const [editEnd, setEditEnd] = useState('')
  const [editScope, setEditScope] = useState<'TODAY' | 'FUTURE'>('TODAY')
  const [editConflicts, setEditConflicts] = useState<Array<{ existing_title: string; existing_start: string; existing_end: string }> | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [focusSessions, setFocusSessions] = useState<FocusSession[]>([])
  const [captures, setCaptures] = useState<QuickCapture[]>([])

  async function refresh(date = selectedDate) {
    const [nextPlans, nextBoard] = await Promise.all([listManualPlans(), getUnifiedToday(date)])
    setPlans(nextPlans.filter((plan) => plan.status === 'ACTIVE'))
    setBoard(nextBoard)
  }

  useEffect(() => { void refresh(selectedDate).catch((caught) => setError(caught instanceof Error ? caught.message : '暂时无法读取固定任务。')) }, [selectedDate])
  useEffect(() => {
    try { setCaptures(JSON.parse(localStorage.getItem('plan-a-fixed-captures') || '[]') as QuickCapture[]) } catch { setCaptures([]) }
  }, [])

  const fixedRules = plans.flatMap((plan) => plan.tasks.filter((task) => task.start_local && task.end_local).map((task) => ({ plan, task })))
  const fixedRuleIds = new Set(fixedRules.map(({ task }) => task.id))
  const dateTasks = board ? [...board.fixed_tasks, ...board.flexible_tasks].filter((task) => task.source === 'QUICK' && fixedRuleIds.has(task.id)) : []
  const completed = dateTasks.filter((task) => task.status === 'COMPLETED').length
  const percentage = dateTasks.length ? Math.round(completed / dateTasks.length * 100) : 0
  const ruleCards = fixedRules.map(({ plan, task }) => {
    const visible = dateTasks.find((item) => item.id === task.id)
    return { plan, task, visible, occursOnDate: Boolean(visible), done: visible?.status === 'COMPLETED' }
  })

  function occurs(task: ManualTask, value: string) {
    if (!task.scheduled_date || value < task.scheduled_date || (task.recurrence_end_date && value > task.recurrence_end_date)) return false
    const day = parseLocalDate(value).getDay(); const weekday = (day + 6) % 7
    return task.recurrence === 'DAILY' || task.recurrence === 'WEEKLY' && task.recurrence_weekdays.includes(weekday) || task.recurrence === 'ONCE' && value === task.scheduled_date
  }

  const calendarStart = parseLocalDate(actualToday); calendarStart.setDate(calendarStart.getDate() - 120)
  const calendarEnd = parseLocalDate(actualToday); calendarEnd.setDate(calendarEnd.getDate() + 365)
  const calendarDays: ExecutionCalendar['days'] = []
  for (const cursor = new Date(calendarStart); cursor <= calendarEnd; cursor.setDate(cursor.getDate() + 1)) {
    const value = dateInputValue(cursor)
    const items = fixedRules.flatMap(({ task }) => {
      if (!occurs(task, value)) return []
      const override = task.overrides?.find((item) => item.override_date === value)
      if (override?.is_deleted) return []
      return [{ ...task, title: override?.title ?? task.title, status: override?.status ?? task.status, override }]
    })
    if (!items.length) continue
    const allDone = items.every((item) => item.status === 'COMPLETED')
    calendarDays.push({ plan_day_id: `manual-${value}`, scheduled_date: value, code: '固定任务', primary_goal: `${items.length} 项固定安排`, status: allDone ? 'COMPLETED' : value < actualToday ? 'MISSED' : value === actualToday ? 'IN_PROGRESS' : 'PLANNED', suggested_minutes: items.reduce((sum, item) => sum + item.estimate_minutes, 0), tasks: items.map((item) => ({ id: item.id, title: item.title, status: item.status, is_required: true, estimate_minutes: item.estimate_minutes })), changes: items.filter((item) => item.override).map((item) => ({ adjustment_kind: 'DATE_OVERRIDE', direction: 'ADDED' as const, summary: `${item.title} 当天安排已调整`, rationale: '用户只修改了这一天', counterpart_date: null })) })
  }
  const calendar: ExecutionCalendar = { first_date: dateInputValue(calendarStart), last_date: dateInputValue(calendarEnd), days: calendarDays }
  const focusTasks: PlanDay['tasks'] = dateTasks.map((task, index) => ({ id: task.id, order: index + 1, title: task.title, description: task.description, estimate_minutes: task.estimate_minutes, priority: task.priority, task_kind: 'FIXED', is_required: true, evidence: [], depends_on: [], status: task.status }))

  function openEditor(plan: ManualPlan, task: ManualTask, visible?: UnifiedToday['fixed_tasks'][number]) {
    setEditing({ plan, task }); setEditTitle(visible?.title ?? task.title); setEditStart(visible?.start_local ?? task.start_local ?? ''); setEditEnd(visible?.end_local ?? task.end_local ?? ''); setEditScope('TODAY'); setEditConflicts(null); setError('')
  }

  async function saveEdit(mode: 'NONE' | 'PARALLEL' | 'PUSH' = 'NONE') {
    if (!editing) return
    setBusy(true); setError('')
    try {
      await updateManualTask(editing.plan.id, editing.task.id, { title: editTitle.trim(), scheduled_date: selectedDate, start_local: editStart || null, end_local: editEnd || null, scope: editScope, conflict_mode: mode })
      setEditing(null); setEditConflicts(null); await refresh()
    } catch (caught) {
      const failure = caught as Error & { code?: string; detail?: { conflicts?: Array<{ existing_title: string; existing_start: string; existing_end: string }> } }
      if (failure.code === 'MANUAL_PLAN_CONFLICT' && failure.detail?.conflicts) setEditConflicts(failure.detail.conflicts)
      else setError(failure.message || '暂时无法保存调整。')
    } finally { setBusy(false) }
  }

  async function deleteEdited() {
    if (!editing) return
    setBusy(true)
    try { await deleteManualTask(editing.plan.id, editing.task.id, { scope: editScope, override_date: selectedDate }); setEditing(null); await refresh() }
    catch (caught) { setError(caught instanceof Error ? caught.message : '暂时无法删除任务。') }
    finally { setBusy(false) }
  }

  async function toggleComplete(taskId: string, nextStatus: 'NOT_STARTED' | 'COMPLETED') {
    const owner = fixedRules.find(({ task }) => task.id === taskId)
    if (!owner) return
    setBusy(true)
    try { await updateManualTask(owner.plan.id, taskId, { scheduled_date: selectedDate, scope: 'TODAY', status: nextStatus }); await refresh() }
    catch (caught) { setError(caught instanceof Error ? caught.message : '暂时无法更新完成状态。') }
    finally { setBusy(false) }
  }

  async function saveFocus(input: { task_id: string | null; planned_minutes: number; actual_seconds: number; interruptions: number }) {
    const task = dateTasks.find((item) => item.id === input.task_id)
    setFocusSessions((items) => [{ id: crypto.randomUUID(), task_id: input.task_id, task_title: task?.title ?? null, planned_minutes: input.planned_minutes, actual_seconds: input.actual_seconds, interruptions: input.interruptions, status: input.actual_seconds < input.planned_minutes * 60 ? 'ENDED_EARLY' : 'COMPLETED', completed_at: new Date().toISOString() }, ...items])
  }

  async function addCapture(content: string) { const next = [{ id: crypto.randomUUID(), content, status: 'INBOX' as const, created_at: new Date().toISOString() }, ...captures]; setCaptures(next); localStorage.setItem('plan-a-fixed-captures', JSON.stringify(next)) }
  async function removeCapture(id: string) { const next = captures.filter((item) => item.id !== id); setCaptures(next); localStorage.setItem('plan-a-fixed-captures', JSON.stringify(next)) }

  return <section className="page planner-stage-page today-page fixed-tasks-page">
    <div className="planner-primary-column">
      <header className="planner-stage-header execution-header"><div className="stage-heading"><p className="eyebrow">快速安排 · 每日执行</p><h1>{selectedDate === actualToday ? '今天的快速任务' : `${friendlyDate(selectedDate)}的快速任务`}</h1></div><div className="execution-header-actions"><button className="button" type="button" onClick={() => navigate('/quick/new')}>添加任务</button>{selectedDate !== actualToday && <button className="button return-today" type="button" onClick={() => setSelectedDate(actualToday)}>回到今天</button>}</div></header>
      <div className="stage-main-scroll"><div className="today-main">
        <p className="eyebrow">{friendlyDate(selectedDate)} · 固定生活安排</p>
        <div className="today-hero fixed-tasks-hero"><h2>把每天确定要做的事，稳稳放进时间里</h2><p>{dateTasks.length} 项生效任务 · 共 {dateTasks.reduce((sum, task) => sum + task.estimate_minutes, 0)} 分钟</p></div>
        <section className="task-groups"><header className="fixed-task-list-heading"><div><p className="eyebrow">固定任务总表</p><h2>所有持续生效的固定任务</h2></div><span>{fixedRules.length} 项</span></header><div className="task-list">{ruleCards.length ? ruleCards.map(({ plan, task, visible, occursOnDate, done }, index) => <article className={`task-card fixed-execution-card${done ? ' completed' : ''}${!occursOnDate ? ' not-scheduled' : ''}`} key={task.id}><span className="task-order">{index + 1}</span><div className="task-content"><header><strong>{visible?.title ?? task.title}</strong><small>{occursOnDate ? `${visible?.start_local ?? task.start_local}–${visible?.end_local ?? task.end_local}` : '所选日期不执行'}</small></header><p>{plan.title} · 建议 {task.estimate_minutes} 分钟 · {task.recurrence === 'DAILY' ? '每天重复' : task.recurrence === 'WEEKLY' ? `${task.recurrence_weekdays.map((day) => weekdays[day]).join('、')}重复` : '一次性'}</p><footer><button className="button" type="button" onClick={() => openEditor(plan, task, visible)}>编辑</button>{occursOnDate && <button className={`button${done ? '' : ' primary'}`} disabled={busy} type="button" onClick={() => void toggleComplete(task.id, done ? 'NOT_STARTED' : 'COMPLETED')}>{done ? '撤销完成' : '标记完成'}</button>}</footer></div></article>) : <div className="rest-day-card"><h2>还没有固定任务</h2><p>点击右上角“添加任务”，建立每天的固定安排。</p></div>}</div></section>
        {error && <p className="form-error" role="alert">{error}</p>}
      </div></div>
    </div>
    <aside className="stage-context-column today-context" aria-label="固定任务工具">
      <div className="fixed-progress-summary"><div className="progress-ring" style={{ '--progress': `${percentage * 3.6}deg` } as CSSProperties}><strong>{percentage}%</strong><span>今日完成</span></div><div><strong>今日进度</strong><small>{completed} / {dateTasks.length} 项已完成</small></div></div>
      <div className="today-tools-scroll">
        <section className="fixed-calendar-tool"><header><span aria-hidden="true">▦</span><div><strong>执行日历</strong><small>查看固定任务与遗漏日期</small></div></header><ExecutionCalendarWidget key={selectedDate.slice(0, 7)} calendar={calendar} selectedDate={selectedDate} onSelectDate={setSelectedDate} /></section>
        <CollapsibleTool icon="◷" title="番茄钟" summary="专注计时与实际用时"><FocusTimerWidget tasks={focusTasks} sessions={focusSessions} onSave={saveFocus} /></CollapsibleTool>
        <CollapsibleTool icon="＋" title="点子备忘" summary={`${captures.length} 条固定生活记录`}><QuickCaptureWidget captures={captures} onAdd={addCapture} onDelete={removeCapture} /></CollapsibleTool>
        <CollapsibleTool icon="⌁" title="今日洞察" summary="完成情况与剩余时间"><section className="fixed-insight"><strong>还剩 {Math.max(0, dateTasks.reduce((sum, task) => sum + (task.status === 'COMPLETED' ? 0 : task.estimate_minutes), 0))} 分钟</strong><p>{dateTasks.length - completed} 项待完成，今天已完成 {percentage}% 。</p></section></CollapsibleTool>
      </div>
    </aside>
    {editing && <DialogBackdrop className="inline-dialog-backdrop" onDismiss={() => setEditing(null)}> <section className="inline-dialog fixed-edit-dialog" role="dialog" aria-modal="true"><header><div><p className="eyebrow">{editing.plan.title}</p><h2>编辑固定任务</h2></div><button className="icon-button" type="button" aria-label="关闭" onClick={() => setEditing(null)}>×</button></header><label>任务名称<input value={editTitle} onChange={(event) => setEditTitle(event.target.value)} /></label><fieldset><legend>修改范围</legend><label><input type="radio" checked={editScope === 'TODAY'} onChange={() => setEditScope('TODAY')} />仅 {friendlyDate(selectedDate)}</label><label><input type="radio" checked={editScope === 'FUTURE'} onChange={() => setEditScope('FUTURE')} />当天及后续</label></fieldset><div className="schedule-fields"><label>开始<TimePicker value={editStart} onChange={(event) => { setEditStart(event.target.value); setEditConflicts(null) }} /></label><label>结束<TimePicker value={editEnd} onChange={(event) => { setEditEnd(event.target.value); setEditConflicts(null) }} /></label></div>{editConflicts && <div className="schedule-conflict-panel"><strong>该时段与 {editConflicts.length} 项安排冲突</strong>{editConflicts.map((item, index) => <p key={index}>{item.existing_title} · {item.existing_start}–{item.existing_end}</p>)}<p>“强势推开”会先用智能安排中保存的可用时段逐项改排；没有完整空档的任务才会转为自由任务。</p></div>}<div className="inline-dialog-actions"><button className="button danger-button" disabled={busy} type="button" onClick={() => void deleteEdited()}>删除{editScope === 'TODAY' ? '当天' : '当天及后续'}</button><button className="button" type="button" onClick={() => setEditing(null)}>取消</button>{editConflicts ? <><button className="button" disabled={busy} type="button" onClick={() => void saveEdit('PARALLEL')}>确认并行</button><button className="button primary" disabled={busy} type="button" onClick={() => void saveEdit('PUSH')}>强势推开并自动改排</button></> : <button className="button primary" disabled={busy || !editTitle.trim() || Boolean(editStart) !== Boolean(editEnd) || Boolean(editStart && editEnd && editStart >= editEnd)} type="button" onClick={() => void saveEdit()}>{busy ? '保存中…' : '保存修改'}</button>}</div></section></DialogBackdrop>}
  </section>
}

