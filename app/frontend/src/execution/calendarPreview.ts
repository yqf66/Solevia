import type { ScheduledDay } from '../api'

export type CalendarPreviewWeek = {
  key: string
  startDate: string
  endDate: string
  days: ScheduledDay[]
}

function shiftUtcDate(date: string, days: number) {
  const value = new Date(`${date}T00:00:00Z`)
  value.setUTCDate(value.getUTCDate() + days)
  return value.toISOString().slice(0, 10)
}

function mondayOf(date: string) {
  const value = new Date(`${date}T00:00:00Z`)
  const daysSinceMonday = (value.getUTCDay() + 6) % 7
  return shiftUtcDate(date, -daysSinceMonday)
}

export function groupCalendarPreviewWeeks(days: ScheduledDay[]): CalendarPreviewWeek[] {
  const weeks = new Map<string, CalendarPreviewWeek>()

  for (const day of days) {
    const startDate = mondayOf(day.scheduled_date)
    const current = weeks.get(startDate)
    if (current) {
      current.days.push(day)
      continue
    }
    weeks.set(startDate, {
      key: startDate,
      startDate,
      endDate: shiftUtcDate(startDate, 6),
      days: [day],
    })
  }

  return [...weeks.values()].sort((left, right) => left.startDate.localeCompare(right.startDate))
}
