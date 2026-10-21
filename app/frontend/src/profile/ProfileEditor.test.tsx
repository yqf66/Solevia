import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { ProfileEditor } from './ProfileEditor'
import { useLocalProfile } from './localProfile'
import { prepareAvatar } from './avatarImage'

afterEach(() => { cleanup(); localStorage.clear(); window.dispatchEvent(new Event('storage')); vi.restoreAllMocks() })
function Preview() { const profile = useLocalProfile(); return <output aria-label="侧栏昵称">{profile.nickname}</output> }

it('previews a nickname without changing the sidebar until saving and restores it on reopen', () => {
  const view = render(<><ProfileEditor /><Preview /></>)
  fireEvent.change(screen.getByRole('textbox', { name: /昵称/ }), { target: { value: '  向前一步  ' } })
  expect(screen.getByLabelText('侧栏昵称')).toHaveTextContent('本地用户')
  fireEvent.click(screen.getByRole('button', { name: '保存个人资料' }))
  expect(screen.getByLabelText('侧栏昵称')).toHaveTextContent('向前一步')
  view.unmount(); render(<ProfileEditor />)
  expect(screen.getByRole('textbox', { name: /昵称/ })).toHaveValue('向前一步')
})
it('does not allow empty or oversized nicknames', () => {
  render(<ProfileEditor />)
  fireEvent.change(screen.getByRole('textbox', { name: /昵称/ }), { target: { value: '   ' } })
  expect(screen.getByRole('button', { name: '保存个人资料' })).toBeDisabled()
  fireEvent.change(screen.getByRole('textbox', { name: /昵称/ }), { target: { value: '字'.repeat(25) } })
  expect(screen.getByRole('button', { name: '保存个人资料' })).toBeDisabled()
})
it('shows storage failure and keeps the previous saved identity', () => {
  render(<><ProfileEditor /><Preview /></>)
  fireEvent.change(screen.getByRole('textbox', { name: /昵称/ }), { target: { value: '新昵称' } })
  vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { throw new Error('quota') })
  fireEvent.click(screen.getByRole('button', { name: '保存个人资料' }))
  expect(screen.getByRole('alert')).toHaveTextContent('无法保存')
  expect(screen.getByLabelText('侧栏昵称')).toHaveTextContent('本地用户')
})
it('rejects unsupported or oversized images before image decoding', async () => {
  await expect(prepareAvatar(new File(['svg'], 'a.svg', { type: 'image/svg+xml' }))).rejects.toThrow('JPG')
  const file = new File(['png'], 'a.png', { type: 'image/png' })
  Object.defineProperty(file, 'size', { value: 11 * 1024 * 1024 })
  await expect(prepareAvatar(file)).rejects.toThrow('10 MB')
})
