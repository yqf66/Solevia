import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { AllocationDialog } from './AllocationDialog'
import type { UnifiedToday } from './api'

afterEach(() => { cleanup(); vi.restoreAllMocks() })

it('shows preview before writing and applies the displayed fingerprint', async () => {
  const today: UnifiedToday = { date: '2035-09-10', timezone: 'Asia/Shanghai', fixed_tasks: [], flexible_tasks: [], conflicts: [] }
  const weekday = (new Date(`${today.date}T12:00:00`).getDay() + 6) % 7
  const input = { scheduled_date: today.date, windows: [{ weekday, start_local: '09:00', end_local: '10:00' }], include_flexible: true }
  const preview = { request: input, fingerprint: 'verified-preview', timezone: today.timezone, assignments: [{ task_id: 'a', start_local: '09:00', end_local: '09:30' }], unallocated: [], notes: [], available_minutes: 60, required_minutes: 30, assigned_minutes: 30, unallocated_minutes: 0 }
  const savedWindows = [{ id: 'window-1', profile_id: 'default', weekday, start_local: '09:00', end_local: '10:00', valid_from: null, valid_to: null, timezone: today.timezone, created_at: '' }]
  const fetchMock = vi.spyOn(globalThis, 'fetch').mockImplementation((url, options) => {
    const path = String(url)
    if (path.includes('/schedule/profile/availability/effective')) return Promise.resolve(new Response(JSON.stringify({ source: 'CUSTOM', windows: [{ weekday, start_local: '09:00', end_local: '10:00', valid_from: null, valid_to: null }] }), { status: 200 }))
    if (path.endsWith('/schedule/profile/availability')) return Promise.resolve(new Response(JSON.stringify(options?.method === 'PUT' ? savedWindows : savedWindows), { status: 200 }))
    return Promise.resolve(new Response(JSON.stringify(path.endsWith('/preview') ? preview : today), { status: 200 }))
  })
  const onApplied = vi.fn()
  render(<AllocationDialog today={today} onClose={vi.fn()} onApplied={onApplied} />)
  expect(screen.getByRole('button', { name: '确认安排' })).toBeDisabled()
  fireEvent.click(await screen.findByRole('button', { name: '生成预览' }))
  expect(await screen.findByText('09:00–09:30')).toBeInTheDocument()
  expect(fetchMock).toHaveBeenCalledTimes(3)
  fireEvent.click(screen.getByRole('button', { name: '确认安排' }))
  await waitFor(() => expect(onApplied).toHaveBeenCalledWith(today))
  expect(JSON.parse(String(fetchMock.mock.calls[3][1]?.body))).toEqual({ request: input, fingerprint: 'verified-preview' })
})

it('shows routine windows by default and persists edits as a reusable template', async () => {
  const today: UnifiedToday = { date: '2035-09-10', timezone: 'Asia/Shanghai', fixed_tasks: [], flexible_tasks: [], conflicts: [] }
  const weekday = (new Date(`${today.date}T12:00:00`).getDay() + 6) % 7
  const fetchMock = vi.spyOn(globalThis, 'fetch').mockImplementation((url, options) => {
    const path = String(url)
    if (path.includes('/availability/effective')) return Promise.resolve(new Response(JSON.stringify({ source: 'ROUTINE', windows: [{ weekday, start_local: '08:00', end_local: '12:00', valid_from: null, valid_to: null }] }), { status: 200 }))
    if (path.endsWith('/availability') && options?.method === 'PUT') return Promise.resolve(new Response(JSON.stringify([]), { status: 200 }))
    if (path.endsWith('/availability')) return Promise.resolve(new Response(JSON.stringify([]), { status: 200 }))
    return Promise.resolve(new Response(JSON.stringify({ request: {}, fingerprint: 'x', timezone: today.timezone, assignments: [], unallocated: [], notes: [], available_minutes: 240, required_minutes: 0, assigned_minutes: 0, unallocated_minutes: 0 }), { status: 200 }))
  })
  render(<AllocationDialog today={today} onClose={vi.fn()} onApplied={vi.fn()} />)
  expect(await screen.findByDisplayValue('08:00')).toBeInTheDocument()
  expect(screen.getByText(/根据你的作息自动扣除/)).toBeInTheDocument()
  fireEvent.change(screen.getByDisplayValue('08:00'), { target: { value: '09:00' } })
  fireEvent.click(screen.getByRole('button', { name: '保存时段并生成预览' }))
  await waitFor(() => expect(fetchMock.mock.calls.some(([url, options]) => String(url).endsWith('/availability') && options?.method === 'PUT')).toBe(true))
  const write = fetchMock.mock.calls.find(([url, options]) => String(url).endsWith('/availability') && options?.method === 'PUT')
  const body = JSON.parse(String(write?.[1]?.body))
  expect(body.windows).toHaveLength(7)
  expect(new Set(body.windows.map((item: { weekday: number }) => item.weekday)).size).toBe(7)
})
