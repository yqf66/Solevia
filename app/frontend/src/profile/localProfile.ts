import { useSyncExternalStore } from 'react'

export type LocalProfile = { nickname: string; avatar: string | null }
const key = 'solevia.local-profile.v1'
const empty: LocalProfile = { nickname: '本地用户', avatar: null }
let cached = empty
let serialized: string | null = null

function snapshot() {
  try {
    const value = localStorage.getItem(key)
    if (value === serialized) return cached
    serialized = value
    const parsed = value ? JSON.parse(value) as Partial<LocalProfile> : null
    cached = {
      nickname: typeof parsed?.nickname === 'string' && parsed.nickname.trim() ? [...parsed.nickname.trim()].slice(0, 24).join('') : empty.nickname,
      avatar: typeof parsed?.avatar === 'string' && /^data:image\/(webp|png|jpeg);base64,/.test(parsed.avatar) ? parsed.avatar : null,
    }
  } catch { cached = empty }
  return cached
}
function subscribe(changed: () => void) {
  window.addEventListener('solevia:profile-changed', changed)
  window.addEventListener('storage', changed)
  return () => { window.removeEventListener('solevia:profile-changed', changed); window.removeEventListener('storage', changed) }
}
export function saveLocalProfile(profile: LocalProfile) {
  const nickname = profile.nickname.trim()
  if (!nickname || [...nickname].length > 24) throw new Error('昵称请填写 1–24 个字。')
  try { localStorage.setItem(key, JSON.stringify({ nickname, avatar: profile.avatar })) }
  catch { throw new Error('浏览器暂时无法保存个人资料，请释放存储空间后重试。') }
  window.dispatchEvent(new Event('solevia:profile-changed'))
}
export function useLocalProfile() { return useSyncExternalStore(subscribe, snapshot, () => empty) }
