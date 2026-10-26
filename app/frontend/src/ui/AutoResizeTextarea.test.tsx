import { createRef } from 'react'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, expect, it } from 'vitest'
import { AutoResizeTextarea } from './AutoResizeTextarea'

afterEach(cleanup)

it('grows, caps, shrinks and remeasures a restored draft while preserving the forwarded input ref', () => {
  const ref = createRef<HTMLTextAreaElement>()
  const view = render(<AutoResizeTextarea ref={ref} aria-label="输入内容" value="" readOnly style={{ minHeight: '26px', maxHeight: '132px', border: 0 }} />)
  const input = screen.getByRole('textbox') as HTMLTextAreaElement
  let contentHeight = 26
  Object.defineProperty(input, 'scrollHeight', { configurable: true, get: () => contentHeight })
  expect(ref.current).toBe(input)
  expect(input).toHaveAttribute('rows', '1')
  view.rerender(<AutoResizeTextarea ref={ref} aria-label="输入内容" value="短消息" readOnly style={{ minHeight: '26px', maxHeight: '132px', border: 0 }} />)
  expect(input.style.height).toBe('26px')
  contentHeight = 98
  view.rerender(<AutoResizeTextarea ref={ref} aria-label="输入内容" value={'多行\n'.repeat(4)} readOnly style={{ minHeight: '26px', maxHeight: '132px', border: 0 }} />)
  expect(input.style.height).toBe('98px')
  contentHeight = 300
  fireEvent(window, new Event('resize'))
  expect(input.style.height).toBe('132px')
  expect(input.style.overflowY).toBe('auto')
  contentHeight = 26
  view.rerender(<AutoResizeTextarea ref={ref} aria-label="输入内容" value="" readOnly style={{ minHeight: '26px', maxHeight: '132px', border: 0 }} />)
  expect(input.style.height).toBe('26px')
  expect(input.style.overflowY).toBe('hidden')
})
