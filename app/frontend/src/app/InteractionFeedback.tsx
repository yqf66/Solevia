import { useEffect, useState } from 'react'
import { createPortal } from 'react-dom'

type Touch = { x: number; y: number; left: number; top: number; width: number; height: number; radius: string; key: number }

// One short, clipped response per deliberate press. No animation loop or pointer tracking.
export function InteractionFeedback() {
  const [touch, setTouch] = useState<Touch | null>(null)
  useEffect(() => {
    if (typeof window.matchMedia !== 'function') return
    const motion = window.matchMedia('(prefers-reduced-motion: reduce)')
    let timer: ReturnType<typeof setTimeout> | undefined
    const clear = () => { clearTimeout(timer); setTouch(null) }
    function respond(event: PointerEvent | KeyboardEvent) {
      if (motion.matches || ('button' in event && event.button !== 0) || ('key' in event && !['Enter', ' '].includes(event.key))) return
      const element = event.target instanceof Element ? event.target.closest<HTMLElement>('button, a.button, summary') : null
      if (!element || element.matches(':disabled, [aria-disabled="true"]')) return
      const rect = element.getBoundingClientRect()
      if (rect.width > 650 || rect.height > 140) return
      clearTimeout(timer)
      setTouch({ left: rect.left, top: rect.top, width: rect.width, height: rect.height,
        x: 'clientX' in event ? event.clientX - rect.left : rect.width / 2,
        y: 'clientY' in event ? event.clientY - rect.top : rect.height / 2,
        radius: getComputedStyle(element).borderRadius, key: performance.now() })
      timer = setTimeout(() => setTouch(null), 380)
    }
    document.addEventListener('pointerdown', respond, { passive: true })
    document.addEventListener('keydown', respond)
    document.addEventListener('scroll', clear, { capture: true, passive: true })
    motion.addEventListener('change', clear)
    return () => {
      clearTimeout(timer)
      document.removeEventListener('pointerdown', respond)
      document.removeEventListener('keydown', respond)
      document.removeEventListener('scroll', clear, true)
      motion.removeEventListener('change', clear)
    }
  }, [])
  if (!touch) return null
  return createPortal(<div className="control-feedback" aria-hidden="true" style={{ left: touch.left, top: touch.top, width: touch.width, height: touch.height, borderRadius: touch.radius }}>
    <i key={touch.key} style={{ left: touch.x, top: touch.y }} />
  </div>, document.body)
}
