import { afterEach, expect, it, vi } from 'vitest'
import { previewTaskRecommendations } from './api'

afterEach(() => vi.restoreAllMocks())

it('coalesces identical in-flight model requests and releases them after completion', async () => {
  let release!: (response: Response) => void
  const fetchMock = vi.spyOn(globalThis, 'fetch').mockImplementationOnce(() => new Promise((resolve) => { release = resolve }))
  const options = { use_ai: true, instruction: '优先阅读' }
  const first = previewTaskRecommendations('dedupe-goal', options)
  const second = previewTaskRecommendations('dedupe-goal', options)
  expect(first).toBe(second)
  await Promise.resolve()
  expect(fetchMock).toHaveBeenCalledTimes(1)
  const result = { items: [], blocked_summary: [], warnings: [], capacity: {} }
  release(new Response(JSON.stringify(result), { status: 200 }))
  await first
  fetchMock.mockResolvedValueOnce(new Response(JSON.stringify(result), { status: 200 }))
  await previewTaskRecommendations('dedupe-goal', options)
  expect(fetchMock).toHaveBeenCalledTimes(2)
})
