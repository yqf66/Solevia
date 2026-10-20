import { useEffect, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { ArrowUpRight, Plus } from 'lucide-react'

export function CreatePlanMenu() {
  const navigate = useNavigate()
  const [open, setOpen] = useState(false)
  const root = useRef<HTMLDivElement>(null)
  const trigger = useRef<HTMLButtonElement>(null)

  useEffect(() => {
    if (!open) return
    root.current?.querySelector<HTMLButtonElement>('[role="menuitem"]')?.focus()
    const dismiss = (event: PointerEvent) => {
      if (!root.current?.contains(event.target as Node)) setOpen(false)
    }
    const keydown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') { setOpen(false); trigger.current?.focus() }
      if (event.key === 'Tab') setOpen(false)
      if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
        event.preventDefault()
        const items = [...(root.current?.querySelectorAll<HTMLButtonElement>('[role="menuitem"]') ?? [])]
        const index = items.indexOf(document.activeElement as HTMLButtonElement)
        items[(index + (event.key === 'ArrowDown' ? 1 : -1) + items.length) % items.length]?.focus()
      }
    }
    document.addEventListener('pointerdown', dismiss)
    document.addEventListener('keydown', keydown)
    return () => {
      document.removeEventListener('pointerdown', dismiss)
      document.removeEventListener('keydown', keydown)
    }
  }, [open])

  return <div className="create-plan-menu" ref={root}>
    <button ref={trigger} className="button primary" type="button" aria-label="＋ 创建规划" aria-haspopup="menu" aria-expanded={open} aria-controls={open ? 'create-plan-options' : undefined} onClick={() => setOpen(value => !value)}>
      <Plus size={17} aria-hidden="true" />创建规划
    </button>
    {open && <div id="create-plan-options" className="create-plan-popover" role="menu" aria-label="创建规划">
      <button type="button" role="menuitem" onClick={() => navigate('/quick/new')}><strong>快速安排 <ArrowUpRight size={16} aria-hidden="true" /></strong><span>直接整理今天或近期的事情</span></button>
      <button type="button" role="menuitem" onClick={() => navigate('/new-agent')}><strong>和 Solevia 一起规划 <ArrowUpRight size={16} aria-hidden="true" /></strong><span>聊清目标后选择先体验或系统推进</span></button>
    </div>}
  </div>
}
