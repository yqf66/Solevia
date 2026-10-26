import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { ConnectionAwareError } from './ConnectionAwareError'

afterEach(cleanup)

it('gives a compact model warning and opens the connection settings without sending anything', () => {
  const open = vi.fn()
  window.addEventListener('solevia:open-model-settings', open)
  render(<ConnectionAwareError message="还没有连接模型。请先配置 API 或连接本机 Codex，再开始对话。" />)
  expect(screen.getByRole('alert')).toHaveClass('model-connection-reply')
  fireEvent.click(screen.getByRole('button', { name: '打开模型连接 →' }))
  expect(open).toHaveBeenCalledOnce()
  window.removeEventListener('solevia:open-model-settings', open)
})

it('only retries a saved initial question when the user asks to retry', () => {
  const retry = vi.fn()
  render(<ConnectionAwareError message="Codex 尚未连接。请先连接本机 Codex，或配置 API 后再发送。" onRetry={retry} />)
  expect(retry).not.toHaveBeenCalled()
  fireEvent.click(screen.getByRole('button', { name: '已配置，重试' }))
  expect(retry).toHaveBeenCalledOnce()
})

it('keeps unrelated failures distinct from connection setup', () => {
  render(<ConnectionAwareError message="内容已发生变化，请刷新后再试。" />)
  expect(screen.getByRole('alert')).toHaveTextContent('内容已发生变化')
  expect(screen.queryByRole('button')).not.toBeInTheDocument()
})
