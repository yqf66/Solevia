import type { ResearchSummary } from '../api'
import { codexFailureForCode } from './codexFailures'
import { ConnectionAwareError } from './ConnectionAwareError'

function evidenceLabel(level: string): string {
  if (level === 'DIRECT_EVIDENCE') return '正文证据'
  if (level === 'FULL_TEXT') return '正文读取'
  if (level === 'SEARCH_SNIPPET') return '搜索摘要'
  return level || '材料'
}

function statusLabel(status: string): string {
  if (status === 'READY') return '已完成'
  if (status === 'PARTIAL') return '部分完成'
  if (status === 'FAILED') return '读取失败'
  if (status === 'SKIPPED') return '未执行'
  if (status === 'DISABLED') return '未开启'
  return status || '处理中'
}

function stopLabel(reason: string): string {
  const failure = codexFailureForCode(reason)
  if (failure) return failure.message
  const labels: Record<string, string> = {
    CODEX_EVIDENCE_VERIFIED: '本轮所需的正文证据已核验',
    CODEX_EVIDENCE_REVIEW_REQUIRED: '已保留收集的资料，仍有证据需要核实',
    CODEX_RESEARCH_INCOMPLETE: '本轮检索未完成，可重试；现有资料已保留',
    CODEX_RESEARCH_BUDGET_EXHAUSTED: '已达到本轮检索上限，可缩小问题后重试',
    CODEX_RESEARCH_READ_BUDGET_EXHAUSTED: '已达到本轮正文读取上限',
  }
  return labels[reason] ?? (/^[A-Z][A-Z0-9_]+$/.test(reason) ? '本轮检索已停止，请结合待核实点判断资料是否适用' : reason)
}

function readLabel(status: string | undefined): string {
  return ({ SUCCEEDED: '已读取', FAILED: '读取失败', PENDING: '尚未读取', SKIPPED: '未读取' } as Record<string, string>)[status ?? ''] ?? '已收集'
}

export function ResearchSources({ research }: { research: ResearchSummary | null }) {
  if (!research) return null
  if (research.sources.length === 0) {
    const failure = codexFailureForCode(research.stop_reason ?? undefined)
    return failure ? <ConnectionAwareError code={research.stop_reason ?? undefined} message={failure.message} /> : null
  }
  return (
    <details className="research-sources-panel">
      <summary>
        来源 · {research.sources.length} 项 · {statusLabel(research.status)}
      </summary>
      <p className="research-source-note">检索问题：{research.question}</p>
      {research.stop_reason && <p className="research-source-note">{stopLabel(research.stop_reason)}</p>}
      {research.error_message && <p className="research-source-note">研究提示：{research.error_message}</p>}
      <div className="research-source-list">
        {research.sources.map((source, index) => (
          <details className="research-source-item" key={`${source.url}-${index}`}>
            <summary>
              <span>{source.title || source.url}</span>
              <small>{evidenceLabel(source.evidence_level)} · {readLabel(source.read_status)}</small>
            </summary>
            <a href={source.url} target="_blank" rel="noreferrer">打开来源</a>
            {source.excerpt && <p>{source.excerpt}</p>}
            {!source.excerpt && source.snippet && <p>{source.snippet}</p>}
          </details>
        ))}
      </div>
      {research.unresolved_points.length > 0 && (
        <div className="research-source-note">
          <p>仍有 {research.unresolved_points.length} 个待核实点：</p>
          <ul>{research.unresolved_points.map((point, index) => <li key={index}>{point}</li>)}</ul>
          <p>后续对话会继续沿用已收集资料。</p>
        </div>
      )}
    </details>
  )
}
