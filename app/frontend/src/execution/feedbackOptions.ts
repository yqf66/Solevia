export type FeedbackTarget =
  | { kind: 'TASK'; taskId: string; title: string; status: string; estimateMinutes: number }
  | { kind: 'DAY'; title: string; reason: string }

export type FeedbackChoice = { code: string; label: string; detail: string }

export const taskFeedbackChoices: FeedbackChoice[] = [
  { code: 'TEMPORARY_CONFLICT', label: '临时有事', detail: '今天出现了临时冲突，任务本身没有问题。' },
  { code: 'TOO_HARD', label: '比预计更难', detail: '需要更多时间、拆解或补充解释。' },
  { code: 'NOT_ENOUGH_TIME', label: '预计时间不够', detail: '可用时间比计划少，但仍想继续。' },
  { code: 'UNCLEAR', label: '任务不够清楚', detail: '不知道具体该怎么做或做到什么程度。' },
  { code: 'DO_NOT_WANT', label: '不想继续这项', detail: '希望换方法、缩小范围或重新讨论。' },
  { code: 'OTHER', label: '其他原因', detail: '用一句话告诉 Solevia 实际情况。' },
]

export const dayPaceChoices: FeedbackChoice[] = [
  { code: 'EARLY_FINISH', label: '做得比预计快', detail: '今天提前完成，可能还有余力。' },
  { code: 'TOO_EASY', label: '内容太简单', detail: '掌握程度比计划预估更高。' },
  { code: 'TOO_LIGHT', label: '工作量太少', detail: '难度合适，但今天还可以多做一些。' },
  { code: 'JUST_RIGHT', label: '节奏刚好', detail: '难度和工作量都适合继续保持。' },
  { code: 'TOO_HARD_OVERALL', label: '今天整体偏难', detail: '多个任务理解或执行起来都比较吃力。' },
  { code: 'TOO_HEAVY', label: '今天工作量太多', detail: '内容能完成，但当前容量承受不了。' },
  { code: 'CUSTOM_ADJUSTMENT', label: '我有具体调整建议', detail: '说明希望哪些内容增加、减少、提前或延后。' },
]

export const intentLabels: Record<string, string> = {
  RESCHEDULE_ONLY: '顺延未完成部分',
  RECOVER_ON_REST_DAY: '在最近的非执行日补做',
  SPLIT_TASK: '把任务拆小',
  LOWER_NEAR_TERM: '降低近期强度',
  EXPLAIN_FIRST: '先解释清楚',
  CHANGE_METHOD: '换一种完成方式',
  REDUCE_SCOPE: '缩小这部分',
  SKIP_FOR_NOW: '暂时跳过',
  REVISIT_ROUTE: '重新讨论路线',
  ACCELERATE: '更快完成计划',
  DEEPEN: '把当前内容学得更深入',
  MORE_PRACTICE: '增加练习或应用',
  OBSERVE: '暂时不调整，继续观察',
}

export const intentsByReason: Record<string, string[]> = {
  TEMPORARY_CONFLICT: ['RESCHEDULE_ONLY', 'LOWER_NEAR_TERM'],
  TOO_HARD: ['SPLIT_TASK', 'EXPLAIN_FIRST', 'LOWER_NEAR_TERM'],
  NOT_ENOUGH_TIME: ['RESCHEDULE_ONLY', 'LOWER_NEAR_TERM'],
  UNCLEAR: ['EXPLAIN_FIRST'],
  DO_NOT_WANT: ['CHANGE_METHOD', 'REDUCE_SCOPE', 'SKIP_FOR_NOW', 'REVISIT_ROUTE'],
  OTHER: ['OBSERVE'],
  EARLY_FINISH: ['OBSERVE', 'ACCELERATE', 'MORE_PRACTICE'],
  TOO_EASY: ['OBSERVE', 'ACCELERATE', 'DEEPEN', 'MORE_PRACTICE'],
  TOO_LIGHT: ['OBSERVE', 'ACCELERATE', 'DEEPEN', 'MORE_PRACTICE'],
  JUST_RIGHT: ['OBSERVE'],
  TOO_HARD_OVERALL: ['LOWER_NEAR_TERM', 'OBSERVE'],
  TOO_HEAVY: ['LOWER_NEAR_TERM', 'OBSERVE'],
  CUSTOM_ADJUSTMENT: ['LOWER_NEAR_TERM', 'ACCELERATE', 'DEEPEN', 'MORE_PRACTICE', 'REVISIT_ROUTE'],
}

export const feedbackStatusLabels: Record<string, string> = {
  RECORDED: '已记录，当前不修改计划',
  PENDING_ADJUSTMENT: '已生成调整方案，等待你确认',
  PENDING_RESPONSE: '等待进一步解释',
  NEEDS_DECISION: '需要你决定处理方式',
  RESCHEDULE_WINDOW_UNAVAILABLE: '未来七天内没有可用的正常执行日，任务尚未移动',
  RECOVERY_DAY_UNAVAILABLE: '未来七天内没有可用的非执行日，任务尚未移动',
  NEEDS_GLOBAL_REVIEW: '超出局部调整范围，需要整体复核',
  ADJUSTED: '调整已经应用',
}

export function feedbackLabel(code: string) {
  return [...taskFeedbackChoices, ...dayPaceChoices].find((item) => item.code === code)?.label ?? code
}
