// execution: extracted without changing request or response semantics.
import type { AllocationInput, AllocationPreview, AvailabilityWindow, CandidatePool, CandidateReplacementPreview, ExecutionCalendar, ExecutionDayMove, ExecutionDiscussionWorkspace, ExecutionPreview, ExecutionProgress, ExecutionScheduleInput, ExecutionSetup, FixedActivityMovePreview, FixedActivityMoveRequest, FlexibleTaskDecisionPreview, FocusSession, PlanScheduleAllocation, PlanWorkspace, QuickCapture, ReminderEvent, ScheduleFoundation, SchedulePreference, SmartCalendar, TodayWorkspace, TriggeredTask, TriggeredTaskActivationPreview, UnifiedToday } from './contracts'
import { request } from './client'

export function previewSmartCalendar(goalId: string, input: { instructions: string; preferred_start_date: string | null }): Promise<SmartCalendar> {
  return request(`/api/goals/${goalId}/execution/calendar/smart-preview`, { method: 'POST', body: JSON.stringify(input) })
}

export function getCandidatePools(goalId: string): Promise<CandidatePool[]> {
  return request(`/api/goals/${goalId}/execution/candidate-pools`)
}

export function previewCandidateReplacement(goalId: string, input: { group_id: string; candidate_item_id: string; target_date: string; reason: string; period_key?: string }): Promise<CandidateReplacementPreview> {
  return request(`/api/goals/${goalId}/execution/candidate-pools/replacement-preview`, { method: 'POST', body: JSON.stringify(input) })
}

export function applyCandidateReplacement(goalId: string, previewId: string): Promise<TodayWorkspace> {
  return request(`/api/goals/${goalId}/execution/candidate-pools/replacement-apply`, { method: 'POST', body: JSON.stringify({ preview_id: previewId }) })
}

export function getTriggeredTasks(goalId: string): Promise<TriggeredTask[]> {
  return request(`/api/goals/${goalId}/execution/triggered-tasks`)
}

export function previewTriggeredTaskActivation(goalId: string, input: { window_id: string; target_date: string; evidence: string }): Promise<TriggeredTaskActivationPreview> {
  return request(`/api/goals/${goalId}/execution/triggered-tasks/preview`, { method: 'POST', body: JSON.stringify(input) })
}

export function applyTriggeredTaskActivation(goalId: string, previewId: string): Promise<TodayWorkspace> {
  return request(`/api/goals/${goalId}/execution/triggered-tasks/apply`, { method: 'POST', body: JSON.stringify({ preview_id: previewId }) })
}

export function previewExecutionDayMove(goalId: string, mode: 'ADVANCE' | 'SWAP', selectedDayId: string): Promise<ExecutionDayMove> {
  return request(`/api/goals/${goalId}/execution/days/preview`, { method: 'POST', body: JSON.stringify({ mode, selected_day_id: selectedDayId }) })
}

export function applyExecutionDayMove(goalId: string, previewId: string): Promise<TodayWorkspace> {
  return request(`/api/goals/${goalId}/execution/days/apply`, { method: 'POST', body: JSON.stringify({ preview_id: previewId }) })
}

export function getFixedActivities(goalId: string, onDate: string): Promise<{ activities: Array<{ id: string; title: string; start_local: string; end_local: string }> }> {
  return request(`/api/goals/${goalId}/fixed-activities?on_date=${onDate}`)
}

export function previewFixedActivity(goalId: string, input: FixedActivityMoveRequest): Promise<FixedActivityMovePreview> {
  return request(`/api/goals/${goalId}/fixed-activities/preview`, { method: 'POST', body: JSON.stringify(input) })
}

export function applyFixedActivity(goalId: string, previewId: string): Promise<FixedActivityMovePreview> {
  return request(`/api/goals/${goalId}/fixed-activities/${previewId}/apply`, { method: 'POST', body: JSON.stringify({ confirmed: true }) })
}

export function discardFixedActivity(goalId: string, previewId: string): Promise<unknown> {
  return request(`/api/goals/${goalId}/fixed-activities/${previewId}/discard`, { method: 'POST' })
}

export function previewAllocation(input: AllocationInput): Promise<AllocationPreview> {
  return request('/api/today/allocation/preview', { method: 'POST', body: JSON.stringify(input) })
}

export function previewAllocations(inputs: AllocationInput[]): Promise<AllocationPreview[]> {
  return request('/api/today/allocation/preview-many', { method: 'POST', body: JSON.stringify({ requests: inputs }) })
}

export function applyAllocation(preview: AllocationPreview): Promise<UnifiedToday> {
  return request('/api/today/allocation/apply', { method: 'POST', body: JSON.stringify({ request: preview.request, fingerprint: preview.fingerprint }) })
}

export function draftExplorationWindow(
  goalId: string,
  mode: 'STANDARD' = 'STANDARD',
): Promise<PlanWorkspace> {
  return request(`/api/goals/${goalId}/plan/exploration/draft`, {
    method: 'POST',
    body: JSON.stringify({ mode }),
  })
}

export function applyExplorationWindow(goalId: string): Promise<PlanWorkspace> {
  return request(`/api/goals/${goalId}/plan/exploration/apply`, { method: 'POST' })
}

export function discardExplorationWindow(goalId: string): Promise<PlanWorkspace> {
  return request(`/api/goals/${goalId}/plan/exploration/discard`, { method: 'POST' })
}

export function getExecutionSetup(goalId: string): Promise<ExecutionSetup> {
  return request(`/api/goals/${goalId}/execution/setup`)
}

export function getScheduleFoundation(goalId: string): Promise<ScheduleFoundation> {
  return request(`/api/goals/${goalId}/execution/schedule`)
}

export function allocateEntirePlan(goalId: string): Promise<PlanScheduleAllocation> {
  return request(`/api/goals/${goalId}/execution/schedule/allocate-plan`, { method: 'POST' })
}

export function getUnifiedToday(onDate?: string): Promise<UnifiedToday> {
  const query = onDate ? `?on_date=${encodeURIComponent(onDate)}` : ''
  return request(`/api/today${query}`)
}

export function scheduleTodayTask(input: {
  source: 'AGENT' | 'QUICK'
  task_id: string
  scheduled_date: string
  start_local: string
  end_local: string
  allow_parallel?: boolean
  parallel_reason?: string | null
}): Promise<{ scheduled: { task_id: string; source: 'AGENT' | 'QUICK'; scheduled_date: string; start_local: string; end_local: string; parallel_confirmed: boolean; parallel_reason: string | null }; conflicts: UnifiedToday['conflicts'] }> {
  return request('/api/today/schedule', { method: 'POST', body: JSON.stringify(input) })
}

export function updateSchedulePreference(
  goalId: string,
  input: Partial<Pick<SchedulePreference, 'mode' | 'timezone' | 'break_minutes' | 'allow_cross_window' | 'reminders_enabled' | 'supervision_mode' | 'cross_plan_priority_mode' | 'quiet_start_local' | 'quiet_end_local'>>,
): Promise<SchedulePreference> {
  return request(`/api/goals/${goalId}/execution/schedule/preferences`, {
    method: 'PATCH',
    body: JSON.stringify(input),
  })
}

export function updateCommitmentReminder(goalId: string, label: string, enabled: boolean): Promise<SchedulePreference> {
  return request(`/api/goals/${goalId}/execution/schedule/reminders`, {
    method: 'PATCH',
    body: JSON.stringify({ label, enabled }),
  })
}

export function listReminderEvents(goalId: string, includeClosed = false): Promise<ReminderEvent[]> {
  return request(`/api/goals/${goalId}/execution/reminders?include_closed=${includeClosed ? 'true' : 'false'}`)
}

export function updateReminderEvent(
  goalId: string,
  eventId: string,
  action: 'READ' | 'DISMISS' | 'REOPEN',
): Promise<ReminderEvent> {
  return request(`/api/goals/${goalId}/execution/reminders/${eventId}`, {
    method: 'PATCH',
    body: JSON.stringify({ action }),
  })
}

export function replaceAvailabilityWindows(
  goalId: string,
  input: {
    timezone?: string
    windows: Array<{
      weekday: number
      start_local: string
      end_local: string
      valid_from?: string | null
      valid_to?: string | null
    }>
  },
): Promise<AvailabilityWindow[]> {
  return request(`/api/goals/${goalId}/execution/schedule/availability`, {
    method: 'PUT',
    body: JSON.stringify(input),
  })
}

export function previewExecution(
  goalId: string,
  input: ExecutionScheduleInput,
): Promise<ExecutionPreview> {
  return request(`/api/goals/${goalId}/execution/preview`, {
    method: 'POST',
    body: JSON.stringify(input),
  })
}

export function startExecution(
  goalId: string,
  input: ExecutionScheduleInput,
): Promise<TodayWorkspace> {
  return request(`/api/goals/${goalId}/execution/start`, {
    method: 'POST',
    body: JSON.stringify(input),
  })
}

export function getToday(goalId: string, onDate?: string): Promise<TodayWorkspace> {
  const query = onDate ? `?on_date=${encodeURIComponent(onDate)}` : ''
  return request(`/api/goals/${goalId}/today${query}`)
}

export function carryForwardTask(
  goalId: string,
  input: { target_date: string; task_id?: string | null; minutes?: number | null },
): Promise<TodayWorkspace> {
  return request(`/api/goals/${goalId}/execution/carry-forward`, {
    method: 'POST',
    body: JSON.stringify(input),
  })
}

export function getExecutionCalendar(goalId: string): Promise<ExecutionCalendar> {
  return request(`/api/goals/${goalId}/execution/calendar`)
}

export function arrangeFlexibleTaskToday(goalId: string, taskId: string): Promise<TodayWorkspace> {
  return request(`/api/goals/${goalId}/execution/flexible-tasks/arrange`, {
    method: 'POST',
    body: JSON.stringify({ task_id: taskId }),
  })
}

export function rescheduleFlexibleTask(
  goalId: string,
  input: { task_id: string; target_date: string; reason: string },
): Promise<TodayWorkspace> {
  return request(`/api/goals/${goalId}/execution/flexible-tasks/reschedule`, {
    method: 'POST',
    body: JSON.stringify(input),
  })
}

export function previewFlexibleTaskDecision(
  goalId: string,
  input: { action: 'DEFER' | 'ARRANGE_TODAY' | 'RESCHEDULE'; task_id: string; target_date?: string; reason?: string },
): Promise<FlexibleTaskDecisionPreview> {
  return request(`/api/goals/${goalId}/execution/flexible-tasks/decision-preview`, {
    method: 'POST',
    body: JSON.stringify(input),
  })
}

export function applyFlexibleTaskDecision(goalId: string, previewId: string): Promise<TodayWorkspace> {
  return request(`/api/goals/${goalId}/execution/flexible-tasks/decision-apply`, {
    method: 'POST',
    body: JSON.stringify({ preview_id: previewId }),
  })
}

export function getExecutionProgress(goalId: string): Promise<ExecutionProgress> {
  return request(`/api/goals/${goalId}/execution/progress`)
}

export function getExecutionDiscussion(goalId: string): Promise<ExecutionDiscussionWorkspace> {
  return request(`/api/goals/${goalId}/execution/discussion`)
}

export function sendExecutionDiscussionMessage(
  goalId: string,
  content: string,
  options: { webResearchEnabled?: boolean; researchRunId?: string; useSavedResearch?: boolean; regenerate?: boolean; materialSourceIds?: string[] } = {},
): Promise<ExecutionDiscussionWorkspace> {
  return request(`/api/goals/${goalId}/execution/discussion/messages`, {
    method: 'POST',
    body: JSON.stringify({
      content,
      ...(options.webResearchEnabled ? { web_research_enabled: true } : {}),
      ...(options.researchRunId ? { research_run_id: options.researchRunId } : {}),
      ...(options.useSavedResearch === false ? { use_saved_research: false } : {}),
      ...(options.materialSourceIds !== undefined ? { material_source_ids: options.materialSourceIds } : {}),
      ...(options.regenerate ? { regenerate: true } : {}),
    }),
  })
}

export function retryExecutionDiscussion(goalId: string): Promise<ExecutionDiscussionWorkspace> {
  return request(`/api/goals/${goalId}/execution/discussion/retry`, { method: 'POST' })
}

export function decideExecutionInsight(
  goalId: string,
  insightId: string,
  action: 'CONFIRM' | 'DISMISS',
): Promise<ExecutionDiscussionWorkspace> {
  return request(`/api/goals/${goalId}/execution/insights/${insightId}`, {
    method: 'PUT',
    body: JSON.stringify({ action }),
  })
}

export function dismissExecutionReview(
  goalId: string,
  reviewId: string,
): Promise<ExecutionDiscussionWorkspace> {
  return request(`/api/goals/${goalId}/execution/reviews/${reviewId}/dismiss`, { method: 'POST' })
}

export function getFocusSessions(goalId: string): Promise<FocusSession[]> {
  return request(`/api/goals/${goalId}/focus-sessions`)
}

export function recordFocusSession(
  goalId: string,
  input: { task_id: string | null; planned_minutes: number; actual_seconds: number; interruptions: number },
): Promise<FocusSession> {
  return request(`/api/goals/${goalId}/focus-sessions`, {
    method: 'POST',
    body: JSON.stringify(input),
  })
}

export function getQuickCaptures(goalId: string): Promise<QuickCapture[]> {
  return request(`/api/goals/${goalId}/quick-captures`)
}

export function createQuickCapture(goalId: string, content: string): Promise<QuickCapture> {
  return request(`/api/goals/${goalId}/quick-captures`, {
    method: 'POST',
    body: JSON.stringify({ content }),
  })
}

export function deleteQuickCapture(goalId: string, captureId: string): Promise<QuickCapture[]> {
  return request(`/api/goals/${goalId}/quick-captures/${captureId}`, { method: 'DELETE' })
}

export function startPlanTask(goalId: string, taskId: string): Promise<TodayWorkspace> {
  return request(`/api/goals/${goalId}/tasks/${taskId}/start`, { method: 'POST' })
}

export function completePlanTask(goalId: string, taskId: string): Promise<TodayWorkspace> {
  return request(`/api/goals/${goalId}/tasks/${taskId}/complete`, { method: 'POST' })
}

export function recordPlanTaskProgress(
  goalId: string,
  taskId: string,
  input: { action: 'PARTIAL' | 'SKIP' | 'DEFER'; amount?: number; unit?: 'COUNT' | 'MINUTES'; reason?: string },
): Promise<TodayWorkspace> {
  return request(`/api/goals/${goalId}/tasks/${taskId}/progress`, {
    method: 'POST',
    body: JSON.stringify(input),
  })
}

export function completeMissedTasks(goalId: string, taskIds: string[]): Promise<ExecutionProgress> {
  return request(`/api/goals/${goalId}/execution/recovery/complete-missed`, {
    method: 'POST',
    body: JSON.stringify({ task_ids: taskIds }),
  })
}

export function completeToday(goalId: string): Promise<TodayWorkspace> {
  return request(`/api/goals/${goalId}/today/complete`, { method: 'POST' })
}

export function submitTaskFeedback(
  goalId: string,
  taskId: string,
  input: { reason_code: string; intent_code: string | null; detail_text: string | null; remaining_minutes?: number | null },
): Promise<TodayWorkspace> {
  return request(`/api/goals/${goalId}/tasks/${taskId}/feedback`, {
    method: 'POST',
    body: JSON.stringify(input),
  })
}

export function submitDayPaceFeedback(
  goalId: string,
  input: { reason_code: string; intent_code: string | null; detail_text: string | null },
): Promise<TodayWorkspace> {
  return request(`/api/goals/${goalId}/today/feedback`, {
    method: 'POST',
    body: JSON.stringify(input),
  })
}

export function deleteFeedback(goalId: string, feedbackId: string): Promise<TodayWorkspace> {
  return request(`/api/goals/${goalId}/feedback/${feedbackId}`, { method: 'DELETE' })
}

export function applyFeedbackAdjustment(
  goalId: string,
  adjustmentId: string,
): Promise<TodayWorkspace> {
  return request(`/api/goals/${goalId}/feedback-adjustments/${adjustmentId}/apply`, {
    method: 'POST',
  })
}

export function discardFeedbackAdjustment(
  goalId: string,
  adjustmentId: string,
): Promise<TodayWorkspace> {
  return request(`/api/goals/${goalId}/feedback-adjustments/${adjustmentId}/discard`, {
    method: 'POST',
  })
}
