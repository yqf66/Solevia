import type { ClarificationWorkspace, RequirementFact } from '../api'
import { factLabels } from '../ui/plannerPresentation'

type Props = {
  workspace: ClarificationWorkspace
  latestAnalysisJob: NonNullable<ClarificationWorkspace['analysis_jobs']>[number] | undefined
  showRequirementReview: boolean
  isPreparingReview: boolean
  analysisPending: boolean
  clarifyingFactId: string | null
  pendingFactId: string | null
  isRetryingAnalysis: boolean
  onPrepareReview: () => void
  onConfirmFact: (id: string) => void
  onClarifyFact: (fact: RequirementFact) => void
  onCancelClarification: () => void
  onRetryAnalysis: () => void
}

export function ClarificationNotebook({ workspace, latestAnalysisJob, showRequirementReview, isPreparingReview,
  analysisPending, clarifyingFactId, pendingFactId, isRetryingAnalysis, onPrepareReview, onConfirmFact,
  onClarifyFact, onCancelClarification, onRetryAnalysis }: Props) {
  const confirmedFacts = workspace.facts.filter(fact => fact.status === 'CONFIRMED')
  const proposedFacts = workspace.facts.filter(fact => fact.status === 'PROPOSED')
  const recentChanges = (workspace.fact_changes ?? []).filter(change => change.operation !== 'ADD').slice(0, 5)
  return <>
          <div className={`panel readiness-panel ${workspace.readiness.ready_for_review ? 'ready' : ''}`}>
            <span className="memory-kicker">当前判断</span>
            <h2>{workspace.readiness.ready_for_review ? '信息已明确，可以进行下一步' : '继续自然讨论就好'}</h2>
            <p className="muted">{workspace.readiness.reason}</p>
            {workspace.memory?.current_topic && !workspace.readiness.ready_for_review && (
              <p className="current-topic">目前在聊：{workspace.memory.current_topic}</p>
            )}
            {workspace.readiness.ready_for_review && !showRequirementReview && (
              <button className="button primary full" type="button" disabled={isPreparingReview || analysisPending} onClick={onPrepareReview}>
                {isPreparingReview ? '正在整理…' : '进入下一步，先核对理解 →'}
              </button>
            )}
          </div>
          <div className="panel">
            <div className="memory-panel-heading">
              <div><span className="memory-kicker">持续更新</span><h2>已经明确的信息</h2></div>
              <strong>{confirmedFacts.length}</strong>
            </div>
            <div className="facts-preview">
              {confirmedFacts.map((fact) => (
                <div key={fact.id}>
                  <span>{factLabels[fact.field_key] ?? fact.field_key}</span>
                  <p>{String(fact.value)}</p>
                </div>
              ))}
              {!confirmedFacts.length && <p className="muted">先聊聊你的想法，我会在这里逐渐整理。</p>}
            </div>
          </div>
          {proposedFacts.length > 0 && (
            <div className="panel memory-candidates">
              <h2>待你确认的理解</h2>
              <p className="muted small">这些还不会写进最终目标。如果不准确，可以带着原内容继续和我澄清。</p>
              <div className="facts-preview">
                {proposedFacts.map((fact) => {
                  const isClarifying = clarifyingFactId === fact.id
                  return (
                  <div className={isClarifying ? 'fact-under-clarification' : ''} key={fact.id}>
                    <span>{factLabels[fact.field_key] ?? fact.field_key}</span>
                    <p>{String(fact.value)}</p>
                    {isClarifying ? (
                      <div className="memory-clarification-state" role="status">
                        <span>原内容已带入对话框，补充后发送即可。</span>
                        <button type="button" onClick={onCancelClarification}>取消修改</button>
                      </div>
                    ) : (
                      <div className="memory-inline-actions">
                        <button type="button" disabled={pendingFactId === fact.id} onClick={() => onConfirmFact(fact.id)}>对，就是这样</button>
                        <button type="button" disabled={pendingFactId === fact.id} onClick={() => onClarifyFact(fact)}>修改这条理解</button>
                      </div>
                    )}
                  </div>
                  )
                })}
              </div>
            </div>
          )}
          {latestAnalysisJob && latestAnalysisJob.status !== 'SUCCEEDED' && (latestAnalysisJob.status !== 'FAILED' ? (
            <p className="muted" role="status">正在后台更新讨论记录，可以继续聊天。</p>
          ) : (
            <div className="panel analysis-status-panel" role="status">
              <span className="memory-kicker">后台记忆整理</span>
              <h2>这轮记忆还没有整理完成</h2>
              <p className="muted">已自动尝试 {latestAnalysisJob.attempts} 次，聊天内容不会丢失。</p>
              <button className="button" type="button" disabled={isRetryingAnalysis} onClick={onRetryAnalysis}>
                {isRetryingAnalysis ? '正在重新整理…' : '重新整理记忆'}
              </button>
            </div>
          ))}
          {workspace.open_questions.length > 0 && !workspace.readiness.ready_for_review && (
            <details className="panel memory-details">
              <summary>仍可继续聊的方向</summary>
              <ul>{workspace.open_questions.map((question) => <li key={question}>{question}</li>)}</ul>
            </details>
          )}
          {recentChanges.length > 0 && (
            <details className="panel memory-details">
              <summary>最近改变的想法</summary>
              <ul>
                {recentChanges.map((change) => (
                  <li key={change.id}>
                    <strong>{factLabels[change.field_key] ?? change.field_key}</strong>
                    {change.operation === 'RETRACT' ? ' 已撤回' : ' 已更新'}
                    {change.reason ? `：${change.reason}` : ''}
                  </li>
                ))}
              </ul>
            </details>
          )}
  </>
}
