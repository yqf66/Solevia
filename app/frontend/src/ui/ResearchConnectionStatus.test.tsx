import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import * as api from '../api'
import { useWebResearch } from './useWebResearch'
import { ResearchConnectionStatus } from './ResearchConnectionStatus'

function Harness() {
  const connection = useWebResearch('goal-1')
  return <><button onClick={() => connection.setEnabled((value) => !value)}>联网</button>
    <span>{connection.enabled ? '回答允许联网' : '回答不联网'}</span>
    <ResearchConnectionStatus connection={connection} /></>
}
const ready = { ready: true, status: 'READY', components: { search_reader: 'READY', bge: 'READY' } }
afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.useRealTimers() })

describe('web connection toggle', () => {
  it('shows progress immediately on toggle without submitting a question', async () => {
    let finish: (value: api.ResearchReadiness) => void = () => undefined
    const connection = vi.spyOn(api, 'setResearchConnection').mockImplementation(() => new Promise((resolve) => { finish = resolve }))
    const create = vi.spyOn(api, 'createResearchResponse')
    render(<Harness />)
    fireEvent.click(screen.getByText('联网'))
    expect(screen.getByText('正在准备联网服务…')).toBeInTheDocument()
    expect(connection).toHaveBeenCalledWith(expect.any(String), true, 1)
    await act(async () => finish(ready))
    expect(screen.getByText('联网已就绪')).toBeInTheDocument()
    expect(create).not.toHaveBeenCalled()
    fireEvent.click(screen.getByText('联网'))
    expect(screen.getByText('回答不联网')).toBeInTheDocument()
    expect(screen.getByText('联网已关闭；组件闲置 10 分钟后释放。')).toBeInTheDocument()
    expect(connection).toHaveBeenLastCalledWith(expect.any(String), false, 2)
  })

  it('retains another connection and reuses ready state after reopening', async () => {
    const connection = vi.spyOn(api, 'setResearchConnection').mockResolvedValue(ready)
    const view = render(<Harness />)
    fireEvent.click(screen.getByText('联网'))
    await screen.findByText('联网已就绪')
    fireEvent.click(screen.getByText('联网'))
    fireEvent.click(screen.getByText('联网'))
    await screen.findByText('联网已就绪')
    expect(connection.mock.calls.map(([, enabled, sequence]) => [enabled, sequence])).toEqual([[true, 1], [false, 2], [true, 3]])
    view.unmount()
    expect(connection).toHaveBeenLastCalledWith(expect.any(String), false, 4)
  })

  it('renews the lease while enabled and stops renewing immediately after disabling', async () => {
    vi.useFakeTimers()
    const connection = vi.spyOn(api, 'setResearchConnection').mockResolvedValue(ready)
    render(<Harness />)
    await act(async () => fireEvent.click(screen.getByText('联网')))
    await act(async () => vi.advanceTimersByTimeAsync(20000))
    expect(connection.mock.calls.filter(([, enabled]) => enabled)).toHaveLength(2)
    await act(async () => fireEvent.click(screen.getByText('联网')))
    await act(async () => vi.advanceTimersByTimeAsync(600000))
    expect(connection.mock.calls.filter(([, enabled]) => enabled)).toHaveLength(2)
    expect(connection.mock.calls.at(-1)?.[1]).toBe(false)
  })
})
