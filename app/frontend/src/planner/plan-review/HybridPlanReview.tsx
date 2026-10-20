import { CalendarRange, CheckCircle2, GitBranch, Repeat2, Sparkles } from 'lucide-react'
import type { PlanWorkItem, PlanWorkspace } from '../../api'
import { PlanDayCard } from './PlanDayCard'

type HybridPlan = NonNullable<PlanWorkspace['plan']>

const bindingLabels: Record<string, string> = {
  FIXED: '固定位置',
  WINDOW: '时间窗口',
  CADENCE: '重复节奏',
  ON_DEMAND: '按需选择',
  TRIGGERED: '条件触发',
}

function objectValue(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown>
    : {}
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
      <div className="hybrid-work-item-meta">
        <span><CalendarRange size={15} />{timingSummary(item)}</span>
        <span><CheckCircle2 size={15} />{item.is_required ? '核心内容' : '可选内容'}</span>
        {dependencies.length > 0 && <span><GitBranch size={15} />完成“{dependencies.join('、')}”后进行</span>}
      </div>
      {item.evidence.length > 0 && <small>完成标志：{item.evidence.join('；')}</small>}
    </div>
  </details>
}

function itemWeekRange(plan: HybridPlan, item: PlanWorkItem): [number, number] {
  const temporal = objectValue(item.constraints.temporal)
  const milestone = plan.milestones.find((candidate) => candidate.stage_order === item.stage_order)
  const declaredStart = typeof temporal.start_week === 'number' ? temporal.start_week : 1
  const declaredEnd = typeof temporal.end_week === 'number' ? temporal.end_week : plan.total_weeks
  if (!milestone) return [declaredStart, declaredEnd]
  return [
    Math.max(declaredStart, milestone.start_week),
    Math.min(declaredEnd, milestone.end_week),
  ]
}

function WorkItemList({ items, plan }: { items: PlanWorkItem[]; plan: HybridPlan }) {
  if (items.length === 0) return <p className="hybrid-empty-stage">这一范围的工作项还在生成中。</p>
  return <div className="hybrid-work-item-list">
    {items.map((item) => <WorkItemRow item={item} allItems={plan.work_items} key={item.id} />)}
  </div>
}

function DailyPlan({ plan }: { plan: HybridPlan }) {
  const weeks = [...new Set(plan.days.map((day) => day.week_index))]
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
      const minutes = days.reduce((total, day) => total + day.suggested_minutes, 0)
      return <details className="hybrid-stage-section hybrid-daily-week" open={weekIndex === 0} key={week}>
        <summary>
          <div><span>W{week}</span><div><h3>第 {week} 周具体安排</h3><p>{days.length} 个执行日 · 约 {minutes} 分钟</p></div></div>
          <span>{plan.schedule_type === 'OUTCOME' ? '成果检查点' : '按顺序推进'}</span>
        </summary>
        <div className="hybrid-daily-list">
          {days.map((day) => <PlanDayCard day={day} allDays={plan.days} scheduleType={plan.schedule_type} key={day.id} />)}
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
  const planItems = plan.work_items.filter((item) => item.stage_order == null)
  const cadenceCount = plan.work_items.filter((item) => item.binding_mode === 'CADENCE').length
  const flexibleCount = plan.work_items.filter((item) => ['WINDOW', 'ON_DEMAND', 'TRIGGERED'].includes(item.binding_mode)).length
  const coveredCommitments = new Set(plan.work_items.flatMap((item) => item.commitment_refs)).size

  return <div className="hybrid-plan-review">
    <section className="hybrid-review-notice">
      <Sparkles size={20} />
      <div><strong>规划约束与执行草案</strong><p>工作项说明必须完成什么，执行日说明具体怎么推进；计划保留必要弹性，但不会每天临时重做决定。</p></div>
    </section>
    <div className="hybrid-review-stats" aria-label="弹性计划摘要">
      <div><span>已展开</span><strong>{generatedWeeks} / {plan.total_weeks} 周</strong></div>
      <div><span>弹性义务</span><strong>{flexibleCount} 项</strong></div>
      <div><span>重复节奏</span><strong>{cadenceCount} 项</strong></div>
      <div><span>覆盖承诺</span><strong>{coveredCommitments} 条</strong></div>
    </div>
    {viewMode === 'DAY' && <DailyPlan plan={plan} />}
    {viewMode === 'STAGE' && planItems.length > 0 && <details className="hybrid-stage-section" open>
      <summary><div><Repeat2 size={18} /><h3>贯穿整个计划</h3></div><span>{planItems.length} 项</span></summary>
      <WorkItemList items={planItems} plan={plan} />
    </details>}
    {viewMode === 'STAGE' && plan.milestones.map((milestone, index) => {
      const items = plan.work_items.filter((item) => item.stage_order === milestone.stage_order)
      return <details className="hybrid-stage-section" open={index === 0} key={milestone.id}>
        <summary>
          <div><span>M{milestone.stage_order}</span><div><h3>{milestone.title}</h3><p>{milestone.purpose}</p></div></div>
          <span>第 {milestone.start_week}{milestone.end_week === milestone.start_week ? '' : `-${milestone.end_week}`} 周 · {items.length} 项</span>
        </summary>
        <WorkItemList items={items} plan={plan} />
      </details>
    })}
    {viewMode === 'WEEK' && Array.from({
      length: plan.planning_mode === 'EXPLORATORY'
        ? Math.min(plan.total_weeks, generatedWeeks)
        : plan.total_weeks,
    }, (_, index) => index + 1).map((week, index) => {
      const items = plan.work_items.filter((item) => {
        const [startWeek, endWeek] = itemWeekRange(plan, item)
        return week >= startWeek && week <= endWeek
      })
      const stageNames = plan.milestones
        .filter((milestone) => week >= milestone.start_week && week <= milestone.end_week)
        .map((milestone) => milestone.title)
      return <details className="hybrid-stage-section hybrid-week-section" open={index === 0} key={week}>
        <summary>
          <div><span>W{week}</span><div><h3>第 {week} 周</h3><p>{stageNames.join(' · ') || '贯穿计划的工作项'}</p></div></div>
          <span>{items.length} 项可推进内容</span>
        </summary>
        <WorkItemList items={items} plan={plan} />
      </details>
    })}
  </div>
}
