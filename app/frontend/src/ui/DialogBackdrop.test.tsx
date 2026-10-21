import { useState } from 'react'
import { cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { DialogBackdrop } from './DialogBackdrop'
import { useConfirmation } from './useConfirmation'

afterEach(cleanup)

it('cancels confirmations outside or with Escape and only confirms an explicit action', async () => {
  const remove = vi.fn()
  function Demo() {
    const { confirm, confirmationDialog } = useConfirmation()
    return <><button onClick={async () => { if (await confirm({ title: '删除草案', message: '这会删除草案。' })) remove() }}>删除</button>{confirmationDialog}</>
  }
  render(<Demo />)
  const trigger = screen.getByRole('button', { name: '删除' })
  trigger.focus(); fireEvent.click(trigger)
  const dialog = screen.getByRole('alertdialog')
  expect(within(dialog).getByRole('button', { name: '取消' })).toHaveFocus()
  fireEvent.mouseDown(dialog)
  expect(dialog).toBeInTheDocument()
  fireEvent.mouseDown(dialog.parentElement!)
  expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument()
  expect(trigger).toHaveFocus()
  fireEvent.click(trigger); fireEvent.keyDown(document, { key: 'Escape' })
  expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument()
  expect(remove).not.toHaveBeenCalled()
  fireEvent.click(trigger); fireEvent.click(screen.getByRole('button', { name: '确认删除' }))
  await Promise.resolve()
  expect(remove).toHaveBeenCalledTimes(1)
})

it('dismisses only the top dialog and keeps Tab inside it', () => {
  function Demo() {
    const [nested, setNested] = useState(true)
    return <DialogBackdrop onDismiss={() => {}}><section role="dialog" aria-label="底层"><button>底层按钮</button>
      {nested && <DialogBackdrop onDismiss={() => setNested(false)}><section role="dialog" aria-label="上层"><button>第一个</button><button>最后一个</button></section></DialogBackdrop>}
    </section></DialogBackdrop>
  }
  render(<Demo />)
  screen.getByRole('button', { name: '最后一个' }).focus()
  fireEvent.keyDown(document, { key: 'Tab' })
  expect(screen.getByRole('button', { name: '第一个' })).toHaveFocus()
  fireEvent.keyDown(document, { key: 'Escape' })
  expect(screen.queryByRole('dialog', { name: '上层' })).not.toBeInTheDocument()
  expect(screen.getByRole('dialog', { name: '底层' })).toBeInTheDocument()
})
