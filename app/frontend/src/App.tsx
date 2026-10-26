import { KernelRuntimeSync } from './codex/KernelSelection'
import { BrowserRouter, Route, Routes } from 'react-router-dom'
import { WorkspaceShell } from './app/WorkspaceShell'
import './styles/index.css'
import './styles/workbench.css'
import { InteractionFeedback } from './app/InteractionFeedback'

// Keep these public exports for existing integrations. Pages depend on their owning modules.
export { ExecutionDiscussionDock } from './execution/ExecutionDiscussionDock'
export { isExecutionDiscussionPath } from './app/routePresentation'

export default function App() {
  return <BrowserRouter><KernelRuntimeSync /><InteractionFeedback /><Routes>
    <Route path="/*" element={<WorkspaceShell />} />
  </Routes></BrowserRouter>
}
