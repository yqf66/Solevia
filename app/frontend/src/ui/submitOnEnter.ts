import type { KeyboardEvent } from 'react'

// A Chinese IME uses Enter to choose a character, so it must never send a draft.
export function submitOnEnter(event: KeyboardEvent<HTMLTextAreaElement>) {
  if (event.key !== 'Enter' || event.shiftKey || event.ctrlKey || event.altKey || event.metaKey
    || event.nativeEvent.isComposing || event.nativeEvent.keyCode === 229) return
  event.preventDefault()
  const form = event.currentTarget.form
  const submit = form?.querySelector<HTMLButtonElement>('button[type="submit"]')
  if (form && submit && !submit.disabled && !event.repeat) form.requestSubmit(submit)
}
