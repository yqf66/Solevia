from planner_api.model_connection import model_required
from datetime import date, timedelta
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from planner_api.ai import build_clarification_provider
from planner_api.config import get_settings
from planner_api.conversation_memory import read_message_page
from planner_api.database import get_db_session
from planner_api.day_schedule_service import DayScheduleService
from planner_api.execution_calendar_service import ExecutionCalendarService
from planner_api.execution_discussion_service import ExecutionDiscussionService
from planner_api.execution_service import ExecutionService
from planner_api.recommendation_schemas import RecommendationPreview, RecommendationRequest
from planner_api.recommendation_service import RecommendationService
from planner_api.generation_orchestration import GenerationOrchestrator
from planner_api.holiday_calendar import suggest_holiday_region
from planner_api.light_arrangement_ai import build_light_provider
from planner_api.light_arrangement_service import LightArrangementService
from planner_api.light_schemas import (
    LightDraftCreate,
    LightDraftEdit,
    LightDraftRead,
    LightRevisionRequest,
    LightTaskDraft,
)
from planner_api.manual_plan_service import ManualPlanService
from planner_api.models import ClarificationAnalysisJob, Goal, utc_now
from planner_api.plan_ai import build_plan_provider
from planner_api.plan_discussion_ai import build_plan_discussion_provider
from planner_api.plan_generation import run_plan_generation, run_recovery_generation
from planner_api.plan_service import PlanService
from planner_api.research.persistence import ResearchRunService
from planner_api.research.schemas import ResearchRunCreate, ResearchRunRead
from planner_api.route_ai import build_route_provider
from planner_api.route_explanation_ai import build_route_explanation_provider
from planner_api.route_service import RouteService
from planner_api.schemas import (
    AvailabilityWindowRead,
    AvailabilityWindowsUpdate,
    CandidatePoolRead,
    CandidateReplacementPreviewRead,
    CandidateReplacementRequest,
    HybridMigrationAuditRead,
    CarryForwardRequest,
    ClarificationWorkspace,
    CommitmentReminderUpdate,
    ConversationMessagePage,
    DayAllocationApply,
    DayAllocationBatchRequest,
    DayAllocationPreview,
    DayAllocationRequest,
    DayPaceFeedbackCreate,
    ExecutionCalendarRead,
    ExecutionDayMoveApply,
    ExecutionDayMoveRead,
    ExecutionDayMoveRequest,
    ExecutionDiscussionCreate,
    ExecutionDiscussionWorkspace,
    ExecutionInactivityRead,
    ExecutionInsightDecision,
    ExecutionPreview,
    ExecutionProgressRead,
    ExecutionScheduleRequest,
    ExecutionSetup,
    ExplorationWindowRequest,
    FactUpdate,
    FlexibleTaskArrangeRequest,
    FlexibleTaskDecisionApply,
    FlexibleTaskDecisionPreviewRead,
    FlexibleTaskDecisionRequest,
    FlexibleTaskRescheduleRequest,
    FocusSessionCreate,
    FocusSessionRead,
    GenerationBatchCreate,
    GenerationBatchRead,
    GenerationRunRead,
    GoalCompleteRequest,
    GoalCreate,
    GoalDeleteRequest,
    GoalLifecycleRead,
    GoalListItem,
    GoalPauseRequest,
    GoalRenameRequest,
    GoalSettingsUpdate,
    HolidayCalendarRead,
    HolidayRegionSuggestionRead,
    ManualPlanCreate,
    ManualPlanRead,
    ManualTaskDelete,
    ManualTaskUpdate,
    MessageCreate,
    ModelConnectionRead,
    ModelConnectionUpdate,
    PlanAdjustmentApply,
    PlanAdjustmentPreviewRead,
    PlanFeedback,
    PlanGenerationRequest,
    PlanningApproachSelection,
    PlanningBriefRead,
    PlanScheduleAllocationRead,
    PlanWorkspace,
    QuickCaptureCreate,
    QuickCaptureRead,
    RecoveryAIRequest,
    RecoveryDecisionRequest,
    RecoveryCatchupApply,
    RecoveryCatchupPreviewRead,
    RecoveryCatchupRequest,
    RecoverySplitRequest,
    RecoverySplitPreviewRead,
    RecoveryDiscussionRequest,
    RecoveryDeferRequest,
    RecoveryShiftApply,
    RecoveryShiftPreviewRead,
    ReminderEventAction,
    ReminderEventRead,
    RetrospectiveCompletionRequest,
    RouteFeedback,
    RouteWorkspace,
    ScheduleFoundationRead,
    SchedulePreferenceRead,
    SchedulePreferenceUpdate,
    ScheduleTypeSelection,
    SmartCalendarRead,
    SmartCalendarRequest,
    TaskFeedbackCreate,
    TaskProgressUpdate,
    TodayTaskScheduleRequest,
    TodayTaskScheduleResult,
    TodayWorkspace,
    TriggeredTaskActivationApply,
    TriggeredTaskActivationPreviewRead,
    TriggeredTaskActivationRequest,
    TriggeredTaskRead,
    UnifiedTodayRead,
    WorkItemDailyPreviewRead,
    WorkspaceAvailabilityUpdate,
    WorkspaceAvailabilityWindowRead,
    WorkspaceEffectiveAvailabilityRead,
    WorkspaceScheduleProfileRead,
    WorkspaceScheduleProfileUpdate,
)
from planner_api.service import ClarificationService, run_pending_clarification_analysis
from planner_api.today_service import TodayService
from planner_api.user_settings_service import UserSettingsService
from planner_api.fixed_activity_service import FixedActivityService, FixedActivityMoveRequest, FixedActivityMoveConfirmation

router = APIRouter(prefix="/api", tags=["clarification"])

SessionDependency = Annotated[Session, Depends(get_db_session)]


def get_service(session: SessionDependency) -> ClarificationService:
    return ClarificationService(session, build_clarification_provider(get_settings()), defer_analysis=True)


ServiceDependency = Annotated[ClarificationService, Depends(get_service)]


def get_route_service(session: SessionDependency) -> RouteService:
    settings = get_settings()
    return RouteService(
        session,
        build_route_provider(settings),
        build_route_explanation_provider(settings),
    )


RouteServiceDependency = Annotated[RouteService, Depends(get_route_service)]


def get_plan_service(session: SessionDependency) -> PlanService:
    settings = get_settings()
    return PlanService(
        session,
        build_plan_provider(settings),
        build_plan_discussion_provider(settings),
        enable_hybrid_work_items=settings.enable_hybrid_work_items,
        hybrid_rollout_mode=settings.hybrid_rollout_mode,
        hybrid_rollout_percentage=settings.hybrid_rollout_percentage,
    )


PlanServiceDependency = Annotated[PlanService, Depends(get_plan_service)]


def get_execution_service(session: SessionDependency) -> ExecutionService:
    return ExecutionService(session)


ExecutionServiceDependency = Annotated[ExecutionService, Depends(get_execution_service)]


def get_execution_discussion_service(session: SessionDependency) -> ExecutionDiscussionService:
    return ExecutionDiscussionService(session, build_plan_discussion_provider(get_settings()))


ExecutionDiscussionServiceDependency = Annotated[
    ExecutionDiscussionService, Depends(get_execution_discussion_service)
]


def get_research_service(session: SessionDependency) -> ResearchRunService:
    return ResearchRunService(session)


ResearchRunServiceDependency = Annotated[
    ResearchRunService, Depends(get_research_service)
]


def get_manual_plan_service(session: SessionDependency) -> ManualPlanService:
    return ManualPlanService(session)


ManualPlanServiceDependency = Annotated[ManualPlanService, Depends(get_manual_plan_service)]


def get_light_arrangement_service(session: SessionDependency) -> LightArrangementService:
    settings = get_settings()
    provider = None
    if settings.llm_provider != "fake" and settings.model_runtime_configured:
        provider = build_light_provider(settings)
    return LightArrangementService(session, provider)


LightArrangementServiceDependency = Annotated[
    LightArrangementService, Depends(get_light_arrangement_service)
]


def get_today_service(session: SessionDependency) -> TodayService:
    return TodayService(session)


TodayServiceDependency = Annotated[TodayService, Depends(get_today_service)]


@router.post("/goals", response_model=ClarificationWorkspace, status_code=201)
@model_required
def create_goal(
    payload: GoalCreate,
    background_tasks: BackgroundTasks,
    service: ServiceDependency,
):
    workspace = service.create_goal(payload.raw_input, payload.timezone)
    if any(job.status in {"PENDING", "FAILED"} for job in workspace.analysis_jobs):
        background_tasks.add_task(run_pending_clarification_analysis, workspace.goal_id)
    return workspace


@router.post("/goals/initialize", response_model=ClarificationWorkspace, status_code=201)
def initialize_goal(payload: GoalCreate, service: ServiceDependency):
    return service.create_goal(
        payload.raw_input,
        payload.timezone,
        defer_initial_response=True,
        web_research_enabled=payload.web_research_enabled,
    )


@router.post(
    "/goals/{goal_id}/clarification/start",
    response_model=ClarificationWorkspace,
)
@model_required
def start_initial_clarification(goal_id: str, service: ServiceDependency):
    return service.complete_initial_response(goal_id)


@router.get("/goals", response_model=list[GoalListItem])
def list_goals(service: ServiceDependency):
    return service.list_goals()


@router.post("/goals/status-check", response_model=list[GoalListItem])
def check_goal_statuses(service: ExecutionServiceDependency):
    return service.check_goal_statuses()


@router.get("/goals/{goal_id}/lifecycle", response_model=GoalLifecycleRead)
def get_goal_lifecycle(goal_id: str, service: ExecutionServiceDependency):
    return service.get_lifecycle(goal_id)


@router.post("/goals/{goal_id}/pause", response_model=GoalLifecycleRead)
def pause_goal(
    goal_id: str,
    payload: GoalPauseRequest,
    service: ExecutionServiceDependency,
):
    return service.pause_goal(goal_id, payload)


@router.post("/goals/{goal_id}/resume", response_model=GoalLifecycleRead)
def resume_goal(goal_id: str, service: ExecutionServiceDependency):
    return service.resume_goal(goal_id)


@router.post("/goals/{goal_id}/complete", response_model=GoalLifecycleRead)
def complete_goal(
    goal_id: str,
    payload: GoalCompleteRequest,
    service: ExecutionServiceDependency,
):
    return service.complete_goal(goal_id, payload)


@router.post("/goals/{goal_id}/archive", response_model=GoalLifecycleRead)
def archive_goal(goal_id: str, service: ExecutionServiceDependency):
    return service.archive_goal(goal_id)


@router.patch("/goals/{goal_id}/settings", response_model=GoalLifecycleRead)
def update_goal_settings(
    goal_id: str,
    payload: GoalSettingsUpdate,
    service: ExecutionServiceDependency,
):
    return service.update_goal_settings(goal_id, payload)


@router.patch("/goals/{goal_id}", response_model=GoalLifecycleRead)
def rename_goal(
    goal_id: str,
    payload: GoalRenameRequest,
    service: ExecutionServiceDependency,
):
    return service.rename_goal(goal_id, payload.title)


@router.delete("/goals/{goal_id}", status_code=204)
def delete_goal(
    goal_id: str,
    payload: GoalDeleteRequest,
    service: ExecutionServiceDependency,
) -> Response:
    service.delete_goal(goal_id, payload)
    return Response(status_code=204)


@router.get("/goals/{goal_id}/clarification", response_model=ClarificationWorkspace)
def get_clarification(goal_id: str, service: ServiceDependency):
    return service.get_workspace(goal_id)


@router.get(
    "/goals/{goal_id}/conversations/{phase}/messages",
    response_model=ConversationMessagePage,
)
def get_conversation_messages(
    goal_id: str,
    phase: str,
    session: SessionDependency,
    before_sequence: int | None = None,
    limit: int = 50,
):
    try:
        return read_message_page(
            session,
            goal_id,
            phase,
            before_sequence=before_sequence,
            limit=limit,
        )
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


@router.post("/goals/{goal_id}/messages", response_model=ClarificationWorkspace)
@model_required
def add_message(
    goal_id: str,
    payload: MessageCreate,
    background_tasks: BackgroundTasks,
    service: ServiceDependency,
):
    workspace = service.add_user_message(
        goal_id,
        payload.content,
        web_research_enabled=payload.web_research_enabled,
        research_run_id=payload.research_run_id,
        reuse_latest_assistant=payload.regenerate,
        material_source_ids=payload.material_source_ids,
    )
    if any(job.status in {"PENDING", "FAILED"} for job in workspace.analysis_jobs):
        background_tasks.add_task(run_pending_clarification_analysis, goal_id)
    return workspace


@router.post("/goals/{goal_id}/messages/retry", response_model=ClarificationWorkspace)
@model_required
def retry_failed_message(goal_id: str, service: ServiceDependency):
    return service.retry_last_failed_message(goal_id)


@router.post("/goals/{goal_id}/messages/regenerate", response_model=ClarificationWorkspace)
@model_required
def regenerate_message(goal_id: str, service: ServiceDependency):
    return service.regenerate_last_message(goal_id)


@router.post("/goals/{goal_id}/clarification/analysis/retry", response_model=ClarificationWorkspace)
@model_required
def retry_analysis(
    goal_id: str,
    background_tasks: BackgroundTasks,
    service: ServiceDependency,
):
    for job in service.session.scalars(
        select(ClarificationAnalysisJob).where(
            ClarificationAnalysisJob.goal_id == goal_id,
            (ClarificationAnalysisJob.status.in_(["PENDING", "FAILED"]))
            | ((ClarificationAnalysisJob.status == "PROCESSING")
               & (ClarificationAnalysisJob.updated_at <= utc_now() - timedelta(minutes=10))),
        )
    ):
        job.status = "PENDING"
        job.next_retry_at = utc_now()
        if job.attempts >= job.max_attempts:
            job.attempts = 0
        job.completed_at = None
    service.session.commit()
    background_tasks.add_task(run_pending_clarification_analysis, goal_id)
    return service.get_workspace(goal_id)


@router.patch("/goals/{goal_id}/facts/{fact_id}", response_model=ClarificationWorkspace)
def update_fact(
    goal_id: str,
    fact_id: str,
    payload: FactUpdate,
    service: ServiceDependency,
):
    return service.update_fact(goal_id, fact_id, payload.action, payload.value)


@router.post("/goals/{goal_id}/requirements/draft", response_model=ClarificationWorkspace)
@model_required
def draft_requirement(goal_id: str, service: ServiceDependency):
    return service.create_requirement_draft(goal_id)


@router.post("/goals/{goal_id}/requirements/confirm", response_model=ClarificationWorkspace)
def confirm_requirement(goal_id: str, service: ServiceDependency):
    return service.confirm_requirement(goal_id)


@router.put("/goals/{goal_id}/planning-approach", response_model=ClarificationWorkspace)
def select_planning_approach(
    goal_id: str,
    payload: PlanningApproachSelection,
    service: ServiceDependency,
):
    return service.select_planning_approach(goal_id, payload.planning_mode)


@router.put("/goals/{goal_id}/schedule-type", response_model=ClarificationWorkspace)
def select_schedule_type(
    goal_id: str,
    payload: ScheduleTypeSelection,
    service: ServiceDependency,
):
    return service.select_schedule_type(goal_id, payload.schedule_type)


@router.post("/goals/{goal_id}/requirements/reopen", response_model=ClarificationWorkspace)
def reopen_requirement(goal_id: str, service: ServiceDependency):
    return service.reopen_requirement(goal_id)


@router.post(
    "/goals/{goal_id}/requirements/changes/discard",
    response_model=ClarificationWorkspace,
)
def discard_requirement_changes(goal_id: str, service: ServiceDependency):
    return service.discard_requirement_changes(goal_id)


@router.get("/goals/{goal_id}/route", response_model=RouteWorkspace)
def get_route(goal_id: str, service: RouteServiceDependency):
    return service.get_workspace(goal_id)


@router.post("/goals/{goal_id}/route/draft", response_model=RouteWorkspace)
@model_required
def draft_route(goal_id: str, service: RouteServiceDependency):
    return service.draft_route(goal_id)


@router.post("/goals/{goal_id}/route/revise", response_model=RouteWorkspace)
@model_required
def revise_route(goal_id: str, payload: RouteFeedback, service: RouteServiceDependency):
    return service.revise_route(
        goal_id,
        payload.content,
        web_research_enabled=payload.web_research_enabled,
        research_run_id=payload.research_run_id,
        regenerate=payload.regenerate,
        material_source_ids=payload.material_source_ids,
    )


@router.post("/goals/{goal_id}/route/explain", response_model=RouteWorkspace)
@model_required
def explain_route(goal_id: str, payload: RouteFeedback, service: RouteServiceDependency):
    return service.explain_route(
        goal_id,
        payload.content,
        web_research_enabled=payload.web_research_enabled,
        research_run_id=payload.research_run_id,
        regenerate=payload.regenerate,
        material_source_ids=payload.material_source_ids,
    )


@router.post("/goals/{goal_id}/route/confirm", response_model=RouteWorkspace)
def confirm_route(goal_id: str, service: RouteServiceDependency):
    return service.confirm_route(goal_id)


@router.get("/goals/{goal_id}/plan", response_model=PlanWorkspace)
def get_plan(goal_id: str, service: PlanServiceDependency):
    return service.get_workspace(goal_id)


@router.post("/goals/{goal_id}/research/runs", response_model=ResearchRunRead, status_code=202)
@model_required
def enqueue_research_run(
    goal_id: str,
    payload: ResearchRunCreate,
    service: ResearchRunServiceDependency,
):
    return service.enqueue(
        goal_id,
        payload.question,
        enabled=payload.enabled,
        conversation_id=payload.conversation_id,
        idempotency_key=payload.idempotency_key,
    )


@router.get("/goals/{goal_id}/research/runs", response_model=list[ResearchRunRead])
def list_research_runs(goal_id: str, service: ResearchRunServiceDependency):
    return service.list(goal_id)


@router.get("/goals/{goal_id}/research/runs/{run_id}", response_model=ResearchRunRead)
def get_research_run(
    goal_id: str,
    run_id: str,
    service: ResearchRunServiceDependency,
):
    return service.get(goal_id, run_id)


@router.post("/goals/{goal_id}/research/runs/{run_id}/cancel", response_model=ResearchRunRead)
def cancel_research_run(
    goal_id: str,
    run_id: str,
    service: ResearchRunServiceDependency,
):
    return service.cancel(goal_id, run_id)


@router.post("/goals/{goal_id}/research/runs/{run_id}/resume", response_model=ResearchRunRead)
@model_required
def resume_research_run(
    goal_id: str,
    run_id: str,
    service: ResearchRunServiceDependency,
):
    return service.resume(goal_id, run_id)


@router.post("/goals/{goal_id}/research/runs/{run_id}/retry", response_model=ResearchRunRead)
@model_required
def retry_research_run(
    goal_id: str,
    run_id: str,
    service: ResearchRunServiceDependency,
):
    return service.retry(goal_id, run_id)


@router.get("/goals/{goal_id}/plan/compiler/brief", response_model=PlanningBriefRead)
def get_planning_brief(goal_id: str, service: PlanServiceDependency):
    return service.get_planning_brief(goal_id)


@router.get("/goals/{goal_id}/plan/compiler/batches", response_model=list[GenerationBatchRead])
def get_generation_batches(goal_id: str, service: PlanServiceDependency):
    return service.get_generation_batches(goal_id)


@router.get(
    "/goals/{goal_id}/generation-runs",
    response_model=list[GenerationRunRead],
)
def get_generation_runs(goal_id: str, session: SessionDependency):
    goal = session.get(Goal, goal_id)
    if goal is None:
        raise HTTPException(status_code=404, detail="Goal not found")
    return GenerationOrchestrator(session).list(goal_id)


@router.post(
    "/goals/{goal_id}/plan/compiler/batches",
    response_model=GenerationBatchRead,
    status_code=201,
)
@model_required
def create_generation_batch(
    goal_id: str,
    payload: GenerationBatchCreate,
    service: PlanServiceDependency,
):
    return service.create_generation_batch(goal_id, payload)


@router.post(
    "/goals/{goal_id}/plan/compiler/batches/{batch_id}/retry",
    response_model=GenerationBatchRead,
)
@model_required
def retry_generation_batch(goal_id: str, batch_id: str, service: PlanServiceDependency):
    return service.retry_recovery_batch(goal_id, batch_id)


@router.post("/goals/{goal_id}/plan/draft", response_model=PlanWorkspace)
@model_required
def draft_plan(goal_id: str, service: PlanServiceDependency):
    return service.draft_plan(goal_id)


@router.post("/goals/{goal_id}/plan/details/next", response_model=PlanWorkspace)
@model_required
def draft_next_plan_details(goal_id: str, service: PlanServiceDependency):
    return service.draft_next_details(goal_id)


@router.post("/goals/{goal_id}/plan/generation/start", response_model=PlanWorkspace)
@model_required
def start_plan_generation(
    goal_id: str,
    payload: PlanGenerationRequest,
    background_tasks: BackgroundTasks,
    service: PlanServiceDependency,
):
    job, created = service.prepare_generation(
        goal_id,
        payload.mode,
    )
    if created:
        background_tasks.add_task(run_plan_generation, job.id)
    return service.get_workspace(goal_id)


@router.post("/goals/{goal_id}/plan/explain", response_model=PlanWorkspace)
@model_required
def explain_plan(goal_id: str, payload: PlanFeedback, service: PlanServiceDependency):
    return service.explain_plan(
        goal_id,
        payload.content,
        web_research_enabled=payload.web_research_enabled,
        research_run_id=payload.research_run_id,
        regenerate=payload.regenerate,
        material_source_ids=payload.material_source_ids,
    )


@router.post("/goals/{goal_id}/plan/revise", response_model=PlanWorkspace)
@model_required
def revise_plan(goal_id: str, payload: PlanFeedback, service: PlanServiceDependency):
    return service.revise_plan(
        goal_id,
        payload.content,
        web_research_enabled=payload.web_research_enabled,
        research_run_id=payload.research_run_id,
        regenerate=payload.regenerate,
        material_source_ids=payload.material_source_ids,
        recovery_options=payload.recovery_options.model_dump(mode="json") if payload.recovery_options else None,
    )


@router.post("/goals/{goal_id}/plan/recovery-decision", response_model=PlanWorkspace)
def decide_recovery(goal_id: str, payload: RecoveryDecisionRequest, service: PlanServiceDependency):
    return service.decide_recovery(goal_id, payload.decision, payload.token)


@router.post("/goals/{goal_id}/execution/recovery/ai", response_model=PlanWorkspace)
@model_required
def revise_recovery_plan(
    goal_id: str,
    payload: RecoveryAIRequest,
    background_tasks: BackgroundTasks,
    service: PlanServiceDependency,
):
    workspace = service.revise_recovery_plan(
        goal_id,
        payload.missed_dates,
        payload.backlog_minutes,
        payload.original_end_date,
        payload.include_non_execution_days,
        payload.preview_id,
        payload.instruction,
        research_run_id=payload.research_run_id,
        availability_offer=payload.availability_offer.model_dump(mode="json") if payload.availability_offer else None,
    )
    if workspace.pending_adjustment:
        background_tasks.add_task(run_recovery_generation, goal_id)
    return workspace


@router.post("/goals/{goal_id}/execution/recovery/discussion", response_model=PlanWorkspace)
def handoff_recovery_discussion(
    goal_id: str, payload: RecoveryDiscussionRequest, service: PlanServiceDependency,
):
    return service.handoff_recovery_discussion(goal_id, payload.preview_id)


@router.post("/goals/{goal_id}/execution/recovery/split-preview", response_model=RecoverySplitPreviewRead)
def preview_recovery_split(
    goal_id: str, payload: RecoverySplitRequest, service: ExecutionServiceDependency,
):
    return service.preview_recovery_split(goal_id, payload)


@router.post("/goals/{goal_id}/execution/recovery/split", response_model=RecoverySplitPreviewRead)
def apply_recovery_split(
    goal_id: str, payload: RecoveryCatchupApply, service: ExecutionServiceDependency,
):
    return service.apply_recovery_split(goal_id, payload.preview_id)


@router.post("/goals/{goal_id}/plan/adjustment/discard", response_model=PlanWorkspace)
def discard_plan_adjustment(goal_id: str, service: PlanServiceDependency):
    return service.discard_adjustment(goal_id)


@router.post("/goals/{goal_id}/plan/adjustment/apply", response_model=PlanWorkspace)
def apply_plan_adjustment(
    goal_id: str,
    service: PlanServiceDependency,
    payload: PlanAdjustmentApply | None = None,
):
    return service.apply_adjustment(
        goal_id,
        payload.preview_id if payload else None,
        payload.accept_unallocated if payload else False,
        [item.model_dump(mode="json") for item in payload.schedule_overrides] if payload else None,
    )


@router.post("/goals/{goal_id}/plan/adjustment/preview", response_model=PlanAdjustmentPreviewRead)
def preview_plan_adjustment(goal_id: str, service: PlanServiceDependency):
    return service.preview_adjustment(goal_id)


@router.post("/goals/{goal_id}/plan/confirm", response_model=PlanWorkspace)
def confirm_plan(goal_id: str, service: PlanServiceDependency):
    return service.confirm_plan(goal_id)


@router.post("/goals/{goal_id}/plan/exploration/draft", response_model=PlanWorkspace)
@model_required
def draft_exploration_window(
    goal_id: str,
    payload: ExplorationWindowRequest,
    service: PlanServiceDependency,
):
    return service.draft_exploration_window(goal_id, payload.mode)


@router.post("/goals/{goal_id}/plan/exploration/apply", response_model=PlanWorkspace)
def apply_exploration_window(goal_id: str, service: PlanServiceDependency):
    return service.apply_exploration_window(goal_id)


@router.post("/goals/{goal_id}/plan/exploration/discard", response_model=PlanWorkspace)
def discard_exploration_window(goal_id: str, service: PlanServiceDependency):
    return service.discard_exploration_window(goal_id)


@router.get("/goals/{goal_id}/execution/setup", response_model=ExecutionSetup)
def get_execution_setup(goal_id: str, service: ExecutionServiceDependency):
    return service.get_setup(goal_id)


@router.get(
    "/goals/{goal_id}/plan/daily-preview",
    response_model=WorkItemDailyPreviewRead,
)
def get_plan_daily_preview(goal_id: str, service: ExecutionServiceDependency):
    return service.preview_plan_days(goal_id)


@router.get("/schedule/profile", response_model=WorkspaceScheduleProfileRead)
def get_workspace_schedule_profile(service: ManualPlanServiceDependency):
    return service.get_profile()


@router.get("/goals/{goal_id}/fixed-activities")
def fixed_activities(goal_id: str, on_date: date, session: SessionDependency):
    return FixedActivityService(session).candidates(goal_id, on_date)


@router.post("/goals/{goal_id}/fixed-activities/preview")
def preview_fixed_activity(goal_id: str, payload: FixedActivityMoveRequest, session: SessionDependency):
    return FixedActivityService(session).preview(goal_id, payload)


@router.post("/goals/{goal_id}/fixed-activities/{preview_id}/apply")
def apply_fixed_activity(goal_id: str, preview_id: str, payload: FixedActivityMoveConfirmation, session: SessionDependency):
    return FixedActivityService(session).apply(goal_id, preview_id, payload.confirmed)


@router.post("/goals/{goal_id}/fixed-activities/{preview_id}/discard")
def discard_fixed_activity(goal_id: str, preview_id: str, session: SessionDependency):
    return FixedActivityService(session).discard(goal_id, preview_id)


@router.patch("/schedule/profile", response_model=WorkspaceScheduleProfileRead)
def update_workspace_schedule_profile(
    payload: WorkspaceScheduleProfileUpdate,
    service: ManualPlanServiceDependency,
):
    return service.update_profile(payload)


@router.get("/settings/model", response_model=ModelConnectionRead)
def get_model_connection():
    return UserSettingsService().get_model_connection()


@router.put("/settings/model", response_model=ModelConnectionRead)
def update_model_connection(payload: ModelConnectionUpdate, request: Request):
    manager = request.app.state.kernel
    with manager.lock:
        if get_settings().llm_provider != "codex" and (manager.active or manager.busy_check()):
            raise HTTPException(409, "当前还有生成或资料研究在进行，请等待完成后再修改 API 配置。")
        return UserSettingsService().update_model_connection(payload)


@router.get("/schedule/profile/holidays", response_model=HolidayCalendarRead)
def get_workspace_holidays(
    start_date: date,
    end_date: date,
    service: ManualPlanServiceDependency,
):
    return service.get_holidays(start_date, end_date)


@router.get("/schedule/profile/holiday-suggestion", response_model=HolidayRegionSuggestionRead)
def get_holiday_region_suggestion(timezone: str, locale: str):
    return suggest_holiday_region(timezone, locale)


@router.put(
    "/schedule/profile/availability",
    response_model=list[WorkspaceAvailabilityWindowRead],
)
def replace_workspace_availability(
    payload: WorkspaceAvailabilityUpdate,
    service: ManualPlanServiceDependency,
):
    return service.replace_windows(payload)


@router.get(
    "/schedule/profile/availability",
    response_model=list[WorkspaceAvailabilityWindowRead],
)
def get_workspace_availability(service: ManualPlanServiceDependency):
    return service.get_windows()


@router.get(
    "/schedule/profile/availability/effective",
    response_model=WorkspaceEffectiveAvailabilityRead,
)
def get_effective_workspace_availability(
    on_date: date,
    service: ManualPlanServiceDependency,
):
    return service.get_effective_windows(on_date)


@router.post(
    "/schedule/profile/availability/reset",
    response_model=WorkspaceEffectiveAvailabilityRead,
)
def reset_workspace_availability(service: ManualPlanServiceDependency):
    return service.reset_windows_to_routine()


@router.post(
    "/manual-plans", response_model=ManualPlanRead, status_code=201, include_in_schema=False
)
@router.post("/quick-plans", response_model=ManualPlanRead, status_code=201)
def create_manual_plan(payload: ManualPlanCreate, service: ManualPlanServiceDependency):
    return service.create_plan(payload)


@router.get("/manual-plans", response_model=list[ManualPlanRead], include_in_schema=False)
@router.get("/quick-plans", response_model=list[ManualPlanRead])
def list_manual_plans(service: ManualPlanServiceDependency):
    return service.list_plans()


@router.get("/manual-plans/{plan_id}", response_model=ManualPlanRead, include_in_schema=False)
@router.get("/quick-plans/{plan_id}", response_model=ManualPlanRead)
def get_manual_plan(plan_id: str, service: ManualPlanServiceDependency):
    return service.get_plan(plan_id)


@router.patch(
    "/manual-plans/{plan_id}/tasks/{task_id}",
    response_model=ManualPlanRead,
    include_in_schema=False,
)
@router.patch("/quick-plans/{plan_id}/tasks/{task_id}", response_model=ManualPlanRead)
def update_manual_task(
    plan_id: str,
    task_id: str,
    payload: ManualTaskUpdate,
    service: ManualPlanServiceDependency,
):
    return service.update_task(plan_id, task_id, payload)


@router.delete(
    "/manual-plans/{plan_id}/tasks/{task_id}",
    response_model=ManualPlanRead,
    include_in_schema=False,
)
@router.delete("/quick-plans/{plan_id}/tasks/{task_id}", response_model=ManualPlanRead)
def delete_manual_task(
    plan_id: str,
    task_id: str,
    payload: ManualTaskDelete | None = None,
    service: ManualPlanServiceDependency = None,
):
    return service.delete_task(
        plan_id,
        task_id,
        payload.scope if payload else "FUTURE",
        payload.override_date if payload else None,
    )


@router.post(
    "/light-arrangements", response_model=LightDraftRead, status_code=201, include_in_schema=False
)
@router.post("/quick-plan-drafts", response_model=LightDraftRead, status_code=201)
def create_light_arrangement(payload: LightDraftCreate, service: LightArrangementServiceDependency):
    return service.create(payload)


@router.patch(
    "/light-arrangements/{draft_id}", response_model=LightDraftRead, include_in_schema=False
)
@router.patch("/quick-plan-drafts/{draft_id}", response_model=LightDraftRead)
def edit_light_arrangement(
    draft_id: str, payload: LightDraftEdit, service: LightArrangementServiceDependency
):
    return service.edit(draft_id, payload)


@router.get("/light-arrangements", response_model=list[LightDraftRead], include_in_schema=False)
@router.get("/quick-plan-drafts", response_model=list[LightDraftRead])
def list_light_arrangement_drafts(service: LightArrangementServiceDependency):
    return service.list_drafts()


@router.get(
    "/light-arrangements/{draft_id}", response_model=LightDraftRead, include_in_schema=False
)
@router.get("/quick-plan-drafts/{draft_id}", response_model=LightDraftRead)
def get_light_arrangement_draft(draft_id: str, service: LightArrangementServiceDependency):
    return service.get(draft_id)


@router.post(
    "/light-arrangements/{draft_id}/generate",
    response_model=LightDraftRead,
    include_in_schema=False,
)
@router.post("/quick-plan-drafts/{draft_id}/generate", response_model=LightDraftRead)
@model_required
def generate_light_arrangement_draft(
    draft_id: str,
    payload: LightRevisionRequest,
    service: LightArrangementServiceDependency,
):
    return service.generate(draft_id, payload)


@router.post(
    "/light-arrangements/{draft_id}/apply", response_model=LightDraftRead, include_in_schema=False
)
@router.post("/quick-plan-drafts/{draft_id}/apply", response_model=LightDraftRead)
def apply_light_arrangement(
    draft_id: str, payload: LightRevisionRequest, service: LightArrangementServiceDependency
):
    return service.apply(draft_id, payload)


@router.post("/manual-plans/{plan_id}/upgrade", include_in_schema=False)
@router.post("/quick-plans/{plan_id}/upgrade")
def upgrade_light_arrangement(plan_id: str, service: LightArrangementServiceDependency):
    goal_id = service.upgrade(plan_id)
    return {"goal_id": goal_id, "status": service.session.get(Goal, goal_id).status}


@router.post(
    "/manual-plans/{plan_id}/tasks",
    response_model=ManualPlanRead,
    status_code=201,
    include_in_schema=False,
)
@router.post("/quick-plans/{plan_id}/tasks", response_model=ManualPlanRead, status_code=201)
def add_light_task(
    plan_id: str,
    payload: LightTaskDraft,
    service: ManualPlanServiceDependency,
):
    return service.add_light_task(plan_id, payload)


@router.delete("/light-arrangements/{draft_id}", status_code=204, include_in_schema=False)
@router.delete("/quick-plan-drafts/{draft_id}", status_code=204)
def discard_light_draft(
    draft_id: str,
    payload: LightRevisionRequest,
    service: LightArrangementServiceDependency,
):
    service.discard(draft_id, payload)


@router.post(
    "/manual-plans/{plan_id}/archive", response_model=ManualPlanRead, include_in_schema=False
)
@router.post("/quick-plans/{plan_id}/archive", response_model=ManualPlanRead)
def archive_light_plan(plan_id: str, service: LightArrangementServiceDependency):
    return service.archive(plan_id)


@router.post(
    "/manual-plans/{plan_id}/restore", response_model=ManualPlanRead, include_in_schema=False
)
@router.post("/quick-plans/{plan_id}/restore", response_model=ManualPlanRead)
def restore_light_plan(plan_id: str, service: LightArrangementServiceDependency):
    return service.archive(plan_id, restore=True)


@router.get("/today", response_model=UnifiedTodayRead)
def get_unified_today(
    service: TodayServiceDependency,
    on_date: date | None = None,
):
    return service.get_today(on_date)


@router.post("/today/schedule", response_model=TodayTaskScheduleResult)
def schedule_today_task(
    payload: TodayTaskScheduleRequest,
    service: TodayServiceDependency,
):
    return service.schedule_task(payload)


@router.post("/today/allocation/preview", response_model=DayAllocationPreview)
@model_required
def preview_day_allocation(payload: DayAllocationRequest, session: SessionDependency):
    return DayScheduleService(session).preview(payload)


@router.post("/today/allocation/preview-many", response_model=list[DayAllocationPreview])
@model_required
def preview_many_day_allocations(payload: DayAllocationBatchRequest, session: SessionDependency):
    return DayScheduleService(session).preview_many(payload.requests)


@router.post("/today/allocation/apply", response_model=UnifiedTodayRead)
def apply_day_allocation(payload: DayAllocationApply, session: SessionDependency):
    return DayScheduleService(session).apply(payload)


@router.get(
    "/goals/{goal_id}/execution/schedule",
    response_model=ScheduleFoundationRead,
)
def get_schedule_foundation(goal_id: str, service: ExecutionServiceDependency):
    return service.get_schedule_foundation(goal_id)


@router.post(
    "/goals/{goal_id}/execution/schedule/allocate-plan",
    response_model=PlanScheduleAllocationRead,
)
@model_required
def allocate_entire_plan(goal_id: str, service: ExecutionServiceDependency):
    return service.allocate_entire_plan(goal_id)


@router.patch(
    "/goals/{goal_id}/execution/schedule/preferences",
    response_model=SchedulePreferenceRead,
)
def update_schedule_preference(
    goal_id: str,
    payload: SchedulePreferenceUpdate,
    service: ExecutionServiceDependency,
):
    return service.update_schedule_preference(goal_id, payload)


@router.patch(
    "/goals/{goal_id}/execution/schedule/reminders",
    response_model=SchedulePreferenceRead,
)
def update_commitment_reminder(
    goal_id: str,
    payload: CommitmentReminderUpdate,
    service: ExecutionServiceDependency,
):
    return service.update_commitment_reminder(goal_id, payload.label, payload.enabled)


@router.get(
    "/goals/{goal_id}/execution/reminders",
    response_model=list[ReminderEventRead],
)
def list_reminder_events(
    goal_id: str,
    service: ExecutionServiceDependency,
    include_closed: bool = False,
):
    return service.list_reminder_events(goal_id, include_closed=include_closed)


@router.patch(
    "/goals/{goal_id}/execution/reminders/{event_id}",
    response_model=ReminderEventRead,
)
def update_reminder_event(
    goal_id: str,
    event_id: str,
    payload: ReminderEventAction,
    service: ExecutionServiceDependency,
):
    return service.update_reminder_event(goal_id, event_id, payload)


@router.put(
    "/goals/{goal_id}/execution/schedule/availability",
    response_model=list[AvailabilityWindowRead],
)
def replace_availability_windows(
    goal_id: str,
    payload: AvailabilityWindowsUpdate,
    service: ExecutionServiceDependency,
):
    return service.replace_availability_windows(goal_id, payload)


@router.post("/goals/{goal_id}/execution/preview", response_model=ExecutionPreview)
def preview_execution(
    goal_id: str,
    payload: ExecutionScheduleRequest,
    service: ExecutionServiceDependency,
):
    return service.preview(goal_id, payload)


@router.post("/goals/{goal_id}/execution/calendar/smart-preview", response_model=SmartCalendarRead)
@model_required
def smart_calendar_preview(goal_id: str, payload: SmartCalendarRequest, session: SessionDependency):
    return ExecutionCalendarService(session).smart_preview(goal_id, payload)


@router.post("/goals/{goal_id}/execution/days/preview", response_model=ExecutionDayMoveRead)
def preview_execution_day_move(
    goal_id: str, payload: ExecutionDayMoveRequest, session: SessionDependency,
):
    return ExecutionCalendarService(session).preview_move(goal_id, payload)


@router.post("/goals/{goal_id}/execution/days/apply", response_model=TodayWorkspace)
def apply_execution_day_move(
    goal_id: str, payload: ExecutionDayMoveApply, session: SessionDependency,
):
    return ExecutionCalendarService(session).apply_move(goal_id, payload.preview_id)


@router.post("/goals/{goal_id}/execution/start", response_model=TodayWorkspace)
def start_execution(
    goal_id: str,
    payload: ExecutionScheduleRequest,
    service: ExecutionServiceDependency,
):
    return service.start(goal_id, payload)


@router.get("/goals/{goal_id}/today", response_model=TodayWorkspace)
def get_today(
    goal_id: str,
    service: ExecutionServiceDependency,
    on_date: date | None = None,
):
    return service.get_today(goal_id, on_date)


@router.post("/goals/{goal_id}/execution/carry-forward", response_model=TodayWorkspace)
def carry_forward_execution_task(
    goal_id: str,
    payload: CarryForwardRequest,
    service: ExecutionServiceDependency,
):
    return service.carry_forward(goal_id, payload)


@router.post("/goals/{goal_id}/execution/flexible-tasks/arrange", response_model=TodayWorkspace)
def arrange_flexible_task(
    goal_id: str,
    payload: FlexibleTaskArrangeRequest,
    service: ExecutionServiceDependency,
):
    return service.arrange_flexible_task(goal_id, payload)


@router.post(
    "/goals/{goal_id}/execution/flexible-tasks/decision-preview",
    response_model=FlexibleTaskDecisionPreviewRead,
)
def preview_flexible_task_decision(
    goal_id: str,
    payload: FlexibleTaskDecisionRequest,
    service: ExecutionServiceDependency,
):
    return service.preview_flexible_decision(goal_id, payload)


@router.post("/goals/{goal_id}/execution/flexible-tasks/decision-apply", response_model=TodayWorkspace)
def apply_flexible_task_decision(
    goal_id: str,
    payload: FlexibleTaskDecisionApply,
    service: ExecutionServiceDependency,
):
    return service.apply_flexible_decision(goal_id, payload)


@router.post("/goals/{goal_id}/execution/flexible-tasks/reschedule", response_model=TodayWorkspace)
def reschedule_flexible_task(
    goal_id: str,
    payload: FlexibleTaskRescheduleRequest,
    service: ExecutionServiceDependency,
):
    return service.reschedule_flexible_task(goal_id, payload)


@router.get("/goals/{goal_id}/execution/calendar", response_model=ExecutionCalendarRead)
def get_execution_calendar(goal_id: str, service: ExecutionServiceDependency):
    return service.get_calendar(goal_id)


@router.post("/goals/{goal_id}/execution/recommendations/preview", response_model=RecommendationPreview)
@model_required
def preview_task_recommendations(goal_id: str, payload: RecommendationRequest, session: SessionDependency):
    return RecommendationService(session).preview(goal_id, payload)


@router.get("/goals/{goal_id}/execution/candidate-pools", response_model=list[CandidatePoolRead])
def get_execution_candidate_pools(goal_id: str, service: ExecutionServiceDependency):
    return service.get_candidate_pools(goal_id)


@router.get("/goals/{goal_id}/execution/hybrid-migration", response_model=HybridMigrationAuditRead)
def get_hybrid_migration_status(goal_id: str, session: SessionDependency):
    from planner_api.models import HybridMigrationAudit, PlanVersion
    from planner_api.work_item_domain import ExecutionModelVersion

    plan = session.scalar(
        select(PlanVersion).where(PlanVersion.goal_id == goal_id).order_by(PlanVersion.version.desc())
    )
    if plan is None:
        raise HTTPException(status_code=404, detail="Plan not found")
    audit = session.scalar(
        select(HybridMigrationAudit).where(HybridMigrationAudit.plan_version_id == plan.id)
    )
    if audit is None:
        import hashlib
        from planner_api.models import utc_now
        audit = HybridMigrationAudit(
            plan_version_id=plan.id,
            source_execution_model=plan.execution_model_version,
            status=(
                "MIGRATED"
                if plan.execution_model_version == ExecutionModelVersion.HYBRID_WORK_ITEM
                else "ELIGIBLE"
            ),
            rollout_bucket=int(hashlib.sha256(goal_id.encode()).hexdigest()[:8], 16) % 100,
            details_json={"legacy_plans_are_isolated": True},
            created_at=utc_now(),
            updated_at=utc_now(),
        )
        session.add(audit)
        session.commit()
    return HybridMigrationAuditRead(
        plan_version_id=audit.plan_version_id,
        source_execution_model=audit.source_execution_model,
        target_execution_model=audit.target_execution_model,
        status=audit.status,
        rollout_bucket=audit.rollout_bucket,
        details=audit.details_json or {},
        created_at=audit.created_at,
        updated_at=audit.updated_at,
    )


@router.post(
    "/goals/{goal_id}/execution/candidate-pools/replacement-preview",
    response_model=CandidateReplacementPreviewRead,
)
def preview_execution_candidate_replacement(
    goal_id: str,
    payload: CandidateReplacementRequest,
    service: ExecutionServiceDependency,
):
    return service.preview_candidate_replacement(goal_id, payload)


@router.post(
    "/goals/{goal_id}/execution/candidate-pools/replacement-apply",
    response_model=TodayWorkspace,
)
def apply_execution_candidate_replacement(
    goal_id: str,
    payload: FlexibleTaskDecisionApply,
    service: ExecutionServiceDependency,
):
    return service.apply_candidate_replacement(goal_id, payload.preview_id)


@router.get("/goals/{goal_id}/execution/triggered-tasks", response_model=list[TriggeredTaskRead])
def get_execution_triggered_tasks(goal_id: str, service: ExecutionServiceDependency):
    return service.get_triggered_tasks(goal_id)


@router.post(
    "/goals/{goal_id}/execution/triggered-tasks/preview",
    response_model=TriggeredTaskActivationPreviewRead,
)
def preview_execution_triggered_task(
    goal_id: str,
    payload: TriggeredTaskActivationRequest,
    service: ExecutionServiceDependency,
):
    return service.preview_triggered_activation(goal_id, payload)


@router.post("/goals/{goal_id}/execution/triggered-tasks/apply", response_model=TodayWorkspace)
def apply_execution_triggered_task(
    goal_id: str,
    payload: TriggeredTaskActivationApply,
    service: ExecutionServiceDependency,
):
    return service.apply_triggered_activation(goal_id, payload)


@router.get("/goals/{goal_id}/execution/progress", response_model=ExecutionProgressRead)
def get_execution_progress(goal_id: str, service: ExecutionServiceDependency):
    return service.get_progress(goal_id)


@router.get(
    "/goals/{goal_id}/execution/discussion",
    response_model=ExecutionDiscussionWorkspace,
)
def get_execution_discussion(
    goal_id: str, service: ExecutionDiscussionServiceDependency
):
    return service.get_workspace(goal_id)


@router.post(
    "/goals/{goal_id}/execution/discussion/messages",
    response_model=ExecutionDiscussionWorkspace,
)
@model_required
def discuss_execution(
    goal_id: str,
    payload: ExecutionDiscussionCreate,
    service: ExecutionDiscussionServiceDependency,
):
    return service.discuss(
        goal_id,
        payload.content,
        web_research_enabled=payload.web_research_enabled,
        research_run_id=payload.research_run_id,
        use_saved_research=payload.use_saved_research,
        regenerate=payload.regenerate,
        material_source_ids=payload.material_source_ids,
    )


@router.post(
    "/goals/{goal_id}/execution/discussion/retry",
    response_model=ExecutionDiscussionWorkspace,
)
@model_required
def retry_execution_discussion(
    goal_id: str, service: ExecutionDiscussionServiceDependency
):
    return service.retry_last(goal_id)


@router.put(
    "/goals/{goal_id}/execution/insights/{insight_id}",
    response_model=ExecutionDiscussionWorkspace,
)
def decide_execution_insight(
    goal_id: str,
    insight_id: str,
    payload: ExecutionInsightDecision,
    service: ExecutionDiscussionServiceDependency,
):
    return service.decide_insight(goal_id, insight_id, payload.action)


@router.post(
    "/goals/{goal_id}/execution/reviews/{review_id}/dismiss",
    response_model=ExecutionDiscussionWorkspace,
)
def dismiss_execution_review(
    goal_id: str,
    review_id: str,
    service: ExecutionDiscussionServiceDependency,
):
    return service.dismiss_review(goal_id, review_id)


@router.get(
    "/goals/{goal_id}/execution/recovery/shift-preview",
    response_model=RecoveryShiftPreviewRead,
)
def preview_recovery_shift(
    goal_id: str,
    service: ExecutionServiceDependency,
    anchor_date: date | None = None,
):
    return service.preview_recovery_shift(goal_id, anchor_date)


@router.post(
    "/goals/{goal_id}/execution/recovery/shift",
    response_model=RecoveryShiftPreviewRead,
)
def apply_recovery_shift(
    goal_id: str,
    payload: RecoveryShiftApply,
    service: ExecutionServiceDependency,
):
    return service.apply_recovery_shift(goal_id, payload.preview_id, payload.accept_unallocated)


@router.post(
    "/goals/{goal_id}/execution/recovery/defer",
    response_model=ExecutionInactivityRead,
)
def defer_recovery(
    goal_id: str,
    payload: RecoveryDeferRequest,
    service: ExecutionServiceDependency,
):
    return service.defer_recovery(goal_id, payload.missed_dates)


@router.get(
    "/goals/{goal_id}/execution/recovery/catchup-preview",
    response_model=RecoveryCatchupPreviewRead,
)
def preview_recovery_catchup(
    goal_id: str,
    service: ExecutionServiceDependency,
    anchor_date: date | None = None,
    include_non_execution_days: bool = False,
):
    return service.preview_recovery_catchup(goal_id, anchor_date, include_non_execution_days)


@router.post(
    "/goals/{goal_id}/execution/recovery/catchup-preview",
    response_model=RecoveryCatchupPreviewRead,
)
def preview_selected_recovery_catchup(
    goal_id: str,
    payload: RecoveryCatchupRequest,
    service: ExecutionServiceDependency,
):
    return service.preview_recovery_catchup(
        goal_id, payload.anchor_date, payload.include_non_execution_days,
        payload.selected_task_ids, payload.source_plan_version_id,
    )


@router.post(
    "/goals/{goal_id}/execution/recovery/catchup",
    response_model=RecoveryCatchupPreviewRead,
)
def apply_recovery_catchup(
    goal_id: str,
    payload: RecoveryCatchupApply,
    service: ExecutionServiceDependency,
):
    return service.apply_recovery_catchup(goal_id, payload.preview_id, payload.accept_unallocated)


@router.get("/goals/{goal_id}/focus-sessions", response_model=list[FocusSessionRead])
def get_focus_sessions(goal_id: str, service: ExecutionServiceDependency):
    return service.get_focus_sessions(goal_id)


@router.post(
    "/goals/{goal_id}/focus-sessions",
    response_model=FocusSessionRead,
    status_code=201,
)
def record_focus_session(
    goal_id: str,
    payload: FocusSessionCreate,
    service: ExecutionServiceDependency,
):
    return service.record_focus_session(goal_id, payload)


@router.get("/goals/{goal_id}/quick-captures", response_model=list[QuickCaptureRead])
def get_quick_captures(goal_id: str, service: ExecutionServiceDependency):
    return service.get_quick_captures(goal_id)


@router.post(
    "/goals/{goal_id}/quick-captures",
    response_model=QuickCaptureRead,
    status_code=201,
)
def create_quick_capture(
    goal_id: str,
    payload: QuickCaptureCreate,
    service: ExecutionServiceDependency,
):
    return service.create_quick_capture(goal_id, payload)


@router.delete(
    "/goals/{goal_id}/quick-captures/{capture_id}",
    response_model=list[QuickCaptureRead],
)
def delete_quick_capture(
    goal_id: str,
    capture_id: str,
    service: ExecutionServiceDependency,
):
    return service.delete_quick_capture(goal_id, capture_id)


@router.post("/goals/{goal_id}/tasks/{task_id}/start", response_model=TodayWorkspace)
def start_task(goal_id: str, task_id: str, service: ExecutionServiceDependency):
    return service.update_task(goal_id, task_id, "STARTED")


@router.post("/goals/{goal_id}/tasks/{task_id}/complete", response_model=TodayWorkspace)
def complete_task(goal_id: str, task_id: str, service: ExecutionServiceDependency):
    return service.update_task(goal_id, task_id, "COMPLETED")


@router.post("/goals/{goal_id}/tasks/{task_id}/progress", response_model=TodayWorkspace)
def record_task_progress(
    goal_id: str,
    task_id: str,
    payload: TaskProgressUpdate,
    service: ExecutionServiceDependency,
):
    return service.record_task_progress(goal_id, task_id, payload)


@router.post(
    "/goals/{goal_id}/execution/recovery/complete-missed",
    response_model=ExecutionProgressRead,
)
def complete_missed_tasks(
    goal_id: str,
    payload: RetrospectiveCompletionRequest,
    service: ExecutionServiceDependency,
):
    return service.complete_missed_tasks(goal_id, payload.task_ids)


@router.post("/goals/{goal_id}/today/complete", response_model=TodayWorkspace)
def complete_today(goal_id: str, service: ExecutionServiceDependency):
    return service.complete_today(goal_id)


@router.post("/goals/{goal_id}/tasks/{task_id}/feedback", response_model=TodayWorkspace)
def add_task_feedback(
    goal_id: str,
    task_id: str,
    payload: TaskFeedbackCreate,
    service: ExecutionServiceDependency,
):
    return service.record_task_feedback(goal_id, task_id, payload)


@router.post("/goals/{goal_id}/today/feedback", response_model=TodayWorkspace)
def add_day_pace_feedback(
    goal_id: str,
    payload: DayPaceFeedbackCreate,
    service: ExecutionServiceDependency,
):
    return service.record_day_pace_feedback(goal_id, payload)


@router.delete("/goals/{goal_id}/feedback/{feedback_id}", response_model=TodayWorkspace)
def delete_feedback(
    goal_id: str,
    feedback_id: str,
    service: ExecutionServiceDependency,
):
    return service.delete_feedback(goal_id, feedback_id)


@router.post(
    "/goals/{goal_id}/feedback-adjustments/{adjustment_id}/apply",
    response_model=TodayWorkspace,
)
def apply_feedback_adjustment(
    goal_id: str,
    adjustment_id: str,
    service: ExecutionServiceDependency,
):
    return service.apply_adjustment(goal_id, adjustment_id)


@router.post(
    "/goals/{goal_id}/feedback-adjustments/{adjustment_id}/discard",
    response_model=TodayWorkspace,
)
def discard_feedback_adjustment(
    goal_id: str,
    adjustment_id: str,
    service: ExecutionServiceDependency,
):
    return service.discard_adjustment(goal_id, adjustment_id)


@router.post(
    "/goals/{goal_id}/plan/versions/{plan_id}/restore",
    response_model=PlanWorkspace,
)
def restore_plan_version(
    goal_id: str,
    plan_id: str,
    service: PlanServiceDependency,
):
    return service.restore_plan_version(goal_id, plan_id)
