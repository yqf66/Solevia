import { CalendarRange, CheckCircle2, GitBranch, Repeat2, Sparkles } from 'lucide-react'
import type { PlanWorkItem, PlanWorkspace } from '../../api'
import { PlanDayCard } from './PlanDayCard'
import { PlanTaskDetail } from './PlanTaskDetail'
import { objectValue, projectPlanTasks, type ReviewTask } from './planReviewProjection'

type HybridPlan = NonNullable<PlanWorkspace['plan']>

const bindingLabels: Record<string, string> = {
  FIXED: '固定位置',
  WINDOW: '时间窗口',
  CADENCE: '重复节奏',
  ON_DEMAND: '按需选择',
  TRIGGERED: '条件触发',
}

function timingSummary(item: PlanWorkItem) {
  const temporal = objectValue(item.constraints.temporal)
  const recurrence = objectValue(item.constraints.recurrence)
  const startWeek = typeof temporal.start_week === 'number' ? temporal.start_week : null
  const endWeek = typeof temporal.end_week === 'number' ? temporal.end_week : null
  const exactDate = typeof temporal.exact_date === 'string' ? temporal.exact_date : null
  const deadline = typeof temporal.deadline === 'string' ? temporal.deadline : null
  if (item.binding_mode === 'CADENCE') {
    const count = typeof recurrence.target_count === 'number' ? recurrence.target_count : item.target_value
    const period = recurrence.period === 'DAY' ? '天' : recurrence.period === 'MONTH' ? '月' : '周'
    return `每${period} ${count} 次，可结合实际状态安排`
  }
  if (item.binding_mode === 'TRIGGERED') return '满足触发条件后再安排'
  if (item.binding_mode === 'ON_DEMAND') return `从候选内容中完成 ${item.target_value} 项`
  if (exactDate) return `${exactDate} 前后固定完成`
  if (deadline) return `${deadline} 前完成`
  if (startWeek && endWeek) {
    return startWeek === endWeek ? `第 ${startWeek} 周内完成` : `第 ${startWeek}-${endWeek} 周内完成`
  }
  return item.scope_type === 'PLAN' ? '在整个计划中持续有效' : '在所属阶段内灵活完成'
}

function WorkItemRow({ item, allItems }: { item: PlanWorkItem; allItems: PlanWorkItem[] }) {
  const trigger = objectValue(item.constraints.trigger)
  const conditions = Array.isArray(trigger.conditions) ? trigger.conditions.filter((value): value is string => typeof value === 'string') : []
  const dependencies = item.dependency_ids
    .map((id) => allItems.find((candidate) => candidate.id === id)?.title)
    .filter((title): title is string => Boolean(title))
  return <details className="hybrid-work-item">
    <summary>
      <div>
        <span>{bindingLabels[item.binding_mode] ?? item.binding_mode}</span>
        <strong>{item.title}</strong>
      </div>
      <small>建议 {item.estimate_minutes} 分钟</small>
    </summary>
    <div className="hybrid-work-item-detail">
      <p>{item.description}</p>
      {conditions.length > 0 && <div className="hybrid-trigger-conditions"><strong>触发条件</strong><ul>{conditions.map((condition) => <li key={condition}>{condition}</li>)}</ul></div>}
      <div className="hybrid-work-item-meta">
        <span><CalendarRange size={15} />{timingSummary(item)}</span>
        <span><CheckCircle2 size={15} />{item.is_required ? '核心内容' : '可选内容'}</span>
        {dependencies.length > 0 && <span><GitBranch size={15} />完成“{dependencies.join('、')}”后进行</span>}
      </div>
      {item.evidence.length > 0 && <small>完成标志：{item.evidence.join('；')}</small>}
    </div>
  </details>
}

function WorkItemList({ items, plan }: { items: PlanWorkItem[]; plan: HybridPlan }) {
  if (items.length === 0) return null
  return <div className="hybrid-work-item-list">
    {items.map((item) => <WorkItemRow item={item} allItems={plan.work_items} key={item.id} />)}
  </div>
}

function TaskList({ tasks, plan, stageOrder }: { tasks: ReviewTask[]; plan: HybridPlan; stageOrder?: number }) {
  if (!tasks.length) return <p className="hybrid-empty-stage">当前已生成范围内没有单独安排执行任务；后续内容需满足条件并由你确认后再展开。</p>
  return <div className="plan-day-detail hybrid-task-list"><ol>
    {tasks.map((entry) => <li key={entry.task.id} data-plan-task-id={entry.task.id}>
      <PlanTaskDetail task={entry.task} allDays={plan.days} connection={entry} stageOrder={stageOrder} />
    </li>)}
  </ol></div>
}

function DailyPlan({ plan, tasks }: { plan: HybridPlan; tasks: ReviewTask[] }) {
  const weeks = [...new Set(plan.days.map((day) => day.week_index))].sort((a, b) => a - b)
  if (weeks.length === 0) {
    return <section className="hybrid-daily-error">
      <strong>这份旧草案还没有每日详情</strong>
      <p>它是在每日计划接入前生成的。请重新生成计划或在计划讨论中提出调整，系统不会再用重复工作项伪装成每日任务。</p>
    </section>
  }
  return <div className="hybrid-daily-preview">
    <section className="hybrid-daily-preview-note">
      <Repeat2 size={19} />
      <div><strong>相对执行日</strong><p>这里展示确认后会真正执行的任务内容。开始前只会结合真实开始日、休息日和已有日程映射日期，不会换成另一套任务。</p></div>
    </section>
    {weeks.map((week, weekIndex) => {
      const days = plan.days.filter((day) => day.week_index === week)
      const minutes = tasks.filter((entry) => entry.day.week_index === week)
        .reduce((total, entry) => total + entry.task.estimate_minutes, 0)
      return <details className="hybrid-stage-section hybrid-daily-week" open={weekIndex === 0} key={week}>
        <summary>
          <div><span>W{week}</span><div><h3>第 {week} 周具体安排</h3><p>{days.length} 个执行日 · 约 {minutes} 分钟</p></div></div>
          <span>{plan.schedule_type === 'OUTCOME' ? '成果检查点' : '按顺序推进'}</span>
        </summary>
        <div className="hybrid-daily-list">
          {days.map((day) => <PlanDayCard day={day} allDays={plan.days} scheduleType={plan.schedule_type} taskConnections={tasks} key={day.id} />)}
        </div>
      </details>
    })}
  </div>
}

export function HybridPlanReview({
  plan,
  generatedWeeks,
  viewMode,
}: {
  plan: HybridPlan
  generatedWeeks: number
  viewMode: 'STAGE' | 'WEEK' | 'DAY'
}) {
  const tasks = projectPlanTasks(plan)
  const scheduledItems = new Set(tasks.flatMap((entry) => entry.workItems.map((item) => item.id)))
  const pendingTriggers = plan.work_items.filter((item) => item.binding_mode === 'TRIGGERED' && !scheduledItems.has(item.id))
  const planItems = plan.work_items.filter((item) => item.stage_order == null && !item.milestone_id
    && !scheduledItems.has(item.id) && !pendingTriggers.includes(item))
  const minutes = tasks.reduce((total, entry) => total + entry.task.estimate_minutes, 0)
  const ungroupedTasks = tasks.filter((entry) => !entry.stages.length)

  return <div className="hybrid-plan-review">
    <section className="hybrid-review-notice">
      <Sparkles size={20} />
      <div><strong>同一份安排，按不同方式查看</strong><p>阶段、周与执行日展示同一组已保存任务；每项都标明周次和来源工作项。跨阶段任务只计一次工时，待触发内容不计入本轮安排。</p></div>
    </section>
    <div className="hybrid-review-stats" aria-label="弹性计划摘要">
      <div><span>已展开</span><strong>{generatedWeeks} / {plan.total_weeks} 周</strong></div>
      <div><span>已安排任务</span><strong>{tasks.length} 项</strong></div>
      <div><span>已安排工时</span><strong>{minutes} 分钟</strong></div>
      <div><span>待触发内容</span><strong>{pendingTriggers.length} 项</strong></div>
    </div>
    {viewMode === 'DAY' && <DailyPlan plan={plan} tasks={tasks} />}
    {viewMode === 'STAGE' && planItems.length > 0 && <details className="hybrid-stage-section" open>
      <summary><div><Repeat2 size={18} /><h3>尚未分解的工作项要求</h3></div><span>{planItems.length} 项</span></summary>
      <WorkItemList items={planItems} plan={plan} />
    </details>}
    {viewMode === 'STAGE' && ungroupedTasks.length > 0 && <details className="hybrid-stage-section" open>
      <summary><div><h3>未归入阶段的任务</h3></div><span>{ungroupedTasks.length} 项任务</span></summary>
      <TaskList tasks={ungroupedTasks} plan={plan} />
    </details>}
    {viewMode === 'STAGE' && plan.milestones.map((milestone, index) => {
      const stageTasks = tasks.filter((entry) => entry.stages.some((stage) => stage.order === milestone.stage_order))
      const relatedItems = new Set(stageTasks.flatMap((entry) => entry.workItems.map((item) => item.id)))
      const items = plan.work_items.filter((item) => !pendingTriggers.includes(item) && (
        item.stage_order === milestone.stage_order || item.milestone_id === milestone.id || relatedItems.has(item.id)
      ))
      return <details className="hybrid-stage-section" open={index === 0} key={milestone.id}>
        <summary>
          <div><span>M{milestone.stage_order}</span><div><h3>{milestone.title}</h3><p>{milestone.purpose}</p></div></div>
          <span>第 {milestone.start_week}{milestone.end_week === milestone.start_week ? '' : `-${milestone.end_week}`} 周 · {stageTasks.length} 项任务{stageTasks.some((entry) => entry.stages.length > 1) ? '（含共享任务）' : ''}</span>
        </summary>
        <TaskList tasks={stageTasks} plan={plan} stageOrder={milestone.stage_order} />
        {items.length > 0 && <details className="hybrid-source-requirements"><summary>对应工作项要求 · {items.length} 项</summary><WorkItemList items={items} plan={plan} /></details>}
      </details>
    })}
    {viewMode === 'WEEK' && Array.from({
      length: plan.planning_mode === 'EXPLORATORY'
        ? Math.min(plan.total_weeks, generatedWeeks)
        : plan.total_weeks,
    }, (_, index) => index + 1).map((week, index) => {
      const weekTasks = tasks.filter((entry) => entry.day.week_index === week)
      const stageNames = plan.milestones
        .filter((milestone) => weekTasks.some((entry) => entry.stages.some((stage) => stage.order === milestone.stage_order)))
        .map((milestone) => milestone.title)
      return <details className="hybrid-stage-section hybrid-week-section" open={index === 0} key={week}>
        <summary>
          <div><span>W{week}</span><div><h3>第 {week} 周</h3><p>{stageNames.join(' · ') || '贯穿计划的工作项'}</p></div></div>
          <span>{weekTasks.length} 项任务 · {weekTasks.reduce((total, entry) => total + entry.task.estimate_minutes, 0)} 分钟</span>
        </summary>
        {week > generatedWeeks ? <p className="hybrid-empty-stage">这一周尚未展开，确认后续窗口后再生成具体安排。</p> : <TaskList tasks={weekTasks} plan={plan} />}
      </details>
    })}
    {pendingTriggers.length > 0 && <section className="hybrid-pending-triggers" aria-label="待触发的后续内容">
      <header><Repeat2 size={18} /><div><h3>待触发的后续内容</h3><p>以下内容尚未排入执行任务，不占本轮工时。</p></div></header>
      <WorkItemList items={pendingTriggers} plan={plan} />
    </section>}
  </div>
}
