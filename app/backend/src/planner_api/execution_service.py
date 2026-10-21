from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from datetime import date, datetime, timedelta
from functools import wraps
from threading import RLock
from zoneinfo import ZoneInfo

from fastapi import HTTPException
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from planner_api.day_allocation import DayAllocator
from planner_api.execution_projection import plan_task_read
from planner_api.generation_orchestration import GenerationOrchestrator
from planner_api.holiday_calendar import HolidayCalendarService
from planner_api.plan_capacity import calendar_capacity, week_capacity
from planner_api.temporal_contract import snapshot_deadline
from planner_api.plan_calendar import plan_week_range, plan_week_index
from planner_api.plan_rule_integrity import validate_action_coverage
from planner_api.work_item_validation import WorkItemValidationError
from planner_api.models import (
    AvailabilityException,
    AvailabilityWindow,
    ExecutionDayLog,
    Feedback,
    FeedbackAdjustment,
    FocusSession,
    Goal,
    PlanActivation,
    PlanAdjustmentPreview,
    PlanDay,
    PlanDaySchedule,
    PlanMilestone,
    PlanSchedulePreference,
    PlanTask,
    PlanVersion,
    PlanWindow,
    PlanWorkItem,
    PlanWorkItemGroup,
    ProgressEvent,
    QuickCapture,
    RecoveryOperation,
    ReminderEvent,
    RequirementVersion,
    ScheduleChange,
    TaskTimeBlock,
    WorkItemDependency,
    WorkItemWindow,
    utc_now,
)
from planner_api.quick_plan_handoff import handoff_quick_plans
from planner_api.recovery_time_preview import (
    catchup_affected_dates,
    preview_fingerprint,
    preview_time,
    snapshot,
)
from planner_api.schedule_reconciliation import allocate_plan_schedule, mark_plan_schedule_review
from planner_api.schemas import (
    AvailabilityExceptionRead,
    AvailabilityWindowRead,
    AvailabilityWindowsUpdate,
    CalendarChangeRead,
    CalendarTaskRead,
    CandidatePoolItemRead,
    CandidatePoolPeriodRead,
    CandidatePoolRead,
    CandidateReplacementPreviewRead,
    CandidateReplacementRequest,
    CarryForwardCandidateRead,
    CarryForwardRequest,
    CommitmentAlertRead,
    CommitmentProgressRead,
    DayPaceFeedbackCreate,
    DayAllocationRequest,
    ExecutionCalendarDayRead,
    ExecutionCalendarRead,
    ExecutionDayLogRead,
    ExecutionFeedbackTimelineRead,
    ExecutionInactivityRead,
    ExecutionPlanRevisionRead,
    ExecutionPreview,
    ExecutionProgressDayRead,
    ExecutionProgressRead,
    ExecutionReviewSignalRead,
    ExecutionScheduleRequest,
    ExecutionSetup,
    ExecutionTrendRead,
    ExplorationExecutionStateRead,
    FeedbackAdjustmentRead,
    FeedbackRead,
    FlexibleTaskArrangeRequest,
    FlexibleTaskDecisionApply,
    FlexibleTaskDecisionPreviewRead,
    FlexibleTaskDecisionRequest,
    FlexibleTaskRescheduleRequest,
    FocusSessionCreate,
    FocusSessionRead,
    GoalCompleteRequest,
    GoalDeleteRequest,
    GoalLifecycleRead,
    GoalListItem,
    GoalPauseRequest,
    GoalSettingsUpdate,
    PlanDayRead,
    PlanScheduleAllocationRead,
    ProgressSummary,
    QuickCaptureCreate,
    QuickCaptureRead,
    RecoveryCatchupDayLoadRead,
    RecoveryCatchupPreviewRead,
    RecoveryDayChangeRead,
    RecoveryShiftPreviewRead,
    ReminderEventAction,
    ReminderEventRead,
    RhythmProgressRead,
    ScheduledDayRead,
    ScheduleFoundationRead,
    SchedulePreferenceRead,
    SchedulePreferenceUpdate,
    TaskFeedbackCreate,
    TaskProgressUpdate,
    TodayCapacityRead,
    TodayWorkspace,
    TriggeredTaskActivationApply,
    TriggeredTaskActivationPreviewRead,
    TriggeredTaskActivationRequest,
    TriggeredTaskRead,
    WorkItemDailyPreviewRead,
)
from planner_api.work_item_domain import ExecutionModelVersion, remap_materialization_key
from planner_api.work_item_materialization import WorkItemMaterializer

_recovery_transaction_lock = RLock()


def serialize_recovery_transaction[**P, R](
    function: Callable[P, R],
) -> Callable[P, R]:
    @wraps(function)
    def wrapped(*args: P.args, **kwargs: P.kwargs) -> R:
        with _recovery_transaction_lock:
            return function(*args, **kwargs)

    return wrapped


class ExecutionService:
    def __init__(
        self,
        session: Session,
        orchestrator: GenerationOrchestrator | None = None,
    ) -> None:
        self.session = session
        self.orchestrator = orchestrator or GenerationOrchestrator(session)

    @staticmethod
    def _today_for(goal: Goal) -> date:
        return datetime.now(ZoneInfo(goal.timezone)).date()

    @staticmethod
    def _local_date(value: datetime, goal: Goal) -> date:
        """Convert persisted UTC timestamps before applying local-day rules."""
        if value.tzinfo is None:
            value = value.replace(tzinfo=ZoneInfo("UTC"))
        return value.astimezone(ZoneInfo(goal.timezone)).date()

    def get_lifecycle(self, goal_id: str) -> GoalLifecycleRead:
        goal = self.session.get(Goal, goal_id)
        if goal is None:
            raise HTTPException(status_code=404, detail="Goal not found")
        return self._lifecycle_read(goal)

    def check_goal_statuses(self) -> list[GoalListItem]:
        goals = list(self.session.scalars(select(Goal).order_by(Goal.last_active_at.desc())))
        changed = False
        for goal in goals:
            if goal.status == "PAUSED" and goal.pause_until:
                if goal.pause_until <= self._today_for(goal):
                    plan = (
                        self.session.get(PlanVersion, goal.active_plan_version_id)
                        if goal.active_plan_version_id
                        else None
                    )
                    if plan is not None:
                        self._resume_paused_goal(goal, plan)
                    else:
                        goal.status = "ACTIVE"
                        goal.pause_until = None
                    changed = True
            elif goal.status == "ACTIVE" and goal.active_plan_version_id:
                plan = self.session.get(PlanVersion, goal.active_plan_version_id)
                if plan is not None:
                    inactivity = self._inactivity_for_plan(goal, plan)
                    if inactivity.level == "PAUSED_7_DAYS":
                        goal.status = "RECOVERY_REQUIRED"
                        changed = True
        if changed:
            self.session.commit()
        return [
            GoalListItem(
                id=goal.id,
                title=goal.title,
                status=goal.status,
                last_active_at=goal.last_active_at,
                timezone=goal.timezone,
                pause_until=goal.pause_until,
                completed_at=goal.completed_at,
                archived_at=goal.archived_at,
            )
            for goal in goals
        ]

    def pause_goal(self, goal_id: str, payload: GoalPauseRequest) -> GoalLifecycleRead:
        goal, _plan = self._goal_and_plan(goal_id)
        if goal.status not in {"ACTIVE", "RECOVERY_REQUIRED"}:
            raise HTTPException(status_code=409, detail="Only an active goal can be paused")
        if payload.pause_until and payload.pause_until <= self._today_for(goal):
            raise HTTPException(status_code=422, detail="Pause date must be in the future")
        goal.status = "PAUSED"
        goal.pause_until = payload.pause_until
        self.session.commit()
        return self._lifecycle_read(goal)

    def resume_goal(self, goal_id: str) -> GoalLifecycleRead:
        goal, plan = self._goal_and_plan(goal_id)
        if goal.status != "PAUSED":
            raise HTTPException(status_code=409, detail="Goal is not paused")
        self._resume_paused_goal(goal, plan)
        self.session.commit()
        return self._lifecycle_read(goal)

    def _resume_paused_goal(self, goal: Goal, plan: PlanVersion) -> None:
        anchor = self._today_for(goal)
        goal.pause_until = None
        inactivity = self._inactivity_for_plan(goal, plan)
        if not inactivity.missed_execution_days:
            goal.status = "ACTIVE"
            goal.last_active_at = utc_now()
            return

        activation = self._activation(plan.id)
        if activation is None:
            raise HTTPException(status_code=409, detail="Plan calendar is missing")
        preview = self._build_recovery_shift_preview(plan, activation, anchor)
        fingerprint = preview_fingerprint(preview)
        operation = RecoveryOperation(
            goal_id=goal.id,
            source_plan_version_id=plan.id,
            mode="SHIFT_FORWARD",
            status="PREVIEW",
            idempotency_key=f"pause-resume:{goal.id}:{plan.id}:{anchor.isoformat()}:{fingerprint}",
            preview_json=preview,
        )
        self.session.add(operation)
        self.session.flush()
        revised = self._clone_plan_for_recovery(goal, plan, preview)
        mark_plan_schedule_review(
            self.session, goal, plan.id, revised.id, "暂停恢复后重新安排执行时间。",
        )
        plan.status = "SUPERSEDED"
        goal.active_plan_version_id = revised.id
        goal.status = "ACTIVE"
        goal.last_active_at = utc_now()
        operation.result_plan_version_id = revised.id
        allocate_plan_schedule(self.session, goal, revised.id)
        operation.status = "APPLIED"
        operation.applied_at = utc_now()
        operation.preview_json = {**preview, "result_plan_version_id": revised.id}

    def complete_goal(self, goal_id: str, payload: GoalCompleteRequest) -> GoalLifecycleRead:
        goal, plan = self._goal_and_plan(goal_id)
        if goal.status not in {"ACTIVE", "RECOVERY_REQUIRED"}:
            raise HTTPException(status_code=409, detail="Goal is not active")
        if self._has_unplanned_exploration(plan):
            raise HTTPException(status_code=409, detail="当前体验阶段已完成后，可继续展开剩余路线；整条路线尚未完成")
        tasks = self._tasks(plan.id)
        remaining = [
            task
            for task in tasks
            if task.status not in {"COMPLETED", "CANCELLED", "CARRIED_OVER"}
        ]
        if remaining or self._has_pending_required_triggers(plan):
            raise HTTPException(status_code=409, detail="Plan tasks are not complete")
        completed = sum(task.status == "COMPLETED" for task in tasks)
        goal.status = "COMPLETED"
        goal.pause_until = None
        goal.completed_at = utc_now()
        goal.completion_summary = payload.note or (
            f"计划中的全部内容已经完成，共完成 {completed} 项任务。"
        )
        goal.last_active_at = utc_now()
        self.session.commit()
        return self._lifecycle_read(goal)

    def archive_goal(self, goal_id: str) -> GoalLifecycleRead:
        goal = self.session.get(Goal, goal_id)
        if goal is None:
            raise HTTPException(status_code=404, detail="Goal not found")
        if goal.status == "ARCHIVED":
            return self._lifecycle_read(goal)
        if goal.status != "COMPLETED":
            goal.completion_summary = "这个目标已提前结束，已有规划与执行记录均已保留。"
        goal.status = "ARCHIVED"
        goal.pause_until = None
        goal.archived_at = utc_now()
        goal.last_active_at = utc_now()
        self.session.commit()
        return self._lifecycle_read(goal)

    def rename_goal(self, goal_id: str, title: str) -> GoalLifecycleRead:
        goal = self.session.get(Goal, goal_id)
        if goal is None:
            raise HTTPException(status_code=404, detail="Goal not found")
        goal.title = title
        goal.last_active_at = utc_now()
        self.session.commit()
        return self._lifecycle_read(goal)

    def update_goal_settings(self, goal_id: str, payload: GoalSettingsUpdate) -> GoalLifecycleRead:
        goal = self.session.get(Goal, goal_id)
        if goal is None:
            raise HTTPException(status_code=404, detail="Goal not found")
        if goal.status == "ARCHIVED":
            raise HTTPException(status_code=409, detail="Archived goal cannot be changed")
        goal.timezone = payload.timezone
        self.session.commit()
        return self._lifecycle_read(goal)

    def delete_goal(self, goal_id: str, payload: GoalDeleteRequest) -> None:
        goal = self.session.get(Goal, goal_id)
        if goal is None:
            raise HTTPException(status_code=404, detail="Goal not found")
        if payload.confirmation != goal.id:
            raise HTTPException(status_code=422, detail="Goal deletion confirmation does not match")
        self.session.delete(goal)
        self.session.commit()

    def _lifecycle_read(self, goal: Goal) -> GoalLifecycleRead:
        remaining = 0
        if goal.active_plan_version_id:
            remaining = sum(
                task.is_required and task.status not in {"COMPLETED", "CANCELLED", "CARRIED_OVER"}
                for task in self._tasks(goal.active_plan_version_id)
            )
        return GoalLifecycleRead(
            id=goal.id,
            title=goal.title,
            status=goal.status,
            timezone=goal.timezone,
            pause_until=goal.pause_until,
            completed_at=goal.completed_at,
            archived_at=goal.archived_at,
            completion_summary=goal.completion_summary,
            remaining_required_tasks=remaining,
        )

    def get_setup(self, goal_id: str) -> ExecutionSetup:
        goal, plan = self._goal_and_plan(goal_id)
        activation = self._activation(plan.id)
        foundation = self.get_schedule_foundation(goal.id)
        suggested_weekdays = self._suggested_weekdays(plan.weekly_execution_days)
        has_daily_plan = self._has_daily_plan(plan)
        suggested_start = self._today_for(goal)
        phased = any(item.get("available_days_per_week") for item in plan.temporal_segments_json or [])
        if phased:
            suggested_weekdays = self._suggested_weekdays(max(
                week_capacity(plan, week)["days"] for week in range(1, plan.total_weeks + 1)))
        if (
            plan.execution_model_version == ExecutionModelVersion.HYBRID_WORK_ITEM
        ):
            seed_payload = ExecutionScheduleRequest(
                requested_start_date=self._today_for(goal),
                execution_weekdays=suggested_weekdays,
                excluded_dates=[],
            )
            try:
                suggested_weekdays = self._work_item_materializer(
                    plan, seed_payload
                ).recommended_execution_weekdays(plan, self._today_for(goal))
            except HTTPException:
                pass  # Search other start anchors before declaring a conflict.
            if has_daily_plan and activation is None:
                # A relative D1 and an explicit Sunday slot may require Sunday
                # as the start anchor. Suggest a feasible anchor without
                # changing an explicit user request or creating a preview.
                for offset in range(7):
                    candidate_start = self._today_for(goal) + timedelta(days=offset)
                    try:
                        candidate_weekdays = self._work_item_materializer(plan, seed_payload).recommended_execution_weekdays(plan, candidate_start)
                        candidate = ExecutionScheduleRequest(requested_start_date=candidate_start, execution_weekdays=candidate_weekdays)
                        mapped = self._mapped_days(plan, candidate)
                        if mapped:
                            self._validate_activation_deadline(plan, candidate, mapped[-1].scheduled_date)
                            suggested_start, suggested_weekdays = candidate_start, candidate_weekdays
                            break
                    except HTTPException:
                        continue
        return ExecutionSetup(
            goal_id=goal.id,
            goal_title=goal.title,
            status=goal.status,
            plan_id=plan.id,
            plan_revision=plan.revision,
            total_weeks=plan.total_weeks,
            weekly_execution_days=plan.weekly_execution_days,
            schedule_type=plan.schedule_type,
            execution_model_version=plan.execution_model_version,
            has_daily_plan=has_daily_plan,
            minimum_execution_days=len(suggested_weekdays),
            has_phased_capacity=phased,
            suggested_weekdays=suggested_weekdays,
            activation=self._activation_preview(activation, plan) if activation else None,
            timezone=goal.timezone,
            schedule_preference=foundation.preference,
            current_date=self._today_for(goal),
            suggested_start_date=suggested_start,
            availability_windows=foundation.availability_windows,
        )

    def preview_plan_days(self, goal_id: str) -> WorkItemDailyPreviewRead:
        goal = self.session.get(Goal, goal_id)
        if goal is None:
            raise HTTPException(status_code=404, detail="Goal not found")
        plan = (
            self.session.get(PlanVersion, goal.active_plan_version_id)
            if goal.active_plan_version_id
            else self.session.scalar(
                select(PlanVersion)
                .where(
                    PlanVersion.goal_id == goal.id,
                    PlanVersion.status == "DRAFT",
                )
                .order_by(PlanVersion.revision.desc(), PlanVersion.created_at.desc())
            )
        )
        if plan is None:
            raise HTTPException(status_code=409, detail="Plan draft is required")
        if plan.execution_model_version != ExecutionModelVersion.HYBRID_WORK_ITEM:
            raise HTTPException(status_code=409, detail="Daily projection is only for Hybrid plans")
        if goal.status not in {"PLAN_REVIEW", "READY_TO_START", "ACTIVE"}:
            raise HTTPException(status_code=409, detail="Plan is not ready for daily preview")
        candidate_start = self._today_for(goal)
        seed_payload = ExecutionScheduleRequest(
            requested_start_date=candidate_start,
            execution_weekdays=self._suggested_weekdays(plan.weekly_execution_days),
            excluded_dates=[],
        )
        suggested_weekdays = self._work_item_materializer(
            plan, seed_payload
        ).recommended_execution_weekdays(plan, candidate_start)
        payload = ExecutionScheduleRequest(
            requested_start_date=candidate_start,
            execution_weekdays=suggested_weekdays,
            excluded_dates=[],
        )
        return self._work_item_materializer(plan, payload).daily_preview(plan, payload)

    def _schedule_preference(self, goal: Goal) -> PlanSchedulePreference:
        preference = self.session.scalar(
            select(PlanSchedulePreference).where(PlanSchedulePreference.goal_id == goal.id)
        )
        if preference is None:
            preference = PlanSchedulePreference(goal_id=goal.id, timezone=goal.timezone)
            self.session.add(preference)
            self.session.flush()
        return preference

    @staticmethod
    def _schedule_preference_read(item: PlanSchedulePreference) -> SchedulePreferenceRead:
        return SchedulePreferenceRead(
            id=item.id,
            goal_id=item.goal_id,
            mode=item.mode,
            timezone=item.timezone,
            break_minutes=item.break_minutes,
            allow_cross_window=item.allow_cross_window,
            reminders_enabled=item.reminders_enabled,
            supervision_mode=item.supervision_mode,
            cross_plan_priority_mode=getattr(item, "cross_plan_priority_mode", "BALANCED"),
            quiet_start_local=item.quiet_start_local,
            quiet_end_local=item.quiet_end_local,
            muted_commitments=list(item.muted_commitments_json or []),
            created_at=item.created_at,
            updated_at=item.updated_at,
        )

    @staticmethod
    def _availability_window_read(item: AvailabilityWindow) -> AvailabilityWindowRead:
        return AvailabilityWindowRead(
            id=item.id,
            goal_id=item.goal_id,
            weekday=item.weekday,
            start_local=item.start_local,
            end_local=item.end_local,
            valid_from=item.valid_from,
            valid_to=item.valid_to,
            timezone=item.timezone,
            source=item.source,
            created_at=item.created_at,
            updated_at=item.updated_at,
        )

    @staticmethod
    def _availability_exception_read(item: AvailabilityException) -> AvailabilityExceptionRead:
        return AvailabilityExceptionRead(
            id=item.id,
            goal_id=item.goal_id,
            exception_date=item.exception_date,
            kind=item.kind,
            start_local=item.start_local,
            end_local=item.end_local,
            reason=item.reason,
            source=item.source,
            created_at=item.created_at,
        )

    def get_schedule_foundation(self, goal_id: str) -> ScheduleFoundationRead:
        goal = self.session.get(Goal, goal_id)
        if goal is None:
            raise HTTPException(status_code=404, detail="Goal not found")
        preference = self._schedule_preference(goal)
        windows = list(
            self.session.scalars(
                select(AvailabilityWindow)
                .where(AvailabilityWindow.goal_id == goal.id)
                .order_by(AvailabilityWindow.weekday, AvailabilityWindow.start_local)
            )
        )
        exceptions = list(
            self.session.scalars(
                select(AvailabilityException)
                .where(AvailabilityException.goal_id == goal.id)
                .order_by(AvailabilityException.exception_date)
            )
        )
        self.session.commit()
        return ScheduleFoundationRead(
            preference=self._schedule_preference_read(preference),
            availability_windows=[self._availability_window_read(item) for item in windows],
            availability_exceptions=[
                self._availability_exception_read(item) for item in exceptions
            ],
        )

    def update_schedule_preference(
        self, goal_id: str, payload: SchedulePreferenceUpdate
    ) -> SchedulePreferenceRead:
        goal = self.session.get(Goal, goal_id)
        if goal is None:
            raise HTTPException(status_code=404, detail="Goal not found")
        preference = self._schedule_preference(goal)
        before = {
            "mode": preference.mode,
            "timezone": preference.timezone,
            "break_minutes": preference.break_minutes,
            "allow_cross_window": preference.allow_cross_window,
            "reminders_enabled": preference.reminders_enabled,
            "supervision_mode": preference.supervision_mode,
            "cross_plan_priority_mode": getattr(preference, "cross_plan_priority_mode", "BALANCED"),
            "quiet_start_local": preference.quiet_start_local,
            "quiet_end_local": preference.quiet_end_local,
        }
        updates = payload.model_dump(exclude_unset=True)
        for key, value in updates.items():
            if value is not None:
                setattr(preference, key, value)
        if "mode" in updates:
            tasks = self.session.scalars(
                select(PlanTask)
                .join(PlanVersion, PlanTask.plan_version_id == PlanVersion.id)
                .where(PlanVersion.goal_id == goal.id)
            )
            for task in tasks:
                task.ai_schedule_requested = updates["mode"] in {"BLOCKS", "FIXED"}
        if "timezone" in updates and updates["timezone"]:
            # Keep the execution clock and the goal's day-boundary clock aligned.
            goal.timezone = updates["timezone"]
        self.session.add(
            ScheduleChange(
                goal_id=goal.id,
                entity_type="PREFERENCE",
                entity_id=preference.id,
                change_kind="UPDATED",
                before_json=before,
                after_json={
                    "mode": preference.mode,
                    "timezone": preference.timezone,
                    "break_minutes": preference.break_minutes,
                    "allow_cross_window": preference.allow_cross_window,
                    "reminders_enabled": preference.reminders_enabled,
                    "supervision_mode": preference.supervision_mode,
                    "cross_plan_priority_mode": getattr(preference, "cross_plan_priority_mode", "BALANCED"),
                    "quiet_start_local": preference.quiet_start_local,
                    "quiet_end_local": preference.quiet_end_local,
                },
            )
        )
        self.session.commit()
        return self._schedule_preference_read(preference)

    def update_commitment_reminder(
        self, goal_id: str, label: str, enabled: bool
    ) -> SchedulePreferenceRead:
        goal = self.session.get(Goal, goal_id)
        if goal is None:
            raise HTTPException(status_code=404, detail="Goal not found")
        normalized = label.strip()
        if not normalized:
            raise HTTPException(status_code=422, detail="Commitment label cannot be blank")
        preference = self._schedule_preference(goal)
        muted = set(preference.muted_commitments_json or [])
        was_enabled = normalized not in muted
        if enabled:
            muted.discard(normalized)
        else:
            muted.add(normalized)
        preference.muted_commitments_json = sorted(muted)
        self.session.add(
            ScheduleChange(
                goal_id=goal.id,
                entity_type="REMINDER",
                entity_id=preference.id,
                change_kind="UPDATED",
                before_json={"label": normalized, "enabled": was_enabled},
                after_json={"label": normalized, "enabled": enabled},
                reason="用户更新承诺提醒设置",
            )
        )
        self.session.commit()
        return self._schedule_preference_read(preference)

    def allocate_entire_plan(self, goal_id: str) -> PlanScheduleAllocationRead:
        goal, plan = self._goal_and_plan(goal_id)
        if goal.status not in {"ACTIVE", "RECOVERY_REQUIRED", "PAUSED"}:
            raise HTTPException(status_code=409, detail="Plan has not started")
        preference = self._schedule_preference(goal)
        tasks = list(
            self.session.scalars(
                select(PlanTask).where(
                    PlanTask.plan_version_id == plan.id,
                    PlanTask.status == "NOT_STARTED",
                )
            )
        )
        orchestration = self.orchestrator.prepare(
            goal.id,
            "TIME_ALLOCATION",
            {
                "goal": {"id": goal.id, "title": goal.title, "timezone": goal.timezone},
                "plan": {
                    "id": plan.id,
                    "schedule_type": plan.schedule_type,
                    "summary": plan.summary,
                },
                "schedule": {
                    "preference": {
                        "mode": preference.mode,
                        "break_minutes": preference.break_minutes,
                        "allow_cross_window": preference.allow_cross_window,
                    },
                    "availability": [
                        {
                            "weekday": item.weekday,
                            "start_local": item.start_local,
                            "end_local": item.end_local,
                        }
                        for item in self.session.scalars(
                            select(AvailabilityWindow).where(AvailabilityWindow.goal_id == goal.id)
                        )
                    ],
                    "exceptions": [
                        {
                            "date": item.exception_date.isoformat(),
                            "kind": item.kind,
                            "start_local": item.start_local,
                            "end_local": item.end_local,
                        }
                        for item in self.session.scalars(
                            select(AvailabilityException).where(
                                AvailabilityException.goal_id == goal.id
                            )
                        )
                    ],
                },
                "tasks": [
                    {
                        "id": item.id,
                        "title": item.title,
                        "minutes": item.estimate_minutes,
                        "priority": item.priority,
                        "policy": item.scheduling_policy,
                        "policy_data": item.scheduling_policy_json,
                    }
                    for item in tasks
                ],
            },
            plan_version_id=plan.id,
            minimum_tier="L2",
        )
        before_mode = preference.mode
        preference.mode = "BLOCKS"
        for task in tasks:
            task.ai_schedule_requested = True
        try:
            records = allocate_plan_schedule(
                self.session,
                goal,
                plan.id,
                orchestration_context=orchestration,
            )
        except Exception as error:
            self.orchestrator.fail(orchestration["run_id"], error)
            raise
        self.orchestrator.complete(
            orchestration["run_id"],
            {
                "status": "PASSED",
                "scheduled_days": len(records),
                "unallocated_tasks": sum(
                    len(item.get("unallocated_titles", [])) for item in records
                ),
            },
        )
        self.session.add(ScheduleChange(
            goal_id=goal.id,
            entity_type="PREFERENCE",
            entity_id=preference.id,
            change_kind="PLAN_AUTO_SCHEDULED",
            before_json={"mode": before_mode},
            after_json={"mode": "BLOCKS", "scheduled_days": len(records)},
            reason="用户在执行页选择安排整个规划",
        ))
        self.session.commit()
        return PlanScheduleAllocationRead(
            scheduled_days=len(records),
            assigned_tasks=sum(len(item.get("assignments", [])) for item in records),
            unallocated_tasks=sum(len(item.get("unallocated_titles", [])) for item in records),
            schedule=records,
        )

    def replace_availability_windows(
        self, goal_id: str, payload: AvailabilityWindowsUpdate
    ) -> list[AvailabilityWindowRead]:
        goal = self.session.get(Goal, goal_id)
        if goal is None:
            raise HTTPException(status_code=404, detail="Goal not found")
        preference = self._schedule_preference(goal)
        timezone = payload.timezone or preference.timezone or goal.timezone
        existing = list(
            self.session.scalars(
                select(AvailabilityWindow).where(AvailabilityWindow.goal_id == goal.id)
            )
        )
        before = [
            {"weekday": item.weekday, "start_local": item.start_local, "end_local": item.end_local}
            for item in existing
        ]
        for item in existing:
            self.session.delete(item)
        created = [
            AvailabilityWindow(
                goal_id=goal.id,
                weekday=item.weekday,
                start_local=item.start_local,
                end_local=item.end_local,
                timezone=timezone,
                valid_from=item.valid_from,
                valid_to=item.valid_to,
            )
            for item in payload.windows
        ]
        self.session.add_all(created)
        if payload.timezone:
            preference.timezone = payload.timezone
        self.session.add(
            ScheduleChange(
                goal_id=goal.id,
                entity_type="AVAILABILITY",
                change_kind="REPLACED",
                before_json={"windows": before},
                after_json={
                    "timezone": timezone,
                    "windows": [
                        {
                            "weekday": item.weekday,
                            "start_local": item.start_local,
                            "end_local": item.end_local,
                        }
                        for item in payload.windows
                    ],
                },
            )
        )
        self.session.commit()
        return [self._availability_window_read(item) for item in created]

    def get_calendar(self, goal_id: str) -> ExecutionCalendarRead:
        goal, plan = self._goal_and_plan(goal_id)
        if goal.status not in {
            "ACTIVE",
            "RECOVERY_REQUIRED",
            "PAUSED",
            "COMPLETED",
            "ARCHIVED",
        }:
            raise HTTPException(status_code=409, detail="Plan has not started")
        activation = self._activation(plan.id)
        if activation is None:
            raise HTTPException(status_code=409, detail="Plan calendar is missing")

        schedules = list(
            self.session.scalars(
                select(PlanDaySchedule)
                .where(PlanDaySchedule.activation_id == activation.id)
                .order_by(PlanDaySchedule.scheduled_date)
            )
        )
        if not schedules:
            raise HTTPException(status_code=409, detail="Plan calendar is empty")

        plan_days = {
            item.id: item
            for item in self.session.scalars(
                select(PlanDay).where(PlanDay.plan_version_id == plan.id)
            )
        }
        tasks_by_day: dict[str, list[PlanTask]] = {}
        for task in self._tasks(plan.id):
            tasks_by_day.setdefault(task.plan_day_id, []).append(task)
        schedule_by_day = {schedule.plan_day_id: schedule.scheduled_date for schedule in schedules}

        # Repeated flexible occurrences are a single recommendation series.
        # The calendar keeps every occurrence for auditability, while Today
        # only needs the first actionable occurrence in each series.
        window_by_id = {
            window.id: window
            for window in self.session.scalars(
                select(WorkItemWindow).where(WorkItemWindow.activation_id == activation.id)
            )
        }
        recommendation_groups: dict[str, list[PlanTask]] = {}
        recommendation_keys: dict[str, str] = {}
        ordinal_pattern = re.compile(r"第\s*[一二三四五六七八九十0-9]+\s*次")

        def recommendation_key(task: PlanTask) -> str | None:
            policy = task.scheduling_policy or "EXACT_DAY"
            if policy not in {"FLOATING", "CADENCE", "DATE_WINDOW", "QUOTA"}:
                return None
            if task.source_work_item_id:
                window = window_by_id.get(task.work_item_window_id or "")
                return f"work-item:{task.source_work_item_id}:{window.period_key if window else 'all'}"
            data = task.scheduling_policy_json or {}
            title = ordinal_pattern.sub("第N次", task.title).strip()
            # A cadence/quota series restarts with each planned week. This
            # keeps week two from being recommended before week one's quota.
            day = plan_days_by_id.get(task.plan_day_id)
            period = data.get("week_index") or (day.week_index if day else "all")
            if policy in {"CADENCE", "QUOTA"}:
                return f"legacy:{policy}:{title}:period:{period}"
            return f"legacy:{policy}:{title}"

        plan_days_by_id = plan_days
        for task in self._tasks(plan.id):
            key = recommendation_key(task)
            if key is None:
                continue
            recommendation_keys[task.id] = key
            recommendation_groups.setdefault(key, []).append(task)
        recommendation_meta: dict[str, tuple[int, int, bool]] = {}
        for _key, group in recommendation_groups.items():
            ordered = sorted(
                group,
                key=lambda item: (
                    schedule_by_day.get(item.plan_day_id, activation.projected_end_date),
                    item.order_in_day,
                    item.id,
                ),
            )
            active = next(
                (item for item in ordered if item.status not in {"COMPLETED", "CANCELLED", "CARRIED_OVER"}),
                None,
            )
            for position, item in enumerate(ordered, start=1):
                recommendation_meta[item.id] = (position, len(ordered), item.id == (active.id if active else ""))

        changes_by_day: dict[str, list[CalendarChangeRead]] = {}
        applied_adjustments = list(
            self.session.scalars(
                select(FeedbackAdjustment).where(
                    FeedbackAdjustment.goal_id == goal.id,
                    FeedbackAdjustment.plan_version_id == plan.id,
                    FeedbackAdjustment.status == "APPLIED",
                )
            )
        )
        for adjustment in applied_adjustments:
            operation = adjustment.operation_json
            kind = operation.get("kind")
            feedback = self.session.get(Feedback, adjustment.feedback_id)
            source_id = operation.get("from_day_id") or (feedback.plan_day_id if feedback else None)
            target_id = operation.get("to_day_id")

            def add_change(
                record: FeedbackAdjustment,
                day_id: str | None,
                direction: str,
                other_id: str | None,
            ) -> None:
                if day_id is None or day_id not in schedule_by_day:
                    return
                changes_by_day.setdefault(day_id, []).append(
                    CalendarChangeRead(
                        adjustment_kind=record.adjustment_kind,
                        direction=direction,
                        summary=record.summary,
                        rationale=record.rationale,
                        counterpart_date=schedule_by_day.get(other_id) if other_id else None,
                    )
                )

            if kind == "MOVE_TASK":
                add_change(adjustment, source_id, "MOVED_FROM", target_id)
                add_change(adjustment, target_id, "MOVED_TO", source_id)
            elif kind == "SPLIT_TASK":
                add_change(adjustment, source_id, "SPLIT_FROM", target_id)
                add_change(adjustment, target_id, "SPLIT_TO", source_id)
            elif kind == "ADD_OPTIONAL_TASK":
                add_change(adjustment, target_id, "ADDED", None)

        calendar_days: list[ExecutionCalendarDayRead] = []
        today = self._today_for(goal)
        allocator = DayAllocator(self.session)
        dependency_indexes = allocator._dependency_indexes(plan.id)
        arrange_reasons = {
            task.id: self._flexible_arrange_blocked_reason(
                goal, activation, task, today, allocator, dependency_indexes,
            )
            for tasks in tasks_by_day.values() for task in tasks
        }
        for schedule in schedules:
            day = plan_days[schedule.plan_day_id]
            tasks = sorted(tasks_by_day.get(day.id, []), key=lambda item: item.order_in_day)
            active_tasks = [item for item in tasks if item.status != "CARRIED_OVER"]
            has_carried_over = any(item.status == "CARRIED_OVER" for item in tasks)
            has_unfinished = bool(active_tasks) and any(
                item.status not in {"COMPLETED", "CANCELLED"} for item in active_tasks
            )
            has_completed_work = any(
                item.status in {"COMPLETED", "CANCELLED"} for item in tasks
            )
            if schedule.scheduled_date < today and (has_carried_over or has_unfinished):
                status = "PARTIAL" if has_completed_work else "MISSED"
            elif active_tasks and all(
                item.status in {"COMPLETED", "CANCELLED"} for item in active_tasks
            ):
                status = "COMPLETED"
            elif any(item.status == "BLOCKED" for item in active_tasks):
                status = "BLOCKED"
            elif any(item.status == "IN_PROGRESS" for item in active_tasks):
                status = "IN_PROGRESS"
            else:
                status = "PLANNED"
            # Recovery can move individual tasks while retaining their original
            # day and its historical description. Read the current task ledger.
            # A published revision can retain old day descriptions after the
            # moved tasks have been cloned without source_task_id. Its task
            # ledger remains the authority for the current calendar heading.
            task_layout_changed = plan.revision > 1 or has_carried_over or any(task.source_task_id for task in tasks)
            current_summary = day.primary_goal
            if task_layout_changed:
                current_summary = "；".join(task.title for task in active_tasks[:3]) if active_tasks else "原任务已转入后续日期"
                if len(active_tasks) > 3:
                    current_summary += f"等共 {len(active_tasks)} 项任务"
                if active_tasks and day.primary_goal.startswith("接续完成"):
                    current_summary = f"接续完成：{current_summary}"
            calendar_days.append(
                ExecutionCalendarDayRead(
                    plan_day_id=day.id,
                    scheduled_date=schedule.scheduled_date,
                    code=f"M{day.stage_order}-W{day.week_index}-D{day.day_index}",
                    task_stage_orders=sorted({
                        origin for task in active_tasks
                        if (origin := self._task_origin_stage(task)) is not None
                    }),
                    primary_goal=current_summary,
                    status=status,
                    suggested_minutes=sum(task.estimate_minutes for task in active_tasks) if task_layout_changed else day.suggested_minutes,
                    tasks=[
                        CalendarTaskRead(
                            id=task.id,
                            title=task.title,
                            status=task.status,
                            is_required=task.is_required,
                            estimate_minutes=task.estimate_minutes,
                            origin_stage_order=self._task_origin_stage(task),
                            scheduling_policy=task.scheduling_policy or "EXACT_DAY",
                            scheduling_policy_data=task.scheduling_policy_json or {},
                            can_arrange_today=arrange_reasons[task.id] is None,
                            arrange_blocked_reason=arrange_reasons[task.id],
                            recommendation_group_key=recommendation_keys.get(task.id),
                            recommendation_position=(recommendation_meta.get(task.id) or (None, None, None))[0],
                            recommendation_total=(recommendation_meta.get(task.id) or (None, None, None))[1],
                            is_recommendation_primary=(recommendation_meta.get(task.id) or (None, None, True))[2],
                        )
                        for task in tasks
                    ],
                    changes=changes_by_day.get(day.id, []),
                )
            )

        return ExecutionCalendarRead(
            first_date=schedules[0].scheduled_date,
            last_date=schedules[-1].scheduled_date,
            days=calendar_days,
        )

    def get_progress(self, goal_id: str) -> ExecutionProgressRead:
        goal, plan = self._goal_and_plan(goal_id)
        if goal.status not in {
            "ACTIVE",
            "RECOVERY_REQUIRED",
            "PAUSED",
            "COMPLETED",
            "ARCHIVED",
        }:
            raise HTTPException(status_code=409, detail="Plan has not started")
        activation = self._activation(plan.id)
        if activation is None:
            raise HTTPException(status_code=409, detail="Plan calendar is missing")

        schedules = list(
            self.session.scalars(
                select(PlanDaySchedule)
                .where(PlanDaySchedule.activation_id == activation.id)
                .order_by(PlanDaySchedule.scheduled_date)
            )
        )
        days = {
            item.id: item
            for item in self.session.scalars(
                select(PlanDay).where(PlanDay.plan_version_id == plan.id)
            )
        }
        milestones = {
            item.stage_order: item
            for item in self.session.scalars(
                select(PlanMilestone).where(PlanMilestone.plan_version_id == plan.id)
            )
        }
        tasks = self._tasks(plan.id)
        tasks_by_day: dict[str, list[PlanTask]] = {}
        for task in tasks:
            tasks_by_day.setdefault(task.plan_day_id, []).append(task)
        logs = {
            item.plan_day_id: item
            for item in self.session.scalars(
                select(ExecutionDayLog).where(ExecutionDayLog.plan_version_id == plan.id)
            )
        }
        feedbacks = list(
            self.session.scalars(
                select(Feedback)
                .where(Feedback.goal_id == goal.id, Feedback.plan_version_id == plan.id)
                .order_by(Feedback.created_at.desc())
            )
        )
        feedbacks_by_day: dict[str, list[Feedback]] = {}
        for feedback in feedbacks:
            feedbacks_by_day.setdefault(feedback.plan_day_id, []).append(feedback)
        adjustments = {
            item.feedback_id: item
            for item in self.session.scalars(
                select(FeedbackAdjustment).where(
                    FeedbackAdjustment.goal_id == goal.id,
                    FeedbackAdjustment.plan_version_id == plan.id,
                )
            )
        }
        focus_by_day = {
            day_id: int(seconds or 0)
            for day_id, seconds in self.session.execute(
                select(
                    FocusSession.plan_day_id,
                    func.sum(FocusSession.actual_seconds),
                )
                .where(
                    FocusSession.goal_id == goal.id,
                    FocusSession.plan_version_id == plan.id,
                )
                .group_by(FocusSession.plan_day_id)
            )
        }

        today = self._today_for(goal)
        current_schedule = next(
            (item for item in schedules if item.scheduled_date >= today),
            schedules[-1] if schedules else None,
        )
        current_day = days.get(current_schedule.plan_day_id) if current_schedule else None
        current_stage_order = current_day.stage_order if current_day else None
        current_stage = milestones.get(current_stage_order) if current_stage_order else None
        stage_tasks = self._tasks_for_stage(tasks, current_stage_order)

        elapsed = [item for item in schedules if item.scheduled_date <= today]
        window = elapsed[-7:]
        progress_days: list[ExecutionProgressDayRead] = []
        completed_days = 0
        planned_minutes = 0
        focus_minutes = 0
        struggle_run = 0
        easy_run = 0
        struggle_codes = {"TOO_HARD", "NOT_ENOUGH_TIME", "TOO_HARD_OVERALL", "TOO_HEAVY"}
        easy_codes = {"EARLY_FINISH", "TOO_EASY", "TOO_LIGHT"}
        for schedule in window:
            day = days[schedule.plan_day_id]
            day_tasks = [
                task for task in tasks_by_day.get(day.id, []) if task.status != "CARRIED_OVER"
            ]
            complete_count = sum(task.status == "COMPLETED" for task in day_tasks)
            is_complete = bool(day_tasks) and all(
                task.status in {"COMPLETED", "CANCELLED"} for task in day_tasks
            )
            if is_complete:
                completed_days += 1
            day_feedback = feedbacks_by_day.get(day.id, [])
            reasons = {item.reason_code for item in day_feedback}
            struggle_run = struggle_run + 1 if reasons & struggle_codes else 0
            easy_run = easy_run + 1 if reasons & easy_codes else 0
            planned_minutes += sum(task.estimate_minutes for task in day_tasks)
            day_focus_minutes = round(focus_by_day.get(day.id, 0) / 60)
            focus_minutes += day_focus_minutes
            log = logs.get(day.id)
            progress_days.append(
                ExecutionProgressDayRead(
                    scheduled_date=schedule.scheduled_date,
                    code=f"M{day.stage_order}-W{day.week_index}-D{day.day_index}",
                    stage_order=day.stage_order,
                    stage_title=milestones[day.stage_order].title,
                    primary_goal=day.primary_goal,
                    status="COMPLETED" if is_complete else (log.status if log else "PLANNED"),
                    completed_tasks=complete_count,
                    total_tasks=len(day_tasks),
                    carried_over_tasks=sum(task.status == "CARRIED_OVER" for task in tasks_by_day.get(day.id, [])),
                    focus_minutes=day_focus_minutes,
                    feedback_count=len(day_feedback),
                    summary=log.summary if log else None,
                )
            )

        observed_days = sum(any(task.status != "CARRIED_OVER" for task in tasks_by_day.get(item.plan_day_id, []))
                            for item in window)
        completion_rate = round(completed_days / observed_days * 100) if observed_days else 0
        recommendation_title = None
        recommendation_request = None
        should_offer_adjustment = False
        if struggle_run >= 3:
            trend_status = "NEEDS_REVIEW"
            trend_summary = "最近几次执行有些吃力，可以看看接下来几天是否需要放松一点。"
            should_offer_adjustment = True
            recommendation_title = "看看接下来几天怎么调"
            recommendation_request = (
                f"最近连续 {struggle_run} 个执行日反馈偏难或偏重。请先判断问题是否"
                "集中在当前阶段，只调整尚未开始的近期安排；不要修改历史执行日，"
                "也不要默认降低后续所有阶段的难度。"
            )
        elif easy_run >= 3:
            if self._progress(stage_tasks).percentage < 35:
                trend_status = "EXPECTED_RAMP"
                trend_summary = (
                    "现在还在阶段前段，进行得轻松是正常的。先保持节奏，后面的内容会逐渐加深。"
                )
            else:
                trend_status = "AHEAD"
                trend_summary = "最近进行得很顺利。如果你愿意，可以学得更深入或适当加快。"
                should_offer_adjustment = True
                recommendation_title = "想学深一点或加快一些"
                recommendation_request = (
                    f"最近连续 {easy_run} 个执行日推进较快。请先给出“加深当前内容”和"
                    "“适度提前后续任务”两种选择，只调整未开始内容，不默认压缩整个周期。"
                )
        elif observed_days < 3:
            trend_status = "LEARNING"
            trend_summary = "目前进展顺利，先保持现在的节奏。"
        else:
            trend_status = "STABLE"
            trend_summary = "最近的完成情况比较稳定，暂时不需要调整。"

        schedule_dates = {item.plan_day_id: item.scheduled_date for item in schedules}
        task_titles = {item.id: item.title for item in tasks}
        revisions = list(
            self.session.scalars(
                select(PlanVersion)
                .where(PlanVersion.goal_id == goal.id)
                .order_by(PlanVersion.created_at.desc())
            )
        )
        # The stage may point at the next execution day on a rest day, but
        # the card labelled "today" must use today's actual task ledger.
        today_tasks = [task for schedule in schedules if schedule.scheduled_date == today
                       for task in tasks_by_day.get(schedule.plan_day_id, [])]
        day_progress = self._progress(today_tasks)
        stage_progress = self._progress(stage_tasks)
        overall_progress = self._progress(tasks)
        exploration = self._exploration_state(plan, tasks)
        inactivity = self._inactivity(goal, plan, activation, schedules, tasks_by_day)
        rhythm = (
            self._rhythm_progress(today, plan, schedules, tasks_by_day, focus_by_day)
            if plan.schedule_type == "RHYTHM"
            else None
        )
        review_signal = self._review_signal(
            plan, stage_progress, rhythm, inactivity, should_offer_adjustment
        )
        return ExecutionProgressRead(
            total_focus_minutes=round(int(self.session.scalar(select(func.sum(FocusSession.actual_seconds)).where(
                FocusSession.goal_id == goal.id)) or 0) / 60),
            applied_adjustments=int(self.session.scalar(select(func.count()).select_from(FeedbackAdjustment).where(
                FeedbackAdjustment.goal_id == goal.id, FeedbackAdjustment.status == "APPLIED")) or 0)
                + int(self.session.scalar(select(func.count()).select_from(PlanAdjustmentPreview).where(
                    PlanAdjustmentPreview.goal_id == goal.id, PlanAdjustmentPreview.status == "APPLIED")) or 0),
            goal_id=goal.id,
            goal_title=goal.title,
            plan_revision=plan.revision,
            schedule_type=plan.schedule_type,
            current_stage_order=current_stage_order,
            current_stage_title=current_stage.title if current_stage else None,
            day_progress=day_progress,
            stage_progress=stage_progress,
            overall_progress=overall_progress,
            exploration=exploration,
            stage_summary=self._progress_summary("当前阶段", stage_progress),
            overall_summary=(
                self._progress_summary("已展开任务", overall_progress)
                + f" 第 {exploration.next_unplanned_week} 周及之后尚未展开，本轮路线尚未结束。"
                if exploration and exploration.next_unplanned_week is not None
                else self._progress_summary("整个目标", overall_progress)
            ),
            trend=ExecutionTrendRead(
                window_days=observed_days,
                completed_days=completed_days,
                completion_rate=completion_rate,
                consecutive_struggle_days=struggle_run,
                consecutive_easy_days=easy_run,
                planned_minutes=planned_minutes,
                focus_minutes=focus_minutes,
                status=trend_status,
                summary=trend_summary,
                should_offer_adjustment=should_offer_adjustment,
                recommendation_title=recommendation_title,
                recommendation_request=recommendation_request,
            ),
            rhythm=rhythm,
            review_signal=review_signal,
            inactivity=inactivity,
            recent_days=list(reversed(progress_days)),
            feedback_timeline=[
                ExecutionFeedbackTimelineRead(
                    id=item.id,
                    scheduled_date=schedule_dates[item.plan_day_id],
                    task_title=task_titles.get(item.task_id),
                    feedback_kind=item.feedback_kind,
                    reason_code=item.reason_code,
                    intent_code=item.intent_code,
                    detail_text=item.detail_text,
                    status=item.status,
                    created_at=item.created_at,
                    adjustment=(
                        self._adjustment_read(adjustments[item.id])
                        if item.id in adjustments
                        else None
                    ),
                )
                for item in feedbacks
                if item.plan_day_id in schedule_dates
            ],
            revision_history=[
                ExecutionPlanRevisionRead(
                    id=item.id,
                    revision=item.revision,
                    status=item.status,
                    feedback=item.feedback,
                    changes=item.change_summary_json,
                    created_at=item.created_at,
                )
                for item in revisions
            ],
        )

    @staticmethod
    def _commitment_progress(
        today: date,
        plan: PlanVersion,
        schedules: list[PlanDaySchedule],
        days: list[PlanDay],
        tasks: list[PlanTask],
        events: list[ProgressEvent] | None = None,
    ) -> list[CommitmentProgressRead]:
        """Project measurable commitments onto the current execution period.

        Commitments are stored on the plan as intent, while task bindings and
        statuses provide the observed evidence.  This projection deliberately
        reports the minimum target: a user can exceed it without changing the
        plan, and an unbound task never silently counts toward a commitment.
        """
        commitments = list(plan.commitments_json or [])
        if not commitments or not schedules or not days:
            return []
        day_by_id = {item.id: item for item in days}
        schedule_by_day = {item.plan_day_id: item for item in schedules}
        dated_days = sorted(
            (
                item.scheduled_date,
                day_by_id[item.plan_day_id].week_index,
                item.plan_day_id,
            )
            for item in schedules
            if item.plan_day_id in day_by_id
        )
        if not dated_days:
            return []
        anchor = next((item for item in dated_days if item[0] == today), None)
        if anchor is None:
            anchor = next((item for item in dated_days if item[0] > today), dated_days[-1])
        current_week = anchor[1]
        task_dates = {
            task.id: schedule_by_day[task.plan_day_id].scheduled_date
            for task in tasks
            if task.plan_day_id in schedule_by_day
        }
        task_weeks = {
            task.id: day_by_id[task.plan_day_id].week_index
            for task in tasks
            if task.plan_day_id in day_by_id
        }
        events_by_task: dict[str, list[ProgressEvent]] = {}
        for event in events or []:
            if event.task_id:
                events_by_task.setdefault(event.task_id, []).append(event)

        def task_minutes(task: PlanTask) -> tuple[int, int, int]:
            """Return completed, partial and remaining planned minutes.

            Planned effort and recorded effort have different meanings:
            completing a task settles its estimate, while recorded minutes
            may exceed that estimate and remain visible after completion.
            """
            estimate = max(0, int(getattr(task, "estimate_minutes", 0) or 0))
            partial = 0
            for event in events_by_task.get(task.id, []):
                if event.event_type != "PARTIAL_COMPLETION":
                    continue
                metadata = event.metadata_json if isinstance(event.metadata_json, dict) else {}
                if str(metadata.get("unit") or "MINUTES") == "MINUTES":
                    try:
                        partial += max(0, int(metadata.get("amount") or 0))
                    except (TypeError, ValueError):
                        continue
            if task.status == "COMPLETED":
                return estimate, partial, 0
            return 0, partial, max(0, estimate - partial)
        result: list[CommitmentProgressRead] = []
        for raw in commitments:
            if not isinstance(raw, dict):
                continue
            label = str(raw.get("label") or "").strip()
            cadence = raw.get("cadence")
            if not label or cadence not in {"TOTAL", "PER_WEEK", "PER_EXECUTION_DAY"}:
                continue
            start_week = int(raw.get("start_week") or 1)
            end_week = int(raw.get("end_week") or start_week)
            active_week = min(max(current_week, start_week), end_week)
            if cadence == "PER_WEEK":
                period_tasks = [
                    task for task in tasks
                    if task_weeks.get(task.id) == active_week
                    and label in (task.satisfies_commitments_json or [])
                ]
                period_dates = [
                    item[0] for item in dated_days if item[1] == active_week
                ]
            elif cadence == "PER_EXECUTION_DAY":
                period_tasks = [
                    task for task in tasks
                    if task_dates.get(task.id) == today
                    and label in (task.satisfies_commitments_json or [])
                ]
                # A rest day is not a missed execution day for a per-day
                # commitment.  Leave the period open until an execution day
                # is actually scheduled.
                period_dates = [today] if any(item[0] == today for item in dated_days) else []
            else:
                period_tasks = [
                    task for task in tasks
                    if start_week <= task_weeks.get(task.id, 0) <= end_week
                    and label in (task.satisfies_commitments_json or [])
                ]
                period_dates = [
                    item[0] for item in dated_days if start_week <= item[1] <= end_week
                ]
            target = max(0, int(raw.get("minimum_occurrences") or 0))
            occurrences: dict[str, list[PlanTask]] = {}
            for task in period_tasks:
                if task.status == 'CARRIED_OVER':
                    # The continuation copy settles this action. Its historical
                    # placeholder must not keep the same occurrence incomplete.
                    continue
                identity = (getattr(task, 'scheduling_policy_json', None) or {}).get('occurrence_key')
                # Historical plans counted frequency per execution day at
                # generation. Preserve that same unit when observing progress.
                key = 'whole-outcome' if cadence == 'TOTAL' and target == 1 else str(identity or task.plan_day_id)
                occurrences.setdefault(key, []).append(task)
            completed = sum(all(task.status == 'COMPLETED' for task in parts)
                            for parts in occurrences.values())
            completed_minutes = 0
            partial_minutes = 0
            remaining_minutes = 0
            for task in period_tasks:
                completed_part, partial_part, remaining_part = task_minutes(task)
                completed_minutes += completed_part
                partial_minutes += partial_part
                if task.status != 'CARRIED_OVER':
                    remaining_minutes += remaining_part
            period_start = min(period_dates) if period_dates else None
            period_end = max(period_dates) if period_dates else None
            if target == 0 and completed == 0:
                risk = "NO_DEADLINE"
            elif completed >= target:
                risk = "COMPLETE"
            elif period_end is None:
                risk = "NO_DEADLINE"
            elif period_end < today or period_end <= today + timedelta(days=1):
                risk = "AT_RISK"
            elif period_end <= today + timedelta(days=3):
                risk = "DUE_SOON"
            else:
                risk = "ON_TRACK"
            result.append(
                CommitmentProgressRead(
                    label=label,
                    cadence=cadence,
                    period_start=period_start,
                    period_end=period_end,
                    target_occurrences=target,
                    completed_occurrences=completed,
                    remaining_occurrences=max(0, target - completed),
                    completed_minutes=completed_minutes,
                    partial_minutes=partial_minutes,
                    remaining_minutes=remaining_minutes,
                    risk=risk,
                    source_text=str(raw.get("source_text") or ""),
                )
            )
        return result

    @staticmethod
    def _commitment_alerts(
        progress: list[CommitmentProgressRead],
        *,
        preference: PlanSchedulePreference | None = None,
        now: datetime | None = None,
    ) -> list[CommitmentAlertRead]:
        if preference is not None:
            if not preference.reminders_enabled or preference.supervision_mode == "QUIET":
                return []
            muted = set(getattr(preference, "muted_commitments_json", []) or [])
            if preference.quiet_start_local and preference.quiet_end_local:
                current = now or datetime.now(ZoneInfo(preference.timezone))
                current_minutes = current.hour * 60 + current.minute
                start_hour, start_minute = map(int, preference.quiet_start_local.split(":"))
                end_hour, end_minute = map(int, preference.quiet_end_local.split(":"))
                start_minutes = start_hour * 60 + start_minute
                end_minutes = end_hour * 60 + end_minute
                quiet = (
                    start_minutes <= current_minutes < end_minutes
                    if start_minutes < end_minutes
                    else current_minutes >= start_minutes or current_minutes < end_minutes
                )
                if quiet:
                    return []
        else:
            muted = set()
        alerts: list[CommitmentAlertRead] = []
        for item in progress:
            if item.label in muted:
                continue
            if item.risk not in {"DUE_SOON", "AT_RISK"} or item.remaining_occurrences <= 0:
                continue
            urgent = item.risk == "AT_RISK"
            if urgent:
                title = f"{item.label} 的完成窗口需要处理"
                message = f"当前还差 {item.remaining_occurrences} 次，周期已经临近或经过，需要讨论承接、拆分或调整安排。"
            else:
                title = f"{item.label} 即将进入检查窗口"
                message = f"当前还差 {item.remaining_occurrences} 次，建议提前安排，避免最后集中处理。"
            alerts.append(
                CommitmentAlertRead(
                    label=item.label,
                    risk=item.risk,
                    severity="URGENT" if urgent else "WARNING",
                    title=title,
                    message=message,
                    action="REVIEW_PLAN" if urgent else "DISCUSS",
                    period_end=item.period_end,
                    remaining_occurrences=item.remaining_occurrences,
                    dedupe_key=(
                        f"{item.label}|{item.risk}|{item.period_end}|{item.remaining_occurrences}"
                    ),
                )
            )
        return alerts

    @staticmethod
    def _reminder_event_read(item: ReminderEvent) -> ReminderEventRead:
        return ReminderEventRead(
            id=item.id,
            goal_id=item.goal_id,
            plan_version_id=item.plan_version_id,
            dedupe_key=item.dedupe_key,
            label=item.label,
            risk=item.risk,
            severity=item.severity,
            title=item.title,
            message=item.message,
            action=item.action,
            period_end=item.period_end,
            remaining_occurrences=item.remaining_occurrences,
            status=item.status,
            first_seen_at=item.first_seen_at,
            last_seen_at=item.last_seen_at,
            read_at=item.read_at,
            dismissed_at=item.dismissed_at,
        )

    def _sync_reminder_events(
        self,
        goal: Goal,
        plan: PlanVersion,
        alerts: list[CommitmentAlertRead],
    ) -> list[CommitmentAlertRead]:
        """Persist the current risk snapshot and suppress dismissed snapshots."""
        if not alerts:
            return []
        keys = [item.dedupe_key for item in alerts]
        existing = list(
            self.session.scalars(
                select(ReminderEvent).where(
                    ReminderEvent.goal_id == goal.id,
                    ReminderEvent.dedupe_key.in_(keys),
                )
            )
        )
        by_key = {item.dedupe_key: item for item in existing}
        now = utc_now()
        pending: list[tuple[CommitmentAlertRead, ReminderEvent]] = []
        changed = False
        for alert in alerts:
            event = by_key.get(alert.dedupe_key)
            if event is None:
                event = ReminderEvent(
                    goal_id=goal.id,
                    plan_version_id=plan.id,
                    dedupe_key=alert.dedupe_key,
                    label=alert.label,
                    risk=alert.risk,
                    severity=alert.severity,
                    title=alert.title,
                    message=alert.message,
                    action=alert.action,
                    period_end=alert.period_end,
                    remaining_occurrences=alert.remaining_occurrences,
                    status="OPEN",
                    first_seen_at=now,
                    last_seen_at=now,
                )
                self.session.add(event)
                changed = True
            else:
                event.plan_version_id = plan.id
                event.label = alert.label
                event.risk = alert.risk
                event.severity = alert.severity
                event.title = alert.title
                event.message = alert.message
                event.action = alert.action
                event.period_end = alert.period_end
                event.remaining_occurrences = alert.remaining_occurrences
                event.last_seen_at = now
                changed = True
            if event.status == "DISMISSED":
                continue
            pending.append((alert, event))
        if changed:
            self.session.flush()
            self.session.commit()
        return [
            alert.model_copy(update={"event_id": event.id, "status": event.status})
            for alert, event in pending
        ]

    def list_reminder_events(
        self, goal_id: str, *, include_closed: bool = False
    ) -> list[ReminderEventRead]:
        self._goal_and_plan(goal_id)
        statement = select(ReminderEvent).where(ReminderEvent.goal_id == goal_id)
        if not include_closed:
            statement = statement.where(ReminderEvent.status != "DISMISSED")
        events = list(
            self.session.scalars(statement.order_by(ReminderEvent.last_seen_at.desc()))
        )
        return [self._reminder_event_read(item) for item in events]

    def update_reminder_event(
        self, goal_id: str, event_id: str, payload: ReminderEventAction
    ) -> ReminderEventRead:
        self._goal_and_plan(goal_id)
        event = self.session.scalar(
            select(ReminderEvent).where(
                ReminderEvent.id == event_id,
                ReminderEvent.goal_id == goal_id,
            )
        )
        if event is None:
            raise HTTPException(status_code=404, detail="Reminder event not found")
        now = utc_now()
        if payload.action == "READ":
            event.status = "READ"
            event.read_at = now
            event.dismissed_at = None
        elif payload.action == "DISMISS":
            event.status = "DISMISSED"
            event.dismissed_at = now
        else:
            event.status = "OPEN"
            event.read_at = None
            event.dismissed_at = None
        event.last_seen_at = now
        self.session.commit()
        return self._reminder_event_read(event)

    @staticmethod
    def _rhythm_progress(
        today: date,
        plan: PlanVersion,
        schedules: list[PlanDaySchedule],
        tasks_by_day: dict[str, list[PlanTask]],
        focus_by_day: dict[str, int],
    ) -> RhythmProgressRead:
        cycle_start = today - timedelta(days=today.weekday())
        cycle_end = cycle_start + timedelta(days=6)
        observed_start = cycle_start - timedelta(days=21)
        observed = [
            item for item in schedules if observed_start <= item.scheduled_date <= today
        ]
        observed_cycle_starts = {
            item.scheduled_date - timedelta(days=item.scheduled_date.weekday())
            for item in observed
        }
        observed_cycles = max(1, len(observed_cycle_starts))

        def completed(item: PlanDaySchedule) -> bool:
            tasks = [
                task
                for task in tasks_by_day.get(item.plan_day_id, [])
                if task.status != "CARRIED_OVER"
            ]
            return bool(tasks) and all(
                task.status in {"COMPLETED", "CANCELLED"} for task in tasks
            )

        completed_observed = sum(completed(item) for item in observed)
        current_completed = sum(
            completed(item)
            for item in schedules
            if cycle_start <= item.scheduled_date <= min(today, cycle_end)
        )
        target = max(1, plan.weekly_execution_days)
        average = round(completed_observed / observed_cycles, 1)
        due_opportunities = max(1, min(len(observed), target * observed_cycles))
        adherence = round(completed_observed / due_opportunities * 100)
        planned_minutes = sum(
            task.estimate_minutes
            for item in observed
            for task in tasks_by_day.get(item.plan_day_id, [])
            if task.status != "CARRIED_OVER"
        )
        focus_minutes = round(sum(focus_by_day.get(item.plan_day_id, 0) for item in observed) / 60)
        if len(observed) < target:
            pace_status = "LEARNING"
            forecast = "执行样本还不多，先保持当前安排，继续观察实际频率和感受。"
        elif adherence < 70:
            pace_status = "BELOW"
            forecast = (
                f"最近平均每周期完成 {average} 次，低于当前目标 {target} 次，"
                "建议讨论频率或单次负荷。"
            )
        elif adherence > 110:
            pace_status = "ABOVE"
            forecast = (
                f"最近平均每周期完成 {average} 次，高于当前目标，"
                "可以讨论加深内容而不是只增加次数。"
            )
        else:
            pace_status = "SUSTAINABLE"
            forecast = f"最近平均每周期完成 {average} 次，与当前目标 {target} 次基本匹配。"
        return RhythmProgressRead(
            cycle_start=cycle_start,
            cycle_end=cycle_end,
            target_sessions=target,
            completed_sessions=current_completed,
            remaining_sessions=max(0, target - current_completed),
            observed_cycles=observed_cycles,
            average_completed_sessions=average,
            adherence_rate=adherence,
            planned_minutes=planned_minutes,
            focus_minutes=focus_minutes,
            pace_status=pace_status,
            forecast_summary=forecast,
        )

    @staticmethod
    def _review_signal(
        plan: PlanVersion,
        stage_progress: ProgressSummary,
        rhythm: RhythmProgressRead | None,
        inactivity: ExecutionInactivityRead,
        should_offer_adjustment: bool,
    ) -> ExecutionReviewSignalRead:
        if plan.schedule_type == "OUTCOME" and stage_progress.percentage >= 100:
            return ExecutionReviewSignalRead(
                recommended=True,
                trigger_type="OUTCOME_CHECKPOINT",
                reason="当前成果阶段已经完成，可以结合实际收获讨论下一步。",
            )
        if plan.schedule_type == "RHYTHM" and rhythm:
            if should_offer_adjustment or rhythm.pace_status in {"BELOW", "ABOVE"}:
                return ExecutionReviewSignalRead(
                    recommended=True,
                    trigger_type="RHYTHM_MISMATCH",
                    reason="近期执行频率或体感与当前节奏不完全匹配，适合一起复盘。",
                )
            if rhythm.observed_cycles >= 2 and rhythm.average_completed_sessions > 0:
                return ExecutionReviewSignalRead(
                    recommended=True,
                    trigger_type="RHYTHM_SAMPLE_READY",
                    reason="已经积累了多个周期的执行样本，可以判断当前节奏是否有效。",
                )
        if plan.schedule_type == "CALENDAR" and inactivity.missed_execution_days > 0:
            return ExecutionReviewSignalRead(
                recommended=True,
                trigger_type="DEADLINE_RISK",
                reason="已有计划节点未按期完成，需要结合剩余容量检查期限风险。",
            )
        return ExecutionReviewSignalRead()

    def get_focus_sessions(self, goal_id: str) -> list[FocusSessionRead]:
        goal, plan = self._goal_and_plan(goal_id)
        sessions = list(
            self.session.scalars(
                select(FocusSession)
                .where(
                    FocusSession.goal_id == goal.id,
                    FocusSession.plan_version_id == plan.id,
                )
                .order_by(FocusSession.completed_at.desc())
                .limit(30)
            )
        )
        return [self._focus_session_read(item) for item in sessions]

    def record_focus_session(self, goal_id: str, payload: FocusSessionCreate) -> FocusSessionRead:
        goal, plan = self._goal_and_plan(goal_id)
        current = self.get_today(goal_id)
        if current.day is None:
            raise HTTPException(status_code=409, detail="Today is not an execution day")
        task = self.session.get(PlanTask, payload.task_id) if payload.task_id else None
        if task is not None and (
            task.plan_version_id != plan.id or task.plan_day_id != current.day.id
        ):
            raise HTTPException(status_code=404, detail="Task not found in today's plan")
        if payload.task_id and task is None:
            raise HTTPException(status_code=404, detail="Task not found in today's plan")
        session = FocusSession(
            goal_id=goal.id,
            plan_version_id=plan.id,
            plan_day_id=current.day.id,
            task_id=task.id if task else None,
            task_title=task.title if task else None,
            planned_minutes=payload.planned_minutes,
            actual_seconds=payload.actual_seconds,
            interruptions=payload.interruptions,
            status=(
                "COMPLETED"
                if payload.actual_seconds >= payload.planned_minutes * 60
                else "ENDED_EARLY"
            ),
        )
        self.session.add(session)
        self.session.flush()
        self._refresh_day_log(goal, plan, self.session.get(PlanDay, current.day.id))
        goal.last_active_at = utc_now()
        self.session.commit()
        return self._focus_session_read(session)

    def get_quick_captures(self, goal_id: str) -> list[QuickCaptureRead]:
        goal, _plan = self._goal_and_plan(goal_id)
        captures = list(
            self.session.scalars(
                select(QuickCapture)
                .where(QuickCapture.goal_id == goal.id, QuickCapture.status == "INBOX")
                .order_by(QuickCapture.created_at.desc())
            )
        )
        return [self._quick_capture_read(item) for item in captures]

    @serialize_recovery_transaction
    def preview_recovery_shift(
        self, goal_id: str, anchor_date: date | None = None
    ) -> RecoveryShiftPreviewRead:
        goal, plan = self._goal_and_plan(goal_id)
        if goal.status not in {"ACTIVE", "RECOVERY_REQUIRED"}:
            raise HTTPException(status_code=409, detail="Plan has not started")
        activation = self._activation(plan.id)
        if activation is None:
            raise HTTPException(status_code=409, detail="Plan calendar is missing")
        anchor = anchor_date or self._today_for(goal)
        preview = self._build_recovery_shift_preview(plan, activation, anchor)
        preview["time_preview"] = preview_time(self, goal, plan, preview)
        fingerprint = preview_fingerprint(preview)
        key = f"shift:{goal.id}:{plan.id}:{anchor.isoformat()}:{fingerprint}"
        operation = self.session.scalar(
            select(RecoveryOperation).where(RecoveryOperation.idempotency_key == key)
        )
        if operation is None:
            operation = RecoveryOperation(
                goal_id=goal.id,
                source_plan_version_id=plan.id,
                mode="SHIFT_FORWARD",
                status="PREVIEW",
                idempotency_key=key,
                preview_json=preview,
            )
            self.session.add(operation)
            self.session.commit()
        return self._recovery_preview_read(operation)

    @serialize_recovery_transaction
    def apply_recovery_shift(
        self, goal_id: str, preview_id: str, accept_unallocated: bool = False,
    ) -> RecoveryShiftPreviewRead:
        if self.session.bind.dialect.name == "sqlite":
            self.session.execute(text("BEGIN IMMEDIATE"))
        operation = self.session.get(RecoveryOperation, preview_id)
        if operation is None or operation.goal_id != goal_id or operation.mode != "SHIFT_FORWARD":
            raise HTTPException(status_code=404, detail="Recovery preview not found")
        if operation.status == "APPLIED":
            return self._recovery_preview_read(operation)
        goal, current = self._goal_and_plan(goal_id)
        if (
            goal.status not in {"ACTIVE", "RECOVERY_REQUIRED"}
            or current.id != operation.source_plan_version_id
        ):
            raise HTTPException(status_code=409, detail="Recovery preview is stale")
        activation = self._activation(current.id)
        if activation is None:
            raise HTTPException(status_code=409, detail="Plan calendar is missing")
        anchor = date.fromisoformat(operation.preview_json["anchor_date"])
        current_preview = self._build_recovery_shift_preview(current, activation, anchor)
        current_preview["time_preview"] = preview_time(
            self,
            goal,
            current,
            current_preview,
        )
        if preview_fingerprint(current_preview) != preview_fingerprint(operation.preview_json):
            raise HTTPException(status_code=409, detail="Recovery preview is stale")
        self._require_time_decision(current_preview, accept_unallocated)
        revised = self._clone_plan_for_recovery(goal, current, operation.preview_json)
        mark_plan_schedule_review(
            self.session,
            goal,
            current.id,
            revised.id,
            "整体顺延后，原未来时间安排需要根据新的执行日期重新确认。",
        )
        current.status = "SUPERSEDED"
        goal.active_plan_version_id = revised.id
        goal.status = "ACTIVE"
        goal.pause_until = None
        goal.last_active_at = utc_now()
        operation.result_plan_version_id = revised.id
        allocate_plan_schedule(
            self.session,
            goal,
            revised.id,
        )
        operation.status = "APPLIED"
        operation.applied_at = utc_now()
        operation.preview_json = {
            **operation.preview_json,
            "result_plan_version_id": revised.id,
        }
        self._verify_published_time(goal, revised, operation.preview_json)
        operation.preview_json = {**operation.preview_json,
                                  "accepted_unallocated": accept_unallocated}
        self.session.commit()
        return self._recovery_preview_read(operation)

    def defer_recovery(self, goal_id: str, missed_dates: list[date]) -> ExecutionInactivityRead:
        goal, plan = self._goal_and_plan(goal_id)
        workspace = self.get_today(goal_id)
        inactivity = workspace.inactivity
        # A recent discussion can refresh last_activity_at while older
        # execution days are still unresolved.  Recovery is determined by
        # the execution ledger, not by the inactivity severity label.
        if not inactivity.missed_dates:
            raise HTTPException(status_code=409, detail="No missed work needs recovery")
        actual_dates = sorted(item.isoformat() for item in inactivity.missed_dates)
        requested_dates = sorted(item.isoformat() for item in missed_dates)
        if requested_dates and requested_dates != actual_dates:
            raise HTTPException(status_code=409, detail="Missed work has changed")
        fingerprint = hashlib.sha256("|".join(actual_dates).encode("utf-8")).hexdigest()[:24]
        key = f"defer:{goal.id}:{plan.id}:{fingerprint}"
        operation = self.session.scalar(
            select(RecoveryOperation).where(RecoveryOperation.idempotency_key == key)
        )
        if operation is None:
            self.session.add(
                RecoveryOperation(
                    goal_id=goal.id,
                    source_plan_version_id=plan.id,
                    mode="DEFER_UNRESOLVED",
                    status="DEFERRED",
                    idempotency_key=key,
                    preview_json={"missed_dates": actual_dates},
                )
            )
        if goal.status == "RECOVERY_REQUIRED":
            goal.status = "ACTIVE"
            goal.pause_until = None
        self.session.commit()
        return self.get_today(goal_id).inactivity

    @serialize_recovery_transaction
    def preview_recovery_split(self, goal_id, payload):
        from planner_api.recovery_split import preview_split

        return preview_split(self, goal_id, payload)

    @serialize_recovery_transaction
    def apply_recovery_split(self, goal_id, preview_id):
        if self.session.bind.dialect.name == "sqlite":
            self.session.execute(text("BEGIN IMMEDIATE"))
        from planner_api.recovery_split import apply_split

        return apply_split(self, goal_id, preview_id)

    @serialize_recovery_transaction
    def preview_recovery_catchup(
        self,
        goal_id: str,
        anchor_date: date | None = None,
        include_non_execution_days: bool = False,
        selected_task_ids: list[str] | None = None,
        source_plan_version_id: str | None = None,
    ) -> RecoveryCatchupPreviewRead:
        goal, plan = self._goal_and_plan(goal_id)
        if source_plan_version_id is not None and source_plan_version_id != plan.id:
            raise HTTPException(409, "当前规划已变化，请重新读取恢复任务范围。")
        if goal.status not in {"ACTIVE", "RECOVERY_REQUIRED"}:
            raise HTTPException(status_code=409, detail="Plan has not started")
        activation = self._activation(plan.id)
        if activation is None:
            raise HTTPException(status_code=409, detail="Plan calendar is missing")
        anchor = anchor_date or self._today_for(goal)
        preview = self._build_recovery_catchup_preview(
            plan, activation, anchor, include_non_execution_days, selected_task_ids
        )
        preview["time_preview"] = preview_time(self, goal, plan, preview, catchup=True)
        fingerprint = preview_fingerprint(preview)
        key = f"catchup:{goal.id}:{plan.id}:{anchor.isoformat()}:{fingerprint}"
        operation = self.session.scalar(
            select(RecoveryOperation).where(
                RecoveryOperation.idempotency_key.like(key + "%"),
                RecoveryOperation.status == "PREVIEW",
            ).order_by(RecoveryOperation.created_at.desc())
        )
        # A changed selection invalidates earlier confirmations for this source.
        for previous in self.session.scalars(select(RecoveryOperation).where(
            RecoveryOperation.goal_id == goal.id,
            RecoveryOperation.source_plan_version_id == plan.id,
            RecoveryOperation.mode == "KEEP_DEADLINE",
            RecoveryOperation.status == "PREVIEW",
        )):
            if operation is None or previous.id != operation.id:
                previous.status = "SUPERSEDED"
        if operation is None:
            from uuid import uuid4
            operation = RecoveryOperation(
                goal_id=goal.id,
                source_plan_version_id=plan.id,
                mode="KEEP_DEADLINE",
                status="PREVIEW",
                idempotency_key=key + ":" + str(uuid4()),
                preview_json=preview,
            )
            self.session.add(operation)
        self.session.commit()
        return self._recovery_catchup_preview_read(operation)

    @serialize_recovery_transaction
    def apply_recovery_catchup(
        self,
        goal_id: str,
        preview_id: str,
        accept_unallocated: bool = False,
    ) -> RecoveryCatchupPreviewRead:
        if self.session.bind.dialect.name == "sqlite":
            self.session.execute(text("BEGIN IMMEDIATE"))
        operation = self.session.get(RecoveryOperation, preview_id)
        if operation is None or operation.goal_id != goal_id or operation.mode != "KEEP_DEADLINE":
            raise HTTPException(status_code=404, detail="Recovery preview not found")
        if operation.status == "APPLIED":
            return self._recovery_catchup_preview_read(operation)
        if operation.status != "PREVIEW":
            raise HTTPException(409, "恢复范围已重新预览，请使用最新方案。")
        goal, current = self._goal_and_plan(goal_id)
        if (
            goal.status not in {"ACTIVE", "RECOVERY_REQUIRED"}
            or current.id != operation.source_plan_version_id
        ):
            raise HTTPException(status_code=409, detail="Recovery preview is stale")
        activation = self._activation(current.id)
        if activation is None:
            raise HTTPException(status_code=409, detail="Plan calendar is missing")
        anchor = date.fromisoformat(operation.preview_json["anchor_date"])
        try:
            current_preview = self._build_recovery_catchup_preview(
                current,
                activation,
                anchor,
                bool(operation.preview_json["include_non_execution_days"]),
                operation.preview_json.get("recovery_scope", {}).get("selected_task_ids"),
            )
            current_preview["time_preview"] = preview_time(
                self, goal, current, current_preview, catchup=True,
            )
        except HTTPException as error:
            if error.status_code in {409, 422}:
                raise HTTPException(409, "恢复任务范围或可用时间已变化，请重新读取并预览。") from error
            raise
        if preview_fingerprint(current_preview) != preview_fingerprint(operation.preview_json):
            raise HTTPException(status_code=409, detail="Recovery preview is stale")
        self._require_time_decision(current_preview, accept_unallocated)
        if not operation.preview_json["programmatic_feasible"]:
            raise HTTPException(status_code=409, detail="AI recovery planning is required")
        clone_preview = {
            **operation.preview_json,
            "mode": "KEEP_DEADLINE",
            "projected_end_date": operation.preview_json["original_end_date"],
            "changes": [],
            "carryover_task_ids": operation.preview_json["backlog_task_ids"],
            "carryovers": operation.preview_json["non_execution_assignments"],
            "redistributions": operation.preview_json["execution_assignments"],
        }
        revised = self._clone_plan_for_recovery(goal, current, clone_preview)
        mark_plan_schedule_review(
            self.session,
            goal,
            current.id,
            revised.id,
            "截止日期内追赶改变了任务分配，原未来时间安排需要重新确认。",
        )
        current.status = "SUPERSEDED"
        goal.active_plan_version_id = revised.id
        goal.status = "ACTIVE"
        goal.pause_until = None
        goal.last_active_at = utc_now()
        operation.result_plan_version_id = revised.id
        operation.status = "APPLIED"
        operation.applied_at = utc_now()
        operation.preview_json = {
            **operation.preview_json,
            "result_plan_version_id": revised.id,
        }
        allocate_plan_schedule(
            self.session,
            goal,
            revised.id,
            affected_dates=catchup_affected_dates(operation.preview_json),
            use_ai=False,
        )
        self._verify_published_time(goal, revised, operation.preview_json)
        operation.preview_json = {**operation.preview_json,
                                  "accepted_unallocated": accept_unallocated}
        self.session.commit()
        return self._recovery_catchup_preview_read(operation)

    @staticmethod
    def _require_time_decision(preview: dict, accepted: bool) -> None:
        schedule = preview.get("time_preview")
        if schedule and schedule["unallocated_count"] and not accepted:
            raise HTTPException(409, "仍有任务没有具体时段，请确认保留待安排或返回修改方案。")

    def _verify_published_time(self, goal, revised, preview):
        expected = preview.get("time_preview")
        if expected is None:
            return
        anchor = max(date.fromisoformat(preview["anchor_date"]), self._today_for(goal))
        after, _, _ = snapshot(self, goal, revised, anchor)
        # Diagnostics and source metadata explain the preview; verify actual
        # published content/times independently of simulation-only clone IDs.
        keys = ("title", "date", "start", "end", "minutes", "state")
        def layout(items):
            return sorted(
                tuple(str(item.get(key)) for key in keys) for item in items
            )
        if layout(after) != layout(expected["after"]):
            self.session.rollback()
            raise HTTPException(409, "排课结果已变化，请重新生成恢复预览。")

    def create_quick_capture(self, goal_id: str, payload: QuickCaptureCreate) -> QuickCaptureRead:
        goal, _plan = self._goal_and_plan(goal_id)
        capture = QuickCapture(goal_id=goal.id, content=payload.content, status="INBOX")
        self.session.add(capture)
        goal.last_active_at = utc_now()
        self.session.commit()
        return self._quick_capture_read(capture)

    def delete_quick_capture(self, goal_id: str, capture_id: str) -> list[QuickCaptureRead]:
        goal, _plan = self._goal_and_plan(goal_id)
        capture = self.session.get(QuickCapture, capture_id)
        if capture is None or capture.goal_id != goal.id:
            raise HTTPException(status_code=404, detail="Quick capture not found")
        self.session.delete(capture)
        self.session.commit()
        return self.get_quick_captures(goal_id)

    def preview(self, goal_id: str, payload: ExecutionScheduleRequest) -> ExecutionPreview:
        goal, plan = self._goal_and_plan(goal_id)
        if goal.status not in {"READY_TO_START", "ACTIVE"}:
            raise HTTPException(status_code=409, detail="Plan is not ready to start")
        preview = self._build_preview(plan, payload)
        if (
            plan.execution_model_version == ExecutionModelVersion.HYBRID_WORK_ITEM
            and not self._has_daily_plan(plan)
        ):
            self.session.commit()
        return preview

    @serialize_recovery_transaction
    def start(self, goal_id: str, payload: ExecutionScheduleRequest) -> TodayWorkspace:
        goal, plan = self._goal_and_plan(goal_id)
        existing = self._activation(plan.id)
        if goal.status == "ACTIVE" and existing:
            handoff_quick_plans(self.session, goal)
            self.session.commit()
            return self.get_today(goal_id)
        if goal.status != "READY_TO_START":
            raise HTTPException(status_code=409, detail="Plan is not ready to start")
        preview = self._build_preview(plan, payload)
        if (
            plan.execution_model_version == ExecutionModelVersion.HYBRID_WORK_ITEM
            and not self._has_daily_plan(plan)
        ):
            activation = self._work_item_materializer(plan, payload).apply(
                goal.id, plan, payload
            )
        else:
            mapped_days = self._mapped_days(plan, payload)
            activation = PlanActivation(
                goal_id=goal.id,
                plan_version_id=plan.id,
                requested_start_date=payload.requested_start_date,
                projected_end_date=preview.projected_end_date,
                execution_weekdays_json=payload.execution_weekdays,
                excluded_dates_json=[item.isoformat() for item in payload.excluded_dates],
            )
            self.session.add(activation)
            self.session.flush()
            for item in mapped_days:
                self.session.add(
                    PlanDaySchedule(
                        activation_id=activation.id,
                        plan_day_id=item.plan_day_id,
                        scheduled_date=item.scheduled_date,
                    )
                )
            if plan.execution_model_version == ExecutionModelVersion.HYBRID_WORK_ITEM:
                self.session.flush()
                self._work_item_materializer(plan, payload).attach_authored(
                    plan, activation, payload)
        self.session.flush()
        today_schedule = self.session.scalar(
            select(PlanDaySchedule).where(
                PlanDaySchedule.activation_id == activation.id,
                PlanDaySchedule.scheduled_date == self._today_for(goal),
            )
        )
        if today_schedule:
            self._refresh_day_log(goal, plan, self.session.get(PlanDay, today_schedule.plan_day_id))
        goal.status = "ACTIVE"
        goal.last_active_at = utc_now()
        handoff_quick_plans(self.session, goal)
        self.session.flush()
        allocate_plan_schedule(self.session, goal, plan.id)
        if payload.smart_preview_id:
            operation = self.session.get(RecoveryOperation, payload.smart_preview_id)
            operation.status = "APPLIED"
            operation.applied_at = utc_now()
            self.session.add(ScheduleChange(
                goal_id=goal.id, entity_type="PLAN_ACTIVATION", entity_id=activation.id,
                change_kind="SMART_CALENDAR_CONFIRMED", before_json={},
                after_json=operation.preview_json,
                reason="用户确认助手结合讨论生成的实际执行日期",
            ))
        self.session.commit()
        return self.get_today(goal_id)

    def get_today(self, goal_id: str, on_date: date | None = None) -> TodayWorkspace:
        goal, plan = self._goal_and_plan(goal_id)
        today = on_date or self._today_for(goal)
        if goal.status == "PAUSED" and goal.pause_until and goal.pause_until <= today:
            self._resume_paused_goal(goal, plan)
            self.session.commit()
            plan = self.session.get(PlanVersion, goal.active_plan_version_id)
            if plan is None:
                raise HTTPException(status_code=409, detail="Active plan is missing")
        if goal.status not in {"ACTIVE", "RECOVERY_REQUIRED", "PAUSED", "COMPLETED"}:
            raise HTTPException(status_code=409, detail="Plan has not started")
        activation = self._activation(plan.id)
        if activation is None:
            raise HTTPException(status_code=409, detail="Plan calendar is missing")
        schedule = self._active_schedule(plan, activation, today)
        next_date = self.session.scalar(
            select(func.min(PlanDaySchedule.scheduled_date)).where(
                PlanDaySchedule.activation_id == activation.id,
                (
                    PlanDaySchedule.scheduled_date > today
                    if schedule
                    else PlanDaySchedule.scheduled_date >= today
                ),
            )
        )
        day = self.session.get(PlanDay, schedule.plan_day_id) if schedule else None
        milestone = self.session.get(PlanMilestone, day.milestone_id) if day else None
        all_tasks = self._tasks(plan.id)
        day_tasks = [item for item in all_tasks if day and item.plan_day_id == day.id]
        remaining_tasks = [
            item for item in day_tasks if item.status not in {"COMPLETED", "CARRIED_OVER"}
        ]
        planned_minutes = sum(item.estimate_minutes for item in day_tasks)
        remaining_minutes = sum(item.estimate_minutes for item in remaining_tasks)
        if day is None:
            load_status = "REST_DAY"
        elif not remaining_tasks:
            load_status = "DONE"
        elif remaining_minutes > day.suggested_minutes:
            load_status = "HEAVY"
        else:
            load_status = "MANAGEABLE"
        stage_tasks = self._tasks_for_stage(all_tasks, day.stage_order if day else None)
        day_completed = bool(
            day
            and self.session.scalar(
                select(ProgressEvent.id).where(
                    ProgressEvent.goal_id == goal.id,
                    ProgressEvent.plan_day_id == day.id,
                    ProgressEvent.event_type == "DAY_COMPLETED",
                )
            )
        )
        feedbacks = (
            list(
                self.session.scalars(
                    select(Feedback)
                    .where(
                        Feedback.goal_id == goal.id,
                        Feedback.plan_day_id == day.id,
                    )
                    .order_by(Feedback.created_at)
                )
            )
            if day
            else []
        )
        feedback_ids = [item.id for item in feedbacks]
        adjustments = (
            list(
                self.session.scalars(
                    select(FeedbackAdjustment)
                    .where(FeedbackAdjustment.feedback_id.in_(feedback_ids))
                    .order_by(FeedbackAdjustment.created_at)
                )
            )
            if feedback_ids
            else []
        )
        day_log = (
            self.session.scalar(
                select(ExecutionDayLog).where(ExecutionDayLog.plan_day_id == day.id)
            )
            if day
            else None
        )
        carry_forward_tasks = self._carry_forward_candidates(
            plan, activation, today, all_tasks
        )
        all_days = list(
            self.session.scalars(
                select(PlanDay).where(PlanDay.plan_version_id == plan.id)
            )
        )
        all_schedules = list(
            self.session.scalars(
                select(PlanDaySchedule)
                .where(PlanDaySchedule.activation_id == activation.id)
                .order_by(PlanDaySchedule.scheduled_date)
            )
        )
        commitment_progress = self._commitment_progress(
            today,
            plan,
            all_schedules,
            all_days,
            all_tasks,
            list(
                self.session.scalars(
                    select(ProgressEvent).where(
                        ProgressEvent.goal_id == goal.id,
                        ProgressEvent.plan_version_id == plan.id,
                    )
                )
            ),
        )
        commitment_alerts = self._sync_reminder_events(
            goal,
            plan,
            self._commitment_alerts(
                commitment_progress,
                preference=self._schedule_preference(goal),
            ),
        )
        return TodayWorkspace(
            goal_id=goal.id,
            goal_title=goal.title,
            status=goal.status,
            today=today,
            schedule_type=plan.schedule_type,
            is_execution_day=day is not None,
            next_execution_date=next_date,
            milestone_title=milestone.title if milestone else None,
            scheduled_day=self._scheduled_read(day, schedule.scheduled_date) if day else None,
            day=self._day_read(day, day_tasks) if day else None,
            day_completed=day_completed,
            day_progress=self._progress(day_tasks),
            stage_progress=self._progress(stage_tasks),
            overall_progress=self._progress(all_tasks),
            exploration=self._exploration_state(plan, all_tasks),
            capacity=TodayCapacityRead(
                planned_minutes=planned_minutes,
                remaining_minutes=remaining_minutes,
                remaining_tasks=len(remaining_tasks),
                load_status=load_status,
            ),
            commitment_progress=commitment_progress,
            commitment_alerts=commitment_alerts,
            inactivity=self._inactivity(
                goal,
                plan,
                activation,
                all_schedules,
                {
                    plan_day.id: [task for task in all_tasks if task.plan_day_id == plan_day.id]
                    for plan_day in all_days
                },
            ),
            feedbacks=[self._feedback_read(item) for item in feedbacks],
            adjustments=[self._adjustment_read(item) for item in adjustments],
            day_log=self._day_log_read(day_log) if day_log else None,
            carry_forward_tasks=carry_forward_tasks,
        )

    def get_candidate_pools(self, goal_id: str) -> list[CandidatePoolRead]:
        """Expose selectable work-item groups before execution materialization."""
        goal, plan = self._goal_and_plan(goal_id)
        groups = list(
            self.session.scalars(
                select(PlanWorkItemGroup)
                .where(PlanWorkItemGroup.plan_version_id == plan.id)
                .order_by(PlanWorkItemGroup.group_key)
            )
        )
        items = list(
            self.session.scalars(
                select(PlanWorkItem)
                .where(PlanWorkItem.plan_version_id == plan.id)
                .order_by(PlanWorkItem.priority, PlanWorkItem.item_key)
            )
        )
        by_group: dict[str, list[PlanWorkItem]] = {}
        for item in items:
            if item.group_id:
                by_group.setdefault(item.group_id, []).append(item)
        active_tasks = list(
            self.session.scalars(select(PlanTask).where(PlanTask.plan_version_id == plan.id))
        )
        has_materialized_windows = False
        materialized_selected_ids = {
            task.source_work_item_id
            for task in active_tasks
            if task.source_work_item_id
            and task.status not in {"CANCELLED", "CARRIED_OVER"}
        }
        activation = self._activation(plan.id)
        windows = list(
            self.session.scalars(
                select(WorkItemWindow).where(WorkItemWindow.activation_id == activation.id)
            )
        ) if activation is not None else []
        if activation is not None:
            has_materialized_windows = bool(
                self.session.scalar(
                    select(func.count(WorkItemWindow.id)).where(
                        WorkItemWindow.activation_id == activation.id
                    )
                )
            )
            materialized_selected_ids.update(
                self.session.scalars(
                    select(WorkItemWindow.work_item_id).where(
                        WorkItemWindow.activation_id == activation.id,
                        WorkItemWindow.status.not_in({"CANCELLED", "EXPIRED"}),
                    )
                )
            )
        pools: list[CandidatePoolRead] = []
        for group in groups:
            candidates = by_group.get(group.id, [])
            default_selected = sorted(candidates, key=lambda item: (item.priority, item.item_key))[
                : group.minimum_selected
            ]
            candidate_ids = {item.id for item in candidates}
            selected_ids = (
                sorted(materialized_selected_ids.intersection(candidate_ids))
                if has_materialized_windows
                else [item.id for item in default_selected]
            )
            period_rows: list[CandidatePoolPeriodRead] = []
            group_windows = [
                window for window in windows
                if window.work_item_id in candidate_ids
                and window.status != "EXPIRED"
            ]
            grouped_windows: dict[str, list[WorkItemWindow]] = {}
            for window in group_windows:
                grouped_windows.setdefault(window.period_key, []).append(window)
            for period_key, period_windows in sorted(grouped_windows.items()):
                period_selected = sorted({
                    window.work_item_id for window in period_windows
                    if window.status != "CANCELLED"
                })
                completed_period = 0
                for window in period_windows:
                    live_tasks = [
                        task for task in active_tasks
                        if task.work_item_window_id == window.id
                        and task.status not in {"CANCELLED", "CARRIED_OVER"}
                    ]
                    if (
                        window.status != "CANCELLED"
                        and live_tasks
                        and all(task.status == "COMPLETED" for task in live_tasks)
                    ):
                        completed_period += 1
                period_rows.append(
                    CandidatePoolPeriodRead(
                        period_key=period_key,
                        eligible_from=min(window.eligible_from for window in period_windows),
                        eligible_to=max(window.eligible_to for window in period_windows),
                        selected_item_ids=period_selected,
                        completed_selected=completed_period,
                        remaining_required=max(0, group.minimum_selected - completed_period),
                        period_status=(
                            "COMPLETE" if completed_period >= group.minimum_selected
                            else "IN_PROGRESS"
                        ),
                    )
                )
            completed_selected = sum(
                period.completed_selected for period in period_rows
            ) if period_rows else 0
            current_period = next(
                (period for period in period_rows
                 if period.eligible_from <= self._today_for(goal) <= period.eligible_to),
                next((period for period in period_rows if period.period_status != "COMPLETE"), None),
            )
            if current_period is not None:
                selected_ids = current_period.selected_item_ids
                completed_selected = current_period.completed_selected
            period_status = (
                "COMPLETE"
                if completed_selected >= group.minimum_selected
                else "IN_PROGRESS"
                if has_materialized_windows
                else "NOT_STARTED"
            )
            pools.append(
                CandidatePoolRead(
                    id=group.id,
                    group_key=group.group_key,
                    title=group.title,
                    selection_mode=group.selection_mode,
                    minimum_selected=group.minimum_selected,
                    maximum_selected=group.maximum_selected,
                    selected_item_ids=selected_ids,
                    candidates=[
                        CandidatePoolItemRead(
                            id=item.id,
                            item_key=item.item_key,
                            title=item.title,
                            description=item.description,
                            estimate_minutes=item.estimate_minutes,
                            priority=item.priority,
                            status=item.status,
                        )
                        for item in candidates
                    ],
                    completed_selected=completed_selected,
                    remaining_required=max(0, group.minimum_selected - completed_selected),
                    period_status=period_status,
                    periods=period_rows,
                )
            )
        return pools

    def _candidate_replacement_items(
        self, plan: PlanVersion, group_id: str, period_key: str | None = None
    ) -> tuple[PlanWorkItemGroup, list[PlanWorkItem], list[PlanWorkItem]]:
        group = self.session.scalar(
            select(PlanWorkItemGroup).where(
                PlanWorkItemGroup.id == group_id,
                PlanWorkItemGroup.plan_version_id == plan.id,
            )
        )
        if group is None:
            raise HTTPException(status_code=404, detail="候选任务组不存在")
        candidates = list(
            self.session.scalars(
                select(PlanWorkItem)
                .where(
                    PlanWorkItem.plan_version_id == plan.id,
                    PlanWorkItem.group_id == group.id,
                )
                .order_by(PlanWorkItem.priority, PlanWorkItem.item_key)
            )
        )
        activation = self._activation(plan.id)
        active_item_ids: set[str] = set()
        if activation is not None:
            active_item_ids.update(
                self.session.scalars(
                    select(WorkItemWindow.work_item_id).where(
                        WorkItemWindow.activation_id == activation.id,
                        WorkItemWindow.status.not_in({"CANCELLED", "EXPIRED"}),
                        *([WorkItemWindow.period_key == period_key] if period_key else []),
                    )
                )
            )
        active_item_ids.update(
            task.source_work_item_id
            for task in self.session.scalars(
                select(PlanTask).where(PlanTask.plan_version_id == plan.id)
            )
            if task.source_work_item_id
            and task.status not in {"CANCELLED", "CARRIED_OVER"}
            and (
                not period_key
                or not task.work_item_window_id
                or (
                    self.session.get(WorkItemWindow, task.work_item_window_id) is not None
                    and self.session.get(WorkItemWindow, task.work_item_window_id).period_key == period_key
                )
            )
        )
        selected = [item for item in candidates if item.id in active_item_ids]
        available = [item for item in candidates if item.id not in active_item_ids]
        return group, selected, available

    def _candidate_target_capacity(
        self,
        plan: PlanVersion,
        activation: PlanActivation,
        target_date: date,
        replaced_item_ids: set[str] | None = None,
    ) -> tuple[int, int]:
        schedule = self.session.scalar(
            select(PlanDaySchedule).where(
                PlanDaySchedule.activation_id == activation.id,
                PlanDaySchedule.scheduled_date == target_date,
            )
        )
        capacity = calendar_capacity(plan, activation.requested_start_date, target_date)
        if schedule is None:
            return 0, max(0, int(capacity["daily_minutes"] if capacity["daily_minutes"] is not None
                                 else plan.weekly_minutes / max(1, plan.weekly_execution_days)))
        day = self.session.get(PlanDay, schedule.plan_day_id)
        planned_tasks = list(
            self.session.scalars(
                select(PlanTask).where(
                    PlanTask.plan_day_id == schedule.plan_day_id,
                    PlanTask.status.not_in({"COMPLETED", "CANCELLED", "CARRIED_OVER"}),
                )
            )
        )
        planned = sum(
            task.estimate_minutes
            for task in planned_tasks
            if (not replaced_item_ids or task.source_work_item_id not in replaced_item_ids)
            and self._counts_toward_phase_date(plan, task, target_date)
        )
        phased = any(item.get("available_days_per_week") for item in plan.temporal_segments_json or [])
        return planned, int(capacity["daily_minutes"] if phased and capacity["daily_minutes"] is not None
                            else day.suggested_minutes if day else 0)

    def _counts_toward_phase_date(self, plan, task, target_date) -> bool:
        """A window's suggested checkpoint date is not a daily booking."""
        if plan.schedule_type != "OUTCOME" or not any(
            item.get("available_days_per_week") for item in plan.temporal_segments_json or []
        ):
            return True
        if task.scheduling_policy not in {"FLOATING", "DATE_WINDOW", "CADENCE", "QUOTA"}:
            return True
        if task.status == "IN_PROGRESS" or (task.scheduling_policy_json or {}).get("confirmed_date") == target_date.isoformat():
            return True
        return self.session.scalar(select(TaskTimeBlock.id).where(
            TaskTimeBlock.task_id == task.id,
            TaskTimeBlock.scheduled_date == target_date,
            TaskTimeBlock.status.in_({"PLANNED", "REQUIRES_REVIEW"}),
        ).limit(1)) is not None

    def _work_item_dependency_preview(
        self, plan: PlanVersion, activation: PlanActivation, item: PlanWorkItem,
        target_date: date, today: date,
    ) -> tuple[list[str], list]:
        blockers: list[str] = []
        edges = list(
            self.session.scalars(
                select(WorkItemDependency)
                .where(
                    WorkItemDependency.plan_version_id == plan.id,
                    WorkItemDependency.successor_work_item_id == item.id,
                )
                .order_by(WorkItemDependency.id)
            )
        )
        tasks = self._tasks(plan.id)
        schedules = list(
            self.session.scalars(
                select(PlanDaySchedule)
                .where(
                    PlanDaySchedule.activation_id == activation.id,
                )
                .order_by(PlanDaySchedule.id)
            )
        )
        dates_by_day = {schedule.plan_day_id: schedule.scheduled_date for schedule in schedules}
        dependency_state = []
        work_items = {row.id: row for row in self.session.scalars(select(PlanWorkItem).where(
            PlanWorkItem.plan_version_id == plan.id))}
        for edge in edges:
            predecessors = [
                task
                for task in tasks
                if self._task_represents_work_item(task, work_items.get(edge.predecessor_work_item_id))
                and task.status not in {"CANCELLED", "CARRIED_OVER"}
            ]
            dependency_state.append(
                [
                    edge.id,
                    edge.minimum_lag_days,
                    edge.maximum_lag_days,
                    [
                        [task.id, task.status, str(dates_by_day.get(task.plan_day_id))]
                        for task in predecessors
                    ],
                ]
            )
            if not predecessors:
                blockers.append("前置任务尚未生成，请先处理候选选择或触发前置任务")
            for predecessor in predecessors:
                predecessor_date = dates_by_day.get(predecessor.plan_day_id)
                if predecessor_date is None:
                    blockers.append(f"前置任务“{predecessor.title}”尚未安排日期")
                    continue
                if target_date < predecessor_date + timedelta(days=edge.minimum_lag_days):
                    blockers.append(f"目标日期早于前置任务“{predecessor.title}”及其最小间隔")
                if (
                    edge.maximum_lag_days is not None
                    and target_date
                    > predecessor_date + timedelta(days=edge.maximum_lag_days)
                ):
                    blockers.append(f"目标日期超过前置任务“{predecessor.title}”允许的最大间隔")
                if target_date == today and predecessor.status != "COMPLETED":
                    blockers.append(f"需先完成：{predecessor.title}")
        successor_edges = list(
            self.session.scalars(
                select(WorkItemDependency)
                .where(
                    WorkItemDependency.plan_version_id == plan.id,
                    WorkItemDependency.predecessor_work_item_id == item.id,
                )
                .order_by(WorkItemDependency.id)
            )
        )
        for edge in successor_edges:
            successors = [
                task
                for task in tasks
                if task.source_work_item_id == edge.successor_work_item_id
                and task.status not in {"CANCELLED", "CARRIED_OVER"}
            ]
            dependency_state.append(
                [
                    "successor",
                    edge.id,
                    edge.minimum_lag_days,
                    edge.maximum_lag_days,
                    [
                        [task.id, task.status, str(dates_by_day.get(task.plan_day_id))]
                        for task in successors
                    ],
                ]
            )
            for successor in successors:
                successor_date = dates_by_day.get(successor.plan_day_id)
                if successor.status == "COMPLETED":
                    blockers.append(f"后续任务“{successor.title}”已完成，请通过讨论处理依赖关系")
                elif successor_date is None:
                    blockers.append(f"后续任务“{successor.title}”尚未安排日期")
                elif successor_date < target_date + timedelta(days=edge.minimum_lag_days):
                    blockers.append(f"目标日期晚于后续任务“{successor.title}”允许的前置日期")
                elif (
                    edge.maximum_lag_days is not None
                    and successor_date > target_date + timedelta(days=edge.maximum_lag_days)
                ):
                    blockers.append(f"与后续任务“{successor.title}”的间隔超过上限")
        return blockers, dependency_state

    def _build_candidate_replacement_preview(
        self,
        goal: Goal,
        plan: PlanVersion,
        payload: CandidateReplacementRequest,
    ) -> tuple[CandidateReplacementPreviewRead, dict, PlanWorkItemGroup, PlanWorkItem, list[PlanWorkItem], PlanActivation]:
        activation = self._activation(plan.id)
        if activation is None:
            raise HTTPException(status_code=409, detail="Plan calendar is missing")
        group, selected, available = self._candidate_replacement_items(
            plan, payload.group_id, payload.period_key
        )
        candidate = next((item for item in available if item.id == payload.candidate_item_id), None)
        blockers: list[str] = []
        warnings: list[str] = []
        if goal.status != "ACTIVE":
            blockers.append("请先恢复规划")
        if candidate is None:
            blockers.append("该候选已经被选中，或不属于当前候选组")
            # Keep a stable response shape for the preview endpoint.
            candidate = self.session.get(PlanWorkItem, payload.candidate_item_id)
        if candidate is None:
            raise HTTPException(status_code=404, detail="候选任务不存在")
        if candidate.fulfillment_mode != "ONCE":
            blockers.append("当前补选入口只支持一次性候选任务")
        active_selection_ids = sorted(item.id for item in selected)
        selection_tasks = list(self.session.scalars(
            select(PlanTask).where(
                PlanTask.plan_version_id == plan.id,
                PlanTask.source_work_item_id.in_(active_selection_ids),
            )
        ))
        if payload.period_key:
            selection_tasks = [
                task for task in selection_tasks
                if task.work_item_window_id
                and (window := self.session.get(WorkItemWindow, task.work_item_window_id)) is not None
                and window.period_key == payload.period_key
            ]
        protected_ids = {
            task.source_work_item_id for task in selection_tasks
            if task.status in {"COMPLETED", "IN_PROGRESS"}
        }
        if len(selected) < group.minimum_selected:
            selected = []
            warnings.append("补选会新增一个候选安排，已有候选及履约记录会保留")
        else:
            replaceable = [item for item in selected if item.id not in protected_ids]
            selected = replaceable[-1:]
            if not selected:
                blockers.append("当前已选候选均已开始或完成，不能替换其履约记录")
        if payload.target_date < self._today_for(goal):
            blockers.append("目标日期不能早于今天")
        if payload.target_date > activation.projected_end_date:
            blockers.append("目标日期超出规划执行范围")
        constraints = candidate.constraints_json or {}
        temporal = constraints.get("temporal") or {}
        eligible_from, eligible_to = WorkItemMaterializer._date_range(plan, activation, temporal)
        if not eligible_from <= payload.target_date <= eligible_to:
            blockers.append(f"该候选必须安排在 {eligible_from} 至 {eligible_to} 的允许窗口内")
        allowed_weekdays = temporal.get("allowed_weekdays") or []
        if allowed_weekdays and payload.target_date.weekday() not in allowed_weekdays:
            blockers.append("目标日期不符合该候选允许的星期")
        excluded_dates = set(activation.excluded_dates_json or []) | set(temporal.get("excluded_dates") or [])
        if payload.target_date.isoformat() in {str(value) for value in excluded_dates}:
            blockers.append("目标日期被明确排除，不能安排该候选")
        if temporal.get("exact_date"):
            exact_date = date.fromisoformat(str(temporal["exact_date"]))
            if payload.target_date != exact_date:
                blockers.append(f"该候选只能安排在 {exact_date}")
        start_week = temporal.get("start_week")
        end_week = temporal.get("end_week")
        target_week = max(1, plan_week_index(plan, activation.requested_start_date, payload.target_date))
        if start_week and target_week < int(start_week):
            blockers.append(f"该候选最早从第 {start_week} 周开始")
        if end_week and target_week > int(end_week):
            blockers.append(f"该候选必须在第 {end_week} 周结束前完成")
        dependency_blockers, dependency_state = self._work_item_dependency_preview(
            plan, activation, candidate, payload.target_date, self._today_for(goal)
        )
        blockers.extend(dependency_blockers)
        replaced_ids = {item.id for item in selected}
        replacement_edges = list(self.session.scalars(
            select(WorkItemDependency).where(
                WorkItemDependency.plan_version_id == plan.id,
                WorkItemDependency.predecessor_work_item_id.in_(replaced_ids),
            ).order_by(WorkItemDependency.id)
        ))
        all_tasks = self._tasks(plan.id)
        dependent_states = []
        for edge in replacement_edges:
            dependent_tasks = [
                task for task in all_tasks
                if task.source_work_item_id == edge.successor_work_item_id
                and task.status not in {"CANCELLED", "CARRIED_OVER"}
            ]
            dependent_states.append([edge.id, sorted(task.id for task in dependent_tasks)])
            if dependent_tasks:
                blockers.append("当前候选仍是其他执行任务的前置条件，请通过规划讨论调整依赖后再替换")
        planned_minutes, capacity_minutes = self._candidate_target_capacity(
            plan, activation, payload.target_date, {item.id for item in selected}
        )
        over_capacity = max(0, planned_minutes + candidate.estimate_minutes - capacity_minutes)
        if over_capacity:
            if any(item.get("available_days_per_week") for item in plan.temporal_segments_json or []):
                blockers.append(f"目标日期超过已确认阶段容量 {over_capacity} 分钟，请改选日期或讨论增加可用时间")
            else:
                warnings.append(f"目标日期预计超出建议容量 {over_capacity} 分钟，可继续确认或改选其他日期")
        if selected:
            warnings.append(f"确认后会停用 {len(selected)} 个当前候选，并保留其历史履约记录")
        base = {
            "plan_id": plan.id,
            "plan_revision": plan.revision,
            "group_id": group.id,
            "candidate_item_id": candidate.id,
            "selected_item_ids": sorted(item.id for item in selected),
            "active_selection_ids": active_selection_ids,
            "selection_task_states": sorted(
                [task.id, task.status, task.plan_day_id] for task in selection_tasks
            ),
            "target_date": payload.target_date.isoformat(),
            "reason": payload.reason,
            "goal_status": goal.status,
            "candidate_updated_at": candidate.updated_at.isoformat(),
            "activation_projected_end": activation.projected_end_date.isoformat(),
            "activation_excluded_dates": sorted(str(value) for value in activation.excluded_dates_json or []),
            "candidate_constraints": constraints,
            "planned_minutes": planned_minutes,
            "capacity_minutes": capacity_minutes,
            "dependencies": dependency_state,
            "replacement_dependents": dependent_states,
        }
        fingerprint = hashlib.sha256(
            json.dumps(base, ensure_ascii=False, sort_keys=True).encode()
        ).hexdigest()
        result = CandidateReplacementPreviewRead(
            id="",
            status="PREVIEW",
            group_id=group.id,
            group_title=group.title,
            candidate_item_id=candidate.id,
            candidate_title=candidate.title,
            replaced_item_ids=[item.id for item in selected],
            target_date=payload.target_date,
            planned_minutes=planned_minutes,
            candidate_minutes=candidate.estimate_minutes,
            capacity_minutes=capacity_minutes,
            over_capacity_minutes=over_capacity,
            blockers=sorted(set(blockers)),
            warnings=list(dict.fromkeys(warnings)),
            can_apply=not blockers,
            fingerprint=fingerprint,
        )
        return result, base, group, candidate, selected, activation

    @serialize_recovery_transaction
    def preview_candidate_replacement(
        self, goal_id: str, payload: CandidateReplacementRequest
    ) -> CandidateReplacementPreviewRead:
        goal, plan = self._goal_and_plan(goal_id)
        result, base, _group, _candidate, _selected, _activation = self._build_candidate_replacement_preview(
            goal, plan, payload
        )
        key = "candidate-replacement:" + hashlib.sha256(
            json.dumps({"goal_id": goal.id, **base}, ensure_ascii=False, sort_keys=True).encode()
        ).hexdigest()
        operation = self.session.scalar(
            select(RecoveryOperation).where(RecoveryOperation.idempotency_key == key)
        )
        if operation is None:
            operation = RecoveryOperation(
                goal_id=goal.id,
                source_plan_version_id=plan.id,
                mode="CANDIDATE_REPLACEMENT",
                status="PREVIEW",
                idempotency_key=key,
                preview_json={
                    "request": payload.model_dump(mode="json"),
                    "base": base,
                    "result": result.model_dump(mode="json"),
                },
            )
            self.session.add(operation)
            self.session.flush()
        elif operation.status == "PREVIEW":
            operation.preview_json = {
                "request": payload.model_dump(mode="json"),
                "base": base,
                "result": result.model_dump(mode="json"),
            }
        result.id = operation.id
        operation.preview_json = {**operation.preview_json, "result": result.model_dump(mode="json")}
        self.session.commit()
        return result

    @serialize_recovery_transaction
    def apply_candidate_replacement(
        self, goal_id: str, preview_id: str
    ) -> TodayWorkspace:
        goal, plan = self._goal_and_plan(goal_id)
        operation = self.session.get(RecoveryOperation, preview_id)
        if operation is None or operation.goal_id != goal.id or operation.mode != "CANDIDATE_REPLACEMENT":
            raise HTTPException(status_code=404, detail="Candidate replacement preview not found")
        request = CandidateReplacementRequest.model_validate(operation.preview_json.get("request") or {})
        if operation.status == "APPLIED":
            return self.get_today(goal_id, request.target_date)
        current, base, group, candidate, selected, activation = self._build_candidate_replacement_preview(
            goal, plan, request
        )
        stored = operation.preview_json.get("base") or {}
        if current.fingerprint != str(operation.preview_json.get("result", {}).get("fingerprint") or "") or base != stored:
            operation.status = "STALE"
            self.session.commit()
            raise HTTPException(status_code=409, detail="候选替换预览已过期，请重新生成")
        if not current.can_apply:
            raise HTTPException(status_code=409, detail="当前候选替换仍有阻塞")
        replacement_period_key = request.period_key
        if replacement_period_key is None:
            replacement_period_key = self.session.scalar(
                select(WorkItemWindow.period_key)
                .where(
                    WorkItemWindow.activation_id == activation.id,
                    WorkItemWindow.work_item_id.in_(
                        select(PlanWorkItem.id).where(PlanWorkItem.group_id == group.id)
                    ),
                    WorkItemWindow.eligible_from <= request.target_date,
                    WorkItemWindow.eligible_to >= request.target_date,
                )
                .order_by(WorkItemWindow.period_key)
            ) or "once"
        for item in selected:
            for task in self.session.scalars(
                select(PlanTask).where(
                    PlanTask.plan_version_id == plan.id,
                    PlanTask.source_work_item_id == item.id,
                    PlanTask.status.not_in({"COMPLETED", "CANCELLED", "CARRIED_OVER"}),
                    *(
                        [
                            PlanTask.work_item_window_id.in_(
                                select(WorkItemWindow.id).where(
                                    WorkItemWindow.period_key == request.period_key
                                )
                            )
                        ]
                        if request.period_key
                        else []
                    ),
                )
            ):
                task.status = "CANCELLED"
                self.session.add(
                    ProgressEvent(
                        goal_id=goal.id,
                        plan_version_id=plan.id,
                        plan_day_id=task.plan_day_id,
                        task_id=task.id,
                        event_type="CANDIDATE_REPLACED",
                        metadata_json={
                            "group_id": group.id,
                            "replacement_item_id": candidate.id,
                            "reason": request.reason,
                        },
                    )
                )
            for window in self.session.scalars(
                select(WorkItemWindow).where(
                    WorkItemWindow.activation_id == activation.id,
                    WorkItemWindow.work_item_id == item.id,
                    WorkItemWindow.status.not_in({"CANCELLED", "EXPIRED"}),
                    *([WorkItemWindow.period_key == replacement_period_key] if request.period_key else []),
                )
            ):
                window.status = "CANCELLED"
                window.materialization_revision += 1
                window.resolution_json = {
                    **(window.resolution_json or {}),
                    "replacement_item_id": candidate.id,
                    "replacement_reason": request.reason,
                }
            item.status = "DRAFT"
        target_schedule = self.session.scalar(
            select(PlanDaySchedule).where(
                PlanDaySchedule.activation_id == activation.id,
                PlanDaySchedule.scheduled_date == request.target_date,
            )
        )
        destination = self.session.get(PlanDay, target_schedule.plan_day_id) if target_schedule else None
        if destination is None:
            milestone = self.session.scalar(
                select(PlanMilestone).where(
                    PlanMilestone.plan_version_id == plan.id,
                    PlanMilestone.stage_order == (candidate.stage_order or 1),
                ).order_by(PlanMilestone.start_week, PlanMilestone.id)
            )
            if milestone is None:
                raise HTTPException(status_code=409, detail="缺少候选所属阶段")
            week_index = max(1, plan_week_index(plan, activation.requested_start_date, request.target_date))
            max_index = self.session.scalar(
                select(func.coalesce(func.max(PlanDay.day_index), 0)).where(
                    PlanDay.plan_version_id == plan.id,
                    PlanDay.week_index == week_index,
                )
            ) or 0
            destination = PlanDay(
                plan_version_id=plan.id,
                milestone_id=milestone.id,
                stage_order=candidate.stage_order or milestone.stage_order,
                week_index=week_index,
                day_index=int(max_index) + 1,
                primary_goal=f"候选替换：{candidate.title}",
                suggested_minutes=candidate.estimate_minutes,
                evidence_json=candidate.evidence_json,
                do_not_json=[],
            )
            self.session.add(destination)
            self.session.flush()
            self.session.add(PlanDaySchedule(
                activation_id=activation.id,
                plan_day_id=destination.id,
                scheduled_date=request.target_date,
            ))
        predecessor_ids = list(self.session.scalars(
            select(WorkItemDependency.predecessor_work_item_id).where(
                WorkItemDependency.plan_version_id == plan.id,
                WorkItemDependency.successor_work_item_id == candidate.id,
            )
        ))
        depends_on = list(self.session.scalars(
            select(PlanTask.id).where(
                PlanTask.plan_version_id == plan.id,
                PlanTask.source_work_item_id.in_(predecessor_ids),
                PlanTask.status.not_in({"CANCELLED", "CARRIED_OVER"}),
            )
        )) if predecessor_ids else []
        window = WorkItemWindow(
            activation_id=activation.id,
            work_item_id=candidate.id,
            period_key=replacement_period_key,
            eligible_from=request.target_date,
            eligible_to=request.target_date,
            required_value=1,
            status="OPEN",
            resolution_json={
                "selected_dates": [request.target_date.isoformat()],
                "replacement_reason": request.reason,
                "activation_mode": "USER_CONFIRMED",
            },
            materialization_revision=1,
            opened_at=utc_now(),
        )
        self.session.add(window)
        self.session.flush()
        task = PlanTask(
            plan_version_id=plan.id,
            plan_day_id=destination.id,
            source_work_item_id=candidate.id,
            work_item_window_id=window.id,
            occurrence_index=1,
            materialization_key=f"work-item:{activation.id}:{candidate.item_key}:candidate-replacement:{operation.id}",
            materialized_at=utc_now(),
            origin_stage_order=candidate.stage_order or destination.stage_order,
            order_in_day=self._next_task_order(destination.id),
            title=candidate.title,
            description=candidate.description,
            estimate_minutes=candidate.estimate_minutes,
            priority=candidate.priority,
            task_kind=candidate.task_kind,
            is_required=candidate.is_required,
            evidence_json=candidate.evidence_json,
            depends_on_json=depends_on,
            satisfies_commitments_json=candidate.commitment_refs_json,
            ai_schedule_requested=False,
            is_splittable=candidate.is_splittable,
            scheduling_policy="DATE_WINDOW",
            scheduling_policy_json={
                "eligible_from": request.target_date.isoformat(),
                "eligible_to": request.target_date.isoformat(),
                "work_item_key": candidate.item_key,
                "replacement": True,
            },
            status="NOT_STARTED",
        )
        self.session.add(task)
        candidate.status = "ACTIVE"
        self.session.add(ProgressEvent(
            goal_id=goal.id,
            plan_version_id=plan.id,
            plan_day_id=destination.id,
            task_id=task.id,
            event_type="CANDIDATE_REPLACED",
            metadata_json={
                "group_id": group.id,
                "replaced_item_ids": [item.id for item in selected],
                "replacement_item_id": candidate.id,
                "target_date": request.target_date.isoformat(),
                "reason": request.reason,
            },
        ))
        self.session.add(ScheduleChange(
            goal_id=goal.id,
            entity_type="PLAN_TASK",
            entity_id=task.id,
            change_kind="CANDIDATE_REPLACED",
            before_json={"selected_item_ids": [item.id for item in selected]},
            after_json={"selected_item_id": candidate.id, "target_date": request.target_date.isoformat()},
            reason=request.reason,
        ))
        self._refresh_day_log(goal, plan, destination)
        goal.last_active_at = utc_now()
        operation.status = "APPLIED"
        operation.applied_at = utc_now()
        operation.result_plan_version_id = plan.id
        self.session.commit()
        return self.get_today(goal_id, request.target_date)

    def _triggered_progress(self, plan_id: str, item: PlanWorkItem) -> int:
        """Aggregate completed count/minutes for a triggered work item."""
        tasks = list(self.session.scalars(select(PlanTask).where(
            PlanTask.plan_version_id == plan_id,
            PlanTask.source_work_item_id == item.id,
        )))
        events = list(self.session.scalars(select(ProgressEvent).where(
            ProgressEvent.plan_version_id == plan_id,
            ProgressEvent.task_id.in_([task.id for task in tasks]) if tasks else text("1=0"),
        )))
        if item.target_unit == "MINUTES" or item.fulfillment_mode == "DURATION":
            partial_by_task: dict[str, int] = {}
            for event in events:
                if (
                    event.event_type == "PARTIAL_COMPLETION"
                    and (event.metadata_json or {}).get("unit", "MINUTES") == "MINUTES"
                ):
                    partial_by_task[event.task_id] = partial_by_task.get(event.task_id, 0) + int(
                        (event.metadata_json or {}).get("amount") or 0
                    )
            # A completion event closes the same task that may already have
            # accumulated partial minutes. Count that task once, using the
            # larger of the recorded partial amount and final estimate.
            return sum(
                max(
                    task.estimate_minutes if task.status == "COMPLETED" else 0,
                    partial_by_task.get(task.id, 0),
                )
                for task in tasks
            )
        groups: dict[tuple, list[PlanTask]] = {}
        for task in tasks:
            key = (task.work_item_window_id, task.occurrence_index or task.plan_day_id)
            groups.setdefault(key, []).append(task)
        return sum(all(task.status == 'COMPLETED' for task in parts) for parts in groups.values())

    @staticmethod
    def _task_represents_work_item(task, item) -> bool:
        return bool(item and (task.source_work_item_id == item.id or item.item_key in
                             ((task.scheduling_policy_json or {}).get('work_item_keys') or [])))

    def _assert_hybrid_predecessors_completed(self, plan, task) -> None:
        reason = self._hybrid_predecessor_blocked_reason(plan, task)
        if reason:
            raise HTTPException(409, reason)

    def _hybrid_predecessor_blocked_reason(self, plan, task) -> str | None:
        if plan.execution_model_version != ExecutionModelVersion.HYBRID_WORK_ITEM or not task.source_work_item_id:
            return
        for edge in self.session.scalars(select(WorkItemDependency).where(
            WorkItemDependency.plan_version_id == plan.id,
            WorkItemDependency.successor_work_item_id == task.source_work_item_id,
        )):
            item = self.session.get(PlanWorkItem, edge.predecessor_work_item_id)
            predecessors = [candidate for candidate in self._tasks(plan.id)
                            if candidate.id != task.id
                            and candidate.status not in {'CANCELLED', 'CARRIED_OVER'}
                            and self._task_represents_work_item(candidate, item)]
            if (not predecessors and self._task_represents_work_item(task, item)
                    and item.binding_mode != "TRIGGERED" and edge.dependency_type == "FINISH_TO_START"
                    and not edge.minimum_lag_days
                    and not edge.condition_json and not task.is_splittable
                    and len((task.scheduling_policy_json or {}).get("stage_contributions") or []) > 1):
                # An explicitly compiled atomic action may check readiness,
                # execute, and archive internally. It is its own zero-lag
                # predecessor, not a separate missing task. External tasks
                # and triggered gates still require completion first.
                continue
            if not predecessors or any(candidate.status != 'COMPLETED' for candidate in predecessors):
                return '需先完成前置工作项：' + (item.title if item else '待确认任务')
        return None

    def get_triggered_tasks(self, goal_id: str) -> list[TriggeredTaskRead]:
        """List trigger windows that still need explicit user confirmation."""
        goal, plan = self._goal_and_plan(goal_id)
        activation = self._activation(plan.id)
        if activation is None:
            return []
        windows = list(
            self.session.scalars(
                select(WorkItemWindow)
                .where(
                    WorkItemWindow.activation_id == activation.id,
                    WorkItemWindow.period_key.like("trigger:%"),
                    WorkItemWindow.status == "PENDING",
                )
                .order_by(WorkItemWindow.eligible_from, WorkItemWindow.id)
            )
        )
        items = {
            item.id: item
            for item in self.session.scalars(
                select(PlanWorkItem).where(PlanWorkItem.plan_version_id == plan.id)
            )
        }
        result: list[TriggeredTaskRead] = []
        for window in windows:
            item = items.get(window.work_item_id)
            if item is None:
                continue
            trigger = (item.constraints_json or {}).get("trigger") or {}
            completed_value = self._triggered_progress(plan.id, item)
            remaining_value = max(0, item.target_value - completed_value)
            unit_label = "分钟" if item.target_unit == "MINUTES" else "次"
            blocked_reason = (
                "请先恢复规划"
                if goal.status != "ACTIVE"
                else "触发窗口已经结束，请先调整窗口"
                if window.eligible_to < self._today_for(goal)
                else "尚未进入触发窗口"
                if window.eligible_from > self._today_for(goal)
                else None
            )
            if remaining_value <= 0:
                blocked_reason = "该触发任务的剩余要求已满足"
            if blocked_reason is None:
                for edge in self.session.scalars(select(WorkItemDependency).where(
                    WorkItemDependency.plan_version_id == plan.id,
                    WorkItemDependency.successor_work_item_id == item.id,
                )):
                    predecessors = [task for task in self._tasks(plan.id)
                                    if self._task_represents_work_item(
                                        task, items.get(edge.predecessor_work_item_id))]
                    if not predecessors or any(task.status != 'COMPLETED' for task in predecessors):
                        blocked_reason = '需要先完成前置工作项'
                        break
            previous_window_id = (window.resolution_json or {}).get("previous_window_id")
            if blocked_reason is None and previous_window_id:
                previous_task = self.session.scalar(
                    select(PlanTask).where(
                        PlanTask.plan_version_id == plan.id,
                        PlanTask.source_work_item_id == item.id,
                        PlanTask.work_item_window_id == previous_window_id,
                    )
                )
                previous_window = self.session.get(WorkItemWindow, previous_window_id)
                if previous_window is None or previous_task is None or previous_task.status != "COMPLETED":
                    blocked_reason = "请先完成上一项触发任务"
            explanation = (
                f"已完成 {completed_value}/{item.target_value}{unit_label}，"
                f"还需 {remaining_value}{unit_label}；每次满足条件后确认一项。"
            )
            result.append(
                TriggeredTaskRead(
                    window_id=window.id,
                    work_item_id=item.id,
                    title=item.title,
                    description=item.description,
                    estimate_minutes=item.estimate_minutes,
                    condition=str(trigger.get("description") or "满足触发条件后执行"),
                    eligible_from=window.eligible_from,
                    eligible_to=window.eligible_to,
                    status=window.status,
                    can_activate=blocked_reason is None,
                    blocked_reason=blocked_reason,
                    fulfillment_mode=item.fulfillment_mode,
                    target_value=item.target_value,
                    target_unit=item.target_unit,
                    completed_value=completed_value,
                    remaining_value=remaining_value,
                    explanation=explanation,
                )
            )
        return result

    def _build_triggered_activation_preview(
        self,
        goal: Goal,
        plan: PlanVersion,
        payload: TriggeredTaskActivationRequest,
    ) -> tuple[
        TriggeredTaskActivationPreviewRead, dict, WorkItemWindow, PlanWorkItem, PlanActivation
    ]:
        today = self._today_for(goal)
        activation = self._activation(plan.id)
        if activation is None:
            raise HTTPException(status_code=409, detail="Plan calendar is missing")
        window = self.session.get(WorkItemWindow, payload.window_id)
        if window is None or window.activation_id != activation.id:
            raise HTTPException(status_code=404, detail="Triggered task window not found")
        item = self.session.get(PlanWorkItem, window.work_item_id)
        if item is None or item.plan_version_id != plan.id:
            raise HTTPException(status_code=404, detail="Triggered work item not found")
        trigger = (item.constraints_json or {}).get("trigger") or {}
        condition = str(trigger.get("description") or "满足触发条件后执行")
        completed_value = self._triggered_progress(plan.id, item)
        remaining_value = max(0, item.target_value - completed_value)
        blockers: list[str] = []
        warnings: list[str] = []
        if goal.status != "ACTIVE":
            blockers.append("请先恢复规划")
        if not window.period_key.startswith("trigger:") or item.binding_mode != "TRIGGERED":
            blockers.append("该任务已经不是待触发任务")
        if window.status != "PENDING":
            blockers.append("该触发窗口已经处理")
        if item.status not in {"DRAFT", "ACTIVE"}:
            blockers.append("该任务已取消或结束")
        if item.fulfillment_mode not in {"ONCE", "COUNT", "DURATION", "EVIDENCE"}:
            blockers.append("当前触发入口不支持这种履约类型")
        if remaining_value <= 0:
            blockers.append("该触发任务的剩余要求已满足")
        previous_window_id = (window.resolution_json or {}).get("previous_window_id")
        if previous_window_id:
            previous_task = self.session.scalar(
                select(PlanTask).where(
                    PlanTask.plan_version_id == plan.id,
                    PlanTask.source_work_item_id == item.id,
                    PlanTask.work_item_window_id == previous_window_id,
                )
            )
            if previous_task is None or previous_task.status != "COMPLETED":
                blockers.append("请先完成上一项触发任务")
        if payload.target_date < today:
            blockers.append("目标日期不能早于今天")
        if not window.eligible_from <= payload.target_date <= window.eligible_to:
            blockers.append(
                f"目标日期必须在 {window.eligible_from} 至 {window.eligible_to} 的触发窗口内"
            )
        existing = self.session.scalar(
            select(PlanTask).where(
                PlanTask.plan_version_id == plan.id,
                PlanTask.source_work_item_id == item.id,
                PlanTask.work_item_window_id == window.id,
            )
        )
        if existing is not None:
            blockers.append("该触发任务已经生成执行任务")
        dependency_blockers, dependency_state = self._work_item_dependency_preview(
            plan, activation, item, payload.target_date, today
        )
        blockers.extend(dependency_blockers)
        target_schedule = self.session.scalar(
            select(PlanDaySchedule).where(
                PlanDaySchedule.activation_id == activation.id,
                PlanDaySchedule.scheduled_date == payload.target_date,
            )
        )
        if target_schedule is not None:
            warnings.append("任务会加入目标日期现有执行日")
        if payload.target_date > activation.projected_end_date:
            warnings.append("目标日期仍在触发窗口内，规划结束日期会随新任务延长")
        base = {
            "plan_id": plan.id,
            "plan_revision": plan.revision,
            "goal_status": goal.status,
            "window_id": window.id,
            "work_item_id": item.id,
            "target_date": payload.target_date.isoformat(),
            "evidence": payload.evidence,
            "window_status": window.status,
            "window_revision": window.materialization_revision,
            "item_status": item.status,
            "item_updated_at": item.updated_at.isoformat(),
            "window_dates": [str(window.eligible_from), str(window.eligible_to)],
            "dependencies": dependency_state,
            "target_schedule_id": target_schedule.id if target_schedule else None,
            "projected_end_date": str(activation.projected_end_date),
            "fulfillment_mode": item.fulfillment_mode,
            "target_value": item.target_value,
            "target_unit": item.target_unit,
            "completed_value": completed_value,
            "remaining_value": remaining_value,
        }
        fingerprint = hashlib.sha256(
            json.dumps(base, ensure_ascii=False, sort_keys=True).encode()
        ).hexdigest()
        result = TriggeredTaskActivationPreviewRead(
            id="",
            status="PREVIEW",
            window_id=window.id,
            work_item_id=item.id,
            title=item.title,
            condition=condition,
            eligible_from=window.eligible_from,
            eligible_to=window.eligible_to,
            target_date=payload.target_date,
            fulfillment_mode=item.fulfillment_mode,
            target_value=item.target_value,
            target_unit=item.target_unit,
            completed_value=completed_value,
            remaining_value=remaining_value,
            blockers=blockers,
            warnings=warnings,
            can_apply=not blockers,
            fingerprint=fingerprint,
        )
        return result, base, window, item, activation

    @serialize_recovery_transaction
    def preview_triggered_activation(
        self, goal_id: str, payload: TriggeredTaskActivationRequest
    ) -> TriggeredTaskActivationPreviewRead:
        goal, plan = self._goal_and_plan(goal_id)
        result, base, _window, _item, _activation = self._build_triggered_activation_preview(
            goal, plan, payload
        )
        key = (
            "triggered-activation:"
            + hashlib.sha256(
                json.dumps(
                    {"goal_id": goal.id, **base}, ensure_ascii=False, sort_keys=True
                ).encode()
            ).hexdigest()
        )
        operation = self.session.scalar(
            select(RecoveryOperation).where(RecoveryOperation.idempotency_key == key)
        )
        if operation is None:
            operation = RecoveryOperation(
                goal_id=goal.id,
                source_plan_version_id=plan.id,
                mode="TRIGGERED_ACTIVATION",
                status="PREVIEW",
                idempotency_key=key,
                preview_json={
                    "request": payload.model_dump(mode="json"),
                    "base": base,
                    "result": result.model_dump(mode="json"),
                },
            )
            self.session.add(operation)
            self.session.flush()
        else:
            operation.status = "PREVIEW"
            operation.preview_json = {
                "request": payload.model_dump(mode="json"),
                "base": base,
                "result": result.model_dump(mode="json"),
            }
        result.id = operation.id
        operation.preview_json = {
            **operation.preview_json,
            "result": result.model_dump(mode="json"),
        }
        self.session.commit()
        return result

    @serialize_recovery_transaction
    def apply_triggered_activation(
        self, goal_id: str, payload: TriggeredTaskActivationApply
    ) -> TodayWorkspace:
        goal, plan = self._goal_and_plan(goal_id)
        operation = self.session.get(RecoveryOperation, payload.preview_id)
        if (
            operation is None
            or operation.goal_id != goal.id
            or operation.mode != "TRIGGERED_ACTIVATION"
        ):
            raise HTTPException(status_code=404, detail="Triggered activation preview not found")
        if operation.status == "APPLIED":
            if operation.source_plan_version_id != plan.id:
                raise HTTPException(status_code=409, detail="规划版本已变化，请刷新执行页")
            request = TriggeredTaskActivationRequest.model_validate(
                operation.preview_json.get("request") or {}
            )
            return self.get_today(goal_id, request.target_date)
        request = TriggeredTaskActivationRequest.model_validate(
            operation.preview_json.get("request") or {}
        )
        current, base, window, item, activation = self._build_triggered_activation_preview(
            goal, plan, request
        )
        stored = operation.preview_json.get("base") or {}
        if (
            current.fingerprint
            != str(operation.preview_json.get("result", {}).get("fingerprint") or "")
            or base != stored
        ):
            operation.status = "STALE"
            self.session.commit()
            raise HTTPException(status_code=409, detail="触发预览已过期，请重新生成")
        if not current.can_apply:
            raise HTTPException(status_code=409, detail="当前触发条件仍有阻塞")
        target_schedule = self.session.scalar(
            select(PlanDaySchedule).where(
                PlanDaySchedule.activation_id == activation.id,
                PlanDaySchedule.scheduled_date == request.target_date,
            )
        )
        destination = (
            self.session.get(PlanDay, target_schedule.plan_day_id) if target_schedule else None
        )
        if destination is None:
            week_index = max(
                1,
                plan_week_index(plan, activation.requested_start_date, request.target_date),
            )
            milestone = self.session.scalar(
                select(PlanMilestone)
                .where(
                    PlanMilestone.plan_version_id == plan.id,
                    PlanMilestone.stage_order == (item.stage_order or 1),
                )
                .order_by(PlanMilestone.start_week, PlanMilestone.id)
            )
            if milestone is None:
                milestone = self.session.scalar(
                    select(PlanMilestone)
                    .where(PlanMilestone.plan_version_id == plan.id)
                    .order_by(PlanMilestone.start_week, PlanMilestone.id)
                )
            if milestone is None:
                raise HTTPException(status_code=409, detail="缺少可用阶段，无法生成执行日")
            max_index = (
                self.session.scalar(
                    select(func.coalesce(func.max(PlanDay.day_index), 0)).where(
                        PlanDay.plan_version_id == plan.id,
                        PlanDay.week_index == week_index,
                    )
                )
                or 0
            )
            destination = PlanDay(
                plan_version_id=plan.id,
                milestone_id=milestone.id,
                stage_order=item.stage_order or milestone.stage_order,
                week_index=week_index,
                day_index=int(max_index) + 1,
                primary_goal=f"触发执行：{item.title}",
                suggested_minutes=item.estimate_minutes,
                evidence_json=item.evidence_json,
                do_not_json=[],
            )
            self.session.add(destination)
            self.session.flush()
            self.session.add(
                PlanDaySchedule(
                    activation_id=activation.id,
                    plan_day_id=destination.id,
                    scheduled_date=request.target_date,
                )
            )
        predecessor_ids: list[str] = []
        predecessor_item_ids = list(
            self.session.scalars(
                select(WorkItemDependency.predecessor_work_item_id).where(
                    WorkItemDependency.plan_version_id == plan.id,
                    WorkItemDependency.successor_work_item_id == item.id,
                )
            )
        )
        if predecessor_item_ids:
            predecessor_ids = list(
                self.session.scalars(
                    select(PlanTask.id).where(
                        PlanTask.plan_version_id == plan.id,
                        PlanTask.source_work_item_id.in_(predecessor_item_ids),
                        PlanTask.status.not_in({"CANCELLED", "CARRIED_OVER"}),
                    )
                )
            )
        remaining_value = current.remaining_value
        occurrence_index = int(
            self.session.scalar(
                select(func.coalesce(func.max(PlanTask.occurrence_index), 0)).where(
                    PlanTask.plan_version_id == plan.id,
                    PlanTask.source_work_item_id == item.id,
                )
            )
            or 0
        ) + 1
        task_estimate = item.estimate_minutes
        if item.fulfillment_mode == "DURATION" or item.target_unit == "MINUTES":
            task_estimate = min(task_estimate, max(1, remaining_value))
        authored = (window.resolution_json or {}).get('authored_tasks') or []
        authored_description = '\n'.join(f"{part['title']}：{part['description']}" for part in authored)
        task = PlanTask(
            plan_version_id=plan.id,
            plan_day_id=destination.id,
            source_work_item_id=item.id,
            work_item_window_id=window.id,
            occurrence_index=occurrence_index,
            materialization_key=f"work-item:{activation.id}:{item.item_key}:trigger:{occurrence_index}",
            materialized_at=utc_now(),
            origin_stage_order=item.stage_order or destination.stage_order,
            lineage_task_id=None,
            order_in_day=self._next_task_order(destination.id),
            title=authored[0]['title'] if len(authored) == 1 else item.title,
            description=authored_description or item.description,
            estimate_minutes=task_estimate,
            priority=item.priority,
            task_kind=item.task_kind,
            is_required=item.is_required,
            evidence_json=item.evidence_json,
            depends_on_json=predecessor_ids,
            satisfies_commitments_json=item.commitment_refs_json,
            ai_schedule_requested=False,
            is_splittable=item.is_splittable,
            scheduling_policy="TRIGGERED",
            scheduling_policy_json={
                "eligible_from": window.eligible_from.isoformat(),
                "eligible_to": window.eligible_to.isoformat(),
                "work_item_key": item.item_key,
                "trigger": current.condition,
            },
            status="NOT_STARTED",
        )
        self.session.add(task)
        self.session.flush()
        successor_ids = list(
            self.session.scalars(
                select(WorkItemDependency.successor_work_item_id).where(
                    WorkItemDependency.plan_version_id == plan.id,
                    WorkItemDependency.predecessor_work_item_id == item.id,
                )
            )
        )
        for successor in self.session.scalars(
            select(PlanTask).where(
                PlanTask.plan_version_id == plan.id,
                PlanTask.source_work_item_id.in_(successor_ids),
                PlanTask.status.not_in({"COMPLETED", "CANCELLED", "CARRIED_OVER"}),
            )
        ):
            successor.depends_on_json = sorted(set(successor.depends_on_json or []) | {task.id})
        window.status = "OPEN"
        window.opened_at = utc_now()
        window.materialization_revision += 1
        window.resolution_json = {
            **(window.resolution_json or {}),
            "selected_dates": [request.target_date.isoformat()],
            "trigger_evidence": request.evidence,
            "activation_mode": "USER_CONFIRMED",
        }
        item.status = "ACTIVE"
        next_remaining = max(0, remaining_value - (1 if item.target_unit == "COUNT" else task_estimate))
        if next_remaining > 0:
            next_period_key = f"trigger:{occurrence_index + 1}"
            existing_next = self.session.scalar(select(WorkItemWindow).where(
                WorkItemWindow.activation_id == activation.id,
                WorkItemWindow.work_item_id == item.id,
                WorkItemWindow.period_key == next_period_key,
            ))
            if existing_next is None:
                self.session.add(WorkItemWindow(
                    activation_id=activation.id,
                    work_item_id=item.id,
                    period_key=next_period_key,
                    eligible_from=window.eligible_from,
                    eligible_to=window.eligible_to,
                    required_value=next_remaining,
                    status="PENDING",
                    resolution_json={
                        "remaining_value": next_remaining,
                        "target_unit": item.target_unit,
                        "previous_window_id": window.id,
                    },
                ))
        activation.projected_end_date = max(activation.projected_end_date, request.target_date)
        self.session.add(
            ProgressEvent(
                goal_id=goal.id,
                plan_version_id=plan.id,
                plan_day_id=destination.id,
                task_id=task.id,
                event_type="TRIGGER_ACTIVATED",
                metadata_json={
                    "condition": current.condition,
                    "evidence": request.evidence,
                    "target_date": request.target_date.isoformat(),
                    "occurrence_index": occurrence_index,
                    "remaining_value": next_remaining,
                },
            )
        )
        self.session.add(
            ScheduleChange(
                goal_id=goal.id,
                entity_type="PLAN_TASK",
                entity_id=task.id,
                change_kind="TRIGGER_ACTIVATED",
                before_json={"window_status": "PENDING"},
                after_json={"target_date": request.target_date.isoformat()},
                reason=request.evidence,
            )
        )
        self._refresh_day_log(goal, plan, destination)
        goal.last_active_at = utc_now()
        operation.status = "APPLIED"
        operation.applied_at = utc_now()
        operation.result_plan_version_id = plan.id
        self.session.commit()
        return self.get_today(goal_id, request.target_date)

    def _carry_forward_candidates(
        self,
        plan: PlanVersion,
        activation: PlanActivation,
        target_date: date,
        all_tasks: list[PlanTask],
    ) -> list[CarryForwardCandidateRead]:
        next_schedule = self.session.scalar(
            select(PlanDaySchedule)
            .where(
                PlanDaySchedule.activation_id == activation.id,
                PlanDaySchedule.scheduled_date > target_date,
            )
            .order_by(PlanDaySchedule.scheduled_date)
        )
        if next_schedule is None:
            return []
        next_day = self.session.get(PlanDay, next_schedule.plan_day_id)
        if next_day is None:
            return []
        allocator = DayAllocator(self.session)
        indexes = allocator._dependency_indexes(plan.id)
        result = []
        for task in sorted(
            (
                item
                for item in all_tasks
                if item.plan_day_id == next_day.id
                and item.status not in {"COMPLETED", "CARRIED_OVER"}
            ),
            key=lambda item: (-int(item.is_required), item.order_in_day),
        ):
            blockers = []
            for dependency in task.depends_on_json or []:
                resolved = allocator._resolve_dependency(dependency, task, indexes)
                if resolved is not None and resolved.status not in {
                    "COMPLETED",
                    "CARRIED_OVER",
                }:
                    blockers.append(resolved.title)
            can_split, split_reason = self._task_split_policy(plan, task)
            result.append(
                CarryForwardCandidateRead(
                    task_id=task.id,
                    title=task.title,
                    description=task.description,
                    estimate_minutes=task.estimate_minutes,
                    scheduled_date=next_schedule.scheduled_date,
                    day_code=self._day_code(next_day),
                    can_carry=not blockers,
                    blocked_reason=(
                        f"需先完成：{'；'.join(blockers)}" if blockers else None
                    ),
                    can_split=can_split,
                    split_reason=split_reason,
                )
            )
        return result

    @staticmethod
    def _task_split_policy(plan: PlanVersion, task: PlanTask) -> tuple[bool, str | None]:
        if not task.is_splittable:
            return False, "该任务在生成规划时被标记为必须完整执行"
        exclusive = {
            str(item.get("label"))
            for item in (plan.commitments_json or [])
            if item.get("requires_separate_execution_day")
        }
        if exclusive.intersection(task.satisfies_commitments_json or []):
            return False, "该任务兑现独占执行日承诺，必须完整完成"
        content = " ".join([task.title, task.description, *(task.evidence_json or [])])
        atomic_markers = (
            "整套",
            "套卷",
            "模拟卷",
            "全真",
            "限时作答",
            "考试",
            "测验",
            "面试",
            "会议",
            "直播",
            "预约",
        )
        if any(marker in content for marker in atomic_markers):
            return False, "这是一项需要连续完整执行的任务，不能拆成部分"
        return True, None

    def carry_forward(self, goal_id: str, payload: CarryForwardRequest) -> TodayWorkspace:
        """Make an explicit early-finish/rest-day carry-in work unit.

        A rest day receives a lightweight synthetic PlanDay so the work is
        visible in the normal Today page and progress ledger.  On an execution
        day the same operation appends to that day's task list.  Dependencies
        are respected by only offering/accepting the first currently eligible
        task from the next scheduled day.
        """
        goal, plan = self._goal_and_plan(goal_id)
        if goal.status != "ACTIVE":
            raise HTTPException(status_code=409, detail="Plan is not active")
        target = payload.target_date
        if target != self._today_for(goal):
            raise HTTPException(status_code=422, detail="只能把后续任务承接到今天")
        activation = self._activation(plan.id)
        if activation is None:
            raise HTTPException(status_code=409, detail="Plan calendar is missing")
        schedule = self.session.scalar(
            select(PlanDaySchedule).where(
                PlanDaySchedule.activation_id == activation.id,
                PlanDaySchedule.scheduled_date == target,
            )
        )
        destination = self.session.get(PlanDay, schedule.plan_day_id) if schedule else None
        all_tasks = self._tasks(plan.id)
        candidates = self._carry_forward_candidates(plan, activation, target, all_tasks)
        selected = next(
            (item for item in candidates if item.task_id == payload.task_id),
            None,
        )
        if selected is None:
            raise HTTPException(status_code=409, detail="该任务不属于下一个执行日")
        if not selected.can_carry:
            raise HTTPException(
                status_code=409,
                detail=selected.blocked_reason or "该任务的前置条件尚未完成",
            )
        source_task = self.session.get(PlanTask, selected.task_id)
        source_day = self.session.get(PlanDay, source_task.plan_day_id) if source_task else None
        if source_task is None or source_day is None:
            raise HTTPException(status_code=409, detail="后续任务已经发生变化，请刷新后重试")
        if destination is None:
            max_index = self.session.scalar(
                select(func.coalesce(func.max(PlanDay.day_index), 0)).where(
                    PlanDay.plan_version_id == plan.id,
                    PlanDay.week_index == source_day.week_index,
                )
            ) or 0
            destination = PlanDay(
                plan_version_id=plan.id,
                milestone_id=source_day.milestone_id,
                stage_order=source_day.stage_order,
                week_index=source_day.week_index,
                day_index=max_index + 1,
                primary_goal="提前承接后续任务",
                suggested_minutes=payload.minutes or source_task.estimate_minutes,
                evidence_json=[],
                do_not_json=[],
            )
            self.session.add(destination)
            self.session.flush()
            self.session.add(
                PlanDaySchedule(
                    activation_id=activation.id,
                    plan_day_id=destination.id,
                    scheduled_date=target,
                )
            )
        original_estimate = source_task.estimate_minutes
        requested = payload.minutes or original_estimate
        if requested > source_task.estimate_minutes:
            raise HTTPException(status_code=422, detail="承接时长不能超过任务剩余时长")
        if requested < original_estimate and not selected.can_split:
            raise HTTPException(
                status_code=422,
                detail=selected.split_reason or "这项任务必须完整执行，不能只承接一部分",
            )
        if requested == source_task.estimate_minutes:
            allocator = DayAllocator(self.session)
            indexes = allocator._dependency_indexes(plan.id)
            for dependent in all_tasks:
                if dependent.status in {"COMPLETED", "CANCELLED", "CARRIED_OVER"}:
                    continue
                dependent.depends_on_json = [
                    resolved.id if (resolved := allocator._resolve_dependency(reference, dependent, indexes))
                    and (dependent.id == source_task.id or resolved.id == source_task.id) else reference
                    for reference in dependent.depends_on_json or []
                ]
            source_task.plan_day_id = destination.id
            source_task.order_in_day = self._next_task_order(destination.id)
            source_task.status = "NOT_STARTED"
            moved_task_id = source_task.id
        else:
            source_task.estimate_minutes -= requested
            moved = PlanTask(
                plan_version_id=plan.id,
                plan_day_id=destination.id,
                source_task_id=source_task.id,
                source_work_item_id=source_task.source_work_item_id,
                work_item_window_id=source_task.work_item_window_id,
                occurrence_index=source_task.occurrence_index,
                materialized_at=source_task.materialized_at,
                origin_stage_order=self._task_origin_stage(source_task),
                lineage_task_id=source_task.lineage_task_id or self._task_lineage_key(source_task),
                order_in_day=self._next_task_order(destination.id),
                title=f"提前承接：{source_task.title}",
                description="先完成该后续任务的一部分，剩余内容仍保留在原执行日。",
                estimate_minutes=requested,
                priority=source_task.priority,
                task_kind=source_task.task_kind,
                is_required=source_task.is_required,
                evidence_json=source_task.evidence_json,
                depends_on_json=source_task.depends_on_json,
                satisfies_commitments_json=source_task.satisfies_commitments_json,
                ai_schedule_requested=source_task.ai_schedule_requested,
                is_splittable=source_task.is_splittable,
                scheduling_policy=source_task.scheduling_policy,
                scheduling_policy_json=source_task.scheduling_policy_json,
                status="NOT_STARTED",
            )
            self.session.add(moved)
            self.session.flush()
            moved_task_id = moved.id
        self.session.add(
            ProgressEvent(
                goal_id=goal.id,
                plan_version_id=plan.id,
                plan_day_id=destination.id,
                task_id=moved_task_id,
                event_type="CARRY_FORWARD",
                metadata_json={
                    "source_day_id": source_day.id,
                    "source_date": selected.scheduled_date.isoformat(),
                    "target_date": target.isoformat(),
                    "minutes": requested,
                    "partial": requested < original_estimate,
                },
            )
        )
        self.session.add(
            ScheduleChange(
                goal_id=goal.id,
                entity_type="PLAN_TASK",
                entity_id=moved_task_id,
                change_kind="CARRY_FORWARD",
                before_json={"source_day_id": source_day.id},
                after_json={
                    "target_day_id": destination.id,
                    "target_date": target.isoformat(),
                    "minutes": requested,
                },
                reason="用户主动承接后续任务",
            )
        )
        self._refresh_day_log(goal, plan, destination)
        self.session.flush()
        allocate_plan_schedule(self.session, goal, plan.id)
        goal.last_active_at = utc_now()
        self.session.commit()
        return self.get_today(goal_id, target)

    def _flexible_arrange_blocked_reason(
        self, goal: Goal, activation: PlanActivation, task: PlanTask, today: date,
        allocator: DayAllocator, indexes: dict[str, object],
    ) -> str | None:
        if task.scheduling_policy != "FLOATING":
            return "只有弹性任务可以直接安排到今天"
        if goal.status != "ACTIVE":
            return "请先恢复规划或处理未完成任务"
        if task.status != "NOT_STARTED":
            return "任务已开始、受阻或结束，请从执行记录处理"
        blocked_reason = self._flexible_date_blocked_reason(activation, task, today)
        if blocked_reason:
            return blocked_reason
        blockers = []
        for dependency in task.depends_on_json or []:
            resolved = allocator._resolve_dependency(dependency, task, indexes)
            if resolved is None:
                blockers.append(f"待确认前置条件：{dependency}")
            elif resolved.status != "COMPLETED":
                blockers.append(resolved.title)
        return f"需先完成：{'；'.join(blockers)}" if blockers else None

    def _flexible_date_blocked_reason(
        self, activation: PlanActivation, task: PlanTask, today: date,
    ) -> str | None:
        window = self.session.get(WorkItemWindow, task.work_item_window_id) if task.work_item_window_id else None
        if window and not window.eligible_from <= today <= window.eligible_to:
            return f"可执行窗口为 {window.eligible_from} 至 {window.eligible_to}，请先调整窗口"
        rules = task.scheduling_policy_json or {}
        for key, is_start in (("not_before", True), ("not_after", False)):
            value = rules.get(key)
            if value:
                try:
                    boundary = date.fromisoformat(str(value))
                except ValueError:
                    return "任务日期规则无法识别，请先调整规则"
                if (is_start and today < boundary) or (not is_start and today > boundary):
                    return f"{'最早' if is_start else '最晚'}执行日期为 {boundary}，请先调整规则"
        for key, is_start in (("start_week", True), ("end_week", False)):
            if rules.get(key) is not None:
                value = rules[key]
                if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                    return "任务周次规则无法识别，请先调整规则"
                plan = self.session.get(PlanVersion, activation.plan_version_id)
                boundary = plan_week_range(plan, activation.requested_start_date, value)[0 if is_start else 1]
                if (is_start and today < boundary) or (not is_start and today > boundary):
                    return f"任务限定在第 {value} 周{'起' if is_start else '结束前'}执行，请先调整规则"
        return None

    @serialize_recovery_transaction
    def arrange_flexible_task(self, goal_id: str, payload: FlexibleTaskArrangeRequest) -> TodayWorkspace:
        """Move one outstanding FLOATING task into today's execution day.

        This is deliberately explicit: a flexible suggestion never becomes a
        commitment merely because it was rendered in Today.  The user action
        creates a carry-in event and preserves the original suggested date in
        the change metadata so progress and history remain explainable.
        """
        if self.session.bind.dialect.name == "sqlite":
            self.session.execute(text("BEGIN IMMEDIATE"))
        goal, plan = self._goal_and_plan(goal_id)
        if goal.status != "ACTIVE":
            raise HTTPException(status_code=409, detail="Plan is not active")
        today = self._today_for(goal)
        activation = self._activation(plan.id)
        if activation is None:
            raise HTTPException(status_code=409, detail="Plan calendar is missing")
        task = self.session.scalar(
            select(PlanTask).where(
                PlanTask.id == payload.task_id,
                PlanTask.plan_version_id == plan.id,
            )
        )
        if task is None:
            raise HTTPException(status_code=404, detail="Flexible task not found")
        if task.scheduling_policy != "FLOATING":
            raise HTTPException(status_code=409, detail="只有弹性任务可以直接安排到今天")
        if task.status in {"COMPLETED", "CANCELLED", "CARRIED_OVER"}:
            raise HTTPException(status_code=409, detail="该任务已经完成或不再可安排")

        schedules = list(
            self.session.scalars(
                select(PlanDaySchedule)
                .where(PlanDaySchedule.activation_id == activation.id)
                .order_by(PlanDaySchedule.scheduled_date, PlanDaySchedule.id)
            )
        )
        target_schedule = self._active_schedule(plan, activation, today)
        destination = self.session.get(PlanDay, target_schedule.plan_day_id) if target_schedule else None
        source_day = self.session.get(PlanDay, task.plan_day_id)
        if source_day is None:
            raise HTTPException(status_code=409, detail="任务所属执行日不存在")
        if any(item.get("available_days_per_week") for item in plan.temporal_segments_json or []):
            occupied, limit = self._candidate_target_capacity(plan, activation, today)
            if self._counts_toward_phase_date(plan, task, today) and source_day.id == (target_schedule.plan_day_id if target_schedule else None):
                occupied -= task.estimate_minutes
            if occupied + task.estimate_minutes > limit:
                raise HTTPException(409, "今天超过已确认阶段容量，请改选日期、分段处理或讨论容量。")
        task.scheduling_policy_json = {**(task.scheduling_policy_json or {}), "confirmed_date": today.isoformat()}
        if any(item.plan_day_id == task.plan_day_id and item.scheduled_date == today for item in schedules):
            self.session.commit()
            return self.get_today(goal_id, today)

        allocator = DayAllocator(self.session)
        indexes = allocator._dependency_indexes(plan.id)
        blocked_reason = self._flexible_arrange_blocked_reason(goal, activation, task, today, allocator, indexes)
        if blocked_reason:
            raise HTTPException(status_code=409, detail=blocked_reason)
        # Resolve relative references before changing coordinates. Only live
        # dependents are rewritten; completed records retain their history.
        for dependent in self._tasks(plan.id):
            if dependent.status in {"COMPLETED", "CANCELLED", "CARRIED_OVER"}:
                continue
            dependencies = []
            for reference in dependent.depends_on_json or []:
                resolved = allocator._resolve_dependency(reference, dependent, indexes)
                dependencies.append(resolved.id if resolved and (dependent.id == task.id or resolved.id == task.id) else reference)
            dependent.depends_on_json = dependencies

        if destination is None:
            max_index = self.session.scalar(
                select(func.coalesce(func.max(PlanDay.day_index), 0)).where(
                    PlanDay.plan_version_id == plan.id,
                    PlanDay.week_index == source_day.week_index,
                )
            ) or 0
            destination = PlanDay(
                plan_version_id=plan.id,
                milestone_id=source_day.milestone_id,
                stage_order=source_day.stage_order,
                week_index=source_day.week_index,
                day_index=max_index + 1,
                primary_goal="今日安排弹性任务",
                suggested_minutes=task.estimate_minutes,
                evidence_json=[],
                do_not_json=[],
            )
            self.session.add(destination)
            self.session.flush()
            self.session.add(
                PlanDaySchedule(
                    activation_id=activation.id,
                    plan_day_id=destination.id,
                    scheduled_date=today,
                )
            )

        source_date = next(
            (item.scheduled_date for item in schedules if item.plan_day_id == source_day.id),
            None,
        )
        original_order = task.order_in_day
        task.origin_stage_order = self._task_origin_stage(task)
        task.lineage_task_id = task.lineage_task_id or self._task_lineage_key(task)
        task.order_in_day = self._next_task_order(destination.id)
        task.plan_day_id = destination.id
        for block in self.session.scalars(select(TaskTimeBlock).where(
            TaskTimeBlock.task_id == task.id,
            TaskTimeBlock.status.in_({"PLANNED", "REQUIRES_REVIEW"}),
        )):
            before = {"status": block.status, "scheduled_date": block.scheduled_date.isoformat()}
            block.status = "CANCELLED"
            block.change_reason = "弹性任务已安排到今天，原时间段取消"
            block.revision += 1
            self.session.add(ScheduleChange(
                goal_id=goal.id, entity_type="TASK_TIME_BLOCK", entity_id=block.id,
                change_kind="FLEXIBLE_TASK_MOVE", before_json=before,
                after_json={"status": block.status}, reason=block.change_reason,
            ))
        self.session.add(
            ProgressEvent(
                goal_id=goal.id,
                plan_version_id=plan.id,
                plan_day_id=destination.id,
                task_id=task.id,
                event_type="CARRY_FORWARD",
                metadata_json={
                    "source_day_id": source_day.id,
                    "source_order": original_order,
                    "source_date": source_date.isoformat() if source_date else None,
                    "target_date": today.isoformat(),
                    "policy": "FLOATING",
                    "reason": "USER_ARRANGED_FLEXIBLE_TODAY",
                },
            )
        )
        self.session.add(
            ScheduleChange(
                goal_id=goal.id,
                entity_type="PLAN_TASK",
                entity_id=task.id,
                change_kind="CARRY_FORWARD",
                before_json={"source_day_id": source_day.id, "source_date": source_date.isoformat() if source_date else None},
                after_json={"target_day_id": destination.id, "target_date": today.isoformat()},
                reason="用户主动将弹性任务安排到今天",
            )
        )
        self._refresh_day_log(goal, plan, destination)
        self.session.flush()
        goal.last_active_at = utc_now()
        self.session.commit()
        return self.get_today(goal_id, today)

    def _build_flexible_decision_preview(
        self,
        goal: Goal,
        plan: PlanVersion,
        payload: FlexibleTaskDecisionRequest,
    ) -> tuple[FlexibleTaskDecisionPreviewRead, dict]:
        today = self._today_for(goal)
        activation = self._activation(plan.id)
        if activation is None:
            raise HTTPException(status_code=409, detail="Plan calendar is missing")
        task = self.session.scalar(
            select(PlanTask).where(
                PlanTask.id == payload.task_id,
                PlanTask.plan_version_id == plan.id,
            )
        )
        if task is None:
            raise HTTPException(status_code=404, detail="Task not found")
        if task.status in {"COMPLETED", "CANCELLED", "CARRIED_OVER"}:
            raise HTTPException(status_code=409, detail="该任务已经完成或不再可安排")
        schedules = list(
            self.session.scalars(
                select(PlanDaySchedule).where(PlanDaySchedule.activation_id == activation.id)
            )
        )
        source_schedule = next((item for item in schedules if item.plan_day_id == task.plan_day_id), None)
        allocator = DayAllocator(self.session)
        indexes = allocator._dependency_indexes(plan.id)
        blockers: list[str] = []
        warnings: list[str] = []
        target_date = today if payload.action in {"DEFER", "ARRANGE_TODAY"} else payload.target_date
        if payload.action == "ARRANGE_TODAY":
            blockers.append(
                self._flexible_arrange_blocked_reason(goal, activation, task, today, allocator, indexes)
                or ""
            )
            blockers = [item for item in blockers if item]
            if source_schedule and source_schedule.scheduled_date != today:
                warnings.append("原建议日期上的任务会移出；原日期时间块会取消，具体时间需重新安排。")
        elif payload.action == "RESCHEDULE":
            if task.scheduling_policy not in {"FLOATING", "DATE_WINDOW", "CADENCE", "QUOTA"}:
                blockers.append("只有弹性、期限、节奏和总量任务可以直接改期")
            if target_date is None:
                blockers.append("请先选择目标日期")
            else:
                if target_date < today:
                    blockers.append("改期日期不能早于今天")
                if target_date > activation.projected_end_date:
                    blockers.append("改期日期超出规划执行范围")
                date_block = self._flexible_date_blocked_reason(activation, task, target_date)
                if date_block:
                    blockers.append(date_block)
                if source_schedule and target_date != source_schedule.scheduled_date:
                    warnings.append("原日期上的时间块会取消，任务会移动到目标日期。")
                source_day = self.session.get(PlanDay, task.plan_day_id)
                target_schedule = next((row for row in schedules if row.scheduled_date == target_date), None)
                target_day = self.session.get(PlanDay, target_schedule.plan_day_id) if target_schedule else None
                target_week = target_day.week_index if target_day else max(1, plan_week_index(plan, activation.requested_start_date, target_date))
                weekly_labels = {item.get("label") for item in plan.commitments_json or [] if isinstance(item, dict) and item.get("cadence") == "PER_WEEK"}
                if source_day and (task.scheduling_policy == "CADENCE" or weekly_labels.intersection(task.satisfies_commitments_json or [])) and target_week != source_day.week_index:
                    blockers.append("改期不能跨越原周期，请通过执行讨论调整周期要求")
                dates = {row.plan_day_id: row.scheduled_date for row in schedules}
                for reference in task.depends_on_json or []:
                    predecessor = allocator._resolve_dependency(reference, task, indexes)
                    if predecessor is None:
                        blockers.append(f"待确认前置条件：{reference}")
                    elif dates.get(predecessor.plan_day_id, target_date) > target_date:
                        blockers.append(f"目标日期早于前置任务“{predecessor.title}”")
                    elif target_date == today and predecessor.status != "COMPLETED":
                        blockers.append(f"需先完成：{predecessor.title}")
        elif payload.action == "DEFER":
            if source_schedule is None or source_schedule.scheduled_date != today:
                blockers.append("只有今天的执行任务可以记录为今天不做")
            warnings.append("只记录今天不做的履约事实，不会自动修改正式计划。")

        planned_minutes = 0
        capacity_minutes = 0
        over_capacity_minutes = 0
        competing_tasks: list[str] = []
        target_task_states: list[list] = []
        if target_date is not None and payload.action in {"ARRANGE_TODAY", "RESCHEDULE"}:
            target_schedule = next(
                (item for item in schedules if item.scheduled_date == target_date), None
            )
            if target_schedule is not None:
                target_day = self.session.get(PlanDay, target_schedule.plan_day_id)
                capacity_minutes = int(target_day.suggested_minutes if target_day else 0)
                target_tasks = list(
                    self.session.scalars(
                        select(PlanTask).where(
                            PlanTask.plan_day_id == target_schedule.plan_day_id,
                            PlanTask.status.not_in({"COMPLETED", "CANCELLED", "CARRIED_OVER"}),
                        ).order_by(PlanTask.id)
                    )
                )
                for target_task in target_tasks:
                    if target_task.id == task.id:
                        continue
                    target_task_states.append([
                        target_task.id, target_task.status, target_task.estimate_minutes,
                    ])
                    if self._counts_toward_phase_date(plan, target_task, target_date):
                        planned_minutes += target_task.estimate_minutes
                        competing_tasks.append(target_task.title)
            else:
                capacity_minutes = self._candidate_target_capacity(plan, activation, target_date)[1]
            phase_capacity = calendar_capacity(plan, activation.requested_start_date, target_date)
            if any(item.get("available_days_per_week") for item in plan.temporal_segments_json or []) and phase_capacity["daily_minutes"] is not None:
                capacity_minutes = int(phase_capacity["daily_minutes"])
            planned_minutes += task.estimate_minutes
            over_capacity_minutes = max(0, planned_minutes - capacity_minutes)
            if over_capacity_minutes:
                if any(item.get("available_days_per_week") for item in plan.temporal_segments_json or []):
                    blockers.append(f"目标日期超过已确认阶段容量 {over_capacity_minutes} 分钟，请改选日期、分段处理或讨论容量。")
                else:
                    warnings.append(f"目标日期预计超出建议容量 {over_capacity_minutes} 分钟，可继续确认或改选其他日期。")
            if competing_tasks:
                warnings.append(
                    f"目标日期已有 {len(competing_tasks)} 项安排，确认后会与它们竞争当天容量。"
                )

        affected_ids = [task.id]
        for dependent in self._tasks(plan.id):
            if dependent.id == task.id or dependent.status in {"COMPLETED", "CANCELLED", "CARRIED_OVER"}:
                continue
            for reference in dependent.depends_on_json or []:
                resolved = allocator._resolve_dependency(reference, dependent, indexes)
                if resolved and resolved.id == task.id:
                    affected_ids.append(dependent.id)
                    warnings.append(f"后续任务“{dependent.title}”会继续保留前置依赖。")
                    dependent_date = next((row.scheduled_date for row in schedules if row.plan_day_id == dependent.plan_day_id), None)
                    if payload.action == "RESCHEDULE" and dependent_date and target_date and dependent_date < target_date:
                        blockers.append(f"改期会晚于后续任务“{dependent.title}”，请先调整后续安排")
                    break
        days = list(self.session.scalars(select(PlanDay).where(PlanDay.plan_version_id == plan.id)))
        events = list(
            self.session.scalars(
                select(ProgressEvent).where(
                    ProgressEvent.goal_id == goal.id,
                    ProgressEvent.plan_version_id == plan.id,
                )
            )
        )
        progress = self._commitment_progress(today, plan, schedules, days, self._tasks(plan.id), events)
        shared_preview = None
        proposed_slot = None
        if payload.action in {"ARRANGE_TODAY", "RESCHEDULE"} and target_date and target_date >= today:
            # Reuse the recommendation rule evaluator without invoking its model or persistence path.
            from planner_api.recommendation_service import RecommendationService
            from planner_api.schemas import UnifiedTodayTaskRead
            from planner_api.today_service import TodayService

            calendar = self.get_calendar(goal.id)
            group_keys = {entry.id: entry.recommendation_group_key for day in calendar.days for entry in day.tasks}
            eligibility = RecommendationService(self.session)._eligibility(
                goal, plan, activation, task, target_date, self._tasks(plan.id),
                {row.plan_day_id: row.scheduled_date for row in schedules},
                {day.id: day for day in days}, events, group_keys.get(task.id), group_keys,
            )
            if eligibility.get("blocked"):
                blockers.append(eligibility["blocked"][1])
            for commitment in self._commitment_progress(target_date, plan, schedules, days, self._tasks(plan.id), events):
                if commitment.label in (task.satisfies_commitments_json or []) and commitment.cadence != "PER_EXECUTION_DAY" and commitment.remaining_occurrences <= 0:
                    blockers.append(f"“{commitment.label}”当前要求已达标")
            board = TodayService(self.session).get_today(target_date)
            incoming = UnifiedTodayTaskRead(
                id=task.id, source="AGENT", plan_id=goal.id, plan_title=goal.title,
                title=task.title, description=task.description, estimate_minutes=task.estimate_minutes,
                priority=task.priority, status=task.status, scheduled_date=target_date,
                timezone=goal.timezone, is_fixed=False,
                scheduling_policy=task.scheduling_policy,
                scheduling_policy_data=task.scheduling_policy_json or {},
            )
            trial_board = board.model_copy(update={
                "fixed_tasks": [entry for entry in board.fixed_tasks if entry.id != task.id],
                "flexible_tasks": [entry for entry in board.flexible_tasks if entry.id != task.id] + [incoming],
            })
            shared_preview = allocator.preview(
                DayAllocationRequest(scheduled_date=target_date, use_ai=False), board=trial_board,
                availability_goal_ids={goal.id},
            )
            proposed_slot = next((entry for entry in shared_preview.assignments if entry.task_id == task.id), None)
            if proposed_slot is None:
                warnings.append("共享日程暂时没有可用完整时段；确认只调整日期，任务将保留待安排，请补充可用时间。")
            warnings.extend(shared_preview.notes)
        # Include related rules, progress and dates so a changed prerequisite or quota invalidates confirmation.
        state_tasks = self._tasks(plan.id)
        base = {
            "plan_id": plan.id,
            "today": today.isoformat(),
            "reason": payload.reason,
            "work_items": [[row.id, row.status, row.constraints_json, row.target_unit] for row in self.session.scalars(select(PlanWorkItem).where(PlanWorkItem.plan_version_id == plan.id).order_by(PlanWorkItem.id))],
            "work_dependencies": [[row.id, row.predecessor_work_item_id, row.successor_work_item_id, row.dependency_type, row.minimum_lag_days, row.maximum_lag_days, row.condition_json] for row in self.session.scalars(select(WorkItemDependency).where(WorkItemDependency.plan_version_id == plan.id).order_by(WorkItemDependency.id))],
            "windows": [[row.id, str(row.eligible_from), str(row.eligible_to), row.status, row.required_value] for row in self.session.scalars(select(WorkItemWindow).where(WorkItemWindow.activation_id == activation.id).order_by(WorkItemWindow.id))],
            "activation": [activation.requested_start_date.isoformat(), activation.projected_end_date.isoformat(), activation.excluded_dates_json],
            "tasks": [[row.id, row.plan_day_id, row.status, row.estimate_minutes, row.depends_on_json, row.scheduling_policy, row.scheduling_policy_json, row.satisfies_commitments_json, row.source_work_item_id, row.work_item_window_id, row.occurrence_index] for row in sorted(state_tasks, key=lambda row: row.id)],
            "schedules": [[row.id, row.plan_day_id, row.scheduled_date.isoformat()] for row in sorted(schedules, key=lambda row: row.id)],
            "events": [[row.id, row.event_type, str(row.occurred_at), row.metadata_json] for row in sorted(events, key=lambda row: row.id)],
            "source_time_blocks": [[row.id, row.status, str(row.scheduled_date), row.start_local, row.end_local, row.revision] for row in self.session.scalars(select(TaskTimeBlock).where(TaskTimeBlock.task_id == task.id).order_by(TaskTimeBlock.id))],
            "commitments": plan.commitments_json,
            "blockers": sorted(set(blockers)),
            "shared_fingerprint": shared_preview.fingerprint if shared_preview else None,
            "action": payload.action,
            "task_id": task.id,
            "target_date": target_date.isoformat() if target_date else None,
            "source_date": source_schedule.scheduled_date.isoformat() if source_schedule else None,
            "task_status": task.status,
            "task_estimate_minutes": task.estimate_minutes,
            "task_plan_day_id": task.plan_day_id,
            "affected_task_ids": affected_ids,
            "planned_minutes": planned_minutes,
            "capacity_minutes": capacity_minutes,
            "over_capacity_minutes": over_capacity_minutes,
            "competing_tasks": competing_tasks,
            "target_task_states": target_task_states,
        }
        fingerprint = hashlib.sha256(json.dumps(base, sort_keys=True).encode()).hexdigest()
        result = FlexibleTaskDecisionPreviewRead(
            id="",
            status="PREVIEW",
            action=payload.action,
            task_id=task.id,
            title=task.title,
            scheduling_policy=task.scheduling_policy or "EXACT_DAY",
            source_date=source_schedule.scheduled_date if source_schedule else None,
            target_date=target_date,
            planned_minutes=planned_minutes,
            capacity_minutes=capacity_minutes,
            over_capacity_minutes=over_capacity_minutes,
            competing_tasks=competing_tasks,
            shared_available_minutes=shared_preview.available_minutes if shared_preview else None,
            proposed_start_local=proposed_slot.start_local if proposed_slot else None,
            proposed_end_local=proposed_slot.end_local if proposed_slot else None,
            blockers=sorted(set(blockers)),
            warnings=list(dict.fromkeys(warnings)),
            affected_task_ids=affected_ids,
            remaining_commitments=progress,
            can_apply=not blockers,
            fingerprint=fingerprint,
        )
        return result, base

    @serialize_recovery_transaction
    def preview_flexible_decision(
        self, goal_id: str, payload: FlexibleTaskDecisionRequest
    ) -> FlexibleTaskDecisionPreviewRead:
        goal, plan = self._goal_and_plan(goal_id)
        if goal.status != "ACTIVE":
            raise HTTPException(status_code=409, detail="Plan is not active")
        result, base = self._build_flexible_decision_preview(goal, plan, payload)
        key = "flexible-decision:" + hashlib.sha256(
            json.dumps({"goal_id": goal.id, **base}, sort_keys=True).encode()
        ).hexdigest()
        existing = self.session.scalar(
            select(RecoveryOperation).where(RecoveryOperation.idempotency_key == key)
        )
        if existing is None:
            existing = RecoveryOperation(
                goal_id=goal.id,
                source_plan_version_id=plan.id,
                mode="FLEXIBLE_DECISION",
                status="PREVIEW",
                idempotency_key=key,
                preview_json={"request": payload.model_dump(mode="json"), "base": base, "result": result.model_dump(mode="json")},
            )
            self.session.add(existing)
            self.session.flush()
        elif existing.status == "PREVIEW":
            # A stale preview is regenerated under the same idempotency key,
            # keeping the UI from accumulating duplicate decision records.
            existing.preview_json = {"request": payload.model_dump(mode="json"), "base": base, "result": result.model_dump(mode="json")}
        result.id = existing.id
        result.status = existing.status if existing.status in {"PREVIEW", "APPLIED"} else "PREVIEW"
        self.session.commit()
        return result

    @serialize_recovery_transaction
    def apply_flexible_decision(
        self, goal_id: str, payload: FlexibleTaskDecisionApply
    ) -> TodayWorkspace:
        goal, plan = self._goal_and_plan(goal_id)
        operation = self.session.get(RecoveryOperation, payload.preview_id)
        if operation is None or operation.goal_id != goal.id or operation.mode != "FLEXIBLE_DECISION":
            raise HTTPException(status_code=404, detail="Decision preview not found")
        if operation.status == "APPLIED":
            return self.get_today(goal_id)
        if goal.status != "ACTIVE" or operation.source_plan_version_id != plan.id:
            raise HTTPException(status_code=409, detail="规划状态或版本已变化，请重新生成预览")
        request = FlexibleTaskDecisionRequest.model_validate(operation.preview_json.get("request") or {})
        current, base = self._build_flexible_decision_preview(goal, plan, request)
        stored = operation.preview_json.get("base") or {}
        if current.fingerprint != str(operation.preview_json.get("result", {}).get("fingerprint") or "") or base != stored:
            raise HTTPException(status_code=409, detail="预览已过期，请重新生成取舍预览")
        if not current.can_apply:
            raise HTTPException(status_code=409, detail="当前取舍仍有阻塞条件")
        if request.action == "DEFER":
            result = self.record_task_progress(
                goal_id,
                request.task_id,
                TaskProgressUpdate(action="DEFER", reason=request.reason),
            )
        elif request.action == "ARRANGE_TODAY":
            result = self.arrange_flexible_task(goal_id, FlexibleTaskArrangeRequest(task_id=request.task_id))
        else:
            result = self.reschedule_flexible_task(
                goal_id,
                FlexibleTaskRescheduleRequest(
                    task_id=request.task_id,
                    target_date=request.target_date,
                    reason=request.reason or "用户确认改期",
                ),
            )
        operation.status = "APPLIED"
        operation.applied_at = utc_now()
        self.session.commit()
        return result

    @serialize_recovery_transaction
    def reschedule_flexible_task(
        self, goal_id: str, payload: FlexibleTaskRescheduleRequest
    ) -> TodayWorkspace:
        """Move an outstanding non-fixed task to a chosen date with an audit trail."""
        if self.session.bind.dialect.name == "sqlite":
            self.session.execute(text("BEGIN IMMEDIATE"))
        goal, plan = self._goal_and_plan(goal_id)
        if goal.status != "ACTIVE":
            raise HTTPException(status_code=409, detail="Plan is not active")
        today = self._today_for(goal)
        if payload.target_date < today:
            raise HTTPException(status_code=422, detail="改期日期不能早于今天")
        activation = self._activation(plan.id)
        if activation is None:
            raise HTTPException(status_code=409, detail="Plan calendar is missing")
        if payload.target_date > activation.projected_end_date:
            raise HTTPException(status_code=422, detail="改期日期超出规划执行范围")
        task = self.session.scalar(
            select(PlanTask).where(
                PlanTask.id == payload.task_id,
                PlanTask.plan_version_id == plan.id,
            )
        )
        if task is None:
            raise HTTPException(status_code=404, detail="Task not found")
        if task.scheduling_policy not in {"FLOATING", "DATE_WINDOW", "CADENCE", "QUOTA"}:
            raise HTTPException(status_code=409, detail="只有弹性、期限、节奏和总量任务可以直接改期")
        if task.status in {"COMPLETED", "CANCELLED", "CARRIED_OVER"}:
            raise HTTPException(status_code=409, detail="该任务已经完成或不再可安排")
        blocked = self._flexible_date_blocked_reason(activation, task, payload.target_date)
        if blocked:
            raise HTTPException(status_code=409, detail=blocked)

        schedules = list(
            self.session.scalars(
                select(PlanDaySchedule)
                .where(PlanDaySchedule.activation_id == activation.id)
                .order_by(PlanDaySchedule.scheduled_date, PlanDaySchedule.id)
            )
        )
        source_day = self.session.get(PlanDay, task.plan_day_id)
        if source_day is None:
            raise HTTPException(status_code=409, detail="任务所属执行日不存在")
        source_schedule = next((item for item in schedules if item.plan_day_id == source_day.id), None)
        if any(item.get("available_days_per_week") for item in plan.temporal_segments_json or []):
            occupied, limit = self._candidate_target_capacity(plan, activation, payload.target_date)
            if source_schedule and source_schedule.scheduled_date == payload.target_date and self._counts_toward_phase_date(plan, task, payload.target_date):
                occupied -= task.estimate_minutes
            if occupied + task.estimate_minutes > limit:
                raise HTTPException(409, "目标日期超过已确认阶段容量，请改选日期、分段处理或讨论容量。")
        task.scheduling_policy_json = {**(task.scheduling_policy_json or {}), "confirmed_date": payload.target_date.isoformat()}
        if source_schedule and source_schedule.scheduled_date == payload.target_date:
            self.session.commit()
            return self.get_today(goal_id, payload.target_date)

        target_schedule = next((item for item in schedules if item.scheduled_date == payload.target_date), None)
        target_day = self.session.get(PlanDay, target_schedule.plan_day_id) if target_schedule else None
        target_week = target_day.week_index if target_day else max(
            1, plan_week_index(plan, activation.requested_start_date, payload.target_date)
        )
        # A weekly quota must retain its period; changing a task's date must
        # not silently transfer the remaining obligation into another week.
        weekly_labels = {
            item.get("label") for item in plan.commitments_json or []
            if isinstance(item, dict) and item.get("cadence") == "PER_WEEK"
        }
        if (task.scheduling_policy == "CADENCE" or weekly_labels.intersection(task.satisfies_commitments_json or [])) and target_week != source_day.week_index:
            raise HTTPException(status_code=409, detail="改期不能跨越原周期，请通过执行讨论调整周期要求")

        allocator = DayAllocator(self.session)
        indexes = allocator._dependency_indexes(plan.id)
        source_dates = {item.plan_day_id: item.scheduled_date for item in schedules}
        for dependency in task.depends_on_json or []:
            resolved = allocator._resolve_dependency(dependency, task, indexes)
            if resolved is None:
                raise HTTPException(status_code=409, detail=f"待确认前置条件：{dependency}")
            dependency_date = source_dates.get(resolved.plan_day_id)
            if dependency_date and dependency_date > payload.target_date:
                raise HTTPException(status_code=409, detail=f"目标日期早于前置任务“{resolved.title}”")
            if payload.target_date == today and resolved.status != "COMPLETED":
                raise HTTPException(status_code=409, detail=f"需先完成：{resolved.title}")

        dependency_updates = {}
        for dependent in self._tasks(plan.id):
            if dependent.status in {"COMPLETED", "CANCELLED", "CARRIED_OVER"}:
                continue
            references = []
            for reference in dependent.depends_on_json or []:
                resolved = allocator._resolve_dependency(reference, dependent, indexes)
                if resolved and resolved.id == task.id:
                    dependent_date = source_dates.get(dependent.plan_day_id)
                    if dependent_date and dependent_date < payload.target_date:
                        raise HTTPException(status_code=409, detail=f"改期会晚于后续任务“{dependent.title}”，请先调整后续安排")
                references.append(resolved.id if resolved and (dependent.id == task.id or resolved.id == task.id) else reference)
            dependency_updates[dependent.id] = references
        for dependent_id, references in dependency_updates.items():
            self.session.get(PlanTask, dependent_id).depends_on_json = references

        destination_schedule = next(
            (item for item in schedules if item.scheduled_date == payload.target_date), None
        )
        destination = self.session.get(PlanDay, destination_schedule.plan_day_id) if destination_schedule else None
        if destination is None:
            week_index = target_week
            max_index = self.session.scalar(
                select(func.coalesce(func.max(PlanDay.day_index), 0)).where(
                    PlanDay.plan_version_id == plan.id,
                    PlanDay.week_index == week_index,
                )
            ) or 0
            destination = PlanDay(
                plan_version_id=plan.id,
                milestone_id=source_day.milestone_id,
                stage_order=source_day.stage_order,
                week_index=week_index,
                day_index=int(max_index) + 1,
                primary_goal="改期执行弹性任务",
                suggested_minutes=task.estimate_minutes,
                evidence_json=[],
                do_not_json=[],
            )
            self.session.add(destination)
            self.session.flush()
            self.session.add(
                PlanDaySchedule(
                    activation_id=activation.id,
                    plan_day_id=destination.id,
                    scheduled_date=payload.target_date,
                )
            )

        original_order = task.order_in_day
        task.origin_stage_order = self._task_origin_stage(task)
        task.lineage_task_id = task.lineage_task_id or self._task_lineage_key(task)
        task.order_in_day = self._next_task_order(destination.id)
        task.plan_day_id = destination.id
        for block in self.session.scalars(
            select(TaskTimeBlock).where(
                TaskTimeBlock.task_id == task.id,
                TaskTimeBlock.status.in_({"PLANNED", "REQUIRES_REVIEW"}),
            )
        ):
            before = {"status": block.status, "scheduled_date": block.scheduled_date.isoformat()}
            block.status = "CANCELLED"
            block.change_reason = "任务已按用户选择改期，原时间段取消"
            block.revision += 1
            self.session.add(
                ScheduleChange(
                    goal_id=goal.id,
                    entity_type="TASK_TIME_BLOCK",
                    entity_id=block.id,
                    change_kind="FLEXIBLE_TASK_RESCHEDULE",
                    before_json=before,
                    after_json={"status": block.status},
                    reason=block.change_reason,
                )
            )
        self.session.add(
            ProgressEvent(
                goal_id=goal.id,
                plan_version_id=plan.id,
                plan_day_id=destination.id,
                task_id=task.id,
                event_type="RESCHEDULED",
                metadata_json={
                    "source_day_id": source_day.id,
                    "source_date": source_schedule.scheduled_date.isoformat() if source_schedule else None,
                    "target_date": payload.target_date.isoformat(),
                    "policy": task.scheduling_policy,
                    "reason": payload.reason,
                },
            )
        )
        self.session.add(
            ScheduleChange(
                goal_id=goal.id,
                entity_type="PLAN_TASK",
                entity_id=task.id,
                change_kind="FLEXIBLE_TASK_RESCHEDULE",
                before_json={
                    "source_day_id": source_day.id,
                    "source_date": source_schedule.scheduled_date.isoformat() if source_schedule else None,
                    "source_order": original_order,
                },
                after_json={"target_day_id": destination.id, "target_date": payload.target_date.isoformat()},
                reason=payload.reason,
            )
        )
        self._refresh_day_log(goal, plan, source_day)
        self._refresh_day_log(goal, plan, destination)
        goal.last_active_at = utc_now()
        self.session.commit()
        return self.get_today(goal_id, payload.target_date)

    def _inactivity(
        self,
        goal: Goal,
        plan: PlanVersion,
        activation: PlanActivation,
        schedules: list[PlanDaySchedule],
        tasks_by_day: dict[str, list[PlanTask]],
    ) -> ExecutionInactivityRead:
        if plan.schedule_type == "OUTCOME":
            return ExecutionInactivityRead(level="NONE")
        today = self._today_for(goal)
        missed = []
        partial_dates: list[date] = []
        interrupted_dates: list[date] = []
        for schedule in schedules:
            if schedule.scheduled_date >= today:
                continue
            active_tasks = [
                task
                for task in tasks_by_day.get(schedule.plan_day_id, [])
                if task.status != "CARRIED_OVER"
            ]
            if active_tasks and any(
                task.status not in {"COMPLETED", "CANCELLED"} for task in active_tasks
            ):
                missed.append(schedule)
                if any(
                    task.status in {"COMPLETED", "CANCELLED"} for task in active_tasks
                ):
                    partial_dates.append(schedule.scheduled_date)
                else:
                    interrupted_dates.append(schedule.scheduled_date)

        if not missed:
            return ExecutionInactivityRead(level="NONE")

        missed_dates = [item.scheduled_date for item in missed]
        deferred = self.session.scalar(
            select(RecoveryOperation)
            .where(
                RecoveryOperation.goal_id == goal.id,
                RecoveryOperation.source_plan_version_id == plan.id,
                RecoveryOperation.mode == "DEFER_UNRESOLVED",
                RecoveryOperation.status == "DEFERRED",
            )
            .order_by(RecoveryOperation.created_at.desc())
        )
        deferred_dates = {
            date.fromisoformat(item)
            for item in (deferred.preview_json.get("missed_dates", []) if deferred else [])
        }
        current_dates = set(missed_dates)
        recovery_deferred = bool(deferred)
        has_new_missed_work = bool(deferred) and not current_dates.issubset(deferred_dates)

        activity_times = [activation.activated_at]
        activity_times.extend(
            self.session.scalars(
                select(ProgressEvent.occurred_at).where(
                    ProgressEvent.goal_id == goal.id,
                    ProgressEvent.plan_version_id == plan.id,
                )
            )
        )
        activity_times.extend(
            self.session.scalars(
                select(Feedback.created_at).where(
                    Feedback.goal_id == goal.id,
                    Feedback.plan_version_id == plan.id,
                )
            )
        )
        activity_times.extend(
            self.session.scalars(
                select(FocusSession.completed_at).where(
                    FocusSession.goal_id == goal.id,
                    FocusSession.plan_version_id == plan.id,
                )
            )
        )
        last_activity = max(item for item in activity_times if item is not None)
        last_activity_date = self._local_date(last_activity, goal)
        inactive_days = max(0, (today - last_activity_date).days)
        if inactive_days == 0:
            level = "NONE"
        elif inactive_days >= 7:
            level = "PAUSED_7_DAYS"
        elif inactive_days >= 3:
            level = "PAUSED_3_DAYS"
        else:
            level = "MISSED_ONCE"
        return ExecutionInactivityRead(
            level=level,
            inactive_days=inactive_days,
            missed_execution_days=len(missed),
            missed_dates=missed_dates,
            partial_execution_days=len(partial_dates),
            partial_dates=partial_dates,
            interrupted_execution_days=len(interrupted_dates),
            interrupted_dates=interrupted_dates,
            last_activity_date=last_activity_date,
            latest_missed_date=missed[-1].scheduled_date,
            recovery_deferred=recovery_deferred,
            has_new_missed_work=has_new_missed_work,
        )

    def _inactivity_for_plan(self, goal: Goal, plan: PlanVersion) -> ExecutionInactivityRead:
        activation = self._activation(plan.id)
        if activation is None:
            return ExecutionInactivityRead(level="NONE")
        schedules = list(
            self.session.scalars(
                select(PlanDaySchedule)
                .where(PlanDaySchedule.activation_id == activation.id)
                .order_by(PlanDaySchedule.scheduled_date)
            )
        )
        tasks_by_day: dict[str, list[PlanTask]] = {}
        for task in self._tasks(plan.id):
            tasks_by_day.setdefault(task.plan_day_id, []).append(task)
        return self._inactivity(goal, plan, activation, schedules, tasks_by_day)

    def update_task(self, goal_id: str, task_id: str, event_type: str) -> TodayWorkspace:
        goal, plan = self._goal_and_plan(goal_id)
        if goal.status != "ACTIVE":
            raise HTTPException(status_code=409, detail="Plan has not started")
        task = self.session.get(PlanTask, task_id)
        if task is None or task.plan_version_id != plan.id:
            raise HTTPException(status_code=404, detail="Task not found")
        activation = self._activation(plan.id)
        if activation is None:
            raise HTTPException(status_code=409, detail="Plan calendar is missing")
        schedule = self.session.scalar(
            select(PlanDaySchedule).where(
                PlanDaySchedule.activation_id == activation.id,
                PlanDaySchedule.plan_day_id == task.plan_day_id,
            )
        )
        active_schedule = self._active_schedule(plan, activation, self._today_for(goal))
        if schedule is None or (
            schedule.scheduled_date != self._today_for(goal)
            and (active_schedule is None or active_schedule.id != schedule.id)
        ):
            raise HTTPException(status_code=409, detail="Only today's tasks can be updated")
        if task.status == "COMPLETED" and event_type == "STARTED":
            return self.get_today(goal_id)
        target_status = "IN_PROGRESS" if event_type == "STARTED" else "COMPLETED"
        if task.status != target_status:
            self._assert_hybrid_predecessors_completed(plan, task)
            task.status = target_status
            self.session.add(
                ProgressEvent(
                    goal_id=goal.id,
                    plan_version_id=plan.id,
                    plan_day_id=task.plan_day_id,
                    task_id=task.id,
                    event_type=event_type,
                )
            )
            goal.last_active_at = utc_now()
            day = self.session.get(PlanDay, task.plan_day_id)
            self._apply_automatic_completions(goal, plan, day)
            self._refresh_day_log(goal, plan, day)
            self.session.commit()
        return self.get_today(goal_id)

    def record_task_progress(
        self, goal_id: str, task_id: str, payload: TaskProgressUpdate
    ) -> TodayWorkspace:
        """Record non-terminal Hybrid履约 facts without rewriting the plan."""
        goal, plan = self._goal_and_plan(goal_id)
        if goal.status != "ACTIVE":
            raise HTTPException(status_code=409, detail="Plan has not started")
        task = self.session.get(PlanTask, task_id)
        if task is None or task.plan_version_id != plan.id:
            raise HTTPException(status_code=404, detail="Task not found")
        if task.status in {"COMPLETED", "CANCELLED", "CARRIED_OVER"}:
            raise HTTPException(status_code=409, detail="Finished task cannot be changed")
        day = self._require_today_plan_day(goal, plan.id, task.plan_day_id)
        if payload.action == 'PARTIAL':
            self._assert_hybrid_predecessors_completed(plan, task)
        event_type = {
            "PARTIAL": "PARTIAL_COMPLETION",
            "SKIP": "SKIPPED",
            "DEFER": "DEFERRED",
        }[payload.action]
        if payload.action == "PARTIAL" and task.status == "NOT_STARTED":
            task.status = "IN_PROGRESS"
        if payload.action == "SKIP":
            task.status = "CANCELLED"
            source_item = self.session.get(PlanWorkItem, task.source_work_item_id) if task.source_work_item_id else None
            skip_route = {
                "EXACT_DAY": "RECOVERY_CANDIDATE",
                "CADENCE": "FLEXIBLE_REMAINDER",
                "QUOTA": "FLEXIBLE_REMAINDER",
                "DATE_WINDOW": "FLEXIBLE_REMAINDER",
                "FLOATING": "FLEXIBLE_INBOX",
                "TASK_POOL": "CANDIDATE_RELEASE",
                "TRIGGERED": "WAIT_FOR_TRIGGER",
            }.get(task.scheduling_policy or "EXACT_DAY", "RECOVERY_CANDIDATE")
            generic_window = (
                self.session.get(WorkItemWindow, task.work_item_window_id)
                if task.work_item_window_id else None
            )
            if generic_window is not None and not (source_item and source_item.group_id):
                generic_window.resolution_json = {
                    **(generic_window.resolution_json or {}),
                    "skip_route": skip_route,
                    "skip_reason": payload.reason,
                }
            if source_item and source_item.group_id and task.work_item_window_id:
                self.session.flush()
                window = self.session.get(WorkItemWindow, task.work_item_window_id)
                remaining_window_tasks = self.session.scalar(
                    select(PlanTask.id).where(
                        PlanTask.work_item_window_id == task.work_item_window_id,
                        PlanTask.status.not_in({"CANCELLED", "CARRIED_OVER"}),
                    )
                )
                if window is not None and window.status not in {"CANCELLED", "EXPIRED"} and remaining_window_tasks is None:
                    previous_status = window.status
                    window.status = "CANCELLED"
                    window.materialization_revision += 1
                    window.resolution_json = {
                        **(window.resolution_json or {}),
                        "selection_state": "SKIPPED",
                        "skip_reason": payload.reason,
                    }
                    self.session.add(
                        ScheduleChange(
                            goal_id=goal.id,
                            entity_type="WORK_ITEM_WINDOW",
                            entity_id=window.id,
                            change_kind="CANDIDATE_SKIPPED",
                            before_json={"status": previous_status},
                            after_json={
                                "status": "CANCELLED",
                                "selection_state": "SKIPPED",
                                "reason": payload.reason,
                            },
                            reason=payload.reason or "用户跳过候选任务",
                        )
                    )
        self.session.add(
            ProgressEvent(
                goal_id=goal.id,
                plan_version_id=plan.id,
                plan_day_id=day.id,
                task_id=task.id,
                event_type=event_type,
                metadata_json={
                    "amount": payload.amount,
                    "unit": payload.unit,
                    "reason": payload.reason,
                    "skip_guidance": (
                        {
                            "TASK_POOL": ["补选其他候选任务", "也可改到其他日期"],
                            "TRIGGERED": ["保留触发条件，满足条件后重新安排"],
                            "CADENCE": ["保留本周期剩余配额", "转入弹性补做"],
                            "QUOTA": ["保留本周期剩余配额", "转入弹性补做"],
                            "DATE_WINDOW": ["加入弹性待安排", "改到允许日期"],
                            "FLOATING": ["加入弹性待安排", "改到其他日期"],
                            "EXACT_DAY": ["记录跳过", "需要时通过恢复安排补做"],
                        }.get(task.scheduling_policy or "EXACT_DAY", ["记录跳过"])
                        if payload.action == "SKIP" else []
                    ),
                },
            )
        )
        goal.last_active_at = utc_now()
        self._apply_automatic_completions(goal, plan, day)
        self._refresh_day_log(goal, plan, day)
        self.session.commit()
        return self.get_today(goal_id)

    def complete_missed_tasks(
        self, goal_id: str, task_ids: list[str]
    ) -> ExecutionProgressRead:
        goal, plan = self._goal_and_plan(goal_id)
        if goal.status not in {"ACTIVE", "RECOVERY_REQUIRED", "PAUSED"}:
            raise HTTPException(status_code=409, detail="Plan is not available for recovery")
        activation = self._activation(plan.id)
        if activation is None:
            raise HTTPException(status_code=409, detail="Plan calendar is missing")

        unique_ids = list(dict.fromkeys(task_ids))
        tasks = list(self.session.scalars(select(PlanTask).where(PlanTask.id.in_(unique_ids))))
        tasks_by_id = {task.id: task for task in tasks}
        if len(tasks_by_id) != len(unique_ids) or any(
            task.plan_version_id != plan.id for task in tasks
        ):
            raise HTTPException(status_code=404, detail="Task not found")

        schedules = {
            item.plan_day_id: item
            for item in self.session.scalars(
                select(PlanDaySchedule).where(PlanDaySchedule.activation_id == activation.id)
            )
        }
        today = self._today_for(goal)
        touched_days: dict[str, PlanDay] = {}
        for task_id in unique_ids:
            task = tasks_by_id[task_id]
            schedule = schedules.get(task.plan_day_id)
            if schedule is None or schedule.scheduled_date >= today:
                raise HTTPException(
                    status_code=409,
                    detail="Only unfinished tasks from past execution days can be recorded",
                )
            if task.status in {"CANCELLED", "CARRIED_OVER"}:
                raise HTTPException(status_code=409, detail="Task can no longer be completed")
            day = self.session.get(PlanDay, task.plan_day_id)
            if day is None:
                raise HTTPException(status_code=404, detail="Plan day not found")
            touched_days[day.id] = day
            if task.status == "COMPLETED":
                continue
            task.status = "COMPLETED"
            self.session.add(
                ProgressEvent(
                    goal_id=goal.id,
                    plan_version_id=plan.id,
                    plan_day_id=task.plan_day_id,
                    task_id=task.id,
                    event_type="COMPLETED",
                    metadata_json={
                        "recording_mode": "RETROACTIVE_COMPLETION",
                        "scheduled_date": schedule.scheduled_date.isoformat(),
                    },
                )
            )

        for day in touched_days.values():
            self._apply_automatic_completions(goal, plan, day)
            self._refresh_day_log(goal, plan, day)
        tasks_by_day: dict[str, list[PlanTask]] = {}
        for task in self._tasks(plan.id):
            tasks_by_day.setdefault(task.plan_day_id, []).append(task)
        remaining_inactivity = self._inactivity(
            goal, plan, activation, list(schedules.values()), tasks_by_day
        )
        if goal.status == "RECOVERY_REQUIRED" and not remaining_inactivity.missed_dates:
            goal.status = "ACTIVE"
        goal.last_active_at = utc_now()
        self.session.commit()
        return self.get_progress(goal_id)

    def complete_today(self, goal_id: str) -> TodayWorkspace:
        goal, plan = self._goal_and_plan(goal_id)
        current = self.get_today(goal_id)
        if current.day is None:
            raise HTTPException(status_code=409, detail="Today is not an execution day")
        if current.day_progress.completed_tasks != current.day_progress.total_tasks:
            raise HTTPException(status_code=409, detail="Tasks are not complete")
        existing = self.session.scalar(
            select(ProgressEvent.id).where(
                ProgressEvent.goal_id == goal.id,
                ProgressEvent.plan_day_id == current.day.id,
                ProgressEvent.event_type == "DAY_COMPLETED",
            )
        )
        if existing is None:
            self.session.add(
                ProgressEvent(
                    goal_id=goal.id,
                    plan_version_id=plan.id,
                    plan_day_id=current.day.id,
                    task_id=None,
                    event_type="DAY_COMPLETED",
                )
            )
            goal.last_active_at = utc_now()
            day = self.session.get(PlanDay, current.day.id)
            self._refresh_day_log(goal, plan, day)
            self.session.commit()
        return self.get_today(goal_id)

    def record_task_feedback(
        self,
        goal_id: str,
        task_id: str,
        payload: TaskFeedbackCreate,
    ) -> TodayWorkspace:
        goal, plan = self._goal_and_plan(goal_id)
        task = self.session.get(PlanTask, task_id)
        if task is None or task.plan_version_id != plan.id:
            raise HTTPException(status_code=404, detail="Task not found")
        if task.status == "COMPLETED":
            raise HTTPException(
                status_code=409,
                detail="Completed task cannot be marked as blocked",
            )
        day = self._require_today_plan_day(goal, plan.id, task.plan_day_id)
        status_by_reason = {
            "UNCLEAR": "PENDING_RESPONSE",
            "DO_NOT_WANT": "NEEDS_DECISION",
        }
        feedback = Feedback(
            goal_id=goal.id,
            plan_version_id=plan.id,
            plan_day_id=day.id,
            task_id=task.id,
            feedback_kind="TASK_BLOCKED",
            reason_code=payload.reason_code,
            intent_code=payload.intent_code,
            detail_text=payload.detail_text,
            status=status_by_reason.get(payload.reason_code, "PENDING_ADJUSTMENT"),
        )
        self.session.add(feedback)
        self.session.flush()
        if task.status != "BLOCKED":
            task.status = "BLOCKED"
            self.session.add(
                ProgressEvent(
                    goal_id=goal.id,
                    plan_version_id=plan.id,
                    plan_day_id=day.id,
                    task_id=task.id,
                    event_type="TASK_BLOCKED",
                    metadata_json={"reason_code": payload.reason_code},
                )
            )
        self._add_feedback_event(goal, plan, day, feedback)
        self._build_adjustment_candidate(
            goal,
            plan,
            day,
            feedback,
            task,
            remaining_minutes=payload.remaining_minutes,
        )
        self._refresh_day_log(goal, plan, day)
        goal.last_active_at = utc_now()
        self.session.commit()
        return self.get_today(goal_id)

    def record_day_pace_feedback(
        self,
        goal_id: str,
        payload: DayPaceFeedbackCreate,
    ) -> TodayWorkspace:
        goal, plan = self._goal_and_plan(goal_id)
        current = self.get_today(goal_id)
        if current.day is None:
            raise HTTPException(status_code=409, detail="Today is not an execution day")
        feedback = Feedback(
            goal_id=goal.id,
            plan_version_id=plan.id,
            plan_day_id=current.day.id,
            task_id=None,
            feedback_kind="DAY_PACE",
            reason_code=payload.reason_code,
            intent_code=payload.intent_code,
            detail_text=payload.detail_text,
            status="RECORDED",
        )
        self.session.add(feedback)
        self.session.flush()
        day = self.session.get(PlanDay, current.day.id)
        self._add_feedback_event(goal, plan, day, feedback)
        self._build_adjustment_candidate(goal, plan, day, feedback, None)
        self._refresh_day_log(goal, plan, day)
        goal.last_active_at = utc_now()
        self.session.commit()
        return self.get_today(goal_id)

    def delete_feedback(self, goal_id: str, feedback_id: str) -> TodayWorkspace:
        goal, plan = self._goal_and_plan(goal_id)
        if goal.status != "ACTIVE":
            raise HTTPException(status_code=409, detail="Plan has not started")
        feedback = self.session.get(Feedback, feedback_id)
        if feedback is None or feedback.goal_id != goal.id or feedback.plan_version_id != plan.id:
            raise HTTPException(status_code=404, detail="Feedback not found")

        day = self.session.get(PlanDay, feedback.plan_day_id)
        selected_date = self.session.scalar(
            select(PlanDaySchedule.scheduled_date).where(
                PlanDaySchedule.plan_day_id == feedback.plan_day_id
            )
        )
        task = self.session.get(PlanTask, feedback.task_id) if feedback.task_id else None
        if task is not None and task.status == "BLOCKED":
            other_feedback = self.session.scalar(
                select(Feedback.id).where(
                    Feedback.task_id == task.id,
                    Feedback.id != feedback.id,
                )
            )
            if other_feedback is None:
                task.status = "NOT_STARTED"

        self.session.delete(feedback)
        self.session.flush()
        self._refresh_day_log(goal, plan, day)
        goal.last_active_at = utc_now()
        self.session.commit()
        return self.get_today(goal_id, selected_date)

    def apply_adjustment(self, goal_id: str, adjustment_id: str) -> TodayWorkspace:
        goal, plan = self._goal_and_plan(goal_id)
        adjustment = self.session.get(FeedbackAdjustment, adjustment_id)
        if adjustment is None or adjustment.goal_id != goal.id:
            raise HTTPException(status_code=404, detail="Adjustment candidate not found")
        if adjustment.status != "PENDING_REVIEW":
            return self.get_today(goal_id)
        operation = adjustment.operation_json
        kind = operation.get("kind")
        task = (
            self.session.get(PlanTask, operation.get("task_id"))
            if operation.get("task_id")
            else None
        )
        origin_day = (
            self.session.get(PlanDay, operation.get("from_day_id"))
            if operation.get("from_day_id")
            else None
        )
        if kind == "MOVE_TASK" and task:
            task.plan_day_id = operation["to_day_id"]
            task.order_in_day = self._next_task_order(operation["to_day_id"])
            task.status = "NOT_STARTED"
        elif kind == "MOVE_TASK_REMAINDER" and task:
            can_split, reason = self._task_split_policy(plan, task)
            if not can_split:
                raise HTTPException(status_code=409, detail=reason)
            original_title = task.title
            task.title = f"{original_title}（已完成部分）"
            task.estimate_minutes = operation["completed_minutes"]
            task.status = "COMPLETED"
            self.session.add(
                PlanTask(
                    plan_version_id=plan.id,
                    plan_day_id=operation["to_day_id"],
                    source_task_id=task.id,
                    source_work_item_id=task.source_work_item_id,
                    work_item_window_id=task.work_item_window_id,
                    occurrence_index=task.occurrence_index,
                    materialized_at=task.materialized_at,
                    origin_stage_order=self._task_origin_stage(task),
                    lineage_task_id=task.lineage_task_id or self._task_lineage_key(task),
                    order_in_day=self._next_task_order(operation["to_day_id"]),
                    title=f"{original_title}（剩余部分）",
                    description="承接此前已经开始的任务，只完成尚未完成的部分。",
                    estimate_minutes=operation["remaining_minutes"],
                    priority=task.priority,
                    task_kind=task.task_kind,
                    is_required=task.is_required,
                    evidence_json=task.evidence_json,
                    depends_on_json=[],
                    satisfies_commitments_json=task.satisfies_commitments_json,
                    is_splittable=task.is_splittable,
                    scheduling_policy=task.scheduling_policy,
                    scheduling_policy_json=task.scheduling_policy_json,
                    status="NOT_STARTED",
                )
            )
        elif kind in {"CREATE_RECOVERY_DAY", "CREATE_RECOVERY_DAY_REMAINDER"} and task:
            if origin_day is None:
                raise HTTPException(status_code=409, detail="Source plan day is missing")
            activation = self._activation(plan.id)
            if activation is None:
                raise HTTPException(status_code=409, detail="Plan calendar is missing")
            recovery_date = date.fromisoformat(operation["target_date"])
            today = self._today_for(goal)
            if recovery_date <= today or recovery_date > today + timedelta(days=7):
                raise HTTPException(
                    status_code=409,
                    detail="Recovery day is outside the allowed window",
                )
            occupied = self.session.scalar(
                select(PlanDaySchedule.id).where(
                    PlanDaySchedule.activation_id == activation.id,
                    PlanDaySchedule.scheduled_date == recovery_date,
                )
            )
            if occupied is not None:
                raise HTTPException(
                    status_code=409,
                    detail="Recovery date is already an execution day",
                )
            next_day_index = (
                self.session.scalar(
                    select(func.coalesce(func.max(PlanDay.day_index), 0)).where(
                        PlanDay.plan_version_id == plan.id,
                        PlanDay.week_index == origin_day.week_index,
                    )
                )
                or 0
            ) + 1
            recovery_day = PlanDay(
                plan_version_id=plan.id,
                milestone_id=origin_day.milestone_id,
                stage_order=origin_day.stage_order,
                week_index=origin_day.week_index,
                day_index=next_day_index,
                primary_goal=f"补回：{task.title}",
                suggested_minutes=operation["remaining_minutes"],
                evidence_json=task.evidence_json,
                do_not_json=[],
            )
            self.session.add(recovery_day)
            self.session.flush()
            self.session.add(
                PlanDaySchedule(
                    activation_id=activation.id,
                    plan_day_id=recovery_day.id,
                    scheduled_date=recovery_date,
                )
            )
            original_title = task.title
            if kind == "CREATE_RECOVERY_DAY_REMAINDER":
                task.title = f"{original_title}（已完成部分）"
                task.estimate_minutes = operation["completed_minutes"]
                task.status = "COMPLETED"
                self.session.add(
                    PlanTask(
                        plan_version_id=plan.id,
                        plan_day_id=recovery_day.id,
                        source_task_id=task.id,
                        source_work_item_id=task.source_work_item_id,
                        work_item_window_id=task.work_item_window_id,
                        occurrence_index=task.occurrence_index,
                        materialized_at=task.materialized_at,
                        origin_stage_order=self._task_origin_stage(task),
                        lineage_task_id=task.lineage_task_id or self._task_lineage_key(task),
                        order_in_day=1,
                        title=f"{original_title}（剩余部分）",
                        description="承接此前已经开始的任务，只完成尚未完成的部分。",
                        estimate_minutes=operation["remaining_minutes"],
                        priority=task.priority,
                        task_kind=task.task_kind,
                        is_required=task.is_required,
                        evidence_json=task.evidence_json,
                        depends_on_json=[],
                        satisfies_commitments_json=task.satisfies_commitments_json,
                        is_splittable=task.is_splittable,
                        scheduling_policy=task.scheduling_policy,
                        scheduling_policy_json=task.scheduling_policy_json,
                        status="NOT_STARTED",
                    )
                )
            else:
                task.plan_day_id = recovery_day.id
                task.order_in_day = 1
                task.status = "NOT_STARTED"
        elif kind == "SPLIT_TASK" and task:
            can_split, reason = self._task_split_policy(plan, task)
            if not can_split:
                raise HTTPException(status_code=409, detail=reason)
            original_title = task.title
            task.title = f"{original_title}（基础部分）"
            task.description = f"{task.description}\n先只完成可独立验证的基础部分。"
            task.estimate_minutes = operation["first_minutes"]
            task.status = "NOT_STARTED"
            self.session.add(
                PlanTask(
                    plan_version_id=plan.id,
                    plan_day_id=operation["to_day_id"],
                    source_task_id=task.id,
                    source_work_item_id=task.source_work_item_id,
                    work_item_window_id=task.work_item_window_id,
                    occurrence_index=task.occurrence_index,
                    materialized_at=task.materialized_at,
                    origin_stage_order=self._task_origin_stage(task),
                    lineage_task_id=task.lineage_task_id or self._task_lineage_key(task),
                    order_in_day=self._next_task_order(operation["to_day_id"]),
                    title=f"{original_title}（后续部分）",
                    description="承接前一部分，处理剩余难点并完成一次检查。",
                    estimate_minutes=operation["second_minutes"],
                    priority=task.priority,
                    task_kind=task.task_kind,
                    is_required=task.is_required,
                    evidence_json=task.evidence_json,
                    depends_on_json=[],
                    satisfies_commitments_json=task.satisfies_commitments_json,
                    is_splittable=task.is_splittable,
                    scheduling_policy=task.scheduling_policy,
                    scheduling_policy_json=task.scheduling_policy_json,
                    status="NOT_STARTED",
                )
            )
        elif kind == "ADD_OPTIONAL_TASK":
            self.session.add(
                PlanTask(
                    plan_version_id=plan.id,
                    plan_day_id=operation["to_day_id"],
                    order_in_day=self._next_task_order(operation["to_day_id"]),
                    title=operation["title"],
                    description=operation["description"],
                    estimate_minutes=operation["minutes"],
                    priority=3,
                    task_kind=operation["task_kind"],
                    is_required=False,
                    evidence_json=[],
                    depends_on_json=[],
                    status="NOT_STARTED",
                )
            )
        feedback = self.session.get(Feedback, adjustment.feedback_id)
        adjustment.status = "APPLIED"
        adjustment.decided_at = utc_now()
        if feedback:
            feedback.status = "ADJUSTED"
        source_day = self.session.get(PlanDay, feedback.plan_day_id) if feedback else None
        self.session.add(
            ProgressEvent(
                goal_id=goal.id,
                plan_version_id=plan.id,
                plan_day_id=source_day.id,
                task_id=feedback.task_id if feedback else None,
                event_type="ADJUSTMENT_APPLIED",
                metadata_json={"adjustment_id": adjustment.id, "kind": adjustment.adjustment_kind},
            )
        )
        self.session.flush()
        if source_day:
            self._refresh_day_log(goal, plan, source_day)
        if task and kind in {
            "MOVE_TASK", "SPLIT_TASK", "CREATE_RECOVERY_DAY",
        }:
            for block in self.session.scalars(select(TaskTimeBlock).where(
                TaskTimeBlock.task_id == task.id,
                TaskTimeBlock.status == "PLANNED",
                TaskTimeBlock.scheduled_date >= self._today_for(goal),
            )):
                before = {"date": str(block.scheduled_date), "start": block.start_local,
                          "end": block.end_local, "plan_day_id": block.plan_day_id}
                scheduled_date = self.session.scalar(select(PlanDaySchedule.scheduled_date).where(
                    PlanDaySchedule.plan_day_id == task.plan_day_id,
                ))
                block.plan_day_id = task.plan_day_id
                if scheduled_date:
                    block.scheduled_date = scheduled_date
                block.status = "REQUIRES_REVIEW"
                block.revision += 1
                block.parallel_confirmed = False
                block.parallel_reason = None
                block.change_reason = "任务反馈调整后重新安排时间"
                self.session.add(ScheduleChange(
                    goal_id=goal.id, entity_type="TASK_TIME_BLOCK", entity_id=block.id,
                    change_kind="FEEDBACK_REVIEW", before_json=before,
                    after_json={"date": str(block.scheduled_date), "status": block.status},
                    reason=block.change_reason,
                ))
        allocate_plan_schedule(self.session, goal, plan.id)
        goal.last_active_at = utc_now()
        self.session.commit()
        return self.get_today(goal_id)

    def discard_adjustment(self, goal_id: str, adjustment_id: str) -> TodayWorkspace:
        goal, plan = self._goal_and_plan(goal_id)
        adjustment = self.session.get(FeedbackAdjustment, adjustment_id)
        if adjustment is None or adjustment.goal_id != goal.id:
            raise HTTPException(status_code=404, detail="Adjustment candidate not found")
        if adjustment.status == "PENDING_REVIEW":
            adjustment.status = "DISMISSED"
            adjustment.decided_at = utc_now()
            feedback = self.session.get(Feedback, adjustment.feedback_id)
            if feedback:
                feedback.status = "RECORDED"
                day = self.session.get(PlanDay, feedback.plan_day_id)
                self._refresh_day_log(goal, plan, day)
            self.session.commit()
        return self.get_today(goal_id)

    def _goal_and_plan(self, goal_id: str) -> tuple[Goal, PlanVersion]:
        goal = self.session.get(Goal, goal_id)
        if goal is None:
            raise HTTPException(status_code=404, detail="Goal not found")
        if not goal.active_plan_version_id:
            raise HTTPException(status_code=409, detail="Confirmed plan is required")
        plan = self.session.get(PlanVersion, goal.active_plan_version_id)
        if plan is None or plan.status != "CONFIRMED":
            raise HTTPException(status_code=409, detail="Confirmed plan is required")
        return goal, plan

    def _require_today_plan_day(
        self, goal: Goal, plan_id: str, plan_day_id: str
    ) -> PlanDay:
        activation = self._activation(plan_id)
        if activation is None:
            raise HTTPException(status_code=409, detail="Plan calendar is missing")
        plan = self.session.get(PlanVersion, plan_id)
        active_schedule = (
            self._active_schedule(plan, activation, self._today_for(goal)) if plan else None
        )
        schedule = self.session.scalar(
            select(PlanDaySchedule).where(
                PlanDaySchedule.activation_id == activation.id,
                PlanDaySchedule.plan_day_id == plan_day_id,
            )
        )
        day = self.session.get(PlanDay, plan_day_id)
        if (
            schedule is None
            or day is None
            or day.plan_version_id != plan_id
            or (
                schedule.scheduled_date != self._today_for(goal)
                and (active_schedule is None or active_schedule.id != schedule.id)
            )
        ):
            raise HTTPException(status_code=409, detail="Only today's tasks can receive feedback")
        return day

    def _active_schedule(
        self, plan: PlanVersion, activation: PlanActivation, target_date: date
    ) -> PlanDaySchedule | None:
        exact = self.session.scalar(
            select(PlanDaySchedule).where(
                PlanDaySchedule.activation_id == activation.id,
                PlanDaySchedule.scheduled_date == target_date,
            )
        )
        if plan.schedule_type != "OUTCOME":
            candidates = list(self.session.scalars(select(PlanDaySchedule).where(
                PlanDaySchedule.activation_id == activation.id,
                PlanDaySchedule.scheduled_date == target_date,
            ).order_by(PlanDaySchedule.id)))
            for candidate in candidates:
                if self.session.scalar(select(PlanTask.id).where(
                    PlanTask.plan_day_id == candidate.plan_day_id,
                    PlanTask.status.not_in(["COMPLETED", "CANCELLED", "CARRIED_OVER"]),
                )):
                    return candidate
            return exact
        schedules = list(
            self.session.scalars(
                select(PlanDaySchedule)
                .where(PlanDaySchedule.activation_id == activation.id)
                .order_by(PlanDaySchedule.scheduled_date)
            )
        )
        tasks_by_day: dict[str, list[PlanTask]] = {}
        for task in self._tasks(plan.id):
            tasks_by_day.setdefault(task.plan_day_id, []).append(task)
        unfinished = next(
            (
                item
                for item in schedules
                if any(
                    task.status not in {"COMPLETED", "CANCELLED", "CARRIED_OVER"}
                    for task in tasks_by_day.get(item.plan_day_id, [])
                )
            ),
            None,
        )
        return unfinished or exact

    def _add_feedback_event(self, goal, plan, day, feedback) -> None:
        self.session.add(
            ProgressEvent(
                goal_id=goal.id,
                plan_version_id=plan.id,
                plan_day_id=day.id,
                task_id=feedback.task_id,
                event_type="FEEDBACK_SUBMITTED",
                metadata_json={
                    "feedback_id": feedback.id,
                    "feedback_kind": feedback.feedback_kind,
                    "reason_code": feedback.reason_code,
                    "intent_code": feedback.intent_code,
                },
            )
        )

    def _build_adjustment_candidate(
        self,
        goal: Goal,
        plan: PlanVersion,
        day: PlanDay,
        feedback: Feedback,
        task: PlanTask | None,
        remaining_minutes: int | None = None,
    ) -> None:
        intent = feedback.intent_code or "OBSERVE"
        if intent in {"OBSERVE", "EXPLAIN_FIRST"}:
            feedback.status = "RECORDED" if intent == "OBSERVE" else "PENDING_RESPONSE"
            return
        if intent == "REVISIT_ROUTE":
            feedback.status = "NEEDS_GLOBAL_REVIEW"
            return

        work_minutes = (
            min(task.estimate_minutes, remaining_minutes or task.estimate_minutes) if task else 30
        )
        target_window_days = (
            7 if intent == "RESCHEDULE_ONLY" else (3 if intent == "LOWER_NEAR_TERM" else None)
        )
        target = self._future_target_day(
            goal,
            plan.id,
            day,
            work_minutes,
            max_calendar_days=target_window_days,
        )
        recovery_date = self._next_non_execution_date(goal, plan.id, max_calendar_days=7)
        operation: dict[str, object]
        title: str
        summary: str
        rationale: str
        impact: list[str]
        adjustment_kind = intent

        if (
            task
            and intent
            in {
                "RESCHEDULE_ONLY",
                "LOWER_NEAR_TERM",
            }
            and target
        ):
            partial = work_minutes < task.estimate_minutes
            operation = (
                {
                    "kind": "MOVE_TASK_REMAINDER",
                    "task_id": task.id,
                    "from_day_id": day.id,
                    "to_day_id": target.id,
                    "remaining_minutes": work_minutes,
                    "completed_minutes": task.estimate_minutes - work_minutes,
                }
                if partial
                else {
                    "kind": "MOVE_TASK",
                    "task_id": task.id,
                    "from_day_id": day.id,
                    "to_day_id": target.id,
                }
            )
            title = "把未完成任务分散到后续执行日"
            summary = (
                f"保留今天已完成的部分，只将“{task.title}”剩余约 {work_minutes} 分钟"
                f"移到 {self._day_code(target)}。"
                if partial
                else f"将“{task.title}”移到 {self._day_code(target)}，今天已完成内容保持不变。"
            )
            rationale = (
                "这是一次临时波动，只在未来七个自然日内寻找最近的正常执行日，"
                "不调用模型，也不改变整体路线和周期。"
                if intent == "RESCHEDULE_ONLY"
                else "只降低最近几天的负担，不调用模型，也不改变整体路线和周期。"
            )
            impact = [
                "今天已经完成的内容保持不变",
                f"{self._day_code(target)} 增加约 {work_minutes} 分钟",
                "不会移到七个自然日以外",
                "整体周期暂不改变",
            ]
        elif task and intent == "RECOVER_ON_REST_DAY" and recovery_date:
            partial = work_minutes < task.estimate_minutes
            operation = {
                "kind": "CREATE_RECOVERY_DAY_REMAINDER" if partial else "CREATE_RECOVERY_DAY",
                "task_id": task.id,
                "from_day_id": day.id,
                "target_date": recovery_date.isoformat(),
                "remaining_minutes": work_minutes,
                "completed_minutes": task.estimate_minutes - work_minutes,
            }
            target_label = recovery_date.strftime("%m月%d日")
            title = "在最近的非执行日安排一次补做"
            summary = (
                f"保留今天已完成的部分，只把“{task.title}”剩余约 {work_minutes} 分钟"
                f"放到 {target_label} 的临时补做日。"
                if partial
                else f"把“{task.title}”放到 {target_label} 的临时补做日。"
            )
            rationale = (
                "你选择短期补回；系统在未来七个自然日内找到最近的非执行日，"
                "只新增这一次补做，不调用模型，也不重排其他任务。"
            )
            impact = [
                "今天已经完成的内容保持不变",
                f"{target_label} 新增约 {work_minutes} 分钟补做内容",
                "其他执行日和整体周期保持不变",
            ]
        elif task and intent in {"RESCHEDULE_ONLY", "LOWER_NEAR_TERM"}:
            feedback.status = "RESCHEDULE_WINDOW_UNAVAILABLE"
            return
        elif task and intent == "RECOVER_ON_REST_DAY":
            feedback.status = "RECOVERY_DAY_UNAVAILABLE"
            return
        elif task and intent == "SPLIT_TASK" and target:
            first = max(10, task.estimate_minutes // 2)
            second = max(10, task.estimate_minutes - first)
            operation = {
                "kind": "SPLIT_TASK",
                "task_id": task.id,
                "to_day_id": target.id,
                "first_minutes": first,
                "second_minutes": second,
            }
            title = "把难任务拆成两次完成"
            summary = f"今天先做“{task.title}”的基础部分，{self._day_code(target)} 再处理剩余难点。"
            rationale = "拆小能降低单次理解压力，同时保留原任务目标。"
            impact = [
                f"今天任务缩短到约 {first} 分钟",
                f"{self._day_code(target)} 增加约 {second} 分钟",
                "不降低最终完成标准",
            ]
        elif task and intent in {"CHANGE_METHOD", "REDUCE_SCOPE", "SKIP_FOR_NOW"}:
            feedback.status = "NEEDS_GLOBAL_REVIEW"
            return
        elif not task and intent == "LOWER_NEAR_TERM":
            future_days = self._future_days(plan.id, goal)
            if len(future_days) < 2:
                feedback.status = "NEEDS_GLOBAL_REVIEW"
                return
            source = future_days[0]
            destination = self._best_target_day(future_days[1:])
            source_task = self.session.scalar(
                select(PlanTask)
                .where(
                    PlanTask.plan_version_id == plan.id,
                    PlanTask.plan_day_id == source.id,
                    PlanTask.status != "COMPLETED",
                )
                .order_by(PlanTask.is_required.asc(), PlanTask.estimate_minutes.desc())
            )
            if source_task is None:
                feedback.status = "RECORDED"
                return
            operation = {
                "kind": "MOVE_TASK",
                "task_id": source_task.id,
                "from_day_id": source.id,
                "to_day_id": destination.id,
            }
            title = "降低下一个执行日的负担"
            summary = (
                f"将 {self._day_code(source)} 的“{source_task.title}”分散到 "
                f"{self._day_code(destination)}，给近期内容留出更多消化时间。"
            )
            rationale = "先调整最近一次负荷，再观察后续难度，不因单日反馈直接放松整个计划。"
            impact = [
                f"{self._day_code(source)} 减少约 {source_task.estimate_minutes} 分钟",
                f"任务转移到 {self._day_code(destination)}",
                "整体路线和周期暂不改变",
            ]
        elif intent in {"DEEPEN", "MORE_PRACTICE"}:
            operation = {
                "kind": "ADD_OPTIONAL_TASK",
                "to_day_id": day.id,
                "title": "可选加深：再做一次迁移或应用",
                "description": (
                    "围绕今天的主目标增加一个小型变式、复述、练习或真实应用；完成到能验证理解即可。"
                ),
                "minutes": 25,
                "task_kind": "PRACTICE",
            }
            title = "为今天增加一个可选加深任务"
            summary = "增加约 25 分钟的可选练习；不完成也不会影响今天或整体进度。"
            rationale = "今天仍有余力，先局部加深，不据一次反馈就提高后续所有阶段难度。"
            impact = ["新增内容明确标为可选", "后续阶段保持原难度", "整体周期不改变"]
        elif intent == "ACCELERATE" and target:
            next_task = self.session.scalar(
                select(PlanTask)
                .where(
                    PlanTask.plan_version_id == plan.id,
                    PlanTask.plan_day_id == target.id,
                    PlanTask.status == "NOT_STARTED",
                )
                .order_by(PlanTask.is_required.desc(), PlanTask.order_in_day)
            )
            if next_task is None:
                feedback.status = "RECORDED"
                return
            operation = {
                "kind": "MOVE_TASK",
                "task_id": next_task.id,
                "from_day_id": target.id,
                "to_day_id": day.id,
            }
            title = "提前一个后续任务作为今日可继续项"
            summary = (
                f"把 {self._day_code(target)} 的“{next_task.title}”提前到今天；由你确认后再移动。"
            )
            rationale = "只提前一个任务来试探节奏，不根据单日表现压缩整个计划。"
            impact = [
                f"今天最多增加约 {next_task.estimate_minutes} 分钟",
                f"{self._day_code(target)} 相应减轻",
                "整体周期暂不改变",
            ]
        else:
            feedback.status = "RECORDED"
            return

        candidate = FeedbackAdjustment(
            feedback_id=feedback.id,
            goal_id=goal.id,
            plan_version_id=plan.id,
            adjustment_kind=adjustment_kind,
            title=title,
            summary=summary,
            rationale=rationale,
            impact_json=impact,
            operation_json=operation,
        )
        self.session.add(candidate)
        feedback.status = "PENDING_ADJUSTMENT"

    def _future_target_day(
        self,
        goal: Goal,
        plan_id: str,
        current_day: PlanDay,
        required_minutes: int,
        *,
        max_calendar_days: int | None = None,
    ) -> PlanDay | None:
        candidates: list[tuple[PlanDay, int]] = []
        today = self._today_for(goal)
        deadline = today + timedelta(days=max_calendar_days or 0)
        for candidate in self._future_days(plan_id, goal):
            if candidate.id == current_day.id:
                continue
            schedule_date = self.session.scalar(
                select(PlanDaySchedule.scheduled_date).where(
                    PlanDaySchedule.plan_day_id == candidate.id
                )
            )
            if max_calendar_days is not None and (
                schedule_date is None or schedule_date > deadline
            ):
                continue
            used = (
                self.session.scalar(
                    select(func.coalesce(func.sum(PlanTask.estimate_minutes), 0)).where(
                        PlanTask.plan_day_id == candidate.id,
                        PlanTask.status != "COMPLETED",
                    )
                )
                or 0
            )
            allowed_minutes = (
                round(candidate.suggested_minutes * 1.3)
                if max_calendar_days is not None
                else candidate.suggested_minutes
            )
            spare = max(0, allowed_minutes - int(used))
            candidates.append((candidate, spare))
        for candidate, spare in candidates:
            if spare >= required_minutes:
                return candidate
        if candidates:
            return max(candidates, key=lambda item: item[1])[0]
        return None

    def _next_non_execution_date(
        self,
        goal: Goal,
        plan_id: str,
        *,
        max_calendar_days: int,
    ) -> date | None:
        activation = self._activation(plan_id)
        if activation is None:
            return None
        today = self._today_for(goal)
        scheduled_dates = set(
            self.session.scalars(
                select(PlanDaySchedule.scheduled_date).where(
                    PlanDaySchedule.activation_id == activation.id,
                    PlanDaySchedule.scheduled_date > today,
                    PlanDaySchedule.scheduled_date <= today + timedelta(days=max_calendar_days),
                )
            )
        )
        excluded_dates = {date.fromisoformat(item) for item in activation.excluded_dates_json}
        holidays = HolidayCalendarService(self.session)
        for offset in range(1, max_calendar_days + 1):
            candidate = today + timedelta(days=offset)
            holiday = holidays.day(candidate)
            if (
                candidate not in scheduled_dates
                and candidate not in excluded_dates
                and not (holiday and holiday["kind"] == "HOLIDAY")
            ):
                return candidate
        return None

    def _future_days(self, plan_id: str, goal: Goal) -> list[PlanDay]:
        activation = self._activation(plan_id)
        if activation is None:
            return []
        today = self._today_for(goal)
        schedules = list(
            self.session.scalars(
                select(PlanDaySchedule)
                .where(
                    PlanDaySchedule.activation_id == activation.id,
                    PlanDaySchedule.scheduled_date > today,
                )
                .order_by(PlanDaySchedule.scheduled_date)
            )
        )
        return [
            day
            for schedule in schedules
            if (day := self.session.get(PlanDay, schedule.plan_day_id)) is not None
        ]

    @staticmethod
    def _progress_summary(label: str, progress: ProgressSummary) -> str:
        if progress.total_tasks == 0:
            return f"{label}还没有安排任务。"
        if progress.percentage == 100:
            return f"{label}的 {progress.total_tasks} 项任务已全部完成。"
        remaining = progress.total_tasks - progress.completed_tasks
        required_remaining = (
            progress.total_required_tasks - progress.completed_required_tasks
        )
        required_copy = (
            f"，其中 {required_remaining} 项为必做"
            if required_remaining > 0
            else ""
        )
        return (
            f"{label}已完成 {progress.completed_tasks}/{progress.total_tasks} 项，"
            f"还有 {remaining} 项{required_copy}。"
        )

    def _best_target_day(self, days: list[PlanDay]) -> PlanDay:
        def remaining_capacity(day: PlanDay) -> int:
            used = (
                self.session.scalar(
                    select(func.coalesce(func.sum(PlanTask.estimate_minutes), 0)).where(
                        PlanTask.plan_day_id == day.id,
                        PlanTask.status != "COMPLETED",
                    )
                )
                or 0
            )
            return day.suggested_minutes - int(used)

        return max(days, key=remaining_capacity)

    def _next_task_order(self, plan_day_id: str) -> int:
        current = self.session.scalar(
            select(func.coalesce(func.max(PlanTask.order_in_day), 0)).where(
                PlanTask.plan_day_id == plan_day_id
            )
        )
        return int(current or 0) + 1

    def _refresh_day_log(self, goal: Goal, plan: PlanVersion, day: PlanDay | None) -> None:
        if day is None:
            return
        schedule = self.session.scalar(
            select(PlanDaySchedule).where(PlanDaySchedule.plan_day_id == day.id)
        )
        if schedule is None:
            return
        tasks = list(self.session.scalars(select(PlanTask).where(PlanTask.plan_day_id == day.id)))
        feedbacks = list(
            self.session.scalars(select(Feedback).where(Feedback.plan_day_id == day.id))
        )
        completed = sum(task.status == "COMPLETED" for task in tasks)
        focus_seconds = int(
            self.session.scalar(
                select(func.coalesce(func.sum(FocusSession.actual_seconds), 0)).where(
                    FocusSession.plan_day_id == day.id
                )
            )
            or 0
        )
        applied = (
            list(
                self.session.scalars(
                    select(FeedbackAdjustment).where(
                        FeedbackAdjustment.feedback_id.in_([item.id for item in feedbacks]),
                        FeedbackAdjustment.status == "APPLIED",
                    )
                )
            )
            if feedbacks
            else []
        )
        day_completed = self.session.scalar(
            select(ProgressEvent.id).where(
                ProgressEvent.plan_day_id == day.id,
                ProgressEvent.event_type == "DAY_COMPLETED",
            )
        )
        status = "COMPLETED" if day_completed else "IN_PROGRESS"
        summary = f"已完成 {completed}/{len(tasks)} 项任务，记录 {len(feedbacks)} 条反馈"
        if focus_seconds:
            summary += f"，专注 {round(focus_seconds / 60)} 分钟"
        log = self.session.scalar(
            select(ExecutionDayLog).where(ExecutionDayLog.plan_day_id == day.id)
        )
        if log is None:
            log = ExecutionDayLog(
                goal_id=goal.id,
                plan_version_id=plan.id,
                plan_day_id=day.id,
                scheduled_date=schedule.scheduled_date,
                status=status,
                summary=summary,
                metrics_json={},
                adjustments_json=[],
            )
            self.session.add(log)
        log.status = status
        log.summary = summary
        log.metrics_json = {
            "completed_tasks": completed,
            "total_tasks": len(tasks),
            "feedback_count": len(feedbacks),
            "focus_seconds": focus_seconds,
        }
        log.adjustments_json = [item.summary for item in applied]
        log.updated_at = utc_now()

    def _apply_automatic_completions(
        self,
        goal: Goal,
        plan: PlanVersion,
        day: PlanDay | None,
    ) -> None:
        if day is None:
            return
        day_tasks = [
            task
            for task in self._tasks(plan.id)
            if task.plan_day_id == day.id and task.status != "CARRIED_OVER"
        ]
        if day_tasks and all(task.status in {"COMPLETED", "CANCELLED"} for task in day_tasks):
            existing = self.session.scalar(
                select(ProgressEvent.id).where(
                    ProgressEvent.goal_id == goal.id,
                    ProgressEvent.plan_day_id == day.id,
                    ProgressEvent.event_type == "DAY_COMPLETED",
                )
            )
            if existing is None:
                self.session.add(
                    ProgressEvent(
                        goal_id=goal.id,
                        plan_version_id=plan.id,
                        plan_day_id=day.id,
                        task_id=None,
                        event_type="DAY_COMPLETED",
                    )
                )
                self.session.flush()

        plan_tasks = [task for task in self._tasks(plan.id) if task.status != "CARRIED_OVER"]
        if not plan_tasks or not all(
            task.status in {"COMPLETED", "CANCELLED"} for task in plan_tasks
        ):
            return
        if self._has_unplanned_exploration(plan) or self._has_pending_required_triggers(plan):
            return
        feedback_count = int(
            self.session.scalar(
                select(func.count(Feedback.id)).where(Feedback.plan_version_id == plan.id)
            )
            or 0
        )
        adjustment_count = int(
            self.session.scalar(
                select(func.count(FeedbackAdjustment.id)).where(
                    FeedbackAdjustment.plan_version_id == plan.id,
                    FeedbackAdjustment.status == "APPLIED",
                )
            )
            or 0
        )
        goal.status = "COMPLETED"
        goal.pause_until = None
        goal.completed_at = utc_now()
        goal.completion_summary = (
            f"计划中的 {len(plan_tasks)} 项任务已全部完成。期间记录 {feedback_count} 条反馈，"
            f"采用 {adjustment_count} 次调整。"
        )

    @staticmethod
    def _has_unplanned_exploration(plan: PlanVersion) -> bool:
        # Finishing a materialized window does not finish its unexpanded route.
        return plan.planning_mode == "EXPLORATORY" and plan.next_unplanned_week <= plan.total_weeks

    def _exploration_state(
        self, plan: PlanVersion, tasks: list[PlanTask]
    ) -> ExplorationExecutionStateRead | None:
        if plan.planning_mode != "EXPLORATORY":
            return None
        pending = self.session.scalar(
            select(PlanWindow).where(
                PlanWindow.goal_id == plan.goal_id,
                PlanWindow.status == "DRAFT",
                PlanWindow.trigger == "EXECUTION_FEEDBACK",
            ).order_by(PlanWindow.created_at.desc()).limit(1)
        )
        return ExplorationExecutionStateRead(
            generated_through_week=min(plan.total_weeks, plan.next_unplanned_week - 1),
            total_weeks=plan.total_weeks,
            next_unplanned_week=(plan.next_unplanned_week if self._has_unplanned_exploration(plan) else None),
            current_window_completed=bool(tasks) and all(
                task.status in {"COMPLETED", "CANCELLED", "CARRIED_OVER"} for task in tasks
            ),
            pending_window_start_week=pending.start_week if pending else None,
            pending_window_end_week=pending.end_week if pending else None,
        )

    def _has_pending_required_triggers(self, plan: PlanVersion) -> bool:
        """A required gated action is still work even before a task card exists."""
        return self.session.scalar(select(WorkItemWindow.id).join(
            PlanActivation, WorkItemWindow.activation_id == PlanActivation.id).join(
            PlanWorkItem, WorkItemWindow.work_item_id == PlanWorkItem.id).where(
                PlanActivation.plan_version_id == plan.id,
                WorkItemWindow.status == 'PENDING',
                PlanWorkItem.binding_mode == 'TRIGGERED',
                PlanWorkItem.is_required.is_(True),
                PlanWorkItem.status.in_(['DRAFT', 'ACTIVE']),
            ).limit(1)) is not None

    @staticmethod
    def _day_code(day: PlanDay) -> str:
        return f"M{day.stage_order}-W{day.week_index}-D{day.day_index}"

    def _build_preview(self, plan, payload) -> ExecutionPreview:
        self._validate_activation_deadline(plan, payload)
        if plan.execution_model_version == ExecutionModelVersion.HYBRID_WORK_ITEM and self._has_daily_plan(plan):
            from types import SimpleNamespace
            days = list(self.session.scalars(select(PlanDay).where(PlanDay.plan_version_id == plan.id)))
            tasks = self._tasks(plan.id)
            milestones = list(self.session.scalars(select(PlanMilestone).where(PlanMilestone.plan_version_id == plan.id)))
            try:
                validate_action_coverage(list(self.session.scalars(select(PlanWorkItem).where(
                    PlanWorkItem.plan_version_id == plan.id))),
                    [SimpleNamespace(week_index=day.week_index, tasks=[task for task in tasks if task.plan_day_id == day.id])
                     for day in days], through_week=max(day.week_index for day in days),
                    milestone_weeks={None: (1, plan.total_weeks),
                        **{row.stage_order: (row.start_week, row.end_week) for row in milestones}})
            except WorkItemValidationError as error:
                raise HTTPException(409, {"code": error.code, "message": str(error)}) from error
        if (
            plan.execution_model_version == ExecutionModelVersion.HYBRID_WORK_ITEM
            and not self._has_daily_plan(plan)
        ):
            goal = self.session.get(Goal, plan.goal_id)
            preview = self._work_item_materializer(plan, payload).preview(goal.id, plan, payload)
            self._validate_activation_deadline(plan, payload, preview.projected_end_date)
            return preview
        if (
            not payload.smart_preview_id
            and not any(item.get("available_days_per_week") for item in plan.temporal_segments_json or [])
            and len(payload.execution_weekdays) != plan.weekly_execution_days
        ):
            raise HTTPException(
                status_code=422,
                detail=f"Please select exactly {plan.weekly_execution_days} execution weekdays",
            )
        mapped = self._mapped_days(plan, payload)
        if not mapped:
            raise HTTPException(status_code=409, detail="Plan has no execution days")
        self._validate_activation_deadline(plan, payload, mapped[-1].scheduled_date)
        return ExecutionPreview(
            requested_start_date=payload.requested_start_date,
            first_execution_date=mapped[0].scheduled_date,
            projected_end_date=mapped[-1].scheduled_date,
            execution_weekdays=payload.execution_weekdays,
            excluded_dates=payload.excluded_dates,
            total_execution_days=len(mapped),
            upcoming_days=mapped[:10],
        )

    def _validate_activation_deadline(self, plan, payload, projected_end=None):
        goal = self.session.get(Goal, plan.goal_id)
        requirement = (self.session.get(RequirementVersion, goal.current_requirement_version_id)
                       if goal.current_requirement_version_id else None)
        if requirement is None:
            return
        try:
            deadline = snapshot_deadline(requirement.snapshot_json, timezone=goal.timezone,
                                         fallback_time=requirement.created_at, require_resolved=True)
        except ValueError as error:
            raise HTTPException(409, {"code": "PLAN_DEADLINE_UNRESOLVED", "message": str(error)}) from error
        if deadline and (deadline < self._today_for(goal) or payload.requested_start_date > deadline
                         or (projected_end and projected_end > deadline)):
            raise HTTPException(409, {"code": "PLAN_START_DEADLINE_CONFLICT",
                "message": f"按当前开始日安排会超过原截止日期{deadline}。请先讨论原期限内重排，或确认调整期限/可用时间；任务不会被删除。",
                "deadline": deadline.isoformat(), "current_date": self._today_for(goal).isoformat(),
                "projected_end_date": projected_end.isoformat() if projected_end else None})

    def _authored_temporal_constraints(self, plan, payload):
        if plan.execution_model_version != ExecutionModelVersion.HYBRID_WORK_ITEM:
            return {}
        items = list(self.session.scalars(select(PlanWorkItem).where(PlanWorkItem.plan_version_id == plan.id)))
        materializer = self._work_item_materializer(plan, payload)
        selected = materializer._selected_item_ids(plan.id, items, plan_selection=payload.selected_work_item_ids)
        by_key = {item.item_key: item for item in items}
        by_id = {item.id: item for item in items}
        dependencies = list(self.session.scalars(select(WorkItemDependency).where(WorkItemDependency.plan_version_id == plan.id)))
        result = {}
        for task in self._tasks(plan.id):
            keys = (task.scheduling_policy_json or {}).get("work_item_keys") or []
            primary = by_id.get(task.source_work_item_id) or (by_key.get(keys[0]) if keys else None)
            if primary and (primary.id not in selected or primary.binding_mode == "TRIGGERED"):
                continue
            related = [by_key[key] for key in keys if key in by_key]
            if primary and primary not in related:
                related.append(primary)
            for item in related:
                temporal = materializer.resolved_temporal(plan, item, dependencies)
                if not temporal:
                    continue
                start, end = materializer._date_range(plan, payload, temporal)
                result.setdefault(task.plan_day_id, []).append((item.title, start, end, temporal))
        return result

    @staticmethod
    def _authored_date_allowed(day_id, value, constraints):
        return all(start <= value <= end
                   and (not temporal.get("allowed_weekdays") or value.weekday() in temporal["allowed_weekdays"])
                   and value.isoformat() not in {str(v) for v in temporal.get("excluded_dates") or []}
                   for _title, start, end, temporal in constraints.get(day_id, []))

    def _has_daily_plan(self, plan: PlanVersion) -> bool:
        return bool(
            self.session.scalar(
                select(func.count(PlanDay.id)).where(PlanDay.plan_version_id == plan.id)
            )
        )

    def _work_item_materializer(
        self, plan: PlanVersion, payload: ExecutionScheduleRequest
    ) -> WorkItemMaterializer:
        holidays = HolidayCalendarService(self.session)
        holiday_execution_dates = self._holiday_execution_dates(
            plan, holidays, payload.requested_start_date
        )

        def date_allowed(value: date) -> bool:
            calendar_day = holidays.day(value)
            if not calendar_day or calendar_day["kind"] == "WORKDAY":
                return True
            return value in holiday_execution_dates

        return WorkItemMaterializer(self.session, date_allowed=date_allowed)

    def _mapped_days(self, plan, payload) -> list[ScheduledDayRead]:
        constraints = self._authored_temporal_constraints(plan, payload)
        if payload.smart_preview_id:
            from planner_api.execution_calendar_service import ExecutionCalendarService

            goal = self.session.get(Goal, plan.goal_id)
            mapped = ExecutionCalendarService(self.session).resolve_smart_input(goal, plan, payload)
            if any(not self._authored_date_allowed(day.plan_day_id, day.scheduled_date, constraints) for day in mapped):
                raise HTTPException(409, {"code": "PLAN_ITEM_DATE_CONFLICT", "message": "日历与已确认的任务日期约束冲突，请重新预览或讨论调整。"})
            return mapped
        days = list(
            self.session.scalars(
                select(PlanDay)
                .where(PlanDay.plan_version_id == plan.id)
                .order_by(PlanDay.week_index, PlanDay.day_index)
            )
        )
        excluded = set(payload.excluded_dates)
        holidays = HolidayCalendarService(self.session)
        execution_weekdays = set(payload.execution_weekdays)
        holiday_execution_dates = self._holiday_execution_dates(
            plan, holidays, payload.requested_start_date
        )
        cursor = payload.requested_start_date
        result: list[ScheduledDayRead] = []
        # New quantified stage contracts use real calendar weeks. Preserve the
        # historical sequential mapping for plans without this contract.
        phased = any(item.get("available_days_per_week") for item in plan.temporal_segments_json or [])
        if phased:
            for week in sorted({day.week_index for day in days}):
                anchor, end = plan_week_range(plan, payload.requested_start_date, week)
                eligible = [anchor + timedelta(days=index) for index in range((end - anchor).days + 1)
                            if self._is_plan_execution_date(anchor + timedelta(days=index),
                                execution_weekdays, excluded, holidays, holiday_execution_dates)]
                week_days = [day for day in days if day.week_index == week]
                if len(eligible) < len(week_days):
                    raise HTTPException(409, {"code": "PLAN_STAGE_CALENDAR_CAPACITY_INVALID",
                        "message": f"第{week}周选定日期不足以承接本阶段安排，请调整可用日期或讨论阶段期限。"})
                def choose(index, after, week_days=week_days, eligible=eligible):
                    if index == len(week_days):
                        return []
                    preferred = index * len(eligible) // len(week_days)
                    options = sorted(enumerate(eligible), key=lambda pair: abs(pair[0] - preferred))
                    for _position, value in options:
                        if after is not None and value <= after:
                            continue
                        if not self._authored_date_allowed(week_days[index].id, value, constraints):
                            continue
                        rest = choose(index + 1, value)
                        if rest is not None:
                            return [self._scheduled_read(week_days[index], value), *rest]
                    return None
                selected_days = choose(0, None)
                if selected_days is None:
                    raise HTTPException(409, {"code": "PLAN_ITEM_DATE_CONFLICT",
                        "message": f"第{week}周所选日期与任务已确认的星期、期限或窗口冲突，请选择符合条件的日期或讨论调整。"})
                result.extend(selected_days)
            return result
        for day in days:
            limit = payload.requested_start_date + timedelta(days=plan.total_weeks * 7 + 366)
            while not (self._is_plan_execution_date(cursor, execution_weekdays, excluded, holidays, holiday_execution_dates)
                       and self._authored_date_allowed(day.id, cursor, constraints)):
                cursor += timedelta(days=1)
                if cursor > limit:
                    raise HTTPException(409, {"code": "PLAN_ITEM_DATE_CONFLICT", "message": "所选日期无法满足已确认的任务窗口，请重新选择或讨论调整。"})
            result.append(self._scheduled_read(day, cursor))
            cursor += timedelta(days=1)
        return result

    @staticmethod
    def _holiday_policy_text(plan: PlanVersion) -> str:
        """Return planning text used to detect an explicit holiday exception.

        The model's free-form plan already records holiday intent.  Keeping this
        interpretation here means old plans continue to work without a schema
        migration, while plans that say “pick one day to rest” can execute on
        the remaining official-holiday dates.
        """
        parts = [
            plan.source_document or "",
            plan.summary or "",
            json.dumps(plan.temporal_segments_json or [], ensure_ascii=False),
        ]
        return "\n".join(parts)

    def _holiday_execution_dates(
        self, plan: PlanVersion, holidays: HolidayCalendarService, start: date
    ) -> set[date]:
        text = self._holiday_policy_text(plan)
        markers = ("挑一天休息", "其中一天休息", "只休一天", "一日休息", "只挑一天")
        days = holidays.days(start, start + timedelta(days=370))
        holiday_days = [item for item in days if item.get("kind") == "HOLIDAY"]
        execution_dates: set[date] = set()
        # Each holiday group is interpreted independently.  This matters when
        # a plan works through National Day but keeps another holiday fully off.
        groups: list[tuple[str, list[date]]] = []
        for item in holiday_days:
            value = date.fromisoformat(str(item["date"]))
            name = str(item.get("local_name") or item.get("name") or "")
            if (
                groups
                and name == groups[-1][0]
                and value == groups[-1][1][-1] + timedelta(days=1)
            ):
                groups[-1][1].append(value)
            else:
                groups.append((name, [value]))
        for name, group in groups:
            aliases = {name, name.removesuffix("节"), name.removesuffix("假期")}
            aliases.discard("")
            occurrences = [
                (index, alias)
                for alias in aliases
                for index in range(len(text))
                if text.startswith(alias, index)
            ]
            selective = any(
                marker in text[max(0, index - 80) : index + len(alias) + 160]
                for index, alias in occurrences
                for marker in markers
            )
            if selective:
                # The first day is a deterministic default rest day.  All other
                # dates in this explicitly named holiday group stay executable.
                execution_dates.update(group[1:])
        return execution_dates

    @staticmethod
    def _is_plan_execution_date(
        value: date,
        execution_weekdays: set[int],
        excluded_dates: set[date],
        holidays: HolidayCalendarService,
        holiday_execution_dates: set[date],
    ) -> bool:
        if value in excluded_dates:
            return False
        calendar_day = holidays.day(value)
        if calendar_day and calendar_day["kind"] == "WORKDAY":
            return True
        if calendar_day and calendar_day["kind"] == "HOLIDAY":
            return (
                value in holiday_execution_dates
                and value.weekday() in execution_weekdays
            )
        return value.weekday() in execution_weekdays

    def _activation_preview(self, activation, plan) -> ExecutionPreview:
        schedules = list(
            self.session.scalars(
                select(PlanDaySchedule)
                .where(PlanDaySchedule.activation_id == activation.id)
                .order_by(PlanDaySchedule.scheduled_date)
            )
        )
        mapped = [
            self._scheduled_read(self.session.get(PlanDay, item.plan_day_id), item.scheduled_date)
            for item in schedules
        ]
        return ExecutionPreview(
            requested_start_date=activation.requested_start_date,
            first_execution_date=mapped[0].scheduled_date,
            projected_end_date=activation.projected_end_date,
            execution_weekdays=activation.execution_weekdays_json,
            excluded_dates=[date.fromisoformat(item) for item in activation.excluded_dates_json],
            total_execution_days=len(mapped),
            upcoming_days=mapped[:10],
        )

    def _build_recovery_shift_preview(
        self, plan: PlanVersion, activation: PlanActivation, anchor: date
    ) -> dict[str, object]:
        days = {
            item.id: item
            for item in self.session.scalars(
                select(PlanDay).where(PlanDay.plan_version_id == plan.id)
            )
        }
        schedules = list(
            self.session.scalars(
                select(PlanDaySchedule)
                .where(PlanDaySchedule.activation_id == activation.id)
                .order_by(PlanDaySchedule.scheduled_date)
            )
        )
        tasks_by_day: dict[str, list[PlanTask]] = {}
        for task in self._tasks(plan.id):
            tasks_by_day.setdefault(task.plan_day_id, []).append(task)
        locked_days = self._recovery_locked_day_ids(plan.id)
        locked_tasks = {task.id for day_id in locked_days for task in tasks_by_day.get(day_id, [])}
        missed = [
            schedule
            for schedule in schedules
            if schedule.scheduled_date < anchor
            and any(
                task.status not in {"COMPLETED", "CANCELLED", "CARRIED_OVER"}
                for task in tasks_by_day.get(schedule.plan_day_id, [])
            )
        ]
        if not missed:
            raise HTTPException(status_code=409, detail="No missed schedule needs recovery")
        first_missed_date = min(item.scheduled_date for item in missed)
        units: list[tuple[str, PlanDaySchedule, list[PlanTask]]] = []
        for schedule in schedules:
            if schedule.scheduled_date < first_missed_date:
                continue
            pending = [
                task
                for task in tasks_by_day.get(schedule.plan_day_id, [])
                if task.status not in {"COMPLETED", "CANCELLED", "CARRIED_OVER"}
            ]
            if not pending:
                continue
            kind = "CARRYOVER" if schedule.plan_day_id in locked_days else "MOVE"
            units.append((kind, schedule, pending))

        weekdays = set(activation.execution_weekdays_json)
        excluded = {date.fromisoformat(item) for item in activation.excluded_dates_json}
        holidays = HolidayCalendarService(self.session)
        holiday_execution_dates = self._holiday_execution_dates(plan, holidays, anchor)
        occupied = {
            schedule.scheduled_date for schedule in schedules if schedule.plan_day_id in locked_days
        }
        cursor = anchor
        changes: list[dict[str, object]] = []
        carryovers: list[dict[str, object]] = []
        for kind, schedule, pending in units:
            while (
                not self._is_plan_execution_date(
                    cursor, weekdays, excluded, holidays, holiday_execution_dates
                )
                or cursor in occupied
            ):
                cursor += timedelta(days=1)
            day = days[schedule.plan_day_id]
            if kind == "MOVE":
                changes.append(
                    {
                        "plan_day_id": day.id,
                        "code": f"M{day.stage_order}-W{day.week_index}-D{day.day_index}",
                        "primary_goal": day.primary_goal,
                        "from_date": schedule.scheduled_date.isoformat(),
                        "to_date": cursor.isoformat(),
                    }
                )
            else:
                carryovers.append(
                    {
                        "source_day_id": day.id,
                        "source_date": schedule.scheduled_date.isoformat(),
                        "to_date": cursor.isoformat(),
                        "task_ids": [task.id for task in pending],
                    }
                )
            occupied.add(cursor)
            cursor += timedelta(days=1)
        projected_dates = [
            date.fromisoformat(str(item["to_date"])) for item in [*changes, *carryovers]
        ]
        unchanged_dates = [
            schedule.scheduled_date for schedule in schedules if schedule.plan_day_id in locked_days
        ]
        projected_end = max([*projected_dates, *unchanged_dates])
        return {
            "anchor_date": anchor.isoformat(),
            "original_end_date": activation.projected_end_date.isoformat(),
            "projected_end_date": projected_end.isoformat(),
            "missed_execution_days": len(missed),
            "locked_day_ids": sorted(locked_days),
            "locked_task_ids": sorted(locked_tasks),
            "carryover_task_ids": sorted(
                task_id for item in carryovers for task_id in item["task_ids"]
            ),
            "changes": changes,
            "carryovers": carryovers,
            "content_changed": False,
            "model_called": False,
            "reason_codes": [
                "MISSED_EXECUTION",
                *( ["HISTORY_LOCKED"] if locked_days else [] ),
            ],
        }

    def _build_recovery_catchup_preview(
        self,
        plan: PlanVersion,
        activation: PlanActivation,
        anchor: date,
        include_non_execution_days: bool,
        selected_task_ids: list[str] | None = None,
    ) -> dict[str, object]:
        from planner_api.recovery_scope import build_scope

        scope = build_scope(self, plan, activation, anchor, selected_task_ids)
        selected = set(scope["selected_task_ids"])
        schedules = list(
            self.session.scalars(
                select(PlanDaySchedule)
                .where(PlanDaySchedule.activation_id == activation.id)
                .order_by(PlanDaySchedule.scheduled_date)
            )
        )
        tasks_by_day: dict[str, list[PlanTask]] = {}
        for task in self._tasks(plan.id):
            tasks_by_day.setdefault(task.plan_day_id, []).append(task)
        missed = []
        backlog_tasks: list[PlanTask] = []
        for schedule in schedules:
            pending = [
                task
                for task in tasks_by_day.get(schedule.plan_day_id, [])
                if task.status not in {"COMPLETED", "CANCELLED", "CARRIED_OVER"}
                and task.id in selected
            ]
            if schedule.scheduled_date < anchor and pending:
                missed.append(schedule)
                backlog_tasks.extend(pending)
        if not missed:
            raise HTTPException(status_code=409, detail="No missed schedule needs recovery")

        future = [
            item
            for item in schedules
            if anchor <= item.scheduled_date <= activation.projected_end_date
        ]
        scheduled_dates = {item.scheduled_date for item in schedules}
        excluded = {date.fromisoformat(item) for item in activation.excluded_dates_json}
        holidays = HolidayCalendarService(self.session)
        holiday_execution_dates = self._holiday_execution_dates(plan, holidays, anchor)
        free_dates = []
        cursor = anchor
        while cursor <= activation.projected_end_date:
            if (
                cursor not in scheduled_dates
                and cursor not in excluded
                and (
                    (holiday := holidays.day(cursor)) is None
                    or holiday["kind"] != "HOLIDAY"
                    or (
                        cursor in holiday_execution_dates
                    )
                )
            ):
                free_dates.append(cursor)
            cursor += timedelta(days=1)

        backlog_minutes = sum(task.estimate_minutes for task in backlog_tasks)
        required_extra = (
            (backlog_minutes + len(future) - 1) // len(future) if future else backlog_minutes
        )
        baseline = max(1, plan.weekly_minutes // max(1, plan.weekly_execution_days))
        phased = any(item.get("available_days_per_week") for item in plan.temporal_segments_json or [])
        def date_capacity(value):
            return calendar_capacity(plan, activation.requested_start_date, value)
        if phased:
            baseline = max(1, int(date_capacity(anchor)["daily_minutes"] or baseline))
        # Recovery is supervisor-led: absorb backlog near the anchor first,
        # with a bounded temporary increase instead of reshaping the whole plan.
        extra_execution_capacity = max(30, round(baseline * 0.35))
        candidates = []
        for schedule in future:
            original_minutes = sum(
                task.estimate_minutes
                for task in tasks_by_day.get(schedule.plan_day_id, [])
                if task.status not in {"COMPLETED", "CARRIED_OVER"}
            )
            candidates.append(
                {
                    "scheduled_date": schedule.scheduled_date,
                    "day_type": "EXECUTION",
                    "plan_day_id": schedule.plan_day_id,
                    "original_minutes": original_minutes,
                    # capacity is the temporary load we can add on top of the
                    # day's existing unfinished work. The old implementation
                    # treated this as the full daily capacity and could create
                    # overloaded recovery days.
                    "capacity": (int(date_capacity(schedule.scheduled_date)["daily_minutes"] or baseline)
                                 if phased else original_minutes + extra_execution_capacity),
                    "tasks": [],
                }
            )
        if include_non_execution_days:
            for free_date in free_dates:
                candidates.append(
                    {
                        "scheduled_date": free_date,
                        "day_type": "NON_EXECUTION",
                        "plan_day_id": None,
                        "original_minutes": 0,
                        "capacity": (int(date_capacity(free_date)["daily_minutes"] or baseline)
                                     if phased else baseline),
                        "tasks": [],
                    }
                )
        candidates.sort(
            key=lambda item: (
                item["scheduled_date"],
                0 if item["day_type"] == "EXECUTION" else 1,
            )
        )

        weekly_remaining, used_dates = {}, {}
        if phased:
            for item in candidates:
                value = item["scheduled_date"]
                week = max(1, plan_week_index(plan, activation.requested_start_date, value))
                item["capacity_week"] = week
                weekly_remaining.setdefault(week, int(date_capacity(value)["weekly_minutes"] or 0))
                used_dates.setdefault(week, set())
            for schedule in schedules:
                week = max(1, plan_week_index(plan, activation.requested_start_date, schedule.scheduled_date))
                if week in weekly_remaining:
                    reserved = sum(task.estimate_minutes for task in tasks_by_day.get(schedule.plan_day_id, [])
                                   if task.status not in {"CANCELLED", "CARRIED_OVER"}
                                   and task.id not in {row.id for row in backlog_tasks})
                    weekly_remaining[week] -= reserved
                    if reserved:
                        used_dates[week].add(schedule.scheduled_date)

        # Recovery must respect the same dependency ledger as Today smart
        # allocation.  A dependent task is eligible only after its resolved
        # prerequisite is completed or has already been placed earlier in
        # this catch-up preview.  Unknown/incomplete prerequisites remain
        # unallocated so the AI/user can explicitly decide how to proceed.
        dependency_allocator = DayAllocator(self.session)
        dependency_indexes = dependency_allocator._dependency_indexes(plan.id)
        pending_by_id = {task.id: task for task in backlog_tasks}
        assigned_ids: set[str] = set()
        assigned_dates: dict[str, date] = {}
        unallocated: list[PlanTask] = []
        remaining = set(pending_by_id)

        def ready(task: PlanTask) -> bool:
            dependencies = task.depends_on_json or []
            for dependency in dependencies:
                resolved = dependency_allocator._resolve_dependency(
                    dependency, task, dependency_indexes
                )
                if resolved is None:
                    return False
                if resolved.status in {"COMPLETED", "CARRIED_OVER"}:
                    continue
                if resolved.id in pending_by_id:
                    if resolved.id not in assigned_ids:
                        return False
                    continue
                return False
            return True

        # Repeatedly take the earliest source-order task whose prerequisites
        # are ready. This is a stable topological order for normal plans.
        while remaining:
            eligible = sorted(
                (pending_by_id[task_id] for task_id in remaining if ready(pending_by_id[task_id])),
                key=lambda item: (item.priority, item.order_in_day, item.id),
            )
            if not eligible:
                unallocated.extend(
                    sorted(
                        (pending_by_id[task_id] for task_id in remaining),
                        key=lambda item: (item.priority, item.order_in_day, item.id),
                    )
                )
                break
            task = eligible[0]
            dependency_dates = [
                assigned_dates[resolved.id]
                for dependency in (task.depends_on_json or [])
                if (
                    resolved := dependency_allocator._resolve_dependency(
                        dependency, task, dependency_indexes
                    )
                ) is not None
                and resolved.id in assigned_dates
            ]
            earliest_allowed = max(dependency_dates, default=anchor)
            target = next(
                (
                    item
                    for item in candidates
                    if item["scheduled_date"] >= earliest_allowed
                    if not phased or (
                        weekly_remaining[item["capacity_week"]] >= task.estimate_minutes
                        and (item["scheduled_date"] in used_dates[item["capacity_week"]]
                             or len(used_dates[item["capacity_week"]]) < date_capacity(item["scheduled_date"])["days"]))
                    if item["original_minutes"]
                    + sum(value.estimate_minutes for value in item["tasks"])
                    + task.estimate_minutes
                    <= item["capacity"]
                ),
                None,
            )
            if target is None:
                unallocated.append(task)
            else:
                target["tasks"].append(task)
                if phased:
                    weekly_remaining[target["capacity_week"]] -= task.estimate_minutes
                    used_dates[target["capacity_week"]].add(target["scheduled_date"])
                assigned_ids.add(task.id)
                assigned_dates[task.id] = target["scheduled_date"]
            remaining.remove(task.id)

        complexity_requires_model = backlog_minutes > baseline * 8 or any(
            task.estimate_minutes > max(extra_execution_capacity, baseline)
            for task in backlog_tasks
        )
        programmatic_feasible = not unallocated and not complexity_requires_model
        used_candidates = [item for item in candidates if item["tasks"]]
        execution_assignments = [
            {"task_id": task.id, "target_day_id": item["plan_day_id"]}
            for item in used_candidates
            if item["day_type"] == "EXECUTION"
            for task in item["tasks"]
        ]
        missed_date_by_day = {
            schedule.plan_day_id: schedule.scheduled_date.isoformat() for schedule in missed
        }
        non_execution_assignments = []
        for item in used_candidates:
            if item["day_type"] != "NON_EXECUTION":
                continue
            tasks = item["tasks"]
            source_task = tasks[0]
            non_execution_assignments.append(
                {
                    "source_day_id": source_task.plan_day_id,
                    "source_date": missed_date_by_day[source_task.plan_day_id],
                    "to_date": item["scheduled_date"].isoformat(),
                    "task_ids": [task.id for task in tasks],
                }
            )
        allocation = [
            {
                "scheduled_date": item["scheduled_date"].isoformat(),
                "day_type": item["day_type"],
                "added_tasks": len(item["tasks"]),
                "added_minutes": sum(task.estimate_minutes for task in item["tasks"]),
                "original_minutes": item["original_minutes"],
                "total_minutes": item["original_minutes"]
                + sum(task.estimate_minutes for task in item["tasks"]),
            }
            for item in used_candidates
        ]
        return {
            "anchor_date": anchor.isoformat(),
            "original_end_date": activation.projected_end_date.isoformat(),
            "missed_dates": [item.scheduled_date.isoformat() for item in missed],
            "backlog_tasks": len(backlog_tasks),
            "backlog_minutes": backlog_minutes,
            "recovery_scope": scope,
            "backlog_task_ids": [item.id for item in backlog_tasks],
            "unallocated_task_ids": [item.id for item in unallocated],
            "complexity_requires_model": complexity_requires_model,
            "remaining_execution_days": len(future),
            "available_non_execution_days": len(free_dates),
            "required_extra_minutes_per_day": required_extra,
            "include_non_execution_days": include_non_execution_days,
            "programmatic_feasible": programmatic_feasible,
            "model_required": not programmatic_feasible,
            "backlog_cleared_date": (
                max(item["scheduled_date"] for item in used_candidates).isoformat()
                if programmatic_feasible and used_candidates
                else None
            ),
            "allocation": allocation if programmatic_feasible else [],
            "non_execution_assignments": (
                non_execution_assignments if programmatic_feasible else []
            ),
            "execution_assignments": execution_assignments if programmatic_feasible else [],
            "locked_day_ids": sorted(self._recovery_locked_day_ids(plan.id)),
            "locked_task_ids": [],
            "reason_codes": [
                "MISSED_EXECUTION",
                "DEADLINE_PROTECTION",
                *( ["CAPACITY_INSUFFICIENT"] if not programmatic_feasible else [] ),
                *( ["HISTORY_LOCKED"] if self._recovery_locked_day_ids(plan.id) else [] ),
                *( ["CONSTRAINT_CHANGE"] if include_non_execution_days else [] ),
            ],
        }

    def _recovery_locked_day_ids(self, plan_id: str) -> set[str]:
        locked = set(
            self.session.scalars(
                select(PlanTask.plan_day_id).where(
                    PlanTask.plan_version_id == plan_id,
                    PlanTask.status.in_(["IN_PROGRESS", "COMPLETED"]),
                )
            )
        )
        for model in (ProgressEvent, Feedback, FocusSession, ExecutionDayLog):
            locked.update(
                self.session.scalars(
                    select(model.plan_day_id).where(model.plan_version_id == plan_id)
                )
            )
        return locked

    def _clone_plan_for_recovery(
        self, goal: Goal, current: PlanVersion, preview: dict[str, object]
    ) -> PlanVersion:
        activation = self._activation(current.id)
        if activation is None:
            raise HTTPException(status_code=409, detail="Plan calendar is missing")
        keeps_deadline = preview.get("mode") == "KEEP_DEADLINE"
        revised = PlanVersion(
            goal_id=goal.id,
            route_version_id=current.route_version_id,
            version=current.version,
            revision=current.revision + 1,
            status="CONFIRMED",
            summary=current.summary,
            total_weeks=current.total_weeks,
            weekly_execution_days=current.weekly_execution_days,
            weekly_minutes=current.weekly_minutes,
            total_estimated_minutes=0,
            buffer_strategy_json=current.buffer_strategy_json,
            assumptions_json=current.assumptions_json,
            open_questions_json=current.open_questions_json,
            change_summary_json=[
                (
                    "在原结束日期内补回未完成内容，历史记录保持原日期。"
                    if keeps_deadline
                    else "从当前日期接续未完成安排，历史记录保持原日期。"
                )
            ],
            temporal_segments_json=current.temporal_segments_json,
            commitments_json=current.commitments_json,
            feedback="保持结束日期的追赶安排" if keeps_deadline else "整体顺延恢复",
            prompt_version="recovery-catchup-v1" if keeps_deadline else "recovery-shift-v1",
            source_document=current.source_document,
            compiled_snapshot_json=current.compiled_snapshot_json,
            compiler_report_json=current.compiler_report_json,
            planning_mode=current.planning_mode,
            schedule_type=current.schedule_type,
            execution_model_version=current.execution_model_version,
            exploration_window_weeks=current.exploration_window_weeks,
            next_unplanned_week=current.next_unplanned_week,
            confirmed_at=utc_now(),
        )
        self.session.add(revised)
        self.session.flush()
        milestone_map: dict[str, PlanMilestone] = {}
        for old in self.session.scalars(
            select(PlanMilestone)
            .where(PlanMilestone.plan_version_id == current.id)
            .order_by(PlanMilestone.stage_order)
        ):
            new = PlanMilestone(
                plan_version_id=revised.id,
                route_stage_id=old.route_stage_id,
                stage_order=old.stage_order,
                title=old.title,
                purpose=old.purpose,
                start_week=old.start_week,
                end_week=old.end_week,
                outputs_json=old.outputs_json,
            )
            self.session.add(new)
            self.session.flush()
            milestone_map[old.id] = new

        carryover_ids = set(preview.get("carryover_task_ids", []))
        day_map: dict[str, PlanDay] = {}
        task_map: dict[str, PlanTask] = {}
        max_day_index: dict[int, int] = {}
        old_days = list(
            self.session.scalars(
                select(PlanDay)
                .where(PlanDay.plan_version_id == current.id)
                .order_by(PlanDay.week_index, PlanDay.day_index)
            )
        )
        for old_day in old_days:
            max_day_index[old_day.week_index] = max(
                max_day_index.get(old_day.week_index, 0), old_day.day_index
            )
            new_day = PlanDay(
                plan_version_id=revised.id,
                milestone_id=milestone_map[old_day.milestone_id].id,
                stage_order=old_day.stage_order,
                week_index=old_day.week_index,
                day_index=old_day.day_index,
                primary_goal=old_day.primary_goal,
                suggested_minutes=old_day.suggested_minutes,
                evidence_json=old_day.evidence_json,
                do_not_json=old_day.do_not_json,
            )
            self.session.add(new_day)
            self.session.flush()
            day_map[old_day.id] = new_day
            for old_task in self.session.scalars(
                select(PlanTask)
                .where(PlanTask.plan_day_id == old_day.id)
                .order_by(PlanTask.order_in_day)
            ):
                new_task = PlanTask(
                    plan_version_id=revised.id,
                    plan_day_id=new_day.id,
                    source_task_id=old_task.source_task_id,
                    source_work_item_id=old_task.source_work_item_id,
                    work_item_window_id=old_task.work_item_window_id,
                    occurrence_index=old_task.occurrence_index,
                    materialization_key=remap_materialization_key(
                        old_task.materialization_key, revised.id
                    ),
                    materialized_at=old_task.materialized_at,
                    origin_stage_order=old_task.origin_stage_order or old_day.stage_order,
                    lineage_task_id=old_task.lineage_task_id or self._task_lineage_key(old_task),
                    order_in_day=old_task.order_in_day,
                    title=old_task.title,
                    description=old_task.description,
                    estimate_minutes=old_task.estimate_minutes,
                    priority=old_task.priority,
                    task_kind=old_task.task_kind,
                    is_required=old_task.is_required,
                    evidence_json=old_task.evidence_json,
                    depends_on_json=old_task.depends_on_json,
                    satisfies_commitments_json=old_task.satisfies_commitments_json,
                    ai_schedule_requested=old_task.ai_schedule_requested,
                    is_splittable=old_task.is_splittable,
                    scheduling_policy=old_task.scheduling_policy,
                    scheduling_policy_json=old_task.scheduling_policy_json,
                    status=("CARRIED_OVER" if old_task.id in carryover_ids else old_task.status),
                )
                self.session.add(new_task)
                self.session.flush()
                task_map[old_task.id] = new_task

        carryover_days: list[tuple[PlanDay, date]] = []
        old_day_by_id = {item.id: item for item in old_days}
        # Do not preserve unfinished schedules in the past. They are handled
        # by the explicit carryover/redistribution records in the preview.
        for raw in preview.get("carryovers", []):
            item = dict(raw)
            source_day = old_day_by_id[str(item["source_day_id"])]
            max_day_index[source_day.week_index] += 1
            tasks = [self.session.get(PlanTask, str(task_id)) for task_id in item["task_ids"]]
            tasks = [task for task in tasks if task is not None]
            carry_day = PlanDay(
                plan_version_id=revised.id,
                milestone_id=milestone_map[source_day.milestone_id].id,
                stage_order=source_day.stage_order,
                week_index=source_day.week_index,
                day_index=max_day_index[source_day.week_index],
                primary_goal=f"接续完成：{source_day.primary_goal}",
                suggested_minutes=sum(task.estimate_minutes for task in tasks),
                evidence_json=source_day.evidence_json,
                do_not_json=source_day.do_not_json,
            )
            self.session.add(carry_day)
            self.session.flush()
            for order, old_task in enumerate(tasks, start=1):
                self.session.add(
                    PlanTask(
                        plan_version_id=revised.id,
                        plan_day_id=carry_day.id,
                        source_task_id=old_task.id,
                        source_work_item_id=old_task.source_work_item_id,
                        work_item_window_id=old_task.work_item_window_id,
                        occurrence_index=old_task.occurrence_index,
                        materialized_at=old_task.materialized_at,
                        origin_stage_order=old_task.origin_stage_order or source_day.stage_order,
                        lineage_task_id=old_task.lineage_task_id
                        or self._task_lineage_key(old_task),
                        order_in_day=order,
                        title=old_task.title,
                        description=old_task.description,
                        estimate_minutes=old_task.estimate_minutes,
                        priority=old_task.priority,
                        task_kind=old_task.task_kind,
                        is_required=old_task.is_required,
                        evidence_json=old_task.evidence_json,
                        depends_on_json=old_task.depends_on_json,
                        satisfies_commitments_json=old_task.satisfies_commitments_json,
                        ai_schedule_requested=old_task.ai_schedule_requested,
                        is_splittable=old_task.is_splittable,
                        scheduling_policy=old_task.scheduling_policy,
                        scheduling_policy_json=old_task.scheduling_policy_json,
                    )
                )
            carryover_days.append((carry_day, date.fromisoformat(str(item["to_date"]))))

        next_order_by_day = {
            old_day.id: max(
                (
                    task.order_in_day
                    for task in self._tasks(current.id)
                    if task.plan_day_id == old_day.id
                ),
                default=0,
            )
            for old_day in old_days
        }
        for raw in preview.get("redistributions", []):
            item = dict(raw)
            old_task = self.session.get(PlanTask, str(item["task_id"]))
            target_old_day_id = str(item["target_day_id"])
            if old_task is None or target_old_day_id not in day_map:
                continue
            target_day = day_map[target_old_day_id]
            next_order_by_day[target_old_day_id] += 1
            target_day.suggested_minutes += old_task.estimate_minutes
            self.session.add(
                PlanTask(
                    plan_version_id=revised.id,
                    plan_day_id=target_day.id,
                    # Point to the copied task in this revision.  This keeps
                    # the original task and its recovery copy in one lineage
                    # group instead of counting the work twice.
                    source_task_id=task_map.get(old_task.id, old_task).id,
                    source_work_item_id=old_task.source_work_item_id,
                    work_item_window_id=old_task.work_item_window_id,
                    occurrence_index=old_task.occurrence_index,
                    materialized_at=old_task.materialized_at,
                    origin_stage_order=(
                        old_task.origin_stage_order or old_day_by_id[target_old_day_id].stage_order
                    ),
                    lineage_task_id=old_task.lineage_task_id or self._task_lineage_key(old_task),
                    order_in_day=next_order_by_day[target_old_day_id],
                    title=f"补回：{old_task.title}",
                    description=old_task.description,
                    estimate_minutes=old_task.estimate_minutes,
                    priority=old_task.priority,
                    task_kind=old_task.task_kind,
                    is_required=old_task.is_required,
                    evidence_json=old_task.evidence_json,
                    depends_on_json=old_task.depends_on_json,
                    satisfies_commitments_json=old_task.satisfies_commitments_json,
                    ai_schedule_requested=old_task.ai_schedule_requested,
                    is_splittable=old_task.is_splittable,
                    scheduling_policy=old_task.scheduling_policy,
                    scheduling_policy_json=old_task.scheduling_policy_json,
                )
            )

        self.session.flush()
        moved_dates = {
            str(item["plan_day_id"]): date.fromisoformat(str(item["to_date"]))
            for item in preview.get("changes", [])
        }
        new_activation = PlanActivation(
            goal_id=goal.id,
            plan_version_id=revised.id,
            requested_start_date=activation.requested_start_date,
            projected_end_date=date.fromisoformat(str(preview["projected_end_date"])),
            execution_weekdays_json=activation.execution_weekdays_json,
            excluded_dates_json=activation.excluded_dates_json,
        )
        self.session.add(new_activation)
        self.session.flush()
        old_schedules = list(
            self.session.scalars(
                select(PlanDaySchedule).where(PlanDaySchedule.activation_id == activation.id)
            )
        )
        for old_schedule in old_schedules:
            self.session.add(
                PlanDaySchedule(
                    activation_id=new_activation.id,
                    plan_day_id=day_map[old_schedule.plan_day_id].id,
                    scheduled_date=moved_dates.get(
                        old_schedule.plan_day_id, old_schedule.scheduled_date
                    ),
                )
            )
        for carry_day, scheduled_date in carryover_days:
            self.session.add(
                PlanDaySchedule(
                    activation_id=new_activation.id,
                    plan_day_id=carry_day.id,
                    scheduled_date=scheduled_date,
                )
            )

        self._migrate_recovery_records(current.id, revised.id, day_map, task_map)
        if current.execution_model_version == ExecutionModelVersion.HYBRID_WORK_ITEM:
            from planner_api.work_item_projection import clone_work_item_projection
            clone_work_item_projection(self.session, current, revised, activation, new_activation)
        revised.total_estimated_minutes = sum(
            task.estimate_minutes
            for task in self.session.scalars(
                select(PlanTask).where(
                    PlanTask.plan_version_id == revised.id,
                    PlanTask.status != "CARRIED_OVER",
                )
            )
        )
        return revised

    def _migrate_recovery_records(
        self,
        source_plan_id: str,
        result_plan_id: str,
        day_map: dict[str, PlanDay],
        task_map: dict[str, PlanTask],
    ) -> None:
        for model in (ProgressEvent, Feedback, FocusSession, ExecutionDayLog):
            records = list(
                self.session.scalars(select(model).where(model.plan_version_id == source_plan_id))
            )
            for record in records:
                record.plan_version_id = result_plan_id
                record.plan_day_id = day_map[record.plan_day_id].id
                if hasattr(record, "task_id") and record.task_id:
                    replacement = task_map.get(record.task_id)
                    record.task_id = replacement.id if replacement else None
        for adjustment in self.session.scalars(
            select(FeedbackAdjustment).where(FeedbackAdjustment.plan_version_id == source_plan_id)
        ):
            adjustment.plan_version_id = result_plan_id

    @staticmethod
    def _recovery_preview_read(operation: RecoveryOperation) -> RecoveryShiftPreviewRead:
        value = operation.preview_json
        return RecoveryShiftPreviewRead(
            time_preview=value.get("time_preview"),
            id=operation.id,
            status=operation.status,
            mode="SHIFT_FORWARD",
            source_plan_version_id=operation.source_plan_version_id,
            result_plan_version_id=operation.result_plan_version_id,
            anchor_date=date.fromisoformat(str(value["anchor_date"])),
            original_end_date=date.fromisoformat(str(value["original_end_date"])),
            projected_end_date=date.fromisoformat(str(value["projected_end_date"])),
            missed_execution_days=int(value["missed_execution_days"]),
            locked_day_ids=list(value["locked_day_ids"]),
            locked_task_ids=list(value["locked_task_ids"]),
            carryover_task_ids=list(value["carryover_task_ids"]),
            changes=[RecoveryDayChangeRead.model_validate(item) for item in value["changes"]],
            content_changed=bool(value["content_changed"]),
            model_called=bool(value["model_called"]),
            reason_codes=list(value.get("reason_codes", [])),
        )

    @staticmethod
    def _recovery_catchup_preview_read(
        operation: RecoveryOperation,
    ) -> RecoveryCatchupPreviewRead:
        value = operation.preview_json
        return RecoveryCatchupPreviewRead(
            recovery_scope=value.get("recovery_scope"),
            time_preview=value.get("time_preview"),
            id=operation.id,
            status=operation.status,
            mode="KEEP_DEADLINE",
            source_plan_version_id=operation.source_plan_version_id,
            result_plan_version_id=operation.result_plan_version_id,
            anchor_date=date.fromisoformat(str(value["anchor_date"])),
            original_end_date=date.fromisoformat(str(value["original_end_date"])),
            missed_dates=[date.fromisoformat(str(item)) for item in value["missed_dates"]],
            backlog_tasks=int(value["backlog_tasks"]),
            backlog_minutes=int(value["backlog_minutes"]),
            remaining_execution_days=int(value["remaining_execution_days"]),
            available_non_execution_days=int(value["available_non_execution_days"]),
            required_extra_minutes_per_day=int(value["required_extra_minutes_per_day"]),
            include_non_execution_days=bool(value["include_non_execution_days"]),
            programmatic_feasible=bool(value["programmatic_feasible"]),
            model_required=bool(value["model_required"]),
            backlog_cleared_date=(
                date.fromisoformat(str(value["backlog_cleared_date"]))
                if value["backlog_cleared_date"]
                else None
            ),
            allocation=[
                RecoveryCatchupDayLoadRead.model_validate(item) for item in value["allocation"]
            ],
            reason_codes=list(value.get("reason_codes", [])),
        )

    @staticmethod
    def _suggested_weekdays(count: int) -> list[int]:
        return list(range(count))

    def _activation(self, plan_id: str) -> PlanActivation | None:
        return self.session.scalar(
            select(PlanActivation).where(PlanActivation.plan_version_id == plan_id)
        )

    def _tasks(self, plan_id: str) -> list[PlanTask]:
        return list(
            self.session.scalars(
                select(PlanTask)
                .where(PlanTask.plan_version_id == plan_id)
                .order_by(PlanTask.plan_day_id, PlanTask.order_in_day)
            )
        )

    @staticmethod
    def _scheduled_read(day: PlanDay, scheduled_date: date) -> ScheduledDayRead:
        return ScheduledDayRead(
            plan_day_id=day.id,
            stage_order=day.stage_order,
            week_index=day.week_index,
            day_index=day.day_index,
            code=f"M{day.stage_order}-W{day.week_index}-D{day.day_index}",
            scheduled_date=scheduled_date,
            primary_goal=day.primary_goal,
        )

    def _day_read(self, day: PlanDay, tasks: list[PlanTask]) -> PlanDayRead:
        plan = self.session.get(PlanVersion, day.plan_version_id)
        return PlanDayRead(
            id=day.id,
            stage_order=day.stage_order,
            week_index=day.week_index,
            day_index=day.day_index,
            primary_goal=day.primary_goal,
            suggested_minutes=day.suggested_minutes,
            evidence=day.evidence_json,
            do_not=day.do_not_json,
            tasks=[
                plan_task_read(item).model_copy(update={
                    "blocked_reason": self._hybrid_predecessor_blocked_reason(plan, item)
                    if plan and item.status != "COMPLETED" else None,
                })
                for item in tasks
            ],
        )

    def _tasks_for_stage(self, tasks: list[PlanTask], stage_order: int | None) -> list[PlanTask]:
        if stage_order is None:
            return []
        return [task for task in tasks if self._task_origin_stage(task) == stage_order]

    def _task_origin_stage(self, task: PlanTask) -> int | None:
        """Return the immutable stage of a work unit, across revisions."""
        if task.origin_stage_order is not None:
            return task.origin_stage_order
        current = task
        visited: set[str] = set()
        while current.source_task_id and current.source_task_id not in visited:
            visited.add(current.id)
            parent = self.session.get(PlanTask, current.source_task_id)
            if parent is None:
                break
            current = parent
            if current.origin_stage_order is not None:
                return current.origin_stage_order
        day = self.session.get(PlanDay, current.plan_day_id)
        return day.stage_order if day else None

    def _task_lineage_key(self, task: PlanTask) -> str:
        """Collapse archival/carryover rows to one stable work-unit identity."""
        if task.lineage_task_id:
            return task.lineage_task_id
        current = task
        visited: set[str] = set()
        while current.source_task_id and current.source_task_id not in visited:
            visited.add(current.id)
            parent = self.session.get(PlanTask, current.source_task_id)
            if parent is None:
                break
            current = parent
        return current.id

    def _progress(self, tasks: list[PlanTask]) -> ProgressSummary:
        # CARRIED_OVER rows are historical ledger entries.  Their actionable
        # replacement is the only row that contributes to current progress.
        active = [item for item in tasks if item.status != "CARRIED_OVER"]
        groups: dict[str, list[PlanTask]] = {}
        for task in active:
            groups.setdefault(self._task_lineage_key(task), []).append(task)

        completed = total = completed_required = total_required = 0
        for members in groups.values():
            required = any(item.is_required for item in members)
            total += 1
            if all(item.status in {"COMPLETED", "CANCELLED"} for item in members):
                completed += 1
            if required:
                total_required += 1
                if all(item.status in {"COMPLETED", "CANCELLED"} for item in members):
                    completed_required += 1

        percentage = round(completed / total * 100) if total else 0
        return ProgressSummary(
            completed_tasks=completed,
            total_tasks=total,
            completed_required_tasks=completed_required,
            total_required_tasks=total_required,
            percentage=min(100, max(0, percentage)),
        )

    @staticmethod
    def _feedback_read(feedback: Feedback) -> FeedbackRead:
        return FeedbackRead(
            id=feedback.id,
            task_id=feedback.task_id,
            feedback_kind=feedback.feedback_kind,
            reason_code=feedback.reason_code,
            intent_code=feedback.intent_code,
            detail_text=feedback.detail_text,
            status=feedback.status,
            created_at=feedback.created_at,
        )

    @staticmethod
    def _adjustment_read(adjustment: FeedbackAdjustment) -> FeedbackAdjustmentRead:
        return FeedbackAdjustmentRead(
            id=adjustment.id,
            feedback_id=adjustment.feedback_id,
            adjustment_kind=adjustment.adjustment_kind,
            status=adjustment.status,
            title=adjustment.title,
            summary=adjustment.summary,
            rationale=adjustment.rationale,
            impact=adjustment.impact_json,
            created_at=adjustment.created_at,
            decided_at=adjustment.decided_at,
        )

    @staticmethod
    def _day_log_read(day_log: ExecutionDayLog) -> ExecutionDayLogRead:
        return ExecutionDayLogRead(
            id=day_log.id,
            scheduled_date=day_log.scheduled_date,
            status=day_log.status,
            summary=day_log.summary,
            metrics=day_log.metrics_json,
            adjustments=day_log.adjustments_json,
            updated_at=day_log.updated_at,
        )

    @staticmethod
    def _focus_session_read(session: FocusSession) -> FocusSessionRead:
        return FocusSessionRead(
            id=session.id,
            task_id=session.task_id,
            task_title=session.task_title,
            planned_minutes=session.planned_minutes,
            actual_seconds=session.actual_seconds,
            interruptions=session.interruptions,
            status=session.status,
            completed_at=session.completed_at,
        )

    @staticmethod
    def _quick_capture_read(capture: QuickCapture) -> QuickCaptureRead:
        return QuickCaptureRead(
            id=capture.id,
            content=capture.content,
            status=capture.status,
            created_at=capture.created_at,
        )
