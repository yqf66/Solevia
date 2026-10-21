import { expect, it } from 'vitest'
import type { ScheduledDay } from '../api'
import { groupCalendarPreviewWeeks } from './calendarPreview'

function day(code: string, scheduledDate: string, stageOrder = 1): ScheduledDay {
  return {
    plan_day_id: `${code}-${scheduledDate}`,
    stage_order: stageOrder,
    week_index: 1,
    day_index: 1,
    code,
    scheduled_date: scheduledDate,
    primary_goal: code,
  }
}

it('groups the complete preview by calendar week across stage boundaries', () => {
  const weeks = groupCalendarPreviewWeeks([
    day('M1-W1-D1', '2026-09-21'),
    day('M1-W1-D2', '2026-09-23'),
    day('M2-W1-D1', '2026-09-27', 2),
    day('M2-W1-D2', '2026-09-28', 2),
    day('M2-W2-D1', '2026-10-05', 2),
  ])

  expect(weeks.map((week) => [week.startDate, week.endDate, week.days.length])).toEqual([
    ['2026-09-21', '2026-09-27', 3],
    ['2026-09-28', '2026-10-04', 1],
    ['2026-10-05', '2026-10-11', 1],
  ])
})
