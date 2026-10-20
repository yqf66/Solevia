import { TimePicker } from './ui/TimePicker'
import { useEffect, useState } from 'react'
import { getScheduleFoundation, replaceAvailabilityWindows, updateSchedulePreference, type ScheduleFoundation } from './api'

export function ScheduleOptions({ goalId }: { goalId: string }) {
  const [value, setValue] = useState<ScheduleFoundation | null>(null)
  const [busy, setBusy] = useState(false)
  const [notice, setNotice] = useState('')
  useEffect(() => { let active = true; getScheduleFoundation(goalId).then(v => { if (active) setValue(v.preference.mode === 'FIXED' ? { ...v, preference: { ...v.preference, mode: 'BLOCKS' } } : v) }).catch(() => { if (active) setNotice('暂时无法读取时间设置') }); return () => { active = false } }, [goalId])
  async function save() {
    if (!value) return
    setBusy(true); setNotice('')
    try {
      await replaceAvailabilityWindows(goalId, { timezone: value.preference.timezone, windows: value.availability_windows })
      await updateSchedulePreference(goalId, {
        mode: value.preference.mode,
        reminders_enabled: value.preference.reminders_enabled,
        supervision_mode: value.preference.supervision_mode,
        cross_plan_priority_mode: value.preference.cross_plan_priority_mode,
        quiet_start_local: value.preference.quiet_start_local || null,
        quiet_end_local: value.preference.quiet_end_local || null,
      })
      setNotice('已保存，后续调整和恢复沿用此安排方式。')
    } catch (e) { setNotice(e instanceof Error ? e.message : '保存失败') }
    finally { setBusy(false) }
  }
  return <details className="setup-section"><summary>执行时间安排</summary>
    {value && <><label>安排方式<select value={value.preference.mode} disabled={busy} onChange={e => setValue({ ...value, preference: { ...value.preference, mode: e.target.value as ScheduleFoundation['preference']['mode'] } })}>
      <option value="BLOCKS">让 AI 安排时间 · 根据当天空闲时间分配</option><option value="FLEXIBLE">不需要 AI 安排 · 自己决定什么时候做</option>
    </select></label><p>已有固定任务优先保留；时间不足的任务会等待你处理。</p>
    <label>监督模式<select value={value.preference.supervision_mode} disabled={busy} onChange={e => setValue({ ...value, preference: { ...value.preference, supervision_mode: e.target.value as ScheduleFoundation['preference']['supervision_mode'] } })}><option value="QUIET">安静：只在 Today 展示</option><option value="STANDARD">标准：风险时提醒</option><option value="INTENSIVE">强监督：更积极提示风险</option></select></label>
    <label>跨规划优先级<select value={value.preference.cross_plan_priority_mode} disabled={busy} onChange={e => setValue({ ...value, preference: { ...value.preference, cross_plan_priority_mode: e.target.value as ScheduleFoundation['preference']['cross_plan_priority_mode'] } })}><option value="BALANCED">平衡：优先级与截止期综合</option><option value="PRIORITY">任务优先级优先</option><option value="DEADLINE">截止期优先</option></select></label>
    <label><span>风险提醒</span><input type="checkbox" checked={value.preference.reminders_enabled} disabled={busy} onChange={e => setValue({ ...value, preference: { ...value.preference, reminders_enabled: e.target.checked } })} /> 启用承诺风险提醒</label>
    <div className="schedule-fields"><label>安静开始<TimePicker value={value.preference.quiet_start_local || ''} disabled={busy} onChange={e => setValue({ ...value, preference: { ...value.preference, quiet_start_local: e.target.value || null } })} /></label><label>安静结束<TimePicker value={value.preference.quiet_end_local || ''} disabled={busy} onChange={e => setValue({ ...value, preference: { ...value.preference, quiet_end_local: e.target.value || null } })} /></label></div>
    {value.availability_windows.map((w, i) => <div className="schedule-fields" key={i}><label>星期<select value={w.weekday} onChange={e => setValue({ ...value, availability_windows: value.availability_windows.map((v, n) => n === i ? { ...v, weekday: Number(e.target.value) } : v) })}>{['一', '二', '三', '四', '五', '六', '日'].map((d, n) => <option key={n} value={n}>周{d}</option>)}</select></label>
      <label>开始<TimePicker value={w.start_local} onChange={e => setValue({ ...value, availability_windows: value.availability_windows.map((v, n) => n === i ? { ...v, start_local: e.target.value } : v) })} /></label>
      <label>结束<TimePicker value={w.end_local} onChange={e => setValue({ ...value, availability_windows: value.availability_windows.map((v, n) => n === i ? { ...v, end_local: e.target.value } : v) })} /></label>
      <button type="button" onClick={() => setValue({ ...value, availability_windows: value.availability_windows.filter((_, n) => n !== i) })}>移除</button></div>)}
      <div className="inline-dialog-actions"><button className="button" disabled={busy} onClick={() => setValue({ ...value, availability_windows: [...value.availability_windows, { id: '', goal_id: goalId, weekday: 0, start_local: '19:00', end_local: '21:00', valid_from: null, valid_to: null, timezone: value.preference.timezone, source: 'USER', created_at: '', updated_at: '' }] })}>＋ 每周可用时间</button><button className="button primary" disabled={busy} onClick={() => void save()}>{busy ? '保存中…' : '保存时间设置'}</button></div></>}
    {notice && <p role="status">{notice}</p>}
  </details>
}
