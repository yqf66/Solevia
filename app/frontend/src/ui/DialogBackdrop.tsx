import { createContext, useContext, useEffect, useRef } from 'react'
import type { ReactNode } from 'react'
import { createPortal } from 'react-dom'

const Depth = createContext(0)
const layers: { element: HTMLElement; depth: number }[] = []
const top = () => layers.reduce<(typeof layers)[number] | undefined>((last, layer) => !last || layer.depth >= last.depth ? layer : last, undefined)?.element
const focusable = 'button:not(:disabled):not([tabindex="-1"]), input:not(:disabled):not([type="hidden"]):not([tabindex="-1"]), select:not(:disabled), textarea:not(:disabled), a[href], [tabindex="0"]'

export function DialogBackdrop({ children, onDismiss, className = 'inline-dialog-backdrop' }: {
  children: ReactNode; onDismiss: () => void; className?: string
}) {
  const root = useRef<HTMLDivElement>(null)
  const depth = useContext(Depth)
  const dismiss = useRef(onDismiss)
  useEffect(() => { dismiss.current = onDismiss }, [onDismiss])
  useEffect(() => {
    const element = root.current!
    const previous = document.activeElement as HTMLElement | null
    const layer = { element, depth }
    layers.push(layer)
    const dialog = element.querySelector<HTMLElement>('[role="dialog"], [role="alertdialog"]')
    if (dialog && top() === element && !element.contains(document.activeElement)) {
      const target = element.querySelector<HTMLElement>('[autofocus], [data-dialog-initial-focus]') ?? element.querySelector<HTMLElement>(focusable) ?? dialog
      if (target === dialog) dialog.tabIndex = -1
      target.focus()
    }
    function onKey(event: KeyboardEvent) {
      if (top() !== element) return
      if (event.key === 'Escape') { event.preventDefault(); event.stopPropagation(); dismiss.current(); return }
      if (event.key !== 'Tab') return
      const items = [...element.querySelectorAll<HTMLElement>(focusable)].filter(item => !item.closest('[hidden], [inert]'))
      const first = items[0], last = items.at(-1)
      if (!first) { event.preventDefault(); dialog?.focus(); return }
      if (event.shiftKey && (document.activeElement === first || !element.contains(document.activeElement))) { event.preventDefault(); last?.focus() }
      else if (!event.shiftKey && (document.activeElement === last || !element.contains(document.activeElement))) { event.preventDefault(); first.focus() }
    }
    document.addEventListener('keydown', onKey, true)
    return () => {
      layers.splice(layers.indexOf(layer), 1)
      document.removeEventListener('keydown', onKey, true)
      const remaining = top()
      if (previous?.isConnected && (!remaining || remaining.contains(previous))) previous.focus()
      else remaining?.querySelector<HTMLElement>(focusable)?.focus()
    }
  }, [depth])
  return <Depth.Provider value={depth + 1}>{createPortal(<div ref={root} className={className} role="presentation" onMouseDown={event => {
    if (event.target === event.currentTarget && top() === root.current) onDismiss()
  }}>{children}</div>, document.body)}</Depth.Provider>
}
