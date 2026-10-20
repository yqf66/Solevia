import type { UnifiedToday } from '../api'

export type DashboardTask = UnifiedToday['fixed_tasks'][number]

export const schedulingPolicyLabels: Record<string, string> = {
  EXACT_DAY: '按计划日',
  DATE_WINDOW: '期限窗口',
  CADENCE: '节奏建议',
  QUOTA: '总量要求',
  TASK_POOL: '候选任务',
  CHECKPOINT: '阶段验收',
  TRIGGERED: '条件触发',
  FLOATING: '弹性待安排',
}

