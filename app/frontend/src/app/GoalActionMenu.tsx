import { useEffect, useRef, useState } from 'react'
import type { ReactNode } from 'react'

export function GoalActionMenu({ title, children }: { title: string; children: ReactNode }) {
  const root = useRef<HTMLDetailsElement>(null)
  const [open, setOpen] = useState(false)
  useEffect(() => {
    if (!open) return
    const closeOutside = (event: MouseEvent) => {
      if (root.current && !root.current.contains(event.target as Node)) root.current.open = false
    }
    const closeEscape = (event: KeyboardEvent) => {
      if (event.key === 'Escape' && root.current) { root.current.open = false; root.current.querySelector('summary')?.focus() }
    }
    document.addEventListener('mousedown', closeOutside)
    document.addEventListener('keydown', closeEscape)
    return () => { document.removeEventListener('mousedown', closeOutside); document.removeEventListener('keydown', closeEscape) }
  }, [open])
  return <details ref={root} className="plan-actions" onToggle={event => setOpen(event.currentTarget.open)} onClick={event => {
    if ((event.target as HTMLElement).closest('button') && root.current) root.current.open = false
  }}><summary aria-label={`更多操作：${title}`} title="更多操作">⋯</summary><div role="menu">{children}</div></details>
}
