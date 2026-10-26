import { useEffect, useState } from 'react'
import { request } from '../api/client'
import './CapabilitySettings.css'

type Features = Record<string, boolean>
type Bridge = { url: string; token: string }
type Prepared = { bridge: Bridge; permit: string }
type Plan = { capabilities: { Key: string; Name: string; Benefit: string; DownloadMiB: number[] }[]; plan: { features: Features; downloadMiB: number[]; diskMiB: number[] }; space: { free: number; required: number; enough: boolean } }
type Progress = { status: string; stage: string; step: number; bytes: number; total: number; speed: number; elapsed: number; traffic?: boolean; logs: string[]; error?: string; url?: string }
const labels: Record<string, string> = { web: 'API 联网检索', documents: 'PDF / Office 阅读', ocr: '图片文字识别', semantic: '本地语义查找' }
const sessionKey = 'solevia-capability-install'
const size = (bytes: number) => bytes >= 1024 ** 3 ? `${(bytes / 1024 ** 3).toFixed(1)} GiB` : bytes >= 1024 ** 2 ? `${(bytes / 1024 ** 2).toFixed(1)} MiB` : `${Math.round(bytes / 1024)} KiB`
const running = (value: Progress | null) => value?.status === 'INSTALLING' || value?.status === 'RESTORING'

export async function installerRequest<T>(bridge: Bridge, path: string, body?: unknown): Promise<T> {
  const controller = new AbortController()
  const timeout = window.setTimeout(() => controller.abort(), 35000)
  try {
    const result = await fetch(`${bridge.url}${path}`, { method: body ? 'POST' : 'GET', headers: { 'X-Solevia-Installer': bridge.token, ...(body ? { 'Content-Type': 'application/json' } : {}) }, ...(body ? { body: JSON.stringify(body) } : {}), signal: controller.signal })
    const value = await result.json()
    if (!result.ok) throw new Error(value.message || '安装服务暂不可用，请稍后重试。')
    return value
  } finally { window.clearTimeout(timeout) }
}

export function CapabilitySettings() {
  const [installed, setInstalled] = useState<Features | null>(null)
  const [bridge, setBridge] = useState<Bridge | null>(() => { try { return JSON.parse(sessionStorage.getItem(sessionKey) || 'null') } catch { return null } })
  const [plan, setPlan] = useState<Plan | null>(null)
  const [features, setFeatures] = useState<Features>({})
  const [progress, setProgress] = useState<Progress | null>(null)
  const [network, setNetwork] = useState('Direct')
  const [source, setSource] = useState('China')
  const [busy, setBusy] = useState(false)
  const [message, setMessage] = useState('')
  const active = running(progress)
  useEffect(() => { void request<{ installed: Features }>('/api/settings/capabilities').then(value => { if (value?.installed) setInstalled(value.installed) }).catch(() => {}) }, [])
  useEffect(() => {
    if (!bridge) return
    let cancelled = false
    let timer: number
    async function poll() {
      try {
        const value = await installerRequest<Progress>(bridge!, '/status')
        if (cancelled) return
        setProgress(value)
        setMessage('')
        if (running(value)) timer = window.setTimeout(() => void poll(), 1000)
        else if (value.status === 'COMPLETE' || value.status === 'FAILED') sessionStorage.removeItem(sessionKey)
      } catch {
        if (!cancelled) { setMessage('暂时无法读取安装进度，正在重连；请保持页面打开。'); timer = window.setTimeout(() => void poll(), 3000) }
      }
    }
    void poll()
    return () => { cancelled = true; window.clearTimeout(timer) }
  }, [bridge])
  useEffect(() => {
    if (!bridge || !plan || active) return
    let cancelled = false
    const timer = window.setTimeout(() => {
      void installerRequest<Plan>(bridge, '/plan', { features }).then(value => { if (!cancelled) setPlan(value) }).catch(error => { if (!cancelled) setMessage(error.message) })
    }, 250)
    return () => { cancelled = true; window.clearTimeout(timer) }
    // Recalculate only when the user's choices change, not when a plan arrives.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [features, bridge, active])

  async function open() {
    setBusy(true); setMessage('')
    try {
      const prepared = await request<Prepared>('/api/settings/capabilities/prepare', { method: 'POST' })
      const value = await installerRequest<Plan>(prepared.bridge, '/plan')
      setFeatures(value.plan.features); setPlan(value); setBridge(prepared.bridge)
    } catch (caught) { setMessage(caught instanceof Error ? caught.message : '安装服务尚未就绪，请稍后重试。') }
    finally { setBusy(false) }
  }
  async function install() {
    if (!bridge) return
    setBusy(true); setMessage('')
    try {
      const prepared = await request<Prepared>('/api/settings/capabilities/prepare', { method: 'POST' })
      const value = await installerRequest<Progress>(prepared.bridge, '/install', { permit: prepared.permit, features, network, source })
      sessionStorage.setItem(sessionKey, JSON.stringify(prepared.bridge))
      setProgress(value); setBridge({ ...prepared.bridge })
    } catch (caught) { setMessage(caught instanceof Error ? caught.message : '无法开始安装，请重试。') }
    finally { setBusy(false) }
  }
  return <details className="connection-guide capability-settings" open={Boolean(bridge) || undefined}>
    <summary>补装与管理能力</summary>
    <p>直接在这里选择、安装并查看进度。普通对话只需连接模型，Codex 使用自己的搜索。</p>
    {!plan && !active && <>
      {installed && <div className="capability-install-list">{Object.entries(labels).map(([key, label]) => <span key={key}>{label}<small>{installed[key] ? '已安装' : '未安装'}</small></span>)}</div>}
      <button type="button" className="button" disabled={busy} onClick={() => void open()}>{busy ? '正在读取…' : '选择要安装的能力'}</button>
    </>}
    {plan && !active && progress?.status !== 'COMPLETE' && <>
      <div className="capability-choice-grid">{plan.capabilities.map(item => <label key={item.Key} className={features[item.Key] ? 'is-selected' : ''}>
        <span><input type="checkbox" checked={Boolean(features[item.Key])} disabled={busy} onChange={event => setFeatures(value => ({ ...value, [item.Key]: event.target.checked }))} /><strong>{labels[item.Key] || item.Name}</strong></span>
        <small>{item.Benefit}</small><small>首次下载约 {item.DownloadMiB[0]}–{item.DownloadMiB[1]} MiB{installed?.[item.Key] ? ' · 已安装，可复用' : ''}</small>
      </label>)}</div>
      <div className="capability-space"><span>安装盘可用 <strong>{size(plan.space.free)}</strong></span><span>建议预留 <strong>{size(plan.space.required)}</strong>（含临时空间）</span><span>{plan.space.enough ? '空间充足；实际下载会复用缓存。' : '空间不足，请减少能力或清理磁盘。'}</span></div>
      <div className="capability-network"><label>网络<select value={network} onChange={e => setNetwork(e.target.value)} disabled={busy}><option value="Direct">直连</option><option value="System">系统 / 环境代理</option></select></label><label>下载源<select value={source} onChange={e => setSource(e.target.value)} disabled={busy}><option value="China">国内镜像</option><option value="Official">官方源</option></select></label></div>
      <p className="capability-install-note">补装时暂停业务服务，资料与对话保留。请保持此页面打开，完成后会提供返回工作台的按钮。取消能力只会停用，不会删除已下载依赖。</p>
      <button type="button" className="button button-primary" disabled={busy || !plan.space.enough} onClick={() => void install()}>{busy ? '正在确认…' : progress?.status === 'FAILED' ? '确认并重试补装' : '确认并开始补装'}</button>
    </>}
    {progress && progress.status !== 'IDLE' && <section className="capability-progress" aria-label="安装进度" aria-live="polite">
      <div className="capability-progress-heading"><strong>{progress.stage}</strong><span>{Math.floor(progress.elapsed / 60)} 分 {progress.elapsed % 60} 秒</span></div>
      <div className="capability-stage-list">{['环境', 'Python', 'Node.js', '依赖', '界面', '工作区'].map((label, index) => <span key={label} className={progress.status === 'COMPLETE' || index + 1 < progress.step ? 'is-complete' : index + 1 === progress.step ? 'is-current' : ''}>{label}</span>)}</div>
      <progress aria-label="当前文件下载进度" {...(progress.total > 0 ? { max: progress.total, value: progress.bytes } : progress.status === 'COMPLETE' ? { max: 1, value: 1 } : {})} />
      <div className="capability-transfer"><span><small>当前下载网速</small><strong>{active ? `${size(progress.speed)} / 秒` : '—'}</strong></span><span><small>{progress.traffic ? '当前步骤传输流量' : '当前文件已下载'}</small><strong>{size(progress.bytes)}{progress.total > 0 ? ` / ${size(progress.total)}` : ''}</strong></span></div>
      {active && <small>总大小未知的步骤显示活动进度。网速为实际传输流量；使用缓存、校验和构建时可以为 0。</small>}
      {progress.error && <p role="alert">{progress.error}</p>}
      {progress.url && <a className="button button-primary" href={`${progress.url}${window.location.pathname}`}>返回工作台继续 →</a>}
      <details className="capability-logs"><summary>查看详细安装日志</summary><pre>{progress.logs.join('\n')}</pre></details>
    </section>}
    {message && <p role="status">{message}</p>}
  </details>
}
