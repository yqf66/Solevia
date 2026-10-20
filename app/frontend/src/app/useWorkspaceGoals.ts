import { useEffect, useState } from 'react'
import { useLocation } from 'react-router-dom'
import { getWorkspaceScheduleProfile, updateWorkspaceScheduleProfile, getHolidayRegionSuggestion, listGoals, listManualPlans, updateGoalSettings, type GoalListItem, type ManualPlan } from '../api'
import { detectedHolidayRegion, detectedTimezone } from '../ui/plannerPresentation'

// Workspace synchronization is separate from navigation and route rendering.
export function useWorkspaceGoals() {
  const location = useLocation()
  const activeGoalId = location.pathname.match(/^\/goals\/([^/]+)/)?.[1]
  const [goals, setGoals] = useState<GoalListItem[]>([])
  const [manualPlans, setManualPlans] = useState<ManualPlan[]>([])

  useEffect(() => {
    if (!activeGoalId) return
    const query = new URLSearchParams(location.search)
    if (location.pathname === `/goals/${activeGoalId}/plan` && query.get('recovery') === '1') {
      sessionStorage.setItem(`planner:recovery-view:${activeGoalId}`, `/goals/${activeGoalId}/plan?recovery=1&discussion=1&adjustment=1`)
    } else if (location.pathname === `/goals/${activeGoalId}/progress`) {
      sessionStorage.removeItem(`planner:recovery-view:${activeGoalId}`)
    }
  }, [activeGoalId, location.pathname, location.search])

  useEffect(() => {
    const detected = detectedHolidayRegion()
    getWorkspaceScheduleProfile().then(async (profile) => {
      if (profile.holiday_country_code) return
      const suggestion = await getHolidayRegionSuggestion(detected.timezone, detected.locale)
      if (!suggestion.country_code || suggestion.requires_confirmation) return
      return updateWorkspaceScheduleProfile({
        timezone: detected.timezone,
        holiday_country_code: suggestion.country_code,
        holiday_locale: detected.locale,
        holiday_source: 'AUTO',
        holiday_confidence: suggestion.confidence,
        holiday_confirmed: false,
      })
    }).catch(() => undefined)
  }, [])

  async function refreshGoals() {
    setGoals(await listGoals({ checkStatuses: false }))
  }

  useEffect(() => {
    let active = true
    let revision = 0
    const refresh = (checkStatuses = false) => {
      const requestRevision = ++revision
      void listGoals({ checkStatuses }).then(items => {
        if (!Array.isArray(items)) return
        if (active && requestRevision === revision) {
          setGoals(items)
          for (const goal of items) {
            if (goal.status !== 'RECOVERY_REQUIRED') sessionStorage.removeItem(`planner:recovery-view:${goal.id}`)
          }
        }
      }).catch(() => { /* Preserve the visible list while a refresh retries. */ })
    }
    refresh(true)
    const handleGoalsChanged = () => refresh()
    const handleVisible = () => { if (document.visibilityState === 'visible') refresh() }
    window.addEventListener('planner:goals-changed', handleGoalsChanged)
    window.addEventListener('focus', handleGoalsChanged)
    document.addEventListener('visibilitychange', handleVisible)
    return () => {
      active = false
      ++revision
      window.removeEventListener('planner:goals-changed', handleGoalsChanged)
      window.removeEventListener('focus', handleGoalsChanged)
      document.removeEventListener('visibilitychange', handleVisible)
    }
  }, [])
  useEffect(() => {
    const current = goals.find((goal) => goal.id === activeGoalId)
    const localTimezone = detectedTimezone()
    if (!current || current.status === 'ARCHIVED' || current.timezone === localTimezone) return
    void updateGoalSettings(current.id, localTimezone).then(() => {
      setGoals((existing) => existing.map((goal) => goal.id === current.id ? { ...goal, timezone: localTimezone } : goal))
    }).catch(() => undefined)
  }, [activeGoalId, goals])
  useEffect(() => {
    let active = true
    const refresh = () => { void listManualPlans().then(items => { if (active) setManualPlans(Array.isArray(items) ? items : []) }).catch(() => { if (active) setManualPlans([]) }) }
    refresh()
    window.addEventListener('planner:quick-plans-changed', refresh)
    return () => { active = false; window.removeEventListener('planner:quick-plans-changed', refresh) }
  }, [location.pathname])


  return { goals, manualPlans, refreshGoals }
}
