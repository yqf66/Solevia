import type { ResearchSummary } from '../api'
import type { useResearchResponse } from './useResearchResponse'
import { ResearchProgress } from './ResearchProgress'
import { ResearchSources } from './ResearchSources'

export function AnswerResearch({ message, summary, research }: {
  message: { id: string; role: string; metadata?: Record<string, unknown> }
  summary: ResearchSummary | null
  research: ReturnType<typeof useResearchResponse>
}) {
  if (message.role !== 'ASSISTANT') return null
  const ownsRequest = research.current?.response_message_id === message.id
  const ownsSources = summary && (message.metadata?.research_run_id === summary.id || (ownsRequest && research.current?.run.id === summary.id))
  if (!ownsRequest && !ownsSources) return null
  return <div className="research-answer-extras">
    {ownsRequest && <ResearchProgress research={research} completedOnly />}
    {ownsSources && <ResearchSources research={summary} />}
  </div>
}
