import { useRef, useState } from 'react'
import type { FormEvent } from 'react'
import { useNavigate } from 'react-router-dom'
import { initializeGoal } from '../api'
import { detectedTimezone } from '../ui/plannerPresentation'
import { PlannerStepper } from '../ui/PlannerStage'
import { PlanningGuide } from '../ui/PlanningGuide'
import { GoalStartingPoints } from './GoalStartingPoints'
import './CreateGoalPage.css'

export function CreateGoalPage() {
  const navigate = useNavigate()
  const [rawInput, setRawInput] = useState('')
  const [webResearchEnabled, setWebResearchEnabled] = useState(false)
  const [isSubmitting, setIsSubmitting] = useState(false)
  const [error, setError] = useState('')
  const inputRef = useRef<HTMLTextAreaElement>(null)

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (!rawInput.trim() || isSubmitting) return

    setIsSubmitting(true)
    setError('')
    try {
      const workspace = await initializeGoal(rawInput, detectedTimezone(), { webResearchEnabled })
      window.dispatchEvent(new Event('planner:goals-changed'))
      navigate(`/goals/${workspace.goal_id}/clarify`)
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : '暂时无法创建目标。')
      setIsSubmitting(false)
    }
  }

  return (
    <section className="page goal-create-page">
      <PlannerStepper />
      <div className="goal-create-layout"><div className="goal-create-body">
      <h1>你现在最想完成什么？</h1><p className="goal-create-intro">不需要描述得很完整，Solevia 会陪你一起把它想清楚。</p>
      <form className="goal-create-form" onSubmit={handleSubmit}>
        <textarea
          ref={inputRef}
          className="goal-input"
          value={rawInput}
          onChange={(event) => setRawInput(event.target.value)}
          placeholder={'比如：\n我想转行做大模型应用开发，但不知道从哪里开始。\n我想把阳台变成容易维护的小花园。\n我想为全家安排一个能长期坚持的运动计划。'}
          aria-label="目标描述"
          required
        />
        {error && <p className="form-error" role="alert">{error}</p>}
        <label className="plan-research-toggle"><input type="checkbox" checked={webResearchEnabled} onChange={(event) => setWebResearchEnabled(event.target.checked)} />本次规划启用联网检索<span>需要外部资料时使用</span></label>
        <div className="goal-create-footer"><p>我们会先理解目标和路线，不会立刻生成密密麻麻的日程。</p>
          <button className="button primary" type="submit" disabled={isSubmitting || !rawInput.trim()}>
            {isSubmitting ? '正在进入…' : '开始规划 →'}
          </button>
        </div>
      </form>
      {!rawInput && <GoalStartingPoints onChoose={text => { setRawInput(text); inputRef.current?.focus() }} />}
      </div><PlanningGuide kind="goal" /></div>
    </section>
  )
}

