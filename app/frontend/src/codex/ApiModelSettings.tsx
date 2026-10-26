import { useEffect, useId, useLayoutEffect, useRef, useState, useSyncExternalStore } from 'react'
import { createPortal } from 'react-dom'
import { ChevronDown } from 'lucide-react'
import { request } from '../api/client'
import type { ModelCatalog } from '../api/contracts'
import './CodexModelSettings.css'

let state: { catalog: ModelCatalog | null; loading: boolean; pending: boolean; error: string } = { catalog: null, loading: false, pending: false, error: '' }
const listeners = new Set<() => void>()
const subscribe = (listener: () => void) => { listeners.add(listener); return () => { listeners.delete(listener) } }
function publish(value: Partial<typeof state>) { state = { ...state, ...value }; listeners.forEach(listener => listener()) }
let loadedAt = 0
let revision = 0
let loading: Promise<void> | undefined

export function invalidateApiModels() { loadedAt = 0; revision++; publish({ catalog: null, error: '' }); void load(true) }
async function load(force = false) {
  if (loading) return loading
  if (!force && state.catalog && Date.now() - loadedAt < 60_000) return
  const current = revision
  publish({ loading: true })
  loading = request<ModelCatalog>('/api/settings/model/catalog').then(catalog => {
    if (!catalog || !Array.isArray(catalog.models) || typeof catalog.connection_id !== 'string') throw new Error('模型列表暂时无法读取，可以稍后重试。')
    if (current === revision) { loadedAt = Date.now(); publish({ catalog, error: '' }) }
  }).catch(error => { if (current === revision) publish({ error: error instanceof Error ? error.message : '模型列表暂时无法读取。' }) })
    .finally(() => { loading = undefined; publish({ loading: false }); if (current !== revision) void load(true) })
  return loading
}
export function useApiSelectionPending() { return useSyncExternalStore(subscribe, () => state.pending) }

export function ApiComposerSettings({ disabled = false }: { disabled?: boolean }) {
  const value = useSyncExternalStore(subscribe, () => state)
  const [open, setOpen] = useState(false)
  const [model, setModel] = useState('')
  const [manual, setManual] = useState(false)
  const [position, setPosition] = useState({ left: 8, top: 8 })
  const trigger = useRef<HTMLButtonElement>(null)
  const panel = useRef<HTMLDivElement>(null)
  const edited = useRef(false)
  const id = useId()
  useEffect(() => { void load() }, [])
  useEffect(() => {
    if (open && value.catalog && !edited.current) {
      setModel(value.catalog.model); setManual(!value.catalog.models.length)
    }
  }, [open, value.catalog])
  useLayoutEffect(() => {
    if (!open) return
    function place() {
      const rect = trigger.current?.getBoundingClientRect()
      if (!rect) return
      const width = Math.min(316, window.innerWidth - 16), height = panel.current?.offsetHeight ?? 310
      setPosition({ left: Math.max(8, Math.min(rect.left, window.innerWidth - width - 8)),
        top: rect.top > height + 16 ? rect.top - height - 8 : Math.max(8, Math.min(rect.bottom + 8, window.innerHeight - height - 8)) })
    }
    place(); panel.current?.querySelector<HTMLElement>('select,input')?.focus()
    const observer = typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(place)
    if (panel.current) observer?.observe(panel.current)
    function outside(event: PointerEvent) {
      if (!state.pending && !panel.current?.contains(event.target as Node) && !trigger.current?.contains(event.target as Node)) setOpen(false)
    }
    function key(event: KeyboardEvent) { if (event.key === 'Escape' && !state.pending) { setOpen(false); trigger.current?.focus() } }
    document.addEventListener('pointerdown', outside); document.addEventListener('keydown', key)
    window.addEventListener('resize', place); window.addEventListener('scroll', place, true)
    return () => { observer?.disconnect(); document.removeEventListener('pointerdown', outside); document.removeEventListener('keydown', key); window.removeEventListener('resize', place); window.removeEventListener('scroll', place, true) }
  }, [open])
  const catalog = value.catalog
  function show() {
    edited.current = false
    setModel(catalog?.model ?? ''); setManual(!catalog?.models.length); setOpen(next => !next); void load()
  }
  async function apply() {
    if (!catalog || !model.trim() || disabled || state.pending) return
    publish({ pending: true, error: '' })
    try {
      const saved = await request<{ model: string; connection_id: string }>('/api/settings/model/selection', {
        method: 'PUT', body: JSON.stringify({ model: model.trim(), connection_id: catalog.connection_id }),
      })
      if (!saved || typeof saved.model !== 'string') throw new Error('模型切换结果不完整，请刷新确认后再发送。')
      revision++; loadedAt = Date.now()
      publish({ catalog: { ...catalog, model: saved.model } }); setOpen(false); trigger.current?.focus()
    } catch (error) { publish({ error: error instanceof Error ? error.message : '模型切换失败，请重试。' }) }
    finally { publish({ pending: false }) }
  }
  return <div className="codex-composer-settings">
    <button ref={trigger} type="button" className="codex-model-trigger" aria-label="选择 API 模型" aria-haspopup="dialog" aria-expanded={open} aria-controls={open ? id : undefined} disabled={disabled || value.pending} onClick={show} title={catalog?.model || '选择 API 模型'}>
      <span>{catalog?.model || 'API 模型'}</span><ChevronDown size={13} />
    </button>
    {open && createPortal(<div id={id} ref={panel} className="codex-model-menu" role="dialog" aria-label="API 模型设置" style={position}>
      <strong>选择模型</strong>
      {catalog?.models.length ? <label>服务商模型<select aria-label="API 模型" value={manual ? '__manual__' : model || catalog.model} disabled={disabled || value.pending} onChange={event => {
        edited.current = true
        if (event.target.value === '__manual__') { setManual(true); setModel('') } else { setManual(false); setModel(event.target.value) }
      }}>
        {!catalog.models.some(item => item.id === (model || catalog.model)) && !manual && <option value={model || catalog.model}>{model || catalog.model || '请选择模型'}</option>}
        {catalog.models.map(item => <option key={item.id} value={item.id}>{item.id}</option>)}<option value="__manual__">手动填写模型 ID…</option>
      </select></label> : null}
      {(manual || !catalog?.models.length) && <label>模型 ID<input aria-label="API 模型 ID" maxLength={160} value={model} placeholder={catalog?.model || '服务商提供的模型 ID'} disabled={disabled || value.pending} onChange={event => { edited.current = true; setModel(event.target.value) }} /></label>}
      <p>{value.loading ? '正在读取模型列表…' : catalog?.message || '可以读取服务商模型列表，也可以手动填写。'}</p>
      <button type="button" disabled={value.loading || value.pending} onClick={() => void load(true)}>刷新模型列表</button>
      <p>应用后用于新发起的对话和规划。已发出的调用沿用原模型。</p>
      {value.error && <p role="alert">{value.error}</p>}
      <footer><button type="button" disabled={value.pending} onClick={() => { setOpen(false); trigger.current?.focus() }}>取消</button><button type="button" disabled={disabled || value.pending || !catalog || !model.trim()} onClick={() => void apply()}>{value.pending ? '切换中…' : '应用'}</button></footer>
    </div>, document.body)}
  </div>
}
