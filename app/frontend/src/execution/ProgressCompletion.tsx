import type { ExecutionProgress } from '../api'
import './ProgressCompletion.css'

export function ProgressCompletion({ progress }: { progress: ExecutionProgress }) {
  const total = progress.overall_progress
  return <section className="progress-completion" aria-label="任务完成轨迹">
    <header><div><span>{progress.exploration?.next_unplanned_week ? '已展开的旅程' : '目标完成轨迹'}</span><strong>每一步，都算数。</strong></div><p><b>{total.completed_tasks}</b> / {total.total_tasks} 项任务已完成</p></header>
    <progress aria-label="整体任务完成比例" value={total.completed_tasks} max={Math.max(1, total.total_tasks)} />
    <footer><span>起点</span><span>{total.total_tasks ? `${total.percentage}% · 按已保存的任务统计` : '等待第一项任务'}</span></footer>
  </section>
}
