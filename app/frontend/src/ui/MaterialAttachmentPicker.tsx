import { useEffect, useId, useLayoutEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { FileUp, FolderUp, LoaderCircle, Plus } from 'lucide-react'
import { listGoalMaterials, uploadMaterialFile, type GoalMaterial } from '../api'

export type MaterialWorkspaceProps = {
  goalId: string | undefined
  compact?: boolean
  selectedSourceIds?: string[]
  onSelectionChange?: (materials: GoalMaterial[]) => void
  disabled?: boolean
}

export function MaterialAttachmentPicker({ goalId, selectedSourceIds = [], onSelectionChange, disabled = false }: MaterialWorkspaceProps) {
  const [open, setOpen] = useState(false)
  const [busy, setBusy] = useState(false)
  const [notice, setNotice] = useState('')
  const [position, setPosition] = useState({ left: 8, top: 8 })
  const trigger = useRef<HTMLButtonElement>(null)
  const menu = useRef<HTMLDivElement>(null)
  const fileInput = useRef<HTMLInputElement>(null)
  const folderInput = useRef<HTMLInputElement>(null)
  const known = useRef(new Map<string, GoalMaterial>())
  const current = useRef({ goalId, selectedSourceIds, onSelectionChange })
  const uploading = useRef(false)
  const menuId = useId()
  const selectionKey = selectedSourceIds.join(',')
  useLayoutEffect(() => {
    current.current = { goalId, selectedSourceIds, onSelectionChange }
    return () => { current.current = { goalId: undefined, selectedSourceIds: [], onSelectionChange: undefined } }
  }, [goalId, selectedSourceIds, onSelectionChange])

  useEffect(() => {
    if (!open) return
    const anchor = trigger.current
    function place() {
      const bounds = anchor?.getBoundingClientRect()
      if (!bounds) return
      const width = Math.min(200, window.innerWidth - 16)
      const height = menu.current?.offsetHeight || 96
      setPosition({
        left: Math.max(8, Math.min(bounds.left, window.innerWidth - width - 8)),
        top: bounds.top >= height + 16 ? bounds.top - height - 8 : Math.min(bounds.bottom + 8, window.innerHeight - height - 8),
      })
    }
    place()
    menu.current?.querySelector<HTMLButtonElement>('button')?.focus()
    function outside(event: PointerEvent) {
      if (!menu.current?.contains(event.target as Node) && !anchor?.contains(event.target as Node)) setOpen(false)
    }
    function keyboard(event: KeyboardEvent) {
      if (event.key === 'Escape') { event.preventDefault(); setOpen(false); anchor?.focus() }
      if (event.key === 'Tab') setOpen(false)
      if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
        event.preventDefault()
        const buttons = Array.from(menu.current?.querySelectorAll<HTMLButtonElement>('button') ?? [])
        const index = buttons.indexOf(document.activeElement as HTMLButtonElement)
        buttons[(index + (event.key === 'ArrowDown' ? 1 : -1) + buttons.length) % buttons.length]?.focus()
      }
    }
    window.addEventListener('resize', place)
    window.addEventListener('scroll', place, true)
    document.addEventListener('pointerdown', outside)
    menu.current?.addEventListener('keydown', keyboard)
    const panel = menu.current
    return () => {
      window.removeEventListener('resize', place)
      window.removeEventListener('scroll', place, true)
      document.removeEventListener('pointerdown', outside)
      panel?.removeEventListener('keydown', keyboard)
    }
  }, [open])

  useEffect(() => {
    if (!goalId || !selectionKey || !current.current.onSelectionChange) return
    let cancelled = false
    let timer: ReturnType<typeof setTimeout>
    async function refresh() {
      try {
        const materials = await listGoalMaterials(goalId!)
        if (cancelled) return
        materials.forEach((material) => known.current.set(material.source_id, material))
        const selected = materials.filter((material) => current.current.selectedSourceIds.includes(material.source_id))
        current.current.onSelectionChange?.(selected)
        if (selected.some((material) => ['QUEUED', 'PROCESSING'].includes(material.latest_revision?.status ?? ''))) {
          timer = setTimeout(refresh, 1800)
        }
      } catch {
        if (!cancelled) timer = setTimeout(refresh, 3000)
      }
    }
    void refresh()
    return () => { cancelled = true; clearTimeout(timer) }
  }, [goalId, selectionKey])

  async function upload(files: File[]) {
    if (!goalId || !files.length || uploading.current || disabled) return
    uploading.current = true
    setBusy(true); setNotice('')
    const requestGoal = goalId
    const imported: GoalMaterial[] = []
    const errors: string[] = []
    try {
      // Retrieve only when needed to preserve attachments selected before this mount.
      if (current.current.selectedSourceIds.some((id) => !known.current.has(id))) {
        (await listGoalMaterials(requestGoal)).forEach((material) => known.current.set(material.source_id, material))
      }
      if (files.length > 50) errors.push(`其余 ${files.length - 50} 个文件请分批添加`)
      for (const file of files.slice(0, 50)) {
        if (current.current.goalId !== requestGoal) return
        try {
          imported.push(await uploadMaterialFile(requestGoal, file, file.webkitRelativePath || file.name))
        } catch (caught) {
          errors.push(`${file.name}：${caught instanceof Error ? caught.message : '上传失败'}`)
        }
      }
      if (current.current.goalId !== requestGoal) return
      imported.forEach((material) => known.current.set(material.source_id, material))
      if (imported.length) {
        const ids = new Set([...current.current.selectedSourceIds, ...imported.map((material) => material.source_id)])
        current.current.onSelectionChange?.([...ids].flatMap((id) => known.current.get(id) ? [known.current.get(id)!] : []))
      }
      setNotice(errors.length ? `${errors[0]}${errors.length > 1 ? `；另有 ${errors.length - 1} 项未添加` : ''}` : '')
    } catch (caught) {
      if (current.current.goalId === requestGoal) setNotice(caught instanceof Error ? caught.message : '文件上传失败，请重试')
    } finally {
      uploading.current = false
      setBusy(false)
    }
  }

  if (!goalId) return null
  return <div className="material-workspace-panel compact material-attachment-picker">
    <button ref={trigger} type="button" className="material-workspace-compact-trigger" aria-label="添加或阅读本地资料" title="添加文件" aria-haspopup="menu" aria-expanded={open} aria-controls={open ? menuId : undefined} disabled={disabled || busy} onClick={() => setOpen((value) => !value)}>
      {busy ? <LoaderCircle size={16} className="material-upload-spinner" /> : <Plus size={18} />}
    </button>
    <input ref={fileInput} type="file" aria-label="选择附件文件" multiple hidden onChange={(event) => { void upload(Array.from(event.target.files ?? [])); event.target.value = '' }} />
    <input ref={folderInput} type="file" aria-label="选择附件文件夹" multiple hidden {...({ webkitdirectory: '', directory: '' } as Record<string, string>)} onChange={(event) => { void upload(Array.from(event.target.files ?? [])); event.target.value = '' }} />
    {busy && <span className="material-upload-notice" role="status">正在添加文件…</span>}
    {notice && <span className="material-upload-notice form-error" role="alert">{notice}</span>}
    {open && createPortal(<div ref={menu} id={menuId} className="material-upload-menu" role="menu" aria-label="添加资料" style={position}>
      <button type="button" role="menuitem" onClick={() => { setOpen(false); fileInput.current?.click(); trigger.current?.focus() }}><FileUp size={17} />选择文件</button>
      <button type="button" role="menuitem" onClick={() => { setOpen(false); folderInput.current?.click(); trigger.current?.focus() }}><FolderUp size={17} />选择文件夹</button>
    </div>, document.body)}
  </div>
}
