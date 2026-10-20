import { useEffect, useId, useRef, useState } from 'react'
import type { InputHTMLAttributes, KeyboardEvent } from 'react'
import { Clock3, X } from 'lucide-react'
import { DialogBackdrop } from './DialogBackdrop'
import './TimePicker.css'

type Props = Omit<InputHTMLAttributes<HTMLInputElement>, 'type' | 'value' | 'onChange'> & {
  value: string; onChange: (event: { target: { value: string } }) => void
}
const padded = (n: number) => String(n).padStart(2, '0')
function normalizeTime(value: string) {
  const match = value.trim().match(/^(\d{1,2})[:：](\d{1,2})$/)
  if (!match || Number(match[1]) > 23 || Number(match[2]) > 59) return null
  return `${padded(Number(match[1]))}:${padded(Number(match[2]))}`
}

export function TimePicker({ value, onChange, disabled, id, 'aria-label': label, ...props }: Props) {
  const generatedId = useId(), inputId = id ?? generatedId
  const [editor, setEditor] = useState({ source: value, text: value })
  // Reset an in-progress edit when the parent switches to another time value.
  if (editor.source !== value) setEditor({ source: value, text: value })
  const text = editor.source === value ? editor.text : value
  const setText = (next: string) => setEditor({ source: value, text: next })
  const [error, setError] = useState('')
  const [open, setOpen] = useState(false)
  const [draft, setDraft] = useState(value || '09:00')
  const [position, setPosition] = useState({ top: 0, left: 0 })
  const anchor = useRef<HTMLSpanElement>(null)
  const input = useRef<HTMLInputElement>(null)
  function publish(next: string) {
    setText(next); setError(''); onChange({ target: { value: next } })
  }
  function commit() {
    const normalized = normalizeTime(text)
    if (!text || normalized) publish(normalized || '')
    else { setText(value); setError('时间无效，已还原。请输入 00:00–23:59。') }
  }
  function show() {
    const rect = anchor.current!.getBoundingClientRect()
    const height = 420
    setPosition({ left: Math.max(12, Math.min(rect.left, window.innerWidth - 312)), top: Math.max(12, rect.bottom + height + 8 < window.innerHeight ? rect.bottom + 8 : rect.top - height - 8) })
    setDraft(normalizeTime(text) || value || '09:00'); setOpen(true)
  }
  const [hours, minutes] = draft.split(':')
  const title = typeof label === 'string' ? label : '选择时间'
  return <span ref={anchor} className={`time-field${disabled ? ' is-disabled' : ''}`}>
    <input {...props} id={inputId} ref={input} type="text" inputMode="numeric" placeholder="--:--" autoComplete="off" spellCheck={false} aria-label={label} aria-describedby={error ? `${inputId}-error` : props['aria-describedby']} disabled={disabled} value={text} maxLength={5} pattern="([01]?[0-9]|2[0-3])[:：][0-5]?[0-9]" onChange={event => { const next = event.target.value; setText(next); setError(''); if (normalizeTime(next) && next.length === 5) onChange({ target: { value: normalizeTime(next)! } }); else if (!next) onChange({ target: { value: '' } }) }} onBlur={event => { commit(); props.onBlur?.(event) }} onKeyDown={event => { if (event.key === 'ArrowDown' && event.altKey) { event.preventDefault(); show() } if (event.key === 'Enter') { event.preventDefault(); commit() } props.onKeyDown?.(event) }} />
    <button type="button" className="time-field-trigger" disabled={disabled} aria-label={`${title}，打开时间选择器`} aria-haspopup="dialog" aria-expanded={open} onClick={show}><Clock3 size={18} aria-hidden="true" /></button>
    {error && <small id={`${inputId}-error`} className="time-field-error" role="status">{error}</small>}
    {open && !disabled && <DialogBackdrop className="time-picker-backdrop" onDismiss={() => setOpen(false)}><section className="time-picker-panel" role="dialog" aria-modal="true" aria-label={title} style={{ top: position.top, left: position.left }}>
      <header><span>{title}</span><button className="icon-button" type="button" aria-label="关闭时间选择" onClick={() => setOpen(false)}><X size={17} /></button></header>
      <div className="time-picker-preview"><time>{hours}<span>:</span>{minutes}</time><small>24 小时制</small></div>
      <div className="time-picker-columns"><TimeColumn label="小时" count={24} value={Number(hours)} onSelect={hour => setDraft(`${padded(hour)}:${minutes}`)} /><span className="time-picker-separator" aria-hidden="true">:</span><TimeColumn label="分钟" count={60} value={Number(minutes)} onSelect={minute => setDraft(`${hours}:${padded(minute)}`)} /></div>
      <footer><button className="text-button" type="button" onClick={() => setOpen(false)}>取消</button><button className="button primary" type="button" onClick={() => { publish(draft); setOpen(false) }}>确定时间</button></footer>
    </section></DialogBackdrop>}
  </span>
}

function TimeColumn({ label, count, value, onSelect }: { label: string; count: number; value: number; onSelect: (value: number) => void }) {
  const list = useRef<HTMLDivElement>(null)
  useEffect(() => {
    const selected = list.current?.querySelector<HTMLElement>('[aria-selected="true"]')
    if (list.current && selected) {
      list.current.style.scrollBehavior = 'auto'
      list.current.scrollTop = selected.offsetTop - 44
      list.current.style.removeProperty('scroll-behavior')
    }
  }, []) // Only position the initial selection; user scroll remains under their control.
  function onKey(event: KeyboardEvent, index: number) {
    let next: number | undefined
    if (event.key === 'ArrowDown') next = Math.min(count - 1, index + 1)
    if (event.key === 'ArrowUp') next = Math.max(0, index - 1)
    if (event.key === 'Home') next = 0
    if (event.key === 'End') next = count - 1
    if (next === undefined) return
    event.preventDefault(); onSelect(next)
    const option = list.current?.querySelectorAll<HTMLButtonElement>('button')[next]
    option?.focus({ preventScroll: true }); option?.scrollIntoView({ block: 'nearest', behavior: 'auto' })
  }
  return <div className="time-picker-column"><span>{label}</span><div ref={list} role="listbox" aria-label={label}>{Array.from({ length: count }, (_, index) => <button key={index} type="button" role="option" aria-selected={index === value} tabIndex={index === value ? 0 : -1} onClick={() => onSelect(index)} onKeyDown={event => onKey(event, index)}>{padded(index)}</button>)}</div></div>
}
