import { useEffect, useRef, useState } from 'react'
import { Camera, RotateCcw } from 'lucide-react'
import { UserAvatar } from './UserAvatar'
import { prepareAvatar } from './avatarImage'
import { saveLocalProfile, useLocalProfile } from './localProfile'

export function ProfileEditor() {
  const saved = useLocalProfile()
  const [nickname, setNickname] = useState(saved.nickname)
  const [avatar, setAvatar] = useState(saved.avatar)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const input = useRef<HTMLInputElement>(null)
  const revision = useRef(0)
  useEffect(() => () => { revision.current++ }, [])
  async function upload(file?: File) {
    if (!file) return
    const current = ++revision.current
    setBusy(true); setError(''); setNotice('')
    try { const result = await prepareAvatar(file); if (current === revision.current) setAvatar(result) }
    catch (caught) { if (current === revision.current) setError((caught as Error).message) }
    finally { if (current === revision.current) setBusy(false) }
  }
  const dirty = nickname.trim() !== saved.nickname || avatar !== saved.avatar
  return <form className="profile-editor" onSubmit={event => {
    event.preventDefault(); setError(''); setNotice('')
    if (busy) return
    try { saveLocalProfile({ nickname, avatar }); setNickname(nickname.trim()); setNotice('个人资料已保存，侧栏已同步更新。') }
    catch (caught) { setError((caught as Error).message) }
  }}>
    <div className="profile-identity-card"><UserAvatar src={avatar} className="profile-avatar-preview" /><div><span>你的 Solevia 空间</span><h3>{nickname.trim() || '为自己取个名字'}</h3><p>从这一刻，走自己的路。</p></div></div>
    <div className="profile-avatar-actions"><input ref={input} className="profile-file-input" tabIndex={-1} type="file" accept="image/png,image/jpeg,image/webp" aria-label="选择本地头像" onChange={event => { void upload(event.target.files?.[0]); event.target.value = '' }} /><button className="button" type="button" disabled={busy} onClick={() => input.current?.click()}><Camera size={16} />{busy ? '正在处理…' : '上传头像'}</button><button className="text-button" type="button" disabled={busy || !avatar} onClick={() => { setAvatar(null); setNotice(''); setError('') }}><RotateCcw size={14} />恢复默认头像</button></div>
    <p className="profile-help">JPG、PNG、WebP，最大 10 MB。图片会居中裁切为方形。</p>
    <label className="profile-nickname-label">昵称<input autoComplete="nickname" value={nickname} maxLength={48} onChange={event => { setNickname(event.target.value); setNotice('') }} placeholder="希望 Solevia 怎样称呼你" required /><small>{[...nickname].length} / 24</small></label>
    <div className="profile-save-row"><p>资料保存在当前浏览器中。</p><button className="button primary" type="submit" disabled={busy || !dirty || !nickname.trim() || [...nickname.trim()].length > 24}>保存个人资料</button></div>
    {error && <p className="form-error" role="alert">{error}</p>}{notice && <p className="settings-success" role="status">{notice}</p>}
  </form>
}
