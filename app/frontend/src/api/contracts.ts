// contracts: extracted without changing request or response semantics.

export type RequirementFact = {
  id: string
  category: string
  field_key: string
  value: unknown
  source: string
  status: string
  confidence: number
  operation?: string
  change_reason?: string | null
  evidence_message_id?: string | null
  supersedes_fact_id?: string | null
  created_at?: string | null
}

export type RequirementChange = {
  id: string
  field_key: string
  operation: string
  reason: string | null
  old_fact_id: string | null
  new_fact_id: string | null
  old_value: unknown
  new_value: unknown
  evidence_message_id: string | null
  created_at: string
}

export type ClarificationAnalysisJob = {
  id: string
  status: 'PENDING' | 'PROCESSING' | 'SUCCEEDED' | 'FAILED'
  attempts: number
  max_attempts: number
  next_retry_at: string
  last_error: string | null
  last_error_code: string | null
  created_at: string
  updated_at: string
  completed_at: string | null
}

export type ConversationMessage = {
  id: string
  sequence: number
  role: 'USER' | 'ASSISTANT' | 'SYSTEM'
  content: string
  generation_status: string
  metadata?: Record<string, unknown>
  created_at: string
}

export type ResearchSource = {
  title: string
  url: string
  read_status: string
  retrieval_method: string
  evidence_level: string
  snippet: string
  excerpt: string
  ranking_score: number
}

export type ResearchSummary = {
  id: string
  status: string
  current_stage: string
  question: string
  stop_reason?: string | null
  error_code?: string | null
  error_message?: string | null
  result_status?: string | null
  source_count: number
  read_pages: number
  unresolved_points: string[]
  sources: ResearchSource[]
  claims: Array<Record<string, unknown>>
  coverage: Record<string, unknown>
}

export type MaterialCitation = {
  source_id: string
  revision_id: string
  sequence: number
  locator: string
  section_title: string
  source_title?: string
  relative_path?: string
}

export type MaterialKeyPoint = {
  title: string
  detail: string
  citations: MaterialCitation[]
}

export type MaterialFinding = {
  text: string
  category: string
  confidence: number
  status: string
  citations: MaterialCitation[]
}

export type MaterialAnalysis = {
  id: string
  goal_id: string
  query: string
  status: string
  summary: string
  key_points: MaterialKeyPoint[]
  findings: MaterialFinding[]
  open_questions: string[]
  source_count: number
  file_count?: number
  conflicts?: Array<{ description: string; citations: MaterialCitation[] }>
  retrieval_coverage?: Array<{ query: string; retrieved_chunks: number }>
  created_at: string
  updated_at: string
}

export type MaterialRoot = {
  id: string
  display_name: string
  path: string
  status: string
  created_at: string
  updated_at: string
}

export type MaterialScan = {
  root_id: string
  scanned_at: string
  truncated: boolean
  files: Array<{ relative_path: string; title: string; extension: string; file_size: number; supported: boolean; reason: string | null; linked: boolean; revision_status: string | null }>
}

export type GoalMaterial = {
  link_id: string
  source_id: string
  kind: string
  title: string
  mime_type: string | null
  root_id: string | null
  relative_path: string | null
  canonical_uri: string
  source_status: string
  scope: string
  usage_policy: string
  pinned: boolean
  link_status: string
  latest_revision: {
    id: string
    revision: number
    status: string
    metadata: Record<string, unknown>
    processing?: { status: string; attempts: number; error_message?: string | null } | null
    error_code?: string | null
    error_message?: string | null
  } | null
}

export type ClarificationWorkspace = {
  goal_id: string
  goal_title: string
  raw_input: string
  status: string
  messages: ConversationMessage[]
  facts: RequirementFact[]
  fact_changes?: RequirementChange[]
  analysis_jobs?: ClarificationAnalysisJob[]
  memory?: {
    summary: string
    current_topic: string | null
    recent_changes: Array<Record<string, unknown>>
    entity_refs: string[]
    through_message_sequence: number
  }
  open_questions: string[]
  conflicts: string[]
  readiness: {
    ready_for_review: boolean
    blocking_fields: string[]
    reason: string
    confidence?: number
    blocking_topics?: string[]
    recommended_next_action?: string | null
  }
  requirement_draft: null | {
    id: string
    version: number
    status: string
    snapshot: Record<string, unknown>
  }
  downstream_route_preserved: boolean
  requirements_changed_since_route: boolean
  planning_approach_recommendation: {
    recommended_mode: 'EXPLORATORY' | 'COMMITTED'
    reasons: string[]
    confidence: 'LOW' | 'MEDIUM' | 'HIGH'
    explicit_trial_weeks: number | null
  }
  selected_planning_mode: 'EXPLORATORY' | 'COMMITTED' | null
  schedule_type_recommendation: {
    recommended_type: 'OUTCOME' | 'RHYTHM' | 'CALENDAR'
    reasons: string[]
    confidence: 'LOW' | 'MEDIUM' | 'HIGH'
  }
  selected_schedule_type: 'OUTCOME' | 'RHYTHM' | 'CALENDAR' | null
  latest_research: ResearchSummary | null
}

export type GoalListItem = {
  id: string
  title: string
  status: string
  last_active_at: string
  timezone: string
  pause_until: string | null
  completed_at: string | null
  archived_at: string | null
}

export type GoalLifecycle = {
  id: string
  title: string
  status: string
  timezone: string
  pause_until: string | null
  completed_at: string | null
  archived_at: string | null
  completion_summary: string | null
  remaining_required_tasks: number
}

export type RouteStage = {
  id: string
  order: number
  title: string
  purpose: string
  topics: string[]
  outputs: string[]
  depends_on: number[]
  options: Array<{
    label: string
    description: string
    tradeoff: string
    recommended: boolean
  }>
}

export type RouteVersion = {
  id: string
  version: number
  revision: number
  status: string
  summary: string
  stages: RouteStage[]
  tradeoffs: string[]
  assumptions: string[]
  open_questions: string[]
  optional_questions: string[]
  change_summary: string[]
  planning_mode: 'EXPLORATORY' | 'COMMITTED'
  schedule_type: 'OUTCOME' | 'RHYTHM' | 'CALENDAR'
}

export type RouteWorkspace = {
  goal_id: string
  goal_title: string
  status: string
  requirement_version: number
  requirement_snapshot: Record<string, unknown>
  route: RouteVersion | null
  messages: ConversationMessage[]
  pending_requirement_version: number | null
  requirements_changed_since_route: boolean
  planning_mode: 'EXPLORATORY' | 'COMMITTED'
  schedule_type: 'OUTCOME' | 'RHYTHM' | 'CALENDAR'
  latest_research: ResearchSummary | null
}

export type PlanTask = {
  id: string
  order: number
  title: string
  description: string
  estimate_minutes: number
  priority: number
  task_kind: string
  is_required: boolean
  evidence: string[]
  depends_on: string[]
  ai_schedule_requested?: boolean
  is_splittable?: boolean
  scheduling_policy?: 'EXACT_DAY' | 'DATE_WINDOW' | 'CADENCE' | 'QUOTA' | 'TASK_POOL' | 'CHECKPOINT' | 'TRIGGERED' | 'FLOATING'
  scheduling_policy_data?: Record<string, unknown>
  skip_guidance?: string[]
  source_kind?: 'LEGACY_PLAN_TASK' | 'WORK_ITEM_OCCURRENCE'
  source_work_item_id?: string | null
  work_item_window_id?: string | null
  occurrence_index?: number | null
  materialization_key?: string | null
  blocked_reason?: string | null
  status: string
}

export type WorkItemWindow = {
  id: string
  period_key: string
  eligible_from: string
  eligible_to: string
  required_value: number
  status: string
  resolution: Record<string, unknown>
  materialization_revision: number
}

export type PlanWorkItemGroup = {
  id: string
  milestone_id: string | null
  scope_type: 'PLAN' | 'STAGE' | 'WEEK' | 'CHECKPOINT'
  stage_order: number | null
  group_key: string
  title: string
  selection_mode: string
  minimum_selected: number
  maximum_selected: number | null
  constraints: Record<string, unknown>
  status: string
}

export type PlanWorkItem = {
  id: string
  item_key: string
  milestone_id: string | null
  group_id: string | null
  source_work_item_id: string | null
  lineage_work_item_id: string | null
  scope_type: 'PLAN' | 'STAGE' | 'WEEK' | 'CHECKPOINT'
  stage_order: number | null
  title: string
  description: string
  task_kind: string
  estimate_minutes: number
  priority: number
  is_required: boolean
  is_splittable: boolean
  binding_mode: string
  fulfillment_mode: string
  target_value: number
  target_unit: string
  constraints: Record<string, unknown>
  completion_rule: Record<string, unknown>
  evidence: string[]
  source_refs: string[]
  commitment_refs: string[]
  dependency_ids: string[]
  status: string
  windows: WorkItemWindow[]
}

export type GenerationRun = {
  id: string
  goal_id: string
  plan_version_id: string | null
  task_type: string
  tier: 'L0' | 'L1' | 'L2' | 'L3'
  status: string
  decision: { roles?: string[]; degraded_roles?: string[]; [key: string]: unknown }
  validation: Record<string, unknown>
  input_units: number
  output_units: number
  estimated_cost_units: number
  latency_ms: number
  error_code: string | null
  error_message: string | null
  reports: Array<{ id: string; role: string; status: string; confidence: number }>
  created_at: string
  completed_at: string | null
}

export type PlanDay = {
  id: string
  stage_order: number
  week_index: number
  day_index: number
  primary_goal: string
  suggested_minutes: number
  evidence: string[]
  do_not: string[]
  tasks: PlanTask[]
}

export type PlanMilestone = {
  id: string
  route_stage_id: string
  stage_order: number
  title: string
  purpose: string
  start_week: number
  end_week: number
  outputs: string[]
}

export type CapacitySegment = {
  label: string
  start_week: number
  end_week: number
  weekly_execution_days?: number | null
  available_days_per_week?: number | null
  suggested_daily_minutes?: number | null
  exception_days_per_week?: number
  exception_daily_minutes?: number | null
  exception_weekdays?: number[]
  weekly_capacity_overrides?: Array<{
    week_index: number
    available_days_per_week?: number | null
    weekly_execution_days: number
    suggested_daily_minutes: number
    exception_days_per_week?: number
    exception_daily_minutes?: number | null
    exception_weekdays?: number[]
  }>
}

export type PlanAdjustmentProposal = {
  assistant_message: string
  user_intent_summary: string
  scope: 'OVERALL' | 'STAGE' | 'WEEK' | 'MULTI_SCOPE'
  affected_stage_orders: number[]
  affected_weeks: number[]
  downstream_affected_weeks: number[]
  proposed_changes: string[]
  tradeoffs: string[]
  unchanged_guarantees: string[]
  total_weeks_change: number
  weekly_intensity_change: 'LOWER' | 'UNCHANGED' | 'HIGHER' | 'MIXED'
  buffer_change: 'LESS' | 'UNCHANGED' | 'MORE' | 'MIXED'
  target_schedule_type?: 'OUTCOME' | 'RHYTHM' | 'CALENDAR' | null
  capacity_segments?: CapacitySegment[] | null
  proposed_deadline?: string | null
  reason_codes?: AdjustmentReasonCode[]
  needs_clarification: boolean
  clarifying_question: string | null
  can_apply: boolean
}

export type PlanAdjustmentPreview = {
  id: string
  goal_id: string
  source_plan_version_id: string
  status: 'PREVIEW' | 'APPLIED'
  fingerprint: string
  plan: Record<string, unknown>
  schedule: Array<{ title: string; date: string; start: string | null; end: string | null; minutes: number; state: string }>
  unallocated_count: number
  reason_codes: AdjustmentReasonCode[]
  schedule_plan?: Array<{ scheduled_date: string; assignments: Array<{ title: string; start_local: string; end_local: string }> }>
  created_at: string
}

export type PlanRecoveryGuard = {
  required: boolean
  message: string
  missed_execution_days: number
  interrupted_execution_days: number
  missed_dates: string[]
  recovery_deferred: boolean
}

export type PlanWindow = {
  id: string
  plan_version_id: string
  sequence: number
  start_week: number
  end_week: number
  status: 'DRAFT' | 'ACTIVE' | 'CLOSED' | 'DISCARDED'
  trigger: string
  basis: Record<string, unknown>
  created_at: string
  published_at: string | null
}

export type PlanWorkspace = {
  recovery_review?: {
    token: string
    decisions: Array<'ALLOW_EXTENSION' | 'KEEP_DEADLINE' | 'ACCEPT_LOAD'>
    extension_dates: string[]
    extension_days: number
    is_recovery: boolean
  } | null
  goal_id: string
  goal_title: string
  status: string
  requirement_version: number
  requirement_snapshot: Record<string, unknown>
  route_version: number
  route_revision: number
  generation_phase: 'OUTLINE' | 'DETAILS' | 'COMPLETE'
  generated_weeks: number
  total_weeks: number
  generation_target_weeks: number
  planning_mode: 'EXPLORATORY' | 'COMMITTED'
  schedule_type: 'OUTCOME' | 'RHYTHM' | 'CALENDAR'
  exploration_windows: PlanWindow[]
  pending_exploration_window: PlanWindow | null
  can_extend_exploration: boolean
  available_generation_modes: Array<'STANDARD' | 'DEEP'>
  generation_job: null | {
    id: string
    mode: 'STANDARD' | 'DEEP'
    planning_mode: 'EXPLORATORY' | 'COMMITTED'
    schedule_type: 'OUTCOME' | 'RHYTHM' | 'CALENDAR'
    status: 'QUEUED' | 'RUNNING' | 'COMPLETED' | 'FAILED'
    phase: 'OUTLINE' | 'DETAILS' | 'COMPLETE'
    generated_weeks: number
    total_weeks: number
    error_code: string | null
    error_message: string | null
    created_at: string
    updated_at: string
  }
  latest_generation_run: GenerationRun | null
  generation_repair?: {
    week_index: number | null
    attempt: number
    maximum_attempts: number
    message: string
  } | null
  capacity_negotiation?: {
    preview_progress?: { status: string; completed_weeks: number; total_weeks: number | null } | null
    state: string
    message: string
    week_index: number | null
    classification?: 'CONSTRAINT_CONFLICT' | 'DRAFT_OVERLOAD'
    verified_conflicts?: Array<{ label: string; weeks: number[]; minimum_minutes: number; available_minutes: number; deficit_minutes: number }>
    capacity_segments: CapacitySegment[]
    options: Array<{ title: string; instruction: string }>
    rules: string[]
  } | null
  latest_research: ResearchSummary | null
  recovery_guard?: PlanRecoveryGuard
  pending_adjustment_preview?: PlanAdjustmentPreview | null
  messages: ConversationMessage[]
  revision_history: Array<{
    id: string
    version: number
    revision: number
    status: string
    change_summary: string[]
    feedback: string | null
    created_at: string
  }>
  pending_adjustment: PlanAdjustmentProposal | null
  plan: null | {
    id: string
    route_version_id: string
    version: number
    revision: number
    status: string
    summary: string
    total_weeks: number
    weekly_execution_days: number
    weekly_minutes: number
    total_estimated_minutes: number
    planning_mode: 'EXPLORATORY' | 'COMMITTED'
    schedule_type: 'OUTCOME' | 'RHYTHM' | 'CALENDAR'
    execution_model_version: 'LEGACY_DAY_BASED' | 'HYBRID_WORK_ITEM'
    exploration_window_weeks: number
    next_unplanned_week: number
    buffer_strategy: string[]
    assumptions: string[]
    open_questions: string[]
    change_summary: string[]
    work_item_groups: PlanWorkItemGroup[]
    temporal_segments?: CapacitySegment[]
    work_items: PlanWorkItem[]
    milestones: PlanMilestone[]
    days: PlanDay[]
  }
}

export type ResearchRun = {
  id: string
  goal_id: string
  conversation_id: string | null
  question: string
  enabled: boolean
  status: string
  current_stage: string
  resume_stage: string | null
  attempt_count: number
  recovery_count: number
  idempotency_key: string | null
  cancel_requested_at: string | null
  cancel_reason: string | null
  decision: Record<string, unknown>
  budget: Record<string, unknown>
  checkpoint: Record<string, unknown>
  result: Record<string, unknown> | null
  stop_reason: string | null
  error_code: string | null
  error_message: string | null
  created_at: string
  started_at: string | null
  updated_at: string
  completed_at: string | null
  evidence: Array<{
    id: string
    canonical_url: string
    url: string
    title: string
    source: string
    read_status: string
    retrieval_method: string
    relevance_score: number
    confidence: number
  }>
}

export type ScheduledDay = {
  plan_day_id: string
  stage_order: number
  week_index: number
  day_index: number
  code: string
  scheduled_date: string
  primary_goal: string
}

export type ExecutionScheduleInput = {
  requested_start_date: string
  execution_weekdays: number[]
  excluded_dates: string[]
  smart_preview_id?: string | null
  materialization_preview_id?: string | null
  selected_work_item_ids?: string[]
}

export type CandidatePool = {
  id: string
  group_key: string
  title: string
  selection_mode: string
  minimum_selected: number
  maximum_selected: number | null
  selected_item_ids: string[]
  completed_selected: number
  remaining_required: number
  period_status: 'NOT_STARTED' | 'IN_PROGRESS' | 'COMPLETE'
  candidates: Array<{ id: string; item_key: string; title: string; description: string; estimate_minutes: number; priority: number; status: string }>
  periods?: Array<{ period_key: string; eligible_from: string; eligible_to: string; selected_item_ids: string[]; completed_selected: number; remaining_required: number; period_status: 'NOT_STARTED' | 'IN_PROGRESS' | 'COMPLETE' }>
}

export type CandidateReplacementPreview = {
  id: string
  status: 'PREVIEW' | 'APPLIED'
  group_id: string
  group_title: string
  candidate_item_id: string
  candidate_title: string
  replaced_item_ids: string[]
  target_date: string
  planned_minutes: number
  candidate_minutes: number
  capacity_minutes: number
  over_capacity_minutes: number
  blockers: string[]
  warnings: string[]
  can_apply: boolean
  fingerprint: string
}

export type TriggeredTask = {
  window_id: string
  work_item_id: string
  title: string
  description: string
  estimate_minutes: number
  condition: string
  eligible_from: string
  eligible_to: string
  status: string
  can_activate: boolean
  blocked_reason: string | null
  fulfillment_mode?: string
  target_value?: number
  target_unit?: string
  completed_value?: number
  remaining_value?: number
  explanation?: string
}

export type TriggeredTaskActivationPreview = {
  id: string
  status: 'PREVIEW' | 'APPLIED'
  window_id: string
  work_item_id: string
  title: string
  condition: string
  eligible_from: string
  eligible_to: string
  target_date: string | null
  fulfillment_mode?: string
  target_value?: number
  target_unit?: string
  completed_value?: number
  remaining_value?: number
  blockers: string[]
  warnings: string[]
  can_apply: boolean
  fingerprint: string
}

export type SmartCalendar = {
  preview_id: string
  input: ExecutionScheduleInput
  preview: ExecutionPreview
  days: ScheduledDay[]
  rationale: string
  rules: string[]
  assumptions: string[]
  questions: string[]
  can_apply: boolean
}

export type ExecutionDayMove = {
  preview_id: string
  mode: 'ADVANCE' | 'SWAP'
  can_apply: boolean
  blockers: string[]
  warnings: string[]
  completed_task_count: number
  changes: Array<{ plan_day_id: string; code: string; primary_goal: string; from_date: string; to_date: string }>
}

export type ExecutionPreview = {
  requested_start_date: string
  first_execution_date: string
  projected_end_date: string
  execution_weekdays: number[]
  excluded_dates: string[]
  total_execution_days: number
  upcoming_days: ScheduledDay[]
  materialization_preview_id?: string | null
}

export type WorkItemDailyTaskPreview = {
  work_item_id: string
  stage_order: number | null
  title: string
  description: string
  estimate_minutes: number
  priority: number
  task_kind: string
  is_required: boolean
  binding_mode: string
  evidence: string[]
  period_key: string
  occurrence_index: number
}

export type WorkItemDailyDayPreview = {
  scheduled_date: string
  week_index: number
  day_index: number
  code: string
  suggested_minutes: number
  tasks: WorkItemDailyTaskPreview[]
}

export type WorkItemDailyPreview = {
  plan_id: string
  candidate_start_date: string
  projected_end_date: string | null
  execution_weekdays: number[]
  days: WorkItemDailyDayPreview[]
  pending_trigger_items: WorkItemDailyTaskPreview[]
  warnings: string[]
}

export type ExecutionSetup = {
  goal_id: string
  goal_title: string
  status: string
  plan_id: string
  plan_revision: number
  total_weeks: number
  weekly_execution_days: number
  schedule_type: 'OUTCOME' | 'RHYTHM' | 'CALENDAR'
  execution_model_version: 'LEGACY_DAY_BASED' | 'HYBRID_WORK_ITEM'
  has_daily_plan: boolean
  minimum_execution_days?: number
  has_phased_capacity?: boolean
  suggested_weekdays: number[]
  activation: ExecutionPreview | null
  timezone: string
  current_date?: string | null
  suggested_start_date?: string | null
  schedule_preference: SchedulePreference | null
  availability_windows: AvailabilityWindow[]
}

export type ScheduleMode = 'FLEXIBLE' | 'BLOCKS' | 'FIXED'

export type SchedulePreference = {
  id: string
  goal_id: string
  mode: ScheduleMode
  timezone: string
  break_minutes: number
  allow_cross_window: boolean
  reminders_enabled: boolean
  supervision_mode: 'QUIET' | 'STANDARD' | 'INTENSIVE'
  cross_plan_priority_mode: 'PRIORITY' | 'DEADLINE' | 'BALANCED'
  quiet_start_local: string | null
  quiet_end_local: string | null
  muted_commitments: string[]
  created_at: string
  updated_at: string
}

export type AvailabilityWindow = {
  id: string
  goal_id: string
  weekday: number
  start_local: string
  end_local: string
  valid_from: string | null
  valid_to: string | null
  timezone: string
  source: string
  created_at: string
  updated_at: string
}

export type WorkspaceAvailabilityWindow = {
  id: string
  profile_id: string
  weekday: number
  start_local: string
  end_local: string
  valid_from: string | null
  valid_to: string | null
  timezone: string
  created_at: string
}

export type WorkspaceScheduleProfile = {
  id: string
  timezone: string
  default_mode: ScheduleMode
  break_minutes: number
  availability_mode: 'ROUTINE' | 'CUSTOM'
  wake_time: string
  available_from: string
  breakfast_enabled: boolean
  breakfast_start: string
  breakfast_end: string
  lunch_enabled: boolean
  lunch_start: string
  lunch_end: string
  nap_enabled: boolean
  nap_start: string
  nap_end: string
  dinner_enabled: boolean
  dinner_start: string
  dinner_end: string
  sleep_time: string
  routine_exclusions: Array<{ label: string; start_local: string; end_local: string; enabled: boolean }>
  schedule_style: string
  holiday_country_code: string | null
  holiday_subdivision_code: string | null
  holiday_locale: string | null
  holiday_source: 'UNSET' | 'AUTO' | 'USER'
  holiday_confidence: number
  holiday_confirmed: boolean
  created_at: string
  updated_at: string
}

export type HolidayCalendarDay = {
  date: string
  kind: 'HOLIDAY' | 'WORKDAY'
  name: string
  local_name: string
  country_code: string
  subdivision_code: string | null
  source: string
  authoritative: boolean
}

export type HolidayCalendar = {
  country_code: string | null
  subdivision_code: string | null
  start_date: string
  end_date: string
  days: HolidayCalendarDay[]
  coverage: 'COMPLETE' | 'PARTIAL' | 'UNAVAILABLE'
  missing_years: number[]
  sources: string[]
}

export type HolidayRegionSuggestion = {
  country_code: string | null
  timezone_country_code: string | null
  locale_country_code: string | null
  confidence: number
  requires_confirmation: boolean
  reason: 'TIMEZONE_LOCALE_AGREE' | 'TIMEZONE_LOCALE_CONFLICT' | 'TIMEZONE_ONLY' | 'LOCALE_ONLY' | 'UNKNOWN'
}

export type FixedActivityMoveRequest = { task_id: string; source_date: string; target_date: string; start_local: string; end_local: string }

export type FixedActivityMovePreview = {
  id: string; status: string; title: string; plan_title: string; can_apply: boolean; message: string
  before: { date: string; start_local: string; end_local: string }
  after: { date: string; start_local: string; end_local: string }
  capacity_changes: Array<{ date: string; before_minutes: number; after_minutes: number; delta_minutes: number }>
  conflicts: string[]
}

export type EffectiveWorkspaceAvailability = {
  source: 'ROUTINE' | 'CUSTOM'
  windows: Array<{ weekday: number; start_local: string; end_local: string; valid_from: string | null; valid_to: string | null }>
}

export type ModelConnection = {
  provider: 'fake' | 'deepseek' | 'openai_compatible'
  base_url: string
  proxy_url: string
  model: string
  api_key_configured: boolean
  api_key_hint: string | null
}

export type ScheduleFoundation = {
  preference: SchedulePreference
  availability_windows: AvailabilityWindow[]
  availability_exceptions: Array<{
    id: string
    goal_id: string
    exception_date: string
    kind: 'UNAVAILABLE' | 'OVERRIDE'
    start_local: string | null
    end_local: string | null
    reason: string | null
    source: string
    created_at: string
  }>
}

export type UnifiedTodayTask = {
  id: string
  source: 'AGENT' | 'QUICK'
  plan_id: string
  plan_title: string
  plan_kind?: 'AGENT' | 'QUICK'
  title: string
  description: string
  estimate_minutes: number
  priority: number
  status: string
  blocked_reason?: string | null
  scheduled_date: string
  start_local: string | null
  end_local: string | null
  recurrence: 'ONCE' | 'DAILY' | 'WEEKLY'
  recurrence_weekdays: number[]
  recurrence_end_date: string | null
  timezone: string
  is_fixed: boolean
  parallel_confirmed: boolean
  schedule_status: 'PLANNED' | 'REQUIRES_REVIEW' | 'CANCELLED' | 'COMPLETED'
  schedule_review_required: boolean
  schedule_change_reason: string | null
  dependencies?: Array<{
    reference: string
    task_id: string | null
    title: string | null
    status: 'COMPLETED' | 'PENDING' | 'UNKNOWN'
  }>
  ai_schedule_requested?: boolean
  scheduling_policy?: 'EXACT_DAY' | 'DATE_WINDOW' | 'CADENCE' | 'QUOTA' | 'TASK_POOL' | 'CHECKPOINT' | 'TRIGGERED' | 'FLOATING'
  scheduling_policy_data?: Record<string, unknown>
}

export type UnifiedToday = {
  date: string
  timezone: string
  fixed_tasks: UnifiedTodayTask[]
  flexible_tasks: UnifiedTodayTask[]
  conflicts: Array<{
    first_task_id: string
    second_task_id: string
    overlap_start: string
    overlap_end: string
    reason: string
  }>
}

export type AllocationInput = {
  scheduled_date: string
  windows: Array<{ weekday: number; start_local: string; end_local: string }>
  include_flexible: boolean
  goal_id?: string
  manual_plan_id?: string
  use_ai?: boolean
  model_task_order?: string[]
  model_preferred_starts?: Record<string, string>
  model_notes?: string[]
}

export type AllocationPreview = {
  fingerprint: string
  request: AllocationInput
  timezone: string
  assignments: Array<{ task_id: string; start_local: string; end_local: string }>
  unallocated: UnifiedTodayTask[]
  available_minutes: number
  required_minutes: number
  assigned_minutes: number
  unallocated_minutes: number
  unallocated_reasons?: Record<string, 'TIMEZONE_CONFLICT' | 'DEPENDENCY_BLOCKED' | 'FIXED_CONSTRAINT' | 'CAPACITY_INSUFFICIENT'>
  competition_summary?: Array<{
    scheduled_date: string
    available_minutes: number
    required_minutes: number
    assigned_minutes: number
    over_capacity_minutes: number
    priority_mode?: 'PRIORITY' | 'DEADLINE' | 'BALANCED'
    priority_order?: string[]
    plans: Array<{
      plan_id: string
      plan_title: string
      required_minutes: number
      assigned_minutes: number
      unallocated_minutes: number
      task_count: number
      unallocated_task_ids: string[]
    }>
    tradeoffs: string[]
  }>
  notes: string[]
  strategy?: 'LOCAL_VALIDATED' | 'MODEL_ASSISTED'
  scheduling_context?: {
    date: string
    timezone: string
    holiday_calendar?: HolidayCalendarDay | null
    available_windows: string[][]
    occupied_fixed_tasks: Array<{ task_id: string; title: string; start_local: string; end_local: string; source: string }>
    occupied_constraints?: Array<{ title: string; start_local: string; end_local: string; source: string }>
    free_windows_before_assignment: string[][]
    priority_order: string[]
    allocation_policy: string[]
  }
}

export type ManualTask = {
  id: string
  manual_plan_id: string
  quick_plan_id?: string
  order_in_plan: number
  title: string
  description: string
  estimate_minutes: number
  priority: number
  scheduled_date: string | null
  start_local: string | null
  end_local: string | null
  recurrence: 'ONCE' | 'DAILY' | 'WEEKLY'
  recurrence_weekdays: number[]
  recurrence_end_date: string | null
  status: string
  created_at: string
  updated_at: string
  overrides: Array<{ override_date: string; title: string | null; description?: string | null; estimate_minutes?: number | null; priority?: number | null; start_local: string | null; end_local: string | null; time_overridden: boolean; status: string | null; is_deleted: boolean }>
}

export type ManualPlan = {
  id: string
  title: string
  description: string
  status: string
  timezone: string
  kind?: 'QUICK'
  arrangement?: { draft_id?: string; start_date?: string; horizon_days?: number; upgraded_goal_id?: string; handoff_at?: string; handoff_start_date?: string; assumptions?: string[]; questions?: string[] }
  tasks: ManualTask[]
  created_at: string
  updated_at: string
}

export type ExecutionCalendar = {
  first_date: string
  last_date: string
  days: Array<{
    plan_day_id: string
    scheduled_date: string
    code: string
    task_stage_orders?: number[]
    primary_goal: string
    status: 'PLANNED' | 'IN_PROGRESS' | 'BLOCKED' | 'COMPLETED' | 'PARTIAL' | 'MISSED'
    suggested_minutes: number
    tasks: Array<{
      id: string
      title: string
      status: string
      is_required: boolean
      estimate_minutes: number
      origin_stage_order?: number | null
      scheduling_policy?: 'EXACT_DAY' | 'DATE_WINDOW' | 'CADENCE' | 'QUOTA' | 'TASK_POOL' | 'CHECKPOINT' | 'TRIGGERED' | 'FLOATING'
      scheduling_policy_data?: Record<string, unknown>
      can_arrange_today?: boolean
      arrange_blocked_reason?: string | null
      recommendation_group_key?: string | null
      recommendation_position?: number | null
      recommendation_total?: number | null
      is_recommendation_primary?: boolean
    }>
    changes: Array<{
      adjustment_kind: string
      direction: 'MOVED_FROM' | 'MOVED_TO' | 'SPLIT_FROM' | 'SPLIT_TO' | 'ADDED'
      summary: string
      rationale: string
      counterpart_date: string | null
    }>
  }>
}

export type ProgressSummary = {
  completed_tasks: number
  total_tasks: number
  completed_required_tasks: number
  total_required_tasks: number
  percentage: number
}

export type FeedbackRecord = {
  id: string
  task_id: string | null
  feedback_kind: 'TASK_BLOCKED' | 'DAY_PACE'
  reason_code: string
  intent_code: string | null
  detail_text: string | null
  status: string
  created_at: string
}

export type FeedbackAdjustment = {
  id: string
  feedback_id: string
  adjustment_kind: string
  status: 'PENDING_REVIEW' | 'APPLIED' | 'DISMISSED'
  title: string
  summary: string
  rationale: string
  impact: string[]
  created_at: string
  decided_at: string | null
}

export type ExecutionDayLog = {
  id: string
  scheduled_date: string
  status: string
  summary: string
  metrics: Record<string, number | string>
  adjustments: string[]
  updated_at: string
}

export type TodayCapacity = {
  planned_minutes: number
  remaining_minutes: number
  remaining_tasks: number
  load_status: 'REST_DAY' | 'DONE' | 'HEAVY' | 'MANAGEABLE'
}

export type CommitmentProgress = {
  label: string
  cadence: 'TOTAL' | 'PER_WEEK' | 'PER_EXECUTION_DAY'
  period_start: string | null
  period_end: string | null
  target_occurrences: number
  completed_occurrences: number
  remaining_occurrences: number
  completed_minutes: number
  partial_minutes: number
  remaining_minutes: number
  risk: 'COMPLETE' | 'ON_TRACK' | 'DUE_SOON' | 'AT_RISK' | 'NO_DEADLINE'
  source_text: string
}

export type CommitmentAlert = {
  label: string
  risk: 'DUE_SOON' | 'AT_RISK'
  severity: 'WARNING' | 'URGENT'
  title: string
  message: string
  action: 'DISCUSS' | 'REVIEW_PLAN'
  period_end: string | null
  remaining_occurrences: number
  dedupe_key?: string
  event_id?: string | null
  status?: 'OPEN' | 'READ' | 'DISMISSED'
}

export type ReminderEvent = {
  id: string
  goal_id: string
  plan_version_id: string | null
  dedupe_key: string
  label: string
  risk: 'DUE_SOON' | 'AT_RISK'
  severity: 'WARNING' | 'URGENT'
  title: string
  message: string
  action: 'DISCUSS' | 'REVIEW_PLAN'
  period_end: string | null
  remaining_occurrences: number
  status: 'OPEN' | 'READ' | 'DISMISSED'
  first_seen_at: string
  last_seen_at: string
  read_at: string | null
  dismissed_at: string | null
}

export type FocusSession = {
  id: string
  task_id: string | null
  task_title: string | null
  planned_minutes: number
  actual_seconds: number
  interruptions: number
  status: 'COMPLETED' | 'ENDED_EARLY'
  completed_at: string
}

export type QuickCapture = {
  id: string
  content: string
  status: 'INBOX'
  created_at: string
}

export type ExecutionInactivity = {
  level: 'NONE' | 'MISSED_ONCE' | 'PAUSED_3_DAYS' | 'PAUSED_7_DAYS'
  inactive_days: number
  missed_execution_days: number
  missed_dates: string[]
  partial_execution_days?: number
  partial_dates?: string[]
  interrupted_execution_days?: number
  interrupted_dates?: string[]
  last_activity_date: string | null
  latest_missed_date: string | null
  recovery_deferred: boolean
  has_new_missed_work: boolean
}

export type RecoveryShiftPreview = {
  time_preview?: RecoveryTimePreview | null
  id: string
  status: 'PREVIEW' | 'APPLIED'
  mode: 'SHIFT_FORWARD'
  source_plan_version_id: string
  result_plan_version_id: string | null
  anchor_date: string
  original_end_date: string
  projected_end_date: string
  missed_execution_days: number
  locked_day_ids: string[]
  locked_task_ids: string[]
  carryover_task_ids: string[]
  changes: Array<{
    plan_day_id: string
    code: string
    primary_goal: string
    from_date: string
    to_date: string
  }>
  content_changed: boolean
  model_called: boolean
  reason_codes: AdjustmentReasonCode[]
}

export type AdjustmentReasonCode =
  | 'MISSED_EXECUTION'
  | 'DEADLINE_PROTECTION'
  | 'CAPACITY_INSUFFICIENT'
  | 'HISTORY_LOCKED'
  | 'CONSTRAINT_CHANGE'
  | 'USER_REQUESTED'

export type RecoveryTimePreview = {
  mode: string
  timezone: string
  before: RecoveryTimeItem[]
  after: RecoveryTimeItem[]
  unallocated_count: number
  fingerprint: string
  diagnostics_only?: boolean
}

export type RecoveryTimeItem = {
  title: string; date: string; start: string | null; end: string | null
  minutes: number; state: 'SCHEDULED' | 'UNALLOCATED' | 'FLEXIBLE'
  source_plan_version_id?: string | null
  source_task_id?: string | null
  lineage_task_id?: string | null
  source_work_item_id?: string | null
  work_item_window_id?: string | null
  source_date?: string | null
  remaining_minutes?: number | null
  reason_code?: string | null
  reason_detail?: string | null
  allowed_actions?: Array<'ADJUST_AVAILABILITY' | 'CHANGE_RECOVERY_DATE' | 'REVIEW_DEPENDENCY' | 'REVIEW_PLAN' | 'KEEP_PENDING'>
}

export type RecoveryCatchupPreview = {
  recovery_scope?: RecoveryScope | null
  time_preview?: RecoveryTimePreview | null
  id: string
  status: 'PREVIEW' | 'APPLIED'
  mode: 'KEEP_DEADLINE'
  source_plan_version_id: string
  result_plan_version_id: string | null
  anchor_date: string
  original_end_date: string
  missed_dates: string[]
  backlog_tasks: number
  backlog_minutes: number
  remaining_execution_days: number
  available_non_execution_days: number
  required_extra_minutes_per_day: number
  include_non_execution_days: boolean
  programmatic_feasible: boolean
  model_required: boolean
  backlog_cleared_date: string | null
  allocation: Array<{
    scheduled_date: string
    day_type: 'EXECUTION' | 'NON_EXECUTION'
    added_tasks: number
    added_minutes: number
    original_minutes: number
    total_minutes: number
  }>
  reason_codes: AdjustmentReasonCode[]
}

export type RecoveryScope = {
  source_plan_version_id: string
  items: Array<{ source_task_id: string; lineage_task_id: string; title: string; source_date: string; minutes: number; remaining_minutes: number; selected: boolean; prerequisite_task_ids: string[]; can_split: boolean; split_reason: string | null }>
  selected_task_ids: string[]
  deferred_task_ids: string[]
  selected_minutes: number
  deferred_minutes: number
  fingerprint: string
}

export type GenerationBatch = {
  id: string
  scope_type: string
  scope: Record<string, unknown>
  status: string
  attempt_count: number
  failure_code: string | null
  failure_message: string | null
  staged_result: Record<string, unknown> | null
}

export type ExplorationExecutionState = {
  generated_through_week: number
  total_weeks: number
  next_unplanned_week: number | null
  current_window_completed: boolean
  pending_window_start_week: number | null
  pending_window_end_week: number | null
}

export type ExecutionProgress = {
  total_focus_minutes?: number
  applied_adjustments?: number
  goal_id: string
  goal_title: string
  plan_revision: number
  schedule_type: 'OUTCOME' | 'RHYTHM' | 'CALENDAR'
  current_stage_order: number | null
  current_stage_title: string | null
  day_progress: ProgressSummary
  stage_progress: ProgressSummary
  overall_progress: ProgressSummary
  exploration?: ExplorationExecutionState | null
  stage_summary: string
  overall_summary: string
  trend: {
    window_days: number
    completed_days: number
    completion_rate: number
    consecutive_struggle_days: number
    consecutive_easy_days: number
    planned_minutes: number
    focus_minutes: number
    status: 'LEARNING' | 'STABLE' | 'NEEDS_REVIEW' | 'EXPECTED_RAMP' | 'AHEAD'
    summary: string
    should_offer_adjustment: boolean
    recommendation_title: string | null
    recommendation_request: string | null
  }
  rhythm: null | {
    cycle_start: string
    cycle_end: string
    target_sessions: number
    completed_sessions: number
    remaining_sessions: number
    observed_cycles: number
    average_completed_sessions: number
    adherence_rate: number
    planned_minutes: number
    focus_minutes: number
    pace_status: 'LEARNING' | 'BELOW' | 'SUSTAINABLE' | 'ABOVE'
    forecast_summary: string
  }
  review_signal: {
    recommended: boolean
    trigger_type: 'NONE' | 'OUTCOME_CHECKPOINT' | 'RHYTHM_SAMPLE_READY' | 'RHYTHM_MISMATCH' | 'DEADLINE_RISK' | 'USER_REQUEST' | 'DIRECTION_CHANGE'
    reason: string | null
    timing?: 'NOW' | 'FUTURE'
    not_before?: string | null
  }
  inactivity?: ExecutionInactivity
  recent_days: Array<{
    scheduled_date: string
    code: string
    stage_order: number
    stage_title: string
    primary_goal: string
    status: string
    completed_tasks: number
    total_tasks: number
    carried_over_tasks?: number
    focus_minutes: number
    feedback_count: number
    summary: string | null
  }>
  feedback_timeline: Array<FeedbackRecord & {
    scheduled_date: string
    task_title: string | null
    adjustment: FeedbackAdjustment | null
  }>
  revision_history: Array<{
    id: string
    revision: number
    status: string
    feedback: string | null
    changes: string[]
    created_at: string
  }>
}

export type ExecutionDiscussionResult = {
  assistant_message: string
  intents: Array<{
    type: 'QUESTION' | 'INSIGHT' | 'CONSTRAINT_CHANGE' | 'PROGRESS_REPORT' | 'DIFFICULTY' | 'PLAN_CHANGE_REQUEST' | 'ROUTE_CHANGE_SIGNAL' | 'REVIEW_REQUEST' | 'NO_ACTION'
    summary: string
    confidence: number
    memory_key: string
    operation: 'UPSERT' | 'RETRACT'
  }>
  memory_candidates: Array<{
    insight_type: 'LEARNING' | 'PREFERENCE' | 'CONSTRAINT' | 'CAPABILITY' | 'DIFFICULTY' | 'STRATEGY'
    content: string
    scope: 'GOAL' | 'STAGE' | 'PLAN' | 'TEMPORARY'
    confidence: number
  }>
  review_signal: ExecutionProgress['review_signal']
  adjustment_signal: {
    recommended: boolean
    scope: 'NEAR_TERM' | 'STAGE' | 'REMAINING_PLAN' | 'ROUTE' | null
    reason: string | null
    request: string | null
    target_schedule_type?: 'OUTCOME' | 'RHYTHM' | 'CALENDAR' | null
  }
  questions: string[]
}

export type ExecutionInsight = {
  id: string
  insight_type: string
  content: string
  scope: string
  confidence: number
  status: 'PROPOSED' | 'CONFIRMED' | 'DISMISSED' | 'SUPERSEDED'
  source_message_id: string
  created_at: string
  decided_at: string | null
  memory_key?: string | null
}

export type ExecutionDiscussionWorkspace = {
  goal_id: string
  goal_title: string
  goal_status: string
  planning_mode: 'EXPLORATORY' | 'COMMITTED'
  schedule_type: 'OUTCOME' | 'RHYTHM' | 'CALENDAR'
  messages: ConversationMessage[]
  insights: ExecutionInsight[]
  active_review: null | {
    id: string
    trigger_type: string
    status: 'SCHEDULED' | 'SUGGESTED' | 'DISCUSSING' | 'DECIDED' | 'DISMISSED'
    reason: string
    evidence: Record<string, unknown>
    summary: string | null
    decision: string | null
    created_at: string
    updated_at: string
    not_before?: string | null
  }
  progress: ExecutionProgress
  latest_analysis: ExecutionDiscussionResult | null
  inherited_discussion?: boolean
  latest_research: ResearchSummary | null
}

export type TodayWorkspace = {
  goal_id: string
  goal_title: string
  status: string
  today: string
  schedule_type: 'OUTCOME' | 'RHYTHM' | 'CALENDAR'
  is_execution_day: boolean
  next_execution_date: string | null
  milestone_title: string | null
  scheduled_day: ScheduledDay | null
  day: PlanDay | null
  day_completed: boolean
  day_progress: ProgressSummary
  stage_progress: ProgressSummary
  overall_progress: ProgressSummary
  exploration?: ExplorationExecutionState | null
  capacity: TodayCapacity
  commitment_progress: CommitmentProgress[]
  commitment_alerts?: CommitmentAlert[]
  inactivity?: ExecutionInactivity
  feedbacks: FeedbackRecord[]
  adjustments: FeedbackAdjustment[]
  day_log: ExecutionDayLog | null
  carry_forward_tasks: Array<{
    task_id: string
    title: string
    description: string
    estimate_minutes: number
    scheduled_date: string
    day_code: string
    can_carry: boolean
    blocked_reason: string | null
    can_split: boolean
    split_reason: string | null
  }>
}

export type MaterialRevision = NonNullable<GoalMaterial['latest_revision']>

export type MaterialReader = { source_id: string; title: string; revision: MaterialRevision; offset: number; next_offset: number | null; total_chars: number; text: string; outline: Array<{ title: string; offset: number; sequence: number }>; outline_truncated: boolean; warnings: string[] }

export type MaterialDiff = { lines: Array<{ kind: string; text: string; old_line?: number; new_line?: number }>; added_lines: number; removed_lines: number; truncated: boolean; compared_chars: number[]; warning: string }

export type ResearchResponsePhase = 'CLARIFICATION' | 'ROUTE' | 'PLAN' | 'EXECUTION' | 'RECOVERY'

export type ResearchResponseRequest = {
  id: string; goal_id: string; conversation_id: string; phase: ResearchResponsePhase
  intent: 'MESSAGE' | 'EXPLAIN' | 'REVISE'; question: string; status: string
  fallback: boolean; attempt_count: number; response_message_id: string | null
  error_message: string | null; run: ResearchRun
}

export type ResearchReadiness = {
  ready: boolean; status: string; components: Record<string, string>
  first_ready_ms?: number | null; elapsed_ms?: number
  active_clients?: number; idle_seconds_remaining?: number | null
}

export type RecoverySplitPreview = { id: string; status: 'PREVIEW' | 'APPLIED'; source_plan_version_id: string; result_plan_version_id: string | null; source_task_id: string; title: string; original_minutes: number; part_minutes: number[]; result_selected_task_ids: string[] }

export type PlanScheduleAllocation = {
  scheduled_days: number
  assigned_tasks: number
  unallocated_tasks: number
  schedule: Array<{ scheduled_date: string; assignments: Array<{ title: string; start_local: string; end_local: string }>; unallocated_titles: string[] }>
}

export type TaskRecommendationItem = {
  task_id: string; title: string; scheduling_policy: string; scheduled_date: string
  estimate_minutes: number; group_key: string | null; eligible_from: string | null; eligible_to: string | null
  remaining_value: number | null; target_unit: string | null; reason: string
  allowed_actions: Array<'ARRANGE_TODAY' | 'RESCHEDULE' | 'VIEW_DATE'>
}

export type TaskRecommendationPreview = {
  id: string; fingerprint: string; date: string; strategy: 'RULES' | 'AI' | 'AI_FALLBACK'
  summary?: string
  capacity: { available_minutes: number; current_task_minutes: number; recommended_minutes: number; spare_minutes: number; remaining_minutes: number }
  items: TaskRecommendationItem[]
  blocked_summary: Array<{ task_id: string; title: string; scheduled_date: string; reason_code: string; reason: string }>
  blocked_count: number; warnings: string[]
}

export type FlexibleTaskDecisionPreview = {
  id: string
  status: 'PREVIEW' | 'APPLIED'
  action: 'DEFER' | 'ARRANGE_TODAY' | 'RESCHEDULE'
  task_id: string
  title: string
  scheduling_policy: string
  source_date: string | null
  target_date: string | null
  planned_minutes: number
  capacity_minutes: number
  over_capacity_minutes: number
  competing_tasks: string[]
  shared_available_minutes?: number | null
  proposed_start_local?: string | null
  proposed_end_local?: string | null
  blockers: string[]
  warnings: string[]
  affected_task_ids: string[]
  remaining_commitments: CommitmentProgress[]
  can_apply: boolean
  fingerprint: string
}
