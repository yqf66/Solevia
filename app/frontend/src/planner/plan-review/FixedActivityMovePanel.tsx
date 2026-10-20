import { TimePicker } from '../../ui/TimePicker'
import { useState } from 'react'
import { applyFixedActivity, discardFixedActivity, getFixedActivities, previewFixedActivity, type FixedActivityMovePreview } from '../../api'
import { dateInputInTimezone } from '../../ui/plannerPresentation'

export function FixedActivityMovePanel({ goalId, onDraft }: { goalId: string; onDraft: (value: string) => void }) {
  const [open, setOpen] = useState(false)
  const [source, setSource] = useState(dateInputInTimezone('Asia/Shanghai', 1))
  const [target, setTarget] = useState(source)
  const [activities, setActivities] = useState<Array<{ id: string; title: string; start_local: string; end_local: string }>>([])
  const [selected, setSelected] = useState('')
  const [start, setStart] = useState('20:00')
  const [end, setEnd] = useState('21:00')
  const [preview, setPreview] = useState<FixedActivityMovePreview | null>(null)
  const [busy, setBusy] = useState('')
  const [error, setError] = useState('')
  const [loaded, setLoaded] = useState(false)
  async function run(label: string, action: () => Promise<void>) {
    setBusy(label); setError('')
    try { await action() } catch (caught) { setError(caught instanceof Error ? caught.message : '操作暂时失败，请重试。') }
    finally { setBusy('') }
  }
  function invalidate() { setPreview(null); setError('') }
  return <section className="fixed-activity-panel">
    <button className="button" type="button" onClick={() => setOpen(!open)} aria-expanded={open}>查看固定活动调整</button>
    {open && <div className="fixed-activity-editor">
      <p className="muted">选择已记录的一次固定活动，先检查迁移的影响。作息中的睡眠、三餐保持原样。</p>
      <div className="fixed-activity-fields">
        <label>原活动日期<input type="date" value={source} disabled={Boolean(busy)} onChange={e => { setSource(e.target.value); setLoaded(false); setSelected(''); setActivities([]); invalidate() }} /></label>
        <button type="button" className="button" disabled={Boolean(busy) || !source} onClick={() => void run('正在读取活动…', async () => {
          const result = await getFixedActivities(goalId, source); setActivities(result.activities); setLoaded(true); setSelected(''); invalidate()
        })}>读取当日活动</button>
        {loaded && !activities.length && <p>当天没有可调整的固定活动。请选择其他日期，或先在快速安排中记录活动。</p>}
        {activities.length > 0 && <label>选择活动<select value={selected} disabled={Boolean(busy)} onChange={e => { setSelected(e.target.value); invalidate(); const item = activities.find(a => a.id === e.target.value); if (item) { setStart(item.start_local); setEnd(item.end_local) } }}><option value="">请选择</option>{activities.map(item => <option key={item.id} value={item.id}>{item.title} · {item.start_local}–{item.end_local}</option>)}</select></label>}
        <label>调整到日期<input type="date" value={target} disabled={Boolean(busy)} onChange={e => { setTarget(e.target.value); invalidate() }} /></label>
        <label>开始时间<TimePicker value={start} disabled={Boolean(busy)} onChange={e => { setStart(e.target.value); invalidate() }} /></label>
        <label>结束时间<TimePicker value={end} disabled={Boolean(busy)} onChange={e => { setEnd(e.target.value); invalidate() }} /></label>
      </div>
      <button className="button" type="button" disabled={Boolean(busy) || !selected || !source || !target || !start || !end} onClick={() => void run('正在核算新旧位置与冲突…', async () => {
        setPreview(null); setPreview(await previewFixedActivity(goalId, { task_id: selected, source_date: source, target_date: target, start_local: start, end_local: end }))
      })}>预览活动调整</button>
      {busy && <p role="status" aria-live="polite">{busy}</p>}
      {error && <p role="alert" className="error">{error}</p>}
      {preview && <div className="fixed-activity-preview">
        <h3>{preview.status === 'APPLIED' ? '活动调整已生效' : '待确认的活动调整'}：{preview.title}</h3>
        <p>原安排：{preview.before.date} {preview.before.start_local}–{preview.before.end_local}</p>
        <p>新安排：{preview.after.date} {preview.after.start_local}–{preview.after.end_local}</p>
        <ul>{preview.capacity_changes.map(item => <li key={item.date}>{item.date}：共享剩余时间 {item.before_minutes} → {item.after_minutes} 分钟（{item.delta_minutes > 0 ? '+' : ''}{item.delta_minutes}）</li>)}</ul>
        <p>{preview.message}</p>
        {preview.conflicts.length > 0 && <div role="alert"><strong>请先解决冲突</strong><ul>{preview.conflicts.map(item => <li key={item}>{item}</li>)}</ul></div>}
        {preview.status === 'PREVIEW' && <div className="capacity-choice-actions">
          <button className="button" disabled={Boolean(busy)} onClick={() => void run('正在取消预览…', async () => { await discardFixedActivity(goalId, preview.id); setPreview(null) })}>取消此次调整</button>
          <button className="button primary" disabled={Boolean(busy) || !preview.can_apply} onClick={() => void run('正在确认活动迁移…', async () => {
            const applied = await applyFixedActivity(goalId, preview.id); setPreview(applied)
            onDraft(`我已确认将“${applied.title}”从${applied.before.date} ${applied.before.start_local}–${applied.before.end_local}迁到${applied.after.date} ${applied.after.start_local}–${applied.after.end_local}。请根据实际共享日历重新核算本规划可用时间；这只是一次迁移，不能推广成每周增时，其他活动和任务工作量保持不变。`)
          })}>确认这一次活动迁移</button>
        </div>}
        {preview.status === 'APPLIED' && <p>调整说明已填入下方对话，请补充要给本规划使用的时间，再发送核算。</p>}
      </div>}
    </div>}
  </section>
}
