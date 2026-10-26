import type { PlanDay, PlanTask, PlanWorkspace, PlanWorkItem } from '../../api'

type Plan = NonNullable<PlanWorkspace['plan']>

export function objectValue(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown>
    : {}
}

export type ReviewTask = {
  day: PlanDay
  task: PlanTask
  workItems: PlanWorkItem[]
  stages: Array<{ order: number; action: string }>
}

// All views group these saved tasks. A work item's eligible window is not an
// additional weekly task, and a contribution to two stages is still one task.
export function projectPlanTasks(plan: Pick<Plan, 'days' | 'work_items' | 'milestones'>): ReviewTask[] {
  const stageOrders = new Set(plan.milestones.map((milestone) => milestone.stage_order))
  return [...plan.days]
    .sort((a, b) => a.week_index - b.week_index || a.day_index - b.day_index)
    .flatMap((day) => [...day.tasks].sort((a, b) => a.order - b.order).map((task) => {
      const metadata = objectValue(task.scheduling_policy_data)
      const keys = Array.isArray(metadata.work_item_keys) ? metadata.work_item_keys : []
      const workItems = plan.work_items.filter((item) => (
        item.id === task.source_work_item_id || keys.includes(item.item_key)
      ))
      const stages: ReviewTask['stages'] = []
      const contributions = Array.isArray(metadata.stage_contributions) ? metadata.stage_contributions : []
      for (const entry of contributions) {
        const contribution = objectValue(entry)
        const order = contribution.stage_order
        if (typeof order === 'number' && stageOrders.has(order) && !stages.some((stage) => stage.order === order)) {
          stages.push({ order, action: typeof contribution.action === 'string' ? contribution.action : '' })
        }
      }
      if (!stages.length) {
        for (const item of workItems) {
          const order = item.stage_order ?? plan.milestones.find((milestone) => milestone.id === item.milestone_id)?.stage_order
          if (order != null && stageOrders.has(order) && !stages.some((stage) => stage.order === order)) {
            stages.push({ order, action: '' })
          }
        }
      }
      if (!stages.length && stageOrders.has(day.stage_order)) stages.push({ order: day.stage_order, action: '' })
      return { day, task, workItems, stages }
    }))
}
