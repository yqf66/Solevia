import { CircleAlert } from 'lucide-react'
import './ConnectionAwareError.css'
import { codexFailureForCode, codexFailureForMessage } from './codexFailures'

export function isModelConnectionMessage(message: string) {
  return message.startsWith('还没有连接模型。') || message.startsWith('Codex 尚未连接。请先')
}

export function ConnectionAwareError({ message, code, onRetry }: { message: string; code?: string; onRetry?: () => void }) {
  if (!message) return null
  const codexFailure = codexFailureForCode(code) ?? codexFailureForMessage(message)
  if (!codexFailure && !isModelConnectionMessage(message)) return <p className="form-error" role="alert">{message}</p>
  return <div className="model-connection-reply" role="alert">
    <CircleAlert size={17} aria-hidden="true" />
    <div><strong>{codexFailure?.title ?? '先连接一个模型'}</strong><p>{codexFailure?.message ?? message}</p>
      <div className="model-connection-actions"><button type="button" onClick={() => window.dispatchEvent(new Event('solevia:open-model-settings'))}>{codexFailure ? '连接设置 / 切换 API →' : '打开模型连接 →'}</button>
      {onRetry && <button type="button" onClick={onRetry}>{codexFailure ? '已处理，重试' : '已配置，重试'}</button>}</div>
    </div>
  </div>
}
