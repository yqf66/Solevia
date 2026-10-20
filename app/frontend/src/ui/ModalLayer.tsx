import type { ReactNode } from 'react'
import { createPortal } from 'react-dom'

// Keep dialogs outside the page grid so they cannot consume a layout column.
export function ModalLayer({ children }: { children: ReactNode }) {
  return createPortal(children, document.body)
}
