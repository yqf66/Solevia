import { useLayoutEffect } from 'react'
import type { RefObject } from 'react'

export function usePageTransition(path: string, content: RefObject<HTMLElement | null>) {
  useLayoutEffect(() => {
    const element = content.current
    if (!element?.animate || window.matchMedia('(prefers-reduced-motion: reduce)').matches) return
    const animation = element.animate([
      { opacity: 0.75, transform: 'translateY(5px)' },
      { opacity: 1, transform: 'translateY(0)' },
    ], { duration: 170, easing: 'cubic-bezier(.2,.75,.25,1)' })
    return () => animation.cancel()
  }, [path, content])
}
