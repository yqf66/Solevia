import { useEffect, useRef, useState } from 'react'
import { X, UserRound, SlidersHorizontal, Clock3, PlugZap } from 'lucide-react'
import { DialogBackdrop } from './ui/DialogBackdrop'
import { TimePicker } from './ui/TimePicker'
import { ProfileEditor } from './profile/ProfileEditor'
import { KernelConnectionSettings, selectKernel } from './codex/KernelSelection'
import { invalidateApiModels } from './codex/ApiModelSettings'
import { request } from './api/client'
import type { ModelCatalog } from './api/contracts'
import './profile/SettingsRefinement.css'
import {
  getModelConnection,
  getWorkspaceScheduleProfile,
  updateModelConnection,
  updateWorkspaceScheduleProfile,
  type ModelConnection,
  type WorkspaceScheduleProfile,
} from './api'

type Tab = 'PROFILE' | 'MODEL' | 'ROUTINE' | 'STYLE'

const boundaryFields: Array<{ key: 'wake_time' | 'available_from' | 'sleep_time'; label: string }> = [
  { key: 'wake_time', label: '起床' },
  { key: 'available_from', label: '最早可安排' },
  { key: 'sleep_time', label: '睡觉' },
]
const routineBlocks: Array<{ enabled: 'breakfast_enabled' | 'lunch_enabled' | 'nap_enabled' | 'dinner_enabled'; start: 'breakfast_start' | 'lunch_start' | 'nap_start' | 'dinner_start'; end: 'breakfast_end' | 'lunch_end' | 'nap_end' | 'dinner_end'; label: string }> = [
  { enabled: 'breakfast_enabled', start: 'breakfast_start', end: 'breakfast_end', label: '早餐' },
  { enabled: 'lunch_enabled', start: 'lunch_start', end: 'lunch_end', label: '午餐' },
  { enabled: 'nap_enabled', start: 'nap_start', end: 'nap_end', label: '午休' },
  { enabled: 'dinner_enabled', start: 'dinner_start', end: 'dinner_end', label: '晚餐' },
]

export function UserSettingsPanel({ onClose, initialTab = 'PROFILE' }: { onClose: () => void; initialTab?: Tab }) {
  const [tab, setTab] = useState<Tab>(initialTab)
  const [profile, setProfile] = useState<WorkspaceScheduleProfile | null>(null)
  const [connection, setConnection] = useState<ModelConnection | null>(null)
  const [apiKey, setApiKey] = useState('')
  const [catalog, setCatalog] = useState<ModelCatalog | null>(null)
  const [catalogBusy, setCatalogBusy] = useState(false)
  const catalogRevision = useRef(0)
  const [busy, setBusy] = useState(false)
  const [notice, setNotice] = useState('')
  const [error, setError] = useState('')

  function checkConnectionInput() {
    if (!connection) return false
    try {
      const url = new URL(connection.base_url.trim())
      if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password || url.search || url.hash) throw new Error()
    } catch { setError('请填写完整的 Base URL，以 https:// 或 http:// 开头，地址中不要包含密钥、查询参数或登录信息。'); return false }
    if (/\/(chat\/completions|responses|models)\/?$/.test(connection.base_url.trim())) {
      setError('这里需要服务商的基础地址。请去掉末尾的 /chat/completions、/responses 或 /models 后重试。'); return false
    }
    if (connection.proxy_url.trim()) {
      try { if (!['http:', 'https:'].includes(new URL(connection.proxy_url.trim()).protocol)) throw new Error() }
      catch { setError('代理地址需以 http:// 或 https:// 开头；不使用代理时请留空。'); return false }
    }
    return true
  }

  useEffect(() => {
    catalogRevision.current++
    setCatalog(null); setCatalogBusy(false)
  }, [connection?.provider, connection?.base_url, connection?.proxy_url, apiKey])

  async function readModels() {
    if (!connection || catalogBusy) return
    if (!checkConnectionInput()) return
    const revision = ++catalogRevision.current
    setCatalogBusy(true); setError('')
    try {
      const result = await request<ModelCatalog>('/api/settings/model/catalog', { method: 'POST', body: JSON.stringify({
        provider: connection.provider, base_url: connection.base_url, proxy_url: connection.proxy_url,
        ...(apiKey.trim() ? { api_key: apiKey.trim() } : {}),
      }) })
      if (revision === catalogRevision.current) setCatalog(result)
    } catch (caught) { if (revision === catalogRevision.current) setError(caught instanceof Error ? caught.message : '模型列表读取失败。') }
    finally { if (revision === catalogRevision.current) setCatalogBusy(false) }
  }

  useEffect(() => {
    let active = true
    Promise.all([getWorkspaceScheduleProfile(), getModelConnection()]).then(([schedule, model]) => {
      if (!active) return
      setProfile(schedule)
      setConnection(model)
    }).catch((caught) => {
      if (active) setError(caught instanceof Error ? caught.message : '暂时无法读取个人设置。')
    })
    return () => { active = false }
  }, [])

  async function saveModel() {
    if (!connection) return
    if (!checkConnectionInput()) return
    if (!apiKey.trim() && !connection.api_key_configured) { setError('请先填写服务商提供的 API Key，再保存并使用。'); return }
    setBusy(true); setError(''); setNotice('')
    try {
      const saved = await updateModelConnection({
        provider: connection.provider,
        base_url: connection.base_url,
        proxy_url: connection.proxy_url,
        model: connection.model,
        ...(apiKey.trim() ? { api_key: apiKey.trim() } : {}),
      })
      setConnection(saved); setApiKey(''); invalidateApiModels(); await selectKernel('api'); setNotice('API 配置已保存，已切换到自配置 API。首次对话会验证模型的实际调用权限。')
    } catch (caught) { setError(caught instanceof Error ? caught.message : '模型配置保存失败。') }
    finally { setBusy(false) }
  }

  async function saveProfile(kind: 'ROUTINE' | 'STYLE') {
    if (!profile) return
    const invalid = document.querySelector<HTMLInputElement>('.user-settings-body input:invalid')
    if (invalid) { invalid.reportValidity(); return }
    setBusy(true); setError(''); setNotice('')
    try {
      const input = kind === 'STYLE'
        ? { schedule_style: profile.schedule_style }
        : {
          wake_time: profile.wake_time, available_from: profile.available_from, sleep_time: profile.sleep_time,
          breakfast_enabled: profile.breakfast_enabled, breakfast_start: profile.breakfast_start, breakfast_end: profile.breakfast_end,
          lunch_enabled: profile.lunch_enabled, lunch_start: profile.lunch_start, lunch_end: profile.lunch_end,
          nap_enabled: profile.nap_enabled, nap_start: profile.nap_start, nap_end: profile.nap_end,
          dinner_enabled: profile.dinner_enabled, dinner_start: profile.dinner_start, dinner_end: profile.dinner_end,
          routine_exclusions: profile.routine_exclusions,
        }
      setProfile(await updateWorkspaceScheduleProfile(input))
      setNotice(kind === 'STYLE' ? '安排偏好已保存，只会轻量影响每日排时。' : '作息已保存，默认可用时间会据此重新计算。')
    } catch (caught) { setError(caught instanceof Error ? caught.message : '个人设置保存失败。') }
    finally { setBusy(false) }
  }

  return <DialogBackdrop className="user-settings-backdrop" onDismiss={onClose}>
    <section className="user-settings-panel" role="dialog" aria-modal="true" aria-labelledby="user-settings-title">
      <header><div><p className="eyebrow">Solevia · 个人空间</p><h2 id="user-settings-title">让这里，更像你</h2></div><button className="icon-button" type="button" aria-label="关闭个人设置" onClick={onClose}><X size={20} /></button></header>
      <nav aria-label="设置分类">
        <button className={tab === 'PROFILE' ? 'active' : ''} type="button" onClick={() => { setTab('PROFILE'); setNotice(''); setError('') }}><UserRound size={16} />个人资料</button>
        <button className={tab === 'ROUTINE' ? 'active' : ''} type="button" onClick={() => { setTab('ROUTINE'); setNotice(''); setError('') }}><Clock3 size={16} />日常作息</button>
        <button className={tab === 'STYLE' ? 'active' : ''} type="button" onClick={() => { setTab('STYLE'); setNotice(''); setError('') }}><SlidersHorizontal size={16} />安排偏好</button>
        <button className={tab === 'MODEL' ? 'active' : ''} type="button" onClick={() => { setTab('MODEL'); setNotice(''); setError('') }}><PlugZap size={16} />模型连接</button>
      </nav>
      <div className="user-settings-body">
        {tab === 'PROFILE' ? <ProfileEditor /> : !profile || !connection ? !error && <p className="muted">正在读取设置…</p> : tab === 'MODEL' ? <KernelConnectionSettings><div className="settings-form model-settings-form">
          <div className="connection-quickstart"><strong>三步接入你的 API</strong><p>① 填写服务地址与 Key　② 读取或填写模型 ID　③ 保存并使用。之后可在对话框中随时切换模型。</p></div>
          <label><span>接口类型</span><select value={connection.provider} onChange={(event) => setConnection({ ...connection, provider: event.target.value as ModelConnection['provider'] })}><option value="deepseek">DeepSeek Responses</option><option value="openai_compatible">OpenAI 兼容接口</option>{connection.provider === 'fake' && <option value="fake" disabled>请选择接口类型</option>}</select><small>OpenAI 兼容接口使用 Chat Completions 协议。</small></label>
          <label><span>Base URL</span><input value={connection.base_url} onChange={(event) => setConnection({ ...connection, base_url: event.target.value })} placeholder="例如 https://api.example.com/v1" /><small>粘贴服务商提供的基础地址，通常以 /v1 结尾；无需附加 /chat/completions 或 /models。</small></label>
          <label><span>模型名称</span><input list="api-provider-models" value={connection.model} onChange={(event) => setConnection({ ...connection, model: event.target.value })} placeholder="选择或填写模型 ID" /><datalist id="api-provider-models">{catalog?.models.map(item => <option key={item.id} value={item.id} />)}</datalist></label>
          <label><span>API Key</span><input aria-label="API Key" type="password" autoComplete="new-password" value={apiKey} onChange={(event) => setApiKey(event.target.value)} placeholder={connection.api_key_configured ? `已配置 · ${connection.api_key_hint}` : '输入 API Key'} /><small>留空会保留现有 Key；明文不会由接口回传。</small></label>
          <div><button className="button" type="button" disabled={busy || catalogBusy || connection.provider === 'fake' || !connection.base_url.trim()} onClick={() => void readModels()}>{catalogBusy ? '正在读取…' : '读取可用模型'}</button><small>部分服务商不提供列表，可直接填写其文档中的模型 ID。读到列表代表列表接口可访问，不代表所有模型均有调用权限。</small>{catalog && <p role="status">{catalog.models.length ? `已读取 ${catalog.models.length} 个模型。` : ''}{catalog.message}</p>}</div>
          <details className="connection-guide"><summary>配置后无法使用，先检查什么？</summary><ul><li>地址：使用服务商的 API 地址，避免填写官网、控制台或完整的聊天接口地址。</li><li>密钥与模型：检查 Key 是否有效、账号余额，以及是否开通所选模型；模型名称须与服务商提供的 ID 一致。</li><li>网络：确认服务商可访问；需要代理时在下方填写代理地址。Codex 自身的代理配置不会自动用于此处。</li></ul><p>保存配置不会发起付费对话；保存后发送一句简短消息验证即可。</p></details>
          <details><summary>代理设置（可选）</summary><label><span>代理 URL</span><input value={connection.proxy_url} onChange={(event) => setConnection({ ...connection, proxy_url: event.target.value })} placeholder="填写你自己的代理地址（可选）" /></label></details>
          <button className="button primary" type="button" disabled={busy || connection.provider === 'fake' || !connection.base_url.trim() || !connection.model.trim()} onClick={() => void saveModel()}>{busy ? '保存中…' : '保存并使用 API'}</button>
        </div></KernelConnectionSettings> : tab === 'ROUTINE' ? <div className="settings-form routine-settings-form">
          <div className="settings-intro"><strong>一天的安排边界</strong><p>起床不等于立刻开始工作；“最早可安排”之前不会放入任务。</p></div>
          <div className="routine-boundary-grid">{boundaryFields.map(({ key, label }) => <label key={key}><span>{label}</span><TimePicker value={profile[key]} onChange={(event) => setProfile({ ...profile, [key]: event.target.value })} /></label>)}</div>
          <div className="routine-section-heading"><div><strong>固定活动</strong><small>关闭你没有的活动；启用后会从可用时间中扣除。</small></div></div>
          <div className="routine-block-list">{routineBlocks.map((block) => <div className={`routine-block-row${profile[block.enabled] ? '' : ' disabled'}`} key={block.label}>
            <label className="routine-toggle"><input type="checkbox" checked={profile[block.enabled]} onChange={(event) => setProfile({ ...profile, [block.enabled]: event.target.checked })} /><span>{block.label}</span></label>
            <label><span>开始</span><TimePicker disabled={!profile[block.enabled]} value={profile[block.start]} onChange={(event) => setProfile({ ...profile, [block.start]: event.target.value })} /></label>
            <label><span>结束</span><TimePicker disabled={!profile[block.enabled]} value={profile[block.end]} onChange={(event) => setProfile({ ...profile, [block.end]: event.target.value })} /></label>
          </div>)}</div>
          <div className="routine-section-heading"><div><strong>其他不可安排时间</strong><small>通勤、接送、家务等可以按你的生活继续添加。</small></div><button className="button" type="button" onClick={() => setProfile({ ...profile, routine_exclusions: [...profile.routine_exclusions, { label: '通勤', start_local: '08:00', end_local: '09:00', enabled: true }] })}>＋ 添加</button></div>
          <div className="routine-extra-list">{profile.routine_exclusions.length === 0 ? <p className="muted">暂未添加</p> : profile.routine_exclusions.map((item, index) => <div className={`routine-extra-row${item.enabled ? '' : ' disabled'}`} key={index}>
            <input aria-label={`启用${item.label || `占用时段${index + 1}`}`} type="checkbox" checked={item.enabled} onChange={(event) => setProfile({ ...profile, routine_exclusions: profile.routine_exclusions.map((value, itemIndex) => itemIndex === index ? { ...value, enabled: event.target.checked } : value) })} />
            <input aria-label={`占用时段${index + 1}名称`} value={item.label} maxLength={40} placeholder="例如：早高峰通勤" onChange={(event) => setProfile({ ...profile, routine_exclusions: profile.routine_exclusions.map((value, itemIndex) => itemIndex === index ? { ...value, label: event.target.value } : value) })} />
            <TimePicker aria-label={`${item.label || `占用时段${index + 1}`}开始`}  disabled={!item.enabled} value={item.start_local} onChange={(event) => setProfile({ ...profile, routine_exclusions: profile.routine_exclusions.map((value, itemIndex) => itemIndex === index ? { ...value, start_local: event.target.value } : value) })} />
            <TimePicker aria-label={`${item.label || `占用时段${index + 1}`}结束`}  disabled={!item.enabled} value={item.end_local} onChange={(event) => setProfile({ ...profile, routine_exclusions: profile.routine_exclusions.map((value, itemIndex) => itemIndex === index ? { ...value, end_local: event.target.value } : value) })} />
            <button className="icon-button" aria-label={`删除${item.label || `占用时段${index + 1}`}`} type="button" onClick={() => setProfile({ ...profile, routine_exclusions: profile.routine_exclusions.filter((_, itemIndex) => itemIndex !== index) })}>×</button>
          </div>)}</div>
          <p className="settings-source-note">当前智能安排使用：{profile.availability_mode === 'ROUTINE' ? '根据作息自动计算' : '你最近保存的自定义时段'}。自定义时段可在智能安排窗口恢复为作息方案。</p>
          <button className="button primary" type="button" disabled={busy} onClick={() => void saveProfile('ROUTINE')}>{busy ? '保存中…' : '保存作息'}</button>
        </div> : <div className="settings-form">
          <div className="settings-intro"><strong>每日排时风格</strong><p>这项偏好只帮助模型在同样可行的时段中做选择，不会改变路线、任务内容、日期、依赖或固定安排。</p></div>
          <label><span>我的偏好</span><textarea rows={7} maxLength={2000} value={profile.schedule_style} onChange={(event) => setProfile({ ...profile, schedule_style: event.target.value })} placeholder="例如：白天优先安排背诵、整理等机械任务；晚上更适合需要专注或创造力的工作。" /><small>{profile.schedule_style.length}/2000</small></label>
          <button className="button primary" type="button" disabled={busy} onClick={() => void saveProfile('STYLE')}>{busy ? '保存中…' : '保存安排偏好'}</button>
        </div>}
        {error && <p className="form-error" role="alert">{error}</p>}
        {notice && <p className="settings-success" role="status">{notice}</p>}
      </div>
    </section>
  </DialogBackdrop>
}
