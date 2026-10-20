import { useNavigate } from 'react-router-dom'
import { TodayTaskCard } from './TodayTaskCard'
import type { DashboardTask } from './taskPresentation'

type Props = {
  lane: 'main' | 'branch'
  tasks?: DashboardTask[]
  conflicts?: number
  error: boolean
  onOpen: (task: DashboardTask) => void
  onSchedule: (task: DashboardTask) => void
}

export function TodayTaskLane({ lane, tasks, conflicts = 0, error, onOpen, onSchedule }: Props) {
  const navigate = useNavigate()
  const isMain = lane === 'main'

  return <section className={`today-dashboard-column today-lane-${lane}`}>
    <header>
      <div><h2>{isMain ? '主线任务' : '支线任务'}</h2><p>{isMain ? '固定时间，专心推进' : '灵活安排，从容完成'}</p></div>
      <small>{tasks ? tasks.length : '—'} 项</small>
    </header>
    {conflicts > 0 && <div className="today-conflict-note">发现 {conflicts} 个时间重叠，点击任务查看并调整。</div>}
    {!tasks ? <p className="dashboard-loading" role="status">{error ? '暂未读取到任务' : '正在整理今日任务…'}</p>
      : tasks.length > 0 ? tasks.map((task, index) => <TodayTaskCard
        key={`${task.source}-${task.id}`}
        task={task} lane={lane} index={index} onOpen={onOpen} onSchedule={onSchedule}
      />)
      : <div className="today-dashboard-empty">
        <strong>{isMain ? '今天还没有主线任务。' : '今天没有待安排的支线任务。'}</strong>
        <p>{isMain ? '给重要的事，一个确定的时间。' : '留一点空白，也是一种安排。'}</p>
        {isMain && <button className="text-button" type="button" onClick={() => navigate('/quick/new')}>添加安排 ↗</button>}
      </div>}
  </section>
}
