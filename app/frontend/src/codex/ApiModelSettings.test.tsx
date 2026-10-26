import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { ApiComposerSettings, invalidateApiModels, useApiSelectionPending } from './ApiModelSettings'
import { request } from '../api/client'

vi.mock('../api/client', () => ({ request: vi.fn() }))
afterEach(() => { cleanup(); vi.resetAllMocks() })

function Composer() {
  const pending = useApiSelectionPending()
  return <><ApiComposerSettings /><button disabled={pending}>发送测试</button></>
}

it('lists API models, guards sending during switching, keeps the old selection on failure and supports manual fallback', async () => {
  const catalog = { model: 'original', models: [{ id: 'original' }, { id: 'other' }], available: true,
    connection_id: '1'.repeat(64), message: '服务商返回的模型' }
  let finish: ((value: { model: string }) => void) | undefined
  vi.mocked(request).mockResolvedValueOnce(catalog).mockRejectedValueOnce(new Error('正在生成，请稍后切换'))
    .mockImplementationOnce(() => new Promise(resolve => { finish = resolve }))
  render(<Composer />)
  await waitFor(() => expect(screen.getByRole('button', { name: '选择 API 模型' })).toHaveTextContent('original'))
  fireEvent.click(screen.getByRole('button', { name: '选择 API 模型' }))
  fireEvent.change(screen.getByRole('combobox', { name: 'API 模型' }), { target: { value: 'other' } })
  fireEvent.click(screen.getByRole('button', { name: '应用' }))
  await screen.findByText('正在生成，请稍后切换')
  expect(screen.getByRole('button', { name: '选择 API 模型' })).toHaveTextContent('original')
  fireEvent.click(screen.getByRole('button', { name: '应用' }))
  expect(screen.getByRole('button', { name: '发送测试' })).toBeDisabled()
  finish?.({ model: 'other' })
  await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
  expect(screen.getByRole('button', { name: '选择 API 模型' })).toHaveTextContent('other')
  expect(request).toHaveBeenLastCalledWith('/api/settings/model/selection', {
    method: 'PUT', body: JSON.stringify({ model: 'other', connection_id: catalog.connection_id }),
  })
  vi.mocked(request).mockResolvedValueOnce({ ...catalog, model: 'other', models: [], available: false, message: '请手动填写模型 ID' })
  invalidateApiModels()
  await waitFor(() => expect(request).toHaveBeenLastCalledWith('/api/settings/model/catalog'))
  await waitFor(() => expect(screen.getByRole('button', { name: '选择 API 模型' })).toHaveTextContent('other'))
  fireEvent.click(screen.getByRole('button', { name: '选择 API 模型' }))
  expect(screen.getByRole('textbox', { name: 'API 模型 ID' })).toHaveValue('other')
  fireEvent.change(screen.getByRole('textbox', { name: 'API 模型 ID' }), { target: { value: 'custom-model' } })
  vi.mocked(request).mockResolvedValueOnce({ model: 'custom-model' })
  fireEvent.click(screen.getByRole('button', { name: '应用' }))
  await waitFor(() => expect(screen.getByRole('button', { name: '选择 API 模型' })).toHaveTextContent('custom-model'))
})
