import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { PrepareStartPage } from './PrepareStartPage'

afterEach(() => { cleanup(); vi.restoreAllMocks() })

it('shows a rejected start setup instead of claiming an unconfirmed plan is ready', async () => {
  vi.spyOn(globalThis, 'fetch').mockImplementation(async input => {
    if (String(input).endsWith('/execution/setup')) return new Response(JSON.stringify({ detail: 'Plan is not ready to start' }), { status: 409 })
    return new Response('{}', { status: 200 })
  })
  render(<MemoryRouter initialEntries={['/goals/test/prepare']}><Routes><Route path="/goals/:goalId/prepare" element={<PrepareStartPage />} /></Routes></MemoryRouter>)
  expect(await screen.findByRole('alert')).toHaveTextContent('计划尚未确认，请先返回时间规划页面确认。')
  expect(screen.getByRole('button', { name: '返回时间计划' })).toBeEnabled()
  expect(screen.queryByText(/计划已经确认/)).not.toBeInTheDocument()
  expect(screen.queryByRole('button', { name: '确认日期安排 →' })).not.toBeInTheDocument()
})
