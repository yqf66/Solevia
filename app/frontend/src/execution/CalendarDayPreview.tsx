import { useLayoutEffect, useRef, useState, type ReactNode } from 'react'
import { createPortal } from 'react-dom'

export function CalendarDayPreview({ id, anchor, date, expanded, children, onEnter, onLeave, onClose }: {
  id: string
  anchor: HTMLButtonElement
  date: string
  expanded: boolean
  children: ReactNode
  onEnter: () => void
  onLeave: () => void
  onClose: () => void
}) {
  const panel = useRef<HTMLDivElement>(null)
  const [position, setPosition] = useState<{ left: number; top: number; width: number } | null>(null)

  useLayoutEffect(() => {
    function place() {
      if (!panel.current) return
      const margin = 12
      const gap = 8
      const viewportWidth = document.documentElement.clientWidth
      const viewportHeight = window.innerHeight
      const width = Math.min(expanded ? 620 : 380, viewportWidth - margin * 2)
      const origin = anchor.getBoundingClientRect()
      const height = panel.current.getBoundingClientRect().height
      // Prefer a side position so a long card does not cover the calendar.
      const beside = origin.left >= width + gap + margin
      const left = beside ? origin.left - width - gap
        : Math.max(margin, Math.min(origin.left, viewportWidth - width - margin))
      const preferredTop = beside ? origin.top : origin.bottom + gap
      const top = Math.max(margin, Math.min(preferredTop, viewportHeight - height - margin))
      setPosition(previous => previous?.left === left && previous.top === top && previous.width === width
        ? previous : { left, top, width })
    }
    function dismiss(event: PointerEvent) {
      const target = event.target as Node
      if (!anchor.contains(target) && !panel.current?.contains(target)) onClose()
    }
    function escape(event: KeyboardEvent) {
      if (event.key === 'Escape') onClose()
    }
    place()
    const observer = typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(place)
    if (panel.current) observer?.observe(panel.current)
    window.addEventListener('resize', place)
    window.addEventListener('scroll', place, true)
    document.addEventListener('pointerdown', dismiss)
    document.addEventListener('keydown', escape)
    return () => {
      observer?.disconnect()
      window.removeEventListener('resize', place)
      window.removeEventListener('scroll', place, true)
      document.removeEventListener('pointerdown', dismiss)
      document.removeEventListener('keydown', escape)
    }
  }, [anchor, expanded, onClose])

  return createPortal(<div ref={panel} id={id} role="tooltip" aria-label={`${date} 每日安排`}
    tabIndex={0} className={`calendar-tooltip${expanded ? ' is-expanded' : ''}`}
    style={{ ...position, visibility: position ? 'visible' : 'hidden' }}
    onClick={event => event.stopPropagation()}
    onMouseEnter={onEnter} onMouseLeave={onLeave} onFocus={onEnter} onBlur={onLeave}>
    <header><time dateTime={date}>{date} · 每日安排</time><span>Esc 关闭</span></header>
    {children}
  </div>, document.body)
}
