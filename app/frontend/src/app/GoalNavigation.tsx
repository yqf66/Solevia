import { DialogBackdrop } from '../ui/DialogBackdrop'
import { useEffect, useRef, useState } from 'react'
import { Link, useLocation, useNavigate } from 'react-router-dom'
import { archiveGoal, deleteGoal, renameGoal, type GoalListItem } from '../api'
import { statusLabels, goalPath } from '../ui/plannerPresentation'
import { pausedGoalLabel } from './goalState'
import { GoalActionMenu } from './GoalActionMenu'

export function GoalListRow({ goal, onChanged }: { goal: GoalListItem; onChanged: () => Promise<void> }) {
  const navigate = useNavigate()
  const location = useLocation()
  const selected = location.pathname.split('/')[1] === 'goals' && location.pathname.split('/')[2] === goal.id
  const row = useRef<HTMLDivElement>(null)
  useEffect(() => {
    if (!selected || !row.current) return
    const group = row.current.closest<HTMLDetailsElement>('details.goal-group')
    if (group) group.open = true
    const frame = requestAnimationFrame(() => row.current?.scrollIntoView?.({ block: 'nearest', behavior: window.matchMedia?.('(prefers-reduced-motion: reduce)')?.matches ? 'auto' : 'smooth' }))
    return () => cancelAnimationFrame(frame)
  }, [selected])
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const [action, setAction] = useState<'RENAME' | 'ARCHIVE' | 'DELETE' | null>(null)
  const [renameValue, setRenameValue] = useState(goal.title)
  const savedRecoveryPath = sessionStorage.getItem(`planner:recovery-view:${goal.id}`)
  const resumePath = goal.status === 'RECOVERY_REQUIRED'
    && savedRecoveryPath === `/goals/${goal.id}/plan?recovery=1&discussion=1&adjustment=1`
    ? savedRecoveryPath : goalPath(goal)

  async function rename() {
    const title = renameValue.trim()
    if (!title || title === goal.title) { setAction(null); return }
    setBusy(true)
    setError('')
    try {
      await renameGoal(goal.id, title)
      await onChanged()
      setAction(null)
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : '暂时无法重命名。')
    } finally {
      setBusy(false)
    }
  }

  async function archive() {
    setBusy(true)
    setError('')
    try {
      await archiveGoal(goal.id)
      await onChanged()
      navigate(`/goals/${goal.id}/complete`)
      setAction(null)
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : '暂时无法归档。')
    } finally {
      setBusy(false)
    }
  }

  async function remove() {
    setBusy(true)
    setError('')
    try {
      await deleteGoal(goal.id)
      await onChanged()
      setAction(null)
      if (location.pathname.includes(`/goals/${goal.id}/`)) navigate('/', { replace: true })
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : '暂时无法删除。')
      setBusy(false)
    }
  }

  const endedLabel = goal.status === 'ARCHIVED'
    ? goal.completed_at ? '已完成' : '已提前结束'
    : goal.status === 'PAUSED' ? pausedGoalLabel(goal) : statusLabels[goal.status] ?? goal.status

  return <div ref={row} className={`plan-row${selected ? ' current-plan' : ''}`}>
    <Link className={`plan-link${selected ? ' current-plan-link' : ''}`} aria-current={selected ? 'page' : undefined} to={resumePath}>
      <strong>{goal.title}</strong>
      <span>{endedLabel}</span>
    </Link>
    <GoalActionMenu title={goal.title}>
        <button type="button" disabled={busy} onClick={() => { setRenameValue(goal.title); setAction('RENAME') }}>重命名</button>
        {goal.status !== 'ARCHIVED' && <button type="button" disabled={busy} onClick={() => setAction('ARCHIVE')}>{goal.status === 'COMPLETED' ? '收官并归档' : '提前结束'}</button>}
        <button className="danger" type="button" disabled={busy} onClick={() => setAction('DELETE')}>删除</button>
    </GoalActionMenu>
    {error && <small className="plan-row-error">{error}</small>}
    {action && <DialogBackdrop className="inline-dialog-backdrop" onDismiss={() => setAction(null)}> <div className="inline-dialog" role="dialog" aria-modal="true"><h2>{action === 'RENAME' ? '重命名规划' : action === 'ARCHIVE' ? (goal.status === 'COMPLETED' ? '收官并归档' : '提前结束规划') : '删除规划'}</h2>{action === 'RENAME' ? <label>规划名称<input autoFocus value={renameValue} onChange={(event) => setRenameValue(event.target.value)} /></label> : <p>{action === 'DELETE' ? `确定永久删除“${goal.title}”及其全部记录吗？` : `${goal.status === 'COMPLETED' ? '收官并归档' : '提前结束并归档'}“${goal.title}”？已有内容和记录会保留。`}</p>}<div className="inline-dialog-actions"><button className="button" type="button" onClick={() => setAction(null)}>取消</button><button className={`button ${action === 'DELETE' ? 'danger-button' : 'primary'}`} type="button" disabled={busy || (action === 'RENAME' && !renameValue.trim())} onClick={() => void (action === 'RENAME' ? rename() : action === 'ARCHIVE' ? archive() : remove())}>{busy ? '处理中…' : action === 'RENAME' ? '保存名称' : action === 'ARCHIVE' ? '确认归档' : '确认删除'}</button></div></div></DialogBackdrop>}
  </div>
}

export function GoalGroup({ title, goals, onChanged, initiallyOpen = true, className = '' }: { title: string; goals: GoalListItem[]; onChanged: () => Promise<void>; initiallyOpen?: boolean; className?: string }) {
  const [open, setOpen] = useState(initiallyOpen)
  return <details className={`plan-list goal-group ${className}`.trim()} open={open} onToggle={(event) => setOpen(event.currentTarget.open)}>
    <summary><span>{title}</span><small>{goals.length}</small></summary>
    <div className="goal-group-list">
      {goals.length === 0 ? <span className="empty-plans">暂无</span> : goals.map((goal) => <GoalListRow goal={goal} onChanged={onChanged} key={goal.id} />)}
    </div>
  </details>
}

