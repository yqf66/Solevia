import { usesCodexKernel } from '../api/kernelMode'
import { useEffect, useRef, useState } from 'react'
import { actResearchResponse, createResearchResponse, getResearchResponse, getResearchReadiness, listResearchResponses, type ResearchReadiness, type ResearchResponsePhase, type ResearchResponseRequest } from '../api'

const activeStatuses = new Set(['WAITING_RESEARCH', 'QUEUED_ANSWER', 'ANSWERING'])

export function useResearchResponse<T>(
  goalId: string | undefined,
  phases: ResearchResponsePhase[],
  loadWorkspace: (goalId: string) => Promise<T>,
  onWorkspace: (workspace: T) => void,
) {
  const [current, setCurrent] = useState<ResearchResponseRequest | null>(null)
  const [error, setError] = useState('')
  const [acting, setActing] = useState(false)
  const [preparing, setPreparing] = useState(false)
  const [readiness, setReadiness] = useState<ResearchReadiness | null>(null)
  const [paused, setPaused] = useState(false)
  const [pollKey, setPollKey] = useState(0)
  const [observations, setObservations] = useState<Array<{ stage: string; queries: number; pages: number; sources: number }>>([])
  const observedRequest = useRef('')
  const callbacks = useRef({ loadWorkspace, onWorkspace })
  const goalRef = useRef(goalId)
  const delivered = useRef('')
  const mutation = useRef(0)
  const outbox = useRef<{ fingerprint: string; id: string } | null>(null)
  const key = phases.join(',')
  useEffect(() => {
    callbacks.current = { loadWorkspace, onWorkspace }
    goalRef.current = goalId
  }, [goalId, loadWorkspace, onWorkspace])
  const requestId = current?.id
  const requestGoal = current?.goal_id
  const requestStatus = current?.status
  const attemptCount = current?.attempt_count

  useEffect(() => {
    if (!current) { setObservations([]); observedRequest.current = ''; return }
    const coverage = (current.run.result?.coverage ?? current.run.checkpoint.coverage) as Record<string, number> | undefined
    const observation = { stage: ['ANSWERING', 'QUEUED_ANSWER', 'COMPLETED', 'CANCELLED', 'INTERRUPTED'].includes(current.status) ? current.status
      : ['SKIPPED', 'DISABLED'].includes(current.run.status) ? current.run.status : current.run.current_stage,
      queries: Number(coverage?.searched_queries ?? 0), pages: Number(coverage?.read_pages ?? 0), sources: current.run.evidence.length }
    const key = current.id
    const changed = observedRequest.current !== key
    observedRequest.current = key
    setObservations((previous) => changed ? [observation] : JSON.stringify(previous.at(-1)) === JSON.stringify(observation) ? previous : [...previous.slice(-29), observation])
  }, [current])

  useEffect(() => {
    let active = true
    setCurrent(null)
    setError('')
    setActing(false)
    setPreparing(false)
    setReadiness(null)
    setPaused(false)
    delivered.current = ''
    outbox.current = null
    const revision = ++mutation.current
    if (goalId) {
      listResearchResponses(goalId).then((rows) => {
        if (active && revision === mutation.current) setCurrent(rows.find((row) => key.split(',').includes(row.phase)) ?? null)
      }).catch((caught) => { if (active && revision === mutation.current) setError(caught instanceof Error ? caught.message : '研究状态读取失败') })
    }
    return () => { active = false; ++mutation.current }
  }, [goalId, key])

  useEffect(() => {
    if (!goalId || !requestId || requestGoal !== goalId || requestStatus !== 'COMPLETED') return
    const token = `${requestId}:${attemptCount}`
    if (delivered.current === token) return
    let active = true
    callbacks.current.loadWorkspace(goalId).then((workspace) => {
      if (active && goalRef.current === goalId) {
        delivered.current = token
        callbacks.current.onWorkspace(workspace)
      }
    }).catch((caught) => { if (active) setError(caught instanceof Error ? caught.message : '回答读取失败，可刷新重试') })
    return () => { active = false }
  }, [goalId, requestId, attemptCount, requestStatus, requestGoal, pollKey])

  useEffect(() => {
    if (!goalId || !requestId || requestGoal !== goalId || !requestStatus || !activeStatuses.has(requestStatus)) return
    let active = true
    const deadline = Date.now() + (usesCodexKernel() ? 6 * 60 * 60 * 1000 : 15 * 60 * 1000)
    let timer: ReturnType<typeof setTimeout>
    async function poll() {
      const revision = mutation.current
      try {
        const next = await getResearchResponse(goalId!, requestId!)
        if (!active || revision !== mutation.current) return
        setCurrent(next)
        setError('')
        if (!activeStatuses.has(next.status)) return
      } catch (caught) {
        if (!active || revision !== mutation.current) return
        setError(caught instanceof Error ? caught.message : '连接暂时中断，研究记录仍已保存')
      }
      if (Date.now() >= deadline) { setPaused(true); return }
      if (active) timer = setTimeout(() => void poll(), 1500)
    }
    timer = setTimeout(() => void poll(), 800)
    return () => { active = false; clearTimeout(timer) }
  }, [goalId, requestId, requestStatus, requestGoal, pollKey])

  async function start(question: string, phase: ResearchResponsePhase, intent: ResearchResponseRequest['intent'], sources: string[] = []) {
    if (!goalId || acting || (current && activeStatuses.has(current.status))) return
    setActing(true)
    setError('')
    setPaused(false)
    const revision = ++mutation.current
    const fingerprint = JSON.stringify([goalId, question, phase, intent, sources])
    if (outbox.current?.fingerprint !== fingerprint) outbox.current = { fingerprint, id: crypto.randomUUID() }
    try {
      if (!usesCodexKernel()) {
      setPreparing(true)
      const deadline = Date.now() + 120000
      while (true) {
        let state: ResearchReadiness
        try { state = await getResearchReadiness() }
        catch { state = { ready: false, status: 'LOADING', components: {} } }
        if (goalRef.current !== goalId || revision !== mutation.current) return
        setReadiness(state)
        if (state.ready) break
        if (Date.now() >= deadline) throw new Error('联网组件尚未就绪，请稍后重试；问题内容仍保留。')
        await new Promise((resolve) => setTimeout(resolve, 1000))
        if (goalRef.current !== goalId || revision !== mutation.current) return
      }
      setPreparing(false)
      }
      const next = await createResearchResponse(goalId, {
        question, phase, intent, material_source_ids: sources, idempotency_key: outbox.current.id,
      })
      if (goalRef.current === goalId) { setCurrent(next); outbox.current = null }
    } catch (caught) {
      if (goalRef.current === goalId) setError(caught instanceof Error ? caught.message : '联网准备未完成，请重试')
      throw caught
    } finally {
      if (goalRef.current === goalId) {
        setPreparing(false)
        setActing(false)
        setPollKey((value) => value + 1)
      }
    }
  }

  async function act(action: Parameters<typeof actResearchResponse>[2]) {
    if (!goalId || !current || acting) return
    setActing(true)
    setError('')
    ++mutation.current
    try {
      const next = await actResearchResponse(goalId, current.id, action)
      if (goalRef.current === goalId) setCurrent(next)
    } catch (caught) {
      if (goalRef.current === goalId) setError(caught instanceof Error ? caught.message : '操作未完成')
    } finally {
      if (goalRef.current === goalId) {
        setActing(false)
        setPollKey((value) => value + 1)
      }
    }
  }

  return { current, error, acting, paused, preparing, readiness, observations, start, act,
    busy: acting || Boolean(current && activeStatuses.has(current.status)),
    refresh: () => { setPaused(false); setPollKey((value) => value + 1) },
  }
}
