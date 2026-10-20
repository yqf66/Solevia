import type { PlanDay } from '../../api'

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

export function PlanDayCard({
  day,
  allDays,
  scheduleType = 'RHYTHM',
}: {
  day: PlanDay
  allDays: PlanDay[]
  scheduleType?: 'OUTCOME' | 'RHYTHM' | 'CALENDAR'
}) {
  const allTasks = allDays.flatMap((item) => item.tasks)
  const dependenciesFor = (task: PlanDay['tasks'][number]) => task.depends_on.map((reference) => {
    let resolved = allTasks.find((item) => item.id === reference)
    const canonicalMatch = reference.match(/^M(\d+)-W(\d+)-D(\d+)-T(\d+)$/i)
    if (!resolved && canonicalMatch) {
      const [, stageOrder, weekIndex, dayIndex, taskOrder] = canonicalMatch.map(Number)
      const targetDay = allDays.find((item) => (
        item.stage_order === stageOrder
        && item.week_index === weekIndex
        && item.day_index === dayIndex
      ))
      resolved = targetDay?.tasks.find((item) => item.order === taskOrder)
    }
    const match = reference.match(/第\s*([一二三四五六七八九十\d]+)\s*[天日].*?任务\s*(\d+)/)
    if (!resolved && match) {
      const numerals: Record<string, number> = { 一: 1, 二: 2, 三: 3, 四: 4, 五: 5, 六: 6, 七: 7, 八: 8, 九: 9, 十: 10 }
      const dayNumber = /^\d+$/.test(match[1])
        ? Number(match[1])
        : match[1].includes('十')
          ? (match[1].startsWith('十') ? 10 : numerals[match[1][0]] * 10) + (match[1].endsWith('十') ? 0 : numerals[match[1].at(-1) ?? ''] ?? 0)
          : numerals[match[1]]
      const targetDay = [...allDays].sort((a, b) => a.week_index - b.week_index || a.day_index - b.day_index)[dayNumber - 1]
      resolved = targetDay?.tasks.find((item) => item.order === Number(match[2]))
    }
    return { reference, resolved }
  })

  return <details className="plan-day-card">
    <summary>
      <span>{scheduleType === 'OUTCOME' ? `窗口 ${day.week_index}` : `M${day.stage_order}-W${day.week_index}-D${day.day_index}`}</span>
      <strong>{day.primary_goal}</strong>
      <small>建议约 {day.suggested_minutes} 分钟</small>
    </summary>
    <div className="plan-day-detail">
      <h4>你需要完成</h4>
      <ol>
        {day.tasks.map((task) => <li key={task.id}>
          <div><strong>{task.title}</strong>{!task.is_required && <em>可选</em>}<span>约 {task.estimate_minutes} 分钟</span><em className="task-policy-label">{schedulingPolicyLabels[task.scheduling_policy ?? 'EXACT_DAY'] ?? '时间规则'}</em></div>
          <p>{task.description}</p>
          {task.depends_on.length > 0 && <div className="plan-task-dependency"><strong>前置任务</strong><span>{dependenciesFor(task).map(({ reference, resolved }) => `${resolved?.title ?? reference} · ${resolved?.status === 'COMPLETED' ? '已完成' : resolved ? '未完成' : '无法识别'}`).join('；')}</span></div>}
          {task.evidence.length > 0 && <small>建议产出或收获：{task.evidence.join('；')}</small>}
        </li>)}
      </ol>
      {day.evidence.length > 0 && <div className="plan-day-evidence"><strong>{scheduleType === 'OUTCOME' ? '本窗口可能的产出或收获' : '当天可能的产出或收获'}</strong><p>{day.evidence.join('；')}</p></div>}
      {day.do_not.length > 0 && <div className="plan-day-dont"><strong>{scheduleType === 'OUTCOME' ? '本窗口先不要做' : '这一天先不要做'}</strong><p>{day.do_not.join('；')}</p></div>}
    </div>
  </details>
}
