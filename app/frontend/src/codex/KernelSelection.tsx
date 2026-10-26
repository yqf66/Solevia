import { useEffect, useState, useSyncExternalStore } from 'react'
import { request } from '../api/client'
import { setKernelMode } from '../api/kernelMode'
import { CapabilitySettings } from './CapabilitySettings'

export type KernelStatus = {
  mode: 'codex' | 'api'; codex_connected: boolean
  capabilities: { codex: boolean; api: boolean; file_copies: boolean }
  codex_error?: string | null
  codex_error_code?: string | null
  codex_detected?: boolean
  api_configured?: boolean
}
let status: KernelStatus | null = null
let loading: Promise<void> | undefined
let loadedAt = 0
let selectionRevision = 0
const listeners = new Set<() => void>()
const subscribe = (listener: () => void) => { listeners.add(listener); return () => { listeners.delete(listener) } }
function publish(value: KernelStatus) {
  if (!value || !['api', 'codex'].includes(value.mode) || typeof value.codex_connected !== 'boolean'
      || !value.capabilities || ['codex', 'api', 'file_copies'].some(key => typeof value.capabilities[key as keyof KernelStatus['capabilities']] !== 'boolean')) {
    throw new Error('连接状态返回不完整，已保留原连接，请稍后重试。')
  }
  status = value; loadedAt = Date.now(); setKernelMode(value.mode)
  listeners.forEach(listener => listener())
}
export async function loadKernel(force = false) {
  if (loading) return loading
  if (!force && status && Date.now() - loadedAt < 10_000) return
  const revision = selectionRevision
  loading = request<KernelStatus>('/api/settings/kernel').then(value => {
    if (revision === selectionRevision) publish(value)
  }).finally(() => { loading = undefined })
  return loading
}
export async function selectKernel(mode: 'api' | 'codex') {
  const value = await request<KernelStatus>('/api/settings/kernel', { method: 'PUT', body: JSON.stringify({ mode }) })
  selectionRevision += 1; publish(value)
  return value
}
export function KernelRuntimeSync() {
  useEffect(() => {
    const refresh = () => { void loadKernel().catch(() => { /* Keep the last confirmed connection during a transient failure. */ }) }
    refresh(); window.addEventListener('focus', refresh)
    const timer = window.setInterval(refresh, 15_000)
    return () => { window.removeEventListener('focus', refresh); window.clearInterval(timer) }
  }, [])
  return null
}

export function useKernelSelection() {
  const value = useSyncExternalStore(subscribe, () => status)
  const legacy = ['codex', 'unified'].includes(import.meta.env.VITE_SOLEVIA_RUNTIME)
  return value ?? { mode: legacy ? 'codex' : 'api', codex_connected: false,
    capabilities: { codex: legacy, api: true, file_copies: legacy } } as KernelStatus
}

export function KernelSetupPrompt({ onConfigure }: { onConfigure: () => void }) {
  const kernel = useKernelSelection()
  const needsSetup = kernel.mode === 'codex' ? !kernel.codex_connected && !!kernel.codex_error : kernel.api_configured === false
  if (!needsSetup) return null
  return <button className="kernel-setup-prompt" type="button" onClick={onConfigure}>
    <strong>{kernel.mode === 'codex' ? 'Codex 需要完成连接' : '先连接一个模型'}</strong>
    <span>打开连接设置，按步骤完成 →</span>
  </button>
}

export function KernelConnectionSettings({ children }: { children: React.ReactNode }) {
  const kernel = useKernelSelection()
  const [mode, setMode] = useState(kernel.mode)
  const [busy, setBusy] = useState(false)
  const [refreshing, setRefreshing] = useState(false)
  const [copied, setCopied] = useState(false)
  const [error, setError] = useState('')
  useEffect(() => { void loadKernel().catch(() => setError('无法读取连接状态。请确认 Solevia 桌面工作台中的服务正在运行，再刷新状态。')) }, [])
  useEffect(() => { setMode(kernel.mode) }, [kernel.mode])
  const ready = kernel.mode === 'codex' ? kernel.codex_connected : kernel.api_configured === true
  async function refresh() {
    setRefreshing(true); setError('')
    try { await loadKernel(true) }
    catch { setError('无法读取连接状态。请确认 Solevia 桌面工作台中的服务正在运行，再刷新状态。') }
    finally { setRefreshing(false) }
  }
  async function copyHelp() {
    try {
      await navigator.clipboard.writeText('请帮我排查 Solevia 的本机 Codex 连接。当前状态：' + (kernel.codex_error_code ?? 'CODEX_UNAVAILABLE') + '。请检查 Codex 是否安装、登录，以及 codex app-server 是否可启动。不要读取或复制登录密钥，不要关闭沙箱或修改全局配置。需要安装或修改设置时先说明。完成后提醒我在 Solevia 的模型连接中点击「检测并连接」。')
      setCopied(true)
    } catch { setError('无法复制。可以直接向 Codex 描述上方的连接状态；无需提供密钥或登录文件。') }
  }
  async function connectLocalCodex() {
    setBusy(true); setError('')
    try {
      const value = await request<KernelStatus>('/api/settings/kernel/codex/connect', { method: 'POST' })
      selectionRevision += 1; publish(value)
      if (!value.codex_connected) setError(value.codex_error || '连接未完成，请按上方提示处理后重试。')
    } catch (caught) { setError(caught instanceof Error ? caught.message : '连接未完成，请重试。') }
    finally { setBusy(false) }
  }
  async function connect(next: 'codex' | 'api') {
    setBusy(true); setError('')
    try { await selectKernel(next) }
    catch (caught) { setError(caught instanceof Error ? caught.message : '连接方式未保存，请重试。') }
    finally { setBusy(false) }
  }
  return <div className="settings-form kernel-connection-settings">
    <label><span>内核连接方式</span><select aria-label="内核连接方式" value={mode} disabled={busy} onChange={event => { setMode(event.target.value as 'api' | 'codex'); setError('') }}>
      <option value="codex" disabled={!kernel.capabilities.codex}>Codex 内核</option><option value="api">自配置 API</option>
    </select></label>
    <div className={`kernel-connection-state${mode !== kernel.mode || !ready ? ' is-pending' : ''}`} role="status">
      <span className="kernel-state-dot" aria-hidden="true" />
      <div><strong>{mode !== kernel.mode ? `正在配置：${mode === 'api' ? '自配置 API' : 'Codex 内核'}` : !ready ? '尚未连接模型' : `当前使用：${kernel.mode === 'codex' ? 'Codex 内核' : '自配置 API'}`}</strong>
      <p>{mode !== kernel.mode ? !ready ? '当前尚未连接模型；完成设置并启用后即可开始对话。' : `启用后切换。目前仍由${kernel.mode === 'codex' ? ' Codex 内核' : '自配置 API'}处理对话。` : kernel.mode === 'codex' ? kernel.codex_connected ? '本机连接和登录状态检查已通过。模型与思考强度在对话框中选择。' : '尚未连接，按下方提示完成设置后再开始对话。' : kernel.api_configured ? 'API 配置已保存；实际调用权限以对话结果为准。' : '请在下方补齐服务地址、密钥和模型，再保存启用。'}</p></div>
      {mode === 'api' && kernel.mode !== 'api' && <button className="button" type="button" disabled={busy} onClick={() => void connect('api')}>{busy ? '切换中…' : '使用已保存的 API'}</button>}
    </div>
    <CapabilitySettings />
    {mode === 'codex' ? <>
      {!kernel.codex_connected && <div className="connection-troubleshoot" role="status">
        <strong>还差一步，连接本机 Codex</strong>
        <p>{kernel.codex_error ?? '正在检查本机 Codex 连接…'}</p>
        {kernel.codex_error_code && <small>状态编号：{kernel.codex_error_code}</small>}
        <div className="connection-guide-actions"><button className="button primary" type="button" disabled={busy} onClick={() => void connectLocalCodex()}>{busy ? '正在检测并连接…' : '检测并连接'}</button><button className="button" type="button" disabled={refreshing} onClick={() => void refresh()}>{refreshing ? '检查中…' : '刷新状态'}</button><button className="button" type="button" onClick={() => void copyHelp()}>{copied ? '已复制排查说明' : '复制排查说明'}</button></div>
      </div>}
      <details className="connection-guide" open={!kernel.codex_connected}>
        <summary>第一次连接，怎么操作？</summary>
        <ol>
          <li><strong>安装 Codex</strong><span>在当前 Windows 用户下安装。Solevia 会自动查找本机程序。</span><a href="https://learn.chatgpt.com/docs/cli" target="_blank" rel="noreferrer">打开官方安装指南 ↗</a></li>
          <li><strong>完成登录</strong><span>打开 Codex 按提示登录；使用命令行时，运行 <code>codex login</code> 并完成浏览器登录。</span></li>
          <li><strong>确认连接并开始</strong><span>点击上方「检测并连接」，检查通过后会启用 Codex 内核。无需重新安装 Solevia；连接失败会显示具体处理提示。</span></li>
        </ol>
        <p className="muted">已安装仍找不到？确认 Codex 与 Solevia 使用同一个 Windows 用户；自定义安装位置可通过 SOLEVIA_CODEX_EXECUTABLE 指定。刷新状态只读取检测结果；新安装或登录后请点击「检测并连接」。</p>
      </details>
      <p className="muted">附件默认只读。需要修改时，在原对话中选择允许或拒绝，修改版随回答展示。</p>
      {kernel.mode !== 'codex' && <button className="button primary" type="button" disabled={busy || !kernel.codex_connected} onClick={() => void connect('codex')}>{busy ? '连接中…' : '使用 Codex 内核'}</button>}
    </> : children}
    {error && <p className="form-error" role="alert">{error}</p>}
  </div>
}
