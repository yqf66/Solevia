import { expect, it } from 'vitest'
import type { TodayWorkspace } from '../api'
import { executionHeading, executionDayLabel } from './executionLabels'

it('keeps internal execution coordinates out of headings and handles older missing names', () => {
  const workspace = { day: {}, milestone_title: '建立 Agent 基础', goal_title: '实习准备',
    today: '2026-10-21', scheduled_day: { code: 'M1-W1-D1' }, schedule_type: 'RHYTHM' } as TodayWorkspace
  expect(executionHeading(workspace, true)).toBe('今天 · 建立 Agent 基础')
  expect(executionHeading({ ...workspace, milestone_title: null }, true)).toBe('今天 · 实习准备')
  expect(executionHeading({ ...workspace, schedule_type: 'OUTCOME' }, true)).toContain('成果检查')
  expect(executionHeading({ ...workspace, day: null }, true)).toContain('休息或缓冲日')
  expect(executionHeading(workspace, false)).not.toContain('M1-W1-D1')
  expect(executionDayLabel('M1-W1-D1')).toBe('第 1 阶段 · 第 1 周 · 第 1 个执行日')
  expect(executionDayLabel('unknown')).toBe('执行安排')
})
