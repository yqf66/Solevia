import { act, cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { FocusTimerWidget } from './FocusTimerWidget'

afterEach(() => { cleanup(); vi.useRealTimers(); vi.restoreAllMocks() })

it('keeps counting through Escape and fullscreen reentry without recording or adding an interruption', async () => {
  vi.useFakeTimers()
  const save = vi.fn().mockResolvedValue(undefined)
  render(<FocusTimerWidget tasks={[]} sessions={[]} onSave={save} />)
  fireEvent.click(screen.getByRole('button', { name: '开始专注' }))
  expect(screen.getByRole('dialog')).toBeInTheDocument()
  act(() => vi.advanceTimersByTime(2000))
  fireEvent.keyDown(document, { key: 'Escape' })
  expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
  act(() => vi.advanceTimersByTime(3000))
  expect(screen.getByText('24:55')).toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', { name: '进入全屏专注' }))
  const dialog = screen.getByRole('dialog')
  expect(within(dialog).getByText('24:55')).toBeInTheDocument()
  expect(save).not.toHaveBeenCalled()
  fireEvent.click(within(dialog).getByRole('button', { name: '暂停专注' }))
  act(() => vi.advanceTimersByTime(3000))
  expect(within(dialog).getByText('24:55')).toBeInTheDocument()
  await act(async () => { fireEvent.click(within(dialog).getByRole('button', { name: '结束并记录' })) })
  expect(save).toHaveBeenCalledExactlyOnceWith({ task_id: null, planned_minutes: 25, actual_seconds: 5, interruptions: 1 })
})

it('automatically records the finished round exactly once', async () => {
  vi.useFakeTimers()
  const save = vi.fn().mockResolvedValue(undefined)
  render(<FocusTimerWidget tasks={[]} sessions={[]} onSave={save} />)
  fireEvent.change(screen.getByRole('spinbutton', { name: '自定义专注分钟' }), { target: { value: '1' } })
  fireEvent.click(screen.getByRole('button', { name: '开始专注' }))
  await act(async () => vi.advanceTimersByTime(60000))
  await act(async () => vi.advanceTimersByTime(5000))
  expect(save).toHaveBeenCalledExactlyOnceWith({ task_id: null, planned_minutes: 1, actual_seconds: 60, interruptions: 0 })
})

it('keeps the round running when native fullscreen exits', () => {
  vi.useFakeTimers()
  const save = vi.fn()
  render(<FocusTimerWidget tasks={[]} sessions={[]} onSave={save} />)
  fireEvent.click(screen.getByRole('button', { name: '开始专注' }))
  act(() => document.dispatchEvent(new Event('fullscreenchange')))
  expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
  act(() => vi.advanceTimersByTime(2000))
  expect(screen.getByText('24:58')).toBeInTheDocument()
  expect(save).not.toHaveBeenCalled()
})
