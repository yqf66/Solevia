// research: extracted without changing request or response semantics.
import type { ResearchReadiness, ResearchResponsePhase, ResearchResponseRequest, ResearchRun } from './contracts'
import { request } from './client'

export function enqueueResearchRun(
  goalId: string,
  question: string,
  options: { conversationId?: string; idempotencyKey?: string; enabled?: boolean } = {},
): Promise<ResearchRun> {
  return request(`/api/goals/${goalId}/research/runs`, {
    method: 'POST',
    body: JSON.stringify({
      question,
      enabled: options.enabled ?? true,
      ...(options.conversationId ? { conversation_id: options.conversationId } : {}),
      ...(options.idempotencyKey ? { idempotency_key: options.idempotencyKey } : {}),
    }),
  })
}

export function listResearchRuns(goalId: string): Promise<ResearchRun[]> {
  return request(`/api/goals/${goalId}/research/runs`)
}

export function getResearchRun(goalId: string, runId: string): Promise<ResearchRun> {
  return request(`/api/goals/${goalId}/research/runs/${runId}`)
}

export function cancelResearchRun(goalId: string, runId: string): Promise<ResearchRun> {
  return request(`/api/goals/${goalId}/research/runs/${runId}/cancel`, { method: 'POST' })
}

export function resumeResearchRun(goalId: string, runId: string): Promise<ResearchRun> {
  return request(`/api/goals/${goalId}/research/runs/${runId}/resume`, { method: 'POST' })
}

export function retryResearchRun(goalId: string, runId: string): Promise<ResearchRun> {
  return request(`/api/goals/${goalId}/research/runs/${runId}/retry`, { method: 'POST' })
}

export function getResearchReadiness(): Promise<ResearchReadiness> {
  return request('/api/research/readiness', undefined, 5000)
}

export function setResearchConnection(clientId: string, enabled: boolean, sequence: number): Promise<ResearchReadiness> {
  return request('/api/research/connection', {
    method: 'POST', keepalive: true, body: JSON.stringify({ client_id: clientId, enabled, sequence }),
  }, 5000)
}

export function createResearchResponse(goalId: string, payload: {
  question: string; phase: ResearchResponsePhase; intent: 'MESSAGE' | 'EXPLAIN' | 'REVISE'
  idempotency_key: string; material_source_ids?: string[]
}): Promise<ResearchResponseRequest> {
  return request(`/api/goals/${goalId}/research/requests`, { method: 'POST', body: JSON.stringify(payload) })
}

export function listResearchResponses(goalId: string): Promise<ResearchResponseRequest[]> {
  return request(`/api/goals/${goalId}/research/requests`)
}

export function getResearchResponse(goalId: string, id: string): Promise<ResearchResponseRequest> {
  return request(`/api/goals/${goalId}/research/requests/${id}`)
}

export function actResearchResponse(goalId: string, id: string, action: 'cancel' | 'ordinary' | 'retry-answer' | 'retry-research'): Promise<ResearchResponseRequest> {
  return request(`/api/goals/${goalId}/research/requests/${id}/${action}`, { method: 'POST' })
}

export async function runResearchToCompletion(
  goalId: string,
  question: string,
  options: { conversationId?: string; idempotencyKey?: string; timeoutMs?: number } = {},
): Promise<ResearchRun> {
  let current = await enqueueResearchRun(goalId, question, options)
  const deadline = Date.now() + (options.timeoutMs ?? 5 * 60 * 1000)
  while (current.status === 'QUEUED' || current.status === 'RUNNING' || current.status === 'CANCEL_REQUESTED') {
    if (Date.now() >= deadline) throw new Error('联网研究仍在进行，请稍后查看研究任务后再继续。')
    await new Promise((resolve) => window.setTimeout(resolve, 800))
    current = await getResearchRun(goalId, current.id)
  }
  return current
}
