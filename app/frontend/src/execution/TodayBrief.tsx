type Props = { goal: string; minutes: number; date: string; milestone: string | null }

export function TodayBrief({ goal, minutes, date, milestone }: Props) {
  return <section className="today-hero">
    <div className="today-brief-meta"><h2>今天的重点</h2><span>{date}{milestone ? ` · ${milestone}` : ''}</span></div>
    <p className="today-primary-goal">{goal}</p>
    <p className="today-brief-time">唯一主目标<span>建议约 <strong>{minutes}</strong> 分钟，可分段完成</span></p>
  </section>
}
