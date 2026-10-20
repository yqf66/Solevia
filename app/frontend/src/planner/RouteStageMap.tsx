import './RouteStageMap.css'

type Stage = { id: string; order: number; title: string }

export function RouteStageMap({ stages }: { stages: Stage[] }) {
  function reveal(id: string) {
    const target = document.getElementById(`route-stage-${id}`)
    target?.scrollIntoView({ behavior: window.matchMedia?.('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth', block: 'start' })
    target?.focus({ preventScroll: true })
  }
  return <nav className="route-stage-map" aria-label="路线阶段导航">
    <header><strong>一步一步，走向目标</strong><span>{stages.length} 个阶段 · 点击查看</span></header>
    <ol>{stages.map(stage => <li key={stage.id}><button type="button" onClick={() => reveal(stage.id)}><span>{stage.order}</span><strong>{stage.title}</strong></button></li>)}</ol>
  </nav>
}
