import { useState } from 'react'
import './DashboardRefinement.css'
import { useNavigate } from 'react-router-dom'
import { AllocationDialog } from '../AllocationDialog'
import type { UnifiedToday } from '../api'
import { WandSparkles } from 'lucide-react'
import { TodayTaskLane } from './TodayTaskLane'
import { ScheduleTaskDialog } from './ScheduleTaskDialog'
import { CreatePlanMenu } from './CreatePlanMenu'
import { useTodayDashboard } from './useTodayDashboard'
import { TodayFocus } from './TodayFocus'
import { TodayCalendar } from './TodayCalendar'

export function TodayDashboard() {
  const [allocationOpen, setAllocationOpen] = useState(false)
  const navigate = useNavigate()
  const dashboard = useTodayDashboard()
  const { today, setToday, error, loading, retry, openSchedule } = dashboard
  const openTask = (task: UnifiedToday['fixed_tasks'][number]) => {
    navigate(task.source === 'AGENT' ? `/goals/${task.plan_id}/today` : `/quick/${task.plan_id}`)
  }

  const tasks = today ? [...today.fixed_tasks, ...today.flexible_tasks] : []
  const completed = tasks.filter(task => task.status === 'COMPLETED').length
  const remainingMinutes = tasks.reduce((sum, task) => sum + (task.status === 'COMPLETED' ? 0 : task.estimate_minutes), 0)
  const nextTask = tasks.find(task => task.status !== 'COMPLETED')

  return <section className="today-dashboard page" aria-busy={loading}>
    {today && allocationOpen && <AllocationDialog today={today} onClose={() => setAllocationOpen(false)} onApplied={setToday} />}
    <header className="today-dashboard-header">
      <div><h1>今天要做什么</h1><p className="muted">把确定的事放进时间，把想做的事留有余地。</p></div>
      <div className="today-dashboard-actions">
        <button className="button" type="button" disabled={!today} onClick={() => setAllocationOpen(true)}><WandSparkles size={16} aria-hidden="true" /> 智能安排</button>
        <CreatePlanMenu />
      </div>
    </header>
    <div className="today-opening">
    <TodayFocus task={nextTask} settled={tasks.length > 0 && completed === tasks.length} loading={loading && !today} error={Boolean(error) && !today} onOpen={openTask} onCreate={() => navigate('/quick/new')} />
    <TodayCalendar date={today?.date} pending={tasks.length - completed} completed={completed} minutes={remainingMinutes} />
    </div>
    {today && tasks.length > 0 && <div className="today-reading-line"><p>{completed === tasks.length ? '今天的任务已完成，给自己留一点余地。' : `今天还有 ${tasks.length - completed} 项任务，选一件开始推进。`}</p></div>}
    {error && !dashboard.scheduleTarget && <div className="dashboard-error" role="alert">
      <p>{error}</p><button className="button" type="button" disabled={loading} onClick={retry}>重新加载</button>
    </div>}
    <div className={`today-dashboard-grid${today && !today.fixed_tasks.length && today.flexible_tasks.length ? ' main-lane-empty' : ''}`}>
      <TodayTaskLane lane="main" tasks={today?.fixed_tasks} conflicts={today?.conflicts.length}
        error={Boolean(error)} onOpen={openTask} onSchedule={openSchedule} />
      <TodayTaskLane lane="branch" tasks={today?.flexible_tasks}
        error={Boolean(error)} onOpen={openTask} onSchedule={openSchedule} />
    </div>
    {dashboard.scheduleTarget && <ScheduleTaskDialog dashboard={dashboard} />}
  </section>
}

