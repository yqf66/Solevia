// manualPlans: extracted without changing request or response semantics.
import type { ManualPlan } from './contracts'
import { request } from './client'

export function createManualPlan(input: {
  title: string
  description?: string
  timezone: string
  tasks: Array<{
    title: string
    description?: string
    estimate_minutes: number
    priority?: number
    scheduled_date?: string | null
    start_local?: string | null
    end_local?: string | null
    recurrence?: 'ONCE' | 'DAILY' | 'WEEKLY'
    recurrence_weekdays?: number[]
    recurrence_end_date?: string | null
  }>
  conflict_mode?: 'NONE' | 'PARALLEL' | 'PUSH'
}): Promise<ManualPlan> {
  return request('/api/quick-plans', { method: 'POST', body: JSON.stringify(input) })
}

export function updateManualTask(planId: string, taskId: string, input: { title?: string; description?: string; estimate_minutes?: number; priority?: number; scheduled_date?: string | null; start_local?: string | null; end_local?: string | null; conflict_mode?: 'NONE' | 'PARALLEL' | 'PUSH'; scope?: 'TODAY' | 'FUTURE'; status?: 'NOT_STARTED' | 'COMPLETED' | 'SKIPPED' }): Promise<ManualPlan> {
  return request(`/api/quick-plans/${planId}/tasks/${taskId}`, { method: 'PATCH', body: JSON.stringify(input) })
}

export function deleteManualTask(planId: string, taskId: string, input?: { scope?: 'TODAY' | 'FUTURE'; override_date?: string }): Promise<ManualPlan> {
  return request(`/api/quick-plans/${planId}/tasks/${taskId}`, { method: 'DELETE', body: JSON.stringify(input || {}) })
}

export function getManualPlan(planId: string): Promise<ManualPlan> {
  return request(`/api/quick-plans/${planId}`)
}

export function listManualPlans(): Promise<ManualPlan[]> { return request('/api/quick-plans') }
