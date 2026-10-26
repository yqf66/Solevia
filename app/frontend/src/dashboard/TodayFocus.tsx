import { ArrowUpRight } from 'lucide-react'
import type { DashboardTask } from './taskPresentation'
import './TodayFocus.css'

type Props = { task?: DashboardTask; settled: boolean; loading: boolean; error: boolean; completed: number; total: number; onOpen: (task: DashboardTask) => void; onCreate: () => void }

export function TodayFocus({ task, settled, loading, error, completed, total, onOpen, onCreate }: Props) {
  return <div className="today-focus">
    <div className="focus-particles" aria-hidden="true"><i /><i /><i /><i /><i /></div>
    <div className="today-focus-heading"><span>{loading ? '正在整理今天' : error ? '今日任务暂未更新' : task ? '从这一件开始' : settled ? '今天的任务已完成' : '给今天，留一个起点'}</span><span className="focus-path" aria-hidden="true"><i /><span /><ArrowUpRight size={20} /></span></div>
    <div className="today-focus-body">
      <h2>{loading ? '稍等，正在读取你的安排。' : error ? '恢复任务后，再决定下一步。' : task ? task.title : settled ? '今天走到这里，就很好。' : '重要的事，从一小步开始。'}</h2>
      <p>{task ? `${task.plan_title} · 预计 ${task.estimate_minutes} 分钟` : error ? '下方仍可重新加载，已有安排不会丢失。' : settled ? '给自己留一点余地，也可以写下接下来的想法。' : '写下想做的事，Solevia 陪你把它放进日常。'}</p>
      {task?.description && task.description !== task.title && <p className="focus-task-description">{task.description}</p>}
    </div>
    <div className="today-focus-footer">
      {!loading && !error && <button className="today-focus-action" type="button" onClick={() => task ? onOpen(task) : onCreate()}>{task ? '查看这项任务' : settled ? '安排接下来的事' : '创建一份安排'}<ArrowUpRight size={18} aria-hidden="true" /></button>}
      {!loading && !error && total > 0 && <div className="focus-momentum"><span>今日步伐 <strong>{completed} / {total}</strong></span><div role="progressbar" aria-label="今日任务完成进度" aria-valuemin={0} aria-valuemax={total} aria-valuenow={completed}><i style={{ transform: `scaleX(${completed / total})` }} /></div></div>}
    </div>
  </div>
}
