import type { UnifiedToday } from '../api'

export type DependencyItem = NonNullable<UnifiedToday['fixed_tasks'][number]['dependencies']>[number]

export function DependencySummary({ dependencies }: { dependencies?: DependencyItem[] }) {
  if (!dependencies?.length) return null
  const blocked = dependencies.some((item) => item.status !== 'COMPLETED')
  return (
    <div className={`task-dependency-note${blocked ? ' blocked' : ' ready'}`}>
      <strong>{blocked ? '前置任务未全部完成' : '前置任务已完成'}</strong>
      <span>{dependencies.map((item) => `${item.title ?? item.reference}${item.status === 'COMPLETED' ? ' · 已完成' : item.status === 'PENDING' ? ' · 待完成' : ' · 无法识别'}`).join('；')}</span>
    </div>
  )
}
