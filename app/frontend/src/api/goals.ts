// goals: extracted without changing request or response semantics.
import type { ClarificationWorkspace, GoalLifecycle, GoalListItem } from './contracts'
import { request } from './client'

export function createGoal(rawInput: string, timezone: string): Promise<ClarificationWorkspace> {
  return request('/api/goals', {
    method: 'POST',
    body: JSON.stringify({ raw_input: rawInput, timezone }),
  })
}

export function initializeGoal(
  rawInput: string,
  timezone: string,
  options: { webResearchEnabled?: boolean } = {},
): Promise<ClarificationWorkspace> {
  return request('/api/goals/initialize', {
    method: 'POST',
    body: JSON.stringify({
      raw_input: rawInput,
      timezone,
      ...(options.webResearchEnabled ? { web_research_enabled: true } : {}),
    }),
  })
}

export function startInitialClarification(goalId: string): Promise<ClarificationWorkspace> {
  return request(`/api/goals/${goalId}/clarification/start`, { method: 'POST' })
}

export function getClarification(goalId: string): Promise<ClarificationWorkspace> {
  return request(`/api/goals/${goalId}/clarification`)
}

export async function listGoals(options: { checkStatuses?: boolean } = {}): Promise<GoalListItem[]> {
  if (options.checkStatuses !== false) {
    try {
      const checked = await request<unknown>('/api/goals/status-check', { method: 'POST' })
      if (Array.isArray(checked)) return checked as GoalListItem[]
    } catch {
      // Listing remains available if the optional startup check cannot run.
    }
  }
  return request('/api/goals')
}

export function getGoalLifecycle(goalId: string): Promise<GoalLifecycle> {
  return request(`/api/goals/${goalId}/lifecycle`)
}

export function pauseGoal(goalId: string, pauseUntil: string | null): Promise<GoalLifecycle> {
  return request(`/api/goals/${goalId}/pause`, {
    method: 'POST',
    body: JSON.stringify({ pause_until: pauseUntil }),
  })
}

export function resumeGoal(goalId: string): Promise<GoalLifecycle> {
  return request(`/api/goals/${goalId}/resume`, { method: 'POST' })
}

export function completeGoal(goalId: string, note?: string): Promise<GoalLifecycle> {
  return request(`/api/goals/${goalId}/complete`, {
    method: 'POST',
    body: JSON.stringify({ note: note?.trim() || null }),
  })
}

export function archiveGoal(goalId: string): Promise<GoalLifecycle> {
  return request(`/api/goals/${goalId}/archive`, { method: 'POST' })
}

export function updateGoalSettings(goalId: string, timezone: string): Promise<GoalLifecycle> {
  return request(`/api/goals/${goalId}/settings`, {
    method: 'PATCH',
    body: JSON.stringify({ timezone }),
  })
}

export function renameGoal(goalId: string, title: string): Promise<GoalLifecycle> {
  return request(`/api/goals/${goalId}`, {
    method: 'PATCH',
    body: JSON.stringify({ title }),
  })
}

export function deleteGoal(goalId: string): Promise<void> {
  return request(`/api/goals/${goalId}`, {
    method: 'DELETE',
    body: JSON.stringify({ confirmation: goalId }),
  })
}

export function sendClarificationMessage(
  goalId: string,
  content: string,
  options: { webResearchEnabled?: boolean; researchRunId?: string; regenerate?: boolean; materialSourceIds?: string[] } = {},
): Promise<ClarificationWorkspace> {
  return request(`/api/goals/${goalId}/messages`, {
    method: 'POST',
    body: JSON.stringify({
      content,
      ...(options.webResearchEnabled ? { web_research_enabled: true } : {}),
      ...(options.researchRunId ? { research_run_id: options.researchRunId } : {}),
      ...(options.regenerate ? { regenerate: true } : {}),
      ...(options.materialSourceIds ? { material_source_ids: options.materialSourceIds } : {}),
    }),
  })
}

export function retryClarificationMessage(goalId: string): Promise<ClarificationWorkspace> {
  return request(`/api/goals/${goalId}/messages/retry`, { method: 'POST' })
}

export function regenerateClarificationMessage(goalId: string): Promise<ClarificationWorkspace> {
  return request(`/api/goals/${goalId}/messages/regenerate`, { method: 'POST' })
}

export function updateRequirementFact(
  goalId: string,
  factId: string,
  action: 'CONFIRM' | 'REJECT' | 'REPLACE',
  value?: unknown,
): Promise<ClarificationWorkspace> {
  return request(`/api/goals/${goalId}/facts/${factId}`, {
    method: 'PATCH',
    body: JSON.stringify({ action, value }),
  })
}

export function confirmRequirements(goalId: string): Promise<ClarificationWorkspace> {
  return request(`/api/goals/${goalId}/requirements/confirm`, { method: 'POST' })
}

export function selectPlanningApproach(
  goalId: string,
  planningMode: 'EXPLORATORY' | 'COMMITTED',
): Promise<ClarificationWorkspace> {
  return request(`/api/goals/${goalId}/planning-approach`, {
    method: 'PUT',
    body: JSON.stringify({ planning_mode: planningMode }),
  })
}

export function selectScheduleType(
  goalId: string,
  scheduleType: 'OUTCOME' | 'RHYTHM' | 'CALENDAR',
): Promise<ClarificationWorkspace> {
  return request(`/api/goals/${goalId}/schedule-type`, {
    method: 'PUT',
    body: JSON.stringify({ schedule_type: scheduleType }),
  })
}

export function prepareRequirementReview(goalId: string): Promise<ClarificationWorkspace> {
  return request(`/api/goals/${goalId}/requirements/draft`, { method: 'POST' })
}

export function retryClarificationAnalysis(goalId: string): Promise<ClarificationWorkspace> {
  return request(`/api/goals/${goalId}/clarification/analysis/retry`, { method: 'POST' })
}

export function discardRequirementChanges(goalId: string): Promise<ClarificationWorkspace> {
  return request(`/api/goals/${goalId}/requirements/changes/discard`, { method: 'POST' })
}
