import { forwardRef, useCallback, useImperativeHandle, useLayoutEffect, useRef, type TextareaHTMLAttributes } from 'react'

// One row until the draft wraps; grow within the screen's CSS max-height, then
// scroll inside the input. Draft changes, restored panels and width changes all
// use the same measurement, so deleting or sending text shrinks it again.
export const AutoResizeTextarea = forwardRef<HTMLTextAreaElement, TextareaHTMLAttributes<HTMLTextAreaElement>>(
  function AutoResizeTextarea({ value, ...props }, forwardedRef) {
    const inputRef = useRef<HTMLTextAreaElement>(null)
    useImperativeHandle(forwardedRef, () => inputRef.current!, [])
    const resize = useCallback(() => {
      const input = inputRef.current
      if (!input) return
      const style = getComputedStyle(input)
      const border = (parseFloat(style.borderTopWidth) || 0) + (parseFloat(style.borderBottomWidth) || 0)
      const minimum = parseFloat(style.minHeight) || 0
      const maximum = parseFloat(style.maxHeight) || Infinity
      input.style.height = 'auto'
      const contentHeight = input.scrollHeight + border
      input.style.height = `${Math.max(minimum, Math.min(maximum, contentHeight))}px`
      input.style.overflowY = contentHeight > maximum ? 'auto' : 'hidden'
    }, [])
    useLayoutEffect(resize, [resize, value])
    useLayoutEffect(() => {
      const input = inputRef.current
      if (!input) return
      let width = input.clientWidth
      const observer = typeof ResizeObserver === 'function' ? new ResizeObserver(() => {
        if (width !== input.clientWidth) { width = input.clientWidth; resize() }
      }) : undefined
      observer?.observe(input)
      window.addEventListener('resize', resize)
      return () => { observer?.disconnect(); window.removeEventListener('resize', resize) }
    }, [resize])
    return <textarea {...props} value={value} rows={1} ref={inputRef} />
  },
)
