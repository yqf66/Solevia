import { Check } from 'lucide-react'
import { useEffect, useState } from 'react'
import type { ReactNode } from 'react'
import './ConversationPresentation.css'

export type ProcessStep = { label: string; detail?: string }

export function ProcessDetails({ title, active = false, attention = false, steps = [], children }: {
  title: string; active?: boolean; attention?: boolean; steps?: ProcessStep[]; children?: ReactNode
}) {
  return <details className={`process-details${active ? ' is-active' : ''}`} open={active || attention || undefined}>
    <summary><span className={active ? 'process-indicator' : ''} aria-hidden="true">{active ? '◌' : <Check size={13} />}</span>{title}</summary>
    <div className="process-details-body">
      {steps.length > 0 && <ol>{steps.map((step, index) => <li key={`${step.label}-${index}`}><span>{step.label}</span>{step.detail && <small>{step.detail}</small>}</li>)}</ol>}
      {children}
    </div>
  </details>
}

// Log observed states only. A synchronous endpoint supplies one honest waiting
// state; it does not pretend timed messages are server milestones.
export function OperationProgress({ active, label, scope, detail, error }: {
  active: boolean; label: string; scope?: string; detail?: string; error?: string
}) {
  const [log, setLog] = useState<{ scope?: string; started: number; seconds: number; steps: ProcessStep[]; status: 'active' | 'done' | 'error' } | null>(null)
  useEffect(() => {
    setLog((previous) => {
      if (active) {
        if (!previous || previous.scope !== scope || previous.status !== 'active') return { scope, started: Date.now(), seconds: 0, steps: [{ label, detail }], status: 'active' }
        const last = previous.steps.at(-1)
        return last?.label === label && last.detail === detail ? previous : { ...previous, steps: [...previous.steps.slice(-19), { label, detail }] }
      }
      if (!previous || previous.scope !== scope) return null
      if (previous.status !== 'active') return error && previous.status !== 'error' ? { ...previous, status: 'error' } : previous
      return { ...previous, seconds: Math.round((Date.now() - previous.started) / 1000), status: error ? 'error' : 'done' }
    })
  }, [active, label, detail, scope, error])
  useEffect(() => {
    if (!active) return
    const timer = window.setInterval(() => setLog((previous) => previous ? { ...previous, seconds: Math.floor((Date.now() - previous.started) / 1000) } : previous), 1000)
    return () => window.clearInterval(timer)
  }, [active])
  if (!log || log.scope !== scope) return null
  const running = active || log.status === 'active'
  return <ProcessDetails active={running} attention={log.status === 'error'} title={running ? `${label} · ${log.seconds} 秒` : log.status === 'error' ? '处理未完成' : `处理完成 · ${log.seconds} 秒`} steps={log.steps}>
    {running && <p role="status">{detail || '正在等待处理结果…'}</p>}
    {!running && error && <p className="inline-error">{error}</p>}
  </ProcessDetails>
}
