import { RefreshCw, X } from 'lucide-react'
import type { useResearchResponse } from './useResearchResponse'
import { ProcessDetails } from './ProcessDetails'
import './ResearchProgress.css'

const stages: Record<string, string> = {
  CREATED: '正在结合上下文', QUEUED: '正在结合上下文', DECIDING: '正在结合上下文', PLANNING: '准备查询',
  ANALYZING: '正在结合上下文', PLANNED: '准备查询', SEARCHED: '搜索资料', READ: '读取来源', REFLECTING: '整理证据',
  SEARCHING: '搜索资料', READING: '读取来源', SYNTHESIZING: '整理证据', COMPLETED: '研究完成',
  READY: '研究完成', PARTIAL: '取得部分证据', FAILED: '研究失败', INTERRUPTED: '研究中断',
  ANSWERING: '正在结合资料回答', QUEUED_ANSWER: '等待生成回答', CANCELLED: '研究已取消', CANCEL_REQUESTED: '正在取消', SKIPPED: '本次无需检索', DISABLED: '未执行检索',
}

export function ResearchProgress({ research, completedOnly = false }: { research: ReturnType<typeof useResearchResponse>; completedOnly?: boolean }) {
  const { current, error, acting, paused, preparing, readiness } = research
  if (preparing && !completedOnly) return <ProcessDetails title="正在准备联网服务…" active>
    <p role="status">确认搜索、网页读取{readiness?.components?.bge ? '和 BGE 排序' : ''}可用后开始。</p>
  </ProcessDetails>
  if (!current) return !completedOnly && error ? <p className="inline-error" role="status">{error}</p> : null
  const completed = current.status === 'COMPLETED'
  if (completed !== completedOnly) return null
  const run = current.run
  const coverage = (run.result?.coverage ?? run.checkpoint.coverage) as Record<string, number> | undefined
  const hasRetrieval = Number(coverage?.searched_queries ?? 0) > 0
    || Number(coverage?.read_pages ?? 0) > 0 || run.evidence.length > 0
  // An unused gate (including a failed one) is not a source or a search report.
  // Keep its diagnostics on the server; show actionable failures while waiting.
  if (completed && !hasRetrieval) return null
  const answering = current.status === 'ANSWERING' || current.status === 'QUEUED_ANSWER'
  const waiting = current.status === 'WAITING_RESEARCH'
  const label = current.status === 'CANCELLED' ? '研究已取消'
    : run.status === 'CANCEL_REQUESTED' && waiting ? '正在取消'
      : completed ? '检索过程'
    : answering ? (current.fallback ? '正在整理普通回答' : hasRetrieval ? '正在结合资料回答' : '正在整理回答')
      : current.status === 'ANSWER_FAILED' || current.status === 'INTERRUPTED' ? '回答未完成'
        : current.status === 'AWAITING_DECISION' ? '研究需要处理'
          : waiting && ['SKIPPED', 'DISABLED'].includes(run.status) ? '正在准备回答'
            : waiting && run.status === 'FAILED' && !hasRetrieval ? '正在准备回答'
              : stages[run.current_stage] ?? stages[run.status] ?? '正在研究'
  return <ProcessDetails title={label} active={waiting || answering} attention={!completed && !waiting && !answering}
    steps={research.observations.map((item) => ({ label: ['ANSWERING', 'QUEUED_ANSWER'].includes(item.stage) && !hasRetrieval ? '正在整理回答' : stages[item.stage] ?? '正在处理', detail: item.queries || item.pages || item.sources ? `已查询 ${item.queries} 次 · 已读取 ${item.pages} 页 · ${item.sources} 个来源` : undefined }))}>
    {hasRetrieval && <small>已完成查询 {Number(coverage?.searched_queries ?? 0)} · 已读取 {Number(coverage?.read_pages ?? 0)} 页 · 收集 {run.evidence.length} 个来源</small>}
    {run.status === 'PARTIAL' && <p>取得部分资料</p>}
    {current.fallback && <p>本次使用普通回答。</p>}
    {(error || current.error_message) && <p className="inline-error">{error || current.error_message}</p>}
    {paused && <p>等待较久，研究仍保存在后台，可刷新查看。</p>}
    {!completed && <div className="research-progress-actions">
      <button type="button" title="刷新研究状态" aria-label="刷新研究状态" disabled={acting} onClick={research.refresh}><RefreshCw size={15} /></button>
      {waiting && <button type="button" title="取消研究" aria-label="取消研究" disabled={acting} onClick={() => void research.act('cancel')}><X size={16} /></button>}
      {(waiting || ['AWAITING_DECISION', 'CANCELLED'].includes(current.status)) && <button className="button" type="button" disabled={acting} onClick={() => void research.act('ordinary')}>继续普通回答</button>}
      {['AWAITING_DECISION', 'CANCELLED'].includes(current.status) && <button className="button" type="button" disabled={acting} onClick={() => void research.act('retry-research')}>重试研究</button>}
      {['ANSWER_FAILED', 'INTERRUPTED'].includes(current.status) && <button className="button" type="button" disabled={acting} onClick={() => void research.act('retry-answer')}>重试回答</button>}
    </div>}
  </ProcessDetails>
}
