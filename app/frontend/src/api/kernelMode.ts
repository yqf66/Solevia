// Request deadlines also work before the runtime status request has completed.
let selected: 'codex' | 'api' | null = null
export function setKernelMode(mode: 'codex' | 'api') { selected = mode }
export function usesCodexKernel() {
  return selected === 'codex' || (selected === null && ['codex', 'unified'].includes(import.meta.env.VITE_SOLEVIA_RUNTIME))
}
