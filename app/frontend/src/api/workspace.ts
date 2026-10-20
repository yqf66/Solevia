// workspace: extracted without changing request or response semantics.
import type { EffectiveWorkspaceAvailability, HolidayCalendar, HolidayRegionSuggestion, ModelConnection, WorkspaceAvailabilityWindow, WorkspaceScheduleProfile } from './contracts'
import { request } from './client'

export function getWorkspaceScheduleProfile(): Promise<WorkspaceScheduleProfile> {
  return request('/api/schedule/profile')
}

export function updateWorkspaceScheduleProfile(input: Partial<Pick<WorkspaceScheduleProfile, 'timezone' | 'default_mode' | 'break_minutes' | 'availability_mode' | 'wake_time' | 'available_from' | 'breakfast_enabled' | 'breakfast_start' | 'breakfast_end' | 'lunch_enabled' | 'lunch_start' | 'lunch_end' | 'nap_enabled' | 'nap_start' | 'nap_end' | 'dinner_enabled' | 'dinner_start' | 'dinner_end' | 'sleep_time' | 'routine_exclusions' | 'schedule_style' | 'holiday_country_code' | 'holiday_subdivision_code' | 'holiday_locale' | 'holiday_source' | 'holiday_confidence' | 'holiday_confirmed'>>): Promise<WorkspaceScheduleProfile> {
  return request('/api/schedule/profile', { method: 'PATCH', body: JSON.stringify(input) })
}

export function getWorkspaceHolidays(startDate: string, endDate: string): Promise<HolidayCalendar> {
  const query = new URLSearchParams({ start_date: startDate, end_date: endDate })
  return request(`/api/schedule/profile/holidays?${query}`)
}

export function getHolidayRegionSuggestion(timezone: string, locale: string): Promise<HolidayRegionSuggestion> {
  const query = new URLSearchParams({ timezone, locale })
  return request(`/api/schedule/profile/holiday-suggestion?${query}`)
}

export function getWorkspaceAvailability(): Promise<WorkspaceAvailabilityWindow[]> {
  return request('/api/schedule/profile/availability')
}

export function replaceWorkspaceAvailability(windows: Array<{ weekday: number; start_local: string; end_local: string; valid_from?: string | null; valid_to?: string | null }>): Promise<WorkspaceAvailabilityWindow[]> {
  return request('/api/schedule/profile/availability', { method: 'PUT', body: JSON.stringify({ windows }) })
}

export function getEffectiveWorkspaceAvailability(onDate: string): Promise<EffectiveWorkspaceAvailability> {
  return request(`/api/schedule/profile/availability/effective?on_date=${encodeURIComponent(onDate)}`)
}

export function resetWorkspaceAvailability(): Promise<EffectiveWorkspaceAvailability> {
  return request('/api/schedule/profile/availability/reset', { method: 'POST' })
}

export function getModelConnection(): Promise<ModelConnection> {
  return request('/api/settings/model')
}

export function updateModelConnection(input: Pick<ModelConnection, 'provider' | 'base_url' | 'proxy_url' | 'model'> & { api_key?: string; clear_api_key?: boolean }): Promise<ModelConnection> {
  return request('/api/settings/model', { method: 'PUT', body: JSON.stringify(input) })
}
