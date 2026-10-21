import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'
import type { PlanWorkspace } from '../../api'
import { HybridPlanReview } from './HybridPlanReview'

afterEach(cleanup)

const plan = {
  id: 'plan-1',
  route_version_id: 'route-1',
  version: 1,
  revision: 1,
  status: 'DRAFT',
  summary: '弹性测试计划',
  total_weeks: 2,
  weekly_execution_days: 3,
  weekly_minutes: 180,
  total_estimated_minutes: 360,
  buffer_strategy: [],
  assumptions: [],
  open_questions: [],
  change_summary: [],
  planning_mode: 'COMMITTED' as const,
  schedule_type: 'RHYTHM' as const,
  execution_model_version: 'HYBRID_WORK_ITEM' as const,
  exploration_window_weeks: 2,
  next_unplanned_week: 3,
  work_item_groups: [],
  work_items: [{
    id: 'item-1',
    item_key: 'ITEM.ONE',
    milestone_id: 'milestone-1',
    group_id: null,
    source_work_item_id: null,
    lineage_work_item_id: null,
    scope_type: 'STAGE' as const,
    stage_order: 1,
    title: '完成基础练习',
    description: '先完成基础练习，再进入后续内容。',
    task_kind: 'PRACTICE',
    estimate_minutes: 45,
    priority: 1,
    is_required: true,
    is_splittable: true,
    binding_mode: 'WINDOW',
    fulfillment_mode: 'ONCE',
    target_value: 1,
    target_unit: 'COUNT',
    constraints: { temporal: { start_week: 1, end_week: 2 } },
    completion_rule: {},
    evidence: ['练习记录'],
    source_refs: ['requirement.objective'],
    commitment_refs: ['基础练习'],
    dependency_ids: [],
    status: 'DRAFT',
    windows: [],
  }],
  milestones: [{
    id: 'milestone-1',
    route_stage_id: 'stage-1',
    stage_order: 1,
    title: '基础阶段',
    purpose: '建立基础',
    start_week: 1,
    end_week: 2,
    outputs: ['练习记录'],
  }],
  days: [{
    id: 'day-1',
    stage_order: 1,
    week_index: 1,
    day_index: 1,
    primary_goal: '明确基础并完成第一轮练习',
    suggested_minutes: 45,
    evidence: ['练习记录'],
    do_not: [],
    tasks: [{
      id: 'task-1',
      order: 1,
      title: '完成第一轮基础练习',
      description: '先记录基线，再完成一组基础练习。',
      estimate_minutes: 45,
      priority: 1,
      task_kind: 'PRACTICE',
      is_required: true,
      evidence: ['练习记录'],
      depends_on: [],
      status: 'PENDING',
    }],
  }],
} satisfies NonNullable<PlanWorkspace['plan']>

describe('HybridPlanReview', () => {
  it('renders real week groupings in week view', () => {
    render(<HybridPlanReview plan={plan} generatedWeeks={2} viewMode="WEEK" />)

    expect(screen.getByText('第 1 周')).toBeInTheDocument()
    expect(screen.getByText('第 2 周')).toBeInTheDocument()
    expect(screen.queryByText('M1')).not.toBeInTheDocument()
  })

  it('expands a work item to reveal its details', () => {
    render(<HybridPlanReview plan={plan} generatedWeeks={2} viewMode="STAGE" />)

    const summary = screen.getByText('完成基础练习').closest('summary')
    const details = summary?.closest('details')
    expect(details).not.toHaveAttribute('open')
    fireEvent.click(summary!)
    expect(details).toHaveAttribute('open')
    expect(screen.getByText('先完成基础练习，再进入后续内容。')).toBeVisible()
  })

  it('renders concrete task details in daily view', () => {
    render(<HybridPlanReview plan={plan} generatedWeeks={2} viewMode="DAY" />)

    expect(screen.getByText('相对执行日')).toBeInTheDocument()
    expect(screen.getByText('第 1 周具体安排')).toBeInTheDocument()
    const summary = screen.getByText('M1-W1-D1').closest('summary')
    fireEvent.click(summary!)
    expect(screen.getByText('你需要完成')).toBeVisible()
    expect(screen.getByText('建议产出或收获：练习记录')).toBeVisible()
  })
})
