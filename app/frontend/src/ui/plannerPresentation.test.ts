import { afterEach, describe, expect, it, vi } from 'vitest'
import { dateInputInTimezone, displayMessageContent } from './plannerPresentation'

it('keeps technical explanations from successful Agent conversations readable', () => {
  const answer = '把 provider 与应用程序分开，检查 API 配置，用 gateway 测试模拟 HTTP 503 和 Schema 校验失败。'
  expect(displayMessageContent(answer)).toBe(answer)
  expect(displayMessageContent('模型服务响应超时。', true)).toBe('Solevia 暂时没有完成这次回复，你刚才的内容已经保存，可以直接重试。')
})

afterEach(() => vi.useRealTimers())

it('presents saved assistant replies with the current name while retaining their content', () => {
  expect(displayMessageContent('我是 Plan-A。**先做一小步**：[资料](https://example.com)'))
    .toBe('我是 Solevia。**先做一小步**：[资料](https://example.com)')
})

describe('planning start dates in the goal timezone', () => {
  it('keeps the correct day across midnight and date shortcuts', () => {
    vi.useFakeTimers()
    vi.setSystemTime(new Date('2026-10-10T17:00:00Z'))
    expect(dateInputInTimezone('Asia/Shanghai')).toBe('2026-10-11')
    expect(dateInputInTimezone('America/Los_Angeles')).toBe('2026-10-10')
    expect(dateInputInTimezone('Asia/Shanghai', 1)).toBe('2026-10-12')
  })

  it('handles month and year rollover without shifting the timezone date', () => {
    vi.useFakeTimers()
    vi.setSystemTime(new Date('2026-12-31T17:00:00Z'))
    expect(dateInputInTimezone('Asia/Shanghai')).toBe('2027-01-01')
    expect(dateInputInTimezone('Asia/Shanghai', -1)).toBe('2026-12-31')
  })
})
