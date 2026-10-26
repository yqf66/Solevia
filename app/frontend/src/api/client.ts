import { usesCodexKernel } from './kernelMode'
import { clearLocalSession, localSession } from './localSession'


export async function request<T>(path: string, options?: RequestInit, timeoutMs?: number): Promise<T> {
  const controller = new AbortController()
  const callerSignal = options?.signal
  const forwardAbort = () => controller.abort(callerSignal?.reason)
  if (callerSignal?.aborted) forwardAbort()
  else callerSignal?.addEventListener('abort', forwardAbort, { once: true })
  const generation = /\/(?:route|plan)\/(?:draft|revise|explain|details|adjustment\/(?:preview|apply))|\/recovery\/ai|\/messages(?:\/retry)?$|\/discussion\/messages$/.test(path)
  const codexGeneration = generation && usesCodexKernel()
  const limit = codexGeneration ? Math.max(timeoutMs ?? 0, 21600000)
    : timeoutMs ?? (generation ? 600000 : (!options?.method || options.method === 'GET') ? 45000 : 180000)
  let timedOut = false
  const timer = setTimeout(() => { timedOut = true; controller.abort() }, limit)
  try {
    return await requestWithoutDeadline<T>(path, { ...options, signal: controller.signal })
  } catch (error) {
    if (timedOut) throw new Error('请求等待超时。服务端仍可能处理中，请刷新查看结果后再重试，避免重复提交。')
    if (error instanceof TypeError && !callerSignal?.aborted) throw new Error('连接暂时中断，请检查网络或服务状态。已提交的操作请先刷新确认结果。')
    throw error
  } finally {
    clearTimeout(timer)
    callerSignal?.removeEventListener('abort', forwardAbort)
  }
}

async function requestWithoutDeadline<T>(path: string, options?: RequestInit): Promise<T> {
  if (!path.startsWith('/api/') || path.startsWith('//') || /[\\\r\n]/.test(path)) {
    throw new Error('只允许访问当前工作台的本地接口。')
  }
  const headers = new Headers(options?.headers)
  if (!headers.has('Content-Type')) headers.set('Content-Type', 'application/json')
  headers.set('X-Solevia-Token', await localSession(options?.signal ?? undefined))
  const response = await fetch(path, {
    ...options,
    credentials: 'same-origin',
    redirect: 'error',
    headers,
  })

  if (!response.ok) {
    if (response.status === 401) {
      clearLocalSession()
      // Never replay a mutation after a service restart.
      throw new Error('本地服务会话已更新，请刷新工作台后继续。已提交的操作请先确认结果。')
    }
    let detail: unknown
    try {
      const payload = await response.json() as { detail?: unknown }
      detail = payload.detail
    } catch {
      detail = null
    }
    if (detail && typeof detail === 'object' && (detail as { code?: unknown }).code === 'MODEL_CONNECTION_REQUIRED') {
      const error = new Error(String((detail as { message?: unknown }).message || '还没有连接模型。请先配置 API 或连接本机 Codex。')) as Error & { code: string }
      error.code = 'MODEL_CONNECTION_REQUIRED'
      throw error
    }
    if (detail && typeof detail === 'object' && (detail as { code?: unknown }).code === 'RECOVERY_DECISION_REQUIRED') {
      throw new Error(String((detail as { message?: unknown }).message || '请先处理未完成任务，或明确从今天重新开始。'))
    }
    const knownErrors: Record<string, string> = {
      '请先保存 API 地址、模型和密钥，再切换到自配置 API。': '请先保存 API 地址、模型和密钥，再切换到自配置 API。',
      '当前还有生成或资料研究在进行，请等待完成后再切换连接方式。': '当前还有生成或资料研究在进行，请等待完成后再切换连接方式。',
      'Codex 尚未连接，请先完成本机安装与登录，再点击「检测并连接」。': 'Codex 尚未连接，请先完成本机安装与登录，再点击「检测并连接」。',
      '调整范围内没有尚未开始且日期不早于今天的任务。原计划和建议已保留，请先恢复逾期任务，或重新选择后续任务。': '调整范围内没有尚未开始且日期不早于今天的任务。原计划和建议已保留，请先恢复逾期任务，或重新选择后续任务。',
      'No requirement draft exists': '暂时没有找到整理好的目标，请刷新页面后重试。',
      'Requirement is not awaiting review': '目标内容已经发生变化，请刷新页面查看最新结果。',
      'Core requirements are no longer complete': '核心需求尚未完整，请先补齐标记出的信息。',
      'Route is not awaiting review': '路线已经发生变化，请刷新页面查看最新结果。',
      'No route draft exists': '暂时没有找到可以操作的路线，请重新整理路线。',
      'No pending plan adjustment exists': '这份调整建议已经处理或失效，请刷新后再试。',
      'The pending adjustment still requires clarification': '还需要补充一点信息，才能采用这份建议。',
      'A plan adjustment is still pending': '请先决定是否采用当前建议，再继续确认计划。',
      'Plan generation is not complete': '每日计划还没有全部生成完成，请先继续生成。',
      'Plan is not ready to start': '计划尚未确认，请先返回时间规划页面确认。',
      'Required tasks are not complete': '今天仍有必做任务尚未完成。',
      'Plan version not found': '没有找到之前的那份安排，请刷新页面后重试。',
      '追赶方案仍有任务未能安全承接，当前计划保持不变；请重新生成更小范围的追赶预览。': '当前积压任务无法在这段时间内全部安全承接，原计划保持不变。请缩小追赶范围或允许更多后续日期后重新生成。',
    }
    if (typeof detail === 'string' && knownErrors[detail]) {
      throw new Error(knownErrors[detail])
    }
    if (response.status === 503 && typeof detail === 'string') {
      if (path.startsWith('/api/codex/')) throw new Error(detail)
      throw new Error('Solevia 暂时没有得到完整结果，你已经填写和确认的内容仍然保留，可以稍后重试。')
    }
    if (detail && typeof detail === 'object' && 'message' in detail) {
      const message = (detail as { message?: unknown }).message
      const providerCode = (detail as { provider_code?: unknown }).provider_code
      if (typeof message === 'string' && providerCode === 'MODEL_GATEWAY_TIMEOUT') {
        throw new Error('这次等待得有些久，但你已经填写和确认的内容仍然保留，可以稍后重试。')
      }
      if (typeof message === 'string') {
        const looksTechnical = /\b(?:API|HTTP|JSON|Schema|provider|token|gateway)\b|中转站|模型服务|状态码|请求失败/i.test(message)
        const error = new Error(looksTechnical ? 'Solevia 暂时没有得到完整结果，你的内容已经保留，可以稍后重试。' : message) as Error & { code?: string; detail?: unknown }
        const code = (detail as { code?: unknown }).code
        error.code = typeof code === 'string' ? code : undefined
        error.detail = detail
        throw error
      }
    }
    if (detail && typeof detail === 'object' && 'code' in detail) {
      const error = new Error(
        typeof (detail as { code?: unknown }).code === 'string'
          ? String((detail as { code: unknown }).code)
          : '暂时无法完成这个操作，请稍后重试。',
      ) as Error & { code?: string }
      error.code = typeof (detail as { code?: unknown }).code === 'string'
        ? String((detail as { code: unknown }).code)
        : undefined
      ;(error as Error & { detail?: unknown }).detail = detail
      throw error
    }
    if (response.status === 409) {
      if (typeof detail === 'string' && /[\u4e00-\u9fff]/.test(detail)
          && !/Traceback|\b(?:SQL|API|HTTP|JSON|Schema|provider|token|gateway)\b/i.test(detail)) {
        throw new Error(detail)
      }
      throw new Error('当前内容已经发生变化，请刷新页面后重试。')
    }
    throw new Error('暂时无法完成这个操作，请稍后重试。')
  }

  const result = response.status === 204 ? undefined : await response.json()
  const method = (options?.method ?? 'GET').toUpperCase()
  if (!['GET', 'HEAD'].includes(method) && /^\/api\/goals(?:\/|$)/.test(path)
      && path !== '/api/goals/status-check') {
    window.dispatchEvent(new Event('planner:goals-changed'))
  }
  return result as T
}
