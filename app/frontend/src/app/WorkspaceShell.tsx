import { SoleviaWordmark } from '../brand/SoleviaMark'
import { useEffect, useRef, useState } from 'react'
import { ListChecks, House, Plus, Menu, X, Settings2 } from 'lucide-react'
import { NavLink, Navigate, Route, Routes, useLocation, useParams } from 'react-router-dom'
import { UserSettingsPanel } from '../UserSettingsPanel'
import { LightCreatePage, LightListPage, LightPlanPage } from '../LightArrangements'
import { ClarificationPage } from '../planner/ClarificationPage'
import { RouteReviewPage } from '../planner/RouteReviewPage'
import { PlanReviewPage } from '../planner/PlanReviewPage'
import { PrepareStartPage } from '../execution/PrepareStartPage'
import { TodayPage } from '../execution/TodayPage'
import { ProgressPage } from '../execution/ProgressPage'
import { GoalCompletionPage } from '../execution/GoalCompletionPage'
import { ExecutionDiscussionDock } from '../execution/ExecutionDiscussionDock'
import { FixedTasksPage } from '../execution/FixedTasksPage'
import { CreateGoalPage } from '../planner/CreateGoalPage'
import { TodayDashboard } from '../dashboard/TodayDashboard'
import { GoalGroup, GoalListRow } from './GoalNavigation'
import { pausedGoalRemainingDays } from './goalState'
import type { GoalListItem } from '../api'
import { useWorkspaceGoals } from './useWorkspaceGoals'
import { isExecutionDiscussionPath } from './routePresentation'
import { usePageTransition } from './usePageTransition'
import { UserAvatar } from '../profile/UserAvatar'
import { useLocalProfile } from '../profile/localProfile'

function LegacyManageRedirect() {
  const { goalId } = useParams()
  return <Navigate to={goalId ? `/goals/${goalId}/today` : '/'} replace />
}

export function WorkspaceShell() {
  const localProfile = useLocalProfile()
  const location = useLocation()
  const contentRef = useRef<HTMLElement | null>(null)
  usePageTransition(location.pathname, contentRef)
  const activeGoalId = location.pathname.match(/^\/goals\/([^/]+)/)?.[1]
  const isPlannerStage = location.pathname === '/fixed-tasks' || /^\/goals\/[^/]+\/(clarify|route|plan|prepare|today)$/.test(location.pathname)
  const isRouteDrafting = /^\/goals\/[^/]+\/route$/.test(location.pathname)
  const isPlanDrafting = /^\/goals\/[^/]+\/plan$/.test(location.pathname)
  const { goals, manualPlans, refreshGoals } = useWorkspaceGoals()
  const [userSettingsOpen, setUserSettingsOpen] = useState(false)
  const [navigationAt, setNavigationAt] = useState<string | null>(null)
  const navigationOpen = navigationAt === location.pathname

  useEffect(() => {
    if (contentRef.current) contentRef.current.scrollTop = 0
  }, [location.pathname])

  useEffect(() => {
    if (!navigationOpen) return
    const closeOnEscape = (event: KeyboardEvent) => { if (event.key === 'Escape') setNavigationAt(null) }
    window.addEventListener('keydown', closeOnEscape)
    return () => window.removeEventListener('keydown', closeOnEscape)
  }, [navigationOpen])

  const executionStatuses = new Set(['ACTIVE', 'RECOVERY_REQUIRED', 'COMPLETED'])
  const pauseHasEnded = (goal: GoalListItem) => goal.status === 'PAUSED' && goal.pause_until !== null && (pausedGoalRemainingDays(goal) ?? 1) <= 0
  const runningGoals = goals.filter((goal) => executionStatuses.has(goal.status) || pauseHasEnded(goal))
  const pausedGoals = goals.filter((goal) => goal.status === 'PAUSED' && !pauseHasEnded(goal))
  const planningGoals = goals.filter((goal) => !executionStatuses.has(goal.status) && goal.status !== 'PAUSED' && goal.status !== 'ARCHIVED')
  const endedGoals = goals.filter((goal) => goal.status === 'ARCHIVED')
  const executionDiscussionGoal = goals.find((goal) => (
    goal.id === activeGoalId && ['ACTIVE', 'RECOVERY_REQUIRED', 'PAUSED'].includes(goal.status)
  ))
  const showExecutionDiscussion = isExecutionDiscussionPath(location.pathname)

  return (
    <div className={`app-shell${navigationOpen ? ' navigation-open' : ''}`}>
      <a className="skip-link" href="#workspace-content">跳到主要内容</a>
      <header className={`topbar${location.pathname === '/' ? ' home-topbar' : ''}`}>
        <span className="goal-name">
          {location.pathname.startsWith('/quick') ? '快速安排' : location.pathname === '/fixed-tasks' ? '快速安排' : location.pathname.endsWith('/complete') ? '目标复盘' : location.pathname.endsWith('/progress') ? '进度回顾' : location.pathname.endsWith('/today') ? '今日任务' : location.pathname.endsWith('/prepare') ? '选择开始时间' : isPlanDrafting ? '安排时间' : isRouteDrafting ? '讨论路线' : location.pathname === '/' ? '今日总览' : activeGoalId ? '一起想清楚目标' : '先从一个目标开始'}
        </span>
      </header>

      <div className="mobile-workspace-bar">
        <NavLink className="sidebar-brand" to="/" aria-label="Solevia 首页"><SoleviaWordmark /></NavLink>
        <button className="icon-button mobile-menu-toggle" type="button" aria-label={navigationOpen ? '关闭工作区导航' : '打开工作区导航'} aria-expanded={navigationOpen} aria-controls="workspace-navigation" onClick={() => setNavigationAt(navigationOpen ? null : location.pathname)}>{navigationOpen ? <X size={20} /> : <Menu size={20} />}</button>
      </div>
      {navigationOpen && <button className="navigation-backdrop" type="button" aria-label="关闭导航" onClick={() => setNavigationAt(null)} />}
      <div className="workspace">
        <aside id="workspace-navigation" className="sidebar chat-sidebar" aria-label="工作区导航">
          <div className="sidebar-navigation-head">
          <div className="sidebar-brand-row">
            <NavLink className="sidebar-brand" to="/" aria-label="Solevia 首页">
              <SoleviaWordmark />
            </NavLink>
          </div>
          <nav className="sidebar-quick-nav" aria-label="工作区">
            <NavLink end to="/"><House size={18} aria-hidden="true" /> 今日总览</NavLink>
            <NavLink to="/quick"><ListChecks size={18} aria-hidden="true" /> 快速安排</NavLink>
            <NavLink to="/new-agent"><Plus size={18} aria-hidden="true" /> 新建目标规划</NavLink>
          </nav>
          <div className="sidebar-divider" />
          </div>
          <div className="sidebar-scroll-region">
          <div className="plan-list running-goals" aria-label="正在进行">
            <p>正在进行</p>
            {manualPlans.filter(plan => plan.status === 'ACTIVE').map(plan => <NavLink key={plan.id} className="plan-link" to={`/quick/${plan.id}`}><strong>{plan.title}</strong><span>快速安排</span></NavLink>)}
            {runningGoals.length === 0 && !manualPlans.some((plan) => plan.status === 'ACTIVE') ? (
              <span className="empty-plans">还没有规划</span>
            ) : runningGoals.map((goal) => <GoalListRow goal={goal} onChanged={refreshGoals} key={goal.id} />)}
          </div>
          <GoalGroup title="暂停中" goals={pausedGoals} onChanged={refreshGoals} className="paused-goals" />
          <GoalGroup title="规划中" goals={planningGoals} onChanged={refreshGoals} />
          <GoalGroup title="已结束" goals={endedGoals} onChanged={refreshGoals} initiallyOpen={false} />
          </div>
          <button className={`sidebar-user-option${userSettingsOpen ? ' active' : ''}`} type="button" aria-expanded={userSettingsOpen} onClick={() => setUserSettingsOpen((current) => !current)}>
            <UserAvatar src={localProfile.avatar} /><span><strong>{localProfile.nickname}</strong><small>个人资料与日常偏好</small></span><Settings2 size={17} aria-hidden="true" />
          </button>
          {userSettingsOpen && <UserSettingsPanel onClose={() => setUserSettingsOpen(false)} />}
        </aside>

        <main id="workspace-content" tabIndex={-1} ref={contentRef} className={`content${isPlannerStage ? ' planner-content' : ''}${location.pathname === '/' ? ' home-content' : ''}`}>
          <Routes>
            <Route path="/" element={<TodayDashboard />} />
            <Route path="/new-agent" element={<CreateGoalPage />} />
            <Route path="/quick" element={<LightListPage />} />
            <Route path="/quick/new" element={<LightCreatePage />} />
            <Route path="/quick/drafts/:draftId" element={<LightCreatePage />} />
            <Route path="/quick/:planId" element={<LightPlanPage />} />
            <Route path="/light/*" element={<Navigate to="/quick" replace />} />
            <Route path="/manual-plans/new" element={<Navigate to="/quick/new" replace />} />
            <Route path="/fixed-tasks" element={<FixedTasksPage />} />
            <Route path="/manual-plans/:planId" element={<Navigate to="/quick" replace />} />
            <Route path="/goals/:goalId/clarify" element={<ClarificationPage key={activeGoalId} />} />
            <Route path="/goals/:goalId/route" element={<RouteReviewPage key={activeGoalId} />} />
            <Route path="/goals/:goalId/plan" element={<PlanReviewPage key={activeGoalId} />} />
            <Route path="/goals/:goalId/prepare" element={<PrepareStartPage key={activeGoalId} />} />
            <Route path="/goals/:goalId/today" element={<TodayPage key={activeGoalId} />} />
            <Route path="/goals/:goalId/progress" element={<ProgressPage key={activeGoalId} />} />
            <Route path="/goals/:goalId/complete" element={<GoalCompletionPage key={activeGoalId} />} />
            <Route path="/goals/:goalId/manage" element={<LegacyManageRedirect />} />
            <Route path="*" element={<Navigate to="/" replace />} />
          </Routes>
        </main>
      </div>
      {executionDiscussionGoal && showExecutionDiscussion && <ExecutionDiscussionDock key={executionDiscussionGoal.id} goalId={executionDiscussionGoal.id} />}
    </div>
  )
}

