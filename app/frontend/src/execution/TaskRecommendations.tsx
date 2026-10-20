import { Sparkles } from 'lucide-react'
import { useEffect, useRef, useState } from 'react'
import { ArrowUp, RefreshCw, SlidersHorizontal } from 'lucide-react'
import { previewTaskRecommendations, type TaskRecommendationItem, type TaskRecommendationPreview } from '../api'
import { friendlyDate } from '../ui/plannerPresentation'
import { CollapsibleTool } from './ExecutionTools'

type Props = {
  goalId: string
  refreshKey: unknown
  disabled: boolean
  onViewDate: (date: string) => void
  onArrange: (task: TaskRecommendationItem) => void
  onReschedule: (task: TaskRecommendationItem) => void
}

const policyLabels: Record<string, string> = {
  FLOATING: '弹性任务', CADENCE: '节奏任务', DATE_WINDOW: '期限任务', QUOTA: '总量任务',
}

export function TaskRecommendations({ goalId, refreshKey, disabled, onViewDate, onArrange, onReschedule }: Props) {
  const [preview, setPreview] = useState<TaskRecommendationPreview | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [refresh, setRefresh] = useState(0)
  const [instruction, setInstruction] = useState('')
  const [appliedInstruction, setAppliedInstruction] = useState('')
  const requestSequence = useRef(0)
  const snapshotKey = JSON.stringify(refreshKey)
  useEffect(() => {
    let active = true
    const sequence = ++requestSequence.current
    setLoading(true)
    setError('')
    setPreview(null)
    previewTaskRecommendations(goalId, { use_ai: true, instruction: appliedInstruction }).then((result) => {
      if (active && sequence === requestSequence.current) setPreview(result)
    }).catch((caught) => {
      if (active && sequence === requestSequence.current) setError(caught instanceof Error ? caught.message : '暂时无法读取建议')
    }).finally(() => { if (active && sequence === requestSequence.current) setLoading(false) })
    return () => { active = false; requestSequence.current += 1 }
  }, [goalId, snapshotKey, appliedInstruction, refresh])
  function recommend() {
    setAppliedInstruction(instruction.trim())
    setRefresh((value) => value + 1)
  }
  const items = preview?.items ?? []
  return <CollapsibleTool icon={<Sparkles size={16} aria-hidden="true" />} title="今日建议" summary={loading ? '正在检查任务与余量' : items.length ? `${items.length} 项 · 预计 ${preview?.capacity.recommended_minutes ?? 0} 分钟` : error ? '暂时无法读取' : '当前没有额外建议'}>
    <section className="task-recommendations" aria-label="今日建议安排" aria-busy={loading} data-strategy={preview?.strategy ?? 'LOADING'}>
      <div className="recommendation-toolbar"><small>{preview ? `今日余量 ${preview.capacity.spare_minutes} 分钟` : '智能推荐'}</small><button className="recommendation-icon" type="button" title="刷新智能建议" aria-label="刷新今日建议" disabled={loading || disabled} onClick={() => setRefresh((value) => value + 1)}><RefreshCw size={15} /></button></div>
      <details className="recommendation-preferences"><summary><SlidersHorizontal size={14} />调整偏好</summary><form className="recommendation-request" onSubmit={(event) => { event.preventDefault(); recommend() }}>
        <textarea aria-label="今日推荐偏好" placeholder="今天想优先做什么？" rows={2} maxLength={1000} value={instruction} disabled={loading || disabled} onChange={(event) => setInstruction(event.target.value)} />
        <button className="recommendation-icon" type="submit" title="按偏好重新推荐" aria-label="按偏好重新推荐" disabled={loading || disabled}><ArrowUp size={16} /></button>
      </form></details>
      {loading && <p className="recommendation-status" role="status">正在分析今天的候选任务…</p>}
      {error && <p className="form-error" role="alert">{error}</p>}
      {preview && <>
        {preview.strategy === 'AI_FALLBACK' && <p className="recommendation-status">规则建议 · 智能推荐未完成</p>}
        {preview.summary && <p className="recommendation-summary">{preview.summary}</p>}
        {preview.warnings.map((warning) => <p className="today-suggestions-risk" key={warning}>{warning}</p>)}
        {items.length === 0 && <p className="today-suggestions-empty">当前没有可加入今天的建议任务。</p>}
        <div className="today-suggestions-list">{items.map((task) => <article key={task.task_id}>
          <div className="today-suggestion-main"><div><strong>{task.title}</strong><small>{policyLabels[task.scheduling_policy] ?? '任务'} · {task.estimate_minutes} 分钟</small></div></div>
          <p>{task.reason}</p>
          {task.remaining_value !== null && <small>还需 {task.remaining_value} {task.target_unit === 'MINUTES' ? '分钟' : '次'}</small>}
          <div className="today-suggestion-actions">
            {task.allowed_actions.includes('ARRANGE_TODAY') && <button className="button" type="button" disabled={disabled || loading} onClick={() => onArrange(task)}>安排到今天</button>}
            {task.allowed_actions.includes('RESCHEDULE') && <button className="button quiet" type="button" disabled={disabled || loading} onClick={() => onReschedule(task)}>改到某日</button>}
            <button className="button quiet" type="button" disabled={disabled} onClick={() => onViewDate(task.scheduled_date)}>查看建议日</button>
          </div>
        </article>)}</div>
        {preview.blocked_count > 0 && <details className="recommendation-blocked"><summary>暂不推荐 · {preview.blocked_count} 项</summary><ul>{preview.blocked_summary.map((task) => <li key={task.task_id}><strong>{task.title}</strong><p>{task.reason}</p><button className="button quiet" type="button" disabled={disabled} onClick={() => onViewDate(task.scheduled_date)}>查看 {friendlyDate(task.scheduled_date)}</button></li>)}</ul>{preview.blocked_count > preview.blocked_summary.length && <small>还有 {preview.blocked_count - preview.blocked_summary.length} 项，请在日历查看。</small>}</details>}
      </>}
    </section>
  </CollapsibleTool>
}
