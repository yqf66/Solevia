import { AssistantAnswer } from '../../ui/AssistantAnswer'
import type { CapacitySegment, PlanAdjustmentPreview, PlanWorkspace } from '../../api'
import { FixedActivityMovePanel } from './FixedActivityMovePanel'

export function CapacitySummary({ segments }: { segments: CapacitySegment[] }) {
  const exceptions = (value: Pick<CapacitySegment, 'exception_daily_minutes' | 'exception_weekdays' | 'exception_days_per_week'>) => {
    if (!value.exception_days_per_week || !value.exception_daily_minutes) return ''
    const names = (value.exception_weekdays ?? []).map(day => ['周一', '周二', '周三', '周四', '周五', '周六', '周日'][day]).join('、')
    return `；${names || `${value.exception_days_per_week} 个例外日`}各 ${value.exception_daily_minutes} 分钟`
  }
  return <ul className="capacity-segment-list">{segments.map((segment, index) => {
    const days = segment.available_days_per_week ?? segment.weekly_execution_days
    return <li key={`${segment.start_week}-${index}`}><strong>{segment.label}</strong><span>第 {segment.start_week}–{segment.end_week} 周</span><span>{days ? `每周可用 ${days} 天` : '天数待核对'} · {segment.suggested_daily_minutes ? `${segment.exception_days_per_week ? '普通日' : '每天'} ${segment.suggested_daily_minutes} 分钟` : '时间待核对'}{exceptions(segment)}</span>{segment.weekly_capacity_overrides?.map(override => <span key={override.week_index}>第 {override.week_index} 周例外：可用 {override.available_days_per_week ?? override.weekly_execution_days} 天 · 普通日 {override.suggested_daily_minutes} 分钟{exceptions(override)}</span>)}</li>
  })}</ul>
}

export function CapacityNegotiationPanel({ workspace, draft, busy, progress, error, preview, onDraft, onSubmit, onPreview, onDiscard }: {
  workspace: PlanWorkspace
  draft: string
  busy: boolean
  progress: string
  error: string
  preview: PlanAdjustmentPreview | null
  onDraft: (value: string) => void
  onSubmit: () => void
  onPreview: () => void
  onDiscard: () => void
}) {
  const conflict = workspace.capacity_negotiation
  if (!conflict) return null
  const proposal = workspace.pending_adjustment
  const previewDays = Array.isArray(preview?.plan.days) ? preview.plan.days as Array<{ week_index: number; suggested_minutes: number; tasks: Array<{ title: string }> }> : []
  return <section className="capacity-negotiation-panel" aria-label="一起调整时间与任务">
    <h2>一起找到可行的安排</h2><p>{conflict.message}</p>
    {Boolean(conflict.verified_conflicts?.length) && <ul>{conflict.verified_conflicts?.map((item, index) => <li key={index}>{item.label} · 第 {item.weeks.join('、')} 周：至少 {item.minimum_minutes} 分钟，可用 {item.available_minutes} 分钟，还差 {item.deficit_minutes} 分钟。</li>)}</ul>}
    <CapacitySummary segments={conflict.capacity_segments} />
    {conflict.preview_progress && <p aria-live="polite">完整预览：已校验 {conflict.preview_progress.completed_weeks} / {conflict.preview_progress.total_weeks ?? '待确定'} 周。{conflict.preview_progress.status === 'FAILED' ? '已完成部分保留，重试将从断点继续。' : conflict.preview_progress.status === 'RUNNING' ? '正在继续生成与校验，原安排保持不变。' : '预览已保存，确认后才应用。'}</p>}
    <p className="muted">先在原范围内调配，再讨论期限与可用时间。选项只是讨论方向，点击不会改变计划。</p>
    <div className="capacity-choice-actions">{conflict.options.map((option) => <button className="button" type="button" key={option.title} disabled={busy} onClick={() => onDraft(option.instruction)}>{option.title}</button>)}</div>
    <FixedActivityMovePanel goalId={workspace.goal_id} onDraft={onDraft} />
    <div className="capacity-negotiation-messages">{workspace.messages.filter(message => message.metadata?.intent === 'REVISE').slice(-8).map(message => <article key={message.id} className={`capacity-message ${message.role === 'USER' ? 'user' : ''}`}><strong>{message.role === 'USER' ? '你' : 'Solevia'}</strong>{message.role === 'ASSISTANT' ? <AssistantAnswer content={message.content} /> : <p>{message.content}</p>}</article>)}</div>
    {proposal && <section className="capacity-proposal"><h3>待确认的调整方向</h3><ul>{proposal.proposed_changes.map(item => <li key={item}>{item}</li>)}</ul>{proposal.capacity_segments && <CapacitySummary segments={proposal.capacity_segments} />}{proposal.tradeoffs.length > 0 && <p>代价：{proposal.tradeoffs.join('；')}</p>}<p>{proposal.unchanged_guarantees.join('；')}</p>{proposal.clarifying_question && <p className="proposal-question">{proposal.clarifying_question}</p>}</section>}
    <form onSubmit={event => { event.preventDefault(); onSubmit() }}><label htmlFor="capacity-feedback">你的选择或补充条件</label><textarea id="capacity-feedback" value={draft} onChange={event => onDraft(event.target.value)} rows={4} placeholder="例如：前两周保持每天30分钟；后两周周末可各增加30分钟，考试日期不变。" disabled={busy} /><button className="button primary" type="submit" disabled={busy || !draft.trim()}>发送并核算方案</button></form>
    {busy && <p role="status" aria-live="polite">{progress} 已有计划保持不变，可以稍后回来查看。</p>}
    {error && <p className="inline-error" role="alert">{error}</p>}
    {preview && <section className="capacity-validated-preview"><h3>已通过校验的完整预览</h3><p>共 {String(preview.plan.total_weeks)} 周。任务、频率、依赖与逐阶段容量已检查，尚未应用。</p><CapacitySummary segments={Array.isArray(preview.plan.temporal_segments) ? preview.plan.temporal_segments as CapacitySegment[] : []} /><details><summary>查看各周任务与工作量</summary>{previewDays.map((day, index) => <div key={index}><strong>第 {day.week_index} 周 · {day.suggested_minutes} 分钟</strong><ul>{day.tasks.map((task, taskIndex) => <li key={taskIndex}>{task.title}</li>)}</ul></div>)}</details></section>}
    {proposal && <div className="capacity-choice-actions"><button className="button" type="button" disabled={busy} onClick={onDiscard}>放弃此方案</button><button className="button primary" type="button" disabled={busy || !proposal.can_apply || proposal.needs_clarification || Boolean(draft.trim())} onClick={onPreview}>{draft.trim() ? '先发送补充条件' : preview ? '确认应用这份预览' : '生成并验证完整预览'}</button></div>}
  </section>
}
