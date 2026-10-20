import { ArrowUpRight } from 'lucide-react'
import { localDate } from '../lightApi'
import { PlanningGuide } from '../ui/PlanningGuide'
import './QuickPlanIntake.css'

type Props = {
  input: string
  start: string
  days: number
  busy: boolean
  onInput: (value: string) => void
  onStart: (value: string) => void
  onDays: (value: number) => void
  onCreate: (useModel: boolean) => void
}

export function QuickPlanIntake({ input, start, days, busy, onInput, onStart, onDays, onCreate }: Props) {
  const invalid = busy || !input.trim() || !start || !Number.isInteger(days) || days < 1 || days > 14
  return <div className="quick-intake-layout">
    <form className="light-input quick-intake" onSubmit={event => { event.preventDefault(); if (!invalid) onCreate(true) }}>
      <div className="quick-input-heading"><label htmlFor="light-input">想做哪些事？</label><span>不用排序，先写下来</span></div>
      <textarea id="light-input" rows={6} value={input} onChange={event => onInput(event.target.value)} maxLength={12000}
        placeholder={'明天改简历，练半小时口语，\n再整理一下照片……'} disabled={busy} required />
      <div className="quick-time-settings">
        <div className="quick-time-heading"><strong>留出一段时间</strong><p>先确定范围，具体安排还可以调整。</p></div>
        <div className="light-range">
          <label>开始日期<input type="date" required value={start} onChange={event => onStart(event.target.value)} disabled={busy} /></label>
          <fieldset className="light-segment"><legend>安排范围</legend>{[[1, '一天'], [7, '一周'], [14, '两周']].map(([value, label]) => <button type="button" key={value} aria-pressed={days === value} disabled={busy} onClick={() => onDays(Number(value))}>{label}</button>)}</fieldset>
          <label className="light-days">天数<input type="number" min={1} max={14} required value={days} disabled={busy} onChange={event => onDays(Number(event.target.value))} /></label>
          <button className="text-button quick-tomorrow" type="button" disabled={busy} onClick={() => { onStart(localDate(1)); onDays(1) }}>只安排明天</button>
        </div>
      </div>
      <footer className="light-actions quick-intake-actions">
        <button className="text-button" type="button" disabled={invalid} onClick={() => onCreate(false)}>直接手动安排</button>
        <button className="button primary" type="submit" disabled={invalid}>{busy ? '正在保存…' : '整理成任务'}<ArrowUpRight size={17} aria-hidden="true" /></button>
      </footer>
    </form>
    <PlanningGuide kind="quick" />
  </div>
}
