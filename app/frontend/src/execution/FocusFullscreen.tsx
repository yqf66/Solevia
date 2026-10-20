import { useEffect } from 'react'
import { Minimize2 } from 'lucide-react'
import type { ReactNode } from 'react'
import { SoleviaWordmark } from '../brand/SoleviaMark'
import { DialogBackdrop } from '../ui/DialogBackdrop'

export function FocusFullscreen({ clock, title, running, finished, notice, onToggle, onFinish, onClose }: {
  clock: ReactNode; title: string; running: boolean; finished: boolean; notice: string
  onToggle: () => void; onFinish?: () => void; onClose: () => void
}) {
  useEffect(() => {
    const exit = () => { if (!document.fullscreenElement) onClose() }
    document.addEventListener('fullscreenchange', exit)
    return () => document.removeEventListener('fullscreenchange', exit)
  }, [onClose])
  useEffect(() => {
    const app = document.querySelector<HTMLElement>('.app-shell')
    if (!app) return
    const inert = app.inert, hidden = app.getAttribute('aria-hidden')
    app.inert = true; app.setAttribute('aria-hidden', 'true')
    return () => { app.inert = inert; if (hidden === null) app.removeAttribute('aria-hidden'); else app.setAttribute('aria-hidden', hidden) }
  }, [])
  return <DialogBackdrop className="focus-fullscreen-backdrop" onDismiss={onClose}>
    <section className="focus-fullscreen" role="dialog" aria-modal="true" aria-labelledby="focus-session-title">
      <header><div className="focus-fullscreen-brand"><SoleviaWordmark /></div><button className="button" type="button" onClick={onClose} aria-label="退出全屏，继续计时"><Minimize2 size={18} />退出全屏 <kbd>Esc</kbd></button></header>
      <div className="focus-fullscreen-center"><p className="focus-fullscreen-kicker">留在此刻，一次做好一件事</p><h2 id="focus-session-title">{title}</h2>{clock}
        <div className="focus-fullscreen-actions"><button className="button primary" type="button" onClick={onToggle}>{running ? '暂停专注' : finished ? '重新开始' : '继续专注'}</button>{onFinish && <button className="button" type="button" onClick={onFinish}>结束并记录</button>}</div>
        <p className="focus-fullscreen-note" role={notice ? 'status' : undefined}>{notice || '退出全屏后，计时仍会继续。'}</p>
      </div>
      <footer>千里之行，始于足下。</footer>
    </section>
  </DialogBackdrop>
}
