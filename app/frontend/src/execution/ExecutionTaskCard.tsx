import { useEffect, useRef } from 'react'
import { Check, ChevronDown } from 'lucide-react'
import type { TodayWorkspace, UnifiedToday } from '../api'
import { feedbackLabel, intentLabels, type FeedbackTarget } from './feedbackOptions'
import type { TaskProgressTarget } from './TaskProgressDialog'
import './ExecutionTaskCard.css'

type Task = NonNullable<TodayWorkspace['day']>['tasks'][number]
type ScheduledTask = UnifiedToday['fixed_tasks'][number]
type Props = {
  task: Task; position: number; tone: number; taskTitles: Map<string, string>
  latestFeedback?: TodayWorkspace['feedbacks'][number]; scheduleTask?: ScheduledTask
  viewingToday: boolean; canEditToday: boolean; busyTaskId: string
  onSchedule: (task: ScheduledTask) => void
  onUpdate: (id: string, action: 'START' | 'COMPLETE') => void
  onProgress: (target: TaskProgressTarget) => void
  onReschedule: () => void; onFeedback: (target: FeedbackTarget) => void
}

const schedulingPolicyLabels: Record<string, string> = {
  EXACT_DAY: '按计划日',
  DATE_WINDOW: '期限窗口',
  CADENCE: '节奏建议',
  QUOTA: '总量要求',
  TASK_POOL: '候选任务',
  CHECKPOINT: '阶段验收',
  TRIGGERED: '条件触发',
  FLOATING: '弹性待安排',
}

function schedulingPolicyDescription(policy: string, data: Record<string, unknown> = {}) {
  const label = schedulingPolicyLabels[policy] ?? '时间规则'
  if (policy === 'CADENCE') {
    const periods: Record<string, string> = { WEEK: '每周', DAY: '每天', MONTH: '每月' }
    const period = typeof data.period === 'string' ? periods[data.period] ?? data.period : ''
    const target = typeof data.target_count === 'number' ? `，目标 ${data.target_count} 次` : ''
    return `${label}${period ? ` · ${period}` : ''}${target}`
  }
  if (policy === 'DATE_WINDOW') {
    const window = typeof data.window === 'string' ? data.window : ''
    return `${label}${window ? ` · ${window}` : ''}`
  }
  if (policy === 'QUOTA') {
    const target = typeof data.target_count === 'number' ? ` · 目标 ${data.target_count}` : ''
    return `${label}${target}`
  }
  return label
}

function schedulingPolicyHelp(policy: string) {
  if (policy === 'EXACT_DAY') return '当天执行，不建议自行移出该日期。'
  if (policy === 'CHECKPOINT') return '阶段检查点，需在对应节点完成验收。'
  if (policy === 'CADENCE') return '本周次数必须达成，当前日期是系统建议的执行日。'
  if (policy === 'DATE_WINDOW') return '可在期限窗口内调整日期，窗口结束前完成即可。'
  if (policy === 'FLOATING') return '日期尚未锁定，可根据前置结果和当天余量安排。'
  if (policy === 'QUOTA') return '总量要求必须达成，具体日期可以灵活安排。'
  if (policy === 'TASK_POOL') return '候选任务，可从任务池中选择合适内容执行。'
  if (policy === 'TRIGGERED') return '满足触发条件后再安排，不要求今天执行。'
  return '具体时间可以根据当天情况安排。'
}


export function ExecutionTaskCard({ task, position, tone, taskTitles, latestFeedback, scheduleTask, viewingToday, canEditToday, busyTaskId, onSchedule, onUpdate, onProgress, onReschedule, onFeedback }: Props) {
  const menuRef = useRef<HTMLDetailsElement>(null)
  useEffect(() => {
    const close = (event: PointerEvent) => { if (!menuRef.current?.contains(event.target as Node)) menuRef.current?.removeAttribute('open') }
    const escape = (event: KeyboardEvent) => { if (event.key === 'Escape' && menuRef.current?.open) { menuRef.current.removeAttribute('open'); menuRef.current.querySelector('summary')?.focus() } }
    document.addEventListener('pointerdown', close)
    document.addEventListener('keydown', escape)
    return () => { document.removeEventListener('pointerdown', close); document.removeEventListener('keydown', escape) }
  }, [])
  const schedulingPolicy = task.scheduling_policy ?? 'EXACT_DAY'
  return (
      <article className={`task-card execution-task-card task-tone-${tone} ${task.status.toLowerCase()}`} key={task.id}>
        <div className="task-status-mark" aria-hidden="true">{task.status === 'COMPLETED' ? <Check size={16} /> : position}</div>
        <div>
          <header><strong>{task.title}</strong><span className="execution-task-state">{task.status === 'COMPLETED' ? '已完成' : task.status === 'IN_PROGRESS' ? '进行中' : '待开始'}</span><small className="task-duration">{task.estimate_minutes}<span>分钟</span></small></header>
          <div className={`task-rule-row task-rule-${schedulingPolicy.toLowerCase()}`}>
            <span className="task-rule-badge">{schedulingPolicyDescription(schedulingPolicy, task.scheduling_policy_data)}</span>
            {schedulingPolicy !== 'EXACT_DAY' && <small className="task-rule-help">{schedulingPolicyHelp(schedulingPolicy)}</small>}
          </div>
          <p>{task.description}</p>
          {task.depends_on.length > 0 && <div className="plan-task-dependency"><strong>前置任务</strong><span>{task.depends_on.map((id) => task.dependency_titles?.[id] ?? taskTitles.get(id) ?? (/^M\d+-W\d+-D\d+-T\d+$/.test(id) ? '待确认的前置任务' : id)).join('；')}</span></div>}
          {task.blocked_reason && <p className="plan-task-dependency" role="status">{task.blocked_reason}</p>}
          {task.evidence.length > 0 && <div className="execution-task-outcome"><span>完成后留下</span><p>{task.evidence.join('；')}</p></div>}
          {task.skip_guidance && task.skip_guidance.length > 0 && schedulingPolicy !== 'EXACT_DAY' && <small className="task-skip-guidance">跳过后：{task.skip_guidance.join(' · ')}</small>}
          {latestFeedback && <div className="task-feedback-note"><strong>已记录：</strong>{feedbackLabel(latestFeedback.reason_code)} · {latestFeedback.intent_code ? intentLabels[latestFeedback.intent_code] : '等待处理'}</div>}
          <footer>
            {!viewingToday && <span className="historical-task-note">日志回看 · 返回今天后可操作</span>}
            {scheduleTask?.is_fixed && <span className="task-time-chip">安排 {scheduleTask.start_local}–{scheduleTask.end_local}</span>}
            {canEditToday && task.status !== 'COMPLETED' && scheduleTask && <button type="button" onClick={() => onSchedule(scheduleTask)}>{scheduleTask.is_fixed ? '调整时间' : '安排时间'}</button>}
            {canEditToday && task.status === 'NOT_STARTED' && <button type="button" onClick={() => onUpdate(task.id, 'START')} disabled={Boolean(busyTaskId) || Boolean(task.blocked_reason)}>开始</button>}
            {canEditToday && task.status !== 'COMPLETED' && <button className="primary-mini" type="button" onClick={() => onUpdate(task.id, 'COMPLETE')} disabled={Boolean(busyTaskId) || Boolean(task.blocked_reason)}>标记完成</button>}
            {canEditToday && task.status !== 'COMPLETED' && schedulingPolicy !== 'EXACT_DAY' && <button type="button" onClick={() => onProgress({ ...task, action: 'PARTIAL' })} disabled={Boolean(busyTaskId) || Boolean(task.blocked_reason)}>记录部分完成</button>}
            {canEditToday && task.status !== 'COMPLETED' && <details className="execution-task-more" ref={menuRef}>
              <summary>更多安排 <ChevronDown size={14} aria-hidden="true" /></summary>
              <div className="execution-task-menu" onClick={(event) => { if ((event.target as HTMLElement).closest('button') && !(event.target as HTMLButtonElement).disabled) menuRef.current?.removeAttribute('open') }}>
            {canEditToday && task.status !== 'COMPLETED' && schedulingPolicy !== 'EXACT_DAY' && <button type="button" onClick={() => onProgress({ ...task, action: 'DEFER' })} disabled={Boolean(busyTaskId)}>今天不做</button>}
            {canEditToday && task.status !== 'COMPLETED' && schedulingPolicy !== 'EXACT_DAY' && schedulingPolicy !== 'CHECKPOINT' && schedulingPolicy !== 'TRIGGERED' && <button type="button" onClick={onReschedule} disabled={Boolean(busyTaskId)}>改到某日</button>}
            {canEditToday && task.status !== 'COMPLETED' && schedulingPolicy !== 'EXACT_DAY' && <button type="button" onClick={() => onProgress({ ...task, action: 'SKIP' })} disabled={Boolean(busyTaskId)}>本周期不做</button>}
            {canEditToday && task.status !== 'COMPLETED' && <button type="button" onClick={() => onFeedback({ kind: 'TASK', taskId: task.id, title: task.title, status: task.status, estimateMinutes: task.estimate_minutes })}>暂时做不完</button>}
              </div>
            </details>}
          </footer>
        </div>
      </article>
  )
}
