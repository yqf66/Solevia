import { BrowserRouter } from 'react-router-dom'
import { WorkspaceShell } from './app/WorkspaceShell'
import './styles/index.css'

// Keep these public exports for existing integrations. Pages depend on their owning modules.
export { ExecutionDiscussionDock } from './execution/ExecutionDiscussionDock'
export { isExecutionDiscussionPath } from './app/routePresentation'

export default function App() {
  return <BrowserRouter><WorkspaceShell /></BrowserRouter>
}
