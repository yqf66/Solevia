import { ConnectionAwareError } from './ui/ConnectionAwareError'
import { TimePicker } from './ui/TimePicker'
import { DialogBackdrop } from './ui/DialogBackdrop'
import { useConfirmation } from './ui/useConfirmation'
import { useEffect, useRef, useState } from 'react'
import { Circle, CircleCheck, Plus, X, Pencil, SkipForward, Trash2, CalendarClock, ArrowUpRight, Undo2, Archive } from 'lucide-react'
import { Link, useNavigate, useParams, useSearchParams } from 'react-router-dom'
import { AllocationDialog } from './AllocationDialog'
import { deleteManualTask, getGoalLifecycle, getManualPlan, getUnifiedToday, listManualPlans, updateManualTask,
  type ManualPlan, type ManualTask, type UnifiedToday } from './api'
import { applyLightDraft, blankLightTask, createLightDraft, generateLightDraft, getLightDraft,
  listLightDrafts, localDate, saveLightDraft, upgradeLightPlan, addLightTask, discardLightDraft, archiveLightPlan,
  type LightDraft, type LightResult, type LightTask } from './lightApi'
import './LightArrangements.css'
import './quick/QuickPlanIdentity.css'
import { QuickPlanIntake } from './quick/QuickPlanIntake'
import { goalPath } from './ui/plannerPresentation'

const weekdays = ['一', '二', '三', '四', '五', '六', '日']
const errorText = (error: unknown) => error instanceof Error ? error.message : '本次操作未完成。'

export function LightCreatePage() {
  const { draftId } = useParams()
  const navigate = useNavigate()
  const [input, setInput] = useState('')
  const [start, setStart] = useState(localDate())
  const [days, setDays] = useState(7)
  const [draft, setDraft] = useState<LightDraft | null>(null)
  const [result, setResult] = useState<LightResult | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [dirty, setDirty] = useState(false)
  const loadedId = useRef<string | null>(null)

  useEffect(() => {
    if (!draftId || loadedId.current === draftId) return
    let active = true
    getLightDraft(draftId).then(value => {
      if (!active) return
      loadedId.current = draftId
      if (value.status === 'APPLIED' && value.quick_plan_id) {
        navigate(`/quick/${value.quick_plan_id}`, { replace: true }); return
      }
      setDraft(value); setResult(value.result); setInput(value.raw_input)
      setStart(value.start_date); setDays(value.horizon_days)
    }).catch(caught => { if (active) setError(errorText(caught)) })
    return () => { active = false }
  }, [draftId, navigate])

  function accept(value: LightDraft) {
    loadedId.current = value.id
    setDraft(value); setResult(value.result); setDirty(false)
  }
  async function create(useModel: boolean) {
    setBusy(true); setError('')
    try {
      const value = await createLightDraft({ raw_input: input.trim(), start_date: start,
        horizon_days: days, timezone: Intl.DateTimeFormat().resolvedOptions().timeZone || 'Asia/Shanghai' })
      accept(value)
      navigate(`/quick/drafts/${value.id}`, { replace: true })
      if (useModel) accept(await generateLightDraft(value))
    } catch (caught) { setError(errorText(caught)) }
    finally { setBusy(false) }
  }
  function editTask(index: number, patch: Partial<LightTask>) {
    if (!result) return
    setResult({ ...result, tasks: result.tasks.map((task, at) => at === index ? { ...task, ...patch } : task) })
    setDirty(true)
  }
  async function save(publish = false) {
    if (!draft || !result) return
    setBusy(true); setError('')
    try {
      const updated = dirty || draft.status === 'GENERATING' ? await saveLightDraft(draft, result) : draft
      accept(updated)
      if (publish) {
        const applied = await applyLightDraft(updated)
        navigate(`/quick/${applied.quick_plan_id}?date=${start}`)
      }
    } catch (caught) { setError(errorText(caught)) }
    finally { setBusy(false) }
  }
  const end = new Date(`${start}T12:00:00`); end.setDate(end.getDate() + days - 1)
  const endDate = `${end.getFullYear()}-${String(end.getMonth() + 1).padStart(2, '0')}-${String(end.getDate()).padStart(2, '0')}`
  const invalid = !result?.tasks.length || result.tasks.some(task => !task.title.trim() ||
    task.estimate_minutes < 1 || task.estimate_minutes > 1440 || task.recurrence !== 'ONCE' && !task.scheduled_date ||
    task.recurrence === 'WEEKLY' && !task.recurrence_weekdays.length ||
    Boolean(task.start_local) !== Boolean(task.end_local) ||
    !!task.start_local && (!task.scheduled_date || task.start_local >= (task.end_local || '')) ||
    !!task.scheduled_date && (task.scheduled_date < start || task.scheduled_date > endDate)) ||
    result.unavailable_windows.some(window => !window.reason.trim() || !window.date ||
      window.date < start || window.date > endDate || !window.start_local || window.start_local >= window.end_local)

  return <section className="page light-page">
    <header className="light-header"><div><p className="eyebrow">快速安排</p><h1>{draft ? '近期任务草案' : '安排今天，或接下来几天'}</h1></div><Link className="back-link" to="/quick">全部快速安排</Link></header>
    {!draft ? <QuickPlanIntake input={input} start={start} days={days} busy={busy}
      onInput={setInput} onStart={setStart} onDays={setDays} onCreate={useModel => void create(useModel)} /> : result && <>
      <div className="light-draft-meta"><span>{draft.start_date} 至 {endDate}</span><span role="status">{busy ? '正在处理…' : dirty ? '有未保存修改' : '草案已保存'}</span></div>
      {draft.error_message && <p className="light-notice" role="status">{draft.error_message}</p>}
      {draft.status === 'GENERATING' && !busy && <p className="light-notice">上次整理未返回。可以保存当前草案并继续安排。</p>}
      <label className="light-title-label">安排名称<input value={result.title} maxLength={160} disabled={busy} onChange={event => { setResult({ ...result, title: event.target.value }); setDirty(true) }} /></label>
      <div className="light-task-list">{result.tasks.map((task, index) => <article className="light-task-editor" key={index}>
        <div className="light-task-heading"><span>{String(index + 1).padStart(2, '0')}</span><input aria-label={`任务 ${index + 1} 名称`} value={task.title} maxLength={180} disabled={busy} onChange={event => editTask(index, { title: event.target.value })} />
          <button className="icon-button" type="button" title="移除任务" aria-label={`移除任务 ${index + 1}`} disabled={busy} onClick={() => { setResult({ ...result, tasks: result.tasks.filter((_, at) => at !== index) }); setDirty(true) }}><X size={18} /></button></div>
        <div className="light-task-fields"><label>预计分钟<input type="number" min={1} max={1440} value={task.estimate_minutes} disabled={busy} onChange={event => editTask(index, { estimate_minutes: Number(event.target.value) })} /></label>
          <label>日期<input type="date" min={start} max={endDate} value={task.scheduled_date || ''} disabled={busy} onChange={event => editTask(index, { scheduled_date: event.target.value || null })} /></label>
          <label>重复<select value={task.recurrence} disabled={busy} onChange={event => editTask(index, { recurrence: event.target.value as LightTask['recurrence'], scheduled_date: event.target.value === 'ONCE' ? task.scheduled_date : task.scheduled_date || start, recurrence_end_date: event.target.value === 'ONCE' ? null : endDate })}><option value="ONCE">仅一次</option><option value="DAILY">每天</option><option value="WEEKLY">每周指定日</option></select></label>
        </div>
        {task.recurrence === 'WEEKLY' && <fieldset className="light-weekdays"><legend>星期</legend>{weekdays.map((label, day) => <label key={day}><input type="checkbox" checked={task.recurrence_weekdays.includes(day)} disabled={busy} onChange={event => editTask(index, { recurrence_weekdays: event.target.checked ? [...task.recurrence_weekdays, day] : task.recurrence_weekdays.filter(value => value !== day) })} />{label}</label>)}</fieldset>}
        <details><summary>备注、优先级与固定时间</summary><label>备注<textarea rows={2} maxLength={4000} value={task.description} disabled={busy} onChange={event => editTask(index, { description: event.target.value })} /></label><label>优先级<select value={task.priority} disabled={busy} onChange={event => editTask(index, { priority: Number(event.target.value) })}>{['很低', '低', '普通', '高', '很高'].map((label, at) => <option key={at} value={at + 1}>{label}</option>)}</select></label><div className="light-task-fields"><label>开始<TimePicker value={task.start_local || ''} disabled={busy} onChange={event => editTask(index, { start_local: event.target.value || null })} /></label><label>结束<TimePicker value={task.end_local || ''} disabled={busy} onChange={event => editTask(index, { end_local: event.target.value || null })} /></label><button type="button" className="button" disabled={busy} onClick={() => editTask(index, { start_local: null, end_local: null })}>不固定时间</button></div></details>
      </article>)}</div>
      <button className="button" type="button" disabled={busy || result.tasks.length >= 30} onClick={() => { setResult({ ...result, tasks: [...result.tasks, blankLightTask()] }); setDirty(true) }}><Plus size={16} /> 添加任务</button>
      <details className="light-assumptions"><summary>原始输入</summary><p className="light-raw">{draft.raw_input}</p></details>
      {!!result.assumptions.length && <details className="light-assumptions"><summary>默认与假设 · {result.assumptions.length}</summary>{result.assumptions.map((text, index) => <p key={index}>{text}</p>)}</details>}
      {!!result.questions.length && <div className="light-notice">{result.questions.map((text, index) => <p key={index}>{text}</p>)}</div>}
      <details className="light-exclusions"><summary>已有时间约定 · {result.unavailable_windows.length}</summary>{result.unavailable_windows.map((window, index) => <div className="light-window-editor" key={index}>
        {(['date', 'start_local', 'end_local', 'reason'] as const).map(key => <label key={key}>{({ date: '日期', start_local: '开始', end_local: '结束', reason: '约定' })[key]}<input type={key === 'date' ? 'date' : key === 'reason' ? 'text' : 'time'} value={window[key]} disabled={busy} maxLength={key === 'reason' ? 180 : undefined} onChange={event => { setResult({ ...result, unavailable_windows: result.unavailable_windows.map((item, at) => at === index ? { ...item, [key]: event.target.value } : item) }); setDirty(true) }} /></label>)}
        <button className="icon-button" title="移除时间约定" aria-label={`移除时间约定 ${index + 1}`} disabled={busy} onClick={() => { setResult({ ...result, unavailable_windows: result.unavailable_windows.filter((_, at) => at !== index) }); setDirty(true) }}><X size={18} /></button></div>)}
        <button className="button" type="button" disabled={busy || result.unavailable_windows.length >= 30} onClick={() => { setResult({ ...result, unavailable_windows: [...result.unavailable_windows, { date: start, start_local: '', end_local: '', reason: '' }] }); setDirty(true) }}><Plus size={16} /> 时间约定</button></details>
      {result.full_planning_recommended && <p className="light-notice">这份安排包含较复杂的目标，可在发布后升级为完整规划。</p>}
      <footer className="light-actions"><button className="button" type="button" disabled={busy || !result.title.trim() || invalid} onClick={() => void save()}>保存草案</button><button className="button primary" type="button" disabled={busy || !result.title.trim() || invalid} onClick={() => void save(true)}>{busy ? '正在处理…' : '确认安排'}</button></footer>
    </>}
    {error && <ConnectionAwareError message={error} />}
  </section>
}

export function LightListPage() {
  const { confirm, confirmationDialog } = useConfirmation()
  const [plans, setPlans] = useState<ManualPlan[]>([])
  const [drafts, setDrafts] = useState<LightDraft[]>([])
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  useEffect(() => {
    Promise.all([listManualPlans(), listLightDrafts()]).then(([items, saved]) => {
      setPlans(items.filter(plan => plan.kind === 'QUICK')); setDrafts(saved)
    }).catch(caught => setError(errorText(caught)))
  }, [])
  async function discard(draft: LightDraft) {
    if (!await confirm({ title: '丢弃草案', message: `确定丢弃草案“${draft.result.title}”？此操作无法撤销。`, confirmLabel: '确认丢弃' })) return
    setBusy(true); setError('')
    try { await discardLightDraft(draft); setDrafts(items => items.filter(item => item.id !== draft.id)) }
    catch (caught) { setError(errorText(caught)) }
    finally { setBusy(false) }
  }
  return <section className="page light-page">{confirmationDialog}<header className="light-header"><div><p className="eyebrow">快速安排</p><h1>近期要做的事</h1></div><Link className="button primary" to="/quick/new"><Plus size={16} /> 新建安排</Link></header>
    {error && <ConnectionAwareError message={error} />}
    {!plans.length && !drafts.length && <p className="muted">还没有近期安排。</p>}
    {!!drafts.length && <section className="light-plan-list"><h2>未发布草案</h2>{drafts.map(draft => <div className="light-draft-row" key={draft.id}><Link to={`/quick/drafts/${draft.id}`}><strong>{draft.result.title}</strong><span>{draft.start_date} · {draft.result.tasks.length} 项</span></Link><button className="icon-button" title="丢弃草案" aria-label={`丢弃 ${draft.result.title}`} disabled={busy || draft.status === 'GENERATING'} onClick={() => void discard(draft)}><Trash2 size={16} /></button></div>)}</section>}
    <section className="light-plan-list">{plans.filter(plan => plan.status === 'ACTIVE').map(plan => <Link key={plan.id} to={`/quick/${plan.id}`}><strong>{plan.title}</strong><span>{plan.tasks.length} 项{plan.arrangement?.start_date ? ` · ${plan.arrangement.start_date}` : ''}</span></Link>)}</section>
    {plans.some(plan => plan.status !== 'ACTIVE') && <details className="light-plan-list light-assumptions"><summary>已归档安排</summary>{plans.filter(plan => plan.status !== 'ACTIVE').map(plan => <Link key={plan.id} to={`/quick/${plan.id}${plan.arrangement?.handoff_at ? '?history=1' : ''}`}><strong>{plan.title}</strong><span>{plan.arrangement?.handoff_at ? '已升级 · 原安排历史' : `${plan.tasks.length} 项`}</span></Link>)}</details>}
  </section>
}

export function LightPlanPage() {
  const { planId } = useParams()
  const [params] = useSearchParams()
  // A pending save or open editor belongs to this plan and date only.
  return <LightPlanWorkspace key={`${planId}:${params.get('date') || localDate()}:${params.get('history') || ''}`} />
}

function LightPlanWorkspace() {
  const { confirm, confirmationDialog } = useConfirmation()
  const { planId } = useParams()
  const navigate = useNavigate()
  const [plan, setPlan] = useState<ManualPlan | null>(null)
  const [searchParams, setSearchParams] = useSearchParams()
  const date = searchParams.get('date') || localDate()
  const history = searchParams.get('history') === '1'
  const setDate = (value: string) => setSearchParams(previous => { previous.set('date', value); return previous }, { replace: true })
  const [board, setBoard] = useState<UnifiedToday | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [editing, setEditing] = useState<(LightTask & { id?: string }) | null>(null)
  const [scope, setScope] = useState<'TODAY' | 'FUTURE'>('TODAY')
  const [allocation, setAllocation] = useState(false)
  const dialogRef = useRef<HTMLFormElement | null>(null)
  const editingOpen = Boolean(editing)
  useEffect(() => {
    if (!editingOpen) return
    const previous = document.activeElement as HTMLElement | null
    const dialog = dialogRef.current
    dialog?.querySelector<HTMLInputElement>('input:not(:disabled)')?.focus()
    const handleKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') { setEditing(null); return }
      if (event.key !== 'Tab' || !dialog) return
      const elements = [...dialog.querySelectorAll<HTMLElement>('button:not(:disabled), input:not(:disabled), textarea:not(:disabled), select:not(:disabled)')]
      const first = elements[0], last = elements[elements.length - 1]
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus() }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus() }
    }
    document.addEventListener('keydown', handleKey)
    return () => { document.removeEventListener('keydown', handleKey); previous?.focus() }
  }, [editingOpen])
  useEffect(() => {
    if (!planId) return
    let active = true
    setPlan(null); setBoard(null); setError('')
    getManualPlan(planId).then(async value => {
      if (!active) return
      if (value.kind !== 'QUICK') { navigate('/quick', { replace: true }); return }
      setPlan(value)
      if (value.arrangement?.handoff_at && value.arrangement.upgraded_goal_id && !history) {
        const successor = await getGoalLifecycle(value.arrangement.upgraded_goal_id)
        if (active) navigate(goalPath(successor), { replace: true })
        return
      }
      if (value.arrangement?.handoff_at) return
      const today = await getUnifiedToday(date)
      if (active) setBoard(today)
    }).catch(caught => { if (active) setError(errorText(caught)) })
    return () => { active = false }
  }, [planId, date, history, navigate])
  async function update(task: ManualTask, patch: Parameters<typeof updateManualTask>[2]) {
    if (!planId) return
    setBusy(true); setError('')
    try { setPlan(await updateManualTask(planId, task.id, patch)); setBoard(await getUnifiedToday(date)); setEditing(null) }
    catch (caught) { setError(errorText(caught)) }
    finally { setBusy(false) }
  }
  function state(task: ManualTask) {
    return task.recurrence === 'ONCE' ? task.status : task.overrides.find(item => item.override_date === date)?.status || 'NOT_STARTED'
  }
  function edit(task: ManualTask) {
    const override = task.overrides.find(item => item.override_date === date)
    setScope(task.recurrence === 'ONCE' ? 'FUTURE' : 'TODAY')
    setEditing({ ...task, flexible_recurrence: true,
      scheduled_date: task.recurrence === 'ONCE' ? task.scheduled_date : date,
      title: override?.title ?? task.title, description: override?.description ?? task.description,
      estimate_minutes: override?.estimate_minutes ?? task.estimate_minutes,
      start_local: override?.time_overridden ? override.start_local : task.start_local,
      end_local: override?.time_overridden ? override.end_local : task.end_local })
  }
  async function saveTask() {
    if (!editing || !planId) return
    setBusy(true); setError('')
    try {
      const { id, ...task } = editing
      const next = id ? await updateManualTask(planId, id, {
        title: task.title, description: task.description, estimate_minutes: task.estimate_minutes, priority: task.priority,
        scheduled_date: task.scheduled_date, start_local: task.start_local, end_local: task.end_local,
        scope: task.recurrence === 'ONCE' ? 'FUTURE' : scope,
      }) : await addLightTask(planId, task)
      setPlan(next); setBoard(await getUnifiedToday(date)); setEditing(null)
    } catch (caught) { setError(errorText(caught)) }
    finally { setBusy(false) }
  }
  async function remove(task: ManualTask) {
    if (!planId) return
    if (!await confirm({ title: '移除安排', message: `确定移除“${task.title}”${task.recurrence === 'ONCE' ? '' : '的本次安排'}？`, confirmLabel: '确认移除' })) return
    setBusy(true); setError('')
    try {
      setPlan(await deleteManualTask(planId, task.id, task.recurrence === 'ONCE' ? { scope: 'FUTURE' } : { scope: 'TODAY', override_date: date }))
      setBoard(await getUnifiedToday(date))
    } catch (caught) { setError(errorText(caught)) }
    finally { setBusy(false) }
  }
  function occurs(task: ManualTask) {
    if (task.overrides.find(item => item.override_date === date)?.is_deleted) return false
    if (task.recurrence === 'ONCE') return task.scheduled_date === date
    return !!task.scheduled_date && task.scheduled_date <= date && (!task.recurrence_end_date || date <= task.recurrence_end_date) && (task.recurrence === 'DAILY' || task.recurrence_weekdays.includes((new Date(`${date}T12:00:00`).getDay() + 6) % 7))
  }
  async function upgrade() {
    if (!planId) return
    setBusy(true); setError('')
    try {
      const existing = plan?.arrangement?.upgraded_goal_id
      if (existing) navigate(goalPath(await getGoalLifecycle(existing)))
      else {
        const value = await upgradeLightPlan(planId)
        window.dispatchEvent(new Event('planner:quick-plans-changed'))
        navigate(goalPath({ id: value.goal_id, status: value.status }))
      }
    }
    catch (caught) { setError(errorText(caught)) }
    finally { setBusy(false) }
  }
  async function archive() {
    if (!planId || !plan) return
    setBusy(true); setError('')
    try { setPlan(await archiveLightPlan(planId, plan.status !== 'ACTIVE')); setBoard(await getUnifiedToday(date)) }
    catch (caught) { setError(errorText(caught)) }
    finally { setBusy(false) }
  }
  const tasks = plan?.tasks.filter(task => occurs(task) || task.recurrence === 'ONCE' &&
    (!task.scheduled_date || task.status === 'NOT_STARTED' && task.scheduled_date < date)) || []
  const others = plan?.tasks.filter(task => !tasks.includes(task)) || []
  const editInvalid = !editing?.title.trim() || !editing.estimate_minutes ||
    Boolean(editing.start_local) !== Boolean(editing.end_local) ||
    !!editing.start_local && (!editing.scheduled_date || editing.start_local >= (editing.end_local || '')) ||
    editing.recurrence !== 'ONCE' && !editing.scheduled_date ||
    editing.recurrence === 'WEEKLY' && !editing.recurrence_weekdays.length
  const readOnly = Boolean(plan?.arrangement?.handoff_at)
  return <section className="page light-page">{confirmationDialog}<header className="light-header"><div><p className="eyebrow">快速安排</p><h1>{plan?.title || '正在读取…'}</h1></div><Link className="back-link" to="/">今日总览</Link></header>
    {readOnly && <p className="light-notice">已升级为完整规划。这里保留原安排及完成记录，不再重复生成每日任务。</p>}
    <div className="light-toolbar"><label>查看日期<input type="date" value={date} onChange={event => { if (event.target.value) setDate(event.target.value) }} /></label><button className="button" disabled={busy || !board || readOnly} onClick={() => setAllocation(true)}><CalendarClock size={16} /> 安排具体时间</button><button className="button" disabled={busy || !plan} onClick={() => void upgrade()}><ArrowUpRight size={16} />{plan?.arrangement?.upgraded_goal_id ? '打开完整规划' : '升级完整规划'}</button><button className="button" disabled={busy || !plan || readOnly || plan.tasks.length >= 30} onClick={() => { setEditing({ ...blankLightTask(), scheduled_date: date }); setScope('FUTURE') }}><Plus size={16} /> 添加任务</button></div>
    <div className="light-secondary-actions">{!readOnly && <button className="button" disabled={busy || !plan} onClick={() => void archive()}><Archive size={16} /> {plan?.status === 'ARCHIVED' ? '恢复安排' : '归档安排'}</button>}<Link className="back-link" to="/quick">全部快速安排</Link></div>
    <div className="light-execution-list">{tasks.map(task => {
      const status = state(task)
      const visible = [...(board?.fixed_tasks || []), ...(board?.flexible_tasks || [])].find(item => item.id === task.id)
      const due = task.scheduled_date && task.scheduled_date < date && task.recurrence === 'ONCE'
      return <article className={`light-execution-task ${status === 'COMPLETED' ? 'completed' : ''}`} key={task.id}>
        <button className="light-check" title={status === 'COMPLETED' ? '撤销完成' : '完成任务'} aria-label={`${status === 'COMPLETED' ? '撤销完成' : '完成'} ${task.title}`} disabled={busy || readOnly} onClick={() => void update(task, { status: status === 'COMPLETED' ? 'NOT_STARTED' : 'COMPLETED', scope: task.recurrence === 'ONCE' ? 'FUTURE' : 'TODAY', scheduled_date: task.recurrence === 'ONCE' ? task.scheduled_date : date })}>{status === 'COMPLETED' ? <CircleCheck size={20} /> : <Circle size={20} />}</button>
        <div className="light-execution-content"><strong>{visible?.title || task.title}</strong><p>{visible?.description ?? task.description}</p><small>{visible?.estimate_minutes ?? task.estimate_minutes} 分钟 · {task.recurrence === 'DAILY' ? '每天' : task.recurrence === 'WEEKLY' ? '每周指定日' : due ? '待重新安排' : task.scheduled_date ? '仅一次' : '未定日期'}{visible?.is_fixed ? ` · ${visible.start_local}–${visible.end_local}` : ' · 自由时间'}{status === 'SKIPPED' ? ' · 本次已跳过' : ''}</small></div>
        <div className="light-row-actions"><button className="icon-button" title="编辑任务" aria-label={`编辑 ${task.title}`} disabled={busy || readOnly} onClick={() => edit(task)}><Pencil size={16} /></button>{status !== 'COMPLETED' && <button className="icon-button" title={status === 'SKIPPED' ? '恢复本次' : '跳过本次'} aria-label={`${status === 'SKIPPED' ? '恢复' : '跳过'} ${task.title}`} disabled={busy || readOnly} onClick={() => void update(task, { status: status === 'SKIPPED' ? 'NOT_STARTED' : 'SKIPPED', scope: task.recurrence === 'ONCE' ? 'FUTURE' : 'TODAY', scheduled_date: task.recurrence === 'ONCE' ? task.scheduled_date : date })}>{status === 'SKIPPED' ? <Undo2 size={16} /> : <SkipForward size={16} />}</button>}<button className="icon-button" title={task.recurrence === 'ONCE' ? '移除任务' : '移除本次'} aria-label={`移除 ${task.title}`} disabled={busy || readOnly} onClick={() => void remove(task)}><Trash2 size={16} /></button></div>
      </article>
    })}</div>
    {plan && !tasks.length && <p className="muted">这个日期没有任务。</p>}
    {!!others.length && <details className="light-assumptions"><summary>其他日期与历史事项 · {others.length}</summary>{others.map(task => <button className="light-future-task" key={task.id} onClick={() => { if (task.scheduled_date) setDate(task.scheduled_date) }}>{task.scheduled_date || '未定日期'} · {task.title} · {task.status === 'COMPLETED' ? '已完成' : task.status === 'SKIPPED' ? '已跳过' : task.recurrence === 'ONCE' ? '待办' : '重复事项'}</button>)}</details>}
    {error && <ConnectionAwareError message={error} />}
    {allocation && board && <AllocationDialog today={board} manualPlanId={planId} onClose={() => setAllocation(false)} onApplied={setBoard} />}
    {editing && <DialogBackdrop className="inline-dialog-backdrop" onDismiss={() => setEditing(null)}> <form ref={dialogRef} className="inline-dialog" role="dialog" aria-modal="true" aria-label="编辑快速安排任务" onSubmit={event => { event.preventDefault(); void saveTask() }}>
      <header><h2>{editing.id ? '编辑任务' : '添加任务'}</h2><button className="icon-button" type="button" aria-label="关闭编辑" disabled={busy} onClick={() => setEditing(null)}><X size={18} /></button></header>
      {editing.id && editing.recurrence !== 'ONCE' && <fieldset className="light-segment"><legend>修改范围</legend><button type="button" aria-pressed={scope === 'TODAY'} onClick={() => setScope('TODAY')}>只改本次</button><button type="button" aria-pressed={scope === 'FUTURE'} onClick={() => setScope('FUTURE')}>从本次起</button></fieldset>}
      <label>名称<input value={editing.title} maxLength={180} required disabled={busy} onChange={event => setEditing({ ...editing, title: event.target.value })} /></label><label>备注<textarea value={editing.description} maxLength={4000} disabled={busy} onChange={event => setEditing({ ...editing, description: event.target.value })} /></label><div className="light-task-fields"><label>分钟<input type="number" min={1} max={1440} required disabled={busy} value={editing.estimate_minutes} onChange={event => setEditing({ ...editing, estimate_minutes: Number(event.target.value) })} /></label><label>{editing.recurrence !== 'ONCE' ? '生效日期' : '日期'}<input type="date" disabled={busy || !!editing.id && editing.recurrence !== 'ONCE' && scope === 'TODAY'} value={editing.scheduled_date || ''} onChange={event => setEditing({ ...editing, scheduled_date: event.target.value || null })} /></label></div>
      <label>优先级<select value={editing.priority} disabled={busy} onChange={event => setEditing({ ...editing, priority: Number(event.target.value) })}>{['很低', '低', '普通', '高', '很高'].map((label, at) => <option key={at} value={at + 1}>{label}</option>)}</select></label>
      {!editing.id && <><label>重复<select value={editing.recurrence} disabled={busy} onChange={event => setEditing({ ...editing, recurrence: event.target.value as LightTask['recurrence'], scheduled_date: event.target.value === 'ONCE' ? editing.scheduled_date : editing.scheduled_date || date })}><option value="ONCE">仅一次</option><option value="DAILY">每天</option><option value="WEEKLY">每周指定日</option></select></label>{editing.recurrence === 'WEEKLY' && <fieldset className="light-weekdays"><legend>星期</legend>{weekdays.map((label, day) => <label key={day}><input type="checkbox" checked={editing.recurrence_weekdays.includes(day)} onChange={event => setEditing({ ...editing, recurrence_weekdays: event.target.checked ? [...editing.recurrence_weekdays, day] : editing.recurrence_weekdays.filter(value => value !== day) })} />{label}</label>)}</fieldset>}</>}
      <div className="light-task-fields"><label>开始<TimePicker disabled={busy} value={editing.start_local || ''} onChange={event => setEditing({ ...editing, start_local: event.target.value || null })} /></label><label>结束<TimePicker disabled={busy} value={editing.end_local || ''} onChange={event => setEditing({ ...editing, end_local: event.target.value || null })} /></label><button className="button" type="button" disabled={busy} onClick={() => setEditing({ ...editing, start_local: null, end_local: null })}>不固定时间</button></div>{error && <p role="alert" className="form-error">{error}</p>}<footer className="light-actions"><button className="button" type="button" disabled={busy} onClick={() => setEditing(null)}>取消</button><button className="button primary" disabled={busy || editInvalid}>{busy ? '保存中…' : '保存'}</button></footer>
    </form></DialogBackdrop>}
  </section>
}
