// recovery: extracted without changing request or response semantics.
import type { ExecutionInactivity, PlanWorkspace, RecoveryCatchupPreview, RecoveryShiftPreview, RecoverySplitPreview } from './contracts'
import { request } from './client'

export function decideRecovery(goalId: string, decision: 'ALLOW_EXTENSION' | 'KEEP_DEADLINE' | 'ACCEPT_LOAD', token: string): Promise<PlanWorkspace> {
  return request(`/api/goals/${goalId}/plan/recovery-decision`, {
    method: 'POST', body: JSON.stringify({ decision, token }),
  })
}

export function reviseRecoveryPlan(
  goalId: string,
  input: { missed_dates: string[]; backlog_minutes: number; original_end_date: string; include_non_execution_days: boolean; preview_id?: string; instruction?: string; research_run_id?: string },
): Promise<PlanWorkspace> {
  return request(`/api/goals/${goalId}/execution/recovery/ai`, {
    method: 'POST',
    body: JSON.stringify(input),
  })
}

export function previewRecoverySplit(goalId: string, input: { preview_id: string; source_task_id: string; part_minutes: number[] }): Promise<RecoverySplitPreview> {
  return request(`/api/goals/${goalId}/execution/recovery/split-preview`, { method: 'POST', body: JSON.stringify(input) })
}

export function applyRecoverySplit(goalId: string, previewId: string): Promise<RecoverySplitPreview> {
  return request(`/api/goals/${goalId}/execution/recovery/split`, { method: 'POST', body: JSON.stringify({ preview_id: previewId }) })
}

export function handoffRecoveryDiscussion(goalId: string, previewId: string): Promise<PlanWorkspace> {
  return request(`/api/goals/${goalId}/execution/recovery/discussion`, { method: 'POST', body: JSON.stringify({ preview_id: previewId }) })
}

export function previewRecoveryShift(goalId: string, anchorDate?: string): Promise<RecoveryShiftPreview> {
  const query = anchorDate ? `?anchor_date=${encodeURIComponent(anchorDate)}` : ''
  return request(`/api/goals/${goalId}/execution/recovery/shift-preview${query}`)
}

export function applyRecoveryShift(goalId: string, previewId: string, acceptUnallocated = false): Promise<RecoveryShiftPreview> {
  return request(`/api/goals/${goalId}/execution/recovery/shift`, {
    method: 'POST',
    body: JSON.stringify({ preview_id: previewId, accept_unallocated: acceptUnallocated }),
  })
}

export function deferRecovery(goalId: string, missedDates: string[]): Promise<ExecutionInactivity> {
  return request(`/api/goals/${goalId}/execution/recovery/defer`, {
    method: 'POST',
    body: JSON.stringify({ missed_dates: missedDates }),
  })
}

export function previewRecoveryCatchup(goalId: string, anchorDate?: string, includeNonExecutionDays = false, selection?: { source_plan_version_id: string; selected_task_ids: string[] }): Promise<RecoveryCatchupPreview> {
  if (selection) return request(`/api/goals/${goalId}/execution/recovery/catchup-preview`, {
    method: 'POST', body: JSON.stringify({ anchor_date: anchorDate, include_non_execution_days: includeNonExecutionDays, ...selection }),
  })
  const query = new URLSearchParams()
  if (anchorDate) query.set('anchor_date', anchorDate)
  query.set('include_non_execution_days', String(includeNonExecutionDays))
  return request(`/api/goals/${goalId}/execution/recovery/catchup-preview?${query}`)
}

export function applyRecoveryCatchup(
  goalId: string,
  previewId: string,
  acceptUnallocated = false,
): Promise<RecoveryCatchupPreview> {
  return request(`/api/goals/${goalId}/execution/recovery/catchup`, {
    method: 'POST',
    body: JSON.stringify({ preview_id: previewId, accept_unallocated: acceptUnallocated }),
  })
}
