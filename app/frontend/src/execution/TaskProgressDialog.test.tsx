import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { recordPlanTaskProgress, type TodayWorkspace } from '../api'
import { TaskProgressDialog, type TaskProgressTarget } from './TaskProgressDialog'

vi.mock('../api', () => ({ recordPlanTaskProgress: vi.fn() }))
afterEach(() => { cleanup(); vi.resetAllMocks() })
const target: TaskProgressTarget = { id: 'task', title: '阅读专项练习', estimate_minutes: 10, action: 'PARTIAL' }
const workspace = { today: '2026-10-08' } as TodayWorkspace
function props(selected = target) { return { goalId: 'goal', target: selected, disabled: false, onClose: vi.fn(), onSaved: vi.fn() } }

it('opens without native prompts and records validated minutes only after confirmation', async () => {
  vi.mocked(recordPlanTaskProgress).mockResolvedValue(workspace)
  const prompt = vi.spyOn(window, 'prompt').mockImplementation(() => { throw new Error('Native dialog unsupported') })
  const callbacks = props()
  render(<TaskProgressDialog {...callbacks} />)
  const input = screen.getByLabelText('本次投入（分钟）')
  expect(input).toHaveValue(10)
  expect(recordPlanTaskProgress).not.toHaveBeenCalled()
  for (const value of ['', '0', '3.5', '1441']) {
    fireEvent.change(input, { target: { value } })
    expect(screen.getByRole('button', { name: '保存进度' })).toBeDisabled()
  }
  fireEvent.change(input, { target: { value: '5' } })
  fireEvent.change(screen.getByLabelText('进展说明（可选）'), { target: { value: ' 完成前半部分 ' } })
  fireEvent.click(screen.getByRole('button', { name: '保存进度' }))
  await waitFor(() => expect(callbacks.onSaved).toHaveBeenCalledWith(workspace, expect.stringContaining('5 分钟')))
  expect(recordPlanTaskProgress).toHaveBeenCalledWith('goal', 'task', { action: 'PARTIAL', amount: 5, unit: 'MINUTES', reason: '完成前半部分' })
  expect(prompt).not.toHaveBeenCalled()
  prompt.mockRestore()
})

it.each(['DEFER', 'SKIP'] as const)('requires a reason and confirms %s without marking completion', async (action) => {
  vi.mocked(recordPlanTaskProgress).mockResolvedValue(workspace)
  const callbacks = props({ ...target, action })
  render(<TaskProgressDialog {...callbacks} />)
  expect(screen.getByRole('button', { name: '确认记录' })).toBeDisabled()
  fireEvent.change(screen.getByLabelText('原因'), { target: { value: '   ' } })
  expect(screen.getByRole('button', { name: '确认记录' })).toBeDisabled()
  fireEvent.change(screen.getByLabelText('原因'), { target: { value: '临时会议' } })
  fireEvent.click(screen.getByRole('button', { name: '确认记录' }))
  await waitFor(() => expect(callbacks.onSaved).toHaveBeenCalled())
  expect(recordPlanTaskProgress).toHaveBeenCalledWith('goal', 'task', { action, reason: '临时会议' })
})

it('cancel makes no request; failed save retains inputs and permits retry', async () => {
  vi.mocked(recordPlanTaskProgress).mockRejectedValueOnce(new Error('暂时无法保存')).mockResolvedValue(workspace)
  const callbacks = props()
  render(<TaskProgressDialog {...callbacks} />)
  fireEvent.click(screen.getByRole('button', { name: '取消' }))
  expect(callbacks.onClose).toHaveBeenCalledOnce()
  expect(recordPlanTaskProgress).not.toHaveBeenCalled()
  fireEvent.change(screen.getByLabelText('本次投入（分钟）'), { target: { value: '7' } })
  fireEvent.click(screen.getByRole('button', { name: '保存进度' }))
  await screen.findByRole('alert')
  expect(screen.getByLabelText('本次投入（分钟）')).toHaveValue(7)
  fireEvent.click(screen.getByRole('button', { name: '保存进度' }))
  await waitFor(() => expect(callbacks.onSaved).toHaveBeenCalled())
  expect(recordPlanTaskProgress).toHaveBeenCalledTimes(2)
})

it('blocks duplicate submissions while saving', async () => {
  let release!: (workspace: TodayWorkspace) => void
  vi.mocked(recordPlanTaskProgress).mockImplementation(() => new Promise((resolve) => { release = resolve }))
  const callbacks = props()
  render(<TaskProgressDialog {...callbacks} />)
  const form = screen.getByRole('dialog')
  fireEvent.submit(form)
  fireEvent.submit(form)
  expect(recordPlanTaskProgress).toHaveBeenCalledTimes(1)
  expect(screen.getByRole('button', { name: '正在保存…' })).toBeDisabled()
  release(workspace)
  await waitFor(() => expect(callbacks.onSaved).toHaveBeenCalledOnce())
})
