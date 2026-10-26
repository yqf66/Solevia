import { useEffect, useId, useLayoutEffect, useRef, useState, useSyncExternalStore } from 'react'
import { createPortal } from 'react-dom'
import { ChevronDown } from 'lucide-react'
import { request } from '../api/client'
import './CodexModelSettings.css'
import { useKernelSelection } from './KernelSelection'
import { ApiComposerSettings, useApiSelectionPending } from './ApiModelSettings'

type Model = { id: string; display_name: string; supported_efforts: string[]; default_effort: string }
type Preferences = {
  model: string; reasoning_effort: string; models: Model[]; supported_efforts: string[]
  wait_seconds: number; wait_seconds_by_effort: Record<string, number>
}
const labels: Record<string, string> = { low: '低', medium: '中', high: '高', xhigh: '很高', max: '最高', ultra: '极高' }
let snapshot: { preferences: Preferences | null; pending: boolean; error: string } = { preferences: null, pending: false, error: '' }
const listeners = new Set<() => void>()
let loadedAt = 0
let loading: Promise<void> | undefined
const subscribe = (listener: () => void) => { listeners.add(listener); return () => { listeners.delete(listener) } }
function publish(value: Partial<typeof snapshot>) { snapshot = { ...snapshot, ...value }; listeners.forEach(listener => listener()) }
async function load() {
  if (loading) return loading
  if (snapshot.preferences && Date.now() - loadedAt < 30_000) return
  loading = request<Preferences>('/api/codex/preferences').then(preferences => {
    loadedAt = Date.now(); publish({ preferences, error: '' })
  }).catch(error => publish({ error: error instanceof Error ? error.message : '暂时无法读取模型目录' }))
    .finally(() => { loading = undefined })
  return loading
}
export function useCodexSelectionPending() {
  const apiPending = useApiSelectionPending()
  return useSyncExternalStore(subscribe, () => snapshot.pending) || apiPending
}

export function CodexComposerSettings({ disabled = false }: { disabled?: boolean }) {
  const state = useSyncExternalStore(subscribe, () => snapshot)
  const [open, setOpen] = useState(false)
  const [model, setModel] = useState('')
  const [effort, setEffort] = useState('')
  const [position, setPosition] = useState({ left: 8, top: 8 })
  const trigger = useRef<HTMLButtonElement>(null)
  const panel = useRef<HTMLDivElement>(null)
  const id = useId()
  const enabled = useKernelSelection().mode === 'codex'
  useEffect(() => { if (enabled) void load() }, [enabled])
  useEffect(() => {
    if (open && !model && state.preferences) {
      setModel(state.preferences.model); setEffort(state.preferences.reasoning_effort)
    }
  }, [open, model, state.preferences])
  useLayoutEffect(() => {
    if (!open) return
    const anchor = trigger.current
    function place() {
      const rect = anchor?.getBoundingClientRect()
      if (!rect) return
      const width = Math.min(316, window.innerWidth - 16)
      const height = panel.current?.offsetHeight ?? 310
      setPosition({ left: Math.max(8, Math.min(rect.left, window.innerWidth - width - 8)),
        top: rect.top > height + 16 ? rect.top - height - 8 : Math.max(8, Math.min(rect.bottom + 8, window.innerHeight - height - 8)) })
    }
    place(); panel.current?.querySelector('select')?.focus()
    const resize = typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(place)
    if (panel.current) resize?.observe(panel.current)
    function outside(event: PointerEvent) {
      if (!panel.current?.contains(event.target as Node) && !anchor?.contains(event.target as Node) && !snapshot.pending) setOpen(false)
    }
    function keyboard(event: KeyboardEvent) {
      if (event.key === 'Escape' && !snapshot.pending) { setOpen(false); anchor?.focus() }
    }
    document.addEventListener('pointerdown', outside); document.addEventListener('keydown', keyboard)
    window.addEventListener('resize', place); window.addEventListener('scroll', place, true)
    return () => {
      document.removeEventListener('pointerdown', outside); document.removeEventListener('keydown', keyboard)
      window.removeEventListener('resize', place); window.removeEventListener('scroll', place, true)
      resize?.disconnect()
    }
  }, [open, state.error])
  if (!enabled) return <ApiComposerSettings disabled={disabled} />
  const preferences = state.preferences
  const target = preferences?.models.find(item => item.id === model)
  function show() {
    setModel(preferences?.model ?? ''); setEffort(preferences?.reasoning_effort ?? '')
    setOpen(value => !value); void load()
  }
  async function apply() {
    if (state.pending || !target?.supported_efforts.includes(effort)) return
    publish({ pending: true, error: '' })
    try {
      const saved = await request<Preferences>('/api/codex/preferences', { method: 'PUT', body: JSON.stringify({ model, reasoning_effort: effort }) })
      loadedAt = Date.now(); publish({ preferences: saved }); setOpen(false); trigger.current?.focus()
    } catch (error) { publish({ error: error instanceof Error ? error.message : '模型设置未保存，请重试' }) }
    finally { publish({ pending: false }) }
  }
  return <div className="codex-composer-settings">
    <button ref={trigger} type="button" className="codex-model-trigger" aria-label="选择 Codex 模型与思考强度" aria-haspopup="dialog" aria-expanded={open} aria-controls={open ? id : undefined}
      disabled={disabled || state.pending} onClick={show} title={preferences ? `${preferences.model} · ${labels[preferences.reasoning_effort]}思考` : '选择 Codex 模型'}>
      <span>{preferences ? `${preferences.model} · ${labels[preferences.reasoning_effort] ?? preferences.reasoning_effort}` : 'Codex 模型'}</span><ChevronDown size={13} />
    </button>
    {open && createPortal(<div id={id} ref={panel} className="codex-model-menu" role="dialog" aria-label="Codex 模型设置" style={position}>
      <strong>模型与思考强度</strong>
      {preferences ? <>
        <label>模型<select aria-label="Codex 模型" value={model} disabled={state.pending || disabled} onChange={event => {
          const next = preferences.models.find(item => item.id === event.target.value)
          setModel(event.target.value); setEffort(next?.supported_efforts.includes(effort) ? effort : next?.default_effort ?? '')
        }}>
          {!preferences.models.some(item => item.id === model) && <option value={model}>请选择可用模型</option>}
          {preferences.models.map(item => <option key={item.id} value={item.id}>{item.display_name}</option>)}
        </select></label>
        <label>思考强度<select aria-label="Codex 思考强度" value={effort} disabled={state.pending || disabled || !target} onChange={event => setEffort(event.target.value)}>
          {!target?.supported_efforts.includes(effort) && <option value={effort}>请选择支持的强度</option>}
          {target?.supported_efforts.map(value => <option key={value} value={value}>{labels[value] ?? value}</option>)}
        </select></label>
        <p>每次生成最多等待 {(preferences.wait_seconds_by_effort[effort] ?? preferences.wait_seconds) / 60} 分钟。多周规划分段生成，整体可能更久。</p>
        <p>应用后用于新发起的模型调用；已发出的调用沿用原设置。模型是否可调用仍取决于账号权限。</p>
        <footer><button type="button" disabled={state.pending} onClick={() => { setOpen(false); trigger.current?.focus() }}>取消</button>
          <button type="button" disabled={state.pending || disabled || !target?.supported_efforts.includes(effort)} onClick={() => void apply()}>{state.pending ? '应用中…' : '应用'}</button></footer>
      </> : <button type="button" onClick={() => void load()}>重新读取模型目录</button>}
      {state.error && <p role="alert">{state.error}</p>}
    </div>, document.body)}
  </div>
}
