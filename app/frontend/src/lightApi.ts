import { request as sharedRequest, type ManualTask, type ManualPlan } from './api'

export type LightTask = Pick<ManualTask, 'title' | 'description' | 'estimate_minutes' | 'priority' |
  'scheduled_date' | 'start_local' | 'end_local' | 'recurrence' | 'recurrence_weekdays' | 'recurrence_end_date'> & {
  flexible_recurrence: boolean
}
export type LightResult = {
  title: string
  tasks: LightTask[]
  assumptions: string[]
  questions: string[]
  unavailable_windows: Array<{ date: string; start_local: string; end_local: string; reason: string }>
  full_planning_recommended: boolean
}
export type LightDraft = {
  id: string; raw_input: string; timezone: string; start_date: string; horizon_days: number
  status: 'DRAFT' | 'GENERATING' | 'READY' | 'FAILED' | 'APPLIED'
  revision: number; result: LightResult; error_message: string | null; quick_plan_id: string | null
}

async function request<T>(path: string, method = 'GET', body?: unknown): Promise<T> {
  return sharedRequest<T>(path, { method,
    body: body === undefined ? undefined : JSON.stringify(body) })
}
export const listLightDrafts = () => request<LightDraft[]>('/api/quick-plan-drafts')
export const getLightDraft = (id: string) => request<LightDraft>(`/api/quick-plan-drafts/${id}`)
export const createLightDraft = (input: { raw_input: string; timezone: string; start_date: string; horizon_days: number }) => request<LightDraft>('/api/quick-plan-drafts', 'POST', input)
export const generateLightDraft = (draft: LightDraft) => request<LightDraft>(`/api/quick-plan-drafts/${draft.id}/generate`, 'POST', { revision: draft.revision })
export const saveLightDraft = (draft: LightDraft, result: LightResult) => request<LightDraft>(`/api/quick-plan-drafts/${draft.id}`, 'PATCH', { revision: draft.revision, result })
export const applyLightDraft = (draft: LightDraft) => request<LightDraft>(`/api/quick-plan-drafts/${draft.id}/apply`, 'POST', { revision: draft.revision })
export const upgradeLightPlan = (id: string) => request<{ goal_id: string; status: string }>(`/api/quick-plans/${id}/upgrade`, 'POST')
export const addLightTask = (id: string, task: LightTask) => request<ManualPlan>(`/api/quick-plans/${id}/tasks`, 'POST', task)
export const discardLightDraft = (draft: LightDraft) => request<void>(`/api/quick-plan-drafts/${draft.id}`, 'DELETE', { revision: draft.revision })
export const archiveLightPlan = (id: string, restore = false) => request<ManualPlan>(`/api/quick-plans/${id}/${restore ? 'restore' : 'archive'}`, 'POST').then(plan => { window.dispatchEvent(new Event('planner:quick-plans-changed')); return plan })

export function localDate(offset = 0): string {
  const date = new Date(); date.setDate(date.getDate() + offset)
  return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, '0')}-${String(date.getDate()).padStart(2, '0')}`
}
export const blankLightTask = (): LightTask => ({ title: '', description: '', estimate_minutes: 30,
  priority: 3, scheduled_date: null, start_local: null, end_local: null, recurrence: 'ONCE',
  recurrence_weekdays: [], recurrence_end_date: null, flexible_recurrence: true })
