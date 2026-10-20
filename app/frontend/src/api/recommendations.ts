// recommendations: extracted without changing request or response semantics.
import type { TaskRecommendationPreview } from './contracts'
import { request } from './client'

const pendingRecommendationRequests = new Map<string, Promise<TaskRecommendationPreview>>()

export function previewTaskRecommendations(goalId: string, options: { use_ai?: boolean; instruction?: string } = {}): Promise<TaskRecommendationPreview> {
  const body = JSON.stringify({ max_items: 3, use_ai: options.use_ai ?? false, instruction: options.instruction ?? '' })
  const key = `${goalId}:${body}`
  const existing = pendingRecommendationRequests.get(key)
  if (existing) return existing
  const pending = request<TaskRecommendationPreview>(`/api/goals/${goalId}/execution/recommendations/preview`, {
    method: 'POST', body,
  }).then((result) => {
    if (!result || !Array.isArray(result.items) || !Array.isArray(result.blocked_summary) || !Array.isArray(result.warnings) || !result.capacity) {
      throw new Error('建议数据不完整，请刷新重试')
    }
    return result
  }).finally(() => { pendingRecommendationRequests.delete(key) })
  pendingRecommendationRequests.set(key, pending)
  return pending
}
