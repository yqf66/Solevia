import { CheckCircle2, LoaderCircle } from 'lucide-react'
import type { useWebResearch } from './useWebResearch'
import './ResearchProgress.css'

const labels: Record<string, string> = { search_reader: '搜索与网页读取', bge: 'BGE 排序' }

export function ResearchConnectionStatus({ connection }: { connection: ReturnType<typeof useWebResearch> }) {
  if (!connection.enabled) return connection.everEnabled
    ? <p className="research-connection" role="status">联网已关闭；组件闲置 10 分钟后释放。</p> : null
  const components = Object.entries(connection.readiness?.components ?? {})
  const ready = connection.readiness?.ready === true
  const completed = components.filter(([, state]) => state === 'READY').length
  return <div className="research-connection" role="status" aria-live="polite">
    {ready ? <CheckCircle2 size={15} aria-hidden="true" /> : <LoaderCircle size={15} className="research-connection-spinner" aria-hidden="true" />}
    <span>{ready ? '联网已就绪' : connection.error || '正在准备联网服务…'}
      {!ready && !connection.error && <small> {components.length ? `${completed}/${components.length} 项已就绪 · ` : ''}{connection.elapsed} 秒</small>}
    </span>
    {!ready && components.length > 0 && <small>{components.map(([name, state]) => `${labels[name] || name}：${state === 'READY' ? '已就绪' : state === 'UNAVAILABLE' ? '等待恢复' : '准备中'}`).join(' · ')}</small>}
    {connection.error && <button type="button" onClick={connection.retry}>重试</button>}
  </div>
}
