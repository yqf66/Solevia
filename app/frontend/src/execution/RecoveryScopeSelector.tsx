import type { RecoveryScope } from '../api'
import { ChevronRight, ListChecks } from 'lucide-react'
import { friendlyDate } from '../ui/plannerPresentation'

export function RecoveryScopeSelector({ scope, selected, onChange, disabled, dirty, onPreview, onSplit }: {
  scope: RecoveryScope
  selected: string[]
  onChange: (ids: string[]) => void
  disabled: boolean
  dirty: boolean
  onPreview: () => void
  onSplit?: (taskId: string) => void
}) {
  const selectedSet = new Set(selected)
  const included = scope.items.filter(item => selectedSet.has(item.source_task_id))
  const deferred = scope.items.filter(item => !selectedSet.has(item.source_task_id))
  const groups = Array.from(new Set(scope.items.map(item => item.source_date)))
  return <section className="recovery-scope" aria-label="本次恢复范围">
    <header><strong>本次恢复范围</strong><small>纳入 {included.length} 项 · {included.reduce((sum, item) => sum + item.minutes, 0)} 分钟；保留待处理 {deferred.length} 项 · {deferred.reduce((sum, item) => sum + item.minutes, 0)} 分钟</small></header>
    <details open={dirty || undefined}><summary><ListChecks size={18} aria-hidden="true" /><strong>选择要恢复的任务</strong><span>已选 {included.length} / {scope.items.length} 项</span><ChevronRight className="recovery-scope-chevron" size={18} aria-hidden="true" /></summary>
      <label className="recovery-scope-select-all"><input type="checkbox" checked={selected.length === scope.items.length} disabled={disabled} onChange={event => onChange(event.target.checked ? scope.items.map(item => item.source_task_id) : [])} />全部纳入本次恢复</label>
      <div className="recovery-scope-list">{groups.map(date => {
        const items = scope.items.filter(item => item.source_date === date)
        const checked = items.every(item => selectedSet.has(item.source_task_id))
        return <fieldset key={date}><legend><label><input type="checkbox" checked={checked} disabled={disabled} onChange={event => onChange(event.target.checked ? Array.from(new Set([...selected, ...items.map(item => item.source_task_id)])) : selected.filter(id => !items.some(item => item.source_task_id === id)))} />{friendlyDate(date)}</label></legend>
          {items.map(item => <label className={`recovery-scope-task ${selectedSet.has(item.source_task_id) ? 'selected' : ''} ${disabled ? 'disabled' : ''}`} key={item.source_task_id}><input type="checkbox" checked={selectedSet.has(item.source_task_id)} disabled={disabled} onChange={event => onChange(event.target.checked ? [...selected, item.source_task_id] : selected.filter(id => id !== item.source_task_id))} /><span><strong>{item.title}</strong><small>预计 {item.minutes} 分钟 · 剩余估时 {item.remaining_minutes} 分钟</small>{selectedSet.has(item.source_task_id) && item.prerequisite_task_ids.some(id => !selectedSet.has(id)) && <small className="recovery-scope-warning">前置积压尚未纳入，请一并选择或先完成前置任务。</small>}</span><small className="recovery-scope-state">{selectedSet.has(item.source_task_id) ? '已选' : '留待处理'}</small>{item.can_split && selectedSet.has(item.source_task_id) && onSplit && <button className="button recovery-scope-split" type="button" disabled={disabled} onClick={(event) => { event.preventDefault(); event.stopPropagation(); onSplit(item.source_task_id) }}>拆分</button>}</label>)}
        </fieldset>
      })}</div>
    </details>
    {deferred.length > 0 && <p className="recovery-scope-deferred">未选任务仍保留原日期、进度与完成要求，可以稍后继续恢复。</p>}
    {dirty && <p role="status">恢复范围已修改，需要重新预览后才能确认。</p>}
    <button className="button" type="button" disabled={disabled || selected.length === 0} onClick={onPreview}>{disabled ? '正在重新预览…' : '按所选范围重新预览'}</button>
    {selected.length === 0 && <small>请至少选择一项任务；暂不恢复可以返回调整方式。</small>}
  </section>
}
