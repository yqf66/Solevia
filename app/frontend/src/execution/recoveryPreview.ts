import type { ExecutionInactivity } from '../api'
import { dateInputValue } from '../ui/plannerPresentation'

export function previewInactivity(
  current: ExecutionInactivity | undefined,
  search: string,
  latestMissedDate?: string | null,
): ExecutionInactivity | undefined {
  if (!import.meta.env.DEV) return current
  const preview = new URLSearchParams(search).get('preview_inactivity')
  if (!['1', '3', '7'].includes(preview ?? '')) return current
  const inactiveDays = Number(preview)
  const params = new URLSearchParams(search)
  const fallbackDate = latestMissedDate ?? current?.latest_missed_date ?? null
  const missedCount = inactiveDays === 1 ? 1 : inactiveDays === 3 ? 2 : 5
  const existingDates = current?.missed_dates ?? []
  const simulatedDates = fallbackDate
    ? Array.from({ length: missedCount }, (_, index) => {
      const value = new Date(`${fallbackDate}T12:00:00`)
      value.setDate(value.getDate() - (missedCount - index - 1))
      return dateInputValue(value)
    })
    : []
  return {
    level: inactiveDays === 1 ? 'MISSED_ONCE' : inactiveDays === 3 ? 'PAUSED_3_DAYS' : 'PAUSED_7_DAYS',
    inactive_days: inactiveDays,
    missed_execution_days: missedCount,
    missed_dates: existingDates.length >= missedCount ? existingDates : simulatedDates,
    last_activity_date: null,
    latest_missed_date: fallbackDate,
    recovery_deferred: params.get('preview_deferred') === '1',
    has_new_missed_work: false,
  }
}
