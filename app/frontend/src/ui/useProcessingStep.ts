import { useEffect, useState } from 'react'

export function useProcessingStep(active: boolean, steps: string[], intervalMs = 1500) {
  const [index, setIndex] = useState(0)

  useEffect(() => {
    if (!active) return
    const resetTimer = window.setTimeout(() => setIndex(0), 0)
    const timer = window.setInterval(() => {
      setIndex((current) => Math.min(current + 1, steps.length - 1))
    }, intervalMs)
    return () => {
      window.clearTimeout(resetTimer)
      window.clearInterval(timer)
    }
  }, [active, intervalMs, steps.length])

  const visibleIndex = active ? index : 0
  return { index: visibleIndex, message: steps[visibleIndex] }
}
