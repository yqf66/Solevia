import type { ResearchSummary } from '../api'

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

export function ResearchSources({ research }: { research: ResearchSummary | null }) {
  if (!research || research.sources.length === 0) return null
  return (
    <details className="research-sources-panel">
      <summary>
        来源 · {research.sources.length} 项 · {statusLabel(research.status)}
      </summary>
      <p className="research-source-note">检索问题：{research.question}</p>
      {research.stop_reason && <p className="research-source-note">停止原因：{research.stop_reason}</p>}
      {research.error_message && <p className="research-source-note">研究提示：{research.error_message}</p>}
      <div className="research-source-list">
        {research.sources.map((source, index) => (
          <details className="research-source-item" key={`${source.url}-${index}`}>
            <summary>
              <span>{source.title || source.url}</span>
              <small>{evidenceLabel(source.evidence_level)} · {source.read_status || '已收集'}</small>
            </summary>
            <a href={source.url} target="_blank" rel="noreferrer">打开来源</a>
            {source.excerpt && <p>{source.excerpt}</p>}
            {!source.excerpt && source.snippet && <p>{source.snippet}</p>}
          </details>
        ))}
      </div>
      {research.unresolved_points.length > 0 && (
        <p className="research-source-note">仍有 {research.unresolved_points.length} 个待核实点，后续对话会继续沿用已收集资料。</p>
      )}
    </details>
  )
}
