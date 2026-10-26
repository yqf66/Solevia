import { useState } from 'react'
import type { PlanAdjustmentPreview } from '../api'
import { TimePicker } from './TimePicker'
import './AdjustmentPreview.css'

type Schedule = PlanAdjustmentPreview['schedule']
const changeLabels = { RECOVERED: '补回积压', MOVED: '更换日期', MODIFIED: '内容或时段调整', UNCHANGED: '保持原安排', UNMATCHED: '新安排 · 请核对' }
const sum = (rows: Array<{ minutes: number }>) => rows.reduce((n, row) => n + row.minutes, 0)
const timeLabel = (row: { start: string | null; end: string | null }) => row.start && row.end ? `${row.start}–${row.end}` : '当天自行安排'

export function AdjustmentPreview({ preview, schedule, onChange, onRequestChange, disabled = false }: {
  preview: PlanAdjustmentPreview
  schedule: Schedule
  onChange?: (schedule: Schedule) => void
  onRequestChange: (request: string, options?: import('../api').RecoveryPreviewOptions) => void
  disabled?: boolean
}) {
  const [showAll, setShowAll] = useState(false)
  const [showEdit, setShowEdit] = useState(false)
  const [dailyLimit, setDailyLimit] = useState('40')
  const [selectedIndex, setSelectedIndex] = useState(0)
  const [targetDate, setTargetDate] = useState('')
  const comparison = preview.comparison
  const rows = schedule.map((row, index) => ({ ...row, change: comparison?.after[index], index }))
  const changed = rows.filter(row => row.change?.change_kind !== 'UNCHANGED')
  const visible = showAll ? rows : changed
  const beforeDays = new Map<string, number>()
  for (const row of comparison?.before ?? []) beforeDays.set(row.date, (beforeDays.get(row.date) ?? 0) + row.minutes)
  const afterDays = new Map<string, number>()
  for (const row of rows) afterDays.set(row.date, (afterDays.get(row.date) ?? 0) + row.minutes)
  const futureLoads = [...afterDays.entries()].filter(([date]) => !comparison || date >= comparison.anchor_date).map(([, minutes]) => minutes)
  const affectedDates = [...new Set([...beforeDays.keys(), ...afterDays.keys()])].sort().filter(date => (beforeDays.get(date) ?? 0) !== (afterDays.get(date) ?? 0))
  const selected = rows[selectedIndex]
  function updateTime(index: number, start: string | null, end: string | null) {
    onChange?.(schedule.map((row, i) => i === index ? { ...row, start, end, state: start && end ? 'SCHEDULED' : 'UNALLOCATED' } : row))
  }
  return <section className="adjustment-preview" aria-label="调整前后对照">
    <header><div><span>确认前的最后一步</span><h3>这次安排，具体改了什么</h3></div><span className="adjustment-draft-status">仅预览 · 尚未发布</span></header>
    {comparison ? <>
      <div className="adjustment-change-facts">
        <div><span>结束日期</span><strong>{comparison.proposed_end_date === comparison.original_end_date ? '保持不变' : `${comparison.original_end_date} → ${comparison.proposed_end_date}`}</strong><small>{comparison.proposed_end_date === comparison.original_end_date ? comparison.original_end_date : '采用新日期前需要确认'}</small></div>
        <div><span>本次补回</span><strong>{rows.filter(row => row.change?.change_kind === 'RECOVERED').length} 项 · {sum(rows.filter(row => row.change?.change_kind === 'RECOVERED'))} 分钟</strong><small>从原日期移到后续日期</small></div>
        <div><span>最高单日用时</span><strong>{Math.max(0, ...futureLoads)} 分钟</strong><small>包含当天原任务与补回任务</small></div>
      </div>
      <div className="adjustment-load-comparison"><strong>每天的负担如何变化</strong>{affectedDates.length ? affectedDates.map(date => <div key={date}><time>{date}</time><span>{beforeDays.get(date) ?? 0} 分钟 <b aria-hidden="true">→</b> <strong>{afterDays.get(date) ?? 0} 分钟</strong></span><small>{date < comparison.anchor_date ? '未完成内容移出，历史记录保留' : (afterDays.get(date) ?? 0) > (beforeDays.get(date) ?? 0) ? `增加 ${(afterDays.get(date) ?? 0) - (beforeDays.get(date) ?? 0)} 分钟` : '减少当日任务'}</small></div>) : <p>每日用时不变，下面核对内容或时段变化。</p>}</div>
      {comparison.backlog_tasks > 0 && <p className="adjustment-habit-note">补回的是未完成的工作量。过去漏掉的日期仍保留真实记录，不会因补回就变成当日已完成。</p>}
    </> : <p className="adjustment-preview-notice">这份草案没有可核对的原日期记录，下面展示新安排；请先核对调整建议中的变化，再确认发布。</p>}
    <div className="adjustment-row-heading"><strong>{showAll ? '全部新安排' : `变化明细 · ${changed.length} 项`}</strong>{comparison && rows.length > changed.length && <button type="button" onClick={() => setShowAll(!showAll)}>{showAll ? '只看有变化的任务' : `查看全部 ${rows.length} 项（含未变动）`}</button>}</div>
    <div className="adjustment-change-list">{visible.map(({ change, index, ...row }) => <article key={index} className={`adjustment-change-row ${change?.change_kind === 'RECOVERED' ? 'is-recovered' : ''}`}>
      <div><span className="adjustment-change-label">{change ? changeLabels[change.change_kind] : '新安排'}</span><strong>{row.title}</strong><small>{row.minutes} 分钟{change?.source_minutes != null && change.source_minutes !== row.minutes ? `（原 ${change.source_minutes} 分钟）` : ''}</small></div>
      <div className="adjustment-date-pair"><span><small>原安排</small>{change?.source_date ? <><time>{change.source_date}</time><small>{timeLabel({ start: change.source_start, end: change.source_end })}</small></> : <small>没有可靠对应记录</small>}</span><b aria-hidden="true">→</b><span><small>调整后</small><time>{row.date}</time><small>{row.state === 'FLEXIBLE' ? '当天自行安排' : row.start && row.end ? timeLabel(row) : '尚无可用时段'}</small></span></div>
      {onChange && row.state !== 'FLEXIBLE' && schedule.filter(item => item.date === row.date && item.title === row.title).length === 1 && <details className="adjustment-time-controls"><summary>修改具体时段</summary><div><label>开始<TimePicker aria-label={`${row.title} ${row.date} 开始`} disabled={disabled} value={row.start ?? ''} onChange={event => updateTime(index, event.target.value || null, row.end)} /></label><label>结束<TimePicker aria-label={`${row.title} ${row.date} 结束`} disabled={disabled} value={row.end ?? ''} onChange={event => updateTime(index, row.start, event.target.value || null)} /></label></div><small>发布时重新校验可用窗口、时长、冲突和前置关系。</small></details>}
    </article>)}</div>
    {!schedule.length && <p>本次先核对工作项与窗口；具体日期会在开始计划前预览。</p>}
    {rows.some(row => row.state === 'FLEXIBLE') && <p className="adjustment-preview-notice">当前采用自由安排：日期已确定，几点开始由你决定，无需填写空白的起止时间。</p>}
    <div className="adjustment-preview-actions"><strong>这个分配不合适？</strong><div><button type="button" disabled={disabled} onClick={() => onRequestChange('请把这次待补回的任务分散到更多后续执行日，尽量降低最高单日用时。保留原结束日期、全部任务和已完成记录；先重新预览，不直接发布。', comparison?.backlog_tasks ? { action: 'SPREAD' } : undefined)}>分散到更多天</button><button type="button" disabled={disabled} onClick={() => setShowEdit(!showEdit)} aria-expanded={showEdit}>指定日期 / 每天用时</button>{schedule.some((row, index) => row.start !== preview.schedule[index]?.start || row.end !== preview.schedule[index]?.end) && <button type="button" disabled={disabled} onClick={() => onChange?.(preview.schedule)}>还原预览时段</button>}</div></div>
    {showEdit && <div className="adjustment-request-editor">
      <p>日期和每日用时会影响容量与任务依赖。把要求带入下方讨论，发送后重新生成对照，再由你确认。</p>
      <form onSubmit={event => { event.preventDefault(); onRequestChange(`我希望保持结束日期，每天总用时（含原任务和补回）最多 ${dailyLimit} 分钟。请在这个上限内分配全部剩余任务，放不下就明确说明缺口并让我选择，不得超时或删任务。先重新预览，不发布。`, comparison?.backlog_tasks ? { action: 'DAILY_LIMIT', daily_limit_minutes: Number(dailyLimit) } : undefined) }}><label>每天总用时上限（分钟）<input type="number" required min="5" max="1440" step="1" disabled={disabled} value={dailyLimit} onChange={event => setDailyLimit(event.target.value)} /></label><button className="button" type="submit" disabled={disabled}>按这个上限继续讨论</button></form>
      {rows.length > 0 && <form onSubmit={event => { event.preventDefault(); if (selected) onRequestChange(`希望把“${selected.title}”（${selected.change?.source_date ? `原日期 ${selected.change.source_date}，` : ''}草案日期 ${selected.date}，${selected.minutes} 分钟）改到 ${targetDate}。请重新核对其他任务、可用容量与前置关系，展示调整前后差异。保留全部任务和进度，先预览，不发布。`, selected.change?.change_kind === 'RECOVERED' && selected.change.source_task_id ? { action: 'MOVE_TASK', source_task_id: selected.change.source_task_id, target_date: targetDate } : undefined) }}><label>选择要换日期的任务<select disabled={disabled} value={selectedIndex} onChange={event => setSelectedIndex(Number(event.target.value))}>{rows.map((row, index) => <option key={index} value={index}>{row.title} · {row.change?.source_date ?? row.date} → {row.date}</option>)}</select></label><label>希望安排到<input type="date" required min={comparison?.anchor_date} disabled={disabled} value={targetDate} onChange={event => setTargetDate(event.target.value)} /></label><button className="button" type="submit" disabled={disabled}>按这个日期继续讨论</button></form>}
    </div>}
  </section>
}
