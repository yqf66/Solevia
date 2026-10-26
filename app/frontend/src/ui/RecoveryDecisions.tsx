import type { PlanWorkspace } from '../api'

export function RecoveryDecisions({ review, busy, onDecision }: {
  review: PlanWorkspace['recovery_review']
  busy: boolean
  onDecision: (decision: 'ALLOW_EXTENSION' | 'KEEP_DEADLINE' | 'ACCEPT_LOAD') => void
}) {
  if (!review?.decisions.length) return null
  return <section className="proposal-question" aria-label="恢复安排待确认事项">
    {review.decisions.includes('ALLOW_EXTENSION') && <p>是否允许增加 {review.extension_days} 个顺延执行日{review.extension_dates.length ? `，结束日期调整到 ${review.extension_dates.at(-1)}` : ''}？确认后先预览，再决定是否发布。</p>}
    <div className="button-row">{review.decisions.map(decision => <button key={decision} className="button" type="button" disabled={busy} onClick={() => onDecision(decision)}>{decision === 'ALLOW_EXTENSION' ? '允许这次顺延' : decision === 'KEEP_DEADLINE' ? '不允许，保持原期限' : '接受这次近期负担'}</button>)}</div>
  </section>
}
