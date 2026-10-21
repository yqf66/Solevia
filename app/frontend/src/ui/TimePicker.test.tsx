import { useState } from 'react'
import { cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { TimePicker } from './TimePicker'
import { DialogBackdrop } from './DialogBackdrop'

afterEach(cleanup)
function Demo() {
  const [value, setValue] = useState('07:30')
  return <label>开始<TimePicker value={value} onChange={event => setValue(event.target.value)} /><output>{value}</output></label>
}
it('holds changes until confirmation and outside dismissal leaves the original value', () => {
  render(<Demo />)
  fireEvent.click(screen.getByRole('button', { name: /打开时间选择器/ }))
  fireEvent.click(within(screen.getByRole('listbox', { name: '小时' })).getByRole('option', { name: '09' }))
  fireEvent.mouseDown(screen.getByRole('dialog').parentElement!)
  expect(screen.getByRole('status')).toHaveTextContent('07:30')
  fireEvent.click(screen.getByRole('button', { name: /打开时间选择器/ }))
  fireEvent.click(within(screen.getByRole('listbox', { name: '小时' })).getByRole('option', { name: '10' }))
  fireEvent.click(within(screen.getByRole('listbox', { name: '分钟' })).getByRole('option', { name: '45' }))
  fireEvent.click(screen.getByRole('button', { name: '确定时间' }))
  expect(screen.getByRole('status')).toHaveTextContent('10:45')
  expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
})
it('accepts direct typing, normalizes short values and restores invalid times without emitting them', () => {
  const change = vi.fn()
  render(<TimePicker aria-label="时间" value="07:30" onChange={change} />)
  const input = screen.getByRole('textbox', { name: '时间' })
  fireEvent.change(input, { target: { value: '8:5' } }); fireEvent.blur(input)
  expect(change).toHaveBeenLastCalledWith({ target: { value: '08:05' } })
  change.mockClear(); fireEvent.change(input, { target: { value: '25:90' } }); fireEvent.blur(input)
  expect(change).not.toHaveBeenCalled(); expect(input).toHaveValue('07:30')
  expect(screen.getByRole('status')).toHaveTextContent('时间无效')
})
it('supports clearing optional time and disabled fields cannot open the picker', () => {
  const change = vi.fn()
  const view = render(<TimePicker aria-label="时间" value="07:30" onChange={change} />)
  fireEvent.change(screen.getByRole('textbox'), { target: { value: '' } })
  expect(change).toHaveBeenCalledWith({ target: { value: '' } })
  view.rerender(<TimePicker value="07:30" onChange={change} disabled />)
  expect(screen.getByRole('button')).toBeDisabled()
})
it('uses keyboard selection and Escape dismisses only the time picker within a dialog', () => {
  const close = vi.fn()
  render(<DialogBackdrop onDismiss={close}><section role="dialog" aria-label="设置"><Demo /></section></DialogBackdrop>)
  fireEvent.click(screen.getByRole('button', { name: /打开时间选择器/ }))
  const column = screen.getByRole('listbox', { name: '小时' })
  // jsdom has no scrolling implementation; keyboard handling still changes the selected option.
  const scroll = HTMLElement.prototype.scrollIntoView
  HTMLElement.prototype.scrollIntoView = vi.fn()
  try {
    fireEvent.keyDown(within(column).getByRole('option', { name: '07' }), { key: 'ArrowDown' })
    expect(within(column).getByRole('option', { name: '08' })).toHaveAttribute('aria-selected', 'true')
    fireEvent.keyDown(document, { key: 'Escape' })
    expect(screen.queryByRole('dialog', { name: '选择时间' })).not.toBeInTheDocument()
    expect(screen.getByRole('dialog', { name: '设置' })).toBeInTheDocument()
    expect(close).not.toHaveBeenCalled()
  } finally { HTMLElement.prototype.scrollIntoView = scroll }
})
