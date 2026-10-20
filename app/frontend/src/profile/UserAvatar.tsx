import './UserProfile.css'

export function UserAvatar({ src, className = '' }: { src?: string | null; className?: string }) {
  return <span className={`user-avatar ${className}`} aria-hidden="true">{src
    ? <img src={src} alt="" />
    : <svg viewBox="0 0 64 64" fill="none"><path d="M6 49c11-10 18-11 28-4s16 5 24-1v20H6Z" fill="currentColor" opacity=".12" /><circle cx="32" cy="25" r="10" fill="currentColor" opacity=".8" /><path d="M15 54c1-11 7-17 17-17s16 6 17 17" fill="currentColor" opacity=".55" /><circle cx="48" cy="13" r="4" fill="currentColor" opacity=".3" /></svg>}</span>
}
