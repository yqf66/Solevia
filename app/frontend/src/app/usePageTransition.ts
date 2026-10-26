import { useLayoutEffect } from 'react'
import type { RefObject } from 'react'

export function usePageTransition(path: string, content: RefObject<HTMLElement | null>) {
  useLayoutEffect(() => {
    const element = content.current
    if (!element?.animate || window.matchMedia('(prefers-reduced-motion: reduce)').matches) return
    const screen = element.firstElementChild ?? element
    const animation = screen.animate([
      { opacity: 0.85, transform: 'translateY(7px)' },
      { opacity: 1, transform: 'translateY(0)' },
    ], { duration: 220, easing: 'cubic-bezier(.16,1,.3,1)' })
    return () => animation.cancel()
  }, [path, content])
}
