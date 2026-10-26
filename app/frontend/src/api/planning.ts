// planning: extracted without changing request or response semantics.
import type { GenerationBatch, GenerationRun, PlanAdjustmentPreview, PlanWorkspace, RouteWorkspace, WorkItemDailyPreview } from './contracts'
import { request } from './client'

export function getRoute(goalId: string): Promise<RouteWorkspace> {
  return request(`/api/goals/${goalId}/route`)
}

export function draftRoute(goalId: string): Promise<RouteWorkspace> {
  return request(`/api/goals/${goalId}/route/draft`, { method: 'POST' })
}

export function reviseRoute(
  goalId: string,
  content: string,
  options: { webResearchEnabled?: boolean; researchRunId?: string; regenerate?: boolean; materialSourceIds?: string[] } = {},
): Promise<RouteWorkspace> {
  return request(`/api/goals/${goalId}/route/revise`, {
    method: 'POST',
    body: JSON.stringify({
      content,
      ...(options.webResearchEnabled ? { web_research_enabled: true } : {}),
      ...(options.researchRunId ? { research_run_id: options.researchRunId } : {}),
      ...(options.regenerate ? { regenerate: true } : {}),
      ...(options.materialSourceIds?.length ? { material_source_ids: options.materialSourceIds } : {}),
    }),
  })
}

export function explainRoute(
  goalId: string,
  content: string,
  options: { webResearchEnabled?: boolean; researchRunId?: string; regenerate?: boolean; materialSourceIds?: string[] } = {},
): Promise<RouteWorkspace> {
  return request(`/api/goals/${goalId}/route/explain`, {
    method: 'POST',
    body: JSON.stringify({
      content,
      ...(options.webResearchEnabled ? { web_research_enabled: true } : {}),
      ...(options.researchRunId ? { research_run_id: options.researchRunId } : {}),
      ...(options.regenerate ? { regenerate: true } : {}),
      ...(options.materialSourceIds?.length ? { material_source_ids: options.materialSourceIds } : {}),
    }),
  })
}

export function confirmRoute(goalId: string): Promise<RouteWorkspace> {
  return request(`/api/goals/${goalId}/route/confirm`, { method: 'POST' })
}

export function getPlan(goalId: string): Promise<PlanWorkspace> {
  return request(`/api/goals/${goalId}/plan`)
}

export function getPlanDailyPreview(goalId: string): Promise<WorkItemDailyPreview> {
  return request(`/api/goals/${goalId}/plan/daily-preview`)
}

export function getGenerationRuns(goalId: string): Promise<GenerationRun[]> {
  return request(`/api/goals/${goalId}/generation-runs`)
}

export function draftPlan(goalId: string): Promise<PlanWorkspace> {
  return request(`/api/goals/${goalId}/plan/draft`, { method: 'POST' })
}

export function draftNextPlanDetails(goalId: string): Promise<PlanWorkspace> {
  return request(`/api/goals/${goalId}/plan/details/next`, { method: 'POST' })
}

export function startPlanGeneration(
  goalId: string,
  mode: 'STANDARD' = 'STANDARD',
): Promise<PlanWorkspace> {
  return request(`/api/goals/${goalId}/plan/generation/start`, {
    method: 'POST',
    body: JSON.stringify({ mode }),
  })
}

export function explainPlan(
  goalId: string,
  content: string,
  webResearchEnabled = false,
  researchRunId?: string,
  regenerate = false,
  materialSourceIds?: string[],
): Promise<PlanWorkspace> {
  return request(`/api/goals/${goalId}/plan/explain`, {
    method: 'POST',
    body: JSON.stringify({
      content,
      ...(webResearchEnabled ? { web_research_enabled: true } : {}),
      ...(researchRunId ? { research_run_id: researchRunId } : {}),
      ...(regenerate ? { regenerate: true } : {}),
      ...(materialSourceIds?.length ? { material_source_ids: materialSourceIds } : {}),
    }),
  })
}

export function revisePlan(
  goalId: string,
  content: string,
  webResearchEnabled = false,
  researchRunId?: string,
  regenerate = false,
  materialSourceIds?: string[],
  recoveryOptions?: import('./contracts').RecoveryPreviewOptions,
): Promise<PlanWorkspace> {
  return request(`/api/goals/${goalId}/plan/revise`, {
    method: 'POST',
    body: JSON.stringify({
      content,
      ...(webResearchEnabled ? { web_research_enabled: true } : {}),
      ...(researchRunId ? { research_run_id: researchRunId } : {}),
      ...(regenerate ? { regenerate: true } : {}),
      ...(materialSourceIds?.length ? { material_source_ids: materialSourceIds } : {}),
      ...(recoveryOptions ? { recovery_options: recoveryOptions } : {}),
    }),
  })
}

export function discardPlanAdjustment(goalId: string): Promise<PlanWorkspace> {
  return request(`/api/goals/${goalId}/plan/adjustment/discard`, {
    method: 'POST',
  })
}

export function previewPlanAdjustment(goalId: string): Promise<PlanAdjustmentPreview> {
  return request(`/api/goals/${goalId}/plan/adjustment/preview`, {
    method: 'POST',
  }, 900000)
}

export function applyPlanAdjustment(goalId: string, previewId?: string, acceptUnallocated = false, scheduleOverrides: Array<{ scheduled_date: string; title: string; start_local: string | null; end_local: string | null }> = []): Promise<PlanWorkspace> {
  return request(`/api/goals/${goalId}/plan/adjustment/apply`, {
    method: 'POST',
    body: JSON.stringify(previewId ? { preview_id: previewId, accept_unallocated: acceptUnallocated, schedule_overrides: scheduleOverrides } : {}),
  })
}

export function restorePlanVersion(
  goalId: string,
  planId: string,
): Promise<PlanWorkspace> {
  return request(`/api/goals/${goalId}/plan/versions/${planId}/restore`, {
    method: 'POST',
  })
}

export function confirmPlan(goalId: string): Promise<PlanWorkspace> {
  return request(`/api/goals/${goalId}/plan/confirm`, { method: 'POST' })
}

export function retryGenerationBatch(goalId: string, batchId: string): Promise<GenerationBatch> {
  return request(`/api/goals/${goalId}/plan/compiler/batches/${batchId}/retry`, {
    method: 'POST',
  })
}

export function getGenerationBatches(goalId: string): Promise<GenerationBatch[]> {
  return request(`/api/goals/${goalId}/plan/compiler/batches`)
}
