import { useState } from 'react'
import { Bell, ChevronDown } from 'lucide-react'
import type { TodayWorkspace, ExecutionInactivity } from '../api'
import { friendlyDate } from '../ui/plannerPresentation'
import { DeferredRecoveryReminder } from './RecoveryControls'

const commitmentRiskLabels: Record<string, string> = {
  COMPLETE: '已达标',
  ON_TRACK: '进行中',
  DUE_SOON: '临近检查',
  AT_RISK: '需要处理',
  NO_DEADLINE: '待安排',
}

const commitmentCadenceLabels: Record<string, string> = {
  TOTAL: '总量',
  PER_WEEK: '每周',
  PER_EXECUTION_DAY: '每个执行日',
}


type Props = {
  workspace: Pick<TodayWorkspace, 'commitment_progress' | 'commitment_alerts'>; viewingToday: boolean; inactivity?: ExecutionInactivity | null
  reminderBusyLabel: string; reminderEventBusyId: string
  onRecovery: () => void; onDiscuss: (label: string) => void
  onAcknowledge: (id: string, action: 'READ' | 'DISMISS') => void; onStopReminder: (label: string) => void
}

export function ExecutionNoticeBar({ workspace, viewingToday, inactivity, reminderBusyLabel, reminderEventBusyId, onRecovery, onDiscuss, onAcknowledge, onStopReminder }: Props) {
  const [commitmentProgressOpen, setCommitmentProgressOpen] = useState(false)
  const toggleCommitmentProgress = () => setCommitmentProgressOpen((value) => !value)
  const commitmentProgress = workspace.commitment_progress ?? []
  const commitmentTarget = commitmentProgress.reduce((sum, item) => sum + Math.max(0, item.target_occurrences), 0)
  const commitmentCompleted = commitmentProgress.reduce((sum, item) => sum + Math.min(item.target_occurrences, Math.max(0, item.completed_occurrences)), 0)
  const commitmentRemaining = commitmentProgress.reduce((sum, item) => sum + Math.max(0, item.remaining_occurrences), 0)
  const commitmentPercentage = commitmentTarget > 0 ? Math.min(100, Math.round(commitmentCompleted / commitmentTarget * 100)) : 100
  const missed = viewingToday ? inactivity?.missed_execution_days ?? 0 : 0
  const alerts = workspace.commitment_alerts ?? []
  if (!missed && !alerts.length && !commitmentProgress.length) return null
  return <details className="execution-notice-bar">
    <summary><Bell size={16} aria-hidden="true" /><strong>提醒与进度</strong>
      <span className="execution-notice-counts">{missed > 0 && <span>{missed} 个执行日待处理</span>}{alerts.length > 0 && <span>{alerts.length} 项提醒</span>}{commitmentProgress.length > 0 && <span>周期进度 {commitmentCompleted}/{commitmentTarget} 次</span>}</span>
      <span className="execution-notice-expand">查看 <ChevronDown size={14} aria-hidden="true" /></span>
    </summary>
    <div className="execution-notice-content">
          {viewingToday && inactivity && inactivity.missed_execution_days > 0 && <DeferredRecoveryReminder inactivity={inactivity} onReview={() => onRecovery()} />}
          {workspace.commitment_alerts && workspace.commitment_alerts.length > 0 && <section className="commitment-alerts-card" aria-label="计划提醒"><header><div><p className="eyebrow">需要留意</p><h2>有些完成要求正在接近窗口</h2></div><small>系统不会自动修改你的安排</small></header><div className="commitment-alert-list">{workspace.commitment_alerts.map((alert) => <article className={`commitment-alert commitment-alert-${alert.severity.toLowerCase()}`} key={alert.event_id || alert.dedupe_key || `${alert.label}-${alert.risk}`}><div><strong>{alert.title}</strong><p>{alert.message}</p>{alert.status === 'READ' && <small>已读 · 状态仍在跟踪</small>}</div><button className="button" type="button" onClick={() => onDiscuss(alert.label)}>{alert.action === 'REVIEW_PLAN' ? '讨论并调整计划' : '去执行讨论'}</button>{alert.event_id && alert.status !== 'READ' && <button className="button quiet" type="button" disabled={reminderEventBusyId === alert.event_id} onClick={() => onAcknowledge(alert.event_id!, 'READ')}>{reminderEventBusyId === alert.event_id ? '保存中…' : '标记已读'}</button>}{alert.event_id && <button className="button quiet" type="button" disabled={reminderEventBusyId === alert.event_id} onClick={() => onAcknowledge(alert.event_id!, 'DISMISS')}>{reminderEventBusyId === alert.event_id ? '保存中…' : '关闭这次提醒'}</button>}<button className="button quiet" type="button" disabled={reminderBusyLabel === alert.label} onClick={() => onStopReminder(alert.label)}>{reminderBusyLabel === alert.label ? '保存中…' : '停止此项提醒'}</button></article>)}</div></section>}
          {commitmentProgress.length > 0 && <section className={`commitment-progress-card${commitmentProgressOpen ? '' : ' commitment-progress-collapsed'}`} aria-label="计划承诺进度">
            <header>
              <button className="commitment-progress-toggle" type="button" aria-expanded={commitmentProgressOpen} aria-label={commitmentProgressOpen ? '收起本周期完成要求' : '展开本周期完成要求'} onClick={toggleCommitmentProgress}>
                <span><strong>周期目标进度</strong></span>
                <i aria-hidden="true">{commitmentProgressOpen ? '⌃' : '⌄'}</i>
              </button>
              <small>按周或阶段累计，完成任务自动计入</small>
            </header>
            {!commitmentProgressOpen && <div className="commitment-progress-compact-summary"><div><span>总体进度</span><strong>{commitmentCompleted} / {commitmentTarget} 次</strong><em>{commitmentPercentage}%</em></div><div className="commitment-progress-bar"><i style={{ width: `${commitmentPercentage}%` }} /></div><small>{commitmentRemaining > 0 ? `还需 ${commitmentRemaining} 次` : '已满足全部完成要求'}</small></div>}
            {commitmentProgressOpen && <div className="commitment-progress-list">{commitmentProgress.map((item) => { const percentage = item.target_occurrences > 0 ? Math.min(100, Math.round(item.completed_occurrences / item.target_occurrences * 100)) : 100; const period = item.period_start && item.period_end ? `${friendlyDate(item.period_start)}–${friendlyDate(item.period_end)}` : '尚未生成执行日期'; return <article className={`commitment-progress-item commitment-risk-${item.risk.toLowerCase()}`} key={`${item.label}-${item.cadence}`}><div className="commitment-progress-heading"><strong>{item.label}</strong><span>{commitmentCadenceLabels[item.cadence] ?? item.cadence}</span><em>{commitmentRiskLabels[item.risk] ?? item.risk}</em></div><div className="commitment-progress-bar"><i style={{ width: `${percentage}%` }} /></div><div className="commitment-progress-meta"><span>{item.completed_occurrences} / {item.target_occurrences} 次</span><span>{item.remaining_occurrences > 0 ? `还需 ${item.remaining_occurrences} 次` : '已满足最低要求'}</span><small>{item.completed_minutes > 0 || item.partial_minutes > 0 ? `已投入 ${item.completed_minutes + item.partial_minutes} 分钟${item.remaining_minutes > 0 ? ` · 预计还需 ${item.remaining_minutes} 分钟` : ''}` : period}</small></div>{(item.completed_minutes > 0 || item.partial_minutes > 0) && <small className="commitment-progress-period">{period}</small>}</article> })}</div>}
          </section>}

    </div>
  </details>
}
