import { useEffect, useState } from 'react'
import { getUnifiedToday, scheduleTodayTask, type UnifiedToday } from '../api'

export function useTodayDashboard() {
  const [today, setToday] = useState<UnifiedToday | null>(null)
  const [error, setError] = useState('')
  const [scheduleTarget, setScheduleTarget] = useState<UnifiedToday['fixed_tasks'][number] | null>(null)
  const [scheduleStart, setScheduleStart] = useState('19:00')
  const [scheduleEnd, setScheduleEnd] = useState('19:30')
  const [scheduleConflict, setScheduleConflict] = useState(false)
  const [parallelReason, setParallelReason] = useState('')
  const [scheduleBusy, setScheduleBusy] = useState(false)
  const [loading, setLoading] = useState(true)
  const [requestId, setRequestId] = useState(0)

  useEffect(() => {
    let cancelled = false
    async function load() {
      try {
        const result = await getUnifiedToday()
        if (!cancelled) { setToday(result); setError('') }
      } catch (caught) {
        if (!cancelled) setError(caught instanceof Error ? caught.message : '暂时无法读取今日任务。')
      } finally {
        if (!cancelled) setLoading(false)
      }
    }
    void load()
    return () => { cancelled = true }
  }, [requestId])

  function retry() {
    setLoading(true)
    setError('')
    setRequestId(value => value + 1)
  }

  async function schedule(task: UnifiedToday['fixed_tasks'][number], allowParallel = false) {
    if (scheduleBusy) return
    setScheduleBusy(true)
    setError('')
    try {
      await scheduleTodayTask({ source: task.source, task_id: task.id, scheduled_date: task.scheduled_date, start_local: scheduleStart, end_local: scheduleEnd, allow_parallel: allowParallel, parallel_reason: allowParallel ? parallelReason : null })
      setToday(await getUnifiedToday(task.scheduled_date))
      setScheduleTarget(null)
      setScheduleConflict(false)
      setParallelReason('')
    } catch (caught) {
      if ((caught as Error & { code?: string }).code === 'SCHEDULE_CONFLICT') setScheduleConflict(true)
      else setError(caught instanceof Error ? caught.message : '暂时无法安排时间。')
    } finally {
      setScheduleBusy(false)
    }
  }

  function openSchedule(task: UnifiedToday['fixed_tasks'][number]) {
    setError('')
    setScheduleTarget(task)
    setScheduleStart('19:00')
    setScheduleEnd('19:30')
    setScheduleConflict(false)
    setParallelReason('')
  }

  return { today, setToday, error, loading, retry, scheduleTarget, setScheduleTarget, scheduleStart, setScheduleStart, scheduleEnd, setScheduleEnd, scheduleConflict, setScheduleConflict, parallelReason, setParallelReason, scheduleBusy, schedule, openSchedule }
}

export type TodayDashboardState = ReturnType<typeof useTodayDashboard>
