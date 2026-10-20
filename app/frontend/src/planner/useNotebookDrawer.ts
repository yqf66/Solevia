import { useEffect, useRef, useState } from 'react'

export function useNotebookDrawer() {
  const [open, setOpen] = useState(false)
  const drawerRef = useRef<HTMLElement>(null)
  const triggerRef = useRef<HTMLButtonElement>(null)
  useEffect(() => {
    if (!open) return
    const drawer = drawerRef.current
    const trigger = triggerRef.current
    const focusable = () => [...(drawer?.querySelectorAll<HTMLElement>('button:not(:disabled), a[href], summary, input:not(:disabled), textarea:not(:disabled), select:not(:disabled)') ?? [])].filter(node => node.getClientRects().length)
    focusable()[0]?.focus()
    const dismiss = (event: KeyboardEvent) => {
      if (event.key === 'Escape') { setOpen(false); return }
      if (event.key !== 'Tab') return
      const nodes = focusable(), first = nodes[0], last = nodes.at(-1)
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus() }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus() }
    }
    const resize = () => { if (window.innerWidth > 1200) setOpen(false) }
    window.addEventListener('keydown', dismiss)
    window.addEventListener('resize', resize)
    return () => {
      window.removeEventListener('keydown', dismiss)
      window.removeEventListener('resize', resize)
      if (trigger?.isConnected && window.innerWidth <= 1200) trigger.focus()
    }
  }, [open])
  return { open, setOpen, drawerRef, triggerRef }
}
