import { SoleviaMark } from '../brand/SoleviaMark'
import { ASSISTANT_MOTTO } from '../brand/identity'
import './PlanningGuide.css'

export function PlanningGuide({ kind }: { kind: 'quick' | 'goal' }) {
  const quick = kind === 'quick'
  const steps = quick ? [
    ['写下要做的事', '一句话、多件事，都可以。'],
    ['一起整理任务', 'Solevia 帮你拆分，你来调整。'],
    ['确认后再开始', '看过草案，再放进日常安排。'],
  ] : [
    ['先聊清楚目标', '从想法、顾虑和期待开始。'],
    ['找到合适的路线', '比较路径，再决定怎么走。'],
    ['把下一步落到实处', '确认时间与节奏，然后出发。'],
  ]
  return <aside className="planning-guide" aria-label={quick ? '快速安排流程' : '目标规划流程'}>
    <SoleviaMark className="planning-guide-mark" />
    <h2>{quick ? '先写下来，\n再从容安排。' : '远一点的目标，\n从眼前这一步开始。'}</h2>
    <ol>{steps.map(([title, detail]) => <li key={title}><strong>{title}</strong><p>{detail}</p></li>)}</ol>
    <p className="planning-guide-motto">Solevia · 足下<span>{ASSISTANT_MOTTO}</span></p>
  </aside>
}
