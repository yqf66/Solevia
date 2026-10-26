import { useEffect, useRef, useState } from 'react'
import { getResearchReadiness, setResearchConnection, type ResearchReadiness } from '../api'
import { useKernelSelection } from '../codex/KernelSelection'

// A lease belongs to this mounted discussion, not the whole browser: another
// tab/goal can retain its own connection without being disabled by this one.
export function useWebResearch(goalId: string | undefined) {
  const native = useKernelSelection().mode === 'codex'
  const [enabled, setEnabled] = useState(false)
  const [readiness, setReadiness] = useState<ResearchReadiness | null>(null)
  const [error, setError] = useState('')
  const [elapsed, setElapsed] = useState(0)
  const [attempt, setAttempt] = useState(0)
  const clientId = useRef(crypto.randomUUID())
  const sequence = useRef(0)
  const everEnabled = useRef(false)

  useEffect(() => {
    if (native || !enabled || !goalId) return
    everEnabled.current = true
    let active = true
    let timer: ReturnType<typeof setTimeout>
    const started = Date.now()
    let renewedAt = 0
    setError('')
    setReadiness(null)
    setElapsed(0)
    async function poll() {
      try {
        const renew = !renewedAt || Date.now() - renewedAt >= 20000
        const state = renew
          ? await setResearchConnection(clientId.current, true, ++sequence.current)
          : await getResearchReadiness()
        if (!active) return
        if (renew) renewedAt = Date.now()
        setReadiness(state)
        setElapsed(Math.floor((Date.now() - started) / 1000))
        setError('')
        if (!state.ready && Date.now() - started >= 120000) {
          setError('联网服务尚未就绪，可以重试或关闭联网。')
          return
        }
        timer = setTimeout(() => void poll(), state.ready ? 20000 : 500)
      } catch {
        if (!active) return
        setElapsed(Math.floor((Date.now() - started) / 1000))
        if (Date.now() - started >= 120000) {
          setError('暂时无法连接联网服务，可以重试或关闭联网。')
          return
        }
        timer = setTimeout(() => void poll(), 1000)
      }
    }
    void poll()
    return () => {
      active = false
      clearTimeout(timer)
      void setResearchConnection(clientId.current, false, ++sequence.current).catch(() => undefined)
    }
  }, [native, enabled, goalId, attempt])

  return { available: !native, enabled: !native && enabled, setEnabled, readiness, error, elapsed, everEnabled: !native && everEnabled.current,
    retry: () => setAttempt((value) => value + 1) }
}
