// Only these host-authored messages may bypass the diagnostic redaction.
// Upstream text (including endpoints, credentials and file paths) is never used.
const reasons: Record<string, [string, string]> = {
  CODEX_QUOTA_EXHAUSTED: ['Codex 额度已用完', 'Codex 可用额度已用完。请等待额度恢复，或打开模型连接切换到自配置 API 后继续。'],
  CODEX_RATE_LIMITED: ['暂时受到限流', 'Codex 请求过于频繁，暂时受到限流。请稍后重试，或切换模型后继续。'],
  CODEX_MODEL_BUSY: ['模型当前繁忙', 'Codex 所选模型当前繁忙、可用容量不足。请稍后重试，或在对话框更换模型。'],
  CODEX_NETWORK_ERROR: ['模型网络连接不稳定', 'Codex 无法稳定连接模型服务。请检查网络、代理或服务可用状态，恢复后重试。'],
  CODEX_SERVICE_ERROR: ['模型服务暂时异常', 'Codex 模型服务暂时异常。请稍后重试，或打开模型连接切换到自配置 API。'],
  CODEX_LOGIN_REQUIRED: ['需要重新登录 Codex', 'Codex 登录已失效或尚未登录。请先在 Codex 完成登录，再到模型连接点击「检测并连接」。'],
  CODEX_ACCESS_DENIED: ['需要检查访问权限', 'Codex 当前没有访问权限。请检查账号、模型权限或程序拦截记录；文件权限需由你明确授权。'],
  CODEX_MODEL_UNAVAILABLE: ['当前模型不可用', '所选模型在当前 Codex 连接中不可用，请在对话框切换模型后重试。'],
  CODEX_CONTEXT_TOO_LONG: ['本次内容超出模型容量', '本次内容超过 Codex 模型的上下文容量。请缩小本次调整范围、减少所选附件，或更换容量更大的模型。'],
  CODEX_INVALID_REQUEST: ['需要检查模型兼容性', '当前 Codex 版本或模型不接受这次请求。请更新 Codex 或更换模型后重试。'],
  CODEX_NOT_FOUND: ['尚未找到 Codex', '尚未找到本机 Codex。请先安装 Codex，再到模型连接点击「检测并连接」。'],
  CODEX_DISCONNECTED: ['本机 Codex 连接已中断', 'Codex 本机进程连接已中断。请到模型连接点击「检测并连接」，连接恢复后继续。'],
  CODEX_NOT_CONNECTED: ['先连接 Codex 或 API', 'Codex 尚未连接。请先在模型连接中连接本机 Codex，或配置 API 后继续。'],
  CODEX_TIMEOUT: ['本次等待已到上限', 'Codex 本次生成已达到等待上限，已停止等待并尝试中断本次请求。请稍后重试，或调整模型与思考强度。'],
  CODEX_QUEUE_TIMEOUT: ['本次排队已到上限', 'Codex 正在处理其他请求，本次排队等待已到上限。请等待其他请求结束后重试。'],
  CODEX_INTERRUPTED: ['本次生成已中断', 'Codex 本次生成已被中断。请确认当前进度后重新发起未完成部分。'],
  CODEX_SAFETY_BLOCKED: ['需要调整本次内容', 'Codex 未能处理本次内容。请调整问题或附件范围后重试。'],
  CODEX_EMPTY_RESPONSE: ['本次没有返回可用内容', 'Codex 本次没有返回可用内容。请稍后重试或在对话框更换模型。'],
  CODEX_VALIDATION_EXHAUSTED: ['本次内容未通过校验', 'Codex 返回的内容经有限修复后仍未通过校验。请重试或更换模型；系统不会放宽日期、容量和任务完整性规则。'],
  CODEX_OUTCOME_UNKNOWN: ['请先核实文件操作结果', '生成中断前已发起文件操作，结果需要核实。请先查看对话中的文件与确认状态，再决定是否重试。'],
  CODEX_GENERATION_UNAVAILABLE: ['本次生成未完成', 'Codex 本次生成未完成。请稍后重试；如持续出现，请在模型连接检查本机 Codex 或切换到自配置 API。'],
}

export function codexFailureForCode(code?: string) {
  const reason = code ? reasons[code] : undefined
  return reason ? { title: reason[0], message: reason[1] + '已保存的输入和计划进度会保留。' } : undefined
}

export function codexFailureForMessage(message: string) {
  const code = Object.keys(reasons).find(key => message.startsWith(reasons[key][1].split('。')[0] + '。'))
  return codexFailureForCode(code)
}
