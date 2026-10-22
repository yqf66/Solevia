import { Check } from 'lucide-react'
import { useNavigate } from 'react-router-dom'
import { DependencySummary } from '../ui/DependencySummary'
import { schedulingPolicyLabels, type DashboardTask } from './taskPresentation'

type Props = { task: DashboardTask; lane: 'main' | 'branch'; index: number; onOpen: (task: DashboardTask) => void; onSchedule: (task: DashboardTask) => void }

export function TodayTaskCard({ task, lane, index, onOpen, onSchedule }: Props) {
  const navigate = useNavigate()
  return (
    <article className={`today-task-card today-task-card-${(index % 4) + 1} ${task.status === 'COMPLETED' ? 'completed' : ''}`} >
      <button className="today-task-card-main" type="button" onClick={() => onOpen(task)}>
        <div className="today-task-card-top"><span className="today-task-lane">{lane === 'main' ? '定时安排' : '灵活推进'}</span><span className="today-dashboard-task-time">{task.is_fixed ? `${task.start_local}–${task.end_local}` : task.schedule_review_required ? '时间需复核' : schedulingPolicyLabels[task.scheduling_policy ?? 'FLOATING'] ?? '弹性待安排'}</span><span className={`task-status-dot ${task.status === 'COMPLETED' ? 'done' : ''}`} aria-hidden="true">{task.status === 'COMPLETED' ? <Check size={14} /> : '·'}</span></div>
        <strong>{task.title}</strong>
        <small>{task.plan_title} · {task.estimate_minutes} 分钟 · {schedulingPolicyLabels[task.scheduling_policy ?? 'FLOATING'] ?? '弹性待安排'}{task.schedule_review_required ? ` · ${task.schedule_change_reason ?? '计划已调整'}` : ''}</small>
        <DependencySummary dependencies={task.dependencies} />
        {task.blocked_reason && <span className="plan-task-dependency">{task.blocked_reason}</span>}
      </button>
      {!task.is_fixed && task.source === 'AGENT' && task.dependencies?.some((dependency) => dependency.status !== 'COMPLETED')
        ? <button className="today-task-schedule today-task-handle" type="button" onClick={() => navigate(`/goals/${task.plan_id}/progress?recovery=1&blocked_task=${task.id}`)}>去处理</button>
        : !task.is_fixed && <button className="today-task-schedule" type="button" onClick={() => onSchedule(task)}>安排时间</button>}
    </article>
  )
}
