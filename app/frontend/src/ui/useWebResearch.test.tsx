import { act, cleanup, renderHook, waitFor } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { useWebResearch } from './useWebResearch'
import { getResearchReadiness, setResearchConnection } from '../api'

let mode: 'codex' | 'api' = 'codex'
vi.mock('../codex/KernelSelection', () => ({ useKernelSelection: () => ({ mode }) }))
vi.mock('../api', () => ({ getResearchReadiness: vi.fn(), setResearchConnection: vi.fn() }))
afterEach(() => { cleanup(); vi.resetAllMocks() })

it('never prewarms Codex search and restores the API web control after a kernel switch', async () => {
  mode = 'codex'
  vi.mocked(setResearchConnection).mockResolvedValue({ ready: true, status: 'READY', components: {} })
  const view = renderHook(() => useWebResearch('goal'))
  act(() => view.result.current.setEnabled(true))
  expect(view.result.current.available).toBe(false)
  expect(view.result.current.enabled).toBe(false)
  expect(setResearchConnection).not.toHaveBeenCalled()
  expect(getResearchReadiness).not.toHaveBeenCalled()
  mode = 'api'; view.rerender()
  await waitFor(() => expect(setResearchConnection).toHaveBeenCalledTimes(1))
  expect(view.result.current.available).toBe(true)
  expect(view.result.current.enabled).toBe(true)
  mode = 'codex'; view.rerender()
  expect(setResearchConnection).toHaveBeenCalledTimes(2)
  expect(vi.mocked(setResearchConnection).mock.calls.at(-1)?.[1]).toBe(false)
  expect(view.result.current.enabled).toBe(false)
})
