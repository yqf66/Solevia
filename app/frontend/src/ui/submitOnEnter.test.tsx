import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { submitOnEnter } from './submitOnEnter'

afterEach(cleanup)

it('sends on Enter and preserves multiline input, IME composition and the disabled state', () => {
  const submit = vi.fn(event => event.preventDefault())
  const { rerender } = render(<form onSubmit={submit}><textarea aria-label="消息" onKeyDown={submitOnEnter} /><button type="submit">发送</button></form>)
  const input = screen.getByRole('textbox')
  fireEvent.keyDown(input, { key: 'Enter', shiftKey: true })
  fireEvent.keyDown(input, { key: 'Enter', isComposing: true })
  fireEvent.keyDown(input, { key: 'Enter', keyCode: 229 })
  fireEvent.keyDown(input, { key: 'Enter', repeat: true })
  expect(submit).not.toHaveBeenCalled()
  fireEvent.keyDown(input, { key: 'Enter' })
  expect(submit).toHaveBeenCalledTimes(1)
  rerender(<form onSubmit={submit}><textarea aria-label="消息" onKeyDown={submitOnEnter} /><button type="submit" disabled>发送</button></form>)
  fireEvent.keyDown(screen.getByRole('textbox'), { key: 'Enter' })
  expect(submit).toHaveBeenCalledTimes(1)
})
