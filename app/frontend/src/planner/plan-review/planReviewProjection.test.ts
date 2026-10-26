import { describe, expect, it } from 'vitest'
import type { PlanDay, PlanMilestone, PlanWorkItem } from '../../api'
import { projectPlanTasks } from './planReviewProjection'

describe('projectPlanTasks', () => {
  it('resolves milestone ids and all work item keys without copying a day into every stage', () => {
    const milestones = [{ id: 'm1', stage_order: 1 }, { id: 'm2', stage_order: 2 }] as PlanMilestone[]
    const work_items = [
      { id: 'i1', item_key: 'ONE', milestone_id: 'm1', stage_order: null },
      { id: 'i2', item_key: 'TWO', milestone_id: null, stage_order: 2 },
    ] as PlanWorkItem[]
    const days = [{ id: 'd1', stage_order: 1, week_index: 1, day_index: 1, tasks: [
      { id: 't1', order: 1, scheduling_policy_data: { work_item_keys: ['TWO'] } },
      { id: 't2', order: 2, source_work_item_id: 'i1' },
      { id: 't3', order: 3, scheduling_policy_data: { work_item_keys: ['ONE', 'TWO'] } },
      { id: 't4', order: 4, scheduling_policy_data: { stage_contributions: [null, { stage_order: 99 }, { stage_order: 2, action: '实际动作' }, { stage_order: 2 }] } },
    ] }] as PlanDay[]
    const tasks = projectPlanTasks({ milestones, work_items, days })
    expect(tasks.map((entry) => entry.stages.map((stage) => stage.order))).toEqual([[2], [1], [1, 2], [2]])
    expect(tasks[2].workItems.map((item) => item.id)).toEqual(['i1', 'i2'])
    expect(tasks[3].stages[0].action).toBe('实际动作')
  })
})
