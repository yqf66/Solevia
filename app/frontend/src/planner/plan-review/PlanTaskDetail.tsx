import type { PlanDay, PlanTask } from '../../api'
import type { ReviewTask } from './planReviewProjection'

const schedulingPolicyLabels: Record<string, string> = {
  EXACT_DAY: '按计划日', DATE_WINDOW: '期限窗口', CADENCE: '节奏建议',
  QUOTA: '总量要求', TASK_POOL: '候选任务', CHECKPOINT: '阶段验收',
  TRIGGERED: '条件触发', FLOATING: '弹性待安排',
}

function dependencyTitle(reference: string, allDays: PlanDay[]) {
  const allTasks = allDays.flatMap((day) => day.tasks)
  let resolved = allTasks.find((task) => task.id === reference)
  const canonical = reference.match(/^M(\d+)-W(\d+)-D(\d+)-T(\d+)$/i)
  if (!resolved && canonical) {
    const [, stage, week, day, order] = canonical.map(Number)
    resolved = allDays.find((item) => item.stage_order === stage && item.week_index === week && item.day_index === day)
      ?.tasks.find((task) => task.order === order)
  }
  const match = reference.match(/第\s*([一二三四五六七八九十\d]+)\s*[天日].*?任务\s*(\d+)/)
  if (!resolved && match) {
    const numerals: Record<string, number> = { 一: 1, 二: 2, 三: 3, 四: 4, 五: 5, 六: 6, 七: 7, 八: 8, 九: 9, 十: 10 }
    const number = /^\d+$/.test(match[1]) ? Number(match[1]) : match[1].includes('十')
      ? (match[1].startsWith('十') ? 10 : numerals[match[1][0]] * 10) + (match[1].endsWith('十') ? 0 : numerals[match[1].at(-1) ?? ''] ?? 0)
      : numerals[match[1]]
    resolved = [...allDays].sort((a, b) => a.week_index - b.week_index || a.day_index - b.day_index)[number - 1]
      ?.tasks.find((task) => task.order === Number(match[2]))
  }
  return `${resolved?.title ?? reference} · ${resolved?.status === 'COMPLETED' ? '已完成' : resolved ? '未完成' : '无法识别'}`
}

export function PlanTaskDetail({ task, allDays, connection, stageOrder }: {
  task: PlanTask
  allDays: PlanDay[]
  connection?: ReviewTask
  stageOrder?: number
}) {
  const contribution = connection?.stages.find((stage) => stage.order === stageOrder)
  return <>
    <div><strong>{task.title}</strong>{!task.is_required && <em>可选</em>}<span>约 {task.estimate_minutes} 分钟</span><em className="task-policy-label">{schedulingPolicyLabels[task.scheduling_policy ?? 'EXACT_DAY'] ?? '时间规则'}</em></div>
    {connection && <div className="plan-task-connections">
      <span>W{connection.day.week_index} · D{connection.day.day_index}</span>
      {connection.stages.length > 0 && <span>{connection.stages.map((stage) => `M${stage.order}`).join(' / ')}</span>}
      {connection.workItems.length > 0 && <span>对应工作项：{connection.workItems.map((item) => item.title).join('、')}</span>}
      {connection.stages.length > 1 && <small>同一任务关联多个阶段，工时只计一次。</small>}
    </div>}
    {contribution?.action && <p className="plan-stage-contribution">本阶段：{contribution.action}</p>}
    <p>{task.description}</p>
    {task.depends_on.length > 0 && <div className="plan-task-dependency"><strong>前置任务</strong><span>{task.depends_on.map((reference) => dependencyTitle(reference, allDays)).join('；')}</span></div>}
    {task.evidence.length > 0 && <small>建议产出或收获：{task.evidence.join('；')}</small>}
  </>
}
