import { useEffect, useRef, useState } from 'react'
import { compareMaterialRevisions, listMaterialRevisions, readMaterial, type MaterialReader, type MaterialRevision, type MaterialDiff } from '../api'

export function MaterialReaderPanel({ goalId, sourceId, onBack }: { goalId: string; sourceId: string; onBack: () => void }) {
  const [versions, setVersions] = useState<MaterialRevision[]>([])
  const [page, setPage] = useState<MaterialReader | null>(null)
  const [base, setBase] = useState('')
  const [diff, setDiff] = useState<MaterialDiff | null>(null)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const requestId = useRef(0)
  async function load(revisionId?: string, offset = 0) {
    const id = ++requestId.current
    setBusy(true); setError(''); setDiff(null)
    try { const result = await readMaterial(goalId, sourceId, revisionId, offset); if (id === requestId.current) setPage(result) }
    catch (caught) { if (id === requestId.current) setError(caught instanceof Error ? caught.message : '资料读取失败') }
    finally { if (id === requestId.current) setBusy(false) }
  }
  useEffect(() => {
    let active = true
    const token = requestId
    void listMaterialRevisions(goalId, sourceId).then((rows) => { if (active) { setVersions(rows); setBase(rows.filter((r) => r.status === 'READY')[1]?.id || '') } }).catch((caught) => { if (active) setError(String(caught)) })
    void load()
    return () => { active = false; token.current++ }
    // The reader resets only when its source changes.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [goalId, sourceId])
  return <section className="material-reader" aria-label="资料全文阅读">
    <div className="material-directory-toolbar"><button type="button" onClick={onBack}>← 返回资料</button><strong>{page?.title || '读取资料…'}</strong></div>
    <div className="material-directory-toolbar"><label>阅读版本 <select aria-label="阅读版本" disabled={busy} value={page?.revision.id || ''} onChange={(event) => { const next = event.target.value; setBase(versions.find((r) => r.status === 'READY' && r.id !== next)?.id || ''); void load(next) }}>{versions.map((r) => <option value={r.id} key={r.id} disabled={r.status !== 'READY'}>版本 {r.revision} · {r.status === 'READY' ? '已解析' : r.status}</option>)}</select></label><small>{page?.total_chars.toLocaleString()} 字符</small></div>
    {!!page?.warnings.length && <div className="form-error">{page.warnings.map((warning, i) => <p key={i}>{warning}</p>)}</div>}
    {!!page?.outline.length && <details className="material-collapsible"><summary>章节目录{page.outline_truncated ? ' · 仅展示前 1000 项' : ''}</summary><div className="material-reader-outline">{page.outline.map((item) => <button key={item.sequence} disabled={busy} type="button" onClick={() => void load(page.revision.id, item.offset)}>{item.title}</button>)}</div></details>}
    {versions.filter((r) => r.status === 'READY').length > 1 && <details className="material-collapsible"><summary>版本对比</summary><div className="material-directory-toolbar"><label>与历史版本对比 <select aria-label="对比版本" value={base} onChange={(event) => setBase(event.target.value)}>{versions.filter((r) => r.status === 'READY' && r.id !== page?.revision.id).map((r) => <option value={r.id} key={r.id}>版本 {r.revision}</option>)}</select></label><button type="button" disabled={busy || !base || base === page?.revision.id} onClick={() => { if (!page) return; setBusy(true); void compareMaterialRevisions(goalId, sourceId, base, page.revision.id).then(setDiff).catch((caught) => setError(String(caught))).finally(() => setBusy(false)) }}>查看变化</button></div>{diff && <><p>新增 {diff.added_lines} 行 · 删除 {diff.removed_lines} 行{diff.truncated ? ' · 当前为部分对比结果' : ''}</p><small>{diff.warning}</small><pre className="material-diff">{diff.lines.map((line, i) => <span className={`material-diff-${line.kind}`} key={i}>{line.kind === 'added' ? '+' : line.kind === 'removed' ? '−' : ' '} {line.text}{'\n'}</span>)}</pre></>}</details>}
    {error && <p className="form-error" role="alert">{error}</p>}
    {busy && <p role="status">正在读取…</p>}
    <pre className="material-reader-text">{page?.text}</pre>
    <div className="material-directory-toolbar"><button type="button" disabled={busy || !page || page.offset === 0} onClick={() => page && void load(page.revision.id, Math.max(0, page.offset - 12000))}>上一段</button><small>字符 {page?.offset || 0}—{page ? page.offset + page.text.length : 0}</small><button type="button" disabled={busy || page?.next_offset == null} onClick={() => page && void load(page.revision.id, page.next_offset!)}>下一段</button></div>
  </section>
}
