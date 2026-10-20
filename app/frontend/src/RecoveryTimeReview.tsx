import type { RecoveryTimePreview, RecoveryTimeItem } from './api'

const actionLabels = {
  ADJUST_AVAILABILITY: '补充可用时间', CHANGE_RECOVERY_DATE: '调整恢复方式或日期',
  REVIEW_DEPENDENCY: '先核对并处理前置任务', REVIEW_PLAN: '讨论调整剩余规划', KEEP_PENDING: '保留待处理',
}

export function RecoveryTimeReview({ preview, accepted, onAccept, disabled, goalId }: {
  preview?: RecoveryTimePreview | null
  accepted: boolean
  onAccept: (accepted: boolean) => void
  disabled: boolean
  goalId?: string
}) {
  if (!preview) return null
  function rows(items: RecoveryTimeItem[]) {
    return items.map((t, i) => <li key={`${t.source_task_id ?? i}-${t.date}`}>
      <time>{t.date} · {t.start ? `${t.start}–${t.end}` : t.state === 'FLEXIBLE' ? '自行决定具体时间' : preview?.diagnostics_only ? '尚未形成可应用安排' : '待安排具体时间'}</time>
      <strong>{t.title}</strong><small>预计 {t.minutes} 分钟{t.remaining_minutes != null ? ` · 剩余估时 ${t.remaining_minutes} 分钟` : ''}{t.source_date && t.source_date !== t.date ? ` · 原日期 ${t.source_date}` : ''}</small>
      {t.state === 'UNALLOCATED' && <>
        <p className="recovery-item-reason">{t.reason_detail ?? '暂时没有完整可用时段，请核对时间与任务要求。'}</p>
        {Boolean(t.allowed_actions?.length) && <small>可处理：{t.allowed_actions?.map(action => actionLabels[action]).join('；')}</small>}
      </>}
    </li>)
  }
  return <section className="recovery-time-review" aria-label="恢复后的具体时间预览">
    <h3>{preview.diagnostics_only ? '积压任务与阻塞原因' : '具体时间会怎样变化'}</h3><p>{preview.timezone} · {preview.mode === 'FLEXIBLE' ? '继续弹性安排' : '沿用自动安排时间策略'}</p>
    <details><summary>查看调整前的安排（{preview.before.length} 项）</summary><ul>{rows(preview.before)}</ul></details>
    <details open><summary>{preview.diagnostics_only ? '待处理积压' : '调整后的安排'}（{preview.after.length} 项）</summary><ul>{rows(preview.after)}</ul></details>
    {preview.unallocated_count > 0 && <div className="schedule-conflict-panel"><strong>{preview.unallocated_count} 项任务{preview.diagnostics_only ? '需要进一步处理' : '暂时无法分配具体时间'}</strong>
      <p>{preview.diagnostics_only ? '当前未生成可应用的追赶方案。请调整恢复方式，或进入剩余规划讨论。' : '可以返回修改方案，或先保留这些任务待安排。此操作不会自动并行，也不会自动删减任务。'}</p>
      {goalId && preview.after.some(item => item.allowed_actions?.includes('ADJUST_AVAILABILITY')) && <a href={`/goals/${goalId}/prepare`}>查看并补充可用时间</a>}
      {!preview.diagnostics_only && <label><input type="checkbox" checked={accepted} disabled={disabled} onChange={e => onAccept(e.target.checked)} />我确认这些任务暂时不设具体时间，之后再安排</label>}
    </div>}
  </section>
}
