import { useCallback, useEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { Plus, X, FolderSearch, FileUp, FolderUp } from 'lucide-react'
import {
  analyzeGoalMaterials, confirmMaterialFindings, createMaterialRoot, importMaterialBatch,
  listGoalMaterials, listMaterialAnalyses, listMaterialRoots, processLocalMaterial,
  refreshLocalMaterial, scanMaterialRoot, unlinkGoalMaterial, uploadMaterialFile,
  indexMaterialSemantics,
  type GoalMaterial, type MaterialAnalysis, type MaterialCitation, type MaterialRoot, type MaterialScan,
} from '../api'
import { MaterialReaderPanel } from './MaterialReaderPanel'
import { MaterialAttachmentPicker, type MaterialWorkspaceProps } from './MaterialAttachmentPicker'

function revisionLabel(material: GoalMaterial): string {
  const revision = material.latest_revision
  if (!revision) return '未建立版本'
  if (revision.status === 'READY') return `已解析${revision.metadata.cache_reused ? ' · 已复用' : ''}${revision.metadata.mineru_used ? ' · MinerU' : revision.metadata.mineru_attempted ? ' · 已回退基础解析' : ''} · ${String(revision.metadata.chunk_count ?? 0)} 个可检索文本块`
  if (revision.status === 'FAILED') return `解析失败 · ${revision.error_message ?? '请重试'}`
  if (revision.status === 'CANCELLED') return '已取消'
  return revision.status === 'PROCESSING' ? '解析中…' : '等待解析'
}

function citationLabel(citations: MaterialCitation[]): string {
  return citations.map((item) => `${item.relative_path || item.source_title || '资料'} · ${item.locator} · 版本 ${item.revision_id.slice(0, 8)}`).join('；')
}

function scanStatus(status: string | null): string {
  return ({ READY: '已解析', FAILED: '解析失败', PROCESSING: '解析中', QUEUED: '等待解析', CANCELLED: '已取消' } as Record<string, string>)[status ?? ''] ?? '待解析'
}

export function MaterialWorkspacePanel(props: MaterialWorkspaceProps) {
  return props.compact ? <MaterialAttachmentPicker {...props} /> : <MaterialLibraryPanel {...props} />
}

function MaterialLibraryPanel({ goalId, compact = false, selectedSourceIds = [], onSelectionChange, disabled = false }: MaterialWorkspaceProps) {
  const [roots, setRoots] = useState<MaterialRoot[]>([])
  const [materials, setMaterials] = useState<GoalMaterial[]>([])
  const [analyses, setAnalyses] = useState<MaterialAnalysis[]>([])
  const [rootName, setRootName] = useState('')
  const [rootPath, setRootPath] = useState('')
  const [selectedRoot, setSelectedRoot] = useState('')
  const [scan, setScan] = useState<MaterialScan | null>(null)
  const [paths, setPaths] = useState<string[]>([])
  const [filter, setFilter] = useState('')
  const [attachmentFilter, setAttachmentFilter] = useState('')
  const [batchErrors, setBatchErrors] = useState<Array<{ relative_path: string; message: string }>>([])
  const [importedCount, setImportedCount] = useState<number | null>(null)
  const [sourceIds, setSourceIds] = useState<string[]>([])
  const [scope, setScope] = useState('all')
  const [tab, setTab] = useState('library')
  const [query, setQuery] = useState('')
  const [busy, setBusy] = useState('')
  const [error, setError] = useState('')
  const [open, setOpen] = useState(false)
  const [readerSource, setReaderSource] = useState<string | null>(null)
  const triggerRef = useRef<HTMLButtonElement>(null)
  const dialogRef = useRef<HTMLDivElement>(null)
  const fileInputRef = useRef<HTMLInputElement>(null)
  const folderInputRef = useRef<HTMLInputElement>(null)
  const selectedSourceIdsRef = useRef(selectedSourceIds)

  useEffect(() => { selectedSourceIdsRef.current = selectedSourceIds }, [selectedSourceIds])

  const reload = useCallback(async () => {
    if (!goalId) return
    const [nextRoots, nextMaterials, nextAnalyses] = await Promise.all([
      listMaterialRoots(), listGoalMaterials(goalId), listMaterialAnalyses(goalId),
    ])
    const safeRoots = Array.isArray(nextRoots) ? nextRoots : []
    const safeMaterials = Array.isArray(nextMaterials) ? nextMaterials : []
    const safeAnalyses = Array.isArray(nextAnalyses) ? nextAnalyses : []
    setRoots(safeRoots); setMaterials(safeMaterials); setAnalyses(safeAnalyses)
    setSelectedRoot((current) => safeRoots.some((root) => root.id === current && root.status === 'ACTIVE') ? current : safeRoots.find((root) => root.status === 'ACTIVE')?.id || '')
    setSourceIds((current) => current.filter((id) => safeMaterials.some((material) => material.source_id === id)))
    if (onSelectionChange && selectedSourceIdsRef.current.length) {
      onSelectionChange(safeMaterials.filter((material) => selectedSourceIdsRef.current.includes(material.source_id)))
    }
  }, [goalId, onSelectionChange])

  useEffect(() => {
    setScan(null); setPaths([]); setSourceIds([]); setScope('all'); setBatchErrors([]); setImportedCount(null)
    void reload().catch((caught) => setError(caught instanceof Error ? caught.message : '资料读取失败'))
  }, [reload])

  useEffect(() => {
    if ((!open && !selectedSourceIds.length) || !materials.some((material) => ['QUEUED', 'PROCESSING'].includes(material.latest_revision?.status ?? ''))) return
    const timer = window.setTimeout(() => {
      void reload().catch((caught) => setError(caught instanceof Error ? caught.message : '解析状态读取失败'))
    }, 1800)
    return () => window.clearTimeout(timer)
  }, [open, materials, reload, selectedSourceIds.length])

  useEffect(() => {
    if (!open) return
    const trigger = triggerRef.current
    dialogRef.current?.focus()
    const previousOverflow = document.body.style.overflow
    document.body.style.overflow = 'hidden'
    function onKey(event: KeyboardEvent) {
      if (event.key === 'Escape') setOpen(false)
      if (event.key !== 'Tab') return
      const elements = Array.from(dialogRef.current?.querySelectorAll<HTMLElement>('button:not(:disabled), input:not(:disabled):not([hidden]), select:not(:disabled), textarea:not(:disabled), [tabindex="0"]') ?? []).filter((element) => element.getClientRects().length)
      if (!elements?.length) return
      const first = elements[0]; const last = elements[elements.length - 1]
      if (event.shiftKey && (document.activeElement === first || document.activeElement === dialogRef.current)) { event.preventDefault(); last.focus() }
      else if (!event.shiftKey && (document.activeElement === last || document.activeElement === dialogRef.current)) { event.preventDefault(); first.focus() }
    }
    document.addEventListener('keydown', onKey)
    return () => { document.body.style.overflow = previousOverflow; document.removeEventListener('keydown', onKey); trigger?.focus() }
  }, [open])

  async function run(action: string, callback: () => Promise<void>) {
    if (busy) return
    setBusy(action); setError('')
    try { await callback(); await reload() }
    catch (caught) { setError(caught instanceof Error ? caught.message : '资料操作未完成') }
    finally { setBusy('') }
  }

  if (!goalId) return null
  const visibleFiles = scan?.files.filter((file) => file.relative_path.toLowerCase().includes(filter.toLowerCase())) ?? []
  const selectable = visibleFiles.filter((file) => file.supported && !file.linked)
  const readyCount = materials.filter((material) => material.latest_revision?.status === 'READY').length
  const pending = materials.filter((material) => ['QUEUED', 'FAILED', 'CANCELLED'].includes(material.latest_revision?.status ?? ''))
  const toggleSource = (id: string) => setSourceIds((current) => current.includes(id) ? current.filter((item) => item !== id) : [...current, id])
  async function uploadSelectedFiles(files: File[]) {
    if (!files.length || !goalId || busy) return
    setBusy('upload'); setError('')
    try {
      const imported: GoalMaterial[] = []
      const errors: Array<{ relative_path: string; message: string }> = []
      const selectedFiles = files.slice(0, 50)
      if (files.length > selectedFiles.length) {
        errors.push({ relative_path: `其余 ${files.length - selectedFiles.length} 个文件`, message: '一次最多选择 50 个文件，请分批上传。' })
      }
      for (const file of selectedFiles) {
        const relativePath = (file as File & { webkitRelativePath?: string }).webkitRelativePath || file.name
        try { imported.push(await uploadMaterialFile(goalId, file, relativePath)) }
        catch (caught) { errors.push({ relative_path: relativePath, message: caught instanceof Error ? caught.message : '文件上传失败' }) }
      }
      setBatchErrors(errors); setImportedCount(imported.length)
      if (onSelectionChange && imported.length) onSelectionChange([...materials.filter((item) => selectedSourceIds.includes(item.source_id)), ...imported])
      await reload()
    } catch (caught) { setError(caught instanceof Error ? caught.message : '文件上传失败') }
    finally { setBusy('') }
  }

  return <section className={`material-workspace-panel${compact ? ' compact' : ''}`} aria-label="本地资料阅读">
    <button ref={triggerRef} className={compact ? 'material-workspace-compact-trigger' : 'material-workspace-toggle'} type="button" disabled={disabled} aria-label="添加或阅读本地资料" title="添加或阅读本地资料" aria-expanded={open} onClick={() => setOpen((value) => !value)}>
      {compact ? <Plus size={18} /> : <span><strong>本地资料阅读</strong><small>{materials.length} 份资料 · {analyses.length} 次分析</small></span>}
    </button>
    {open && createPortal(<div className="material-dialog-backdrop" onMouseDown={(event) => { if (event.target === event.currentTarget) setOpen(false) }}>
      <div className="material-dialog" ref={dialogRef} role="dialog" aria-modal="true" aria-labelledby={`material-title-${goalId}`} tabIndex={-1}>
        <header className="material-dialog-header"><div><h2 id={`material-title-${goalId}`}>规划资料</h2><small>{materials.length} 份资料 · {readyCount} 份已解析</small></div><button type="button" title="关闭资料" aria-label="关闭资料" onClick={() => setOpen(false)}><X size={18} /></button></header>
        {!onSelectionChange && <div className="material-tabs" role="tablist" aria-label="资料视图">{[['library', '资料库'], ['analysis', '阅读分析']].map(([value, label]) => <button key={value} type="button" role="tab" aria-selected={tab === value} onClick={() => setTab(value)}>{label}</button>)}</div>}
        {readerSource ? <div className="material-workspace-body"><MaterialReaderPanel goalId={goalId} sourceId={readerSource} onBack={() => setReaderSource(null)} /></div> : <div className="material-workspace-body">
          {error && <p className="form-error" role="alert">{error}</p>}
          {onSelectionChange && <section className="material-conversation-picker">
            <div className="material-directory-toolbar"><strong>本次对话资料 · 已选 {selectedSourceIds.length} 份</strong><button type="button" onClick={() => setOpen(false)}>完成选择</button></div>
            <div className="material-upload-actions"><button className="button" type="button" disabled={Boolean(busy)} onClick={() => fileInputRef.current?.click()}><FileUp size={16} />选择文件</button><button className="button" type="button" disabled={Boolean(busy)} onClick={() => folderInputRef.current?.click()}><FolderUp size={16} />选择文件夹</button></div>
            <input ref={fileInputRef} type="file" multiple hidden onChange={(event) => { void uploadSelectedFiles(Array.from(event.target.files ?? [])); event.target.value = '' }} />
            <input ref={folderInputRef} type="file" multiple hidden {...({ webkitdirectory: '', directory: '' } as Record<string, string>)} onChange={(event) => { void uploadSelectedFiles(Array.from(event.target.files ?? [])); event.target.value = '' }} />
            <small className="material-help">“文本块”是系统为检索切分的内容单位，不等于页数；文件越长通常会有更多文本块。</small>
            {busy === 'upload' && <p role="status">上传中…</p>}
            {importedCount !== null && <p role="status">已关联 {importedCount} 份资料，分别进入解析队列。</p>}
            {batchErrors.length > 0 && <ul className="form-error" aria-label="导入失败文件">{batchErrors.map((item) => <li key={item.relative_path}>{item.relative_path}：{item.message}</li>)}</ul>}
            <input aria-label="筛选已关联资料" placeholder="搜索资料" value={attachmentFilter} onChange={(event) => setAttachmentFilter(event.target.value)} />
            <div className="material-scope-list">{materials.filter((material) => `${material.title} ${material.relative_path}`.toLowerCase().includes(attachmentFilter.toLowerCase())).map((material) => <label key={material.source_id}>
              <input type="checkbox" aria-label={`附加 ${material.title}`} disabled={material.latest_revision?.status !== 'READY' || disabled} checked={selectedSourceIds.includes(material.source_id)} onChange={() => {
                const ids = selectedSourceIds.includes(material.source_id) ? selectedSourceIds.filter((id) => id !== material.source_id) : [...selectedSourceIds, material.source_id]
                onSelectionChange(materials.filter((item) => ids.includes(item.source_id)))
              }} />
              <span>{material.title}<small>{revisionLabel(material)}{material.latest_revision?.metadata.coverage === 'PARTIAL' ? ' · 部分扫描页未读全' : ''}</small><button type="button" disabled={material.latest_revision?.status !== 'READY'} onClick={(event) => { event.preventDefault(); setReaderSource(material.source_id) }}>阅读全文 / 版本对比</button></span>
            </label>)}</div>
          </section>}
          {onSelectionChange || tab === 'library' ? <>
            <details className="material-collapsible" open={onSelectionChange ? undefined : true}><summary>管理目录和资料</summary>
            <div className="material-root-form">
              <input aria-label="资料目录名称" value={rootName} onChange={(event) => setRootName(event.target.value)} placeholder="目录名称" />
              <input aria-label="资料目录路径" value={rootPath} onChange={(event) => setRootPath(event.target.value)} placeholder="本机资料目录路径" />
              <button type="button" disabled={!rootName.trim() || !rootPath.trim() || Boolean(busy)} onClick={() => void run('root', async () => {
                const root = await createMaterialRoot(rootName.trim(), rootPath.trim()); setSelectedRoot(root.id); setPaths([])
                setScan(await scanMaterialRoot(goalId, root.id)); setRootName(''); setRootPath('')
              })}>{busy === 'root' ? '扫描中…' : '授权并扫描'}</button>
            </div>
            <div className="material-directory-toolbar">
              <select aria-label="选择资料目录" disabled={Boolean(busy)} value={selectedRoot} onChange={(event) => { setSelectedRoot(event.target.value); setScan(null); setPaths([]); setBatchErrors([]); setImportedCount(null) }}>
                <option value="">选择已授权目录</option>{roots.filter((root) => root.status === 'ACTIVE').map((root) => <option value={root.id} key={root.id}>{root.display_name}</option>)}
              </select>
              <button type="button" disabled={!selectedRoot || Boolean(busy)} onClick={() => void run('scan', async () => { setScan(await scanMaterialRoot(goalId, selectedRoot)); setPaths([]) })}><FolderSearch size={16} />{busy === 'scan' ? '扫描中…' : '扫描目录'}</button>
            </div>
            {scan && <div className="material-file-browser">
              <div className="material-directory-toolbar"><input aria-label="筛选目录文件" value={filter} onChange={(event) => setFilter(event.target.value)} placeholder="筛选文件名或路径" /><button type="button" disabled={Boolean(busy) || !selectable.length} onClick={() => setPaths(selectable.slice(0, 500).map((file) => file.relative_path))}>选择可导入文件</button><button type="button" disabled={Boolean(busy) || !paths.length} onClick={() => setPaths([])}>清空选择</button></div>
              <small>{scan.files.length} 个文件 · 已选 {paths.length} 个</small>
              {scan.truncated && <p className="form-error">目录过大或部分子目录无法读取，当前为部分扫描结果。</p>}
              <details className="material-collapsible" open={paths.length > 0}><summary>扫描结果 · {scan.files.length} 个文件 · 已选 {paths.length} 个</summary><ul className="material-scan-list">{visibleFiles.map((file) => <li key={file.relative_path}><label><input type="checkbox" aria-label={`选择 ${file.relative_path}`} checked={paths.includes(file.relative_path)} disabled={!file.supported || file.linked || Boolean(busy) || (paths.length >= 500 && !paths.includes(file.relative_path))} onChange={() => setPaths((current) => current.includes(file.relative_path) ? current.filter((path) => path !== file.relative_path) : [...current, file.relative_path])} /><span>{file.relative_path}<small>{Math.ceil(file.file_size / 1024)} KB · {file.linked ? `已关联 · ${scanStatus(file.revision_status)}` : file.reason || '可导入'}</small></span></label></li>)}</ul></details>
              {!visibleFiles.length && <p>没有匹配的文件</p>}
              <button className="button primary" type="button" disabled={!paths.length || Boolean(busy)} onClick={() => void run('batch', async () => {
                const result = await importMaterialBatch(goalId, selectedRoot, paths); setBatchErrors(result.errors); setImportedCount(result.imported.length)
                setPaths(result.errors.map((item) => item.relative_path)); setScan(await scanMaterialRoot(goalId, selectedRoot))
              })}>{busy === 'batch' ? '导入中…' : `导入并解析${paths.length ? ` ${paths.length} 份` : ''}`}</button>
            </div>}
            {!onSelectionChange && importedCount !== null && <p role="status">已关联 {importedCount} 份资料，分别进入解析队列。</p>}
            {!onSelectionChange && batchErrors.length > 0 && <ul className="form-error" aria-label="导入失败文件">{batchErrors.map((item) => <li key={item.relative_path}>{item.relative_path}：{item.message}</li>)}</ul>}
            {materials.length > 0 && <>
              <div className="material-directory-toolbar"><strong>已关联资料</strong><button type="button" disabled={Boolean(busy) || !pending.length} onClick={() => void run('queue', async () => {
                const results = await Promise.allSettled(pending.map((material) => processLocalMaterial(goalId, material.source_id)))
                const failures = results.filter((result) => result.status === 'rejected').length
                if (failures) setError(`${failures} 份资料未能进入解析队列，请分别重试。`)
              })}>解析待处理资料</button></div>
              <details className="material-collapsible"><summary>已关联资料 · {materials.length} 份 · {readyCount} 份已解析</summary><ul className="material-list">{materials.map((material) => <li key={material.source_id}><div><strong>{material.title}</strong><small>{material.relative_path} · {revisionLabel(material)}</small></div><div className="material-actions">
                <button type="button" disabled={Boolean(busy) || material.latest_revision?.status !== 'READY'} onClick={() => setReaderSource(material.source_id)}>阅读全文 / 版本对比</button>
                <button type="button" disabled={Boolean(busy) || material.latest_revision?.status !== 'READY'} onClick={() => void run(`index-${material.source_id}`, async () => { await indexMaterialSemantics(goalId, material.source_id) })}>{material.latest_revision?.metadata.semantic_status === 'READY' ? '语义索引已就绪' : '建立语义索引'}</button>
                <button type="button" disabled={Boolean(busy) || ['READY', 'PROCESSING'].includes(material.latest_revision?.status ?? '')} onClick={() => void run(`process-${material.source_id}`, async () => { await processLocalMaterial(goalId, material.source_id) })}>{material.latest_revision?.status === 'READY' ? '已解析' : '解析 / 重试'}</button>
                {material.title.toLowerCase().endsWith('.pdf') && <button type="button" disabled={Boolean(busy) || material.latest_revision?.status !== 'READY'} onClick={() => void run(`mineru-${material.source_id}`, async () => { await processLocalMaterial(goalId, material.source_id, 'MINERU') })}>用 MinerU 重解析</button>}
                <button type="button" disabled={Boolean(busy)} onClick={() => void run(`refresh-${material.source_id}`, async () => { await refreshLocalMaterial(goalId, material.source_id); await processLocalMaterial(goalId, material.source_id) })}>更新并解析</button>
                <button type="button" disabled={Boolean(busy)} onClick={() => void run(`unlink-${material.source_id}`, () => unlinkGoalMaterial(goalId, material.source_id))}>移除</button>
              </div></li>)}</ul></details>
            </>}
            </details>
          </> : <>
            <div className="material-directory-toolbar"><label>分析范围 <select aria-label="分析范围" value={scope} disabled={Boolean(busy)} onChange={(event) => setScope(event.target.value)}><option value="all">全部已关联资料</option><option value="selected">选定资料</option></select></label><small>{scope === 'all' ? `${readyCount} 份已解析` : `已选 ${sourceIds.length} 份`}</small></div>
            {scope === 'selected' && <div className="material-scope-list">{materials.map((material) => <label key={material.source_id}><input type="checkbox" disabled={Boolean(busy) || material.latest_revision?.status !== 'READY'} checked={sourceIds.includes(material.source_id)} onChange={() => toggleSource(material.source_id)} />{material.relative_path || material.title}</label>)}</div>}
            <div className="material-analysis-form"><textarea aria-label="资料分析问题" maxLength={2000} value={query} onChange={(event) => setQuery(event.target.value)} placeholder="交付期限、人员投入和验收要求分别是什么？文件之间是否有矛盾？" /><button className="button primary" type="button" disabled={query.trim().length < 2 || Boolean(busy) || !readyCount || (scope === 'selected' && !sourceIds.length)} onClick={() => void run('analyze', async () => { await analyzeGoalMaterials(goalId, query.trim(), { sourceIds: scope === 'selected' ? sourceIds : undefined }); setQuery('') })}>{busy === 'analyze' ? '正在检索并分析…' : '分析资料'}</button></div>
            {analyses.map((analysis) => <article className="material-analysis-result" key={analysis.id}>
              <header><div><strong>{analysis.query}</strong><small>{analysis.file_count ?? '—'} 份文件 · {analysis.source_count} 个片段 · {new Date(analysis.created_at).toLocaleString('zh-CN')}</small></div><span>已完成</span></header>
              <p>{analysis.summary}</p>
              {(analysis.conflicts?.length ?? 0) > 0 && <section className="material-conflicts"><strong>文件冲突 · 待核实</strong>{analysis.conflicts?.map((conflict, index) => <div key={index}><p>{conflict.description}</p><small>{citationLabel(conflict.citations)}</small></div>)}</section>}
              <details className="material-collapsible"><summary>查看分析详情 · {analysis.key_points.length} 个要点 · {analysis.findings.length} 条事实</summary>
                {analysis.key_points.length > 0 && <section><strong>关键要点</strong><ul>{analysis.key_points.map((point, index) => <li key={index}><strong>{point.title}</strong><span>{point.detail}</span><small>{citationLabel(point.citations)}</small></li>)}</ul></section>}
                {analysis.findings.length > 0 && <section><strong>可能影响规划的事实</strong>{analysis.findings.map((finding, index) => <label className={`material-finding material-finding-${finding.status.toLowerCase()}`} key={`${analysis.id}-${index}`}><input type="checkbox" disabled={finding.status !== 'PROPOSED' || !finding.citations.length || Boolean(busy)} onChange={() => void run(`confirm-${analysis.id}-${index}`, async () => { await confirmMaterialFindings(goalId, analysis.id, [index]) })} checked={finding.status === 'CONFIRMED'} /><span>{finding.text}<small>{citationLabel(finding.citations) || '缺少引用'}{finding.status === 'BLOCKED' && ' · 需核实后才能确认'}</small></span>{finding.status === 'CONFIRMED' && <em>已进入规划上下文</em>}</label>)}</section>}
                {analysis.open_questions.length > 0 && <small className="material-open-questions">仍需确认：{analysis.open_questions.join('；')}</small>}
                {!!analysis.retrieval_coverage?.length && <details><summary>检索主题与覆盖</summary><ul>{analysis.retrieval_coverage.map((item, index) => <li key={index}>{item.query} · {item.retrieved_chunks} 个片段</li>)}</ul></details>}
              </details>
            </article>)}
          </>}
        </div>}
      </div>
    </div>, document.body)}
  </section>
}
