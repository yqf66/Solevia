import { useEffect, useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import { archiveGoal, getExecutionCalendar, getExecutionProgress, getGoalLifecycle, type GoalLifecycle, type ExecutionCalendar, type ExecutionProgress } from '../api'
import { dateInputValue, goalPath, friendlyDate } from '../ui/plannerPresentation'
import { feedbackLabel } from './feedbackOptions'

function inclusiveDays(firstDate: string | null, finalDate: string | null) {
  if (!firstDate || !finalDate) return null
  const first = new Date(`${firstDate.slice(0, 10)}T00:00:00`)
  const finalDay = finalDate.includes('T') ? dateInputValue(new Date(finalDate)) : finalDate.slice(0, 10)
  const final = new Date(`${finalDay}T00:00:00`)
  const difference = Math.floor((final.getTime() - first.getTime()) / 86_400_000)
  return Number.isFinite(difference) ? Math.max(1, difference + 1) : null
}

export function GoalCompletionPage() {
  const { goalId } = useParams()
  const navigate = useNavigate()
  const [goal, setGoal] = useState<GoalLifecycle | null>(null)
  const [progress, setProgress] = useState<ExecutionProgress | null>(null)
  const [calendar, setCalendar] = useState<ExecutionCalendar | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')

  useEffect(() => {
    if (!goalId) return
    let active = true
    Promise.all([
      getGoalLifecycle(goalId),
      getExecutionProgress(goalId).catch(() => null),
      getExecutionCalendar(goalId).catch(() => null),
    ]).then(([lifecycle, executionProgress, executionCalendar]) => {
      if (!active) return
      if (lifecycle.status !== 'COMPLETED' && lifecycle.status !== 'ARCHIVED') {
        navigate(goalPath({ ...lifecycle, last_active_at: '', completed_at: lifecycle.completed_at, archived_at: lifecycle.archived_at }), { replace: true })
        return
      }
      setGoal(lifecycle)
      setProgress(executionProgress)
      setCalendar(executionCalendar)
    }).catch((caught) => {
      if (active) setError(caught instanceof Error ? caught.message : '暂时无法读取目标复盘。')
    })
    return () => { active = false }
  }, [goalId, navigate])

  async function closeGoal() {
    if (!goalId) return
    setBusy(true)
    setError('')
    try {
      const archived = await archiveGoal(goalId)
      setGoal(archived)
      navigate(`/goals/${goalId}/complete?closed=1`, { replace: true })
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : '暂时无法完成收官。')
    } finally {
      setBusy(false)
    }
  }

  if (!goal && !error) return <section className="page completion-loading"><span className="completion-mark" aria-hidden="true">✓</span><h1>正在整理这段目标历程</h1><p>完成情况、反馈与调整记录马上就好。</p></section>
  if (!goal) return <section className="page narrow error-state"><h1>暂时无法读取目标复盘</h1><p>{error}</p><button className="button" type="button" onClick={() => navigate('/')}>返回首页</button></section>

  const completedTasks = progress?.overall_progress.completed_tasks ?? 0
  const totalTasks = progress?.overall_progress.total_tasks ?? 0
  const focusMinutes = progress?.total_focus_minutes ?? progress?.trend.focus_minutes ?? 0
  const appliedAdjustments = progress?.applied_adjustments ?? progress?.feedback_timeline.filter((item) => item.adjustment?.status === 'APPLIED').length ?? 0
  const durationDays = inclusiveDays(calendar?.first_date ?? null, goal.completed_at ?? goal.archived_at)
  const endedNormally = Boolean(goal.completed_at)

  return <section className="page completion-page">
    <header className="completion-hero">
      <span className="completion-mark" aria-hidden="true">✓</span>
      <p className="eyebrow">{goal.status === 'COMPLETED' ? '所有安排已经完成' : endedNormally ? '目标已收官' : '目标已提前结束'}</p>
      <h1>{goal.title}</h1>
      <p>{goal.completion_summary || (endedNormally ? '这段目标已经完成，执行记录已整理在下方。' : '这段目标已结束，现有记录已完整保留。')}</p>
    </header>

    <section className="completion-stats" aria-label="目标结果概览">
      <div><span>历时</span><strong>{durationDays ? `${durationDays} 天` : '未进入执行'}</strong></div>
      <div><span>任务完成</span><strong>{totalTasks ? `${completedTasks} / ${totalTasks}` : '暂无任务'}</strong></div>
      <div><span>专注记录</span><strong>{focusMinutes} 分钟</strong></div>
      <div><span>计划版本</span><strong>{progress ? `${progress.plan_revision} 版` : '无'}</strong></div>
    </section>

    <section className="completion-section">
      <header><div><p className="eyebrow">过程复盘</p><h2>反馈与调整</h2></div><small>{progress?.feedback_timeline.length ?? 0} 条反馈 · {appliedAdjustments} 次已采用调整</small></header>
      {!progress || progress.feedback_timeline.length === 0
        ? <p className="completion-empty">这段执行期间没有留下反馈记录。</p>
        : <div className="completion-timeline">{progress.feedback_timeline.map((item) => <article key={item.id}>
          <time>{friendlyDate(item.scheduled_date)}</time>
          <div><header><strong>{item.task_title || '当天整体反馈'}</strong><span>{feedbackLabel(item.reason_code)}</span></header>{item.detail_text && <p>{item.detail_text}</p>}{item.adjustment && <div className="completion-adjustment"><strong>{item.adjustment.title}</strong><p>{item.adjustment.summary}</p><small>{item.adjustment.status === 'APPLIED' ? '已采用' : item.adjustment.status === 'DISMISSED' ? '未采用' : '待处理'}</small></div>}</div>
        </article>)}</div>}
    </section>

    <section className="completion-section">
      <header><div><p className="eyebrow">计划演进</p><h2>期间调整记录</h2></div><small>{progress?.revision_history.length ?? 0} 个版本记录</small></header>
      {!progress || progress.revision_history.length === 0
        ? <p className="completion-empty">计划期间没有发生版本调整。</p>
        : <div className="completion-revisions">{progress.revision_history.map((revision) => <article key={revision.id}><time>{friendlyDate(revision.created_at)}</time><div><strong>计划第 {revision.revision} 版</strong>{revision.feedback && <p>{revision.feedback}</p>}{revision.changes.length > 0 && <ul>{revision.changes.map((change) => <li key={change}>{change}</li>)}</ul>}</div></article>)}</div>}
    </section>

    {error && <p className="form-error" role="alert">{error}</p>}
    <footer className="completion-footer">
      {goal.status === 'COMPLETED'
        ? <button className="button primary close-goal-button" type="button" disabled={busy} onClick={() => void closeGoal()}>{busy ? '正在收官…' : '收官'}</button>
        : <button className="button" type="button" onClick={() => navigate('/')}>返回首页</button>}
      <p>{goal.status === 'COMPLETED' ? '收官后会移入左侧“已经结束的目标”，所有记录仍可随时查看。' : '这个目标已存放在左侧“已经结束的目标”中。'}</p>
    </footer>
  </section>
}
