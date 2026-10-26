import type { PlanDay } from '../../api'
import { PlanTaskDetail } from './PlanTaskDetail'
import type { ReviewTask } from './planReviewProjection'

export function PlanDayCard({
  day,
  allDays,
  scheduleType = 'RHYTHM',
  taskConnections,
}: {
  day: PlanDay
  allDays: PlanDay[]
  scheduleType?: 'OUTCOME' | 'RHYTHM' | 'CALENDAR'
  taskConnections?: ReviewTask[]
}) {
  return <details className="plan-day-card">
    <summary>
      <span>{scheduleType === 'OUTCOME' ? `窗口 ${day.week_index}` : `M${day.stage_order}-W${day.week_index}-D${day.day_index}`}</span>
      <strong>{day.primary_goal}</strong>
      <small>建议约 {taskConnections ? day.tasks.reduce((total, task) => total + task.estimate_minutes, 0) : day.suggested_minutes} 分钟</small>
    </summary>
    <div className="plan-day-detail">
      <h4>你需要完成</h4>
      <ol>
        {day.tasks.map((task) => <li key={task.id} data-plan-task-id={task.id}>
          <PlanTaskDetail task={task} allDays={allDays} connection={taskConnections?.find((entry) => entry.task.id === task.id)} />
        </li>)}
      </ol>
      {day.evidence.length > 0 && <div className="plan-day-evidence"><strong>{scheduleType === 'OUTCOME' ? '本窗口可能的产出或收获' : '当天可能的产出或收获'}</strong><p>{day.evidence.join('；')}</p></div>}
      {day.do_not.length > 0 && <div className="plan-day-dont"><strong>{scheduleType === 'OUTCOME' ? '本窗口先不要做' : '这一天先不要做'}</strong><p>{day.do_not.join('；')}</p></div>}
    </div>
  </details>
}
