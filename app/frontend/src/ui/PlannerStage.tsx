import { ProcessDetails } from './ProcessDetails'
import { Check } from 'lucide-react'
import { plannerSteps, ASSISTANT_NAME } from './plannerPresentation'

export function PlannerStepper({
  current = 0,
  viewing = current,
  onStepSelect,
  executing = false,
}: {
  current?: number
  viewing?: number
  onStepSelect?: (index: number) => void
  executing?: boolean
}) {
  return (
    <ol className="stepper" aria-label={executing ? '已确认规划与执行入口' : '创建规划进度'}>
      {plannerSteps.map((originalLabel, index) => {
        const label = executing && index === 3 ? '今日执行' : originalLabel
        const canNavigate = index <= current && index !== viewing && onStepSelect
        const content = (
          <>
            <span className="step-dot" aria-hidden="true">{index < current ? <Check size={13} /> : index + 1}</span>
            <span>{label}</span>
          </>
        )
        return (
          <li className={`${index < current ? 'complete' : index === current ? 'current' : ''} ${index === viewing ? 'viewing' : ''}`} key={label}>
            {canNavigate ? (
              <button type="button" onClick={() => onStepSelect(index)} aria-label={`前往“${label}”阶段`}>
                {content}
              </button>
            ) : <div>{content}</div>}
          </li>
        )
      })}
    </ol>
  )
}



export function StageTransition({
  current,
  title,
  detail,
  checkpoints,
  activeIndex,
  progressNote,
  executing = false,
  restoring = false,
}: {
  current: number
  title: string
  detail: string
  checkpoints: string[]
  activeIndex?: number
  progressNote?: string
  executing?: boolean
  restoring?: boolean
}) {
  const stageHeading = restoring
    ? { eyebrow: '同步已保存状态', title: '读取已有内容' }
    : executing
    ? { eyebrow: '正在执行', title: '读取已保存的执行安排' }
    : current === 1
    ? { eyebrow: '第 2 步 · 讨论路线', title: '讨论一级路线' }
    : current === 3
      ? { eyebrow: '第 4 步 · 准备开始', title: '建立执行日历' }
      : { eyebrow: '第 3 步 · 安排时间', title: '安排阶段时间' }

  return (
    <section className="page planner-stage-page transition-page" aria-live="polite">
      <div className="planner-primary-column">
        <header className="planner-stage-header">
          <div className="stage-heading">
            <p className="eyebrow">{stageHeading.eyebrow}</p>
            <h1>{stageHeading.title}</h1>
          </div>
          {!restoring && <PlannerStepper current={current} executing={executing} />}
        </header>
        <div className="stage-main-scroll">
          <div className="transition-card">
            <div className="transition-orbit" aria-hidden="true"><span /><i /></div>
            <p className="eyebrow">{ASSISTANT_NAME} {restoring ? '正在读取' : '正在准备'}</p>
            <h2>{title}</h2>
            <p>{detail}</p>
            <ProcessDetails active title="处理进度"
              steps={activeIndex == null ? [{ label: '请求已提交，正在等待服务端返回结果' }] : checkpoints.slice(0, activeIndex + 1).map((label, index) => ({ label, detail: index < activeIndex ? '已完成' : '处理中' }))}>
              {progressNote && <p>{progressNote}</p>}
              {activeIndex == null && <><small>本次处理环节</small><ol>{checkpoints.map((item) => <li key={item}>{item}</li>)}</ol></>}
            </ProcessDetails>
            <small>你的已确认内容已经保存，可以放心等待。</small>
          </div>
        </div>
      </div>
      <aside className="stage-context-column transition-context" aria-label="已保存的阶段信息">
        <div className="panel">
          <h2>内容已保存</h2>
          <p className="muted">切换阶段不会丢失已经确认的内容。准备完成后，中间工作区会平滑更新。</p>
        </div>
      </aside>
    </section>
  )
}
