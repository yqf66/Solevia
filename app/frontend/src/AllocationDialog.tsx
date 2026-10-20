import { TimePicker } from './ui/TimePicker'
import { DialogBackdrop } from './ui/DialogBackdrop'
import { useEffect, useMemo, useState } from 'react'
import { applyAllocation, getEffectiveWorkspaceAvailability, getWorkspaceAvailability, previewAllocation, replaceWorkspaceAvailability, resetWorkspaceAvailability, type AllocationPreview, type UnifiedToday, type WorkspaceAvailabilityWindow } from './api'

export function AllocationDialog({ today, onClose, onApplied, manualPlanId }: {
  today: UnifiedToday; onClose: () => void; onApplied: (board: UnifiedToday) => void
  manualPlanId?: string
}) {
  const [windows, setWindows] = useState<Array<{ start_local: string; end_local: string }>>([])
  const [preview, setPreview] = useState<AllocationPreview | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [storedWindows, setStoredWindows] = useState<WorkspaceAvailabilityWindow[]>([])
  const [source, setSource] = useState<'ROUTINE' | 'CUSTOM'>('ROUTINE')
  const [dirty, setDirty] = useState(false)
  const [loaded, setLoaded] = useState(false)
  const weekday = useMemo(() => (new Date(`${today.date}T12:00:00`).getDay() + 6) % 7, [today.date])
  useEffect(() => {
    let active = true
    Promise.all([getWorkspaceAvailability(), getEffectiveWorkspaceAvailability(today.date)]).then(([items, effective]) => {
      if (!active) return
      setStoredWindows(items)
      setSource(effective.source)
      setWindows(effective.windows.map(item => ({ start_local: item.start_local, end_local: item.end_local })))
      setLoaded(true)
    }).catch(e => { if (active) { setError(e instanceof Error ? e.message : '暂时无法读取常用时段'); setLoaded(true) } })
    return () => { active = false }
  }, [today.date, weekday])

  async function persistWindows() {
    const reusable = source === 'ROUTINE'
      ? Array.from({ length: 7 }, (_, day) => windows.map(item => ({ ...item, weekday: day, valid_from: null, valid_to: null }))).flat()
      : [...storedWindows.filter(item => item.weekday !== weekday || item.valid_from || item.valid_to).map(item => ({ weekday: item.weekday, start_local: item.start_local, end_local: item.end_local, valid_from: item.valid_from, valid_to: item.valid_to })), ...windows.map(item => ({ ...item, weekday, valid_from: null, valid_to: null }))]
    const saved = await replaceWorkspaceAvailability(reusable)
    setStoredWindows(saved)
    setSource('CUSTOM')
    setDirty(false)
  }
  async function generate() {
    setBusy(true); setError(''); setPreview(null)
    try {
      if (dirty) await persistWindows()
      setPreview(await previewAllocation({ scheduled_date: today.date, include_flexible: true,
        manual_plan_id: manualPlanId,
        windows: windows.map(w => ({ ...w, weekday })) }))
    } catch (e) { setError(e instanceof Error ? e.message : '暂时无法生成预览') }
    finally { setBusy(false) }
  }
  async function restoreRoutine() {
    setBusy(true); setError(''); setPreview(null)
    try {
      const effective = await resetWorkspaceAvailability()
      setStoredWindows([]); setSource('ROUTINE'); setDirty(false)
      setWindows(effective.windows.map(item => ({ start_local: item.start_local, end_local: item.end_local })))
    } catch (e) { setError(e instanceof Error ? e.message : '暂时无法恢复作息时段') }
    finally { setBusy(false) }
  }
  async function apply() {
    if (!preview) return
    setBusy(true); setError('')
    try { onApplied(await applyAllocation(preview)); onClose() }
    catch (e) { setError(e instanceof Error ? e.message : '保存失败'); setPreview(null) }
    finally { setBusy(false) }
  }
  const titles = new Map([...today.fixed_tasks, ...today.flexible_tasks].map(t => [t.id, t.title]))
  const occupiedItems = preview ? [
    ...(preview.scheduling_context?.occupied_constraints ?? []).map(item => ({ ...item, kind: '课程或约定' })),
    ...(preview.scheduling_context?.occupied_fixed_tasks ?? []).map(item => ({ ...item, kind: '已有固定任务' })),
  ].sort((left, right) => left.start_local.localeCompare(right.start_local)) : []
  return <DialogBackdrop className="inline-dialog-backdrop" onDismiss={onClose}> <section className="inline-dialog allocation-dialog" role="dialog" aria-modal="true" aria-labelledby="allocation-title">
    <header><h2 id="allocation-title">智能安排今日任务</h2><button type="button" disabled={busy} onClick={onClose} aria-label="关闭排课">×</button></header>
    <p>{today.date} · {today.timezone}。保留已有固定安排，优先安排需要具体时间的任务。</p>
    <div className="allocation-window-source"><p>{loaded ? source === 'ROUTINE' ? '当前根据你的作息自动扣除三餐与午休；修改后会保存为一套全周通用时段。' : '当前使用你最近保存的自定义时段；修改后会继续保存并复用。' : '正在计算可用时段…'}</p>{source === 'CUSTOM' && <button className="button" type="button" disabled={busy} onClick={() => void restoreRoutine()}>恢复为作息自动计算</button>}</div>
    {windows.map((w, i) => <div className="schedule-fields" key={i}>
      <label>开始<TimePicker value={w.start_local} disabled={busy} onChange={e => { setDirty(true); setPreview(null); setWindows(windows.map((v, n) => n === i ? { ...v, start_local: e.target.value } : v)) }} /></label>
      <label>结束<TimePicker value={w.end_local} disabled={busy} onChange={e => { setDirty(true); setPreview(null); setWindows(windows.map((v, n) => n === i ? { ...v, end_local: e.target.value } : v)) }} /></label>
      <button type="button" disabled={busy} onClick={() => { setDirty(true); setPreview(null); setWindows(windows.filter((_, n) => n !== i)) }}>移除</button>
    </div>)}
    <button className="button" disabled={busy} onClick={() => { setDirty(true); setPreview(null); setWindows([...windows, { start_local: '19:00', end_local: '21:00' }]) }}>＋ 可用时间段</button>
    {preview && <div className="allocation-results"><p>候选任务 {preview.required_minutes} 分钟 · 已安排 {preview.assigned_minutes} 分钟 · 待安排 {preview.unallocated_minutes} 分钟 · 空闲 {preview.available_minutes} 分钟</p>
      <p className="muted">本次按当天独立排课：{preview.strategy === 'MODEL_ASSISTED' ? 'AI 已提出建议，并通过本地规则校验。' : '使用本地规则。'}先保留 {preview.scheduling_context?.occupied_fixed_tasks.length ?? 0} 项固定安排，再按任务优先级与前置关系分配。</p>
      {occupiedItems.length > 0 && <section className="allocation-occupied" aria-label="本次排课避开的时间"><header><strong>这些时间已有安排</strong><small>不会再放入新任务</small></header><div>{occupiedItems.map((item, index) => <article key={`${item.source}-${item.start_local}-${item.end_local}-${index}`}><time>{item.start_local}–{item.end_local}</time><span><strong>{item.title}</strong><small>{item.kind}</small></span></article>)}</div></section>}
      {preview.competition_summary?.map((summary) => <section className="allocation-occupied" aria-label="多个规划共享容量" key={summary.scheduled_date}><header><strong>多个规划共享当天容量</strong><small>{summary.required_minutes} / {summary.available_minutes} 分钟</small></header><div>{summary.plans.map(plan => <article key={plan.plan_id}><span><strong>{plan.plan_title}</strong><small>已安排 {plan.assigned_minutes} 分钟 · 待安排 {plan.unallocated_minutes} 分钟</small></span></article>)}</div>{summary.tradeoffs.map(item => <p className="muted" key={item}>{item}</p>)}</section>)}
      {preview.assignments.map(a => <p key={a.task_id}><strong>{a.start_local}–{a.end_local}</strong>　{titles.get(a.task_id)}</p>)}
      {preview.unallocated.length > 0 && <><h3>暂时无法安排</h3>{preview.unallocated.map(t => <p key={t.id}>{t.title} · {t.estimate_minutes} 分钟 · {{ TIMEZONE_CONFLICT: '时区冲突', DEPENDENCY_BLOCKED: '前置任务阻塞', FIXED_CONSTRAINT: '固定安排冲突', CAPACITY_INSUFFICIENT: '可用容量不足', CONTINUOUS_SLOT_REQUIRED: '没有足够长的连续空档' }[preview.unallocated_reasons?.[t.id] ?? 'CAPACITY_INSUFFICIENT']}</p>)}<p>需要并行时，请返回任务的“安排时间”逐项确认；也可增加可用时间后重新预览。</p></>}
      {preview.notes.map(n => <p key={n}>{n}</p>)}
    </div>}
    {error && <p role="alert" className="form-error">{error}</p>}
    <footer className="inline-dialog-actions"><button className="button" disabled={busy} onClick={onClose}>取消</button><button className="button" disabled={busy || !loaded || !windows.length || windows.some(w => !w.start_local || w.start_local >= w.end_local)} onClick={() => void generate()}>{busy ? '生成中…' : dirty ? '保存时段并生成预览' : '生成预览'}</button><button className="button primary" disabled={busy || !preview?.assignments.length} onClick={() => void apply()}>确认安排</button></footer>
  </section></DialogBackdrop>
}
