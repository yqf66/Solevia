import { useEffect, useRef, useState } from 'react'
import { Check, ChevronDown, Download, FilePenLine, FileText } from 'lucide-react'
import { request } from '../api/client'
import './ConversationFiles.css'
import { useKernelSelection } from './KernelSelection'

export type FileCopy = {
  id: string; goal_id: string; status: string; source_names: string[]
  reason: string; error: string; summary: string
  files: { index: number; name: string; bytes: number }[]
}
type FileContent = { name: string; content: string }

function FileResult({ task, file }: { task: FileCopy; file: FileCopy['files'][number] }) {
  const [content, setContent] = useState<FileContent | null>(null)
  const [open, setOpen] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  async function read(download: boolean) {
    if (busy) return
    if (!download && open) { setOpen(false); return }
    setBusy(true); setError('')
    try {
      const result = content ?? await request<FileContent>(`/api/codex/conversation-files/${task.goal_id}/${task.id}/files/${file.index}`)
      setContent(result)
      if (download) {
        const url = URL.createObjectURL(new Blob([result.content], { type: 'text/plain;charset=utf-8' }))
        const anchor = document.createElement('a'); anchor.href = url; anchor.download = result.name; anchor.click()
        window.setTimeout(() => URL.revokeObjectURL(url), 1000)
      } else setOpen(true)
    } catch (caught) { setError(caught instanceof Error ? caught.message : '文件暂时无法读取') }
    finally { setBusy(false) }
  }
  return <div className="conversation-file-result">
    <div className="conversation-file-row">
      <FileText size={16} aria-hidden="true" />
      <button className="conversation-file-name" type="button" disabled={busy} aria-expanded={open} onClick={() => void read(false)}>
        <span title={file.name}>{file.name}</span><ChevronDown size={13} aria-hidden="true" />
      </button>
      <small>{Math.max(1, Math.ceil(file.bytes / 1024))} KB</small>
      <button className="conversation-file-download" type="button" disabled={busy} aria-label={`下载 ${file.name}`} title="下载文件" onClick={() => void read(true)}><Download size={15} /></button>
    </div>
    {open && content && <pre className="conversation-file-preview" tabIndex={0} aria-label={`${file.name} 内容预览`}>{content.content}</pre>}
    {error && <p className="form-error" role="alert">{error}</p>}
  </div>
}

export function ConversationFiles({ messageId, ready = true, answerVersion = '' }: { messageId: string; ready?: boolean; answerVersion?: string }) {
  const kernel = useKernelSelection()
  const enabled = kernel.capabilities.file_copies
  const [tasks, setTasks] = useState<FileCopy[]>([])
  const [loadedFor, setLoadedFor] = useState('')
  const [pending, setPending] = useState('')
  const [reason, setReason] = useState('')
  const [reasonFor, setReasonFor] = useState('')
  const [error, setError] = useState('')
  const scope = useRef(messageId)
  scope.current = messageId
  const running = tasks.some(task => ['QUEUED', 'RUNNING'].includes(task.status))
  useEffect(() => {
    setTasks([]); setLoadedFor(''); setError(''); setReason(''); setReasonFor(''); setPending('')
    if (!enabled || !ready) return
    let active = true
    void request<FileCopy[]>(`/api/codex/conversation-files/answers/${messageId}`)
      .then(result => { if (active && Array.isArray(result)) { setTasks(result); setLoadedFor(messageId) } })
      .catch(() => { /* Ordinary answers need no file decoration when offline. */ })
    return () => { active = false }
  }, [enabled, messageId, ready, answerVersion])
  useEffect(() => {
    if (!enabled || !running) return
    let active = true
    const timer = window.setInterval(() => {
      void request<FileCopy[]>(`/api/codex/conversation-files/answers/${messageId}`)
        .then(result => { if (active && Array.isArray(result)) { setTasks(result); setError('') } })
        .catch(() => { if (active) setError('状态暂时未更新，文件仍在后台生成。') })
    }, 1500)
    return () => { active = false; window.clearInterval(timer) }
  }, [enabled, running, messageId])
  async function decide(task: FileCopy, decision: 'ALLOW' | 'DENY') {
    if (pending) return
    const owner = messageId
    setPending(task.id); setError('')
    try {
      const saved = await request<FileCopy>(`/api/codex/conversation-files/${task.goal_id}/${task.id}/decision`, {
        method: 'POST', body: JSON.stringify({ decision, reason: decision === 'DENY' ? reason : '' }),
      })
      if (scope.current === owner) { setTasks(current => current.map(row => row.id === saved.id ? saved : row)); setReasonFor(''); setReason('') }
    } catch (caught) { if (scope.current === owner) setError(caught instanceof Error ? caught.message : '选择未保存，请重试') }
    finally { if (scope.current === owner) setPending('') }
  }
  if (!enabled || !ready || loadedFor !== messageId || !tasks.length) return null
  return <div className="conversation-files" aria-label="本次文件处理">
    {tasks.map(task => <div key={task.id}>
      {task.status === 'NEEDS_CONFIRMATION' ? <section className="conversation-file-consent" aria-label="文件修改确认">
        <div className="conversation-file-question"><FilePenLine size={16} aria-hidden="true" /><div>
          <strong>允许生成文件修改版吗？</strong>
          <p title={task.source_names.join('、')}>{task.source_names.join('、')} · 原文件保持只读</p>
        </div></div>
        <div className="conversation-file-choices">
          <button title={kernel.mode !== 'codex' ? '先在个人设置中切换到 Codex 内核' : undefined} className="file-consent-allow" type="button" disabled={!!pending || kernel.mode !== 'codex'} onClick={() => void decide(task, 'ALLOW')}>允许</button>
          <button type="button" disabled={!!pending} onClick={() => void decide(task, 'DENY')}>拒绝</button>
          <button className="file-consent-reason" type="button" disabled={!!pending} aria-expanded={reasonFor === task.id} onClick={() => setReasonFor(reasonFor === task.id ? '' : task.id)}>填写拒绝理由</button>
        </div>
        {reasonFor === task.id && <input className="file-consent-reason-input" autoFocus aria-label="拒绝理由（选填）" placeholder="拒绝理由（选填）" maxLength={1000} value={reason} onChange={event => setReason(event.target.value)} />}
      </section> : task.status === 'SUCCEEDED' ? <div className="conversation-file-output">
        <p className="conversation-file-complete"><Check size={14} aria-hidden="true" />修改版已生成 · 原文件保持只读</p>
        {task.summary && <p className="conversation-file-summary">{task.summary}</p>}
        {task.files.map(file => <FileResult key={file.index} task={task} file={file} />)}
      </div> : task.status === 'DENIED' ? <p className="conversation-file-note">已拒绝本次修改{task.reason ? `：${task.reason}` : '，原文件保持只读。'}</p>
        : ['QUEUED', 'RUNNING'].includes(task.status) ? <p className="conversation-file-note" role="status">正在生成文件修改版，可以继续阅读对话…</p>
          : <div className="conversation-file-note"><span>{task.error || '这次生成已中断，原文件保持不变。'}</span> <button type="button" disabled={!!pending || kernel.mode !== 'codex'} onClick={() => void decide(task, 'ALLOW')}>重新生成</button></div>}
    </div>)}
    {error && <p className="form-error" role="alert">{error}</p>}
  </div>
}
