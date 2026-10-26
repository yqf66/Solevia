import { cleanup, fireEvent, render, screen, within } from '@testing-library/react'
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
    expect(screen.getByText('M1')).toBeInTheDocument() // Task context remains visible in a week grouping.
  })

  it('expands a work item to reveal its details', () => {
    render(<HybridPlanReview plan={plan} generatedWeeks={2} viewMode="STAGE" />)

    fireEvent.click(screen.getByText('对应工作项要求 · 1 项'))
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

  it('keeps parallel stages, weeks and days consistent without counting an untriggered branch', () => {
    // Like an outcome plan: global obligations, parallel stages and one review
    // action that also records the boundary of a future (untriggered) stage.
    const milestones = [1, 2, 3, 4].map((order) => ({
      ...plan.milestones[0], id: `milestone-${order}`, stage_order: order,
      title: `阶段 ${order}`, start_week: order < 3 ? 1 : 2,
    }))
    const workItems = ['观察记录', '候选证据', '复盘决定', '条件满足后试养'].map((title, index) => ({
      ...plan.work_items[0], id: `item-${index + 1}`, item_key: `ITEM.${index + 1}`,
      stage_order: null, milestone_id: null, scope_type: 'PLAN' as const, title,
      binding_mode: index === 3 ? 'TRIGGERED' : 'WINDOW',
    }))
    const task = (id: string, title: string, minutes: number, item: number, stages: number[]) => ({
      ...plan.days[0].tasks[0], id, title, estimate_minutes: minutes,
      source_work_item_id: `item-${item}`,
      scheduling_policy_data: {
        work_item_keys: [`ITEM.${item}`],
        stage_contributions: stages.map((order) => ({ stage_order: order, action: `${title}的阶段动作` })),
      },
    })
    const actualPlan = {
      ...plan, schedule_type: 'OUTCOME' as const, milestones, work_items: workItems,
      days: [
        { ...plan.days[0], id: 'day-1', suggested_minutes: 25, tasks: [task('a', '记录初稿', 10, 1, [1]), task('b', '比较表初稿', 15, 2, [2])] },
        { ...plan.days[0], id: 'day-2', stage_order: 3, week_index: 2, suggested_minutes: 25, tasks: [task('c', '补全观察', 10, 1, [1]), task('d', '补全比较表', 5, 2, [2]), task('e', '复盘并保留后续边界', 10, 3, [3, 4])] },
      ],
    }
    const { container, rerender } = render(<HybridPlanReview plan={actualPlan} generatedWeeks={2} viewMode="STAGE" />)
    const ids = () => [...container.querySelectorAll('[data-plan-task-id]')].map((element) => element.getAttribute('data-plan-task-id'))
    expect(ids()).toEqual(['a', 'c', 'b', 'd', 'e', 'e'])
    expect(screen.queryByText(/还在生成/)).not.toBeInTheDocument()
    expect(screen.getByText('50 分钟')).toBeInTheDocument()
    expect(screen.getAllByText('同一任务关联多个阶段，工时只计一次。')).toHaveLength(2)
    const stages = [...container.querySelectorAll('.hybrid-stage-section')]
    expect(stages[1]).toHaveTextContent('比较表初稿')
    expect(stages[1]).toHaveTextContent('补全比较表')
    expect(stages[1]).not.toHaveTextContent('记录初稿')

    for (const viewMode of ['WEEK', 'DAY'] as const) {
      rerender(<HybridPlanReview plan={actualPlan} generatedWeeks={2} viewMode={viewMode} />)
      expect(ids()).toEqual(['a', 'b', 'c', 'd', 'e'])
      expect(screen.getByText('50 分钟')).toBeInTheDocument()
      expect(screen.getByLabelText('弹性计划摘要')).toHaveTextContent('已安排任务5 项')
      const pending = screen.getByLabelText('待触发的后续内容')
      expect(within(pending).getByText('条件满足后试养')).toBeInTheDocument()
      expect(pending.querySelector('[data-plan-task-id]')).toBeNull()
      for (const section of container.querySelectorAll('.hybrid-stage-section')) {
        expect(section).not.toHaveTextContent('条件满足后试养')
      }
    }
  })

  it('shows a saved empty stage honestly and keeps tasks with old stage metadata', () => {
    render(<HybridPlanReview plan={{ ...plan, work_items: [], milestones: [...plan.milestones, { ...plan.milestones[0], id: 'empty', stage_order: 2, title: '尚未展开的阶段' }] }} generatedWeeks={1} viewMode="STAGE" />)
    expect(screen.getByText('完成第一轮基础练习')).toBeInTheDocument()
    expect(screen.getByText(/当前已生成范围内没有单独安排执行任务/)).toBeInTheDocument()
    expect(screen.queryByText(/还在生成/)).not.toBeInTheDocument()
  })

  it('preserves an ungrouped saved task and derives displayed daily minutes from its tasks', () => {
    const savedPlan = { ...plan, milestones: [], work_items: [], days: [{ ...plan.days[0], suggested_minutes: 999 }] }
    const { container, rerender } = render(<HybridPlanReview plan={savedPlan} generatedWeeks={1} viewMode="STAGE" />)
    expect(screen.getByText('未归入阶段的任务')).toBeInTheDocument()
    expect(container.querySelector('[data-plan-task-id="task-1"]')).toBeInTheDocument()
    rerender(<HybridPlanReview plan={savedPlan} generatedWeeks={1} viewMode="DAY" />)
    expect(screen.getByText('建议约 45 分钟')).toBeInTheDocument()
    expect(screen.getByText('1 个执行日 · 约 45 分钟')).toBeInTheDocument()
    expect(screen.queryByText(/999/)).not.toBeInTheDocument()
  })
})
