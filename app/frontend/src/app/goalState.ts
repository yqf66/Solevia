import type { GoalListItem } from '../api'
import { statusLabels } from '../ui/plannerPresentation'

export function pausedGoalRemainingDays(goal: GoalListItem) {
  if (!goal.pause_until) return null
  const [year, month, day] = goal.pause_until.split('-').map(Number)
  const now = new Date()
  return Math.round(
    (Date.UTC(year, month - 1, day) - Date.UTC(now.getFullYear(), now.getMonth(), now.getDate()))
      / 86_400_000,
  )
}

export function pausedGoalLabel(goal: GoalListItem) {
  const remainingDays = pausedGoalRemainingDays(goal)
  if (remainingDays === null) return '待你恢复'
  if (remainingDays <= 0) return statusLabels.ACTIVE
  return `距离恢复还剩 ${remainingDays} 天`
}

