import { useEffect, useState } from 'react'
import { request } from '../api/client'
import { useKernelSelection } from './KernelSelection'

type Status = {
  connected: boolean
  queued_count: number
  active_count: number
  requests: Array<{ stage: string; last_activity_seconds: number; reasoning_effort: string; wait_seconds: number }>
  plan_checkpoint?: { status: string; completed_weeks: number; total_weeks: number | null }
}

export function CodexGenerationStatus({ active, scope }: { active: boolean; scope?: string }) {
  const enabled = useKernelSelection().mode === 'codex'
  const [status, setStatus] = useState<Status | null>(null)
  const [unavailable, setUnavailable] = useState(false)
  useEffect(() => {
    if (!active || !enabled) return
    let stopped = false
    let timer: ReturnType<typeof setTimeout>
    setStatus(null)
    const goal = scope?.match(/^[a-f0-9-]{36}(?=:|$)/)?.[0]
    async function poll() {
      try {
        const value = await request<Status>(`/api/codex/generation-status${goal ? `?goal_id=${encodeURIComponent(goal)}` : ''}`)
        if (!stopped) { setStatus(value); setUnavailable(false) }
      } catch { if (!stopped) setUnavailable(true) }
      if (!stopped) timer = setTimeout(() => void poll(), 5000)
    }
    void poll()
    return () => { stopped = true; clearTimeout(timer) }
  }, [active, scope, enabled])
  if (!active || !enabled) return null
  const checkpoint = status?.plan_checkpoint
  return <div className="codex-generation-status" role="status" aria-live="polite">
    <p>{unavailable ? '暂时无法取得后台生成状态，尚未确认完成，请勿重复提交。'
      : !status ? '正在连接后台，查询生成状态…'
      : !status.connected ? 'Codex 连接已断开，正在等待本次请求返回错误信息。'
      : status.active_count ? `Codex 已连接，当前正在处理 ${status.active_count} 个生成请求${status.queued_count ? `，另有 ${status.queued_count} 个排队` : ''}。`
      : status.queued_count ? 'Codex 已连接，请求正在排队。'
      : '后台已连接，正在等待本次处理结果。'}</p>
    {!unavailable && status?.connected && status.requests.some(row => row.last_activity_seconds > 90) && <p>生成尚未结束，暂未收到新的阶段事件。复杂规划可能需要较长时间，请继续等待。</p>}
    {checkpoint?.status === 'RUNNING' && checkpoint.total_weeks != null && <p>本规划已校验 {checkpoint.completed_weeks} / {checkpoint.total_weeks} 周；通过的段落已保存。</p>}
  </div>
}
