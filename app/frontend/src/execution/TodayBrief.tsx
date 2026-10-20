type Props = { goal: string; minutes: number; date: string; milestone: string | null }

export function TodayBrief({ goal, minutes, date, milestone }: Props) {
  return <section className="today-hero" aria-label={`${date}${milestone ? ` · ${milestone}` : ''}`}>
    <div className="today-brief-meta"><h2>今天的重点</h2><span>建议约 <strong>{minutes}</strong> 分钟 · 可分段完成</span></div>
    {goal.length > 90 ? <details className="today-brief-long-goal"><summary aria-label="展开或收起今日重点"><p className="today-primary-goal">{goal}</p><span className="today-brief-expand" aria-hidden="true" /></summary></details> : <p className="today-primary-goal">{goal}</p>}
  </section>
}
