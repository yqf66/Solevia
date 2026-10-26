import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, expect, it } from 'vitest'
import { RequirementSummary } from './RequirementSummary'

afterEach(cleanup)

it('shows readable facts while excluding internal and future metadata fields', () => {
  const { container } = render(<RequirementSummary snapshot={{
    objective: '准备 Agent 开发实习', deadline: '2026 年 12 月 20 日',
    desired_outcomes: ['可展示作品', '面试准备'], preferences: ['Python', { internal: true }],
    temporal_context: { deadline: { resolved_date: '2026-12-20' } }, capacity_source: { id: 'internal' },
    future_internal_field: 'must stay private', risks: { unexpected: true }, constraints: [],
  }} />)
  expect(screen.getByText('准备 Agent 开发实习')).toBeVisible()
  expect(screen.getByText('可展示作品、面试准备')).toBeVisible()
  expect(screen.getByText('Python')).toBeVisible()
  expect(container.textContent).not.toMatch(/object Object|temporal_context|capacity_source|internal|must stay private/)
})
