import { type GoalListItem } from '../api'
import { normalizeAssistantName } from '../brand/identity'

export { ASSISTANT_NAME } from '../brand/identity'

export function displayMessageContent(content: string, failed = false) {
  content = normalizeAssistantName(content)
  const looksLikeInternalFailure = /模型暂时没有返回|模型服务|结构化结果|ValidationError|Schema 校验|HTTP \d|API 配置|中转站|provider|gateway/i.test(content)
  // Technical terms can be part of a successful answer (for example Agent
  // provider boundaries). Only redact diagnostics on an actually failed reply.
  if (failed) return looksLikeInternalFailure
    ? 'Solevia 暂时没有完成这次回复，你刚才的内容已经保存，可以直接重试。'
    : content
  return content
    .replace(/现在六个核心需求点都已明确，可以进入需求审核环节了。/g, '我已经把这些内容整理好了，你可以在下面检查并确认。')
    .replace(/六项核心需求(?:已经)?(?:全部)?(?:齐全|明确|完整)/g, '目标和现实条件已经整理完整')
    .replace(/需求审核环节/g, '目标确认')
}

export const plannerSteps = ['明确需求', '讨论路线', '安排时间', '准备开始']

export const factLabels: Record<string, string> = {
  objective: '目标',
  current_state: '当前基础',
  desired_outcomes: '预期成果',
  success_criteria: '完成标准',
  available_capacity: '可用时间',
  deadline: '目标时间',
  motivation: '为什么想做',
  scope_in: '包含内容',
  scope_out: '暂不包含',
  constraints: '现实限制',
  preferences: '偏好',
  existing_resources: '已有资源',
  risks: '需要留意',
}





export const statusLabels: Record<string, string> = {
  CLARIFYING: '正在一起梳理',
  REQUIREMENT_REVIEW: '等你确认目标',
  ROUTE_DRAFTING: '正在整理路线',
  ROUTE_REVIEW: '等你确认路线',
  PLAN_DRAFTING: '正在安排时间',
  PLAN_REVIEW: '等你确认时间',
  READY_TO_START: '准备开始',
  ACTIVE: '正在执行',
  RECOVERY_REQUIRED: '等待恢复',
  PAUSED: '已暂停',
  COMPLETED: '已完成，待收官',
  ARCHIVED: '已归档',
}

export const scheduleTypeLabels = { OUTCOME: '成果驱动', RHYTHM: '频率驱动', CALENDAR: '期限驱动' } as const

export function goalPath<T extends Pick<GoalListItem, 'id' | 'status'>>(goal: T) {
  if (goal.status === 'ROUTE_DRAFTING' || goal.status === 'ROUTE_REVIEW') {
    return `/goals/${goal.id}/route`
  }
  if (goal.status === 'PLAN_DRAFTING' || goal.status === 'PLAN_REVIEW') {
    return `/goals/${goal.id}/plan`
  }
  if (goal.status === 'READY_TO_START') return `/goals/${goal.id}/prepare`
  if (goal.status === 'ACTIVE') return `/goals/${goal.id}/today`
  if (goal.status === 'RECOVERY_REQUIRED') return `/goals/${goal.id}/progress?recovery=1`
  if (goal.status === 'PAUSED') return `/goals/${goal.id}/today`
  if (goal.status === 'COMPLETED' || goal.status === 'ARCHIVED') return `/goals/${goal.id}/complete`
  return `/goals/${goal.id}/clarify`
}

export function detectedTimezone() {
  return Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC'
}

export const holidayCountryNames: Record<string, string> = {
  CN: '中国大陆', JP: '日本', KR: '韩国', SG: '新加坡', US: '美国', GB: '英国',
  DE: '德国', FR: '法国', CA: '加拿大', AU: '澳大利亚', IN: '印度',
}

export function detectedHolidayRegion() {
  const locale = Intl.DateTimeFormat().resolvedOptions().locale || navigator.language
  return { timezone: detectedTimezone(), locale }
}

export function holidayCountryName(code: string) {
  if (holidayCountryNames[code]) return holidayCountryNames[code]
  try {
    return new Intl.DisplayNames(['zh-CN'], { type: 'region' }).of(code) || code
  } catch { return code }
}

export const weekdayLabels = ['周一', '周二', '周三', '周四', '周五', '周六', '周日']

export function dateInputValue(value = new Date()) {
  const year = value.getFullYear()
  const month = String(value.getMonth() + 1).padStart(2, '0')
  const day = String(value.getDate()).padStart(2, '0')
  return `${year}-${month}-${day}`
}

export function dateInputInTimezone(timezone: string, days = 0) {
  const parts = new Intl.DateTimeFormat('en', {
    timeZone: timezone, year: 'numeric', month: '2-digit', day: '2-digit',
  }).formatToParts(new Date())
  const part = (kind: Intl.DateTimeFormatPartTypes) => parts.find(item => item.type === kind)?.value ?? ''
  const value = new Date(`${part('year')}-${part('month')}-${part('day')}T12:00:00Z`)
  value.setUTCDate(value.getUTCDate() + days)
  return value.toISOString().slice(0, 10)
}

export function shiftedDateInput(days: number) {
  const value = new Date()
  value.setDate(value.getDate() + days)
  return dateInputValue(value)
}

export function friendlyDate(value: string) {
  const parsed = new Date(value.includes('T') ? value : `${value}T12:00:00`)
  if (Number.isNaN(parsed.getTime())) return value
  return new Intl.DateTimeFormat('zh-CN', {
    month: 'long', day: 'numeric', weekday: 'short',
  }).format(parsed)
}

export function parseLocalDate(value: string) {
  return new Date(`${value}T00:00:00`)
}
