import { useEffect, useId, useRef, useState } from 'react'
import { DialogBackdrop } from './DialogBackdrop'

type Options = { title: string; message: string; confirmLabel?: string }

export function useConfirmation() {
  const [request, setRequest] = useState<Options | null>(null)
  const resolve = useRef<((result: boolean) => void) | null>(null)
  const titleId = useId()
  useEffect(() => () => { resolve.current?.(false) }, [])
  function finish(result: boolean) {
    resolve.current?.(result); resolve.current = null; setRequest(null)
  }
  function confirm(options: Options) {
    resolve.current?.(false)
    setRequest(options)
    return new Promise<boolean>(done => { resolve.current = done })
  }
  return { confirm, confirmationDialog: request && <DialogBackdrop onDismiss={() => finish(false)}>
    <section className="inline-dialog confirmation-dialog" role="alertdialog" aria-modal="true" aria-labelledby={titleId}>
      <h2 id={titleId}>{request.title}</h2><p>{request.message}</p>
      <div className="inline-dialog-actions"><button className="button" type="button" data-dialog-initial-focus onClick={() => finish(false)}>取消</button><button className="button danger-button" type="button" onClick={() => finish(true)}>{request.confirmLabel ?? '确认删除'}</button></div>
    </section>
  </DialogBackdrop> }
}
