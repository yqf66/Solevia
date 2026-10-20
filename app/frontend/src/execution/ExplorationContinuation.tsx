import { useNavigate } from 'react-router-dom'
import type { ExplorationExecutionState } from '../api'
import './ExplorationContinuation.css'

export function ExplorationContinuation({ goalId, state }: { goalId: string; state?: ExplorationExecutionState | null }) {
  const navigate = useNavigate()
  if (!state || state.next_unplanned_week === null) return null
  const pending = state.pending_window_start_week !== null
  return <section className="exploration-continuation" aria-label="探索进展与下一阶段">
    <div><p className="eyebrow">探索式规划 · 已展开到第 {state.generated_through_week} 周 / 共 {state.total_weeks} 周</p>
      <h2>{pending ? '下一阶段草案已保存' : state.current_window_completed ? '当前体验阶段已完成，整条路线尚未结束' : '先体验当前安排，再结合反馈展开后续'}</h2>
      <p>{pending ? `第 ${state.pending_window_start_week}–${state.pending_window_end_week} 周草案尚未接入执行。退出或刷新后可以继续查看，确认后才会更新安排。` : `第 ${state.next_unplanned_week} 周及之后尚未展开。下一阶段会结合执行记录和反馈生成，已有完成记录会保留。`}</p>
    </div>
    <button className="button primary" type="button" onClick={() => navigate(`/goals/${goalId}/plan`)}>{pending ? '查看已保存的下一阶段草案' : state.current_window_completed ? '继续探索下一阶段' : '查看体验路线'}</button>
  </section>
}
