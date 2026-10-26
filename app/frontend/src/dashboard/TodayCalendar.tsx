import './TodayCalendar.css'

type Props = { date?: string; pending: number; completed: number; minutes: number }
const weekdays = ['一', '二', '三', '四', '五', '六', '日']

// Construct the calendar from the API's local date, without UTC date shifts.
export function TodayCalendar({ date, pending, completed, minutes }: Props) {
  const parts = date?.split('-').map(Number)
  const current = parts ? new Date(parts[0], parts[1] - 1, parts[2], 12) : null
  const weekday = current ? (current.getDay() + 6) % 7 : -1
  const week = current ? weekdays.map((label, index) => {
    const day = new Date(current)
    day.setDate(current.getDate() - weekday + index)
    return { label, day: day.getDate(), selected: index === weekday }
  }) : []

  return <section className="today-calendar" aria-label="今日概况">
    <header className="today-calendar-header"><span>今日安排</span><span>{current ? `${current.getFullYear()} 年 · ${current.getMonth() + 1} 月` : '正在读取日期'}</span></header>
    <div className="today-calendar-date"><time dateTime={date}>{current ? current.getDate().toString().padStart(2, '0') : '—'}</time><div><strong>{current ? `星期${weekdays[weekday]}` : '正在读取'}</strong><span>把今天，过成想要的样子。</span></div></div>
    <div className="today-calendar-week" aria-label="本周日期">{week.map(day => <div key={day.label} className={day.selected ? 'is-today' : ''} aria-current={day.selected ? 'date' : undefined}><span>{day.label}</span><strong>{day.day}</strong></div>)}</div>
    <dl className="today-calendar-stats"><div><dt>待推进</dt><dd>{date ? pending : '—'}<small>项</small></dd></div><div><dt>预计投入</dt><dd>{date ? minutes : '—'}<small>分钟</small></dd></div><div><dt>已完成</dt><dd>{date ? completed : '—'}<small>项</small></dd></div></dl>
  </section>
}
