const startingPoints = [
  ['学习与成长', '我想学会一项新技能，但不知道从哪里开始。'],
  ['工作与转型', '我想为下一步职业发展做准备，需要梳理一条适合自己的路线。'],
  ['生活与习惯', '我想让运动成为能长期坚持的习惯，需要一个现实可行的计划。'],
] as const

export function GoalStartingPoints({ onChoose }: { onChoose: (text: string) => void }) {
  return <div className="goal-starting-points"><span>还没想好怎么说？从一个方向开始</span>{startingPoints.map(([label, text]) => <button key={label} type="button" onClick={() => onChoose(text)}>{label} ↗</button>)}</div>
}
