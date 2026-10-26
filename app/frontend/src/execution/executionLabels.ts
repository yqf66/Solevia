import type { TodayWorkspace } from '../api'
import { friendlyDate } from '../ui/plannerPresentation'

export function executionDayLabel(code: string) {
  const match = /^(?:M(\d+)-)?W(\d+)-D(\d+)$/i.exec(code)
  return match ? `${match[1] ? `第 ${match[1]} 阶段 · ` : ''}第 ${match[2]} 周 · 第 ${match[3]} 个执行日` : '执行安排'
}

export function executionHeading(workspace: Pick<TodayWorkspace, 'day' | 'milestone_title' | 'goal_title' | 'schedule_type' | 'today' | 'scheduled_day'>, viewingToday: boolean) {
  if (!workspace.day) return `${friendlyDate(workspace.today)} · 休息或缓冲日`
  if (workspace.schedule_type === 'OUTCOME') return `${friendlyDate(workspace.scheduled_day?.scheduled_date ?? workspace.today)} · 成果检查`
  const subject = workspace.milestone_title?.trim() || workspace.goal_title?.trim() || '任务安排'
  return `${viewingToday ? '今天' : friendlyDate(workspace.today)} · ${subject}`
}
