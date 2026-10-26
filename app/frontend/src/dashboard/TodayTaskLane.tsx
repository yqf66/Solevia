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
      <div><h2>{isMain ? '定时安排' : '灵活推进'}</h2><p>{isMain ? '已安排具体时段，按时推进' : '时间可以灵活，重要的事同样在这里'}</p></div>
      <small>{tasks ? tasks.length : '—'} 项</small>
    </header>
    {conflicts > 0 && <div className="today-conflict-note">发现 {conflicts} 个时间重叠，点击任务查看并调整。</div>}
    {!tasks ? <p className="dashboard-loading" role="status">{error ? '暂未读取到任务' : '正在整理今日任务…'}</p>
      : tasks.length > 0 ? <div className="today-task-list">{tasks.map((task, index) => <TodayTaskCard
        key={`${task.source}-${task.id}`}
        task={task} lane={lane} index={index} onOpen={onOpen} onSchedule={onSchedule}
      />)}</div>
      : <div className="today-dashboard-empty">
        <strong>{isMain ? '今天还没有安排固定时段。' : '今天没有待安排的弹性任务。'}</strong>
        <p>{isMain ? '需要固定时段时，可以为任务安排时间。' : '留一点空白，也是一种安排。'}</p>
        {isMain && <button className="text-button" type="button" onClick={() => navigate('/quick/new')}>添加安排 ↗</button>}
      </div>}
  </section>
}
