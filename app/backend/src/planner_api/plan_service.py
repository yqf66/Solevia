from __future__ import annotations

import hashlib
import json
import logging
import re
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta
from math import ceil
from types import SimpleNamespace
from threading import Lock
from zoneinfo import ZoneInfo

from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from planner_api.ai import ClarificationProviderError, ProviderUnavailableError
from planner_api.config import get_settings
from planner_api.model_telemetry import submit_model_task, business_check
from planner_api.conversation_memory import refresh_phase_summary, require_clarification_analysis_complete
from planner_api.day_allocation import DayAllocator
from planner_api.execution_projection import plan_task_read
from planner_api.generation_orchestration import GenerationOrchestrator
from planner_api.goal_error_memory import persist_explicit_error_memories
from planner_api.holiday_calendar import HolidayCalendarService
from planner_api.models import (
    AvailabilityException,
    AvailabilityWindow,
    ContextPack,
    Conversation,
    ConversationSummary,
    ExecutionDayLog,
    Feedback,
    FeedbackAdjustment,
    FocusSession,
    GenerationBatch,
    Goal,
    ManualTask,
    Message,
    PlanActivation,
    PlanAdjustmentPreview,
    PlanDay,
    PlanDaySchedule,
    PlanGenerationJob,
    PlanMilestone,
    PlanningBrief,
    PlanSchedulePreference,
    PlanTask,
    PlanVersion,
    PlanWindow,
    PlanWorkItem,
    PlanWorkItemGroup,
    ProgressEvent,
    RequirementVersion,
    ResearchRun,
    RouteStage,
    RouteVersion,
    TaskTimeBlock,
    WorkItemDependency,
    WorkItemWindow,
    WorkspaceAvailabilityWindow,
    WorkspaceScheduleProfile,
    utc_now,
)
from planner_api.plan_adjustment_apply_prompts import (
    PLAN_ADJUSTMENT_APPLY_PROMPT_VERSION,
)
from planner_api.plan_adjustment_prompts import PLAN_ADJUSTMENT_PROMPT_VERSION
from planner_api.plan_ai import PlanContext, PlanProvider
from planner_api.plan_capacity import capacity_assessment, capacity_conflicts
from planner_api.plan_compiler_repair import repair_bookkeeping
from planner_api.temporal_contract import snapshot_deadline
from planner_api.plan_calendar import calendar_weeks, plan_week_range
from planner_api.plan_rule_integrity import quantified_workloads, validate_action_coverage, validate_recurrence_contracts
from planner_api.plan_discussion_ai import (
    PLAN_DISCUSSION_PROMPT_VERSION,
    PlanDiscussionContext,
    PlanDiscussionProvider,
)
from planner_api.plan_prompts import HYBRID_PLAN_PROMPT_VERSION, PLAN_PROMPT_VERSION
from planner_api.planning_approach import recommend_planning_approach
from planner_api.planning_context import build_planning_context_snapshot
from planner_api.recovery_allocation_prompts import RECOVERY_ALLOCATION_PROMPT_VERSION
from planner_api.research.context import compile_research_context
from planner_api.research.persistence import ResearchRunService
from planner_api.research.schemas import ResearchPacket
from planner_api.schedule_reconciliation import (
    allocate_plan_schedule,
    changed_schedule_dates,
    mark_plan_schedule_review,
)
from planner_api.schemas import (
    ContextPackRead,
    PlanGenerationRepairRead,
    ErrorAvoidanceMemoryCandidate,
    GenerationArtifactEnvelope,
    GenerationBatchCreate,
    GenerationBatchRead,
    HybridPlanOutlineResult,
    HybridPlanDetailResult,
    MessageRead,
    PlanAdjustmentApplyResult,
    PlanAdjustmentPreviewRead,
    PlanAdjustmentProposal,
    PlanDayDraft,
    PlanDayRead,
    PlanDetailResult,
    PlanGenerationJobRead,
    PlanMilestoneRead,
    PlanningBriefRead,
    PlanRecoveryGuardRead,
    PlanResearchRead,
    PlanRevisionRead,
    PlanRevisionResult,
    PlanVersionRead,
    PlanWindowRead,
    PlanWorkItemGroupRead,
    PlanWorkItemRead,
    PlanWorkspace,
    RecoveryAllocationResult,
    WorkItemWindowRead,
)
from planner_api.work_item_domain import (
    ExecutionModelVersion,
    hybrid_rollout_enabled,
    PlanWorkItemDraft,
    PlanWorkItemGroupDraft,
    WorkItemDependencyDraft,
    remap_materialization_key,
)
from planner_api.work_item_validation import (
    WorkItemValidationError,
    validate_work_item_batch,
)

logger = logging.getLogger(__name__)

_plan_generation_locks: defaultdict[str, Lock] = defaultdict(Lock)
RECOVERY_END_DATE_GRACE_DAYS = 3
RECOVERY_LOCAL_WINDOW_RATIO = 0.30
RECOVERY_LOCAL_WINDOW_MIN_DAYS = 2
RECOVERY_LOCAL_WINDOW_MAX_DAYS = 10
RECOVERY_ALLOCATION_VALIDATION_RETRIES = 2
ADJUSTMENT_VALIDATION_RETRIES = 2
PLAN_DETAIL_BUSINESS_VALIDATION_RETRIES = 2
PLAN_AUTOMATIC_REPAIR_ROUNDS = 3
PLAN_AUTOMATIC_REPAIR_WEEK_BUDGET = 12
PLAN_DEPENDENCY_EXPANSION_WEEK_LIMIT = 4
PLAN_WEEK_ESTIMATE_TOLERANCE = 1.10
PLAN_DAY_ESTIMATE_TOLERANCE = 1.25


class PlanService:
    detail_parallelism = 3

    @staticmethod
    def _today_for(goal: Goal) -> date:
        return datetime.now(ZoneInfo(goal.timezone)).date()

    def _today_for_goal_id(self, goal_id: str) -> date:
        goal = self.session.get(Goal, goal_id)
        if goal is None:
            raise HTTPException(status_code=404, detail="Goal not found")
        return self._today_for(goal)

    def __init__(
        self,
        session: Session,
        provider: PlanProvider,
        discussion_provider: PlanDiscussionProvider,
        orchestrator: GenerationOrchestrator | None = None,
        enable_hybrid_work_items: bool = False,
        hybrid_rollout_mode: str = "OFF",
        hybrid_rollout_percentage: int = 0,
    ) -> None:
        self.session = session
        self.provider = provider
        self.discussion_provider = discussion_provider
        self.orchestrator = orchestrator or GenerationOrchestrator(session)
        self.enable_hybrid_work_items = enable_hybrid_work_items
        self.hybrid_rollout_mode = hybrid_rollout_mode
        self.hybrid_rollout_percentage = hybrid_rollout_percentage

    def get_workspace(self, goal_id: str) -> PlanWorkspace:
        goal, requirement, route = self._get_inputs(goal_id)
        plan = self._current_plan(goal_id)
        generated_weeks = self._generated_weeks(plan.id) if plan else set()
        milestone_count = len(self._milestones(plan.id)) if plan else 0
        generation_target = self._generation_target(plan) if plan else 0
        if plan is None or milestone_count == 0:
            phase = "OUTLINE"
        elif plan.status == "GENERATING" or len(generated_weeks) < generation_target:
            phase = "DETAILS"
        else:
            phase = "COMPLETE"
        conversation = self._plan_conversation(goal_id)
        messages = self._conversation_messages(conversation.id) if conversation else []
        generation_job = self._latest_generation_job(goal_id)
        versions = list(
            self.session.scalars(
                select(PlanVersion)
                .where(PlanVersion.goal_id == goal_id)
                .order_by(PlanVersion.revision.desc(), PlanVersion.created_at.desc())
            )
        )
        windows = list(
            self.session.scalars(
                select(PlanWindow)
                .where(PlanWindow.goal_id == goal_id)
                .order_by(PlanWindow.created_at)
            )
        )
        pending_window = next((item for item in reversed(windows) if item.status == "DRAFT"), None)
        pending_adjustment = self._scope_exploratory_adjustment(
            plan,
            self._pending_adjustment(messages),
        )
        recovery_context = self._active_recovery_context(goal_id) if pending_adjustment else None
        recovery_review = self._recovery_review(recovery_context) if recovery_context else None
        if recovery_context and recovery_context.get("ai_allocation_generated"):
            pending_adjustment = self._local_recovery_proposal(recovery_context)
        pending_adjustment_preview = self._pending_adjustment_preview(
            goal.id,
            plan,
            pending_adjustment,
        )
        recovery_guard = self._recovery_guard(goal, plan)
        return PlanWorkspace(
            goal_id=goal.id,
            goal_title=goal.title,
            status=goal.status,
            requirement_version=requirement.version,
            requirement_snapshot=requirement.snapshot_json,
            route_version=route.version,
            route_revision=route.revision,
            plan=self._plan_read(plan) if plan else None,
            messages=[self._message_read(message) for message in messages],
            revision_history=[
                PlanRevisionRead(
                    id=item.id,
                    version=item.version,
                    revision=item.revision,
                    status=item.status,
                    change_summary=item.change_summary_json,
                    feedback=item.feedback,
                    created_at=item.created_at,
                )
                for item in versions
            ],
            pending_adjustment=pending_adjustment,
            recovery_review=recovery_review,
            generation_phase=phase,
            generated_weeks=len(generated_weeks),
            total_weeks=plan.total_weeks if plan else 0,
            generation_target_weeks=generation_target,
            planning_mode=plan.planning_mode if plan else goal.planning_mode,
            schedule_type=plan.schedule_type if plan else goal.schedule_type,
            exploration_windows=[self._plan_window_read(item) for item in windows],
            pending_exploration_window=(
                self._plan_window_read(pending_window) if pending_window else None
            ),
            can_extend_exploration=bool(
                goal.status == "ACTIVE"
                and plan
                and plan.planning_mode == "EXPLORATORY"
                and pending_window is None
                and pending_adjustment is None
                and plan.next_unplanned_week <= plan.total_weeks
            ),
            available_generation_modes=["STANDARD"],
            generation_job=self._generation_job_read(generation_job) if generation_job else None,
            generation_repair=self._generation_repair_read(goal_id, plan),
            capacity_negotiation=self._capacity_negotiation(goal_id, plan),
            latest_generation_run=self.orchestrator.latest(goal_id),
            latest_research=self._latest_research_read(
                goal_id,
                conversation.id if conversation else None,
            ),
            recovery_guard=recovery_guard,
            pending_adjustment_preview=pending_adjustment_preview,
        )

    def _recovery_guard(self, goal: Goal, plan: PlanVersion | None) -> PlanRecoveryGuardRead:
        if plan is None:
            return PlanRecoveryGuardRead()
        # Reuse the execution ledger's canonical inactivity calculation so the
        # plan-adjustment gate and the progress page cannot disagree.
        from planner_api.execution_service import ExecutionService

        inactivity = ExecutionService(self.session)._inactivity_for_plan(goal, plan)
        pending_recovery = self._active_recovery_context(goal.id)
        has_pending_recovery = bool(
            pending_recovery and pending_recovery.get("proposed_assignments")
        )
        unresolved_missed_work = bool(inactivity.missed_dates) and (
            not inactivity.recovery_deferred or inactivity.has_new_missed_work
        )
        required = has_pending_recovery or unresolved_missed_work
        if has_pending_recovery:
            message = "已有一份追赶方案等待确认。先处理它，再发布新的计划调整。"
        elif required:
            message = "当前还有未处理的中断任务。请先决定追赶，或明确从今天重新开始。"
        else:
            message = ""
        return PlanRecoveryGuardRead(
            required=required,
            message=message,
            missed_execution_days=inactivity.missed_execution_days,
            interrupted_execution_days=inactivity.interrupted_execution_days,
            missed_dates=inactivity.missed_dates,
            recovery_deferred=inactivity.recovery_deferred,
        )

    def get_planning_brief(self, goal_id: str) -> PlanningBriefRead:
        context, brief = self._ensure_planning_brief(goal_id)
        return self._planning_brief_read(brief, context)

    def get_generation_batches(self, goal_id: str) -> list[GenerationBatchRead]:
        self._get_inputs(goal_id)
        # 恢复调整是可重复发起的；页面只应展示最近一次恢复方案的批次，
        # 避免旧方案的周次继续混入当前预览。
        latest_recovery_brief = self.session.scalar(
            select(PlanningBrief)
            .where(
                PlanningBrief.goal_id == goal_id,
                PlanningBrief.brief_kind == "RECOVERY",
            )
            .order_by(PlanningBrief.version.desc(), PlanningBrief.created_at.desc())
        )
        query = select(GenerationBatch).where(
            GenerationBatch.goal_id == goal_id,
            GenerationBatch.status != "CANCELLED",
        )
        if latest_recovery_brief is not None:
            query = query.where(
                (GenerationBatch.scope_type != "RECOVERY_WEEK")
                | (GenerationBatch.brief_id == latest_recovery_brief.id)
            )
        rows = self.session.scalars(query.order_by(GenerationBatch.created_at.desc()))
        proposal = None
        try:
            conversation = self._ensure_plan_conversation(goal_id)
            proposal = self._pending_adjustment(self._conversation_messages(conversation.id))
        except Exception:
            proposal = None
        if self._active_recovery_context(goal_id) is None:
            rows = [row for row in rows if row.scope_type != "RECOVERY_WEEK"]
        affected_weeks = set(proposal.affected_weeks) if proposal else None
        return [
            self._generation_batch_read(row)
            for row in rows
            if (
                row.scope_type != "RECOVERY_WEEK"
                or affected_weeks is None
                or int(row.scope_json.get("week", 0)) in affected_weeks
            )
        ]

    def create_generation_batch(
        self, goal_id: str, payload: GenerationBatchCreate
    ) -> GenerationBatchRead:
        require_clarification_analysis_complete(self.session, goal_id)
        goal, _, _ = self._get_inputs(goal_id)
        context, brief = self._ensure_planning_brief(goal_id)
        existing = self.session.scalar(
            select(GenerationBatch).where(
                GenerationBatch.idempotency_key == payload.idempotency_key
            )
        )
        if existing is not None:
            return self._generation_batch_read(existing)
        plan = self._current_plan(goal_id)
        batch = GenerationBatch(
            goal_id=goal.id,
            plan_version_id=plan.id if plan else None,
            brief_id=brief.id,
            idempotency_key=payload.idempotency_key,
            scope_type=payload.scope_type,
            scope_json=payload.scope,
            status="QUEUED",
            attempt_count=0,
        )
        self.session.add(batch)
        self.session.commit()
        return self._generation_batch_read(batch)

    def _begin_compiler_batch(
        self,
        goal_id: str,
        plan_id: str | None,
        brief: PlanningBrief,
        idempotency_key: str,
        scope_type: str,
        scope: dict[str, object],
    ) -> GenerationBatch:
        batch = self.session.scalar(
            select(GenerationBatch).where(GenerationBatch.idempotency_key == idempotency_key)
        )
        if batch is None:
            batch = GenerationBatch(
                goal_id=goal_id,
                plan_version_id=plan_id,
                brief_id=brief.id,
                idempotency_key=idempotency_key,
                scope_type=scope_type,
                scope_json=scope,
                status="RUNNING",
                attempt_count=1,
            )
            self.session.add(batch)
        else:
            batch._previous_status = batch.status
            batch._previous_failure_code = batch.failure_code
            batch._previous_failure_message = batch.failure_message
            batch.plan_version_id = plan_id
            batch.brief_id = brief.id
            batch.scope_json = scope
            batch.status = "RUNNING"
            batch.attempt_count += 1
            batch.failure_code = None
            batch.failure_message = None
        self.session.commit()
        return batch

    def _complete_compiler_batch(self, batch: GenerationBatch, result: dict[str, object]) -> None:
        batch.status = "COMPLETED"
        batch.failure_code = None
        batch.failure_message = None
        batch.staged_result_json = result

    @staticmethod
    def _generation_artifact_context_hash(batch: GenerationBatch) -> str:
        return hashlib.sha256(
            json.dumps(
                {
                    "brief_id": batch.brief_id,
                    "scope_type": batch.scope_type,
                    "scope": batch.scope_json,
                },
                ensure_ascii=False,
                sort_keys=True,
                default=str,
            ).encode()
        ).hexdigest()

    def _checkpoint_hybrid_artifact(
        self,
        batch: GenerationBatch,
        phase: str,
        *,
        source_document: str | None = None,
        compiled_result: dict[str, object] | None = None,
        validation: dict[str, object] | None = None,
        retry_scope: dict[str, object] | None = None,
        commit: bool = True,
    ) -> None:
        previous = dict(batch.staged_result_json or {})
        artifact = GenerationArtifactEnvelope(
            phase=phase,
            context_hash=self._generation_artifact_context_hash(batch),
            prompt_version=HYBRID_PLAN_PROMPT_VERSION,
            source_document=source_document or previous.get("source_document"),
            compiled_result=compiled_result or previous.get("compiled_result"),
            validation={**dict(previous.get("validation") or {}), **(validation or {})},
            retry_scope=retry_scope or dict(previous.get("retry_scope") or {}),
        )
        batch.staged_result_json = artifact.model_dump(mode="json")
        if commit:
            self.session.commit()

    def _reusable_hybrid_source(self, batch: GenerationBatch) -> str | None:
        try:
            artifact = GenerationArtifactEnvelope.model_validate(batch.staged_result_json)
        except (TypeError, ValueError):
            return None
        if artifact.prompt_version != HYBRID_PLAN_PROMPT_VERSION:
            return None
        if artifact.context_hash != self._generation_artifact_context_hash(batch):
            return None
        return artifact.source_document

    def _reusable_detail_result(
        self,
        batch: GenerationBatch,
        plan: PlanVersion,
        milestone_by_order: dict[int, PlanMilestone],
        week: int,
        outline: dict[str, object],
    ) -> PlanDetailResult | None:
        """Reuse a completed exploratory week only after re-running local gates."""
        try:
            result = PlanDetailResult.model_validate(batch.staged_result_json or {})
            self._validate_detail_result(
                plan, milestone_by_order, [week], result.days, result.source_document,
            )
            self._validate_week_dependency_targets(result, outline)
        except (TypeError, ValueError, ValidationError, HTTPException):
            return None
        return result

    def _record_hybrid_validation_failure(
        self,
        batch: GenerationBatch,
        repair_round: int,
        error: WorkItemValidationError | HTTPException,
        source_document: str | None,
        compiler_repair: dict | None = None,
    ) -> None:
        detail = getattr(error, "detail", None)
        code = (detail.get("code") if isinstance(detail, dict)
                else getattr(error, "code", "HYBRID_PLAN_BATCH_INVALID"))
        diagnostic = (detail.get("diagnostic") if isinstance(detail, dict)
                      else list(getattr(error, "issues", [])))
        validation = dict((batch.staged_result_json or {}).get("validation") or {})
        failures = list(validation.get("automatic_repair_failures") or [])
        failures.append({"batch_attempt": batch.attempt_count, "attempt": repair_round + 1,
                         "code": code, "diagnostic": diagnostic,
                         "validation_detail": detail,
                         "batch_id": batch.id, "plan_version_id": batch.plan_version_id,
                         "scope": dict(batch.scope_json or {}),
                         "context_hash": self._generation_artifact_context_hash(batch),
                         "author_hash": hashlib.sha256((source_document or "").encode()).hexdigest(),
                         "compiler_repair": compiler_repair,
                         "failure_class": self._validation_failure_class(code)})
        self._checkpoint_hybrid_artifact(
            batch, "AUTHOR_COMPLETE", source_document=source_document,
            validation={
                "automatic_repair_failures": failures,
                "automatic_repair": {
                    "batch_attempt": batch.attempt_count,
                    "attempt": min(repair_round + 2, 3),
                    "state": "RUNNING" if repair_round < 2 else "EXHAUSTED",
                },
            },
        )

    def _generation_repair_read(
        self, goal_id: str, plan: PlanVersion | None,
    ) -> PlanGenerationRepairRead | None:
        batch = self.session.scalar(
            select(GenerationBatch).where(
                GenerationBatch.goal_id == goal_id,
                GenerationBatch.plan_version_id == (plan.id if plan else None),
                GenerationBatch.status == "RUNNING",
                GenerationBatch.scope_type.in_(["HYBRID_OUTLINE", "HYBRID_WEEK", "EXPLORATION_WEEK"]),
            ).order_by(GenerationBatch.updated_at.desc()).limit(1)
        )
        if batch is None:
            return None
        repair = ((batch.staged_result_json or {}).get("validation") or {}).get("automatic_repair")
        if (not repair or repair.get("state") != "RUNNING"
                or repair.get("batch_attempt") != batch.attempt_count):
            return None
        week = batch.scope_json.get("week_index")
        subject = f"第 {week} 周的安排" if week else "整体规划"
        return PlanGenerationRepairRead(
            week_index=week, attempt=repair["attempt"],
            message=f"{subject}未通过约束校验，正在让模型重新安排。已完成内容保留，时间上限和确认要求保持有效。",
        )

    def _capacity_negotiation(self, goal_id: str, plan: PlanVersion | None) -> dict | None:
        """Read durable failures; an overloaded draft is not proof of impossible demand."""
        if plan is None or plan.status != "GENERATING":
            return None
        job = self._latest_generation_job(goal_id)
        if job and job.status in {"RUNNING", "QUEUED"}:
            return None
        batch = self.session.scalar(select(GenerationBatch).where(
            GenerationBatch.goal_id == goal_id, GenerationBatch.plan_version_id == plan.id,
            GenerationBatch.status == "FAILED",
            GenerationBatch.failure_code.in_([
                "PLAN_TEMPORAL_SEGMENT_MINUTES_INVALID", "PLAN_TEMPORAL_SEGMENT_CAPACITY_INVALID",
                "PLAN_DETAIL_WEEK_DAY_COUNT_INVALID", "PLAN_COMMITMENT_CAPACITY_INVALID", "PLAN_CAPACITY_INPUT_INCOMPLETE",
            ]),
        ).order_by(GenerationBatch.updated_at.desc()).limit(1))
        if batch is None and (job is None or job.status != "FAILED" or job.error_code not in {
            "PLAN_TEMPORAL_SEGMENT_MINUTES_INVALID", "PLAN_TEMPORAL_SEGMENT_CAPACITY_INVALID",
            "PLAN_DETAIL_WEEK_DAY_COUNT_INVALID", "PLAN_COMMITMENT_CAPACITY_INVALID", "PLAN_CAPACITY_INPUT_INCOMPLETE",
        }):
            return None
        preview_batch = self.session.scalar(select(GenerationBatch).where(
            GenerationBatch.plan_version_id == plan.id,
            GenerationBatch.scope_type == "HYBRID_ADJUSTMENT_PREVIEW",
        ).order_by(GenerationBatch.updated_at.desc()).limit(1))
        stored = preview_batch.staged_result_json or {} if preview_batch else {}
        goal = self.session.get(Goal, goal_id)
        requirement = self.session.get(RequirementVersion, goal.current_requirement_version_id)
        assessment = capacity_assessment(plan, requirement.snapshot_json.get("available_capacity") if requirement else None)
        return {
            "state": "AWAITING_USER", "source_plan_id": plan.id, "batch_id": batch.id if batch else None,
            "preview_progress": ({"status": preview_batch.status,
                "completed_weeks": sum(bool(detail) for detail in stored.get("details", [])),
                "total_weeks": (stored.get("outline") or {}).get("total_weeks")}
                if preview_batch else None),
            "classification": ("CONSTRAINT_CONFLICT" if assessment["verified_conflicts"] else
                               "CAPACITY_UNKNOWN" if assessment["state"] == "UNKNOWN" else "DRAFT_OVERLOAD"),
            "capacity_assessment": assessment,
            "verified_conflicts": assessment["verified_conflicts"],
            "week_index": batch.scope_json.get("week_index") if batch else None,
            "code": batch.failure_code if batch else job.error_code,
            "diagnostic": batch.failure_message if batch else job.diagnostic,
            "message": ("确认义务的最低工作量已超过对应窗口容量，需要一起协商时间边界；原任务和时间条件均已保留。"
                        if assessment["verified_conflicts"] else
                        "自动重排后，这份草稿仍未通过容量校验。先核对工作量与各阶段可用时间，再一起选择可行安排。"),
            "capacity_segments": list(plan.temporal_segments_json or []),
            "options": [
                {"title": "先在原期限内重新分配", "instruction": "先检查能否保留所有任务和频率，将可移动工作放到后期空闲阶段。前期少后期多，保留原时间上限与期限，不均摊。"},
                {"title": "讨论是否可以延长期限", "instruction": "先帮我核对截止日期是否可以协商，再估算需要延长多久；不要修改固定考试或硬截止日期。"},
                {"title": "讨论增加可用时间", "instruction": "期限先不变。请先问我哪些阶段、哪些日子能增加多少时间，再核算是否够用；不能自行增加容量。"},
                {"title": "只调整可以移动的活动", "instruction": "请先询问我哪些活动可以移动，再核算释放的时间。睡眠、吃饭、课程、工作保持原约束。"},
            ],
            "rules": ["不删除任务或降低次数", "不自动放宽时间", "预览通过并确认后才创建新版本"],
        }

    def _resume_hybrid_context(self, batch: GenerationBatch, context: PlanContext) -> PlanContext:
        validation = (batch.staged_result_json or {}).get("validation") or {}
        failures = validation.get("automatic_repair_failures") or []
        previous_code = getattr(batch, "_previous_failure_code", None)
        if not failures and previous_code in {
            "PLAN_TEMPORAL_SEGMENT_MINUTES_INVALID", "PLAN_TEMPORAL_SEGMENT_CAPACITY_INVALID",
        }:
            failures = [{"code": previous_code,
                         "diagnostic": getattr(batch, "_previous_failure_message", None)}]
        # A failed compiler can reuse a valid author draft. A failed business
        # validation must ask the author to revise it, including across retries.
        if (failures and getattr(batch, "_previous_status", None) == "FAILED"
                and failures[-1].get("code") == getattr(batch, "_previous_failure_code", None)):
            failure = failures[-1]
            return self._business_retry_context(
                context, batch.scope_type, 0,
                HTTPException(503, detail={
                    "code": failure["code"], "diagnostic": failure.get("diagnostic"),
                    "message": "上次草稿未通过约束校验，请重新编排。",
                }),
                (batch.staged_result_json or {}).get("source_document"),
            )
        source = self._reusable_hybrid_source(batch)
        return PlanContext(
            requirements=context.requirements, route=context.route,
            planning_context=context.planning_context, source_document=source,
        )

    def _complete_hybrid_batch(
        self,
        batch: GenerationBatch,
        result: dict[str, object],
        validation: dict[str, object],
    ) -> None:
        self._checkpoint_hybrid_artifact(
            batch,
            "COMPLETED",
            compiled_result=result,
            validation=validation,
            commit=False,
        )
        batch.status = "COMPLETED"
        batch.failure_code = None
        batch.failure_message = None

    @staticmethod
    def _fail_compiler_batch(batch: GenerationBatch, code: str, message: str | None) -> None:
        batch.status = "FAILED"
        batch.failure_code = code
        batch.failure_message = message[:2000] if message else None

    def _ensure_planning_brief(self, goal_id: str) -> tuple[ContextPack, PlanningBrief]:
        goal, requirement, route = self._get_inputs(goal_id)
        plan = self._current_plan(goal_id)
        plan_id = plan.id if plan else None
        locked_day_ids, locked_task_ids = self._locked_execution_ids(goal_id, plan_id)
        route_stages = self._route_stages(route.id)
        milestones = self._milestones(plan.id) if plan else []
        facts = build_planning_context_snapshot(
            self.session,
            goal=goal,
            requirement=requirement,
            route=route,
            route_stages=route_stages,
            plan=plan,
            milestones=milestones,
            locked_day_ids=locked_day_ids,
            locked_task_ids=locked_task_ids,
        )
        latest = self.session.scalar(
            select(ContextPack)
            .where(
                ContextPack.goal_id == goal.id,
                ContextPack.requirement_version_id == requirement.id,
                ContextPack.route_version_id == route.id,
                ContextPack.plan_version_id == plan_id,
            )
            .order_by(ContextPack.version.desc(), ContextPack.created_at.desc())
        )
        if latest is None or (
            latest.facts_json != facts
            or latest.locked_day_ids_json != locked_day_ids
            or latest.locked_task_ids_json != locked_task_ids
        ):
            latest = ContextPack(
                goal_id=goal.id,
                requirement_version_id=requirement.id,
                route_version_id=route.id,
                plan_version_id=plan_id,
                version=(latest.version + 1 if latest else 1),
                facts_json=facts,
                locked_day_ids_json=locked_day_ids,
                locked_task_ids_json=locked_task_ids,
            )
            self.session.add(latest)
            self.session.flush()
        brief = self.session.scalar(
            select(PlanningBrief)
            .where(
                PlanningBrief.context_pack_id == latest.id,
                PlanningBrief.brief_kind == "PLANNING",
            )
            .order_by(PlanningBrief.version.desc())
        )
        if brief is None:
            brief_payload = {
                "source_requirement_ids": [requirement.id],
                "route_stage_ids": [stage["id"] for stage in facts["route"]],
                "strategy_summary": plan.summary if plan else "等待生成计划后补充规划策略。",
                "stage_allocations": [
                    {
                        "stage_order": stage["order"],
                        "title": stage["title"],
                        "start_week": next(
                            (
                                milestone.start_week
                                for milestone in self._milestones(plan.id)
                                if milestone.stage_order == stage["order"]
                            ),
                            None,
                        )
                        if plan
                        else None,
                        "end_week": next(
                            (
                                milestone.end_week
                                for milestone in self._milestones(plan.id)
                                if milestone.stage_order == stage["order"]
                            ),
                            None,
                        )
                        if plan
                        else None,
                    }
                    for stage in facts["route"]
                ],
                "capacity": facts["capacity"],
                "difficulty_curve": "GRADUAL",
                "buffer_rules": plan.buffer_strategy_json if plan else [],
                "generation_rules": [
                    "每周独立生成并校验",
                    "任务估时不得超过当天建议容量",
                    "已锁定内容不得被改写",
                ],
                "locked_day_ids": locked_day_ids,
                "locked_task_ids": locked_task_ids,
                "affected_weeks": [],
                "risks": [],
                "user_confirmations_required": [],
            }
            brief = PlanningBrief(
                goal_id=goal.id,
                context_pack_id=latest.id,
                plan_version_id=plan_id,
                brief_kind="PLANNING",
                version=1,
                status="DRAFT",
                brief_json=brief_payload,
            )
            self.session.add(brief)
            self.session.commit()
        return latest, brief

    def _locked_execution_ids(
        self, goal_id: str, plan_id: str | None
    ) -> tuple[list[str], list[str]]:
        if plan_id is None:
            return [], []
        day_ids = set(
            self.session.scalars(
                select(ExecutionDayLog.plan_day_id).where(
                    ExecutionDayLog.goal_id == goal_id,
                    ExecutionDayLog.plan_version_id == plan_id,
                )
            )
        )
        for model in (ProgressEvent, Feedback, FocusSession):
            day_ids.update(
                self.session.scalars(
                    select(model.plan_day_id).where(
                        model.goal_id == goal_id,
                        model.plan_version_id == plan_id,
                    )
                )
            )
        task_ids = set(
            self.session.scalars(
                select(ProgressEvent.task_id).where(
                    ProgressEvent.goal_id == goal_id,
                    ProgressEvent.plan_version_id == plan_id,
                    ProgressEvent.task_id.is_not(None),
                )
            )
        )
        task_ids.update(
            self.session.scalars(
                select(Feedback.task_id).where(
                    Feedback.goal_id == goal_id,
                    Feedback.plan_version_id == plan_id,
                    Feedback.task_id.is_not(None),
                )
            )
        )
        task_ids.update(
            self.session.scalars(
                select(FocusSession.task_id).where(
                    FocusSession.goal_id == goal_id,
                    FocusSession.plan_version_id == plan_id,
                    FocusSession.task_id.is_not(None),
                )
            )
        )
        if day_ids:
            task_ids.update(
                self.session.scalars(select(PlanTask.id).where(PlanTask.plan_day_id.in_(day_ids)))
            )
        return sorted(day_ids), sorted(task_ids)

    def prepare_generation(
        self,
        goal_id: str,
        mode: str,
    ) -> tuple[PlanGenerationJob, bool]:
        require_clarification_analysis_complete(self.session, goal_id)
        goal, _, route = self._get_inputs(goal_id)
        if mode not in self.provider.supported_generation_modes:
            raise HTTPException(
                status_code=422,
                detail="The selected generation mode is not supported by this provider",
            )
        conversation = self._plan_conversation(goal_id)
        if conversation and self._pending_adjustment(self._conversation_messages(conversation.id)):
            raise HTTPException(409, "先确认或放弃正在讨论的方案，再继续生成。")
        current = self._current_plan(goal_id)
        if current and capacity_conflicts(current):
            raise HTTPException(409, "已确认的最低工作量超过可用时间，请先讨论并确认可行方案。")
        active = self._latest_generation_job(goal_id)
        if active and active.status in {"QUEUED", "RUNNING"}:
            active.status = "FAILED"
            active.error_code = "PROCESS_OR_PAGE_INTERRUPTED"
            active.error_message = "上一次生成已经中断，已完成内容仍然保留，本次将继续补齐。"
            active.completed_at = utc_now()
            self.session.commit()
        workspace = self.get_workspace(goal_id)
        if workspace.generation_phase == "COMPLETE":
            raise HTTPException(status_code=409, detail="Plan generation is already complete")
        current = self._current_plan(goal_id)
        if current is not None and current.planning_mode != route.planning_mode:
            raise HTTPException(409, "已有草案的规划模式不能在生成中途切换")
        if current is not None and current.schedule_type != route.schedule_type:
            raise HTTPException(409, "已有草案的时间组织方式不能在生成中途切换")
        if goal.planning_mode != route.planning_mode:
            raise HTTPException(409, "路线采用的规划方式与目标不一致，请重新生成路线")
        if goal.schedule_type != route.schedule_type:
            raise HTTPException(409, "路线采用的时间组织方式与目标不一致，请重新生成路线")
        job = PlanGenerationJob(
            goal_id=goal.id,
            mode=mode,
            planning_mode=route.planning_mode,
            schedule_type=route.schedule_type,
            status="QUEUED",
            phase=workspace.generation_phase,
            generated_weeks=workspace.generated_weeks,
            total_weeks=workspace.total_weeks,
        )
        self.session.add(job)
        goal.last_active_at = utc_now()
        self.session.commit()
        return job, True

    def confirm_plan(self, goal_id: str) -> PlanWorkspace:
        require_clarification_analysis_complete(self.session, goal_id)
        goal, _, _ = self._get_inputs(goal_id)
        if (
            goal.status
            in {
                "READY_TO_START",
                "ACTIVE",
                "RECOVERY_REQUIRED",
                "PAUSED",
                "COMPLETED",
            }
            and goal.active_plan_version_id
            and (active := self.session.get(PlanVersion, goal.active_plan_version_id)) is not None
            and active.status == "CONFIRMED"
        ):
            return self.get_workspace(goal_id)
        if goal.status not in {"PLAN_REVIEW", "READY_TO_START"}:
            raise HTTPException(status_code=409, detail="Plan is not awaiting review")
        plan = self._require_review_plan(goal_id)
        if len(self._generated_weeks(plan.id)) < self._generation_target(plan):
            raise HTTPException(status_code=409, detail="Plan generation is not complete")
        conversation = self._plan_conversation(goal_id)
        messages = self._conversation_messages(conversation.id) if conversation else []
        if self._pending_adjustment(messages) is not None:
            raise HTTPException(status_code=409, detail="A plan adjustment is still pending")
        plan.status = "CONFIRMED"
        plan.confirmed_at = utc_now()
        goal.active_plan_version_id = plan.id
        goal.status = "READY_TO_START"
        if plan.planning_mode == "EXPLORATORY":
            window = self.session.scalar(
                select(PlanWindow)
                .where(PlanWindow.plan_version_id == plan.id, PlanWindow.status == "DRAFT")
                .order_by(PlanWindow.sequence.desc())
            )
            if window:
                window.status = "ACTIVE"
                window.published_at = utc_now()
        goal.last_active_at = utc_now()
        self.session.commit()
        return self.get_workspace(goal_id)

    def draft_exploration_window(
        self, goal_id: str, generation_mode: str = "STANDARD"
    ) -> PlanWorkspace:
        """Materialize the next short window as a reviewable plan revision."""
        with _plan_generation_locks[goal_id]:
            goal, requirement, route = self._get_inputs(goal_id)
            if goal.status != "ACTIVE" or not goal.active_plan_version_id:
                raise HTTPException(409, "滚动续订只能在正在执行的计划中进行")
            current = self.session.get(PlanVersion, goal.active_plan_version_id)
            if current is None or current.planning_mode != "EXPLORATORY":
                raise HTTPException(409, "当前计划不是探索式规划")
            if generation_mode not in self.provider.supported_generation_modes:
                raise HTTPException(422, "当前模型不支持所选生成方式")
            pending = self.session.scalar(
                select(PlanWindow).where(
                    PlanWindow.goal_id == goal_id, PlanWindow.status == "DRAFT"
                )
            )
            if pending:
                return self.get_workspace(goal_id)
            plan_conversation = self._plan_conversation(goal_id)
            plan_messages = (
                self._conversation_messages(plan_conversation.id) if plan_conversation else []
            )
            if self._pending_adjustment(plan_messages) is not None:
                raise HTTPException(409, "当前已有待确认的调整建议，请先处理它")
            start_week = current.next_unplanned_week
            if start_week > current.total_weeks:
                raise HTTPException(409, "整条路线已经全部展开")
            end_week = self._next_exploration_stage_end(current, start_week)
            evidence = self._exploration_execution_evidence(goal, current)
            route_stages = self._route_stages(route.id)
            milestones = self._milestones(current.id)
            outline = self._outline_payload(current, milestones)
            outline["generated_task_registry"] = self._generated_task_registry(current.id)
            context_pack, brief = self._ensure_planning_brief(goal_id)
            orchestration = self.orchestrator.prepare(
                goal.id,
                "ROLLING_EXTENSION",
                {
                    **context_pack.facts_json,
                    "exploration_execution_evidence": evidence,
                    "exploration_window": {"start_week": start_week, "end_week": end_week},
                },
                plan_version_id=current.id,
                context_pack_id=context_pack.id,
                minimum_tier="L2",
            )
            context = self._plan_context(
                requirement,
                route,
                route_stages,
                planning_context={
                    **context_pack.facts_json,
                    "exploration_execution_evidence": evidence,
                    "exploration_window": {"start_week": start_week, "end_week": end_week},
                    "agent_orchestration": orchestration,
                    "exploration_policy": (
                        "只细化到下一个可观察路线阶段；根据实际完成、节奏反馈和现有日历"
                        "调整强度，但不得改写已执行周、已确认路线和长期约束。"
                    ),
                },
            )
            milestone_by_order = {item.stage_order: item for item in milestones}
            context_key = hashlib.sha256(json.dumps({
                "evidence": {key: value for key, value in evidence.items() if key != "captured_at"},
                "facts": context_pack.facts_json,
                "requirements": context.requirements, "route": context.route,
                "mode": generation_mode,
            }, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()
            new_days = []
            for week in range(start_week, end_week + 1):
                key = f"exploration:{current.id}:{start_week}:{end_week}:{week}:{context_key}"
                batch = self.session.scalar(select(GenerationBatch).where(
                    GenerationBatch.idempotency_key == key))
                detail = (self._reusable_detail_result(
                    batch, current, milestone_by_order, week, outline)
                    if batch and batch.status == "COMPLETED" else None)
                if detail is None:
                    batch = self._begin_compiler_batch(goal.id, current.id, brief,
                        key, "EXPLORATION_WEEK", {"week_index": week})
                    try:
                        detail = self._draft_validated_week(
                            context, outline, current, milestone_by_order,
                            week, generation_mode, repair_batch=batch,
                        )
                        self._complete_compiler_batch(batch, detail.model_dump(mode="json"))
                        self.session.commit()
                    except (ClarificationProviderError, HTTPException) as error:
                        failure = error.detail if isinstance(error, HTTPException) else {}
                        self._fail_compiler_batch(batch,
                            failure.get("code", getattr(error, "code", "PLAN_WEEK_GENERATION_FAILED")) if isinstance(failure, dict)
                            else "PLAN_WEEK_GENERATION_FAILED", str(error))
                        self.orchestrator.fail(orchestration["run_id"], error)
                        self.session.commit()
                        raise
                new_days.extend(day.model_dump(mode="json") for day in detail.days)
                outline["generated_task_registry"].extend(
                    {"title": task.title,
                     "reference": f"M{day.stage_order}-W{day.week_index}-D{day.day_index}-T{task.order}"}
                    for day in detail.days for task in day.tasks
                )
            payload = self._plan_payload(current)
            payload["assistant_message"] = (
                f"已根据实际执行情况准备第 {start_week}-{end_week} 周草案。"
            )
            payload["days"] = [*payload["days"], *new_days]
            payload["change_summary"] = [
                f"新增第 {start_week}-{end_week} 周执行草案",
                "已把完成情况、节奏反馈和当前约束作为本窗口生成依据",
            ]
            result = PlanRevisionResult.model_validate(payload)
            revised = self._persist_revision(
                goal,
                route,
                route_stages,
                current,
                result,
                f"探索式生成第 {start_week}-{end_week} 周",
                "exploration-window-v1",
            )
            revised.next_unplanned_week = end_week + 1
            revised.exploration_window_weeks = end_week - start_week + 1
            sequence = (
                int(
                    self.session.scalar(
                        select(func.coalesce(func.max(PlanWindow.sequence), 0)).where(
                            PlanWindow.goal_id == goal_id
                        )
                    )
                    or 0
                )
                + 1
            )
            self.session.add(
                PlanWindow(
                    goal_id=goal_id,
                    plan_version_id=revised.id,
                    sequence=sequence,
                    start_week=start_week,
                    end_week=end_week,
                    status="DRAFT",
                    trigger="EXECUTION_FEEDBACK",
                    basis_json=evidence,
                )
            )
            self.orchestrator.complete(
                orchestration["run_id"],
                {"status": "PASSED", "generated_weeks": list(range(start_week, end_week + 1))},
            )
            self.session.commit()
            return self.get_workspace(goal_id)

    def apply_exploration_window(self, goal_id: str) -> PlanWorkspace:
        with _plan_generation_locks[goal_id]:
            goal = self.session.get(Goal, goal_id)
            if goal is None or goal.status != "ACTIVE" or not goal.active_plan_version_id:
                raise HTTPException(409, "当前没有可续订的执行中计划")
            current = self.session.get(PlanVersion, goal.active_plan_version_id)
            pending = self.session.scalar(
                select(PlanWindow)
                .where(PlanWindow.goal_id == goal_id, PlanWindow.status == "DRAFT")
                .order_by(PlanWindow.created_at.desc())
            )
            revised = self.session.get(PlanVersion, pending.plan_version_id) if pending else None
            if current is None or pending is None or revised is None:
                raise HTTPException(409, "没有待确认的滚动窗口草案")
            if (pending.basis_json or {}).get("source_plan_version_id") != current.id:
                raise HTTPException(409, "当前执行版本已变化，请放弃旧草案后重新展开下一阶段")
            conversation = self._plan_conversation(goal_id)
            messages = self._conversation_messages(conversation.id) if conversation else []
            if self._pending_adjustment(messages) is not None:
                raise HTTPException(409, "当前还有待处理的调整建议，请先处理后再接入下一阶段")
            try:
                self._migrate_active_execution(
                    goal,
                    current,
                    revised,
                    {"allow_exploration_extension": True},
                )
                mark_plan_schedule_review(
                    self.session,
                    goal,
                    current.id,
                    revised.id,
                    "滚动计划新增了后续窗口，请核对新增任务的具体时段。",
                )
            except ProviderUnavailableError as error:
                self.session.rollback()
                raise HTTPException(409, str(error)) from error
            for window in self.session.scalars(
                select(PlanWindow).where(
                    PlanWindow.goal_id == goal_id, PlanWindow.status == "ACTIVE"
                )
            ):
                window.status = "CLOSED"
            pending.status = "ACTIVE"
            pending.published_at = utc_now()
            self._publish_revision_schedule(goal, current, revised)
            goal.last_active_at = utc_now()
            self.session.commit()
            return self.get_workspace(goal_id)

    def discard_exploration_window(self, goal_id: str) -> PlanWorkspace:
        goal = self.session.get(Goal, goal_id)
        pending = self.session.scalar(
            select(PlanWindow)
            .where(PlanWindow.goal_id == goal_id, PlanWindow.status == "DRAFT")
            .order_by(PlanWindow.created_at.desc())
        )
        if goal is None or pending is None:
            raise HTTPException(409, "没有待放弃的滚动窗口草案")
        revised = self.session.get(PlanVersion, pending.plan_version_id)
        pending.status = "DISCARDED"
        if revised:
            revised.status = "SUPERSEDED"
        self.session.commit()
        return self.get_workspace(goal_id)

    def _exploration_execution_evidence(self, goal: Goal, plan: PlanVersion) -> dict[str, object]:
        tasks = list(
            self.session.scalars(select(PlanTask).where(PlanTask.plan_version_id == plan.id))
        )
        feedbacks = list(
            self.session.scalars(
                select(Feedback)
                .where(Feedback.goal_id == goal.id, Feedback.plan_version_id == plan.id)
                .order_by(Feedback.created_at.desc())
                .limit(20)
            )
        )
        status_counts = {
            status: sum(task.status == status for task in tasks)
            for status in {"NOT_STARTED", "IN_PROGRESS", "COMPLETED", "CARRIED_OVER"}
        }
        return {
            "captured_at": utc_now().isoformat(),
            "source_plan_version_id": plan.id,
            "task_status_counts": status_counts,
            "recent_feedback": [
                {
                    "kind": item.feedback_kind,
                    "reason": item.reason_code,
                    "intent": item.intent_code,
                    "detail": item.detail_text,
                }
                for item in feedbacks
            ],
        }

    def explain_plan(
        self,
        goal_id: str,
        question: str,
        *,
        web_research_enabled: bool = False,
        research_run_id: str | None = None,
        regenerate: bool = False,
        use_saved_research: bool = True,
        material_source_ids: list[str] | None = None,
    ) -> PlanWorkspace:
        goal, requirement, route = self._get_review_inputs(goal_id)
        current = self._require_review_plan(goal_id)
        conversation = self._ensure_plan_conversation(goal_id)
        clean_question = question.strip()
        from planner_api.materials.conversation import resolve_discussion_sources, remember_discussion_sources
        material_source_ids = resolve_discussion_sources(self.session, conversation, clean_question, "EXPLAIN", material_source_ids, regenerate=regenerate)
        from planner_api.materials.analysis_ai import build_material_analysis_provider
        from planner_api.materials.analysis_service import MaterialAnalysisService
        material_context = MaterialAnalysisService(self.session, build_material_analysis_provider(get_settings())).prepare_conversation_context(
            goal_id, clean_question, material_source_ids) if material_source_ids else None
        user_message, existing_assistant = self._prepare_discussion_exchange(
            conversation, clean_question, "EXPLAIN", allow_existing=regenerate
        )
        remember_discussion_sources(self.session, conversation, material_source_ids, material_context)
        goal.last_active_at = utc_now()
        self.session.commit()
        research_context, research_read = self._prepare_plan_research(
            goal_id,
            conversation,
            clean_question,
            message_id=user_message.id,
            web_research_enabled=web_research_enabled,
            research_run_id=research_run_id,
            decision_context=self._research_decision_context(goal, requirement, route, current),
            reuse_latest=use_saved_research,
        )

        try:
            result = self.discussion_provider.explain(
                self._discussion_context(
                    requirement,
                    route,
                    current,
                    clean_question,
                    research_context=research_context,
                    material_context=material_context,
                )
            )
        except ClarificationProviderError as error:
            self._add_failed_message(conversation, error, "EXPLAIN", existing=existing_assistant)
            self.session.commit()
            return self.get_workspace(goal_id)

        from planner_api.goal_memory import save_memories
        save_memories(self.session, conversation, result.long_term_memories, source_message=user_message)
        self._add_assistant_message(
            conversation,
            result.answer,
            "EXPLAIN",
            {
                "covered_questions": result.covered_questions,
                "needs_clarification": result.needs_clarification,
                **(
                    {
                        "research_run_id": research_read.id,
                        "research_status": research_read.status,
                    }
                    if research_read
                    else {}
                ),
            },
            existing=existing_assistant,
        )
        self.session.commit()
        return self.get_workspace(goal_id)

    def revise_plan(
        self,
        goal_id: str,
        feedback: str,
        recovery_context: dict[str, object] | None = None,
        *,
        web_research_enabled: bool = False,
        research_run_id: str | None = None,
        regenerate: bool = False,
        use_saved_research: bool = True,
        material_source_ids: list[str] | None = None,
        recovery_options: dict[str, object] | None = None,
    ) -> PlanWorkspace:
        require_clarification_analysis_complete(self.session, goal_id)
        goal, requirement, route = self._get_review_inputs(goal_id)
        if self.session.scalar(select(PlanWindow.id).where(
            PlanWindow.goal_id == goal_id, PlanWindow.status == "DRAFT",
            PlanWindow.trigger == "EXECUTION_FEEDBACK",
        ).limit(1)):
            raise HTTPException(409, "下一阶段草案尚未处理，请先确认接入或放弃草案，再调整计划")
        current = self._require_review_plan(goal_id)
        conversation = self._ensure_plan_conversation(goal_id)
        existing_messages = self._conversation_messages(conversation.id)
        previous_adjustment = self._pending_adjustment(existing_messages)
        if recovery_context is None:
            recovery_context = self._active_recovery_context(goal_id)
        clean_feedback = feedback.strip()
        if recovery_options:
            if not recovery_context or not previous_adjustment:
                raise HTTPException(409, "当前没有待调整的恢复方案，请先生成恢复预览。")
            updated = self._with_recovery_preview_options(recovery_context, recovery_options)
            brief = self.session.scalar(select(PlanningBrief).where(
                PlanningBrief.goal_id == goal_id, PlanningBrief.brief_kind == "RECOVERY",
                PlanningBrief.status == "DRAFT").order_by(PlanningBrief.version.desc()))
            if brief is None:
                raise HTTPException(409, "恢复方案已变化，请重新读取。")
            proposal = self._local_recovery_proposal(updated)
            brief.brief_json = {**brief.brief_json, "recovery_context": updated}
            self._add_user_message(conversation, clean_feedback, "REVISE")
            self._add_assistant_message(conversation, proposal.assistant_message, "REVISE", {
                "prompt_version": "recovery-preview-preferences-v1", "recovery_brief_id": brief.id,
                "adjustment_proposal": proposal.model_dump(mode="json")})
            goal.last_active_at = utc_now()
            self.session.commit()
            return self.get_workspace(goal_id)
        from planner_api.materials.conversation import resolve_discussion_sources, remember_discussion_sources
        material_source_ids = resolve_discussion_sources(self.session, conversation, clean_feedback, "REVISE", material_source_ids, regenerate=regenerate)
        from planner_api.materials.analysis_ai import build_material_analysis_provider
        from planner_api.materials.analysis_service import MaterialAnalysisService
        material_context = MaterialAnalysisService(self.session, build_material_analysis_provider(get_settings())).prepare_conversation_context(
            goal_id, clean_feedback, material_source_ids) if material_source_ids else None
        # Answering a saved decision must not start research or generation.
        if recovery_context:
            answer = re.split(r"[。；;！!？?：:\s]+", clean_feedback.strip("。；;！!？?，,. \r\n\t"))[-1]
            decision = None
            if recovery_context.get("requires_direct_extension"):
                if answer in {"不允许", "不同意", "不允许顺延", "不接受顺延", "保持期限"}:
                    decision = "KEEP_DEADLINE"
                elif (answer in {"允许", "同意", "可以", "允许顺延", "接受顺延", "确认顺延", "直接顺延", "可以延长"}
                      or (any(phrase in clean_feedback for phrase in ("允许顺延", "接受顺延", "确认顺延", "直接顺延", "可以延长"))
                          and not re.search(r"不(?:允许|接受|同意)|不能|不要|是否|能否", clean_feedback))):
                    decision = "ALLOW_EXTENSION"
            elif recovery_context.get("requires_load_confirmation") and answer in {"允许", "同意", "可以接受", "接受这个强度", "接受当前强度"}:
                decision = "ACCEPT_LOAD"
            if decision and decision in self._recovery_review(recovery_context)["decisions"]:
                return self.decide_recovery(
                    goal_id, decision, self._recovery_review(recovery_context)["token"],
                    feedback=clean_feedback,
                )
        user_message, existing_assistant = self._prepare_discussion_exchange(
            conversation, clean_feedback, "REVISE", allow_existing=regenerate
        )
        remember_discussion_sources(self.session, conversation, material_source_ids, material_context)
        goal.last_active_at = utc_now()
        self.session.commit()
        research_context, research_read = self._prepare_plan_research(
            goal_id,
            conversation,
            clean_feedback,
            message_id=user_message.id,
            web_research_enabled=web_research_enabled,
            research_run_id=research_run_id,
            decision_context=self._research_decision_context(goal, requirement, route, current),
            reuse_latest=use_saved_research,
        )

        compact_feedback = "".join(clean_feedback.split())
        if recovery_context and research_context:
            recovery_context = {**recovery_context, "_research_context": research_context}
        rejects_load = any(
            phrase in compact_feedback
            for phrase in ("不接受", "不能接受", "太高", "分摊", "降低负担", "往后安排")
        )
        accepts_load = not rejects_load and any(
            phrase in compact_feedback
            for phrase in (
                "接受这个强度",
                "接受当前强度",
                "可以接受",
                "按这个方案",
                "就这样安排",
                "确认应用",
            )
        )
        if rejects_load and recovery_context:
            target_refs = [
                f"week:{week}"
                for week in recovery_context.get("recommended_local_weeks", [])
                if isinstance(week, int)
            ]
            persist_explicit_error_memories(
                self.session,
                goal_id=goal_id,
                source_message_id=user_message.id,
                user_content=clean_feedback,
                candidates=[
                    ErrorAvoidanceMemoryCandidate(
                        rule=(
                            "追赶安排不得再次把积压集中到用户已明确拒绝的高负荷日期；"
                            "应先扩大近期承接范围并重新核算峰值负担。"
                        ),
                        evidence_quote=clean_feedback[:500],
                        scope="RECOVERY",
                        target_refs=target_refs,
                    )
                ],
            )
        if (
            recovery_context
            and recovery_context.get("requires_direct_extension")
            and not recovery_context.get("slight_extension_possible")
        ):
            result = self._local_recovery_proposal(
                recovery_context,
                assistant_message="三天内无法完成顺延",
                can_apply=False,
                clarifying_question=(
                    "原结束日期后 3 天内仍无法承载全部工作。"
                    "请明确选择更长顺延，或先减少可选任务；当前方案不会应用。"
                ),
            )
            active_brief = self.session.scalar(
                select(PlanningBrief)
                .where(
                    PlanningBrief.goal_id == goal_id,
                    PlanningBrief.brief_kind == "RECOVERY",
                    PlanningBrief.status == "DRAFT",
                )
                .order_by(PlanningBrief.version.desc())
            )
            self._add_assistant_message(
                conversation,
                result.assistant_message,
                "REVISE",
                {
                    "prompt_version": "recovery-allocation-v1",
                    "adjustment_proposal": result.model_dump(mode="json"),
                    "recovery_brief_id": active_brief.id if active_brief else None,
                    "recovery_strategy": "AI_STRUCTURED_OPERATIONS",
                    "slight_extension_rejected": True,
                },
            )
            self.session.commit()
            return self.get_workspace(goal_id)
        if (
            recovery_context
            and recovery_context.get("ai_allocation_generated")
            and recovery_context.get("requires_load_confirmation")
            and recovery_context.get("proposed_assignments")
            and accepts_load
        ):
            recovery_context = {**recovery_context, "requires_load_confirmation": False, "load_confirmed": True}
            active_brief = self.session.scalar(select(PlanningBrief).where(
                PlanningBrief.goal_id == goal_id, PlanningBrief.brief_kind == "RECOVERY", PlanningBrief.status == "DRAFT"
            ).order_by(PlanningBrief.version.desc()))
            if active_brief:
                active_brief.brief_json = {**(active_brief.brief_json or {}), "recovery_context": recovery_context}
            latest_recovery_id = self.session.scalar(
                select(PlanningBrief.id)
                .where(
                    PlanningBrief.goal_id == goal_id,
                    PlanningBrief.brief_kind == "RECOVERY",
                    PlanningBrief.status == "DRAFT",
                )
                .order_by(PlanningBrief.version.desc())
                .limit(1)
            )
            result = self._local_recovery_proposal(
                recovery_context,
                assistant_message="用户已明确接受近期的较高负担",
                can_apply=True,
            )
            result.assistant_message = (
                f"已记录你接受近期最高约 "
                f"{int(recovery_context.get('projected_peak_daily_minutes', 0) or 0)} 分钟的负担。"
                "结构化承接操作没有改变，现在可以确认应用；应用后原有后续任务仍保持不变。"
            )
            self._add_assistant_message(
                conversation,
                result.assistant_message,
                "REVISE",
                {
                    "prompt_version": "recovery-allocation-v1",
                    "adjustment_proposal": result.model_dump(mode="json"),
                    "recovery_brief_id": latest_recovery_id,
                    "recovery_strategy": "AI_STRUCTURED_OPERATIONS",
                    "load_confirmation": "ACCEPTED",
                },
            )
            self.session.commit()
            return self.get_workspace(goal_id)

        context_pack, _brief = self._ensure_planning_brief(goal_id)
        orchestration = self.orchestrator.prepare(
            goal.id,
            "RECOVERY_PLANNING" if recovery_context else "PLAN_ADJUSTMENT",
            {
                **context_pack.facts_json,
                "user_request": clean_feedback,
                "current_plan": self._plan_payload(current),
                "recovery_context": recovery_context,
            },
            plan_version_id=current.id,
            context_pack_id=context_pack.id,
            minimum_tier="L2",
        )
        if recovery_context:
            recovery_context = {
                **recovery_context,
                "_agent_orchestration": orchestration,
            }

        try:
            if (
                recovery_context
                and recovery_context.get("ai_allocation_generated")
            ):
                all_candidates = [
                    item
                    for item in recovery_context.get("candidate_days", [])
                    if isinstance(item, dict)
                ]
                active_candidates = [
                    item
                    for item in recovery_context.get("allocation_candidate_days", [])
                    if isinstance(item, dict)
                ]
                expanded_count = min(
                    len(all_candidates),
                    len(active_candidates) + max(1, ceil(len(active_candidates) * 0.5)),
                ) if rejects_load else len(active_candidates)
                allocation_fallback = False
                if rejects_load and expanded_count <= len(active_candidates):
                    result = self._local_recovery_proposal(
                        recovery_context,
                        assistant_message="已检查所有剩余执行日",
                        can_apply=False,
                        clarifying_question=(
                            "已经没有更后的既有执行日可用于继续分摊。"
                            "你可以接受当前强度，或保留当前计划暂不应用。"
                        ),
                    )
                else:
                    recovery_context = {
                        **recovery_context,
                        "allocation_candidate_days": all_candidates[:expanded_count],
                        "allocation_expansion_execution_days": (
                            expanded_count
                            - int(recovery_context.get("local_window_execution_days", 0) or 0)
                        ),
                    }
                    recovery_context, allocation, allocation_fallback = (
                        self._allocate_validated_recovery(
                            requirement,
                            route,
                            current,
                            clean_feedback,
                            recovery_context,
                            previous_adjustment,
                        )
                    )
                    active_brief = self.session.scalar(
                        select(PlanningBrief)
                        .where(
                            PlanningBrief.goal_id == goal_id,
                            PlanningBrief.brief_kind == "RECOVERY",
                            PlanningBrief.status == "DRAFT",
                        )
                        .order_by(PlanningBrief.version.desc())
                    )
                    if active_brief is not None:
                        active_brief.brief_json = {
                            **(active_brief.brief_json or {}),
                            "recovery_context": recovery_context,
                        }
                    load_question = None
                    if recovery_context.get("requires_load_confirmation"):
                        peak = int(recovery_context.get("projected_peak_daily_minutes", 0) or 0)
                        load_question = (
                            f"继续向后扩展后，系统复算的最高单日总负担约 {peak} 分钟。"
                            "你是否接受这个强度？如果仍不接受，我可以再扩大一层承接范围。"
                        )
                    result = self._local_recovery_proposal(
                        recovery_context,
                        assistant_message=allocation.assistant_message,
                        can_apply=(
                            not recovery_context.get("unassigned_task_ids")
                            and not allocation.needs_clarification
                            and not load_question
                        ),
                        clarifying_question=allocation.clarifying_question or load_question,
                    )
                latest_recovery_id = self.session.scalar(
                    select(PlanningBrief.id)
                    .where(
                        PlanningBrief.goal_id == goal_id,
                        PlanningBrief.brief_kind == "RECOVERY",
                        PlanningBrief.status == "DRAFT",
                    )
                    .order_by(PlanningBrief.version.desc())
                    .limit(1)
                )
                self._add_assistant_message(
                    conversation,
                    result.assistant_message,
                    "REVISE",
                    {
                        "prompt_version": RECOVERY_ALLOCATION_PROMPT_VERSION,
                        "adjustment_proposal": result.model_dump(mode="json"),
                        "recovery_brief_id": latest_recovery_id,
                        "recovery_strategy": (
                            "DETERMINISTIC_LEDGER_FALLBACK"
                            if allocation_fallback
                            else "AI_STRUCTURED_OPERATIONS"
                        ),
                        "allocation_expanded": True,
                    },
                )
                self.orchestrator.complete(orchestration["run_id"], {"can_apply": result.can_apply})
                self.session.commit()
                return self.get_workspace(goal_id)

            discussion_context = self._discussion_context(
                requirement,
                route,
                current,
                clean_feedback,
                previous_adjustment,
                recovery_context=recovery_context,
                orchestration=orchestration,
                research_context=research_context,
                material_context=material_context,
            )
            result = self.discussion_provider.revise(discussion_context)
            self._normalize_outcome_adjustment(result, current.schedule_type)
            if (self._capacity_negotiation(goal_id, current)
                    and re.search(r"(挪|移动|迁移|调整).{0,12}(娱乐|固定活动)|(?:娱乐|固定活动).{0,18}(挪|移动|迁移)", clean_feedback)
                    and result.capacity_segments
                    and not (discussion_context.planning_context or {}).get("confirmed_activity_moves")):
                result = result.model_copy(update={
                    "can_apply": False, "needs_clarification": True,
                    "clarifying_question": "请先在‘查看固定活动调整’中选择日期，预览并确认活动新旧位置，再核算增加的时间。",
                })
            if self._capacity_negotiation(goal_id, current) and result.can_apply:
                if (result.total_weeks_change or result.weekly_intensity_change != "UNCHANGED") and not result.capacity_segments:
                    result = result.model_copy(update={
                        "can_apply": False, "needs_clarification": True,
                        "clarifying_question": "请确认各阶段可用天数、每天分钟数，以及哪些期限可以调整；先核对具体数值，再生成预览。",
                    })
            # A model can return valid JSON while conservatively marking a
            # sufficiently specified request as non-applicable. Give it one
            # explicit correction chance, without overriding clarification.
            if not result.can_apply and not result.needs_clarification:
                retry_context = self._discussion_context(
                    requirement,
                    route,
                    current,
                    f"{clean_feedback}\n请重新检查：信息已足够时给出可执行的调整方案；只有存在两种无法区分的方案时才要求澄清。",
                    previous_adjustment,
                    recovery_context=recovery_context,
                    orchestration=orchestration,
                    research_context=research_context,
                    material_context=material_context,
                )
                retry_result = self.discussion_provider.revise(retry_context)
                if retry_result.can_apply or retry_result.needs_clarification:
                    result = retry_result
            if recovery_context:
                result = self._constrain_recovery_proposal(result, recovery_context)
            result = self._scope_exploratory_adjustment(current, result)
            route_stages = self._route_stages(route.id)
            valid_stage_orders = {item.order for item in route_stages}
            if not set(result.affected_stage_orders).issubset(valid_stage_orders):
                raise ProviderUnavailableError(
                    "这次建议没有整理完整，当前安排保持不变，可以重新试一次。"
                )
            referenced_weeks = [
                *result.affected_weeks,
                *result.downstream_affected_weeks,
            ]
            # Extension proposals legitimately reference newly added weeks.
            proposed_horizon = current.total_weeks + result.total_weeks_change
            if any(week > max(current.total_weeks, proposed_horizon) for week in referenced_weeks):
                raise ProviderUnavailableError(
                    "这次建议没有正确对应到现有周次，当前安排保持不变，可以重新试一次。"
                )
            self.orchestrator.complete(
                orchestration["run_id"],
                {
                    "status": "PASSED",
                    "can_apply": result.can_apply,
                    "affected_weeks": result.affected_weeks,
                },
            )
        except ClarificationProviderError as error:
            self.orchestrator.fail(orchestration["run_id"], error)
            self._add_failed_message(conversation, error, "REVISE", existing=existing_assistant)
            self.session.commit()
            return self.get_workspace(goal_id)

        # If the user is answering a clarification for the active recovery
        # attempt, keep the new proposal in that same brief.  Otherwise the
        # UI can display a valid proposal while apply() correctly ignores it
        # because it is not tied to the latest recovery brief.
        recovery_metadata = {}
        if recovery_context and recovery_context.get("active_brief_id"):
            recovery_metadata["recovery_brief_id"] = recovery_context["active_brief_id"]

        self._add_assistant_message(
            conversation,
            result.assistant_message,
            "REVISE",
            {
                "prompt_version": PLAN_ADJUSTMENT_PROMPT_VERSION,
                "adjustment_proposal": result.model_dump(mode="json"),
                **(
                    {
                        "research_run_id": research_read.id,
                        "research_status": research_read.status,
                    }
                    if research_read
                    else {}
                ),
                **recovery_metadata,
            },
            existing=existing_assistant,
        )
        persist_explicit_error_memories(
            self.session,
            goal_id=goal_id,
            source_message_id=user_message.id,
            user_content=clean_feedback,
            candidates=result.error_memory_candidates,
        )
        from planner_api.goal_memory import save_memories
        save_memories(self.session, conversation, result.long_term_memories, source_message=user_message)
        self.session.commit()
        return self.get_workspace(goal_id)

    def _latest_recovery_brief(self, goal_id: str) -> PlanningBrief | None:
        return self.session.scalar(
            select(PlanningBrief)
            .where(
                PlanningBrief.goal_id == goal_id,
                PlanningBrief.brief_kind == "RECOVERY",
            )
            .order_by(PlanningBrief.version.desc(), PlanningBrief.created_at.desc())
        )

    def _active_recovery_context(self, goal_id: str) -> dict[str, object] | None:
        # Filtering for DRAFT first resurrects older attempts after publication.
        brief = self._latest_recovery_brief(goal_id)
        plan = self._current_plan(goal_id)
        if brief is None or brief.status != "DRAFT" or plan is None or brief.plan_version_id != plan.id:
            return None
        conversation = self._plan_conversation(goal_id)
        messages = self._conversation_messages(conversation.id) if conversation else []
        if any(message.role == "ASSISTANT" and message.created_at.replace(tzinfo=None) >= brief.created_at.replace(tzinfo=None)
               and (message.metadata_json.get("adjustment_applied")
                    or message.metadata_json.get("adjustment_dismissed")) for message in messages):
            return None
        payload = brief.brief_json or {}
        stored_context = payload.get("recovery_context")
        if isinstance(stored_context, dict):
            context = self._refresh_recovery_dependencies(stored_context)
            return {**context, "active_brief_id": brief.id,
                    "decision_source_fingerprint": self._adjustment_schedule_fingerprint(plan)}
        try:
            missed_dates = [date.fromisoformat(item) for item in payload.get("missed_dates", [])]
            original_end_date = date.fromisoformat(payload["original_end_date"])
        except (KeyError, TypeError, ValueError):
            return None
        context = self._build_recovery_context(
            goal_id,
            missed_dates,
            int(payload.get("backlog_minutes", 0) or 0),
            original_end_date,
            bool(payload.get("include_non_execution_days", False)),
        )
        return {**context, "active_brief_id": brief.id,
                "decision_source_fingerprint": self._adjustment_schedule_fingerprint(plan)}

    def _close_recovery_drafts(self, goal_id: str, status: str) -> str | None:
        latest = self._latest_recovery_brief(goal_id)
        for brief in self.session.scalars(select(PlanningBrief).where(
            PlanningBrief.goal_id == goal_id, PlanningBrief.brief_kind == "RECOVERY",
            PlanningBrief.status == "DRAFT",
        )):
            brief.status = status if latest and brief.id == latest.id else "SUPERSEDED"
            if brief.status == "CONFIRMED":
                brief.confirmed_at = utc_now()
        return latest.id if latest else None

    def _refresh_recovery_dependencies(self, context: dict[str, object]) -> dict[str, object]:
        """Reconcile proven legacy references without calling a model or writing data."""
        from planner_api.dependency_resolver import PlanDependencyResolver

        resolver = PlanDependencyResolver(self.session)
        tasks = []
        repaired = False
        for item in context.get("backlog_tasks", []):
            task = self.session.get(PlanTask, str(item.get("task_id")))
            if task is None:
                tasks.append(item)
                continue
            constraints = []
            for reference in task.depends_on_json or []:
                resolved = resolver.resolve(reference, task)
                constraints.append({"reference": str(reference), "task_id": resolved.id if resolved else None,
                                    "status": resolved.status if resolved else "UNKNOWN"})
            old = item.get("dependency_constraints", [])
            repaired |= any(row.get("status") == "UNKNOWN" for row in old) and not any(
                row["status"] == "UNKNOWN" for row in constraints
            )
            tasks.append({**item, "dependency_constraints": constraints})
        updated = {**context, "backlog_tasks": tasks}
        if repaired and context.get("ai_allocation_generated"):
            assignments = self._ordered_balanced_recovery_assignments(
                tasks, context.get("allocation_candidate_days", [])
            )
            if len(assignments) == len(tasks):
                updated |= {"proposed_assignments": assignments, "unassigned_task_ids": [],
                            "dependency_blocked_task_ids": [], "dependency_ledger_reconciled": True}
                updated |= self._plan_recovery_displacement(
                    updated, assignments, int(context.get("soft_load_reference_minutes", 0) or 0)
                )
                if updated.get("direct_extension_dates") != context.get("direct_extension_dates"):
                    updated["direct_extension_confirmed"] = False
        return updated

    @staticmethod
    def _recovery_review(context: dict[str, object]) -> dict[str, object]:
        public = {key: value for key, value in context.items() if not key.startswith("_")}
        decisions = []
        if context.get("requires_direct_extension") and context.get("slight_extension_possible") and not context.get("direct_extension_confirmed"):
            decisions = ["ALLOW_EXTENSION"] if context.get("direct_extension_rejected") else ["ALLOW_EXTENSION", "KEEP_DEADLINE"]
        elif context.get("requires_load_confirmation"):
            decisions = ["ACCEPT_LOAD"]
        return {"token": hashlib.sha256(json.dumps(public, sort_keys=True, ensure_ascii=False).encode()).hexdigest(),
                "decisions": decisions, "extension_dates": context.get("direct_extension_dates", []),
                "extension_days": context.get("direct_extension_execution_days", 0),
                "is_recovery": True}

    def decide_recovery(self, goal_id: str, decision: str, token: str, *, feedback: str | None = None) -> PlanWorkspace:
        require_clarification_analysis_complete(self.session, goal_id)
        with _plan_generation_locks[goal_id]:
            return self._decide_recovery_locked(goal_id, decision, token, feedback=feedback)

    def _decide_recovery_locked(self, goal_id: str, decision: str, token: str, *, feedback: str | None = None) -> PlanWorkspace:
        self._get_review_inputs(goal_id)
        self._require_review_plan(goal_id)
        context = self._active_recovery_context(goal_id)
        if not context or self._recovery_review(context)["token"] != token:
            raise HTTPException(status_code=409, detail="恢复建议已变化，请重新读取后确认。")
        if decision not in self._recovery_review(context)["decisions"]:
            raise HTTPException(status_code=409, detail="这份建议当前不需要此项确认。")
        if decision == "ALLOW_EXTENSION":
            context = {**context, "direct_extension_confirmed": True, "direct_extension_rejected": False}
        elif decision == "KEEP_DEADLINE":
            context = {**context, "direct_extension_confirmed": False, "direct_extension_rejected": True}
        else:
            context = {**context, "requires_load_confirmation": False, "load_confirmed": True}
        brief = self.session.scalar(select(PlanningBrief).where(
            PlanningBrief.goal_id == goal_id, PlanningBrief.brief_kind == "RECOVERY", PlanningBrief.status == "DRAFT"
        ).order_by(PlanningBrief.version.desc()))
        brief.brief_json = {**(brief.brief_json or {}), "recovery_context": context}
        conversation = self._ensure_plan_conversation(goal_id)
        labels = {"ALLOW_EXTENSION": "允许这次顺延", "KEEP_DEADLINE": "保持原期限，不允许顺延", "ACCEPT_LOAD": "接受这次近期负担"}
        self._prepare_discussion_exchange(conversation, feedback or labels[decision], "REVISE")
        result = self._local_recovery_proposal(context)
        self._add_assistant_message(conversation, result.assistant_message, "REVISE", {
            "adjustment_proposal": result.model_dump(mode="json"), "recovery_brief_id": brief.id,
            "recovery_strategy": "AI_STRUCTURED_OPERATIONS", "recovery_decision": decision,
        })
        self.session.commit()
        return self.get_workspace(goal_id)

    @staticmethod
    def _local_recovery_proposal(
        recovery_context: dict[str, object],
        *,
        assistant_message: str | None = None,
        tradeoffs: list[str] | None = None,
        can_apply: bool = True,
        clarifying_question: str | None = None,
    ) -> PlanAdjustmentProposal:
        assignments = [
            item
            for item in recovery_context.get("proposed_assignments", [])
            if isinstance(item, dict)
        ]
        moves = [
            item
            for item in recovery_context.get("original_task_moves", [])
            if isinstance(item, dict)
        ]
        affected_weeks = sorted(
            {int(item["target_week"]) for item in assignments}
            | {int(item["source_week"]) for item in moves}
            | {int(item["target_week"]) for item in moves}
        )
        affected_stages = sorted({int(item["target_stage"]) for item in assignments})
        dates = sorted(
            {str(item["target_date"]) for item in assignments}
            | {str(item["source_date"]) for item in moves}
            | {str(item["target_date"]) for item in moves}
        )
        backlog = int(recovery_context.get("backlog_task_minutes", 0) or 0)
        peak = int(recovery_context.get("projected_peak_daily_minutes", 0) or 0)
        date_range = f"{dates[0]} 至 {dates[-1]}" if dates else "后续执行日"
        shifted_count = int(recovery_context.get("shifted_original_task_count", 0) or 0)
        shifted_minutes = int(recovery_context.get("shifted_original_minutes", 0) or 0)
        shift_explanation = (
            f"为避免近期单日硬堆积，另有 {shifted_count} 个原任务（约 {shifted_minutes} 分钟）"
            "按原顺序逐步向后顺延。"
            if shifted_count
            else "近期原任务无需顺延。"
        )
        unassigned = set(recovery_context.get("unassigned_task_ids", []))
        backlog_items = [
            item for item in recovery_context.get("backlog_tasks", []) if isinstance(item, dict)
        ]
        if backlog_items and not assignments and not unassigned and not clarifying_question:
            clarifying_question = (
                "当前恢复账本还没有生成任何可执行承接项。"
                "请重新生成恢复安排或缩小恢复范围；系统不会把普通计划调整误当成恢复承接。"
            )
        questions = [clarifying_question] if clarifying_question else []
        saved_question = recovery_context.get("allocation_clarifying_question")
        if saved_question and saved_question not in questions:
            questions.append(str(saved_question))
        if unassigned:
            titles = [
                str(item.get("title", item["task_id"]))
                for item in recovery_context.get("backlog_tasks", [])
                if isinstance(item, dict) and item.get("task_id") in unassigned
            ]
            blocked = set(recovery_context.get("dependency_blocked_task_ids", []))
            questions.append(
                f"仍有 {len(unassigned)} 项未安排：{'、'.join(titles[:3])}。"
                + ("前置依赖尚未解析或不在所选恢复范围内，需要先核对前置任务；" if blocked else
                   "请缩小恢复范围、补充可用时间，或说明是否允许提高近期负荷；")
                +
                "未安排项和已有进度会保留。"
            )
        if recovery_context.get("requires_direct_extension") and not recovery_context.get("direct_extension_confirmed"):
            if recovery_context.get("direct_extension_rejected"):
                questions.append("已记录保持原期限。当前安排超出原期限，请补充可用时间或调整恢复范围后再试算。")
            elif recovery_context.get("slight_extension_possible"):
                extension_dates = recovery_context.get("direct_extension_dates", [])
                questions.append(f"需要增加 {recovery_context.get('direct_extension_execution_days', 0)} 个顺延执行日，"
                                 f"结束日期调整到 {extension_dates[-1] if extension_dates else '待核对'}。是否允许这次顺延？")
            else:
                questions.append("原结束日期后 3 天内仍无法承载全部工作，请调整恢复范围或可用时间。")
        if recovery_context.get("requires_load_confirmation"):
            questions.append(f"近期最高单日约 {peak} 分钟，是否接受这个强度？")
        clarifying_question = "\n".join(dict.fromkeys(questions)) or None
        confirmation_message = (
            clarifying_question
            if clarifying_question
            else "如果你确认，将按这份操作表创建新安排。"
        )
        return PlanAdjustmentProposal(
            assistant_message=(
                (
                    f"AI 已在结构化任务账本内生成逐任务承接操作，影响日期为 {date_range}。"
                    f"{shift_explanation}系统复算后的最高单日总负担约 {peak} 分钟。"
                    f"{confirmation_message}"
                )
                if assistant_message
                else (
                    f"系统已按任务账本把约 {backlog} 分钟积压优先分配到 {date_range} 的近期执行日。"
                    f"{shift_explanation}预计最高单日总负担约 {peak} 分钟。"
                    f"{confirmation_message}"
                )
            ),
            user_intent_summary="优先在近期执行日补回积压，同时保留后续原有安排。",
            scope="WEEK" if len(affected_weeks) == 1 else "MULTI_SCOPE",
            affected_stage_orders=affected_stages,
            affected_weeks=affected_weeks,
            downstream_affected_weeks=[],
            proposed_changes=[
                f"将 {len(assignments)} 个未完成任务按原时长承接到 {date_range} 的执行日",
                (
                    f"将 {shifted_count} 个近期原任务按原顺序向后推移，不改写任务内容和时长"
                    if shifted_count
                    else "近期原任务无需推移"
                ),
            ],
            tradeoffs=[
                f"近期执行日负担会上升，系统复算的最高单日约 {peak} 分钟",
                f"共影响 {len(dates)} 个执行日，最后一个承接日为 {dates[-1] if dates else '暂无'}",
            ],
            unchanged_guarantees=[
                "已完成内容和历史记录保持不变",
                "原有任务的标题、时长和相对顺序保持不变",
                "最终目标和阶段路线保持不变",
            ],
            total_weeks_change=0,
            weekly_intensity_change="HIGHER",
            buffer_change="LESS",
            needs_clarification=bool(clarifying_question),
            clarifying_question=clarifying_question,
            can_apply=can_apply and bool(assignments or not backlog_items) and not clarifying_question,
        )

    @staticmethod
    def _normalize_ai_recovery_allocation(
        recovery_context: dict[str, object],
        allocation: RecoveryAllocationResult,
    ) -> dict[str, object]:
        tasks = {
            str(item["task_id"]): item
            for item in recovery_context.get("backlog_tasks", [])
            if isinstance(item, dict)
        }
        allocation_candidates = recovery_context.get(
            "allocation_candidate_days",
            recovery_context.get("recommended_local_days", []),
        )
        candidates = {
            str(item["plan_day_id"]): item
            for item in allocation_candidates
            if isinstance(item, dict)
        }
        assigned_ids = {item.backlog_task_id for item in allocation.assignments}
        unassigned_ids = set(allocation.unassigned_task_ids)
        unknown = (assigned_ids | unassigned_ids) - set(tasks)
        missing = set(tasks) - assigned_ids - unassigned_ids
        invalid_targets = {
            item.target_plan_day_id
            for item in allocation.assignments
            if item.target_plan_day_id not in candidates
        }
        if unknown or missing or invalid_targets:
            raise ProviderUnavailableError(
                "AI 生成的承接操作没有完整对应任务账本，原安排保持不变。",
                diagnostic=(
                    f"unknown={sorted(unknown)}, missing={sorted(missing)}, "
                    f"invalid_targets={sorted(invalid_targets)}"
                ),
            )
        out_of_scope_targets = {
            item.target_plan_day_id
            for item in allocation.assignments
            if item.target_plan_day_id not in candidates
        }
        proposed = []
        projected = {
            str(item["date"]): int(item["existing_minutes"]) for item in candidates.values()
        }
        if not out_of_scope_targets:
            for allocation_order, item in enumerate(allocation.assignments):
                target = candidates[item.target_plan_day_id]
                task = tasks[item.backlog_task_id]
                target_date = str(target["date"])
                projected[target_date] += int(task["estimate_minutes"])
                proposed.append(
                    {
                        "backlog_task_id": item.backlog_task_id,
                        "target_date": target_date,
                        "target_week": int(target["week_index"]),
                        "target_day": int(target["day_index"]),
                        "target_stage": int(target["stage_order"]),
                        "estimate_minutes": int(task["estimate_minutes"]),
                        "rationale": item.rationale,
                        "allocation_order": allocation_order,
                    }
                )
        # Validate AI placement against the persisted dependency ledger. The
        # model may choose a valid day for every task but still place a
        # dependent task before an unfinished prerequisite; filter that case
        # into an explicit unassigned result instead of silently publishing it.
        assignment_by_task = {str(item["backlog_task_id"]): item for item in proposed}
        blocked: set[str] = set()
        changed = True
        while changed:
            changed = False
            for task_id, item in assignment_by_task.items():
                if task_id in blocked:
                    continue
                constraints = tasks[task_id].get("dependency_constraints", [])
                for constraint in constraints if isinstance(constraints, list) else []:
                    if not isinstance(constraint, dict):
                        blocked.add(task_id)
                        changed = True
                        break
                    dependency_id = constraint.get("task_id")
                    status = str(constraint.get("status", "UNKNOWN"))
                    if status in {"COMPLETED", "CARRIED_OVER"}:
                        continue
                    if not dependency_id or str(dependency_id) not in assignment_by_task:
                        blocked.add(task_id)
                        changed = True
                        break
                    dependency_item = assignment_by_task[str(dependency_id)]
                    dependency_position = (
                        str(dependency_item["target_date"]),
                        int(dependency_item.get("allocation_order", 0)),
                    )
                    task_position = (
                        str(item["target_date"]),
                        int(item.get("allocation_order", 0)),
                    )
                    if str(dependency_id) in blocked or dependency_position >= task_position:
                        blocked.add(task_id)
                        changed = True
                        break
        if blocked:
            proposed = [item for item in proposed if str(item["backlog_task_id"]) not in blocked]
            unassigned_ids.update(blocked)
            assignment_by_task = {
                task_id: item
                for task_id, item in assignment_by_task.items()
                if task_id not in blocked
            }
        ai_peak = max(projected.values(), default=0) if proposed else 0
        rebalanced = PlanService._ordered_balanced_recovery_assignments(
            [tasks[task_id] for task_id in assignment_by_task],
            list(candidates.values()),
            {item.backlog_task_id: item.rationale for item in allocation.assignments},
        )
        if rebalanced:
            balanced_loads = {
                str(item["date"]): int(item["existing_minutes"]) for item in candidates.values()
            }
            for item in rebalanced:
                balanced_loads[str(item["target_date"])] += int(item["estimate_minutes"])
            balanced_peak = max(balanced_loads.values(), default=0)
            if out_of_scope_targets or balanced_peak < ai_peak:
                proposed = rebalanced
                projected = balanced_loads
        peak = max(projected.values(), default=0)
        soft_reference = int(recovery_context.get("soft_load_reference_minutes", 0) or 0)
        normalized = {
            **recovery_context,
            "allocation_clarifying_question": allocation.clarifying_question if allocation.needs_clarification else None,
            "proposed_assignments": proposed,
            "projected_peak_daily_minutes": peak,
            "ai_projected_peak_daily_minutes": ai_peak,
            "allocation_rebalanced": peak < ai_peak,
            "allocation_scope_corrected": bool(out_of_scope_targets),
            "dependency_blocked_task_ids": sorted(blocked),
            "unassigned_task_ids": sorted(unassigned_ids),
            "requires_load_confirmation": False,
            "ai_allocation_generated": True,
        } | PlanService._plan_recovery_displacement(
            recovery_context,
            proposed,
            soft_reference,
        )
        if normalized.get("direct_extension_dates") != recovery_context.get("direct_extension_dates"):
            normalized["direct_extension_confirmed"] = False
            normalized["direct_extension_rejected"] = False
        return normalized

    def _allocate_validated_recovery(
        self,
        requirement,
        route,
        current: PlanVersion,
        user_input: str,
        recovery_context: dict[str, object],
        previous_adjustment: PlanAdjustmentProposal | None = None,
    ) -> tuple[dict[str, object], RecoveryAllocationResult, bool]:
        validation_context = recovery_context
        last_error: ProviderUnavailableError | None = None
        for attempt in range(RECOVERY_ALLOCATION_VALIDATION_RETRIES + 1):
            allocation = self.discussion_provider.allocate_recovery(
                self._discussion_context(
                    requirement,
                    route,
                    current,
                    user_input,
                    previous_adjustment,
                    recovery_context=validation_context,
                    research_context=recovery_context.get("_research_context"),
                )
            )
            try:
                normalized = self._normalize_ai_recovery_allocation(recovery_context, allocation)
                from planner_api.goal_memory import save_memories
                conversation = self._ensure_plan_conversation(current.goal_id)
                save_memories(self.session, conversation, allocation.long_term_memories)
                return (
                    normalized,
                    allocation,
                    False,
                )
            except ProviderUnavailableError as error:
                last_error = error
                if attempt >= RECOVERY_ALLOCATION_VALIDATION_RETRIES:
                    break
                validation_context = {
                    **recovery_context,
                    "validation_feedback": {
                        "attempt": attempt + 1,
                        "diagnostic": error.diagnostic or str(error),
                        "instruction": (
                            "重新输出完整账本映射；每个 backlog task 必须恰好出现一次，"
                            "目标只能使用 allocation_candidate_days 中的 plan_day_id。"
                        ),
                    },
                }

        tasks = [
            item for item in recovery_context.get("backlog_tasks", []) if isinstance(item, dict)
        ]
        candidates = [
            item
            for item in recovery_context.get("allocation_candidate_days", [])
            if isinstance(item, dict)
        ]
        if not candidates:
            if last_error is not None:
                raise last_error
            raise ProviderUnavailableError("没有可用于承接积压任务的候选执行日。")
        assignments = []
        task_count = max(1, len(tasks))
        for index, task in enumerate(tasks):
            target_index = min(len(candidates) - 1, index * len(candidates) // task_count)
            assignments.append(
                {
                    "backlog_task_id": str(task["task_id"]),
                    "target_plan_day_id": str(candidates[target_index]["plan_day_id"]),
                    "rationale": "模型账本校验多次失败后，由系统按原任务顺序完成承接映射。",
                }
            )
        fallback = RecoveryAllocationResult.model_validate(
            {
                "assistant_message": (
                    "模型分配多次未完整覆盖任务账本，系统已按原任务顺序补全承接映射，"
                    "仍需经过负载、依赖和发布时间校验。"
                ),
                "assignments": assignments,
                "unassigned_task_ids": [],
                "tradeoffs": ["系统兜底优先保证任务不丢失，具体负载以预览复算结果为准。"],
            }
        )
        return self._normalize_ai_recovery_allocation(recovery_context, fallback), fallback, True

    @staticmethod
    def _plan_recovery_displacement(
        recovery_context: dict[str, object],
        backlog_assignments: list[dict[str, object]],
        soft_reference: int,
    ) -> dict[str, object]:
        """Give backlog priority and push movable original work forward as needed."""
        days = sorted(
            [item for item in recovery_context.get("candidate_days", []) if isinstance(item, dict)],
            key=lambda item: (str(item.get("date", "")), int(item.get("week_index", 0))),
        )
        if not days:
            return {
                "original_task_moves": [],
                "original_task_layout": [],
                "requires_direct_extension": bool(backlog_assignments),
                "direct_extension_minutes": sum(
                    int(item.get("estimate_minutes", 0)) for item in backlog_assignments
                ),
            }
        backlog_by_date = {str(item["date"]): 0 for item in days}
        for item in backlog_assignments:
            target_date = str(item.get("target_date", ""))
            if target_date in backlog_by_date:
                backlog_by_date[target_date] += int(item.get("estimate_minutes", 0))
        # This is an optimization target, not a hard user limit.  A single
        # indivisible backlog group may set a higher practical target.
        load_target = max(soft_reference, max(backlog_by_date.values(), default=0), 1)
        original_by_day: list[list[dict[str, object]]] = [
            [dict(task) for task in day.get("original_tasks", []) if isinstance(task, dict)]
            for day in days
        ]
        origin_by_task: dict[str, dict[str, object]] = {}
        for day, tasks in zip(days, original_by_day, strict=False):
            for task in tasks:
                origin_by_task[str(task["task_id"])] = {
                    "source_plan_day_id": str(day["plan_day_id"]),
                    "source_date": str(day["date"]),
                    "source_week": int(day["week_index"]),
                    "source_day": int(day["day_index"]),
                }

        for index, day in enumerate(days[:-1]):
            current_tasks = original_by_day[index]
            load = backlog_by_date[str(day["date"])] + sum(
                int(task.get("estimate_minutes", 0)) for task in current_tasks
            )
            while load > load_target:
                movable_index = next(
                    (
                        position
                        for position in range(len(current_tasks) - 1, -1, -1)
                        if bool(current_tasks[position].get("movable", False))
                    ),
                    None,
                )
                if movable_index is None:
                    break
                moved = current_tasks.pop(movable_index)
                original_by_day[index + 1].insert(0, moved)
                load -= int(moved.get("estimate_minutes", 0))

        projected_loads: dict[str, int] = {}
        final_target_by_task: dict[str, dict[str, object]] = {}
        layouts: list[dict[str, object]] = []
        for day, tasks in zip(days, original_by_day, strict=False):
            target_date = str(day["date"])
            projected_loads[target_date] = backlog_by_date[target_date] + sum(
                int(task.get("estimate_minutes", 0)) for task in tasks
            )
            layouts.append(
                {
                    "target_plan_day_id": str(day["plan_day_id"]),
                    "target_date": target_date,
                    "target_week": int(day["week_index"]),
                    "target_day": int(day["day_index"]),
                    "source_task_ids": [str(task["task_id"]) for task in tasks],
                }
            )
            for task in tasks:
                final_target_by_task[str(task["task_id"])] = {
                    "target_plan_day_id": str(day["plan_day_id"]),
                    "target_date": target_date,
                    "target_week": int(day["week_index"]),
                    "target_day": int(day["day_index"]),
                }
        moves: list[dict[str, object]] = []
        for task_id, target in final_target_by_task.items():
            origin = origin_by_task[task_id]
            if origin["source_plan_day_id"] == target["target_plan_day_id"]:
                continue
            task = next(
                task
                for tasks in original_by_day
                for task in tasks
                if str(task["task_id"]) == task_id
            )
            moves.append(
                {
                    "source_task_id": task_id,
                    "title": str(task.get("title", "")),
                    "estimate_minutes": int(task.get("estimate_minutes", 0)),
                    **origin,
                    **target,
                }
            )
        last_day = days[-1]
        final_tasks = list(original_by_day[-1])
        final_load = projected_loads[str(last_day["date"])]
        extension_tasks: list[dict[str, object]] = []
        while final_load > load_target:
            movable_index = next(
                (
                    position
                    for position in range(len(final_tasks) - 1, -1, -1)
                    if bool(final_tasks[position].get("movable", False))
                ),
                None,
            )
            if movable_index is None:
                break
            moved = final_tasks.pop(movable_index)
            extension_tasks.insert(0, moved)
            final_load -= int(moved.get("estimate_minutes", 0))
        extension_groups: list[list[str]] = []
        extension_group_loads: list[int] = []
        for task in extension_tasks:
            minutes = int(task.get("estimate_minutes", 0))
            if not extension_groups or extension_group_loads[-1] + minutes > load_target:
                extension_groups.append([])
                extension_group_loads.append(0)
            extension_groups[-1].append(str(task["task_id"]))
            extension_group_loads[-1] += minutes
        unresolved_overload = max(0, final_load - load_target)
        original_end = date.fromisoformat(
            str(recovery_context.get("original_end_date", days[-1]["date"]))
        )
        extension_dates = [
            (original_end + timedelta(days=offset)).isoformat()
            for offset in range(1, len(extension_groups) + 1)
        ]
        slight_extension_possible = (
            bool(extension_groups)
            and len(extension_groups) <= RECOVERY_END_DATE_GRACE_DAYS
            and unresolved_overload == 0
        )
        return {
            "original_task_moves": moves,
            "original_task_layout": layouts,
            "displacement_load_target_minutes": load_target,
            "projected_daily_minutes_after_displacement": projected_loads,
            "projected_peak_daily_minutes": max(projected_loads.values(), default=0),
            "shifted_original_task_count": len(moves),
            "shifted_original_minutes": sum(int(item["estimate_minutes"]) for item in moves),
            "preserve_future_schedule": not bool(moves),
            "last_shifted_date": max(
                (str(item["target_date"]) for item in moves),
                default=None,
            ),
            "requires_direct_extension": bool(extension_tasks or unresolved_overload),
            "direct_extension_minutes": sum(
                int(item.get("estimate_minutes", 0)) for item in extension_tasks
            )
            + unresolved_overload,
            "direct_extension_task_groups": extension_groups,
            "direct_extension_day_loads": extension_group_loads,
            "direct_extension_execution_days": len(extension_groups),
            "direct_extension_dates": extension_dates,
            "slight_extension_possible": slight_extension_possible,
            "direct_extension_unresolved_overload_minutes": unresolved_overload,
        }

    @staticmethod
    def _dependency_ordered_recovery_tasks(
        tasks: list[dict[str, object]],
    ) -> list[dict[str, object]]:
        """Return a stable dependency order, prioritizing tasks that unlock more work."""
        by_id = {str(item.get("task_id")): item for item in tasks if item.get("task_id")}
        prerequisites: dict[str, set[str]] = {task_id: set() for task_id in by_id}
        dependents: dict[str, set[str]] = {task_id: set() for task_id in by_id}
        for task_id, item in by_id.items():
            constraints = item.get("dependency_constraints", [])
            for constraint in constraints if isinstance(constraints, list) else []:
                if not isinstance(constraint, dict):
                    return []
                status = str(constraint.get("status", "UNKNOWN"))
                if status in {"COMPLETED", "CARRIED_OVER"}:
                    continue
                dependency_id = constraint.get("task_id")
                if not dependency_id or str(dependency_id) not in by_id:
                    return []
                dependency_id = str(dependency_id)
                prerequisites[task_id].add(dependency_id)
                dependents[dependency_id].add(task_id)

        descendant_cache: dict[str, set[str]] = {}

        def descendants(task_id: str, visiting: set[str] | None = None) -> set[str]:
            if task_id in descendant_cache:
                return descendant_cache[task_id]
            visiting = set(visiting or ())
            if task_id in visiting:
                return set()
            visiting.add(task_id)
            result = set(dependents[task_id])
            for dependent_id in dependents[task_id]:
                result.update(descendants(dependent_id, visiting))
            descendant_cache[task_id] = result
            return result

        ordered: list[dict[str, object]] = []
        emitted: set[str] = set()
        remaining = set(by_id)
        while remaining:
            eligible = [task_id for task_id in remaining if prerequisites[task_id] <= emitted]
            if not eligible:
                return []
            task_id = min(
                eligible,
                key=lambda value: (
                    -len(descendants(value)),
                    -int(bool(by_id[value].get("is_required", True))),
                    -int(by_id[value].get("priority", 0)),
                    str(by_id[value].get("source_date", "")),
                    int(by_id[value].get("source_week", 0)),
                    int(by_id[value].get("source_day", 0)),
                    int(by_id[value].get("order", 0)),
                    value,
                ),
            )
            ordered.append(by_id[task_id])
            emitted.add(task_id)
            remaining.remove(task_id)
        return ordered

    @staticmethod
    def _ordered_balanced_recovery_assignments(
        tasks: list[dict[str, object]],
        candidates: list[dict[str, object]],
        rationales: dict[str, str] | None = None,
    ) -> list[dict[str, object]]:
        """Keep source order while minimizing the peak load in the nearby window."""
        if not tasks or not candidates:
            return []
        has_dependencies = any(item.get("dependency_constraints") for item in tasks)
        ordered_tasks = (
            PlanService._dependency_ordered_recovery_tasks(tasks)
            if has_dependencies
            else sorted(
                tasks,
                key=lambda item: (
                    str(item.get("source_date", "")),
                    int(item.get("source_week", 0)),
                    int(item.get("source_day", 0)),
                    int(item.get("order", 0)),
                ),
            )
        )
        if not ordered_tasks:
            return []
        ordered_days = sorted(
            candidates,
            key=lambda item: (
                str(item.get("date", "")),
                int(item.get("week_index", 0)),
                int(item.get("day_index", 0)),
            ),
        )
        prefix = [0]
        for task in ordered_tasks:
            prefix.append(prefix[-1] + int(task.get("estimate_minutes", 0)))

        task_count = len(ordered_tasks)
        day_count = len(ordered_days)
        infinity = 10**12
        costs = [[infinity] * (task_count + 1) for _ in range(day_count + 1)]
        previous = [[-1] * (task_count + 1) for _ in range(day_count + 1)]
        costs[0][0] = 0
        for day_number in range(1, day_count + 1):
            existing = int(ordered_days[day_number - 1].get("existing_minutes", 0))
            for used in range(task_count + 1):
                for before in range(used + 1):
                    if costs[day_number - 1][before] == infinity:
                        continue
                    load = existing + prefix[used] - prefix[before]
                    if load > int(ordered_days[day_number - 1].get("maximum_total_minutes", 1440)):
                        continue
                    candidate_cost = max(costs[day_number - 1][before], load)
                    if candidate_cost < costs[day_number][used]:
                        costs[day_number][used] = candidate_cost
                        previous[day_number][used] = before

        boundaries: list[tuple[int, int, int]] = []
        used = task_count
        for day_number in range(day_count, 0, -1):
            before = previous[day_number][used]
            if before < 0:
                return []
            boundaries.append((day_number - 1, before, used))
            used = before
        boundaries.reverse()
        rationale_by_task = rationales or {}
        result: list[dict[str, object]] = []
        for day_index, start, end in boundaries:
            target = ordered_days[day_index]
            for task in ordered_tasks[start:end]:
                task_id = str(task["task_id"])
                result.append(
                    {
                        "backlog_task_id": task_id,
                        "target_date": str(target["date"]),
                        "target_week": int(target["week_index"]),
                        "target_day": int(target["day_index"]),
                        "target_stage": int(target["stage_order"]),
                        "estimate_minutes": int(task["estimate_minutes"]),
                        "rationale": rationale_by_task.get(task_id, "按原任务顺序均衡承接近期负担"),
                    }
                )
        return result

    @staticmethod
    def _recovery_local_window_days(remaining_execution_days: int) -> int:
        if remaining_execution_days <= 0:
            return 0
        return min(
            remaining_execution_days,
            RECOVERY_LOCAL_WINDOW_MAX_DAYS,
            max(
                RECOVERY_LOCAL_WINDOW_MIN_DAYS,
                ceil(remaining_execution_days * RECOVERY_LOCAL_WINDOW_RATIO),
            ),
        )

    @staticmethod
    def _constrain_recovery_proposal(
        result: PlanAdjustmentProposal,
        recovery_context: dict[str, object],
    ) -> PlanAdjustmentProposal:
        """Prefer the deterministic local window whenever it can carry backlog.

        The model may conservatively return OVERALL after seeing a long plan.
        The recovery ledger already tells us whether the nearby window is
        sufficient, so keep the model's explanation but normalize its scope to
        the smallest executable range in that case.
        """
        recommended_weeks = sorted(
            {int(item) for item in recovery_context.get("recommended_local_weeks", [])}
        )
        candidate_days = recovery_context.get("candidate_days", [])
        backlog = int(recovery_context.get("backlog_task_minutes", 0) or 0)
        if not recommended_weeks or not candidate_days or backlog <= 0:
            return result
        if result.needs_clarification:
            return result
        if (
            set(result.affected_weeks).issubset(set(recommended_weeks))
            and result.scope != "OVERALL"
        ):
            return result
        result.scope = "WEEK" if len(recommended_weeks) == 1 else "MULTI_SCOPE"
        result.affected_weeks = recommended_weeks
        result.downstream_affected_weeks = []
        result.affected_stage_orders = (
            sorted(
                {
                    int(day.get("stage_order", 0))
                    for day in candidate_days
                    if isinstance(day, dict) and int(day.get("week_index", 0)) in recommended_weeks
                }
            )
            or result.affected_stage_orders
        )
        result.unchanged_guarantees = list(
            dict.fromkeys(
                [
                    *result.unchanged_guarantees,
                    "近期窗口足以承接积压，其他后续执行日保持原安排不变",
                ]
            )
        )
        result.assistant_message = (
            f"已优先把积压安排到最近的 {len(recommended_weeks)} 个执行周，"
            "后续未受影响的安排保持原样；只有近期承载不足时才会扩大调整范围。"
        )
        return result

    @staticmethod
    def _with_recovery_preview_options(context, options):
        from copy import deepcopy
        updated = deepcopy(context)
        days = sorted([day for day in context.get("candidate_days", [])
            if str(context["today"]) <= str(day["date"]) <= str(context["original_end_date"])], key=lambda day: day["date"])
        tasks = context.get("backlog_tasks", [])
        action = options["action"]
        if action == "MOVE_TASK":
            source_id, target_date = options.get("source_task_id"), options.get("target_date")
            day = next((day for day in days if str(day["date"]) == str(target_date)), None)
            assignment = next((row for row in updated.get("proposed_assignments", []) if row["backlog_task_id"] == source_id), None)
            if day is None or assignment is None:
                raise HTTPException(409, "只能把本次所选补回任务移到原结束日期前的后续执行日。请更换日期，其他范围可继续文字讨论。")
            assignment.update(target_date=str(day["date"]), target_week=day["week_index"], target_day=day["day_index"], target_stage=day["stage_order"])
        elif action in {"SPREAD", "DAILY_LIMIT"}:
            if action == "DAILY_LIMIT":
                limit = int(options.get("daily_limit_minutes") or 0)
                if not 5 <= limit <= 1440:
                    raise HTTPException(422, "请填写每天总用时上限。")
                updated["daily_total_limit_minutes"] = limit
            offer = updated.get("availability_offer")
            candidates = [{**day, "maximum_total_minutes": min(
                int(updated.get("daily_total_limit_minutes") or 1440),
                int(day["existing_minutes"]) + int(offer["extra_minutes_per_execution_day"]) if offer else 1440)} for day in days
                if not offer or str(day["date"]) >= str(offer["from_date"])]
            updated["proposed_assignments"] = PlanService._ordered_balanced_recovery_assignments(tasks, candidates)
        else:
            raise HTTPException(422, "不支持的恢复调整方式。")
        assignments = updated.get("proposed_assignments", [])
        by_task = {row["backlog_task_id"]: row for row in assignments}
        ordered = PlanService._dependency_ordered_recovery_tasks(tasks)
        previous_date = ""
        for task in ordered:
            row = by_task.get(task["task_id"])
            if row is None:
                continue
            if str(row["target_date"]) < previous_date or any(
                    str(dep.get("status", "UNKNOWN")) not in {"COMPLETED", "CARRIED_OVER"}
                    and (dep.get("task_id") not in by_task or str(by_task[dep["task_id"]]["target_date"]) > str(row["target_date"]))
                    for dep in task.get("dependency_constraints", [])):
                raise HTTPException(409, "这个日期会打乱原任务顺序或前置关系，请同时讨论相关任务，或换一个日期。")
            previous_date = str(row["target_date"])
        loads = {str(day["date"]): int(day["existing_minutes"]) for day in days}
        for row in assignments:
            if str(row["target_date"]) not in loads:
                raise HTTPException(409, "草案包含范围外的日期，请重新读取恢复方案。")
            loads[str(row["target_date"])] += int(row["estimate_minutes"])
        if action == "MOVE_TASK":
            offer = updated.get("availability_offer")
            for day in days:
                maximum = int(updated.get("daily_total_limit_minutes") or 1440)
                if offer:
                    maximum = min(maximum, int(day["existing_minutes"]) + (int(offer["extra_minutes_per_execution_day"]) if str(day["date"]) >= str(offer["from_date"]) else 0))
                if loads[str(day["date"])] > maximum:
                    raise HTTPException(409, "这个日期超出你确认的每日用时上限，请换一天或先补充可用时间。")
        missing = sorted({str(task["task_id"]) for task in tasks} - set(by_task))
        return {**updated, "unassigned_task_ids": missing, "ai_required": False, "ai_allocation_generated": False,
            "projected_peak_daily_minutes": max(loads.values(), default=0), "original_task_moves": [],
            "original_task_layout": [], "shifted_original_task_count": 0, "shifted_original_minutes": 0,
            "requires_direct_extension": False, "direct_extension_dates": [],
            "allocation_clarifying_question": "这个用时上限内放不下全部积压，请增加时间、缩小范围或选择顺延。" if missing else None}

    @staticmethod
    def _with_recovery_availability_offer(context, offer):
        """Use only the user's additional budget; never enlarge it to make a draft fit."""
        from_date = str(offer["from_date"])
        extra = int(offer["extra_minutes_per_execution_day"])
        if not 5 <= extra <= 480 or not str(context["today"]) <= from_date <= str(context["original_end_date"]):
            raise HTTPException(422, "额外可用时间须为每天 5–480 分钟，起始日期须在今天至原结束日期之间。")
        days = sorted((day for day in context.get("candidate_days", [])
            if from_date <= str(day["date"]) <= str(context["original_end_date"])), key=lambda day: day["date"])
        tasks = context.get("backlog_tasks", [])
        ordered = PlanService._dependency_ordered_recovery_tasks(tasks)
        assignments, unassigned = [], []
        added = {str(day["date"]): 0 for day in days}
        assigned_ids = set()
        cursor = 0
        for task in ordered:
            constraints = task.get("dependency_constraints", [])
            if any(str(dep.get("status", "UNKNOWN")) not in {"COMPLETED", "CARRIED_OVER"}
                   and str(dep.get("task_id")) not in assigned_ids for dep in constraints):
                unassigned.append(task["task_id"])
                continue
            minutes = int(task["estimate_minutes"])
            while cursor < len(days) and added[str(days[cursor]["date"])] + minutes > extra:
                cursor += 1
            if cursor == len(days):
                unassigned.append(task["task_id"])
                continue
            day = days[cursor]
            added[str(day["date"])] += minutes
            assigned_ids.add(str(task["task_id"]))
            assignments.append({"backlog_task_id": task["task_id"], "target_date": str(day["date"]),
                "target_week": day["week_index"], "target_day": day["day_index"], "target_stage": day["stage_order"],
                "estimate_minutes": minutes, "rationale": "按用户明确补充的每日额外时间上限承接"})
        unassigned = sorted({str(task["task_id"]) for task in tasks} - assigned_ids)
        peak = max((int(day["existing_minutes"]) + added[str(day["date"])] for day in days), default=0)
        return {**context, "availability_offer": offer, "proposed_assignments": assignments,
            "unassigned_task_ids": unassigned, "projected_peak_daily_minutes": peak,
            "ai_required": False, "ai_allocation_generated": False, "original_task_moves": [],
            "original_task_layout": [], "shifted_original_task_count": 0, "shifted_original_minutes": 0,
            "requires_direct_extension": False, "direct_extension_dates": [], "direct_extension_confirmed": False,
            "allocation_clarifying_question": (f"按你补充的每天额外 {extra} 分钟，仍有 {len(unassigned)} 项放不下；请增加可用时间、缩小补回范围或选择顺延。" if unassigned else None)}

    @staticmethod
    def _assert_recovery_budget(context, schedule=None):
        """Keep explicit user budgets binding through model turns and publication."""
        offer = context.get("availability_offer")
        limit = context.get("daily_total_limit_minutes")
        if not offer and not limit:
            return
        days = {str(day["date"]): day for day in context.get("candidate_days", [])}
        loads = {key: int(day["existing_minutes"]) for key, day in days.items()}
        for row in context.get("proposed_assignments", []):
            target = str(row["target_date"])
            if target not in days or target > str(context["original_end_date"]) or (offer and target < str(offer["from_date"])):
                raise HTTPException(409, "这份安排超出你确认的可用日期，请重新预览或补充可用时间。")
            loads[target] += int(row["estimate_minutes"])
        if schedule is not None:
            loads = {key: 0 for key in days}
            for row in schedule:
                if str(row["date"]) in loads:
                    loads[str(row["date"])] += int(row["minutes"])
        for target, load in loads.items():
            maximum = int(limit or 1440)
            if offer:
                maximum = min(maximum, int(days[target]["existing_minutes"]) + (int(offer["extra_minutes_per_execution_day"]) if target >= str(offer["from_date"]) else 0))
            if load > maximum:
                raise HTTPException(409, f"{target} 需要 {load} 分钟，超出你确认的 {maximum} 分钟上限。请重新分配，或明确补充可用时间。")

    def revise_recovery_plan(
        self,
        goal_id: str,
        missed_dates: list[date],
        backlog_minutes: int,
        original_end_date: date,
        include_non_execution_days: bool,
        preview_id: str | None = None,
        instruction: str = "",
        *,
        research_run_id: str | None = None,
        availability_offer: dict[str, object] | None = None,
    ) -> PlanWorkspace:
        """Start C3-C with a persisted recovery brief and resumable week batches."""
        if research_run_id:
            run = self.session.get(ResearchRun, research_run_id)
            if run is None or run.goal_id != goal_id:
                raise HTTPException(404, "Research run not found")
            if run.status in {"QUEUED", "RUNNING", "CANCEL_REQUESTED"}:
                raise HTTPException(409, "Research run is not complete")
        scoped_preview = None
        if preview_id:
            from planner_api.execution_service import ExecutionService
            from planner_api.recovery_split import validated_preview

            operation, _goal, _plan = validated_preview(ExecutionService(self.session), goal_id, preview_id)
            scoped_preview = operation.preview_json
            missed_dates = [date.fromisoformat(day) for day in scoped_preview["missed_dates"]]
            backlog_minutes = scoped_preview["backlog_minutes"]
            original_end_date = date.fromisoformat(scoped_preview["original_end_date"])
            include_non_execution_days = scoped_preview["include_non_execution_days"]
        context_pack, brief = self._ensure_planning_brief(goal_id)
        # 新一轮恢复方案开始时，旧轮次的周批次不再属于当前预览。
        stale_batches = self.session.scalars(
            select(GenerationBatch).where(
                GenerationBatch.goal_id == goal_id,
                GenerationBatch.scope_type == "RECOVERY_WEEK",
                GenerationBatch.status.in_({"QUEUED", "RUNNING", "FAILED"}),
            )
        )
        for stale in stale_batches:
            stale.status = "CANCELLED"
        recovery_context = self._build_recovery_context(
            goal_id,
            missed_dates,
            backlog_minutes,
            original_end_date,
            include_non_execution_days,
            scoped_preview["recovery_scope"]["selected_task_ids"] if scoped_preview else None,
        )
        if scoped_preview:
            recovery_context = {**recovery_context,
                                "scope_preview_id": preview_id,
                                "recovery_scope": scoped_preview["recovery_scope"],
                                "blocking_snapshot": scoped_preview.get("time_preview", {}),
                                "user_instruction": instruction.strip()}
        if availability_offer:
            recovery_context = self._with_recovery_availability_offer(recovery_context, availability_offer)
        research_context = None
        research_status = None
        if research_run_id:
            from planner_api.research.consumer import ResearchContextService

            conversation = self._ensure_plan_conversation(goal_id)
            research_context, _, research_status = ResearchContextService(self.session).prepare(
                goal_id, instruction or "讨论积压任务恢复", phase="RECOVERY", run_id=research_run_id,
                conversation_id=conversation.id, reuse_latest=False,
            )
            if research_context:
                recovery_context = {**recovery_context, "_research_context": research_context}
        recovery_payload = {
            "missed_dates": [item.isoformat() for item in missed_dates],
            "backlog_minutes": backlog_minutes,
            "original_end_date": original_end_date.isoformat(),
            "include_non_execution_days": include_non_execution_days,
            "locked_guarantees": ["已完成任务", "最终目标", "原结束日期边界"],
            "recovery_context": recovery_context,
        }
        current = self._require_review_plan(goal_id)
        self._close_recovery_drafts(goal_id, "SUPERSEDED")
        recovery_brief = PlanningBrief(
            goal_id=brief.goal_id,
            context_pack_id=brief.context_pack_id,
            plan_version_id=current.id,
            brief_kind="RECOVERY",
            version=(
                self.session.scalar(
                    select(func.max(PlanningBrief.version)).where(
                        PlanningBrief.goal_id == goal_id,
                        PlanningBrief.brief_kind == "RECOVERY",
                    )
                )
                or 0
            )
            + 1,
            status="DRAFT",
            brief_json=recovery_payload,
        )
        self.session.add(recovery_brief)
        self.session.commit()
        if availability_offer or not recovery_context.get("ai_required", True):
            conversation = self._ensure_plan_conversation(goal_id)
            local_request = (
                f"这是一次中断恢复重规划：错过 {len(missed_dates)} 个执行日，"
                f"积压约 {backlog_minutes} 分钟。请优先在近期执行日追赶，"
                "后续原安排尽量不变。"
            )
            if scoped_preview:
                local_request += (
                    f"本次仅处理所选 {len(scoped_preview['recovery_scope']['selected_task_ids'])} 项积压；"
                    f"未选 {len(scoped_preview['recovery_scope']['deferred_task_ids'])} 项保留原日期和进度，不删除或承接。"
                    "不可擅自改变目标、期限、任务内容或完成次数。"
                    + (f"用户补充：{instruction.strip()}" if instruction.strip() else "")
                )
            self._add_user_message(
                conversation,
                local_request,
                "REVISE",
            )
            proposal = self._local_recovery_proposal(recovery_context)
            self._add_assistant_message(
                conversation,
                proposal.assistant_message,
                "REVISE",
                {
                    "prompt_version": PLAN_ADJUSTMENT_PROMPT_VERSION,
                    "adjustment_proposal": proposal.model_dump(mode="json"),
                    "recovery_brief_id": recovery_brief.id,
                    "recovery_strategy": "DETERMINISTIC_LOCAL",
                    "research_run_id": research_run_id,
                    "research_status": research_status,
                },
            )
            self.session.commit()
            return self.get_workspace(goal_id)
        goal, requirement, route = self._get_review_inputs(goal_id)
        current = self._require_review_plan(goal_id)
        conversation = self._ensure_plan_conversation(goal_id)
        request_text = (
            f"近期窗口只能承载约 {recovery_context.get('local_capacity_minutes', 0)} 分钟，"
            f"但积压为 {recovery_context.get('backlog_task_minutes', backlog_minutes)} 分钟。"
            "请生成积压任务的近期逐任务承接操作；系统会在超载时保持原任务内容和顺序，"
            "将部分原任务逐步向后推移。"
        )
        if scoped_preview:
            request_text += (f"本次仅处理所选 {len(scoped_preview['recovery_scope']['selected_task_ids'])} 项积压；"
                             f"未选 {len(scoped_preview['recovery_scope']['deferred_task_ids'])} 项保留原日期和进度，不删除或承接。"
                             "不可擅自改变目标、期限、任务内容或完成次数。"
                             + (f"用户补充：{instruction.strip()}" if instruction.strip() else ""))
        self._add_user_message(conversation, request_text, "REVISE")
        self.session.commit()
        orchestration = self.orchestrator.prepare(
            goal.id,
            "RECOVERY_PLANNING",
            {
                **context_pack.facts_json,
                "user_request": request_text,
                "current_plan": self._plan_payload(current),
                "recovery_context": recovery_context,
            },
            plan_version_id=current.id,
            context_pack_id=context_pack.id,
            minimum_tier="L2",
        )
        recovery_context = {
            **recovery_context,
            "_agent_orchestration": orchestration,
        }
        try:
            recovery_context, allocation, allocation_fallback = self._allocate_validated_recovery(
                requirement,
                route,
                current,
                request_text,
                recovery_context,
            )
            recovery_brief.brief_json = {
                **recovery_brief.brief_json,
                "recovery_context": recovery_context,
            }
            load_question = None
            if recovery_context.get("requires_direct_extension"):
                remaining = int(recovery_context.get("direct_extension_minutes", 0) or 0)
                extension_days = int(
                    recovery_context.get("direct_extension_execution_days", 0) or 0
                )
                if recovery_context.get("slight_extension_possible"):
                    extension_dates = recovery_context.get("direct_extension_dates", [])
                    load_question = (
                        f"积压优先并逐日推移原任务后，原结束日期内仍有约 {remaining} 分钟需要承接。"
                        f"建议增加 {extension_days} 个顺延日，结束日期调整到 "
                        f"{extension_dates[-1] if extension_dates else '原日期后 3 天内'}；"
                        "请确认是否允许这次小幅顺延。"
                    )
                else:
                    load_question = (
                        f"积压优先并逐日推移原任务后，仍有约 {remaining} 分钟无法承载。"
                        "即使把结束日期延后 3 天也装不下；需要选择更长顺延或减少任务量，"
                        "系统不会把这些工作硬塞进已有日期。"
                    )
            proposal = self._local_recovery_proposal(
                recovery_context,
                assistant_message=allocation.assistant_message,
                tradeoffs=allocation.tradeoffs,
                can_apply=(
                    not allocation.unassigned_task_ids
                    and not allocation.needs_clarification
                    and not load_question
                ),
                clarifying_question=allocation.clarifying_question or load_question,
            )
            self._add_assistant_message(
                conversation,
                proposal.assistant_message,
                "REVISE",
                {
                    "prompt_version": RECOVERY_ALLOCATION_PROMPT_VERSION,
                    "adjustment_proposal": proposal.model_dump(mode="json"),
                    "recovery_brief_id": recovery_brief.id,
                    "recovery_strategy": (
                        "DETERMINISTIC_LEDGER_FALLBACK"
                        if allocation_fallback
                        else "AI_STRUCTURED_OPERATIONS"
                    ),
                    "research_run_id": research_run_id,
                    "research_status": research_status,
                },
            )
            self.orchestrator.complete(
                orchestration["run_id"],
                {
                    "status": "PASSED",
                    "assigned_tasks": len(recovery_context.get("proposed_assignments", [])),
                    "can_apply": proposal.can_apply,
                },
            )
        except ClarificationProviderError as error:
            self.orchestrator.fail(orchestration["run_id"], error)
            self._add_failed_message(conversation, error, "REVISE")
        self.session.commit()
        return self.get_workspace(goal_id)

    def handoff_recovery_discussion(self, goal_id: str, preview_id: str) -> PlanWorkspace:
        """Carry a validated recovery preview into the existing plan discussion."""
        from planner_api.execution_service import ExecutionService
        from planner_api.recovery_split import validated_preview

        operation, _goal, _plan = validated_preview(ExecutionService(self.session), goal_id, preview_id)
        value = operation.preview_json
        return self.revise_recovery_plan(
            goal_id,
            [date.fromisoformat(item) for item in value["missed_dates"]],
            int(value["backlog_minutes"]),
            date.fromisoformat(value["original_end_date"]),
            bool(value["include_non_execution_days"]),
            preview_id,
            "请根据所选范围和阻塞明细，先讨论可行的拆分、目标或期限取舍；确认前不要改动正式计划。",
        )

    def retry_recovery_batch(self, goal_id: str, batch_id: str) -> GenerationBatchRead:
        """Retry exactly one failed/queued recovery week without publishing a plan."""
        goal, requirement, route = self._get_review_inputs(goal_id)
        current = self._require_review_plan(goal_id)
        batch = self.session.get(GenerationBatch, batch_id)
        if batch is None or batch.goal_id != goal_id or batch.scope_type != "RECOVERY_WEEK":
            raise HTTPException(status_code=404, detail="Recovery batch not found")
        latest_recovery_id = self.session.scalar(
            select(PlanningBrief.id)
            .where(
                PlanningBrief.goal_id == goal_id,
                PlanningBrief.brief_kind == "RECOVERY",
            )
            .order_by(PlanningBrief.version.desc())
            .limit(1)
        )
        if latest_recovery_id is None or batch.brief_id != latest_recovery_id:
            raise HTTPException(
                status_code=409, detail="Recovery batch belongs to an older recovery attempt"
            )
        week = int(batch.scope_json.get("week", 0))
        if week < 1 or week > current.total_weeks:
            raise HTTPException(status_code=409, detail="Recovery batch scope is invalid")
        conversation = self._ensure_plan_conversation(goal_id)
        messages = self._conversation_messages(conversation.id)
        latest_recovery_id = self.session.scalar(
            select(PlanningBrief.id)
            .where(PlanningBrief.goal_id == goal_id, PlanningBrief.brief_kind == "RECOVERY")
            .order_by(PlanningBrief.version.desc())
            .limit(1)
        )
        proposal = self._pending_adjustment(messages, latest_recovery_id)
        if proposal is None:
            raise HTTPException(status_code=409, detail="No pending recovery proposal exists")
        batch.status = "RUNNING"
        batch.attempt_count += 1
        batch.failure_code = None
        batch.failure_message = None
        self.session.commit()
        try:
            result = self.discussion_provider.apply(
                self._discussion_context(
                    requirement,
                    route,
                    current,
                    "仅重新生成恢复方案中失败的这一周，保持其他周和已锁定内容不变。",
                    proposal,
                    application_requested_weeks=[week],
                )
            )
            self._validate_adjustment_batch(
                result, [week], current.total_weeks + proposal.total_weeks_change
            )
            self._validate_locked_days_omitted(current, result)
        except Exception as error:
            self._fail_compiler_batch(batch, "RECOVERY_BATCH_FAILED", str(error))
            self.session.commit()
            raise HTTPException(
                status_code=502, detail="这一周暂时生成失败，可以稍后重试"
            ) from error
        self._complete_compiler_batch(batch, result.model_dump(mode="json"))
        self.session.commit()
        return self._generation_batch_read(batch)

    def discard_adjustment(self, goal_id: str) -> PlanWorkspace:
        goal, _, _ = self._get_review_inputs(goal_id)
        self._require_review_plan(goal_id)
        conversation = self._ensure_plan_conversation(goal_id)
        messages = self._conversation_messages(conversation.id)
        if self._pending_adjustment(messages) is None:
            return self.get_workspace(goal_id)
        recovery_id = self._close_recovery_drafts(goal_id, "DISMISSED")
        for preview in self.session.scalars(select(PlanAdjustmentPreview).where(
            PlanAdjustmentPreview.goal_id == goal_id, PlanAdjustmentPreview.status == "PREVIEW",
        )):
            preview.status = "INVALID"
        self._add_assistant_message(
            conversation,
            "已放弃这份调整建议，当前时间安排没有变化。",
            "REVISE",
            {
                "prompt_version": PLAN_ADJUSTMENT_PROMPT_VERSION,
                "adjustment_dismissed": True,
                "recovery_brief_id": recovery_id,
            },
        )
        goal.last_active_at = utc_now()
        self.session.commit()
        return self.get_workspace(goal_id)

    def apply_adjustment(
        self,
        goal_id: str,
        preview_id: str | None = None,
        accept_unallocated: bool = False,
        schedule_overrides: list[dict[str, object]] | None = None,
    ) -> PlanWorkspace:
        require_clarification_analysis_complete(self.session, goal_id)
        with _plan_generation_locks[goal_id]:
            if preview_id:
                operation = self.session.get(PlanAdjustmentPreview, preview_id)
                if (
                    operation is None
                    or operation.goal_id != goal_id
                    or operation.status != "PREVIEW"
                ):
                    if operation and operation.goal_id == goal_id and operation.status == "APPLIED":
                        return self.get_workspace(goal_id)
                    raise HTTPException(status_code=404, detail="Plan adjustment preview not found")
                current = self._current_plan(goal_id)
                if current is None or current.id != operation.source_plan_version_id:
                    raise HTTPException(status_code=409, detail="Plan adjustment preview is stale")
                guard = self._recovery_guard(
                    self.session.get(Goal, goal_id),
                    current,
                )
                if guard.required and not operation.preview_json.get("recovery_context"):
                    raise HTTPException(
                        status_code=409,
                        detail={
                            "code": "RECOVERY_DECISION_REQUIRED",
                            "message": guard.message,
                            "missed_dates": [item.isoformat() for item in guard.missed_dates],
                        },
                    )
                if self._adjustment_schedule_fingerprint(current) != operation.preview_json.get(
                    "source_fingerprint"
                ):
                    raise HTTPException(
                        status_code=409,
                        detail="计划占用或时间设置已变化，请重新生成调整预览。",
                    )
                conversation = self._ensure_plan_conversation(goal_id)
                current_proposal = self._pending_adjustment(
                    self._conversation_messages(conversation.id)
                )
                context = self._active_recovery_context(goal_id)
                if current_proposal and context and context.get("ai_allocation_generated"):
                    current_proposal = self._local_recovery_proposal(context)
                if current_proposal is None or current_proposal.model_dump(
                    mode="json"
                ) != operation.preview_json.get("proposal"):
                    raise HTTPException(
                        status_code=409,
                        detail="调整建议已变化，请重新生成预览。",
                    )
                if self._recovery_preview_key(context) != self._recovery_preview_key(operation.preview_json.get("recovery_context")):
                    raise HTTPException(409, "任务分配或每日上限已变化，请重新生成预览。")
                if operation.preview_json.get("unallocated_count") and not accept_unallocated:
                    raise HTTPException(
                        status_code=409,
                        detail="仍有任务没有具体时段，请确认保留待安排或返回修改方案。",
                    )
                self._validate_adjustment_time_overrides(operation.preview_json.get("schedule", []), schedule_overrides)
                if operation.preview_json.get("execution_model") == "HYBRID_WORK_ITEM":
                    return self._apply_staged_hybrid_adjustment(
                        goal_id, operation, current_proposal
                    )
                if operation.preview_json.get("recovery_context"):
                    return self._apply_staged_recovery_adjustment(
                        goal_id, operation, schedule_overrides
                    )
                return self._apply_staged_adjustment(goal_id, operation, schedule_overrides)
            if self._capacity_negotiation(goal_id, self._current_plan(goal_id)):
                raise HTTPException(409, "容量协商必须先生成并确认通过校验的完整预览。")
            return self._apply_adjustment_locked(goal_id)

    def _apply_staged_hybrid_adjustment(
        self,
        goal_id: str,
        operation: PlanAdjustmentPreview,
        proposal: PlanAdjustmentProposal,
    ) -> PlanWorkspace:
        goal, _requirement, route = self._get_review_inputs(goal_id)
        current = self._require_review_plan(goal_id)
        staged = operation.preview_json["plan"]
        outline = HybridPlanOutlineResult.model_validate(
            {
                key: value
                for key, value in staged.items()
                if key in HybridPlanOutlineResult.model_fields
            }
        )
        work_items = [PlanWorkItemDraft.model_validate(item) for item in staged["work_items"]]
        groups = [
            PlanWorkItemGroupDraft.model_validate(item) for item in staged["work_item_groups"]
        ]
        dependencies = [
            WorkItemDependencyDraft.model_validate(item)
            for item in staged["work_item_dependencies"]
        ]
        days = [PlanDayDraft.model_validate(item) for item in staged.get("days", [])]
        self._validate_hybrid_result(
            work_items,
            groups,
            dependencies,
            outline.milestones,
            outline.commitments,
        )
        if proposal.capacity_segments or proposal.proposed_deadline:
            route = self._publish_capacity_contract(goal, _requirement, route, proposal, operation.id)
        route_stages = self._route_stages(route.id)
        stage_by_order = {item.order: item for item in route_stages}
        revised = PlanVersion(
            goal_id=goal.id,
            route_version_id=route.id,
            version=current.version,
            revision=current.revision + 1,
            status="DRAFT",
            summary=outline.summary,
            total_weeks=outline.total_weeks,
            weekly_execution_days=outline.weekly_execution_days,
            weekly_minutes=outline.weekly_minutes,
            total_estimated_minutes=0,
            buffer_strategy_json=outline.buffer_strategy,
            assumptions_json=outline.assumptions,
            open_questions_json=outline.open_questions,
            change_summary_json=list(proposal.proposed_changes),
            temporal_segments_json=[
                item.model_dump(mode="json") for item in outline.temporal_segments
            ],
            commitments_json=[item.model_dump(mode="json") for item in outline.commitments],
            feedback=proposal.user_intent_summary,
            prompt_version=HYBRID_PLAN_PROMPT_VERSION,
            source_document="\n\n".join(
                [
                    outline.source_document or "",
                    *[str(item) for item in staged.get("detail_documents", [])],
                ]
            ).strip(),
            compiled_snapshot_json=staged,
            compiler_report_json={
                **outline.compiler_report,
                "adjustment_preview_id": operation.id,
                "confirmed_capacity_adjustment": proposal.model_dump(mode="json"),
                "validation": staged.get("validation", {}),
            },
            planning_mode=current.planning_mode,
            schedule_type=proposal.target_schedule_type or current.schedule_type,
            execution_model_version=str(ExecutionModelVersion.HYBRID_WORK_ITEM),
            exploration_window_weeks=min(outline.total_weeks, current.exploration_window_weeks),
            next_unplanned_week=min(outline.total_weeks + 1, current.next_unplanned_week),
        )
        self.session.add(revised)
        revised.compiler_report_json = {**revised.compiler_report_json,
            "week_basis": "CALENDAR_MONDAY" if calendar_weeks(revised, from_source=True) else "ROLLING_START"}
        self.session.flush()
        for milestone in outline.milestones:
            self.session.add(
                PlanMilestone(
                    plan_version_id=revised.id,
                    route_stage_id=stage_by_order[milestone.stage_order].id,
                    stage_order=milestone.stage_order,
                    title=milestone.title,
                    purpose=milestone.purpose,
                    start_week=milestone.start_week,
                    end_week=milestone.end_week,
                    outputs_json=milestone.outputs,
                )
            )
        self.session.flush()
        self._persist_hybrid_work_items(revised, work_items, groups, dependencies)
        if days:
            revised.total_estimated_minutes = self._persist_days(
                revised,
                {item.stage_order: item for item in self._milestones(revised.id)},
                days,
            )

        previous_by_key = {
            item.item_key: item
            for item in self.session.scalars(
                select(PlanWorkItem).where(PlanWorkItem.plan_version_id == current.id)
            )
        }
        for item in self.session.scalars(
            select(PlanWorkItem).where(PlanWorkItem.plan_version_id == revised.id)
        ):
            source = previous_by_key.get(item.item_key)
            if source:
                item.source_work_item_id = source.id
                item.lineage_work_item_id = source.lineage_work_item_id or source.id

        old_batches = list(
            self.session.scalars(
                select(GenerationBatch).where(
                    GenerationBatch.plan_version_id == current.id,
                    GenerationBatch.scope_type == "HYBRID_WEEK",
                    GenerationBatch.status == "COMPLETED",
                )
            )
        )
        batch_by_week = {
            int(item.scope_json["week_index"]): item
            for item in old_batches
            if item.scope_json.get("week_index") is not None
        }
        fallback_brief = (
            old_batches[0].brief_id if old_batches else self._ensure_planning_brief(goal_id)[1].id
        )
        for week in range(1, revised.total_weeks + 1):
            source = batch_by_week.get(week)
            self.session.add(
                GenerationBatch(
                    goal_id=goal.id,
                    plan_version_id=revised.id,
                    brief_id=source.brief_id if source else fallback_brief,
                    idempotency_key=f"hybrid-adjustment:{operation.id}:week:{week}",
                    scope_type="HYBRID_WEEK",
                    scope_json={
                        "week_index": week,
                        "generation_mode": "STANDARD",
                        "source": "HYBRID_ADJUSTMENT_PREVIEW",
                    },
                    status="COMPLETED",
                    attempt_count=1,
                    staged_result_json={"preview_id": operation.id},
                )
            )
        if revised.planning_mode == "EXPLORATORY":
            self.session.add(
                PlanWindow(
                    goal_id=goal.id,
                    plan_version_id=revised.id,
                    sequence=1,
                    start_week=1,
                    end_week=revised.exploration_window_weeks,
                    status="DRAFT",
                    trigger="ADJUSTMENT",
                    basis_json={"source_plan_version_id": current.id},
                )
            )
        self._apply_schedule_type_transition(goal, route, revised, proposal.target_schedule_type)
        if goal.status == "READY_TO_START":
            # Applying an explicitly approved preview before first startup
            # replaces the confirmed revision too, even without an activation.
            revised.status = "CONFIRMED"
            revised.confirmed_at = utc_now()
            goal.active_plan_version_id = revised.id
        if goal.status in {"ACTIVE", "RECOVERY_REQUIRED", "PAUSED"}:
            self._migrate_active_execution(goal, current, revised)
            revised.status = "CONFIRMED"
            revised.confirmed_at = utc_now()
            goal.active_plan_version_id = revised.id
            mark_plan_schedule_review(
                self.session, goal, current.id, revised.id,
                "弹性计划调整后，重新核对未来任务时段。",
            )
        current.status = "SUPERSEDED"
        if goal.status == "PLAN_DRAFTING":
            goal.status = "PLAN_REVIEW"
        operation.status = "APPLIED"
        operation.applied_at = utc_now()
        operation.preview_json = {
            **operation.preview_json,
            "result_revision": revised.revision,
            "result_plan_version_id": revised.id,
        }
        conversation = self._ensure_plan_conversation(goal_id)
        self._add_assistant_message(
            conversation,
            f"调整已经发布为弹性计划 v{revised.version}.{revised.revision}。"
            "未受影响的工作项沿用稳定标识，具体日期仍会在开始前预览确认。",
            "APPLY",
            {
                "prompt_version": HYBRID_PLAN_PROMPT_VERSION,
                "adjustment_applied": True,
                "preview_id": operation.id,
                "plan_revision": revised.revision,
                "change_summary": list(proposal.proposed_changes),
            },
        )
        goal.last_active_at = utc_now()
        self.session.commit()
        return self.get_workspace(goal_id)

    def _publish_capacity_contract(self, goal, requirement, route, proposal, preview_id):
        """Version a user-confirmed contract; retain historical route/requirements."""
        snapshot = dict(requirement.snapshot_json)
        snapshot["confirmed_capacity_adjustment"] = {
            **proposal.model_dump(mode="json"), "preview_id": preview_id,
            "supersedes_time_conditions": True,
        }
        if proposal.capacity_segments:
            from planner_api.plan_capacity import capacity_segment_summary
            snapshot["available_capacity"] = "；".join(
                capacity_segment_summary(item.model_dump(mode="json")) for item in proposal.capacity_segments)
        if proposal.proposed_deadline:
            snapshot["deadline"] = proposal.proposed_deadline.isoformat()
        elif proposal.total_weeks_change:
            snapshot["deadline"] = f"用户确认的{self._current_plan(goal.id).total_weeks + proposal.total_weeks_change}周规划期限"
        version = self.session.scalar(select(func.max(RequirementVersion.version)).where(
            RequirementVersion.goal_id == goal.id)) or requirement.version
        updated = RequirementVersion(goal_id=goal.id, version=version + 1, status="CONFIRMED",
                                     snapshot_json=snapshot, confirmed_at=utc_now())
        self.session.add(updated)
        self.session.flush()
        requirement.status = "SUPERSEDED"
        new_route = RouteVersion(**{
            column.name: getattr(route, column.name) for column in RouteVersion.__table__.columns
            if column.name not in {"id", "created_at", "requirement_version_id", "revision"}
        }, requirement_version_id=updated.id, revision=route.revision + 1)
        self.session.add(new_route)
        self.session.flush()
        for stage in self._route_stages(route.id):
            self.session.add(RouteStage(**{
                column.name: getattr(stage, column.name) for column in RouteStage.__table__.columns
                if column.name not in {"id", "route_version_id"}
            }, route_version_id=new_route.id))
        route.status = "SUPERSEDED"
        goal.current_requirement_version_id = updated.id
        goal.active_route_version_id = new_route.id
        self.session.flush()
        return new_route

    def _apply_staged_recovery_adjustment(
        self,
        goal_id: str,
        operation: PlanAdjustmentPreview,
        schedule_overrides: list[dict[str, object]] | None = None,
    ) -> PlanWorkspace:
        """Publish a previously previewed AI recovery layout without another model call."""
        goal, _requirement, route = self._get_review_inputs(goal_id)
        current = self._require_review_plan(goal_id)
        conversation = self._ensure_plan_conversation(goal_id)
        try:
            proposal = PlanAdjustmentProposal.model_validate(operation.preview_json["proposal"])
            recovery_context = operation.preview_json["recovery_context"]
            merged = self._build_local_recovery_revision(current, proposal, recovery_context)
            revised = self._persist_revision(
                goal,
                route,
                self._route_stages(route.id),
                current,
                merged,
                proposal.user_intent_summary,
                "recovery-local-allocation-v1",
            )
            self._apply_recovery_original_layout(current, revised, recovery_context)
            if goal.status in {"ACTIVE", "RECOVERY_REQUIRED"}:
                self._migrate_active_execution(goal, current, revised, recovery_context)
                mark_plan_schedule_review(
                    self.session,
                    goal,
                    current.id,
                    revised.id,
                    "追赶计划改变了任务承接日期，重新核对时间安排。",
                )
            schedule_plan = self._with_schedule_plan_overrides(
                operation.preview_json.get("schedule_plan", []), schedule_overrides
            )
            self._publish_revision_schedule(goal, current, revised, schedule_plan)
            actual_schedule = self._preview_schedule(goal, revised, rebuild=False)
            self._assert_recovery_budget(recovery_context, actual_schedule)
            expected_schedule = self._with_schedule_overrides(
                operation.preview_json.get("schedule", []), schedule_overrides
            )
            if not schedule_overrides and actual_schedule != expected_schedule:
                raise HTTPException(
                    status_code=409,
                    detail="实际时间安排与预览不一致，请重新生成调整预览。",
                )
            self._assert_adjustment_time_overrides(actual_schedule, schedule_overrides)
        except HTTPException:
            self.session.rollback()
            raise
        except (ProviderUnavailableError, ValidationError, ValueError, TypeError) as error:
            self.session.rollback()
            conversation = self._ensure_plan_conversation(goal_id)
            failure = (
                error
                if isinstance(error, ProviderUnavailableError)
                else ProviderUnavailableError(
                    "追赶草案没有通过发布校验，当前计划保持不变。",
                    diagnostic=str(error),
                )
            )
            self._add_failed_message(conversation, failure, "APPLY")
            self.session.commit()
            return self.get_workspace(goal_id)
        current.status = "SUPERSEDED"
        recovery_id = self._close_recovery_drafts(goal.id, "CONFIRMED")
        self._add_assistant_message(
            conversation,
            f"追赶安排已经发布为计划 v{revised.version}.{revised.revision}，"
            "执行日历和已完成进度已经安全迁移，可以继续执行。",
            "APPLY",
            {
                "prompt_version": "recovery-local-allocation-v1",
                "adjustment_applied": True,
                "preview_id": operation.id,
                "recovery_brief_id": recovery_id,
                "recovery_strategy": "AI_STRUCTURED_OPERATIONS",
                "plan_revision": revised.revision,
            },
        )
        operation.status = "APPLIED"
        operation.applied_at = utc_now()
        operation.preview_json = {
            **operation.preview_json,
            "result_revision": revised.revision,
        }
        goal.last_active_at = utc_now()
        self.session.commit()
        return self.get_workspace(goal_id)

    def _apply_staged_adjustment(
        self,
        goal_id: str,
        operation: PlanAdjustmentPreview,
        schedule_overrides: list[dict[str, object]] | None = None,
    ) -> PlanWorkspace:
        goal, _requirement, route = self._get_review_inputs(goal_id)
        current = self._require_review_plan(goal_id)
        staged = operation.preview_json
        proposal = PlanAdjustmentProposal.model_validate(staged["proposal"])
        plan_payload = {
            **staged["plan"],
            "assistant_message": proposal.assistant_message,
            "change_summary": staged["plan"].get("change_summary") or proposal.proposed_changes,
        }
        merged = PlanRevisionResult.model_validate(plan_payload)
        revised = self._persist_revision(
            goal,
            route,
            self._route_stages(route.id),
            current,
            merged,
            proposal.user_intent_summary,
            PLAN_ADJUSTMENT_APPLY_PROMPT_VERSION,
            schedule_type=proposal.target_schedule_type,
        )
        self._apply_schedule_type_transition(goal, route, revised, proposal.target_schedule_type)
        if goal.status in {"ACTIVE", "RECOVERY_REQUIRED"}:
            self._migrate_active_execution(goal, current, revised)
            mark_plan_schedule_review(
                self.session,
                goal,
                current.id,
                revised.id,
                "计划调整后，重新核对时间安排。",
            )
        schedule_plan = self._with_schedule_plan_overrides(
            staged.get("schedule_plan", []), schedule_overrides
        )
        self._publish_revision_schedule(goal, current, revised, schedule_plan)
        actual_schedule = self._preview_schedule(goal, revised, rebuild=False)
        expected_schedule = self._with_schedule_overrides(
            staged.get("schedule", []), schedule_overrides
        )
        if not schedule_overrides and actual_schedule != expected_schedule:
            self.session.rollback()
            raise HTTPException(
                status_code=409,
                detail="实际时间安排与预览不一致，请重新生成调整预览。",
            )
        self._assert_adjustment_time_overrides(actual_schedule, schedule_overrides)
        conversation = self._ensure_plan_conversation(goal_id)
        self._add_assistant_message(
            conversation,
            f"调整已经发布为计划 v{revised.version}.{revised.revision}，"
            "执行日历和已完成进度已经安全迁移。",
            "APPLY",
            {
                "prompt_version": PLAN_ADJUSTMENT_APPLY_PROMPT_VERSION,
                "adjustment_applied": True,
                "preview_id": operation.id,
                "plan_revision": revised.revision,
            },
        )
        operation.status = "APPLIED"
        operation.applied_at = utc_now()
        operation.preview_json = {**operation.preview_json, "result_revision": revised.revision}
        goal.last_active_at = utc_now()
        self.session.commit()
        return self.get_workspace(goal_id)

    @staticmethod
    def _validate_adjustment_time_overrides(schedule, overrides):
        seen = set()
        for override in overrides or []:
            key = (str(override.get("scheduled_date")), str(override.get("title")))
            matches = [row for row in schedule if (str(row["date"]), row["title"]) == key]
            if key in seen or len(matches) != 1:
                raise HTTPException(409, "修改时段无法唯一对应预览任务，请继续讨论调整日期或重新预览。")
            seen.add(key)
            row = matches[0]
            start, end = override.get("start_local"), override.get("end_local")
            if row["state"] == "FLEXIBLE":
                if start is None and end is None:
                    continue
                raise HTTPException(409, "当前为自由安排，无需填写起止时间；如需固定时段，请先切换时间安排方式。")
            if not start or not end:
                raise HTTPException(422, "请为修改的任务填写完整起止时间；任务日期与用时不能在这里省略。")
            def minutes(value):
                hour, minute = map(int, str(value).split(":"))
                return hour * 60 + minute
            if minutes(end) - minutes(start) != row["minutes"]:
                raise HTTPException(422, "修改后的时段长度须与任务预计用时一致，不能压缩任务来通过校验。")

    def _assert_adjustment_time_overrides(self, actual, overrides):
        for override in overrides or []:
            if override.get("start_local") is None and override.get("end_local") is None:
                continue
            matches = [row for row in actual if row["date"] == str(override["scheduled_date"]) and row["title"] == override["title"]]
            if len(matches) != 1 or (matches[0]["start"], matches[0]["end"]) != (override["start_local"], override["end_local"]):
                self.session.rollback()
                raise HTTPException(409, "你修改的时段未通过可用窗口、冲突或前置关系校验，当前计划未改变。请换一个时段或继续讨论。")

    @staticmethod
    def _with_schedule_overrides(
        schedule: list[dict[str, object]],
        overrides: list[dict[str, object]] | None,
    ) -> list[dict[str, object]]:
        if not overrides:
            return schedule
        result = [dict(item) for item in schedule]
        for override in overrides:
            date_value, title = str(override.get("scheduled_date")), str(override.get("title"))
            for item in result:
                if item.get("date") == date_value and item.get("title") == title:
                    item["start"] = override.get("start_local")
                    item["end"] = override.get("end_local")
                    item["state"] = "SCHEDULED" if item["start"] else "UNALLOCATED"
        return result

    @staticmethod
    def _with_schedule_plan_overrides(
        schedule_plan: list[dict[str, object]],
        overrides: list[dict[str, object]] | None,
    ) -> list[dict[str, object]]:
        if not overrides:
            return schedule_plan
        result = [
            {
                **record,
                "assignments": [dict(item) for item in record.get("assignments", [])],
            }
            for record in schedule_plan
        ]
        for override in overrides:
            date_value = str(override.get("scheduled_date"))
            title = str(override.get("title"))
            for record in result:
                if str(record.get("scheduled_date")) != date_value:
                    continue
                assignments = record["assignments"]
                if override.get("start_local") is None:
                    record["assignments"] = [
                        item for item in assignments if item.get("title") != title
                    ]
                    continue
                for item in assignments:
                    if item.get("title") == title:
                        item["start_local"] = override.get("start_local")
                        item["end_local"] = override.get("end_local")
                        break
                else:
                    assignments.append({"title": title, "start_local": override.get("start_local"), "end_local": override.get("end_local")})
        return result

    def preview_adjustment(self, goal_id: str) -> PlanAdjustmentPreviewRead:
        with _plan_generation_locks[goal_id]:
            goal, requirement, route = self._get_review_inputs(goal_id)
            current = self._require_review_plan(goal_id)
            conversation = self._ensure_plan_conversation(goal_id)
            proposal = self._pending_adjustment(self._conversation_messages(conversation.id))
            proposal = self._scope_exploratory_adjustment(current, proposal)
            recovery_context = self._active_recovery_context(goal_id)
            if proposal and recovery_context and recovery_context.get("ai_allocation_generated"):
                proposal = self._local_recovery_proposal(recovery_context)
            if proposal is None:
                raise HTTPException(status_code=409, detail="No pending plan adjustment exists")
            if not proposal.can_apply or proposal.needs_clarification:
                raise HTTPException(
                    status_code=409,
                    detail="The pending adjustment still requires clarification",
                )
            source_fingerprint = self._adjustment_schedule_fingerprint(current)
            existing_preview = self._matching_adjustment_preview(
                goal_id,
                current.id,
                proposal,
                source_fingerprint,
            )
            if existing_preview is None:
                # Reuse an older full-workspace fingerprint only while its
                # original state still matches; never bless a stale preview.
                existing_preview = self._matching_adjustment_preview(
                    goal_id, current.id, proposal, self._adjustment_schedule_fingerprint())
                if existing_preview is not None:
                    existing_preview.preview_json = {**existing_preview.preview_json,
                        "source_fingerprint": source_fingerprint}
                    self.session.commit()
            if existing_preview is not None:
                staged = existing_preview.preview_json.get("plan", {})
                keys = [item["item_key"] for item in staged.get("work_items", [])]
                groups = [item["group_key"] for item in staged.get("work_item_groups", [])]
                if len(keys) == len(set(keys)) and len(groups) == len(set(groups)):
                    return self._adjustment_preview_read(existing_preview)
                # Older previews lacked the within-batch identity guard.
                # Keep the evidence, repair the bad week, and never publish it.
                existing_preview.status = "INVALID"
                self.session.commit()
            if current.execution_model_version == ExecutionModelVersion.HYBRID_WORK_ITEM:
                return self._preview_hybrid_adjustment(
                    goal,
                    requirement,
                    route,
                    current,
                    proposal,
                    source_fingerprint,
                )
            recovery_context = self._active_recovery_context(goal_id)
            if recovery_context and recovery_context.get("unassigned_task_ids"):
                raise HTTPException(
                    status_code=409,
                    detail="恢复范围仍有未安排任务，请先补充可用时间或缩小范围并重新讨论。",
                )
            if recovery_context and recovery_context.get("proposed_assignments"):
                try:
                    return self._preview_recovery_adjustment(
                        goal, route, current, proposal, recovery_context, source_fingerprint
                    )
                except ProviderUnavailableError as error:
                    logger.warning(
                        "recovery adjustment preview rejected goal_id=%s code=%s diagnostic=%s",
                        goal.id,
                        error.code,
                        error.diagnostic or str(error),
                    )
                    raise HTTPException(
                        status_code=502,
                        detail=(
                            "追赶方案仍有任务未能安全承接，当前计划保持不变；"
                            "请重新生成更小范围的追赶预览。"
                        ),
                    ) from error
            context_pack, _brief = self._ensure_planning_brief(goal_id)
            orchestration = self.orchestrator.prepare(
                goal.id,
                "PLAN_ADJUSTMENT",
                {
                    **context_pack.facts_json,
                    "current_plan": self._plan_payload(current),
                    "approved_adjustment": proposal.model_dump(mode="json"),
                    "generation_scope": {
                        "affected_weeks": proposal.affected_weeks,
                        "downstream_weeks": proposal.downstream_affected_weeks,
                    },
                },
                plan_version_id=current.id,
                context_pack_id=context_pack.id,
                minimum_tier="L2",
            )
            savepoint = self.session.begin_nested()
            try:
                # Reuse the existing deterministic application path up to the
                # newly-created revision. It is intentionally rolled back;
                # only the serialised plan and schedule review are retained.
                try:
                    result = self._build_adjustment_result(
                        goal,
                        requirement,
                        route,
                        current,
                        proposal,
                        orchestration=orchestration,
                    )
                except ClarificationProviderError as error:
                    logger.warning(
                        "plan adjustment preview provider failed goal_id=%s code=%s diagnostic=%s",
                        goal.id,
                        error.code,
                        error.diagnostic or str(error),
                    )
                    raise HTTPException(
                        status_code=502,
                        detail=(
                            "调整草案连续未通过系统校验，当前计划保持不变，可以稍后重新生成预览。"
                        ),
                    ) from error
                revised = self._persist_revision(
                    goal,
                    route,
                    self._route_stages(route.id),
                    current,
                    result,
                    proposal.user_intent_summary,
                    PLAN_ADJUSTMENT_APPLY_PROMPT_VERSION,
                    schedule_type=proposal.target_schedule_type,
                )
                if self._capacity_negotiation(goal_id, current):
                    self._validate_negotiated_outline(current, result, proposal)
                self._apply_schedule_type_transition(
                    goal, route, revised, proposal.target_schedule_type
                )
                if goal.status in {"ACTIVE", "RECOVERY_REQUIRED"}:
                    self._migrate_active_execution(goal, current, revised)
                    mark_plan_schedule_review(
                        self.session,
                        goal,
                        current.id,
                        revised.id,
                        "计划调整预览，重新核对时间安排。",
                    )
                    goal.active_plan_version_id = revised.id
                    goal.status = "ACTIVE"
                    self.session.flush()
                schedule = self._preview_schedule(goal, revised, source_plan=current)
                payload = {
                    # Keep the validated compiler result separate from execution
                    # migration artifacts such as carryovers and split tasks.
                    "plan": result.model_dump(mode="json"),
                    "schedule": schedule,
                    "comparison": self._adjustment_comparison(current, schedule, revised=revised),
                    "schedule_plan": getattr(self, "_last_schedule_plan", []),
                    "unallocated_count": sum(
                        item.get("state") == "UNALLOCATED" for item in schedule
                    ),
                    "source_fingerprint": source_fingerprint,
                    "proposal": proposal.model_dump(mode="json"),
                    "recovery_context": recovery_context,
                }
            finally:
                savepoint.rollback()
            encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()
            fingerprint = hashlib.sha256(encoded).hexdigest()
            existing = self.session.scalar(
                select(PlanAdjustmentPreview).where(
                    PlanAdjustmentPreview.goal_id == goal_id,
                    PlanAdjustmentPreview.source_plan_version_id == current.id,
                    PlanAdjustmentPreview.fingerprint == fingerprint,
                    PlanAdjustmentPreview.status == "PREVIEW",
                )
            )
            if existing is None:
                existing = PlanAdjustmentPreview(
                    goal_id=goal_id,
                    source_plan_version_id=current.id,
                    fingerprint=fingerprint,
                    preview_json=payload,
                )
                self.session.add(existing)
                self.session.flush()
            self.orchestrator.complete(
                orchestration["run_id"],
                {
                    "status": "PASSED",
                    "preview_id": existing.id,
                    "regenerated_weeks": sorted(
                        set(proposal.affected_weeks + proposal.downstream_affected_weeks)
                    ),
                },
            )
            self.session.commit()
            return self._adjustment_preview_read(existing)

    @staticmethod
    def _merge_locked_adjustment_days(days, locked_days, week):
        """Validate a complete week while treating recorded days as immutable."""
        protected = {(day.week_index, day.day_index): day for day in locked_days
                     if day.week_index == week}
        return sorted(
            [day for day in days if (day.week_index, day.day_index) not in protected]
            + list(protected.values()),
            key=lambda day: (day.week_index, day.day_index),
        )

    def _preview_hybrid_adjustment(
        self,
        goal: Goal,
        requirement: RequirementVersion,
        route: RouteVersion,
        current: PlanVersion,
        proposal: PlanAdjustmentProposal,
        source_fingerprint: str,
        _repair_feedback: dict | None = None,
        _repair_attempt: int = 0,
    ) -> PlanAdjustmentPreviewRead:
        """Regenerate a complete, validated work-item graph without mutating the draft."""
        context_pack, _brief = self._ensure_planning_brief(goal.id)
        current_payload = self._plan_payload(current)
        locked_coordinates = (self._locked_execution_coordinates(current)
                              if goal.status in {"ACTIVE", "RECOVERY_REQUIRED", "PAUSED"} else set())
        locked_days = [PlanDayDraft.model_validate(day) for day in current_payload["days"]
                       if (day["week_index"], day["day_index"]) in locked_coordinates]
        current_read = self._plan_read(current)
        current_payload["work_items"] = [
            item.model_dump(mode="json") for item in current_read.work_items
        ]
        current_payload["work_item_groups"] = [
            item.model_dump(mode="json") for item in current_read.work_item_groups
        ]
        orchestration = self.orchestrator.prepare(
            goal.id,
            "PLAN_ADJUSTMENT",
            {
                **context_pack.facts_json,
                "current_plan": current_payload,
                "approved_adjustment": proposal.model_dump(mode="json"),
                "execution_model": "HYBRID_WORK_ITEM",
            },
            plan_version_id=current.id,
            context_pack_id=context_pack.id,
            minimum_tier="L2",
        )
        route_stages = self._route_stages(route.id)
        context = self._plan_context(
            requirement,
            route,
            route_stages,
            planning_context={
                **context_pack.facts_json,
                "agent_orchestration": orchestration,
                "revision_request": proposal.model_dump(mode="json"),
                "generation_validation_feedback": _repair_feedback,
                "current_hybrid_plan": current_payload,
                "revision_rules": [
                    "返回调整后的完整工作项图，不要只返回差异。",
                    "尽量保持未受影响工作项的 item_key 稳定。",
                    "不得删除已确认需求、承诺、依赖或来源引用。",
                ],
            },
        )
        if proposal.target_schedule_type:
            context.route["schedule_type"] = proposal.target_schedule_type
        try:
            if proposal.proposed_deadline and requirement.snapshot_json.get("deadline"):
                conversation = self._ensure_plan_conversation(goal.id)
                evidence = json.dumps(requirement.snapshot_json, ensure_ascii=False) + " " + " ".join(
                    item.content for item in self._conversation_messages(conversation.id)
                    if item.role == "USER")
                if not re.search(r"期限(?:可以|允许|可)延长|截止(?:日期|时间)(?:可以|可)调整|(?:期限|截止日期)(?:是)?可协商", evidence):
                    raise WorkItemValidationError("PLAN_DEADLINE_CONSENT_REQUIRED", (
                        "原截止日期保持有效；请先明确该期限可以协商，不能推迟固定考试。",))
            key = hashlib.sha256(json.dumps({"proposal": proposal.model_dump(mode="json"),
                "source": source_fingerprint, "repair": _repair_attempt}, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
            preview_batch = self._begin_compiler_batch(goal.id, current.id, _brief,
                f"capacity-preview:{current.id}:{key}", "HYBRID_ADJUSTMENT_PREVIEW", {"repair_attempt": _repair_attempt})
            checkpoint = dict(preview_batch.staged_result_json or {})
            outline = (HybridPlanOutlineResult.model_validate(checkpoint["outline"]) if checkpoint.get("outline")
                       else self.provider.draft_hybrid_outline(context, "STANDARD"))
            negotiating = self._capacity_negotiation(goal.id, current)
            if negotiating:
                # Capacity and obligation identities were explicitly agreed
                # before this generation. They are authoritative inputs, not
                # fields for the model to redesign during compilation.
                self._restore_negotiated_contract(current, outline, proposal)
            self._validate_hybrid_outline_schedule(outline, proposal.target_schedule_type or current.schedule_type)
            self._validate_outline_route_coverage(outline, [item.order for item in route_stages])
            self._validate_executable_commitments(outline)
            if negotiating:
                self._validate_negotiated_outline(current, outline, proposal)
            requirements = dict(requirement.snapshot_json)
            if proposal.proposed_deadline:
                requirements["deadline"] = proposal.proposed_deadline.isoformat()
            self._validate_deadline_horizon(outline, requirements, goal.timezone)
            checkpoint["outline"] = outline.model_dump(mode="json")
            checkpoint.setdefault("details", [])
            preview_batch.staged_result_json = dict(checkpoint)
            self.session.commit()
            all_items = list(outline.standing_work_items)
            all_groups = list(outline.work_item_groups)
            all_dependencies = list(outline.work_item_dependencies)
            all_days: list[PlanDayDraft] = []
            outline_payload = outline.model_dump(mode="json")
            outline_payload["standing_work_item_registry"] = [
                item.model_dump(mode="json") for item in outline.standing_work_items
            ]
            detail_documents: list[str] = []
            local_week_failure = False
            for week in range(1, outline.total_weeks + 1):
                outline_payload["standing_work_item_registry"] = [
                    item.model_dump(mode="json") for item in all_items]
                outline_payload["existing_dependency_registry"] = [item.model_dump(mode="json") for item in all_dependencies]
                outline_payload["detail_identity_contract"] = {
                    "existing_item_keys": [item.item_key for item in all_items],
                    "existing_edges": [[item.predecessor_key, item.successor_key] for item in all_dependencies],
                    "rule": "work_items仅允许未注册的新键。注册键只在days.tasks.work_item_keys引用；原对象和约束不变。"
                            "已有依赖只引用不重声明；允许work_items=[]。多阶段动作写stage_contributions。"}
                outline_payload["stage_week_contract"] = {
                    "week": week,
                    "eligible_stage_orders": [item.stage_order for item in outline.milestones if item.start_week <= week <= item.end_week],
                    "milestones": [item.model_dump(mode="json") for item in outline.milestones],
                    "rule": "只能引用覆盖本周的阶段；不能把旧阶段延伸到本周，不得改既有阶段窗口。"}
                outline_payload["generated_task_registry"] = [
                    {"title": task.title, "reference": f"M{day.stage_order}-W{day.week_index}-D{day.day_index}-T{task.order}",
                     "work_item_keys": task.work_item_keys, "occurrence_key": task.occurrence_key,
                     "dependency_period_key": task.scheduling_policy_data.get("dependency_period_key") or f"week:{day.week_index}"}
                    for day in all_days for task in day.tasks
                ]
                promises = [item.model_dump(mode="json") for item in outline.commitments]
                identities = quantified_workloads(promises)
                outline_payload["remaining_confirmed_workload"] = [{"label": item["label"],
                    "remaining_minutes": max(0, int(item.get("minimum_total_minutes") or 0) - sum(
                        self._task_workload_minutes(task, item["label"], identities)
                        for day in all_days for task in day.tasks if item["label"] in task.satisfies_commitments)),
                    "deadline_week": item["end_week"]}
                    for item in promises if item.get("minimum_total_minutes")]
                future_plan = SimpleNamespace(temporal_segments_json=[item.model_dump(mode="json") for item in outline.temporal_segments],
                    weekly_execution_days=outline.weekly_execution_days, total_weeks=outline.total_weeks)
                outline_payload["workload_capacity_lookahead"] = self._workload_capacity_lookahead(
                    future_plan, promises, all_days, week)
                detail_context = context
                if checkpoint.get("failed_week") == week:
                    detail_context = self._business_retry_context(context, "ADJUSTMENT_WEEK", 0,
                        HTTPException(503, checkpoint["week_failure"]), None)
                    detail_context = self._detail_identity_retry_context(detail_context, outline_payload)
                for repair in range(3):
                    details = (HybridPlanDetailResult.model_validate(checkpoint["details"][week - 1])
                               if week <= len(checkpoint["details"]) and checkpoint["details"][week - 1]
                               else self.provider.draft_hybrid_details(
                                   detail_context, outline_payload, week, week, "STANDARD"))
                    try:
                        # Providers may return only the editable remainder of a
                        # week. Restore recorded coordinates before *all* range,
                        # capacity, workload and dependency checks, not afterwards.
                        details.days = self._merge_locked_adjustment_days(details.days, locked_days, week)
                        details = repair_bookkeeping(details, outline_payload,
                            enabled=get_settings().plan_compiler_bookkeeping_repair_enabled)
                        self._reuse_registered_detail_items(details, all_items)
                        self._validate_hybrid_result(
                            [*all_items, *details.work_items], [*all_groups, *details.work_item_groups],
                            list({json.dumps(item.model_dump(mode="json"), sort_keys=True): item
                                  for item in [*all_dependencies, *details.work_item_dependencies]}.values()),
                            outline.milestones, outline.commitments)
                        self._bind_hybrid_actions(SimpleNamespace(id="unpersisted-preview", commitments_json=promises),
                            SimpleNamespace(work_items=[*all_items, *details.work_items],
                                work_item_groups=details.work_item_groups, days=details.days))
                        validate_action_coverage([*all_items, *details.work_items], all_days + details.days,
                            through_week=week, milestone_weeks={None: (1, outline.total_weeks),
                                **{row.stage_order: (row.start_week, row.end_week) for row in outline.milestones}})
                        self._validate_minimum_commitment_minutes(
                            [item for item in promises if int(item["end_week"]) <= week],
                            all_days + details.days,
                        )
                        self._validate_remaining_workload_feasibility(
                            SimpleNamespace(temporal_segments_json=[item.model_dump(mode="json") for item in outline.temporal_segments],
                                weekly_execution_days=outline.weekly_execution_days, total_weeks=outline.total_weeks),
                            promises, all_days + details.days, week)
                        self._validate_detail_result(
                            SimpleNamespace(weekly_execution_days=outline.weekly_execution_days,
                                schedule_type=proposal.target_schedule_type or current.schedule_type,
                                temporal_segments_json=[item.model_dump(mode="json") for item in outline.temporal_segments],
                                commitments_json=promises),
                            {item.stage_order: item for item in outline.milestones}, [week], details.days,
                            covered_work_item_stages=self._referenced_action_stages(
                                details.days, [*all_items, *details.work_items]),
                        )
                        self._validate_week_dependency_targets(PlanDetailResult(days=details.days), outline_payload)
                        from planner_api.work_item_validation import validate_authored_dependency_order
                        validate_authored_dependency_order(all_days + details.days,
                            [dependency.model_dump(mode="json") for dependency in
                             [*all_dependencies, *details.work_item_dependencies]])
                        self._mark_compiler_bookkeeping_validated(details)
                        break
                    except (WorkItemValidationError, HTTPException) as error:
                        # Later drafts used the rejected prefix's remaining
                        # workload/identity ledger, so they must be invalidated.
                        checkpoint["details"] = checkpoint["details"][:week - 1]
                        checkpoint["failed_week"] = week
                        checkpoint["week_failure"] = {
                            "code": getattr(error, "code", "HYBRID_ADJUSTMENT_WEEK_INVALID"),
                            "diagnostic": str(error.detail if isinstance(error, HTTPException) else error)}
                        checkpoint["failed_candidates"] = [*(checkpoint.get("failed_candidates") or [])[-5:],
                            {"week": week, "attempt": repair + 1, "failure": checkpoint["week_failure"],
                             "candidate": details.model_dump(mode="json")}]
                        preview_batch.staged_result_json = dict(checkpoint)
                        self.session.commit()
                        if repair == 2:
                            local_week_failure = True
                            raise
                        detail_context = self._business_retry_context(context, "ADJUSTMENT_WEEK", repair + 1,
                                                                     error, details.source_document)
                        detail_context = self._detail_identity_retry_context(detail_context, outline_payload)
                        if getattr(error, "code", "") == "HYBRID_REGISTERED_ITEM_CHANGED":
                            registry = {item.item_key: item.model_dump(mode="json") for item in all_items}
                            duplicates = [item.item_key for item in details.work_items if item.item_key in registry]
                            correction = (
                                "本次编译必须输出work_items=[]（已有行动用原键引用），以下重复对象不是允许新增或修改的对象：" +
                                json.dumps({key: registry[key] for key in duplicates}, ensure_ascii=False) +
                                "。不要返回这些对象，即使作者稿写了不同标题、scope或估时；把本周实际工作写days.tasks，"
                                "任务时长独立于工作项每次默认估时，不能改旧项以表达本周投入。")
                            detail_context = PlanContext(requirements=detail_context.requirements, route=detail_context.route,
                                source_document=details.source_document,
                                planning_context={**(detail_context.planning_context or {}), "compiler_identity_correction": correction})
                all_items.extend(details.work_items)
                all_groups.extend(details.work_item_groups)
                all_dependencies.extend(details.work_item_dependencies)
                # Identical edges repeated across week responses express one
                # constraint. Different lag/condition declarations still fail.
                all_dependencies = list({json.dumps(item.model_dump(mode="json"), sort_keys=True): item
                                         for item in all_dependencies}.values())
                all_days.extend(details.days)
                checkpoint.pop("failed_week", None)
                checkpoint.pop("week_failure", None)
                if week > len(checkpoint["details"]):
                    checkpoint["details"].append(details.model_dump(mode="json"))
                else:
                    checkpoint["details"][week - 1] = details.model_dump(mode="json")
                preview_batch.staged_result_json = dict(checkpoint)
                self.session.commit()
                if details.source_document:
                    detail_documents.append(details.source_document)
            if goal.status in {"ACTIVE", "RECOVERY_REQUIRED", "PAUSED"}:
                self._bind_hybrid_actions(SimpleNamespace(id="unpersisted-preview",
                    commitments_json=[item.model_dump(mode="json") for item in outline.commitments]),
                    SimpleNamespace(work_items=all_items, work_item_groups=all_groups, days=all_days))
                self._validate_execution_bounds(current, outline.total_weeks, outline.weekly_execution_days)
                protected = self._locked_execution_coordinates(current)
                all_days = [day for day in all_days
                            if (day.week_index, day.day_index) not in protected]
                all_days.extend(PlanDayDraft.model_validate(day) for day in current_payload["days"]
                                if (day["week_index"], day["day_index"]) in protected)
                all_days.sort(key=lambda day: (day.week_index, day.day_index))
            validate_action_coverage(all_items, all_days,
                through_week=max((day.week_index for day in all_days), default=0),
                milestone_weeks={None: (1, outline.total_weeks),
                    **{row.stage_order: (row.start_week, row.end_week) for row in outline.milestones}})
            validation = self._validate_hybrid_result(
                all_items,
                all_groups,
                all_dependencies,
                outline.milestones,
                outline.commitments,
                days=all_days,
            )
            if goal.status not in {"ACTIVE", "RECOVERY_REQUIRED", "PAUSED"}:
                self._bind_hybrid_actions(SimpleNamespace(id="unpersisted-preview",
                    commitments_json=[item.model_dump(mode="json") for item in outline.commitments]),
                    SimpleNamespace(work_items=all_items, work_item_groups=all_groups, days=all_days))
            if negotiating:
                self._validate_minimum_commitment_minutes(
                    [item.model_dump(mode="json") for item in outline.commitments], all_days)
            if not all_days:
                raise WorkItemValidationError(
                    "HYBRID_DAILY_PLAN_MISSING",
                    ["调整后的 Hybrid 计划没有生成每日执行内容"],
                )
        except (ClarificationProviderError, WorkItemValidationError, HTTPException) as error:
            if "preview_batch" in locals():
                self._fail_compiler_batch(preview_batch, getattr(error, "code", "HYBRID_ADJUSTMENT_INVALID"), str(error))
                if isinstance(error, (WorkItemValidationError, HTTPException)) and not locals().get("local_week_failure"):
                    # Network interruption resumes validated weeks. A business
                    # rejection requires new content, not replay of that draft.
                    preview_batch.staged_result_json = {}
            self.orchestrator.fail(orchestration["run_id"], error)
            self.session.commit()
            if (isinstance(error, (WorkItemValidationError, HTTPException))
                    and not locals().get("local_week_failure") and _repair_attempt < 2):
                return self._preview_hybrid_adjustment(goal, requirement, route, current, proposal,
                    source_fingerprint, {"code": getattr(error, "code", "HYBRID_ADJUSTMENT_INVALID"),
                        "diagnostic": str(error),
                        "repair_policy": "保留已选容量、全部义务和最低工作量，重新生成可行的完整预览；不能放宽时间。"},
                    _repair_attempt + 1)
            raise HTTPException(
                502,
                {
                    "code": getattr(error, "code", "HYBRID_ADJUSTMENT_INVALID"),
                    "message": "这个方案还没有通过完整校验，原安排保持不变。请继续讨论调整边界，或按原容量重排；不会删任务或强行通过。",
                    "diagnostic": (error.detail if isinstance(error, HTTPException) else getattr(error, "diagnostic", None)) or str(error),
                },
            ) from error

        plan_payload = {
            **outline.model_dump(mode="json"),
            "work_items": [item.model_dump(mode="json") for item in all_items],
            "work_item_groups": [item.model_dump(mode="json") for item in all_groups],
            "work_item_dependencies": [item.model_dump(mode="json") for item in all_dependencies],
            "days": [item.model_dump(mode="json") for item in all_days],
            "change_summary": list(proposal.proposed_changes),
            "detail_documents": detail_documents,
            "validation": validation,
        }
        payload = {
            "plan": plan_payload,
            "schedule": [],
            "unallocated_count": 0,
            "source_fingerprint": source_fingerprint,
            "proposal": proposal.model_dump(mode="json"),
            "execution_model": "HYBRID_WORK_ITEM",
            "recovery_context": self._active_recovery_context(goal.id),
        }
        encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()
        preview = PlanAdjustmentPreview(
            goal_id=goal.id,
            source_plan_version_id=current.id,
            fingerprint=hashlib.sha256(encoded).hexdigest(),
            preview_json=payload,
        )
        self.session.add(preview)
        preview_batch.status = "COMPLETED"
        self.session.flush()
        self.orchestrator.complete(
            orchestration["run_id"],
            {
                "status": "PASSED",
                "preview_id": preview.id,
                "work_item_count": len(all_items),
                "dependency_count": len(all_dependencies),
            },
        )
        self.session.commit()
        return self._adjustment_preview_read(preview)

    @staticmethod
    def _normalize_outcome_adjustment(proposal, schedule_type):
        if (proposal.target_schedule_type or schedule_type) != "OUTCOME" or not proposal.capacity_segments:
            return
        # Outcome check points and available work days are different axes.
        # Preserve supply while canonicalizing the check point count.
        for segment in proposal.capacity_segments:
            if segment.weekly_execution_days and segment.weekly_execution_days != 1:
                segment.available_days_per_week = segment.available_days_per_week or segment.weekly_execution_days
                segment.weekly_execution_days = 1
            for override in segment.weekly_capacity_overrides:
                if override.weekly_execution_days != 1:
                    override.available_days_per_week = override.available_days_per_week or override.weekly_execution_days
                    override.weekly_execution_days = 1

    @staticmethod
    def _restore_negotiated_contract(current, outline, proposal) -> None:
        from planner_api.schemas import PlanCommitmentDraft, PlanTemporalSegmentDraft
        expected = (proposal.capacity_segments if proposal.capacity_segments else
                    [PlanTemporalSegmentDraft.model_validate(item) for item in current.temporal_segments_json])
        outline.temporal_segments = [item.model_copy(deep=True) for item in expected]
        original = [PlanCommitmentDraft.model_validate(item) for item in current.commitments_json]
        labels_by_key = {}
        for item in original:
            if item.workload_key:
                labels_by_key.setdefault(item.workload_key, []).append(item.label)
        aliases = {}
        for compiled in outline.commitments:
            candidates = labels_by_key.get(compiled.workload_key, [])
            if len(candidates) == 1:
                aliases[compiled.label] = candidates[0]
        for item in outline.standing_work_items:
            item.commitment_refs = [aliases.get(label, label) for label in item.commitment_refs]
        for item in original:
            if proposal.total_weeks_change and item.end_week == current.total_weeks:
                old_end = item.end_week
                item.end_week = outline.total_weeks
                for work in outline.standing_work_items:
                    if item.label in work.commitment_refs and work.constraint_set.temporal.end_week == old_end:
                        work.constraint_set.temporal.end_week = item.end_week
            elif (proposal.total_weeks_change > 0 and item.cadence == "TOTAL"
                  and item.minimum_total_minutes and item.source_text
                  and not re.search(r"周|星期|截止|期限|\d{4}[-年]|之前|以后", item.source_text)):
                # The source fixes work quantity, not this intermediate week.
                # An authorized whole-plan extension may move derived windows;
                # a user-specified intermediate deadline remains unchanged.
                old_end = item.end_week
                item.start_week = 1
                item.end_week = min(outline.total_weeks, old_end + proposal.total_weeks_change)
                for work in outline.standing_work_items:
                    if item.label in work.commitment_refs:
                        work.constraint_set.temporal.start_week = 1
                        if work.constraint_set.temporal.end_week == old_end:
                            work.constraint_set.temporal.end_week = item.end_week
        outline.commitments = original

    @staticmethod
    def _validate_negotiated_outline(current, outline, proposal) -> None:
        expected_weeks = current.total_weeks + proposal.total_weeks_change
        issues = []
        if outline.total_weeks != expected_weeks:
            issues.append(f"周期必须为已讨论的 {expected_weeks} 周，不能自行改变。")
        expected = ([item.model_dump(mode="json") for item in proposal.capacity_segments]
                    if proposal.capacity_segments else current.temporal_segments_json)
        actual = [item.model_dump(mode="json") for item in outline.temporal_segments]
        numeric_fields = ("weekly_execution_days", "available_days_per_week", "suggested_daily_minutes",
                          "exception_days_per_week", "exception_daily_minutes", "exception_weekdays", "weekly_capacity_overrides")
        def capacity_value(item, key):
            if key == "exception_weekdays":
                return item.get(key) or []
            if key == "weekly_capacity_overrides":
                return [{**value, "exception_weekdays": value.get("exception_weekdays") or []}
                        for value in item.get(key) or []]
            return item.get(key)
        for week in range(1, expected_weeks + 1):
            old = next((item for item in expected if item["start_week"] <= week <= item["end_week"]), None)
            new = next((item for item in actual if item["start_week"] <= week <= item["end_week"]), None)
            if old is None or new is None or any(capacity_value(old, key) != capacity_value(new, key) for key in numeric_fields):
                issues.append(f"W{week} 的容量必须与本次讨论的逐阶段数值一致；任务超载不得抬高可用时间。")
        promises = {item.label: item for item in outline.commitments}
        for old in current.commitments_json or []:
            new = promises.get(old["label"])
            if new is None or new.cadence != old["cadence"] or new.minimum_occurrences < old["minimum_occurrences"]:
                issues.append(f"确认义务 {old['label']} 的身份、频率和数量必须保留。")
            elif (old.get("minimum_minutes_per_occurrence") or 0) > (new.minimum_minutes_per_occurrence or 0):
                issues.append(f"确认义务 {old['label']} 的单次工作量不得为了通过而缩小。")
            elif ((old.get("minimum_total_minutes") or 0) > (new.minimum_total_minutes or 0)
                  or old.get("workload_key") != new.workload_key):
                issues.append(f"确认义务 {old['label']} 的累计工作量与工作身份必须保留。")
        if issues:
            raise WorkItemValidationError("PLAN_NEGOTIATED_CAPACITY_INVALID", tuple(issues))

    @staticmethod
    def _task_workload_minutes(task, label, identities):
        distinct = {identities[ref] for ref in task.satisfies_commitments if ref in identities}
        allocation = task.scheduling_policy_data.get("workload_minutes")
        if len(distinct) <= 1 and allocation is None:
            return task.estimate_minutes
        if (not isinstance(allocation, dict) or set(allocation) != distinct
                or any(type(value) is not int or value <= 0 for value in allocation.values())
                or sum(allocation.values()) > task.estimate_minutes):
            raise WorkItemValidationError("PLAN_COMMITMENT_MINUTES_INVALID", (
                f"{task.title}: 不同独立成果不能重复计入同一时长；拆成独立行动，或用workload_minutes按workload_key分配分钟（合计不超过任务估时）。",))
        return allocation.get(identities.get(label), task.estimate_minutes)

    @staticmethod
    def _validate_minimum_commitment_minutes(commitments, days) -> None:
        issues = []
        identities = quantified_workloads(commitments)
        for promise in commitments:
            minimum = int(promise.get("minimum_minutes_per_occurrence") or 0) * int(promise.get("minimum_occurrences", 0))
            if promise["cadence"] == "TOTAL":
                minimum = max(minimum, int(promise.get("minimum_total_minutes") or 0))
            if not minimum:
                continue
            scoped = [day for day in days if int(promise["start_week"]) <= day.week_index <= int(promise["end_week"])]
            groups = ([scoped] if promise["cadence"] == "TOTAL" else
                      [[day for day in scoped if day.week_index == week]
                       for week in range(int(promise["start_week"]), int(promise["end_week"]) + 1)]
                      if promise["cadence"] == "PER_WEEK" else [[day] for day in scoped])
            for group in groups:
                assigned = sum(PlanService._task_workload_minutes(task, promise["label"], identities)
                               for day in group for task in day.tasks
                               if promise["label"] in task.satisfies_commitments)
                if assigned < minimum:
                    issues.append(f"{promise['label']}: 已确认至少 {minimum} 分钟，草稿实际仅 {assigned} 分钟；不能削减工作量过关。")
        if issues:
            raise WorkItemValidationError("PLAN_COMMITMENT_MINUTES_INVALID", tuple(issues))

    def _preview_recovery_adjustment(
        self, goal, route, current, proposal, recovery_context, source_fingerprint
    ) -> PlanAdjustmentPreviewRead:
        savepoint = self.session.begin_nested()
        try:
            merged = self._build_local_recovery_revision(current, proposal, recovery_context)
            revised = self._persist_revision(
                goal,
                route,
                self._route_stages(route.id),
                current,
                merged,
                proposal.user_intent_summary,
                "recovery-local-allocation-v1",
            )
            self._apply_recovery_original_layout(current, revised, recovery_context)
            if goal.status in {"ACTIVE", "RECOVERY_REQUIRED"}:
                self._migrate_active_execution(goal, current, revised, recovery_context)
                mark_plan_schedule_review(
                    self.session,
                    goal,
                    current.id,
                    revised.id,
                    "追赶计划预览，重新核对时间安排。",
                )
                self._publish_revision_schedule(goal, current, revised)
                self.session.flush()
            schedule = self._preview_schedule(goal, revised, rebuild=False)
            self._assert_recovery_budget(recovery_context, schedule)
            payload = {
                "plan": self._plan_payload(revised),
                "schedule": schedule,
                "comparison": self._adjustment_comparison(current, schedule, recovery_context, revised),
                "schedule_plan": getattr(self, "_last_schedule_plan", []),
                "unallocated_count": sum(item.get("state") == "UNALLOCATED" for item in schedule),
                "source_fingerprint": source_fingerprint,
                "proposal": proposal.model_dump(mode="json"),
                "recovery_context": recovery_context,
            }
        finally:
            savepoint.rollback()
        encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()
        fingerprint = hashlib.sha256(encoded).hexdigest()
        existing = self.session.scalar(
            select(PlanAdjustmentPreview).where(
                PlanAdjustmentPreview.goal_id == goal.id,
                PlanAdjustmentPreview.source_plan_version_id == current.id,
                PlanAdjustmentPreview.fingerprint == fingerprint,
                PlanAdjustmentPreview.status == "PREVIEW",
            )
        )
        if existing is None:
            existing = PlanAdjustmentPreview(
                goal_id=goal.id,
                source_plan_version_id=current.id,
                fingerprint=fingerprint,
                preview_json=payload,
            )
            self.session.add(existing)
            self.session.commit()
        return self._adjustment_preview_read(existing)

    def _build_local_recovery_revision(self, current, proposal, recovery_context):
        self._assert_recovery_budget(recovery_context)
        if recovery_context.get("requires_direct_extension") and not (
            recovery_context.get("direct_extension_confirmed")
            and recovery_context.get("slight_extension_possible")
        ):
            raise ProviderUnavailableError("顺延范围尚未确认，当前方案没有应用。")
        payload = self._plan_payload(current)
        # A few pre-0056 plans recorded progress on a day beyond the then
        # configured weekly cadence. Recovery must preserve that historical
        # coordinate so the revision validator does not interpret the repair as
        # deleting execution history.
        protected = self._locked_execution_coordinates(current)
        protected_days = max((day_index for _, day_index in protected), default=0)
        protected_weeks = max((week_index for week_index, _ in protected), default=0)
        payload["weekly_execution_days"] = max(
            int(payload.get("weekly_execution_days") or 0), protected_days
        )
        payload["total_weeks"] = max(int(payload.get("total_weeks") or 0), protected_weeks)
        effective_execution_days = int(payload["weekly_execution_days"])
        payload["days"] = [
            day for day in payload["days"] if int(day["day_index"]) <= effective_execution_days
        ]
        for day in payload["days"]:
            for order, task in enumerate(day.get("tasks", []), start=1):
                task["order"] = order
            day["suggested_minutes"] = max(
                int(day["suggested_minutes"]),
                sum(int(task["estimate_minutes"]) for task in day.get("tasks", [])),
            )
        assignments = [
            item
            for item in recovery_context.get("proposed_assignments", [])
            if isinstance(item, dict)
        ]
        shifted_count = int(recovery_context.get("shifted_original_task_count", 0) or 0)
        shifted_minutes = int(recovery_context.get("shifted_original_minutes", 0) or 0)
        change_summary = [f"将 {len(assignments)} 个积压任务承接到弹性近期窗口"]
        if shifted_count:
            change_summary.append(
                f"将 {shifted_count} 个原任务（约 {shifted_minutes} 分钟）按原顺序向后推移"
            )
        else:
            change_summary.append("近期原任务的日期保持不变")
        if recovery_context.get("direct_extension_confirmed"):
            extension_days = int(recovery_context.get("direct_extension_execution_days", 0) or 0)
            change_summary.append(f"增加 {extension_days} 个顺延执行日")
        payload.update(
            {
                "assistant_message": proposal.assistant_message,
                "change_summary": change_summary,
            }
        )
        return PlanRevisionResult.model_validate(payload)

    def _build_adjustment_result(
        self,
        goal,
        requirement,
        route,
        current,
        proposal,
        orchestration: dict[str, object] | None = None,
    ):
        """Generate and validate the complete adjustment without persistence."""
        directly_affected = set(proposal.affected_weeks + proposal.downstream_affected_weeks)
        if proposal.scope == "OVERALL" or not directly_affected:
            directly_affected = set(range(1, current.total_weeks + 1))
        locked = self._locked_execution_coordinates(current)
        expected_total_weeks = current.total_weeks + proposal.total_weeks_change
        effective_weekly_execution_days = max(
            current.weekly_execution_days,
            max((day_index for _, day_index in locked), default=0),
        )
        self._validate_execution_bounds(
            current, expected_total_weeks, effective_weekly_execution_days
        )
        activation = self.session.scalar(
            select(PlanActivation).where(PlanActivation.plan_version_id == current.id)
        )
        dates = (
            {
                row.plan_day_id: row.scheduled_date
                for row in self.session.scalars(
                    select(PlanDaySchedule).where(PlanDaySchedule.activation_id == activation.id)
                )
            }
            if activation
            else {}
        )
        today = self._today_for_goal_id(goal.id)
        days = self.session.scalars(select(PlanDay).where(PlanDay.plan_version_id == current.id))
        modifiable = {
            day.week_index
            for day in days
            if (day.week_index, day.day_index) not in locked and dates.get(day.id, today) >= today
        }
        required = sorted(
            {
                week
                for week in directly_affected
                if week in modifiable and week <= expected_total_weeks
            }
            | set(range(current.total_weeks + 1, expected_total_weeks + 1))
        )
        if not required:
            raise HTTPException(
                status_code=409,
                detail="调整范围内没有尚未开始且日期不早于今天的任务。原计划和建议已保留，请先恢复逾期任务，或重新选择后续任务。",
            )
        # A proposal can outlive the weeks it originally targeted. Do not ask
        # the remote compiler to rewrite a mixed historical/future batch; a
        # local preview is enough to show the remaining dates immediately.
        deterministic_preview = bool(set(directly_affected) - set(required))
        first_weeks = required[:1]
        first_result = self._generate_adjustment_preview_batch(
            requirement,
            route,
            current,
            proposal,
            first_weeks,
            expected_total_weeks,
            locked,
            deterministic_preview=deterministic_preview,
            orchestration=orchestration,
        )
        application_outline = self._adjustment_outline(first_result)
        batch_results = {first_weeks[0]: first_result}
        for week in required[1:]:
            batch_results[week] = self._generate_adjustment_preview_batch(
                requirement,
                route,
                current,
                proposal,
                [week],
                expected_total_weeks,
                locked,
                application_outline=application_outline,
                deterministic_preview=deterministic_preview,
                orchestration=orchestration,
            )
        result = PlanAdjustmentApplyResult.model_validate(
            {
                **first_result.model_dump(mode="json"),
                "assistant_message": (
                    f"调整预览已经整理好，本次重排 {len(required)} 个已展开周。"
                    "其他内容和已经完成的记录保持不变。"
                ),
                "regenerated_weeks": required,
                "replacement_days": [
                    day.model_dump(mode="json")
                    for week in required
                    for day in batch_results[week].replacement_days
                ],
            }
        )
        merged = self._merge_adjustment_result(
            current,
            result,
        )
        return merged

    def _generate_adjustment_preview_batch(
        self,
        requirement,
        route,
        current,
        proposal,
        requested_weeks: list[int],
        expected_total_weeks: int,
        locked: set[tuple[int, int]],
        *,
        application_outline: dict[str, object] | None = None,
        deterministic_preview: bool = False,
        orchestration: dict[str, object] | None = None,
    ) -> PlanAdjustmentApplyResult:
        request_text = "预览已经审核的调整草案。"
        for attempt in range(ADJUSTMENT_VALIDATION_RETRIES + 1):
            context = self._discussion_context(
                    requirement,
                    route,
                    current,
                    request_text,
                    proposal,
                    application_requested_weeks=requested_weeks,
                    application_outline=application_outline,
                    orchestration=orchestration,
                )
            try:
                result = (
                    self._build_deterministic_adjustment_preview(context)
                    if deterministic_preview
                    else self.discussion_provider.apply(context)
                )
                self._validate_adjustment_batch(
                    result,
                    requested_weeks,
                    expected_total_weeks,
                )
                try:
                    self._validate_future_replacement_days(current, result)
                except ProviderUnavailableError as error:
                    if error.code != "PAST_RECOVERY_DAYS" or attempt < ADJUSTMENT_VALIDATION_RETRIES:
                        raise
                    # The final attempt may still contain stale days from a
                    # proposal created before the current date. Keep the
                    # future part rather than failing the whole preview.
                    result = self._remove_past_replacement_days(current, result)
                self._validate_locked_days_omitted(current, result)
                self._validate_execution_bounds(
                    current, result.total_weeks, result.weekly_execution_days
                )
                return result
            except (ProviderUnavailableError, ValidationError, ValueError, TypeError) as error:
                if attempt >= ADJUSTMENT_VALIDATION_RETRIES:
                    if isinstance(error, (ValidationError, ValueError, TypeError)):
                        fallback = self._build_deterministic_adjustment_preview(context)
                        self._validate_adjustment_batch(
                            fallback,
                            requested_weeks,
                            expected_total_weeks,
                        )
                        self._validate_locked_days_omitted(current, fallback)
                        self._validate_execution_bounds(
                            current, fallback.total_weeks, fallback.weekly_execution_days
                        )
                        return fallback
                    raise
                request_text = (
                    "上一版调整没有通过系统校验，请只重做当前这一批。"
                    f"校验诊断：{getattr(error, 'diagnostic', None) or str(error)}。"
                    f"本批只生成这些周：{requested_weeks}；"
                    f"必须省略这些已锁定坐标：{sorted(locked)}。"
                    "不要改动已完成、已开始或已过去的执行日。"
                )
        raise AssertionError("adjustment preview retry loop exited unexpectedly")

    @staticmethod
    def _build_deterministic_adjustment_preview(context) -> PlanAdjustmentApplyResult:
        """Build a lossless local preview for stale or legacy plan data."""
        current = context.current_plan
        requested_weeks = set(context.application_requested_weeks or [])
        locked = {
            (item["week_index"], item["day_index"])
            for item in current.get("execution_state", [])
            if item.get("locked") is True
        }
        replacement_days = []
        for day in current["days"]:
            coordinate = (day["week_index"], day["day_index"])
            if day["week_index"] not in requested_weeks or coordinate in locked:
                continue
            revised_day = dict(day)
            revised_day["primary_goal"] = f"{day['primary_goal']}（已按反馈调整）"
            revised_day["tasks"] = [
                {**task, "order": index}
                for index, task in enumerate(day.get("tasks", []), start=1)
            ]
            replacement_days.append(revised_day)
        regenerated_weeks = sorted(requested_weeks)
        return PlanAdjustmentApplyResult.model_validate(
            {
                "assistant_message": "调整草案已经应用为新的计划修订版本，未受影响的周保持原样。",
                "summary": current["summary"],
                "total_weeks": current["total_weeks"],
                "weekly_execution_days": current["weekly_execution_days"],
                "weekly_minutes": current["weekly_minutes"],
                "milestones": current["milestones"],
                "temporal_segments": current.get("temporal_segments"),
                "commitments": current.get("commitments"),
                "regenerated_weeks": regenerated_weeks,
                "replacement_days": replacement_days,
                "buffer_strategy": current["buffer_strategy"],
                "assumptions": current["assumptions"],
                "open_questions": current["open_questions"],
                "change_summary": [
                    f"已重生成第 {week} 周，其他周保持原样。"
                    for week in regenerated_weeks
                ],
            }
        )

    def _remove_past_replacement_days(
        self, current: PlanVersion, result: PlanAdjustmentApplyResult
    ) -> PlanAdjustmentApplyResult:
        """Keep stale proposals from trying to rewrite dates that already passed.

        A discussion proposal can remain pending for weeks. The model still sees
        the original week and may return every day in it, including dates that
        are now historical. Historical execution is immutable; silently omit
        only those returned days and keep the future portion of the batch.
        """
        today = self._today_for_goal_id(current.goal_id)
        activation = self.session.scalar(
            select(PlanActivation).where(PlanActivation.plan_version_id == current.id)
        )
        if activation is None:
            return result
        day_ids = {
            (day.week_index, day.day_index): day.id
            for day in self.session.scalars(
                select(PlanDay).where(PlanDay.plan_version_id == current.id)
            )
        }
        dates = {
            row.plan_day_id: row.scheduled_date
            for row in self.session.scalars(
                select(PlanDaySchedule).where(PlanDaySchedule.activation_id == activation.id)
            )
        }
        kept = []
        dropped = []
        for day in result.replacement_days:
            scheduled = dates.get(day_ids.get((day.week_index, day.day_index)))
            if scheduled is not None and scheduled < today:
                dropped.append((day.week_index, day.day_index))
                continue
            kept.append(day)
        if dropped:
            logger.info(
                "plan adjustment preview omitted historical days goal_id=%s today=%s coordinates=%s",
                current.goal_id,
                today,
                dropped,
            )
            return result.model_copy(update={"replacement_days": kept})
        return result

    def _preview_schedule(self, goal, plan, *, source_plan=None, rebuild=True):
        if goal.status not in {"ACTIVE", "RECOVERY_REQUIRED"}:
            return []
        if rebuild:
            self._last_schedule_plan = allocate_plan_schedule(
                self.session,
                goal,
                plan.id,
                affected_dates=(
                    changed_schedule_dates(self.session, source_plan.id, plan.id)
                    if source_plan is not None
                    else None
                ),
            )
        activation = self.session.scalar(
            select(PlanActivation).where(PlanActivation.plan_version_id == plan.id)
        )
        dates = {
            row.plan_day_id: row.scheduled_date
            for row in self.session.scalars(
                select(PlanDaySchedule).where(PlanDaySchedule.activation_id == activation.id)
            )
        }
        blocks = {
            row.task_id: row
            for row in self.session.scalars(
                select(TaskTimeBlock).where(TaskTimeBlock.plan_version_id == plan.id)
            )
        }
        preference = self.session.scalar(
            select(PlanSchedulePreference).where(PlanSchedulePreference.goal_id == goal.id)
        )
        mode = preference.mode if preference else "FLEXIBLE"
        result = []
        for task in self.session.scalars(
            select(PlanTask).where(
                PlanTask.plan_version_id == plan.id,
                PlanTask.status.in_(("NOT_STARTED", "IN_PROGRESS")),
            )
        ):
            block = blocks.get(task.id)
            result.append(
                {
                    "title": task.title,
                    "date": dates[task.plan_day_id].isoformat(),
                    "start": block.start_local if block else None,
                    "end": block.end_local if block else None,
                    "minutes": task.estimate_minutes,
                    "state": (
                        "SCHEDULED"
                        if block
                        else "FLEXIBLE"
                        if mode == "FLEXIBLE"
                        else "UNALLOCATED"
                    ),
                }
            )
        return sorted(
            result,
            key=lambda item: (
                item["date"],
                item["start"] or "99:99",
                item["title"],
            ),
        )

    def _adjustment_schedule_fingerprint(self, current: PlanVersion | None = None):
        from planner_api.models import FixedActivityMovePreview, ManualTaskDateOverride
        rows = []
        models = (
            PlanSchedulePreference,
            AvailabilityWindow,
            AvailabilityException,
            WorkspaceAvailabilityWindow,
            WorkspaceScheduleProfile,
            PlanDaySchedule,
            PlanTask,
            TaskTimeBlock,
            ManualTask,
            ManualTaskDateOverride,
            PlanVersion,
            RequirementVersion,
        )
        relative_only = (current is not None
                         and current.execution_model_version == ExecutionModelVersion.HYBRID_WORK_ITEM
                         and self.session.scalar(select(PlanActivation.id).where(
                             PlanActivation.plan_version_id == current.id).limit(1)) is None)
        if relative_only:
            # Before startup this preview contains relative weeks and the
            # confirmed capacity contract, no shared dates or time bookings.
            # Loading automatic holiday metadata cannot invalidate it. Once
            # activated, the full shared-calendar guard remains mandatory.
            models = (PlanVersion, RequirementVersion, PlanTask)
            if self.session.scalar(select(FixedActivityMovePreview.id).where(
                    FixedActivityMovePreview.goal_id == current.goal_id,
                    FixedActivityMovePreview.status == "APPLIED").limit(1)):
                models = (*models, ManualTask, ManualTaskDateOverride, WorkspaceAvailabilityWindow,
                          WorkspaceScheduleProfile, TaskTimeBlock)
        for model in models:
            mapper = model.__mapper__
            query = select(model)
            if relative_only:
                if model is PlanTask:
                    query = query.where(model.plan_version_id == current.id)
                elif model in (PlanVersion, RequirementVersion):
                    query = query.where(model.goal_id == current.goal_id)
            for row in self.session.scalars(query):
                if model is PlanSchedulePreference:
                    # Opening Today lazily creates these defaults. That is a
                    # read-side initialization, not a calendar change; a saved
                    # preview must survive it. Explicit preference changes still
                    # participate in the full calendar guard.
                    goal = self.session.get(Goal, row.goal_id)
                    defaults = {
                        "mode": "FLEXIBLE",
                        "timezone": goal.timezone if goal else "Asia/Shanghai",
                        "break_minutes": 10,
                        "allow_cross_window": False,
                        "reminders_enabled": True,
                        "supervision_mode": "STANDARD",
                        "cross_plan_priority_mode": "BALANCED",
                        "quiet_start_local": None,
                        "quiet_end_local": None,
                        "muted_commitments_json": [],
                    }
                    if all(getattr(row, key) == value for key, value in defaults.items()):
                        continue
                values = {
                    column.name: str(getattr(row, mapper.get_property_by_column(column).key))
                    for column in model.__table__.columns
                }
                rows.append({"table": model.__tablename__, "id": row.id, "values": values})
        rows.sort(key=lambda item: (item["table"], item["id"]))
        encoded = json.dumps(rows, sort_keys=True, ensure_ascii=False).encode()
        return hashlib.sha256(encoded).hexdigest()

    @staticmethod
    def _recovery_preview_key(context):
        if not context:
            return None
        # Summary text can stay identical after a task move or budget change.
        # Exclude refreshed observation fields, retain every saved operation.
        return {key: context.get(key) for key in (
            "proposed_assignments", "original_task_layout", "original_task_moves",
            "availability_offer", "daily_total_limit_minutes", "recovery_scope",
            "unassigned_task_ids", "requires_direct_extension", "direct_extension_dates",
            "direct_extension_confirmed",
        )}

    def _matching_adjustment_preview(
        self,
        goal_id: str,
        source_plan_version_id: str,
        proposal: PlanAdjustmentProposal,
        source_fingerprint: str,
    ) -> PlanAdjustmentPreview | None:
        proposal_payload = proposal.model_dump(mode="json")
        recovery_key = self._recovery_preview_key(self._active_recovery_context(goal_id))
        previews = self.session.scalars(
            select(PlanAdjustmentPreview)
            .where(
                PlanAdjustmentPreview.goal_id == goal_id,
                PlanAdjustmentPreview.source_plan_version_id == source_plan_version_id,
                PlanAdjustmentPreview.status == "PREVIEW",
            )
            .order_by(PlanAdjustmentPreview.created_at.desc())
        )
        return next(
            (
                item
                for item in previews
                if item.preview_json.get("source_fingerprint") == source_fingerprint
                and item.preview_json.get("proposal") == proposal_payload
                and self._recovery_preview_key(item.preview_json.get("recovery_context")) == recovery_key
            ),
            None,
        )

    def _pending_adjustment_preview(
        self,
        goal_id: str,
        plan: PlanVersion | None,
        proposal: PlanAdjustmentProposal | None,
    ) -> PlanAdjustmentPreviewRead | None:
        """Restore a completed preview when the user revisits the plan page."""
        if plan is None or proposal is None:
            return None
        proposal_payload = proposal.model_dump(mode="json")
        previews = self.session.scalars(
            select(PlanAdjustmentPreview)
            .where(
                PlanAdjustmentPreview.goal_id == goal_id,
                PlanAdjustmentPreview.source_plan_version_id == plan.id,
                PlanAdjustmentPreview.status == "PREVIEW",
            )
            .order_by(PlanAdjustmentPreview.created_at.desc())
        )
        source_fingerprint = self._adjustment_schedule_fingerprint(plan)
        recovery_key = self._recovery_preview_key(self._active_recovery_context(goal_id))
        for preview in previews:
            if (preview.preview_json.get("proposal") == proposal_payload
                    and preview.preview_json.get("source_fingerprint") == source_fingerprint
                    and self._recovery_preview_key(preview.preview_json.get("recovery_context")) == recovery_key):
                return self._adjustment_preview_read(preview)
        return None

    def _adjustment_comparison(self, source, schedule, recovery_context=None, revised=None):
        """Explain a staged draft from task lineage, without allocating or changing it."""
        from collections import defaultdict, deque

        activation = self.session.scalar(select(PlanActivation).where(
            PlanActivation.plan_version_id == source.id))
        if activation is None or not schedule:
            return None
        dates = {row.plan_day_id: row.scheduled_date.isoformat() for row in self.session.scalars(
            select(PlanDaySchedule).where(PlanDaySchedule.activation_id == activation.id))}
        blocks = {row.task_id: row for row in self.session.scalars(
            select(TaskTimeBlock).where(TaskTimeBlock.plan_version_id == source.id))}
        before = []
        for task in self.session.scalars(select(PlanTask).where(
                PlanTask.plan_version_id == source.id, PlanTask.status.in_(("NOT_STARTED", "IN_PROGRESS")))
                .order_by(PlanTask.order_in_day, PlanTask.id)):
            if task.plan_day_id not in dates:
                continue
            block = blocks.get(task.id)
            before.append({"source_task_id": task.id, "title": task.title,
                "date": dates[task.plan_day_id], "minutes": task.estimate_minutes,
                "start": block.start_local if block else None,
                "end": block.end_local if block else None})
        before.sort(key=lambda row: (row["date"], row["title"], row["source_task_id"]))
        by_id = {row["source_task_id"]: row for row in before}
        target_sources = defaultdict(deque)
        if revised is not None:
            revised_activation = self.session.scalar(select(PlanActivation).where(
                PlanActivation.plan_version_id == revised.id))
            revised_dates = {row.plan_day_id: row.scheduled_date.isoformat()
                for row in self.session.scalars(select(PlanDaySchedule).where(
                    PlanDaySchedule.activation_id == revised_activation.id))} if revised_activation else {}
            for task in self.session.scalars(select(PlanTask).where(
                    PlanTask.plan_version_id == revised.id, PlanTask.status.in_(("NOT_STARTED", "IN_PROGRESS")))
                    .order_by(PlanTask.order_in_day, PlanTask.id)):
                if task.source_task_id in by_id and task.plan_day_id in revised_dates:
                    target_sources[(revised_dates[task.plan_day_id], task.title)].append(task.source_task_id)
        # Older saved previews have no comparison snapshot. Recovery assignments
        # contain explicit source IDs, so repeated daily titles remain distinguishable.
        context = recovery_context or {}
        if revised is None:
            for assignment in context.get("proposed_assignments", []):
                origin = by_id.get(assignment.get("backlog_task_id"))
                if origin:
                    target_sources[(assignment["target_date"], "补回：" + origin["title"])].append(origin["source_task_id"])
            for layout in context.get("original_task_layout", []):
                for source_id in layout.get("source_task_ids", []):
                    if source_id in by_id:
                        target_sources[(layout["target_date"], by_id[source_id]["title"])].append(source_id)
        same_day = defaultdict(deque)
        for row in before:
            same_day[(row["date"], row["title"])].append(row["source_task_id"])
        used = set()
        after = []
        today = context.get("today") or self._today_for_goal_id(source.goal_id).isoformat()
        for item in schedule:
            key = (item["date"], item["title"])
            candidates = target_sources.get(key) or same_day.get(key) or deque()
            while candidates and candidates[0] in used:
                candidates.popleft()
            source_id = candidates.popleft() if candidates else None
            origin = by_id.get(source_id)
            if origin:
                used.add(source_id)
            kind = ("RECOVERED" if origin and origin["date"] < today and origin["date"] != item["date"]
                    else "MOVED" if origin and origin["date"] != item["date"]
                    else "MODIFIED" if origin and any(origin.get(k) != item.get(k) for k in ("title", "minutes", "start", "end"))
                    else "UNCHANGED" if origin else "UNMATCHED")
            after.append({**item, "source_task_id": source_id, "source_date": origin["date"] if origin else None,
                "source_minutes": origin["minutes"] if origin else None,
                "source_start": origin["start"] if origin else None, "source_end": origin["end"] if origin else None,
                "change_kind": kind})
        return {"before": before, "after": after, "anchor_date": today,
            "original_end_date": str(context.get("original_end_date") or activation.projected_end_date),
            "proposed_end_date": str(revised_activation.projected_end_date) if revised is not None and revised_activation else max(row["date"] for row in schedule),
            "backlog_minutes": sum(row["minutes"] for row in before if row["date"] < today),
            "backlog_tasks": sum(row["date"] < today for row in before)}

    def _adjustment_preview_read(self, item):
        proposal = item.preview_json.get("proposal", {})
        comparison = item.preview_json.get("comparison")
        source = self.session.get(PlanVersion, item.source_plan_version_id)
        if comparison is None and source is not None:
            comparison = self._adjustment_comparison(source, item.preview_json.get("schedule", []),
                item.preview_json.get("recovery_context"))
        return PlanAdjustmentPreviewRead(
            id=item.id,
            goal_id=item.goal_id,
            source_plan_version_id=item.source_plan_version_id,
            status=item.status,
            fingerprint=item.fingerprint,
            plan=item.preview_json["plan"],
            schedule=item.preview_json.get("schedule", []),
            comparison=comparison,
            unallocated_count=item.preview_json.get("unallocated_count", 0),
            reason_codes=list(proposal.get("reason_codes", ["USER_REQUESTED"])),
            created_at=item.created_at,
        )

    def _publish_revision_schedule(self, goal, current, revised, schedule_plan=None) -> None:
        """Publish an already validated revision and rebuild its calendar.

        Keeping this boundary explicit is the first step toward the next
        preview flow: preview code can call the validation/build phase without
        silently changing the active revision, while the final commit remains
        concentrated here.
        """
        current.status = "SUPERSEDED"
        if goal.status in {"ACTIVE", "RECOVERY_REQUIRED"}:
            revised.status = "CONFIRMED"
            revised.confirmed_at = utc_now()
            goal.active_plan_version_id = revised.id
            goal.status = "ACTIVE"
            goal.pause_until = None
            self._last_schedule_plan = allocate_plan_schedule(
                self.session,
                goal,
                revised.id,
                schedule_plan=schedule_plan,
                affected_dates=changed_schedule_dates(self.session, current.id, revised.id),
                use_ai=schedule_plan is None,
            )

    def _apply_adjustment_locked(self, goal_id: str) -> PlanWorkspace:
        goal, requirement, route = self._get_review_inputs(goal_id)
        current = self._require_review_plan(goal_id)
        was_active = goal.status in {"ACTIVE", "RECOVERY_REQUIRED"}
        conversation = self._ensure_plan_conversation(goal_id)
        messages = self._conversation_messages(conversation.id)
        proposal = self._pending_adjustment(messages)
        proposal = self._scope_exploratory_adjustment(current, proposal)
        recovery_context = self._active_recovery_context(goal_id)
        if proposal and recovery_context and recovery_context.get("ai_allocation_generated"):
            proposal = self._local_recovery_proposal(recovery_context)
        if proposal is None:
            raise HTTPException(status_code=409, detail="No pending plan adjustment exists")
        if not proposal.can_apply or proposal.needs_clarification:
            raise HTTPException(
                status_code=409,
                detail="The pending adjustment still requires clarification",
            )
        recovery_context = self._active_recovery_context(goal_id)
        if recovery_context and recovery_context.get("proposed_assignments"):
            return self._apply_local_recovery(
                goal,
                route,
                current,
                conversation,
                proposal,
                recovery_context,
                was_active,
            )
        guard = self._recovery_guard(goal, current)
        if guard.required:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "RECOVERY_DECISION_REQUIRED",
                    "message": guard.message,
                    "missed_dates": [item.isoformat() for item in guard.missed_dates],
                },
            )

        try:
            # downstream 周只是承接影响，不代表需要重新生成；只有明确
            # affected_weeks 才进入本次应用范围。
            directly_affected = set(proposal.affected_weeks)
            if proposal.scope == "OVERALL":
                directly_affected = set(range(1, current.total_weeks + 1))
            if not directly_affected:
                directly_affected = set(range(1, current.total_weeks + 1))
            is_recovery_proposal = bool(recovery_context)
            # Recovery date grace is measured in days (at most three), not a
            # new planning week.  The model sometimes encodes that grace as
            # total_weeks_change=1; keep the canonical six-week timeline and
            # let the calendar validator enforce the actual end-date bound.
            expected_total_weeks = (
                current.total_weeks
                if is_recovery_proposal
                else current.total_weeks + proposal.total_weeks_change
            )
            locked_coordinates = self._locked_execution_coordinates(current)
            locked_weeks = {week for week, _ in locked_coordinates}
            if locked_weeks and expected_total_weeks < max(locked_weeks):
                raise ProviderUnavailableError(
                    "调整后的周期会删除已经发生的执行记录，原计划保持不变。"
                )
            added_weeks = set(range(current.total_weeks + 1, expected_total_weeks + 1))
            current_coordinates = {
                (day.week_index, day.day_index)
                for day in self.session.scalars(
                    select(PlanDay).where(PlanDay.plan_version_id == current.id)
                )
            }
            modifiable_weeks = {
                week
                for week, day_index in current_coordinates
                if (week, day_index) not in locked_coordinates
            }
            required_weeks = sorted(
                {
                    week
                    for week in directly_affected
                    if week <= expected_total_weeks and week in modifiable_weeks
                }
                | added_weeks
            )
            if not required_weeks:
                raise ProviderUnavailableError(
                    "这份调整建议没有明确需要改动的周，当前安排保持不变。"
                )

            recovery_batches = list(
                self.session.scalars(
                    select(GenerationBatch)
                    .where(
                        GenerationBatch.goal_id == goal_id,
                        GenerationBatch.brief_id
                        == self.session.scalar(
                            select(PlanningBrief.id)
                            .where(
                                PlanningBrief.goal_id == goal_id,
                                PlanningBrief.brief_kind == "RECOVERY",
                            )
                            .order_by(PlanningBrief.version.desc())
                            .limit(1)
                        ),
                        GenerationBatch.scope_type == "RECOVERY_WEEK",
                    )
                    .order_by(GenerationBatch.created_at.desc())
                )
            )
            if not is_recovery_proposal:
                recovery_batches = []
            recovery_by_week = {}
            # 同一周可能残留旧轮次批次；优先使用已有完整结果，避免
            # 新轮次的 QUEUED 记录遮蔽已成功生成的内容。
            recovery_batches.sort(
                key=lambda item: (
                    int(item.scope_json.get("week", 0)),
                    0 if item.status == "COMPLETED" and item.staged_result_json else 1,
                    -item.created_at.timestamp(),
                )
            )
            for recovery_batch in recovery_batches:
                week = int(recovery_batch.scope_json.get("week", 0))
                recovery_by_week.setdefault(week, recovery_batch)
            # Queued or missing recovery batches can be generated during apply.
            # This keeps confirmation reliable if a background worker was interrupted.

            first_weeks = required_weeks[:1]
            first_batch = recovery_by_week.get(first_weeks[0])
            if first_batch and first_batch.status == "COMPLETED" and first_batch.staged_result_json:
                first_result = self._scoped_recovery_result(
                    first_batch.staged_result_json, first_weeks
                )
            else:
                first_result = self.discussion_provider.apply(
                    self._discussion_context(
                        requirement,
                        route,
                        current,
                        "应用已经审核的调整草案。",
                        proposal,
                        application_requested_weeks=first_weeks,
                    )
                )
                if first_batch:
                    first_batch.attempt_count += 1
                    self._complete_compiler_batch(first_batch, first_result.model_dump(mode="json"))
            self._validate_adjustment_batch(first_result, first_weeks, expected_total_weeks)
            self._validate_locked_days_omitted(current, first_result)
            application_outline = self._adjustment_outline(first_result)
            batch_results = {first_weeks[0]: first_result}
            remaining = [
                week
                for week in required_weeks[1:]
                if not (
                    recovery_by_week.get(week)
                    and recovery_by_week[week].status == "COMPLETED"
                    and recovery_by_week[week].staged_result_json
                )
            ]
            for week in required_weeks[1:]:
                stored = recovery_by_week.get(week)
                if stored and stored.status == "COMPLETED" and stored.staged_result_json:
                    batch_results[week] = self._scoped_recovery_result(
                        stored.staged_result_json, [week]
                    )
            if remaining:
                with ThreadPoolExecutor(max_workers=min(3, len(remaining))) as executor:
                    futures = {
                        submit_model_task(executor,
                            self.discussion_provider.apply,
                            self._discussion_context(
                                requirement,
                                route,
                                current,
                                "应用已经审核的调整草案。",
                                proposal,
                                application_requested_weeks=[week],
                                application_outline=application_outline,
                            ),
                        ): week
                        for week in remaining
                    }
                    for future in as_completed(futures):
                        week = futures[future]
                        batch = future.result()
                        self._validate_adjustment_batch(batch, [week], expected_total_weeks)
                        self._validate_locked_days_omitted(current, batch)
                        batch_results[week] = batch
                        stored = recovery_by_week.get(week)
                        if stored:
                            stored.attempt_count += 1
                            self._complete_compiler_batch(stored, batch.model_dump(mode="json"))

            result = PlanAdjustmentApplyResult.model_validate(
                {
                    **first_result.model_dump(mode="json"),
                    "assistant_message": (
                        f"调整已经完成，接下来仍按 {expected_total_weeks} 周推进。"
                        f"这次更新了 {len(required_weeks)} 个相关周，"
                        "其他安排和已经完成的内容都保持原样。"
                    ),
                    "regenerated_weeks": required_weeks,
                    "replacement_days": [
                        day.model_dump(mode="json")
                        for week in required_weeks
                        for day in batch_results[week].replacement_days
                    ],
                }
            )
            added_weeks = set(range(current.total_weeks + 1, result.total_weeks + 1))
            allowed_weeks = directly_affected | added_weeks
            regenerated = set(result.regenerated_weeks)
            # Locked execution days and not-yet-materialized exploratory weeks
            # are deliberately omitted from generation. Completeness therefore
            # applies to the concrete modifiable batch, not every semantic week
            # named by the original proposal.
            required_week_set = set(required_weeks)
            if not required_week_set.issubset(regenerated):
                raise ProviderUnavailableError(
                    "这次没有完整整理好所有相关周，当前安排保持不变，可以重新试一次。"
                )
            if not regenerated.issubset(allowed_weeks):
                raise ProviderUnavailableError(
                    "这次调整超出了你确认的范围，因此没有采用；当前安排保持不变。"
                )
            merged = (
                self._merge_recovery_result(current, result)
                if is_recovery_proposal and result.total_weeks == current.total_weeks
                else self._merge_adjustment_result(current, result)
            )
            route_stages = self._route_stages(route.id)
            if [item.stage_order for item in merged.milestones] != [
                item.order for item in route_stages
            ]:
                raise ProviderUnavailableError(
                    "这次调整后的安排不够完整，因此没有采用；当前安排保持不变。"
                )
            revised = self._persist_revision(
                goal,
                route,
                route_stages,
                current,
                merged,
                proposal.user_intent_summary,
                PLAN_ADJUSTMENT_APPLY_PROMPT_VERSION,
                schedule_type=proposal.target_schedule_type,
            )
            self._apply_schedule_type_transition(
                goal,
                route,
                revised,
                proposal.target_schedule_type,
            )
        except ClarificationProviderError as error:
            self._add_failed_message(conversation, error, "APPLY")
            self.session.commit()
            return self.get_workspace(goal_id)
        except (KeyError, TypeError, ValueError) as error:
            failure = ProviderUnavailableError(
                "这次调整没有完整处理好，当前安排保持不变，可以重新试一次。",
                diagnostic=str(error),
            )
            self._add_failed_message(conversation, failure, "APPLY")
            self.session.commit()
            return self.get_workspace(goal_id)

        if was_active:
            # Validate and build the live calendar before superseding the
            # source revision, so a capacity/end-date failure is atomic.
            # Migration errors are expected business failures (for example,
            # the three-day grace window cannot carry the backlog), not 500s.
            try:
                self._migrate_active_execution(goal, current, revised)
                mark_plan_schedule_review(
                    self.session,
                    goal,
                    current.id,
                    revised.id,
                    "计划调整后，重新核对时间安排。",
                )
            except ProviderUnavailableError as error:
                self.session.rollback()
                conversation = self._ensure_plan_conversation(goal_id)
                self._add_failed_message(conversation, error, "APPLY")
                self.session.commit()
                return self.get_workspace(goal_id)
        self._publish_revision_schedule(goal, current, revised)
        applied_message = result.assistant_message
        if was_active:
            applied_message = (
                f"调整已经发布为计划 v{revised.version}.{revised.revision}，"
                "执行日历和已完成进度已经安全迁移，可以继续执行。"
            )
        self._add_assistant_message(
            conversation,
            applied_message,
            "APPLY",
            {
                "prompt_version": PLAN_ADJUSTMENT_APPLY_PROMPT_VERSION,
                "adjustment_applied": True,
                "regenerated_weeks": result.regenerated_weeks,
                "change_summary": result.change_summary,
                "plan_revision": revised.revision,
            },
        )
        goal.last_active_at = utc_now()
        self.session.commit()
        return self.get_workspace(goal_id)

    def _apply_local_recovery(
        self,
        goal: Goal,
        route: RouteVersion,
        current: PlanVersion,
        conversation: Conversation,
        proposal: PlanAdjustmentProposal,
        recovery_context: dict[str, object],
        was_active: bool,
    ) -> PlanWorkspace:
        shifted_count = int(recovery_context.get("shifted_original_task_count", 0) or 0)
        last_shifted_date = recovery_context.get("last_shifted_date")
        try:
            merged = self._build_local_recovery_revision(current, proposal, recovery_context)
            revised = self._persist_revision(
                goal,
                route,
                self._route_stages(route.id),
                current,
                merged,
                proposal.user_intent_summary,
                "recovery-local-allocation-v1",
            )
            self._apply_recovery_original_layout(current, revised, recovery_context)
            if was_active:
                self._migrate_active_execution(goal, current, revised, recovery_context)
                mark_plan_schedule_review(
                    self.session,
                    goal,
                    current.id,
                    revised.id,
                    "追赶计划改变了任务承接日期，原未来时间安排需要重新确认。",
                )
        except (ProviderUnavailableError, ValidationError, ValueError, TypeError) as error:
            self.session.rollback()
            conversation = self._ensure_plan_conversation(goal.id)
            failure = (
                error
                if isinstance(error, ProviderUnavailableError)
                else ProviderUnavailableError(
                    "局部追赶方案没有通过完整性校验，原计划保持不变。",
                    diagnostic=str(error),
                )
            )
            self._add_failed_message(conversation, failure, "APPLY")
            self.session.commit()
            return self.get_workspace(goal.id)
        current.status = "SUPERSEDED"
        if was_active:
            revised.status = "CONFIRMED"
            revised.confirmed_at = utc_now()
            goal.active_plan_version_id = revised.id
            goal.status = "ACTIVE"
            goal.pause_until = None
            self._last_schedule_plan = allocate_plan_schedule(
                self.session,
                goal,
                revised.id,
                affected_dates=changed_schedule_dates(self.session, current.id, revised.id),
            )
        revised_activation = self.session.scalar(
            select(PlanActivation).where(PlanActivation.plan_version_id == revised.id)
        )
        if recovery_context.get("direct_extension_confirmed"):
            projected_end = (
                revised_activation.projected_end_date.isoformat()
                if revised_activation
                else "顺延执行日"
            )
            applied_summary = (
                f"追赶安排已经发布为计划 v{revised.version}.{revised.revision}。"
                f"积压已优先放入近期窗口，原任务保持顺序并直接顺延；"
                f"新的预计结束日期为 {projected_end}。"
            )
        else:
            applied_summary = (
                f"局部追赶安排已经发布为计划 v{revised.version}.{revised.revision}。"
                f"积压已优先放入近期窗口，{shifted_count} 个原任务按原顺序顺延"
                f"至 {last_shifted_date}；任务内容和时长没有被改写。"
                if shifted_count
                else (
                    f"局部追赶安排已经发布为计划 v{revised.version}.{revised.revision}，"
                    "积压已放入近期窗口，原有后续任务无需顺延。"
                )
            )
        recovery_id = self._close_recovery_drafts(goal.id, "CONFIRMED")
        self._add_assistant_message(
            conversation,
            applied_summary,
            "APPLY",
            {
                "prompt_version": "recovery-local-allocation-v1",
                "adjustment_applied": True,
                "recovery_brief_id": recovery_id,
                "recovery_strategy": (
                    "AI_STRUCTURED_OPERATIONS"
                    if recovery_context.get("ai_allocation_generated")
                    else "DETERMINISTIC_LOCAL"
                ),
                "regenerated_weeks": proposal.affected_weeks,
                "change_summary": revised.change_summary_json,
                "plan_revision": revised.revision,
            },
        )
        goal.last_active_at = utc_now()
        self.session.commit()
        return self.get_workspace(goal.id)

    def _apply_recovery_original_layout(
        self,
        source: PlanVersion,
        revised: PlanVersion,
        recovery_context: dict[str, object],
    ) -> None:
        layouts = [
            item
            for item in recovery_context.get("original_task_layout", [])
            if isinstance(item, dict)
        ]
        if not layouts:
            return
        source_days = list(
            self.session.scalars(select(PlanDay).where(PlanDay.plan_version_id == source.id))
        )
        revised_days = list(
            self.session.scalars(select(PlanDay).where(PlanDay.plan_version_id == revised.id))
        )
        source_by_id = {item.id: item for item in source_days}
        revised_by_coordinate = {(item.week_index, item.day_index): item for item in revised_days}
        clone_by_source_task: dict[str, PlanTask] = {}
        for source_day in source_days:
            revised_day = revised_by_coordinate.get((source_day.week_index, source_day.day_index))
            if revised_day is None:
                continue
            source_tasks = list(
                self.session.scalars(
                    select(PlanTask)
                    .where(PlanTask.plan_day_id == source_day.id)
                    .order_by(PlanTask.order_in_day)
                )
            )
            revised_tasks = list(
                self.session.scalars(
                    select(PlanTask)
                    .where(PlanTask.plan_day_id == revised_day.id)
                    .order_by(PlanTask.order_in_day)
                )
            )
            if len(source_tasks) != len(revised_tasks):
                raise ProviderUnavailableError(
                    "恢复方案无法稳定对应原任务，原安排保持不变。",
                    diagnostic=(
                        f"coordinate={(source_day.week_index, source_day.day_index)}, "
                        f"source_tasks={len(source_tasks)}, revised_tasks={len(revised_tasks)}"
                    ),
                )
            for source_task, revised_task in zip(source_tasks, revised_tasks, strict=True):
                clone_by_source_task[source_task.id] = revised_task
                revised_task.source_task_id = source_task.id
                revised_task.origin_stage_order = (
                    source_task.origin_stage_order
                    or source_by_id[source_task.plan_day_id].stage_order
                )
                revised_task.lineage_task_id = source_task.lineage_task_id or source_task.id

        for layout in layouts:
            source_target_day = source_by_id.get(str(layout.get("target_plan_day_id", "")))
            if source_target_day is None:
                raise ProviderUnavailableError("恢复方案引用了不存在的承接日，原安排保持不变。")
            revised_target_day = revised_by_coordinate.get(
                (source_target_day.week_index, source_target_day.day_index)
            )
            if revised_target_day is None:
                raise ProviderUnavailableError("恢复方案无法对应新的承接日，原安排保持不变。")
            for order, source_task_id in enumerate(layout.get("source_task_ids", []), start=1):
                clone = clone_by_source_task.get(str(source_task_id))
                if clone is None:
                    raise ProviderUnavailableError("恢复方案中的原任务已经变化，原安排保持不变。")
                clone.plan_day_id = revised_target_day.id
                clone.order_in_day = order
        if recovery_context.get("direct_extension_confirmed"):
            extension_groups = [
                [str(task_id) for task_id in group]
                for group in recovery_context.get("direct_extension_task_groups", [])
                if isinstance(group, list) and group
            ]
            if extension_groups:
                extension_dates = [
                    str(item) for item in recovery_context.get("direct_extension_dates", [])
                ]
                if len(extension_dates) != len(extension_groups):
                    raise ProviderUnavailableError("顺延日期没有完整生成，原安排保持不变。")
                last_milestone = self.session.scalar(
                    select(PlanMilestone)
                    .where(PlanMilestone.plan_version_id == revised.id)
                    .order_by(PlanMilestone.end_week.desc(), PlanMilestone.stage_order.desc())
                )
                if last_milestone is None:
                    raise ProviderUnavailableError("无法为顺延任务找到最终阶段，原安排保持不变。")
                base_week = revised.total_weeks
                extension_schedule: list[dict[str, object]] = []
                for index, task_ids in enumerate(extension_groups):
                    week_index = base_week + index // revised.weekly_execution_days + 1
                    day_index = index % revised.weekly_execution_days + 1
                    extension_day = PlanDay(
                        plan_version_id=revised.id,
                        milestone_id=last_milestone.id,
                        stage_order=last_milestone.stage_order,
                        week_index=week_index,
                        day_index=day_index,
                        primary_goal="承接因中断恢复而顺延的原任务",
                        suggested_minutes=sum(
                            clone_by_source_task[task_id].estimate_minutes
                            for task_id in task_ids
                            if task_id in clone_by_source_task
                        ),
                        evidence_json=[],
                        do_not_json=["不新增目标，只完成顺延的原任务"],
                    )
                    self.session.add(extension_day)
                    self.session.flush()
                    revised_days.append(extension_day)
                    extension_schedule.append(
                        {
                            "week_index": week_index,
                            "day_index": day_index,
                            "date": extension_dates[index],
                        }
                    )
                    for order, task_id in enumerate(task_ids, start=1):
                        clone = clone_by_source_task.get(task_id)
                        if clone is None:
                            raise ProviderUnavailableError("顺延任务已经变化，原安排保持不变。")
                        clone.plan_day_id = extension_day.id
                        clone.order_in_day = order
                revised.total_weeks = base_week + ceil(
                    len(extension_groups) / revised.weekly_execution_days
                )
                last_milestone.end_week = revised.total_weeks
                recovery_context["direct_extension_schedule"] = extension_schedule
        self.session.flush()
        for revised_day in revised_days:
            revised_day.suggested_minutes = max(
                revised_day.suggested_minutes,
                sum(
                    task.estimate_minutes
                    for task in self.session.scalars(
                        select(PlanTask).where(PlanTask.plan_day_id == revised_day.id)
                    )
                ),
            )
        self.session.flush()

    def restore_plan_version(self, goal_id: str, plan_id: str) -> PlanWorkspace:
        with _plan_generation_locks[goal_id]:
            return self._restore_plan_version_locked(goal_id, plan_id)

    def _restore_plan_version_locked(
        self,
        goal_id: str,
        plan_id: str,
    ) -> PlanWorkspace:
        goal, _, route = self._get_review_inputs(goal_id)
        current = self._require_review_plan(goal_id)
        source = self.session.get(PlanVersion, plan_id)
        if source is None or source.goal_id != goal_id or source.route_version_id != route.id:
            raise HTTPException(status_code=404, detail="Plan version not found")
        if source.id == current.id:
            return self.get_workspace(goal_id)
        payload = self._plan_payload(source)
        restored = PlanRevisionResult.model_validate(
            {
                **payload,
                "assistant_message": "之前保存的那份安排已经恢复，可以继续查看和使用。",
                "change_summary": ["恢复了之前保存的完整安排。"],
            }
        )
        route_stages = self._route_stages(route.id)
        revised = self._persist_revision(
            goal,
            route,
            route_stages,
            current,
            restored,
            f"恢复计划 v{source.version}.{source.revision}",
            "plan-restore-v1",
        )
        current.status = "SUPERSEDED"
        conversation = self._ensure_plan_conversation(goal_id)
        self._add_assistant_message(
            conversation,
            restored.assistant_message,
            "RESTORE",
            {
                "adjustment_dismissed": True,
                "change_summary": restored.change_summary,
                "plan_revision": revised.revision,
                "restored_from_plan_id": source.id,
            },
        )
        goal.last_active_at = utc_now()
        self.session.commit()
        return self.get_workspace(goal_id)

    def draft_plan(self, goal_id: str, generation_mode: str = "STANDARD") -> PlanWorkspace:
        goal, requirement, route = self._get_inputs(goal_id)
        if goal.status not in {"PLAN_DRAFTING", "PLAN_REVIEW"}:
            raise HTTPException(status_code=409, detail="Goal is not ready for plan drafting")
        existing = self._current_plan(goal_id)
        if existing and self._milestones(existing.id):
            return self.get_workspace(goal_id)

        route_stages = self._route_stages(route.id)
        context_pack, brief = self._ensure_planning_brief(goal_id)
        execution_model = (
            ExecutionModelVersion.HYBRID_WORK_ITEM
            if hybrid_rollout_enabled(
                goal.id,
                self.hybrid_rollout_mode,
                self.hybrid_rollout_percentage,
                self.enable_hybrid_work_items,
            )
            else ExecutionModelVersion.LEGACY_DAY_BASED
        )
        orchestration = self.orchestrator.prepare(
            goal.id,
            (
                "WORK_ITEM_COMPILATION"
                if execution_model == ExecutionModelVersion.HYBRID_WORK_ITEM
                else "FULL_PLAN"
            ),
            {
                **context_pack.facts_json,
                "requirements": requirement.snapshot_json,
                "generation_scope": "OUTLINE",
            },
            context_pack_id=context_pack.id,
            minimum_tier="L2",
        )
        context = self._plan_context(
            requirement,
            route,
            route_stages,
            planning_context={
                **context_pack.facts_json,
                "agent_orchestration": orchestration,
            },
        )
        batch = self._begin_compiler_batch(
            goal.id,
            None,
            brief,
            f"outline:{execution_model}:{goal.id}:{requirement.id}:{route.id}",
            (
                "HYBRID_OUTLINE"
                if execution_model == ExecutionModelVersion.HYBRID_WORK_ITEM
                else "OUTLINE"
            ),
            {
                "generation_mode": generation_mode,
                "execution_model": execution_model,
            },
        )
        if execution_model == ExecutionModelVersion.HYBRID_WORK_ITEM:
            context = self._resume_hybrid_context(batch, context)
        stage_orders = [stage.order for stage in route_stages]
        try:
            if execution_model == ExecutionModelVersion.HYBRID_WORK_ITEM:
                for repair_round in range(3):
                    result = self.provider.draft_hybrid_outline(
                        context,
                        generation_mode,
                        checkpoint=lambda document: self._checkpoint_hybrid_artifact(
                            batch,
                            "AUTHOR_COMPLETE",
                            source_document=document,
                            retry_scope={"phase": "OUTLINE"},
                        ),
                    )
                    try:
                        self._validate_executable_commitments(result)
                        self._validate_stage_day_capacity(result)
                        self._validate_hybrid_result(
                            result.standing_work_items,
                            result.work_item_groups,
                            result.work_item_dependencies,
                            result.milestones,
                            result.commitments,
                        )
                        self._validate_hybrid_outline_schedule(result, route.schedule_type)
                        self._validate_deadline_horizon(
                            result,
                            requirement.snapshot_json,
                            goal.timezone,
                            planning_context=context.planning_context,
                        )
                        self._validate_outline_route_coverage(result, stage_orders)
                        self._retry_outline_capacity_conflict(result, requirement.snapshot_json, repair_round)
                        break
                    except WorkItemValidationError as error:
                        self._record_hybrid_validation_failure(
                            batch, repair_round, error, result.source_document,
                        )
                        if repair_round >= 2:
                            raise
                        context = self._business_retry_context(
                            context,
                            "HYBRID_OUTLINE",
                            repair_round + 1,
                            error,
                            result.source_document,
                        )
            else:
                for repair_round in range(3):
                    result = self.provider.draft_outline(context, generation_mode)
                    try:
                        self._validate_executable_commitments(result)
                        self._validate_stage_day_capacity(result)
                        self._validate_deadline_horizon(
                            result,
                            requirement.snapshot_json,
                            goal.timezone,
                            planning_context=context.planning_context,
                        )
                        self._validate_outline_route_coverage(result, stage_orders)
                        self._validate_hybrid_outline_schedule(result, route.schedule_type)
                        self._retry_outline_capacity_conflict(result, requirement.snapshot_json, repair_round)
                        break
                    except WorkItemValidationError as error:
                        if repair_round >= 2:
                            raise
                        context = self._business_retry_context(
                            context,
                            "OUTLINE",
                            repair_round + 1,
                            error,
                            result.source_document,
                        )
        except (ClarificationProviderError, WorkItemValidationError) as error:
            self.orchestrator.fail(orchestration["run_id"], error)
            self._fail_compiler_batch(
                batch,
                error.code,
                getattr(error, "diagnostic", None) or str(error),
            )
            self.session.commit()
            if isinstance(error, WorkItemValidationError):
                raise HTTPException(
                    503,
                    {
                        "code": error.code,
                        "message": "工作项结构连续修复后仍未完整对齐，作者设计稿已保留。",
                        "diagnostic": list(error.issues),
                    },
                ) from error
            self._raise_generation_failure(error, "OUTLINE")

        if existing:
            existing.status = "RECOVERY_SOURCE"
        plan = self._persist_outline(
            goal,
            route,
            route_stages,
            result,
            execution_model=str(execution_model),
        )
        validation = {"status": "PASSED", "phase": "OUTLINE"}
        if isinstance(result, HybridPlanOutlineResult):
            self._persist_hybrid_work_items(
                plan,
                result.standing_work_items,
                result.work_item_groups,
                result.work_item_dependencies,
            )
        batch.plan_version_id = plan.id
        assessment = capacity_assessment(plan, requirement.snapshot_json.get("available_capacity"))
        conflicts = assessment["verified_conflicts"]
        if not conflicts and assessment["state"] == "UNKNOWN" and assessment["confirmed_total_minutes"] is not None:
            diagnostic = json.dumps(assessment, ensure_ascii=False)
            self._fail_compiler_batch(batch, "PLAN_CAPACITY_INPUT_INCOMPLETE", diagnostic)
            self.orchestrator.fail(orchestration["run_id"], WorkItemValidationError(
                "PLAN_CAPACITY_INPUT_INCOMPLETE", ("总容量已保留，阶段分布尚未确认",)))
            self.session.commit()
            raise HTTPException(409, {"code": "PLAN_CAPACITY_INPUT_INCOMPLETE",
                "message": "总可用时间已经保留，但各阶段如何分配还不明确。请一起确认前期和后期的可用时间，再生成安排。",
                "diagnostic": diagnostic, "retryable": False})
        if conflicts:
            diagnostic = json.dumps(conflicts, ensure_ascii=False)
            self._fail_compiler_batch(batch, "PLAN_COMMITMENT_CAPACITY_INVALID", diagnostic)
            self.orchestrator.fail(orchestration["run_id"], WorkItemValidationError(
                "PLAN_COMMITMENT_CAPACITY_INVALID", tuple(item["label"] for item in conflicts)))
            self.session.commit()
            raise HTTPException(409, {
                "code": "PLAN_COMMITMENT_CAPACITY_INVALID",
                "message": "确认的工作量超过了对应窗口的可用时间。已保留需求和时间边界，请一起讨论可行方案。",
                "diagnostic": diagnostic, "retryable": False,
            })
        if isinstance(result, HybridPlanOutlineResult):
            self._complete_hybrid_batch(batch, result.model_dump(mode="json"), validation)
        else:
            self._complete_compiler_batch(batch, result.model_dump(mode="json"))
        self.orchestrator.complete(
            orchestration["run_id"],
            {"status": "PASSED", "phase": "OUTLINE", "total_weeks": result.total_weeks},
        )
        goal.last_active_at = utc_now()
        self.session.commit()
        return self.get_workspace(goal_id)

    @staticmethod
    def _retry_outline_capacity_conflict(result, requirements: dict, repair_round: int) -> None:
        # Catch model-authored demand conflicts before persisting an unusable
        # plan. After the bounded repairs, the existing capacity gate still
        # preserves the draft and asks for negotiation; no limits are changed.
        if repair_round >= 2:
            return
        view = SimpleNamespace(total_weeks=result.total_weeks,
            weekly_execution_days=result.weekly_execution_days,
            temporal_segments_json=[item.model_dump(mode="json") for item in result.temporal_segments],
            commitments_json=[item.model_dump(mode="json") for item in result.commitments])
        conflicts = capacity_assessment(view, requirements.get("available_capacity"))["verified_conflicts"]
        if conflicts:
            raise WorkItemValidationError("PLAN_COMMITMENT_CAPACITY_INVALID",
                (json.dumps(conflicts, ensure_ascii=False),))

    def draft_next_details(self, goal_id: str, generation_mode: str = "STANDARD") -> PlanWorkspace:
        with _plan_generation_locks[goal_id]:
            return self._draft_next_details_locked(goal_id, generation_mode)

    def _draft_next_details_locked(
        self, goal_id: str, generation_mode: str = "STANDARD"
    ) -> PlanWorkspace:
        current = self._current_plan(goal_id)
        if current and capacity_conflicts(current):
            raise HTTPException(409, {"code": "PLAN_COMMITMENT_CAPACITY_INVALID",
                                     "message": "请先协商并确认时间边界，再生成可执行安排。"})
        if (
            current is not None
            and current.execution_model_version == ExecutionModelVersion.HYBRID_WORK_ITEM
        ):
            return self._draft_next_hybrid_details_once(goal_id, generation_mode)
        feedback: dict[str, object] | None = None
        repair_rounds = 0
        repair_week_calls = 0
        while True:
            plan = self._current_plan(goal_id)
            missing = (
                [
                    week
                    for week in range(1, self._generation_target(plan) + 1)
                    if week not in self._generated_weeks(plan.id)
                ]
                if plan
                else []
            )
            if feedback:
                parallelism = (
                    1
                    if generation_mode == "DEEP"
                    or getattr(self.provider, "requires_sequential_details", False)
                    else self.detail_parallelism
                )
                repair_week_calls += min(parallelism, len(missing))
                if repair_week_calls > PLAN_AUTOMATIC_REPAIR_WEEK_BUDGET:
                    raise HTTPException(
                        503,
                        {
                            "code": "PLAN_AUTOMATIC_REPAIR_EXHAUSTED",
                            "message": "自动修正已达到本次上限，已完成内容保留，可以稍后继续补齐。",
                            "diagnostic": feedback,
                        },
                    )
            try:
                workspace = self._draft_next_details_once(
                    goal_id,
                    generation_mode,
                    feedback,
                    expand_dependencies=repair_rounds >= 2,
                )
            except HTTPException as error:
                detail = error.detail
                if (
                    not isinstance(detail, dict)
                    or detail.get("code")
                    not in {
                        "PLAN_DEPENDENCY_INVALID",
                        "PLAN_GLOBAL_COMMITMENT_INVALID",
                        "PLAN_WEEK_GENERATION_FAILED",
                    }
                    or detail.get("retryable") is False
                ):
                    raise
                if repair_rounds >= PLAN_AUTOMATIC_REPAIR_ROUNDS:
                    raise HTTPException(
                        503,
                        {
                            **detail,
                            "message": "自动修正暂未完成，已完成内容保留，可以稍后继续补齐。",
                            "automatic_repair_rounds": repair_rounds,
                        },
                    ) from error
                repair_rounds += 1
                feedback = {**detail, "automatic_repair_round": repair_rounds}
                plan = self._current_plan(goal_id)
                if plan:
                    report = dict(plan.compiler_report_json or {})
                    history = list(report.get("automatic_repairs", []))
                    history.append(feedback)
                    report["automatic_repairs"] = history[-20:]
                    plan.compiler_report_json = report
                    self.session.commit()
                continue
            if not feedback or workspace.generation_phase != "DETAILS":
                return workspace

    def _draft_next_hybrid_details_once(
        self,
        goal_id: str,
        generation_mode: str = "STANDARD",
    ) -> PlanWorkspace:
        goal, requirement, route = self._get_inputs(goal_id)
        plan = self._current_plan(goal_id)
        if plan is None:
            return self.get_workspace(goal_id)
        generated = self._generated_weeks(plan.id)
        target = self._generation_target(plan)
        missing = [week for week in range(1, target + 1) if week not in generated]
        if not missing:
            plan.status = "DRAFT"
            goal.status = "PLAN_REVIEW"
            self.session.commit()
            return self.get_workspace(goal_id)
        if plan.status == "DRAFT":
            plan.status = "GENERATING"
            goal.status = "PLAN_DRAFTING"
            self.session.commit()
        if plan.status != "GENERATING":
            return self.get_workspace(goal_id)

        week = missing[0]
        route_stages = self._route_stages(route.id)
        milestones = self._milestones(plan.id)
        outline = self._outline_payload(plan, milestones)
        outline["standing_work_item_registry"] = [
            {
                "item_key": item.item_key,
                "title": item.title,
                "scope_type": item.scope_type,
                "stage_order": item.stage_order,
                "constraints": item.constraints_json,
                "commitment_refs": item.commitment_refs_json,
            }
            for item in self.session.scalars(
                select(PlanWorkItem)
                .where(PlanWorkItem.plan_version_id == plan.id)
                .order_by(PlanWorkItem.created_at)
            )
        ]
        outline["generated_task_registry"] = self._generated_task_registry(plan.id)
        outline["existing_dependency_registry"] = self._registered_dependency_contracts(plan.id)
        persisted_days = [PlanDayDraft.model_validate(item) for item in self._plan_payload(plan)["days"]]
        identities = quantified_workloads(plan.commitments_json)
        outline["remaining_confirmed_workload"] = [{
            "label": promise["label"],
            "remaining_minutes": max(0, int(promise.get("minimum_total_minutes") or 0) - sum(
                self._task_workload_minutes(task, promise["label"], identities)
                for day in persisted_days for task in day.tasks
                if promise["start_week"] <= day.week_index <= promise["end_week"]
                and promise["label"] in task.satisfies_commitments)),
            "deadline_week": promise["end_week"],
        } for promise in plan.commitments_json if promise.get("minimum_total_minutes")]
        context_pack, brief = self._ensure_planning_brief(goal_id)
        orchestration = self.orchestrator.prepare(
            goal.id,
            "WORK_ITEM_COMPILATION",
            {
                **context_pack.facts_json,
                "current_plan": outline,
                "generation_scope": {"weeks": [week]},
            },
            plan_version_id=plan.id,
            context_pack_id=context_pack.id,
            minimum_tier="L2",
        )
        context = self._plan_context(
            requirement,
            route,
            route_stages,
            planning_context={
                **context_pack.facts_json,
                "agent_orchestration": orchestration,
            },
        )
        batch = self._begin_compiler_batch(
            goal.id,
            plan.id,
            brief,
            f"hybrid-week:{plan.id}:{week}:{generation_mode}",
            "HYBRID_WEEK",
            {
                "week_index": week,
                "generation_mode": generation_mode,
                "execution_model": "HYBRID_WORK_ITEM",
            },
        )
        context = self._resume_hybrid_context(batch, context)
        outline = {
            **outline,
            "maximum_execution_days_for_requested_week": self._effective_week_day_limit(plan, week),
            "allowed_day_indexes_for_requested_week": list(range(1, self._effective_week_day_limit(plan, week) + 1)),
        }
        try:
            for repair_round in range(3):
                result = self.provider.draft_hybrid_details(
                    context,
                    outline,
                    week,
                    week,
                    generation_mode,
                    checkpoint=lambda document: self._checkpoint_hybrid_artifact(
                        batch,
                        "AUTHOR_COMPLETE",
                        source_document=document,
                        retry_scope={"phase": "DETAILS", "weeks": [week]},
                    ),
                )
                try:
                    result = repair_bookkeeping(result, outline,
                        enabled=get_settings().plan_compiler_bookkeeping_repair_enabled)
                    validation = self._validate_hybrid_result(
                        result.work_items,
                        result.work_item_groups,
                        result.work_item_dependencies,
                        milestones,
                        plan.commitments_json,
                        plan=plan,
                        requested_weeks={week},
                        days=result.days,
                    )
                    self._bind_hybrid_actions(plan, result)
                    self._validate_detail_result(
                        plan,
                        {item.stage_order: item for item in milestones},
                        [week],
                        result.days,
                        result.source_document,
                        covered_work_item_stages=self._referenced_action_stages(
                            result.days, [*self.session.scalars(select(PlanWorkItem).where(
                                PlanWorkItem.plan_version_id == plan.id)), *result.work_items]),
                    )
                    self._validate_week_dependency_targets(
                        PlanDetailResult(
                            days=result.days,
                            source_document=result.source_document,
                            compiler_report=result.compiler_report,
                        ),
                        outline,
                    )
                    # Repair a shortfall while its last eligible week is still
                    # editable, instead of discovering it after later weeks
                    # have already been saved outside that obligation's window.
                    self._validate_minimum_commitment_minutes(
                        [item for item in plan.commitments_json if int(item["end_week"]) <= week],
                        persisted_days + result.days,
                    )
                    self._validate_remaining_workload_feasibility(
                        plan, plan.commitments_json, persisted_days + result.days, week)
                    validate_action_coverage([*self.session.scalars(select(PlanWorkItem).where(
                        PlanWorkItem.plan_version_id == plan.id)), *result.work_items],
                        persisted_days + result.days, through_week=week,
                        milestone_weeks={None: (1, plan.total_weeks),
                            **{row.stage_order: (row.start_week, row.end_week) for row in milestones}})
                    break
                except (WorkItemValidationError, HTTPException) as error:
                    self._record_hybrid_validation_failure(
                        batch, repair_round, error, result.source_document,
                        compiler_repair=result.compiler_report.get("bookkeeping_repair"),
                    )
                    if repair_round >= 2:
                        raise
                    context = self._business_retry_context(
                        context,
                        "HYBRID_DETAILS",
                        repair_round + 1,
                        error,
                        result.source_document,
                    )
            if len(generated | {week}) == target:
                self._validate_minimum_commitment_minutes(
                    [item for item in plan.commitments_json if int(item["end_week"]) <= target],
                    [PlanDayDraft.model_validate(item) for item in self._plan_payload(plan)["days"]] + result.days,
                )
                existing_refs = {
                    reference
                    for item in self.session.scalars(
                        select(PlanWorkItem).where(PlanWorkItem.plan_version_id == plan.id)
                    )
                    for reference in item.commitment_refs_json
                }
                generated_refs = {
                    reference for item in result.work_items for reference in item.commitment_refs
                }
                commitment_labels = {
                    str(item.get("label")) for item in plan.commitments_json if item.get("label")
                }
                missing_commitments = commitment_labels - existing_refs - generated_refs
                if missing_commitments:
                    raise WorkItemValidationError(
                        "HYBRID_COMMITMENT_COVERAGE_INVALID",
                        ("未被工作项覆盖的确认承诺: " + "、".join(sorted(missing_commitments)),),
                    )
            self._mark_compiler_bookkeeping_validated(result)
            self._persist_hybrid_work_items(
                plan,
                result.work_items,
                result.work_item_groups,
                result.work_item_dependencies,
            )
            plan.total_estimated_minutes += self._persist_days(
                plan,
                {item.stage_order: item for item in milestones},
                result.days,
            )
            self._complete_hybrid_batch(batch, result.model_dump(mode="json"), validation)
            generated.add(week)
            if len(generated) == target:
                plan.status = "DRAFT"
                goal.status = "PLAN_REVIEW"
            goal.last_active_at = utc_now()
            self.orchestrator.complete(
                orchestration["run_id"],
                {"status": "PASSED", "generated_weeks": [week]},
            )
            self.session.commit()
        except (ClarificationProviderError, WorkItemValidationError, HTTPException) as error:
            detail = error.detail if isinstance(error, HTTPException) else None
            code = (
                str(detail.get("code"))
                if isinstance(detail, dict) and detail.get("code")
                else getattr(error, "code", "HYBRID_PLAN_BATCH_INVALID")
            )
            diagnostic = (
                detail.get("diagnostic")
                if isinstance(detail, dict)
                else getattr(error, "diagnostic", None)
            ) or str(error)
            self._fail_compiler_batch(batch, code, diagnostic)
            self.orchestrator.fail(orchestration["run_id"], error)
            self.session.commit()
            raise HTTPException(
                503,
                {
                    "message": self._generation_failure_message(
                        code, diagnostic,
                        "这一周的约束或每日安排暂未整理完整，作者设计稿已保留，可以直接重试。",
                    ),
                    "code": code,
                    "diagnostic": diagnostic,
                    "retryable": code
                    not in {
                        "MODEL_REQUEST_REJECTED",
                        "PROVIDER_CONFIGURATION_ERROR",
                    },
                },
            ) from error
        return self.get_workspace(goal_id)

    @staticmethod
    def _validate_deadline_horizon(
        result,
        requirements: dict[str, object],
        timezone: str,
        *,
        today: date | None = None,
        planning_context: dict[str, object] | None = None,
    ) -> None:
        validate_recurrence_contracts(result, requirements)
        horizon = (planning_context or {}).get("planning_horizon_contract")
        if isinstance(horizon, dict) and horizon.get("enforcement") == "EXACT_TARGET":
            target = horizon.get("target_weeks")
            if isinstance(target, int) and result.total_weeks != target:
                raise WorkItemValidationError(
                    "PLAN_HORIZON_CONTRACT_INVALID",
                    (
                        "计划总周期没有遵守讨论阶段确定的周期边界："
                        f"target_weeks={target}, returned={result.total_weeks}, "
                        f"basis={horizon.get('basis')}, source={horizon.get('source_text')}",
                    ),
                )
        try:
            deadline = snapshot_deadline(requirements, timezone=timezone, require_resolved=True)
        except ValueError as error:
            raise WorkItemValidationError("PLAN_DEADLINE_UNRESOLVED", (str(error),)) from error
        if deadline is None:
            return

        anchor = today or datetime.now(ZoneInfo(timezone)).date()
        remaining_days = (deadline - anchor).days + 1
        maximum_weeks = max(0, ceil(remaining_days / 7))
        if remaining_days > 0 and result.total_weeks <= maximum_weeks:
            return

        raise WorkItemValidationError(
            "PLAN_DEADLINE_HORIZON_INVALID",
            (
                "计划总周期超过明确截止日期允许的范围："
                f"today={anchor.isoformat()}, deadline={deadline.isoformat()}, "
                f"maximum_weeks={maximum_weeks}, returned={result.total_weeks}",
            ),
        )

    @staticmethod
    def _validate_hybrid_outline_schedule(
        result: HybridPlanOutlineResult,
        schedule_type: str,
    ) -> None:
        if schedule_type != "OUTCOME":
            return
        invalid_segments = [
            segment.label
            for segment in result.temporal_segments
            if segment.weekly_execution_days not in {None, 1}
        ]
        if result.weekly_execution_days != 1 or invalid_segments:
            raise WorkItemValidationError(
                "PLAN_OUTCOME_WINDOW_INVALID",
                [
                    "成果型规划每个成果窗口只能有一个 D1 检查点；"
                    f"weekly_execution_days={result.weekly_execution_days}, "
                    f"invalid_segments={invalid_segments}"
                ],
            )

    @staticmethod
    def _validate_outline_route_coverage(result, stage_orders: list[int]) -> None:
        returned = [item.stage_order for item in result.milestones]
        if returned == stage_orders:
            return
        missing = [stage for stage in stage_orders if stage not in returned]
        unexpected = [stage for stage in returned if stage not in stage_orders]
        raise WorkItemValidationError(
            "PLAN_OUTLINE_STAGE_INVALID",
            (
                "规划大纲没有逐项覆盖已确认路线："
                f"expected={stage_orders}, returned={returned}, "
                f"missing={missing}, unexpected={unexpected}",
            ),
        )

    @staticmethod
    def _mark_compiler_bookkeeping_validated(result) -> None:
        for key in ("bookkeeping_repair", "reference_bookkeeping"):
            repair = result.compiler_report.get(key)
            if isinstance(repair, dict):
                result.compiler_report[key] = {**repair, "status": "FULL_BUSINESS_VALIDATION_PASSED"}

    @staticmethod
    def _validation_failure_class(code: str) -> str:
        # This metadata never authorizes repair: business retry routing keeps
        # its narrower reference-only proof and full validation gates.
        if code == "INVALID_MODEL_RESPONSE":
            return "SCHEMA"
        if code == "PLAN_WEEK_DEPENDENCY_INVALID":
            return "REFERENCE"
        if "CAPACITY" in code or "DAY_COUNT" in code:
            return "CAPACITY"
        return "DESIGN"

    @staticmethod
    def _business_retry_context(
        context: PlanContext,
        phase: str,
        attempt: int,
        error: WorkItemValidationError | HTTPException,
        previous_source_document: str | None,
    ) -> PlanContext:
        planning_context = dict(context.planning_context or {})
        detail = getattr(error, "detail", None)
        code = (
            str(detail.get("code"))
            if isinstance(detail, dict) and detail.get("code")
            else getattr(error, "code", "HYBRID_PLAN_BATCH_INVALID")
        )
        diagnostic = (
            detail.get("diagnostic")
            if isinstance(detail, dict)
            else list(getattr(error, "issues", []))
        )
        # One compile-only repair for reference bookkeeping. Content, capacity,
        # stage coverage and workload failures still require a revised design.
        # A repeated reference failure also escalates to the author.
        previous_feedback = planning_context.get("generation_validation_feedback") or {}
        reference_only = (
            code == "PLAN_WEEK_DEPENDENCY_INVALID"
            or (code == "HYBRID_WORK_ITEM_BATCH_INVALID" and isinstance(diagnostic, list)
                and bool(diagnostic)
                and all(str(issue).startswith("依赖边重复:") or
                        "selection.group_key 与组成员声明不一致" in str(issue) or
                        ("缺少候选组" in str(issue) and "candidate_item_keys" in (previous_source_document or "")
                         and "selection.group_key" in (previous_source_document or ""))
                        for issue in diagnostic))
        )
        reuse_source = (bool(previous_source_document) and reference_only
                        and not previous_feedback.get("compile_only_repair"))
        planning_context["generation_validation_feedback"] = {
            "phase": phase,
            "attempt": attempt,
            "code": code,
            "diagnostic": diagnostic,
            "message": str(error),
            "previous_source_document": previous_source_document,
            "validation_detail": detail,
            "failure_class": PlanService._validation_failure_class(code),
            "author_hash": hashlib.sha256((previous_source_document or "").encode()).hexdigest(),
            "invariant_hash": hashlib.sha256(json.dumps({
                "requirements": context.requirements, "route": context.route,
            }, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest(),
            "compile_only_repair": reuse_source,
            "repair_policy": (
                "复用原设计稿，只修正结构化依赖、候选组引用或重复注册的依赖边。保留原行动、工作量、"
                "日期、次数及前置语义，不得删除必要前置条件；无法唯一映射则仍报错。"
                "候选项的selection.group_key必须与candidate_item_keys反向一致。"
                "操作候选组的独立选择步骤不属于候选项本身；在该步骤description中逐字引用设计稿"
                "已定义的group_key，不把这个步骤添加为第四个候选，也不虚造第二个选择型工作项。"
                if reuse_source else
                "重新生成未通过校验的部分，而不是复用错误设计稿。遵守原始确认需求、"
                "时间容量、截止日期、次数承诺与依赖；只在这些边界内重新分配任务。"
                "容量超限时先检查是否重复生成了同一行动的准备、阶段任务和承诺任务；"
                "可以将这些重复步骤合为一个完整可验收任务，保留其全部行动、产出和承诺引用。"
                "窗口内可调换日期或分段执行，但不得取消必做内容、把一次完整训练算作多次，"
                "或把当前周必做次数移出该周。任务数量更少不等于删减用户任务。"
                "work_item_keys只能引用item_key，绝不能引用group_key。选择操作的公共步骤"
                "使用真实普通工作项键，在description引用候选组；候选拍摄动作只引用一个候选成员键，"
                "不把候选组键当工作项，也不把公共选择步骤添加为另一个候选。"
                "前置工作项动作不得同时在work_item_keys中引用它的后置成果；"
                "前置准备已在注册commitment_refs绑定成果工作量时，才可用satisfies_commitments"
                "与workload_minutes记账；未绑定时不能虚构分配键，须保留独立投入并重新核算。"
                "不把后置成果item_key附在前置动作上，否则第一项会被自己的前置阻塞。"
                "不要减少必做次数、虚报估时、扩大容量或修改已完成周来绕过校验。"
                "硬最低时长必须有该成果的用户量化时长原话，source_text不能只有成果数量。"
                "不能把可用容量相乘后伪装成成果最低工作量；建议估时放在行动估时，保留成果验收。"
                "跨周成果只安排本周实际推进量，不能把两周总量全部塞进第一周D1。"
                "如果已知事实包含另外独立的每日活动，不能把其分钟数偷偷加到已确认的工作块容量；"
                "须在结构化容量中完整表达独立时段及其限制，缺少可排时段就先保留待确认，"
                "不能把一周所有天的活动挤成一个超长D1。成果型D1是一个成果窗口，"
                "窗口suggested_minutes与各任务合计也必须遵守该窗口明确的容量。"
                "不同workload_key表示独立工作；一个任务引用多个独立承诺时，"
                "必须在scheduling_policy_data.workload_minutes中逐键分配实际分钟，"
                "合计不能超过estimate_minutes。仅核对其他成果已完成的复盘任务"
                "不要承接那些成果的工作量承诺；用依赖表达先后，不用承诺引用表达依赖。"
            ),
        }
        return PlanContext(
            requirements=context.requirements,
            route=context.route,
            planning_context=planning_context,
            source_document=previous_source_document if reuse_source else None,
        )

    def _draft_next_details_once(
        self,
        goal_id: str,
        generation_mode: str = "STANDARD",
        repair_feedback: dict[str, object] | None = None,
        *,
        expand_dependencies: bool = False,
    ) -> PlanWorkspace:
        goal, requirement, route = self._get_inputs(goal_id)
        plan = self._current_plan(goal_id)
        if plan is None:
            return self.get_workspace(goal_id)
        generated = self._generated_weeks(plan.id)
        target = self._generation_target(plan)
        missing = [week for week in range(1, target + 1) if week not in generated]
        if plan.status == "DRAFT" and missing and self._milestones(plan.id):
            plan.status = "GENERATING"
            goal.status = "PLAN_DRAFTING"
            self.session.commit()
        if plan.status != "GENERATING":
            return self.get_workspace(goal_id)
        if not missing:
            self._reject_invalid_plan_dependency_weeks(plan, expand=expand_dependencies)
            if plan.planning_mode == "COMMITTED":
                self._reject_invalid_plan_commitments(plan)
            plan.status = "DRAFT"
            goal.status = "PLAN_REVIEW"
            self.session.commit()
            return self.get_workspace(goal_id)

        parallelism = (
            1
            if generation_mode == "DEEP"
            or getattr(self.provider, "requires_sequential_details", False)
            else self.detail_parallelism
        )
        requested = missing[:parallelism]
        route_stages = self._route_stages(route.id)
        milestones = self._milestones(plan.id)
        outline = self._outline_payload(plan, milestones)
        outline["generated_task_registry"] = self._generated_task_registry(plan.id)
        context_pack, brief = self._ensure_planning_brief(goal_id)
        orchestration = self.orchestrator.prepare(
            goal.id,
            "FULL_PLAN",
            {
                **context_pack.facts_json,
                "current_plan": self._outline_payload(plan, milestones),
                "generation_scope": {"weeks": requested},
                "repair_feedback": repair_feedback,
            },
            plan_version_id=plan.id,
            context_pack_id=context_pack.id,
            minimum_tier="L2",
        )
        context = self._plan_context(
            requirement,
            route,
            route_stages,
            planning_context={
                **context_pack.facts_json,
                "agent_orchestration": orchestration,
            },
        )
        if repair_feedback:
            context = PlanContext(
                requirements=context.requirements,
                route=context.route,
                planning_context={
                    **(context.planning_context or {}),
                    "generation_validation_feedback": repair_feedback,
                    "repair_policy": (
                        "修正列出的冲突，使用 generated_task_registry 中真实存在的前置任务。"
                        "不得静默删除真实依赖或改变已确认目标、期限、容量、承诺。"
                        "关联周重新生成后须同步修正引用。"
                    ),
                },
            )
        batches = {
            week: self._begin_compiler_batch(
                goal.id,
                plan.id,
                brief,
                f"week:{plan.id}:{week}:{generation_mode}",
                "WEEK",
                {"week_index": week, "generation_mode": generation_mode},
            )
            for week in requested
        }
        milestone_by_order = {item.stage_order: item for item in milestones}
        completed: dict[int, PlanDetailResult] = {}
        failures: list[str] = []
        failure_codes: list[str] = []

        def persist_week(week: int, result: PlanDetailResult) -> None:
            self._record_week_capacity_override(plan, week, result)
            completed[week] = result
            self._complete_compiler_batch(batches[week], result.model_dump(mode="json"))
            plan.total_estimated_minutes += self._persist_days(
                plan,
                milestone_by_order,
                result.days,
            )
            goal.last_active_at = utc_now()
            self.session.commit()

        uncached: list[int] = []
        for week in requested:
            cached = self._validated_cached_week(
                batches[week],
                outline,
                plan,
                milestone_by_order,
                week,
            )
            if cached is None:
                uncached.append(week)
            else:
                persist_week(week, cached)

        with ThreadPoolExecutor(max_workers=max(1, len(uncached))) as executor:
            futures = {
                submit_model_task(executor,
                    self._draft_validated_week,
                    context,
                    outline,
                    plan,
                    milestone_by_order,
                    week,
                    generation_mode,
                ): week
                for week in uncached
            }
            for future in as_completed(futures):
                week = futures[future]
                try:
                    persist_week(week, future.result())
                except ClarificationProviderError as error:
                    failure_codes.append(error.code)
                    self._fail_compiler_batch(
                        batches[week], error.code, error.diagnostic or str(error)
                    )
                    failures.append(f"W{week}: {error.diagnostic or str(error)}")
                    self.session.commit()
                except HTTPException as error:
                    detail = error.detail
                    code = (
                        str(detail.get("code", "PLAN_WEEK_VALIDATION_FAILED"))
                        if isinstance(detail, dict)
                        else "PLAN_WEEK_VALIDATION_FAILED"
                    )
                    self._fail_compiler_batch(batches[week], code, str(detail))
                    failure_codes.append(code)
                    failures.append(f"W{week}: {error.detail}")
                    self.session.commit()

        if len(generated | set(completed)) == target:
            self._reject_invalid_plan_dependency_weeks(plan, expand=expand_dependencies)
            if plan.planning_mode == "COMMITTED":
                self._reject_invalid_plan_commitments(plan)
            plan.status = "DRAFT"
            goal.status = "PLAN_REVIEW"
        goal.last_active_at = utc_now()
        if completed:
            self.orchestrator.complete(
                orchestration["run_id"],
                {"status": "PASSED", "generated_weeks": sorted(completed)},
            )
        elif failures:
            self.orchestrator.fail(
                orchestration["run_id"],
                ProviderUnavailableError("本批次没有生成可采用的周计划。"),
            )
        self.session.commit()
        if failures and not completed:
            diagnostic = "; ".join(failures)
            raise HTTPException(
                status_code=503,
                detail={
                    "message": self._generation_failure_message(
                        "MODEL_REQUEST_REJECTED"
                        if "MODEL_REQUEST_REJECTED" in failure_codes
                        else "PLAN_WEEK_GENERATION_FAILED",
                        diagnostic,
                        "这一批周计划暂时生成失败，之前完成的内容仍然保留。",
                    ),
                    "code": "PLAN_WEEK_GENERATION_FAILED",
                    "diagnostic": diagnostic,
                    "retryable": all(
                        code
                        not in {
                            "MODEL_REQUEST_REJECTED",
                            "PROVIDER_CONFIGURATION_ERROR",
                        }
                        for code in failure_codes
                    ),
                },
            )
        return self.get_workspace(goal_id)

    def _validated_cached_week(
        self,
        batch: GenerationBatch,
        outline: dict[str, object],
        plan: PlanVersion,
        milestone_by_order: dict[int, PlanMilestone],
        week: int,
    ) -> PlanDetailResult | None:
        if (
            getattr(batch, "_previous_status", None) != "FAILED"
            or getattr(batch, "_previous_failure_code", None)
            not in {"MODEL_REQUEST_REJECTED", "MODEL_TIMEOUT"}
            or not batch.staged_result_json
        ):
            return None
        try:
            result = PlanDetailResult.model_validate(batch.staged_result_json)
            self._validate_detail_result(
                plan,
                milestone_by_order,
                [week],
                result.days,
                result.source_document,
            )
            self._validate_week_dependency_targets(result, outline)
        except (ValueError, HTTPException):
            return None
        if {day.week_index for day in result.days} != {week}:
            return None
        return result

    def _draft_validated_week(
        self,
        context: PlanContext,
        outline: dict[str, object],
        plan: PlanVersion,
        milestone_by_order: dict[int, PlanMilestone],
        week: int,
        generation_mode: str,
        *,
        repair_batch: GenerationBatch | None = None,
    ) -> PlanDetailResult:
        """Regenerate a week when structured output violates plan semantics."""
        attempt_context = context
        week_outline = {
            **outline,
            "execution_model_contract": {
                "version": getattr(plan, "execution_model_version", "LEGACY_DAY_TASK"),
                "rule": (
                    "当前是按日任务模型，不存在工作项注册表。每个执行日用stage_order承接本周阶段；"
                    "不要输出work_item_keys或stage_contributions，也不要编造工作项键。"
                    "历史阶段的成果作为输入或depends_on依赖，不宣称本周重新完成历史阶段。"
                    if getattr(plan, "execution_model_version", None) != ExecutionModelVersion.HYBRID_WORK_ITEM
                    else "工作项承接必须引用已注册的真实工作项键。"
                ),
            },
            "maximum_execution_days_for_requested_week": self._effective_week_day_limit(plan, week),
            "allowed_day_indexes_for_requested_week": list(
                range(1, self._effective_week_day_limit(plan, week) + 1)
            ),
            "authoritative_stages_for_requested_week": [
                {
                    "stage_order": stage_order,
                    "start_week": milestone.start_week,
                    "end_week": milestone.end_week,
                }
                for stage_order, milestone in milestone_by_order.items()
                if milestone.start_week <= week <= milestone.end_week
            ],
            "required_exclusive_stage_orders": [
                order for order, milestone in milestone_by_order.items()
                if milestone.start_week == week == milestone.end_week
            ],
            "active_commitments_for_requested_weeks": [
                commitment
                for commitment in outline.get("commitments", [])
                if int(commitment["start_week"]) <= week <= int(commitment["end_week"])
            ],
        }
        for attempt in range(PLAN_DETAIL_BUSINESS_VALIDATION_RETRIES + 1):
            result = self.provider.draft_details(
                attempt_context,
                week_outline,
                week,
                week,
                generation_mode,
            )
            try:
                self._validate_detail_result(
                    plan,
                    milestone_by_order,
                    [week],
                    result.days,
                    result.source_document,
                )
                self._validate_week_dependency_targets(result, week_outline)
                return result.model_copy(
                    update={
                        "compiler_report": {
                            **result.compiler_report,
                            "business_validation_attempts": attempt + 1,
                        }
                    }
                )
            except HTTPException as error:
                if repair_batch:
                    self._record_hybrid_validation_failure(
                        repair_batch, attempt, error, result.source_document)
                if attempt >= PLAN_DETAIL_BUSINESS_VALIDATION_RETRIES:
                    raise
                detail = error.detail
                feedback = (
                    detail
                    if isinstance(detail, dict)
                    else {"code": "PLAN_WEEK_VALIDATION_FAILED", "message": str(detail)}
                )
                planning_context = dict(attempt_context.planning_context or {})
                planning_context["generation_validation_feedback"] = {
                    "week_index": week,
                    "attempt": attempt + 1,
                    "previous_source_document": result.source_document,
                    "repair_instruction": (
                        "只重写本周未通过校验的安排，保留确认的阶段、容量、次数与日期。"
                        "按日任务模型使用day.stage_order表达阶段；不要填写没有真实工作项的stage_contributions。"
                    ),
                    **feedback,
                }
                attempt_context = PlanContext(
                    requirements=context.requirements,
                    route=context.route,
                    planning_context=planning_context,
                )
        raise RuntimeError("Unreachable plan detail validation state")

    @staticmethod
    def _validate_stage_day_capacity(result) -> None:
        if isinstance(result, HybridPlanOutlineResult):
            # Hybrid stages may be fulfilled by flexible work items inside
            # one outcome window. The detail pass validates actual day load.
            return
        for week in range(1, result.total_weeks + 1):
            segments = [item.model_dump() for item in result.temporal_segments]
            segment = next((item for item in segments
                            if item["start_week"] <= week <= item["end_week"]), None)
            capacity = PlanService._week_capacity_override(segment, week, [], None) or segment or {}
            limit = int(capacity.get("weekly_execution_days") or result.weekly_execution_days)
            required = [item.stage_order for item in result.milestones
                        if item.start_week == week == item.end_week]
            if len(required) > limit:
                raise WorkItemValidationError(
                    "PLAN_STAGE_DAY_CAPACITY_INVALID",
                    (f"W{week}: stages={required} require {len(required)} execution days, capacity={limit}; "
                     "redistribute milestone week ranges within the confirmed horizon before drafting details.",),
                )

    @staticmethod
    def _validate_executable_commitments(result) -> None:
        # Capacity does not establish a hard minimum for a deliverable. Ask
        # the author to repair missing evidence; never erase a minimum here.
        unsupported_minutes = [
            item.label for item in result.commitments
            if (getattr(item, "minimum_total_minutes", None)
                or getattr(item, "minimum_minutes_per_occurrence", None))
            and not re.search(
                r"(?:\d+(?:\.\d+)?|[一二两三四五六七八九十百千万半]+)\s*"
                r"(?:分钟|小时|钟头|minutes?\b|mins?\b|hours?\b|hrs?\b|h\b)",
                getattr(item, "source_text", ""), re.IGNORECASE,
            )
        ]
        if unsupported_minutes:
            raise WorkItemValidationError(
                "PLAN_COMMITMENT_MINUTES_UNSUPPORTED",
                tuple(f"{label}: 硬最低时长的source_text缺少该成果的用户量化时长证据。"
                      "保留真实用户最低量并补齐其原话；只有成果数量、没有最低用时要求时，"
                      "将建议估时放在行动估时中，不得把可用容量推导成必须耗满的工作量。"
                      for label in unsupported_minutes),
            )
        # Capacity conditions belong to the temporal contract, not task quotas.
        invalid = [
            item.label for item in result.commitments
            if not re.search(r"完成|阅读|刷题|健身|练习|复盘|编写|运行|演示|整理|背诵|背单词", item.label)
            and re.match(
                r"^(?:(?:执行|学习|工作|训练)\s*)?(?:时段|时间)\s*(?:限定|限制|约束)|"
                r"^(?:可用(?:时间|时段|容量)|容量缺口|延期请求|每周可用|不占用|不得占用|避让(?:课程|工作)|available time)|"
                r"^每周[一二两三四五六七\d\s个]*执行[日天]|"
                r"^.*每周.*每次\s*\d+\s*分钟.*(?:执行节奏|执行单元)",
                item.label.strip(), re.IGNORECASE,
            )
        ]
        if invalid:
            raise WorkItemValidationError(
                "PLAN_NON_EXECUTABLE_COMMITMENT",
                tuple(f"{label}: 可用时间或避让条件应写入 temporal_segments，不得生成次数承诺。"
                      for label in invalid),
            )

        unsupported_daily = [
            item.label for item in result.commitments
            if getattr(item, "cadence", None) == "PER_EXECUTION_DAY"
            and getattr(item, "source_text", "").strip()
            and not re.search(
                r"每|每日|日常|反复|固定|常规|贯穿|持续|坚持|规律|daily|each|every|per\s+(?:day|week)|repeat",
                item.source_text + " " + item.label, re.IGNORECASE,
            )
        ]
        if unsupported_daily:
            raise WorkItemValidationError(
                "PLAN_COMMITMENT_CADENCE_UNSUPPORTED",
                tuple(f"{label}: 原文未要求该行动每执行日重复；一次性实验/阶段成果应按TOTAL编译，"
                      "不能套用整个规划的每周学习节奏或每次时长。"
                      for label in unsupported_daily),
            )

    @staticmethod
    def _effective_week_day_limit(plan, week: int) -> int:
        segment = next((item for item in plan.temporal_segments_json
                        if int(item["start_week"]) <= week <= int(item["end_week"])), None)
        override = PlanService._week_capacity_override(segment, week, [], None)
        capacity = override or segment or {}
        if getattr(plan, "schedule_type", "RHYTHM") == "OUTCOME":
            return 1
        return int(capacity.get("weekly_execution_days") or plan.weekly_execution_days)

    @staticmethod
    @business_check("plan.week_dependencies")
    def _validate_week_dependency_targets(
        result: PlanDetailResult, outline: dict[str, object]
    ) -> None:
        registry = {
            str(item["reference"]).upper() for item in outline.get("generated_task_registry", [])
        }
        local = {
            f"M{day.stage_order}-W{day.week_index}-D{day.day_index}-T{task.order}"
            for day in result.days
            for task in day.tasks
        }
        local_weeks = {day.week_index for day in result.days}
        conflicts = []
        for day in result.days:
            for task in day.tasks:
                for dependency in task.depends_on:
                    normalized = dependency.strip().upper()
                    match = re.fullmatch(r"M\d+-W(\d+)-D\d+-T\d+", normalized)
                    if match is None and normalized not in registry:
                        conflicts.append({"task": task.title, "dependency": dependency,
                                          "reason": "UNRESOLVED_NONCANONICAL_REFERENCE"})
                    # Other weeks can still be in flight; the global pass checks
                    # cross-week references and owns expansion of their repair scope.
                    if match and int(match.group(1)) in local_weeks:
                        if normalized not in registry | local:
                            conflicts.append(
                                {
                                    "task": task.title,
                                    "dependency": dependency,
                                    "reason": "MISSING_PREREQUISITE",
                                }
                            )
        if conflicts:
            raise HTTPException(
                503,
                {
                    "code": "PLAN_WEEK_DEPENDENCY_INVALID",
                    "message": "正在修正本周的前置任务引用。",
                    "conflicts": conflicts[:30],
                    "valid_task_references": sorted(registry | local),
                },
            )

    @staticmethod
    @business_check("plan.details")
    def _validate_detail_result(
        plan,
        milestone_by_order,
        requested,
        days,
        source_document: str | None = None,
        *,
        covered_work_item_stages: set[int] | None = None,
    ) -> None:
        returned_weeks = {day.week_index for day in days}
        if returned_weeks != set(requested):
            raise HTTPException(
                status_code=503,
                detail={
                    "message": "这次没有完整安排好相关周，请重新试一次。",
                    "code": "PLAN_DETAIL_RANGE_INVALID",
                    "diagnostic": f"expected={requested}, returned={sorted(returned_weeks)}",
                },
            )
        day_indexes_by_week: dict[int, list[int]] = {}
        for day in days:
            day_indexes_by_week.setdefault(day.week_index, []).append(day.day_index)
        if any(
            indexes != list(range(1, len(indexes) + 1)) for indexes in day_indexes_by_week.values()
        ):
            raise HTTPException(
                status_code=503,
                detail={
                    "message": "这次每周安排没有整理完整，请重新试一次。",
                    "code": "PLAN_DETAIL_DAY_INDEX_INVALID",
                    "diagnostic": (
                        "day_index must start at 1 and be contiguous; "
                        f"received={day_indexes_by_week}"
                    ),
                },
            )
        if any(
            len(indexes) > PlanService._effective_week_day_limit(plan, week)
            for week, indexes in day_indexes_by_week.items()
        ):
            raise HTTPException(503, {
                "message": "安排的执行日超过了该时期的可用天数，正在重新安排。",
                "code": "PLAN_DETAIL_WEEK_DAY_COUNT_INVALID",
                "diagnostic": f"received={day_indexes_by_week}",
            })
        exclusive_stage_orders = {
            stage_order
            for stage_order, milestone in milestone_by_order.items()
            if milestone.start_week == milestone.end_week and milestone.start_week in set(requested)
        }
        returned_stage_orders = {day.stage_order for day in days} | (covered_work_item_stages or set())
        for day in days:
            for task in day.tasks:
                contributions = (task.stage_contributions or
                                 task.scheduling_policy_data.get("stage_contributions") or [])
                seen = set()
                for contribution in contributions:
                    value = contribution.model_dump() if hasattr(contribution, "model_dump") else contribution
                    stage = value.get("stage_order") if isinstance(value, dict) else None
                    milestone = milestone_by_order.get(stage)
                    if (stage in seen or milestone is None or not str(value.get("action") or "").strip()
                            or not milestone.start_week <= day.week_index <= milestone.end_week
                            or not task.work_item_keys):
                        raise HTTPException(503, {"code": "PLAN_STAGE_CONTRIBUTION_INVALID",
                            "message": "阶段承接必须引用真实工作项，并明确当前周范围内的实际动作。",
                            "task_title": task.title,
                            "week_index": day.week_index,
                            "invalid_contribution": value,
                            "has_work_item_keys": bool(task.work_item_keys),
                            "allowed_stage_orders": [order for order, item in milestone_by_order.items()
                                if item.start_week <= day.week_index <= item.end_week],
                            "repair_instruction": "只为本周路线范围内且引用真实工作项的动作填写阶段承接；历史阶段成果作为输入或依赖，不作为本周完成的阶段。"})
                    seen.add(stage)
                    returned_stage_orders.add(stage)
        missing_exclusive_stages = sorted(exclusive_stage_orders - returned_stage_orders)
        if missing_exclusive_stages:
            raise HTTPException(
                status_code=503,
                detail={
                    "message": "这一批没有覆盖只属于当前周的路线阶段，请重新生成。",
                    "code": "PLAN_STAGE_COVERAGE_INVALID",
                    "diagnostic": (
                        f"requested_weeks={requested}, "
                        f"missing_stage_orders={missing_exclusive_stages}"
                    ),
                },
            )
        for week, indexes in day_indexes_by_week.items():
            segment = next(
                (
                    item
                    for item in plan.temporal_segments_json
                    if int(item["start_week"]) <= week <= int(item["end_week"])
                ),
                None,
            )
            segment_limit = segment.get("weekly_execution_days") if segment else None
            capacity_override = PlanService._week_capacity_override(
                segment,
                week,
                [day for day in days if day.week_index == week],
                source_document,
            )
            if capacity_override:
                segment_limit = capacity_override["weekly_execution_days"]
            if segment_limit is not None and len(indexes) > int(segment_limit):
                raise HTTPException(
                    status_code=503,
                    detail={
                        "message": "这一周的执行日超过了对应时期的可用节奏，请重新生成。",
                        "code": "PLAN_TEMPORAL_SEGMENT_CAPACITY_INVALID",
                        "diagnostic": (
                            f"week={week}, days={len(indexes)}, segment_limit={segment_limit}"
                        ),
                    },
                )
            segment_minutes = (
                capacity_override["suggested_daily_minutes"]
                if capacity_override
                else (segment.get("suggested_daily_minutes") if segment else None)
            )
            if segment_minutes is not None:
                week_days = [day for day in days if day.week_index == week]
                exception_count = int(
                    (
                        capacity_override.get("exception_days_per_week", 0)
                        if capacity_override
                        else segment.get("exception_days_per_week", 0)
                    )
                    or 0
                )
                exception_minutes = (
                    capacity_override.get("exception_daily_minutes")
                    if capacity_override
                    else segment.get("exception_daily_minutes")
                )
                outcome = getattr(plan, "schedule_type", "RHYTHM") == "OUTCOME"
                capacity = capacity_override or segment or {}
                available_days = int(capacity.get("available_days_per_week")
                                     or capacity.get("weekly_execution_days")
                                     or plan.weekly_execution_days)
                exception_slots = min(exception_count, available_days)
                weekly_capacity = (
                    int(segment_minutes) * (available_days - exception_slots)
                    + int(exception_minutes or segment_minutes) * exception_slots
                )
                weekly_load = sum(day.suggested_minutes for day in week_days)
                # An exception day can be either a longer intensive session or a
                # shorter holiday/rest day.  A shorter exception must not become
                # the ceiling for otherwise normal execution days.
                daily_ceiling = max(
                    int(segment_minutes),
                    int(exception_minutes or segment_minutes),
                )
                # Explicit user capacity is a ceiling, not an estimate to
                # silently stretch. Retain legacy estimate tolerance only
                # where no quantified available-day contract exists.
                strict_capacity = bool((segment or {}).get("available_days_per_week"))
                day_tolerance = 1 if strict_capacity else PLAN_DAY_ESTIMATE_TOLERANCE
                week_tolerance = 1 if strict_capacity else PLAN_WEEK_ESTIMATE_TOLERANCE
                exceeds_daily_ceiling = not outcome and (
                    any(day.suggested_minutes > int(daily_ceiling * day_tolerance)
                        for day in week_days)
                    or sum(day.suggested_minutes > int(int(segment_minutes) * day_tolerance)
                           for day in week_days) > exception_slots
                )
                exceeds_weekly_capacity = weekly_load > int(
                    weekly_capacity * week_tolerance
                )
                if exceeds_weekly_capacity or exceeds_daily_ceiling:
                    raise HTTPException(
                        status_code=503,
                        detail={
                            "message": "这一周的单日安排超过了对应时期的建议容量，请重新生成。",
                            "code": "PLAN_TEMPORAL_SEGMENT_MINUTES_INVALID",
                            "diagnostic": (
                                f"week={week}, day_minutes="
                                f"{[day.suggested_minutes for day in week_days]}, "
                                f"normal_limit={segment_minutes}, "
                                f"exception_days={exception_count}, "
                                f"exception_limit={exception_minutes}, "
                                f"weekly_load={weekly_load}, "
                                f"weekly_capacity={weekly_capacity}, "
                                f"weekly_tolerance={PLAN_WEEK_ESTIMATE_TOLERANCE}, "
                                f"day_tolerance={PLAN_DAY_ESTIMATE_TOLERANCE}"
                            ),
                        },
                    )
            PlanService._validate_commitments_for_week(plan, week, days)
        for day in days:
            matching = milestone_by_order.get(day.stage_order)
            if matching is None or not (matching.start_week <= day.week_index <= matching.end_week):
                allowed_stages = sorted(
                    order
                    for order, milestone in milestone_by_order.items()
                    if milestone.start_week <= day.week_index <= milestone.end_week
                )
                raise HTTPException(
                    status_code=503,
                    detail={
                        "message": "这次每日任务与已经确认的阶段安排没有对齐，请重新试一次。",
                        "code": "PLAN_DETAIL_STAGE_INVALID",
                        "diagnostic": (
                            f"week={day.week_index}, day={day.day_index}, "
                            f"returned_stage={day.stage_order}, allowed_stages="
                            f"{allowed_stages}"
                        ),
                    },
                )
            known_commitments = {
                str(item["label"])
                for item in plan.commitments_json or []
                if int(item["start_week"]) <= day.week_index <= int(item["end_week"])
            }
            unknown_bindings = {
                label
                for task in day.tasks
                for label in task.satisfies_commitments
                if label not in known_commitments
            }
            if unknown_bindings:
                raise HTTPException(
                    status_code=503,
                    detail={
                        "message": "这次任务引用了不存在的验收承诺，请重新生成。",
                        "code": "PLAN_COMMITMENT_BINDING_INVALID",
                        "diagnostic": f"unknown_labels={sorted(unknown_bindings)}",
                    },
                )

    @staticmethod
    def _week_capacity_override(
        segment: dict[str, object] | None,
        week: int,
        week_days,
        source_document: str | None,
    ) -> dict[str, object] | None:
        if not segment:
            return None
        for item in segment.get("weekly_capacity_overrides", []) or []:
            if isinstance(item, dict) and int(item.get("week_index", 0)) == week:
                return item
        # A holiday name or an author's overloaded draft is not user consent.
        # Only explicit, scoped overrides in the confirmed outline may apply.
        return None

    @staticmethod
    def _record_week_capacity_override(
        plan: PlanVersion,
        week: int,
        result: PlanDetailResult,
    ) -> None:
        segments = [dict(item) for item in plan.temporal_segments_json]
        changed = False
        for segment in segments:
            if not (int(segment["start_week"]) <= week <= int(segment["end_week"])):
                continue
            override = PlanService._week_capacity_override(
                segment,
                week,
                [day for day in result.days if day.week_index == week],
                result.source_document,
            )
            if override is None:
                break
            overrides = [
                item
                for item in segment.get("weekly_capacity_overrides", []) or []
                if int(item.get("week_index", 0)) != week
            ]
            segment["weekly_capacity_overrides"] = [*overrides, override]
            changed = True
            break
        if changed:
            plan.temporal_segments_json = segments

    @staticmethod
    def _task_matches_commitment(
        task, match_terms: list[str], commitment_label: str | None = None
    ) -> bool:
        bindings = getattr(task, "satisfies_commitments", None)
        if bindings is None:
            bindings = getattr(task, "satisfies_commitments_json", [])
        if commitment_label and commitment_label in (bindings or []):
            return True
        evidence = getattr(task, "evidence", None)
        if evidence is None:
            evidence = getattr(task, "evidence_json", [])
        searchable = " ".join(
            [
                str(task.title),
                str(task.description),
                *[str(item) for item in (evidence or [])],
            ]
        ).casefold()
        compact_searchable = "".join(character for character in searchable if character.isalnum())
        for term in match_terms:
            compact_term = "".join(
                character for character in term.strip().casefold() if character.isalnum()
            )
            if not compact_term:
                continue
            if compact_term in compact_searchable:
                return True
            if len(compact_term) >= 4 and any(
                compact_term[index : index + 2] in compact_searchable
                for index in range(len(compact_term) - 1)
            ):
                return True
        return False

    @staticmethod
    def _task_has_commitment_binding(task, commitment_label: str) -> bool:
        bindings = getattr(task, "satisfies_commitments", None)
        if bindings is None:
            bindings = getattr(task, "satisfies_commitments_json", [])
        return commitment_label in (bindings or [])

    @classmethod
    def _count_commitment_days(
        cls,
        days_and_tasks,
        commitment_label: str,
        match_terms: list[str],
    ) -> int:
        values = list(days_and_tasks)
        explicitly_bound = set()
        for day, tasks in values:
            for task in tasks:
                if not cls._task_has_commitment_binding(task, commitment_label):
                    continue
                metadata = getattr(task, 'scheduling_policy_data', None)
                if metadata is None:
                    metadata = getattr(task, 'scheduling_policy_json', None)
                identity = getattr(task, 'occurrence_key', None) or (metadata or {}).get('occurrence_key')
                fallback = (getattr(day, 'id', None) or
                            f'W{day.week_index}-D{day.day_index}'
                            if hasattr(day, 'week_index') else str(day))
                explicitly_bound.add(str(identity or fallback))
        if explicitly_bound:
            return len(explicitly_bound)
        return sum(
            1
            for _day, tasks in values
            if any(
                cls._task_matches_commitment(task, match_terms, commitment_label) for task in tasks
            )
        )

    @classmethod
    def _is_separate_commitment_day(cls, day, tasks, commitments) -> bool:
        return any(
            commitment.get("requires_separate_execution_day")
            and int(commitment["start_week"]) <= day.week_index <= int(commitment["end_week"])
            and cls._count_commitment_days(
                [(day, tasks)],
                str(commitment["label"]),
                [str(item) for item in commitment.get("match_terms", [])],
            )
            for commitment in commitments
        )

    @classmethod
    def _validate_commitments_for_week(cls, plan, week: int, days) -> None:
        commitments = list(plan.commitments_json or [])
        for commitment in commitments:
            if not (int(commitment["start_week"]) <= week <= int(commitment["end_week"])):
                continue
            cadence = commitment["cadence"]
            if cadence == "TOTAL":
                continue
            terms = [str(item) for item in commitment.get("match_terms", [])]
            minimum = int(commitment.get("minimum_occurrences", 0))
            maximum = commitment.get("maximum_occurrences")
            groups = (
                [days]
                if cadence == "PER_WEEK"
                else [
                    [day]
                    for day in days
                    if not cls._is_separate_commitment_day(day, day.tasks, commitments)
                ]
            )
            for group in groups:
                count = cls._count_commitment_days(
                    ((day, day.tasks) for day in group),
                    str(commitment["label"]),
                    terms,
                )
                if count < minimum or (maximum is not None and count > int(maximum)):
                    raise HTTPException(
                        status_code=503,
                        detail={
                            "message": "这一周没有兑现用户明确要求的任务频率，请重新生成。",
                            "code": "PLAN_COMMITMENT_INVALID",
                            "diagnostic": (
                                f"week={week}, commitment={commitment['label']}, "
                                f"count={count}, expected={minimum}.."
                                f"{maximum if maximum is not None else '*'}"
                            ),
                        },
                    )

    def _reject_invalid_plan_dependency_weeks(
        self, plan: PlanVersion, *, expand: bool = False
    ) -> None:
        days = list(
            self.session.scalars(
                select(PlanDay)
                .where(PlanDay.plan_version_id == plan.id)
                .order_by(PlanDay.week_index, PlanDay.day_index)
            )
        )
        day_by_id = {day.id: day for day in days}
        tasks = list(
            self.session.scalars(select(PlanTask).where(PlanTask.plan_version_id == plan.id))
        )
        allocator = DayAllocator(self.session)
        indexes = allocator._dependency_indexes(plan.id)
        invalid_weeks: set[int] = set()
        related_weeks: set[int] = set()
        conflicts: list[dict[str, object]] = []
        for task in tasks:
            task_day = day_by_id[task.plan_day_id]
            task_position = (task_day.week_index, task_day.day_index, task.order_in_day)
            for dependency in task.depends_on_json or []:
                resolved = allocator._resolve_dependency(dependency, task, indexes)
                if resolved is None:
                    invalid_weeks.add(task_day.week_index)
                    match = re.fullmatch(r"M\d+-W(\d+)-D\d+-T\d+", str(dependency), re.I)
                    if match:
                        related_weeks.add(int(match.group(1)))
                    conflicts.append(
                        {
                            "task": task.title,
                            "task_reference": (
                                f"M{task_day.stage_order}-W{task_day.week_index}"
                                f"-D{task_day.day_index}-T{task.order_in_day}"
                            ),
                            "dependency": str(dependency),
                            "reason": "MISSING_PREREQUISITE",
                        }
                    )
                    continue
                dependency_day = day_by_id.get(resolved.plan_day_id)
                if (
                    dependency_day is None
                    or (
                        dependency_day.week_index,
                        dependency_day.day_index,
                        resolved.order_in_day,
                    )
                    >= task_position
                ):
                    invalid_weeks.add(task_day.week_index)
                    if dependency_day:
                        related_weeks.add(dependency_day.week_index)
                    conflicts.append(
                        {
                            "task": task.title,
                            "week_index": task_day.week_index,
                            "dependency": str(dependency),
                            "reason": "PREREQUISITE_NOT_EARLIER",
                        }
                    )
        if not invalid_weeks:
            return
        if expand:
            scope = invalid_weeks | (related_weeks & set(range(1, plan.total_weeks + 1)))
            # Expand to persisted downstream consumers so references never silently
            # acquire a different meaning after their prerequisite week is rewritten.
            while True:
                affected_ids = {
                    task.id for task in tasks if day_by_id[task.plan_day_id].week_index in scope
                }
                enlarged = scope | {
                    day_by_id[task.plan_day_id].week_index
                    for task in tasks
                    if any(
                        (resolved := allocator._resolve_dependency(dep, task, indexes)) is not None
                        and resolved.id in affected_ids
                        for dep in task.depends_on_json or []
                    )
                }
                if enlarged == scope:
                    break
                scope = enlarged
            if len(scope) > PLAN_DEPENDENCY_EXPANSION_WEEK_LIMIT:
                raise HTTPException(
                    503,
                    {
                        "code": "PLAN_DEPENDENCY_REPAIR_SCOPE_LIMIT",
                        "message": "关联任务涉及较多周，已保留草案，暂未自动扩大修改范围。",
                        "weeks": sorted(scope),
                        "conflicts": conflicts[:30],
                    },
                )
            invalid_weeks = scope
        self._ensure_generation_weeks_mutable(plan, invalid_weeks)
        invalid_day_ids = {day.id for day in days if day.week_index in invalid_weeks}
        for day in days:
            if day.id in invalid_day_ids:
                self.session.delete(day)
        self.session.flush()
        plan.total_estimated_minutes = int(
            self.session.scalar(
                select(func.coalesce(func.sum(PlanTask.estimate_minutes), 0)).where(
                    PlanTask.plan_version_id == plan.id
                )
            )
            or 0
        )
        self.session.commit()
        raise HTTPException(
            status_code=503,
            detail={
                "message": "部分周的任务前置关系没有正确对应，已撤回这些周并准备重新生成。",
                "code": "PLAN_DEPENDENCY_INVALID",
                "diagnostic": f"weeks={sorted(invalid_weeks)}",
                "weeks": sorted(invalid_weeks),
                "conflicts": conflicts[:30],
                "repair_scope": "RELATED_WEEKS" if expand else "INVALID_WEEKS",
                "previous_tasks": [
                    {
                        "reference": (
                            f"M{day_by_id[task.plan_day_id].stage_order}"
                            f"-W{day_by_id[task.plan_day_id].week_index}"
                            f"-D{day_by_id[task.plan_day_id].day_index}-T{task.order_in_day}"
                        ),
                        "title": task.title,
                        "evidence": list(task.evidence_json or []),
                        "depends_on": list(task.depends_on_json or []),
                    }
                    for task in tasks
                    if day_by_id[task.plan_day_id].week_index in invalid_weeks
                ],
            },
        )

    def _ensure_generation_weeks_mutable(self, plan: PlanVersion, weeks: set[int]) -> None:
        locked = self._locked_execution_coordinates(plan)
        tasks = list(
            self.session.scalars(
                select(PlanTask)
                .join(PlanDay, PlanDay.id == PlanTask.plan_day_id)
                .where(
                    PlanTask.plan_version_id == plan.id,
                    PlanDay.week_index.in_(weeks),
                    PlanTask.status != "NOT_STARTED",
                )
            )
        )
        if (
            plan.confirmed_at
            or plan.status not in {"GENERATING", "DRAFT"}
            or tasks
            or any(week in weeks for week, _day in locked)
        ):
            raise HTTPException(
                409,
                {
                    "code": "PLAN_REPAIR_PROTECTED_EXECUTION",
                    "message": "关联周已有执行记录或已确认，未自动重写，请通过计划调整处理。",
                    "weeks": sorted(weeks),
                },
            )

    def _reject_invalid_plan_commitments(self, plan: PlanVersion) -> None:
        if not plan.commitments_json:
            return
        days = list(
            self.session.scalars(
                select(PlanDay)
                .where(PlanDay.plan_version_id == plan.id)
                .order_by(PlanDay.week_index, PlanDay.day_index)
            )
        )
        tasks = list(
            self.session.scalars(
                select(PlanTask)
                .where(PlanTask.plan_version_id == plan.id)
                .order_by(PlanTask.plan_day_id, PlanTask.order_in_day)
            )
        )
        tasks_by_day: dict[str, list[PlanTask]] = defaultdict(list)
        for task in tasks:
            tasks_by_day[task.plan_day_id].append(task)
        invalid_weeks: set[int] = set()
        diagnostics: list[str] = []
        invalid_scopes: list[str] = []
        for commitment in plan.commitments_json:
            start_week = int(commitment["start_week"])
            end_week = int(commitment["end_week"])
            scoped = [day for day in days if start_week <= day.week_index <= end_week]
            terms = [str(item) for item in commitment.get("match_terms", [])]
            minimum = int(commitment.get("minimum_occurrences", 0))
            maximum = commitment.get("maximum_occurrences")
            cadence = commitment["cadence"]
            if cadence == "TOTAL":
                groups = [(set(range(start_week, end_week + 1)), scoped)]
            elif cadence == "PER_WEEK":
                groups = [
                    ({week}, [day for day in scoped if day.week_index == week])
                    for week in range(start_week, end_week + 1)
                ]
            else:
                groups = [
                    ({day.week_index}, [day])
                    for day in scoped
                    if not self._is_separate_commitment_day(
                        day, tasks_by_day.get(day.id, []), plan.commitments_json
                    )
                ]
            for group_weeks, group_days in groups:
                count = self._count_commitment_days(
                    ((day, tasks_by_day.get(day.id, [])) for day in group_days),
                    str(commitment["label"]),
                    terms,
                )
                # TOTAL describes one stage-level outcome, not the number of days used
                # to produce it. Several bound tasks may legitimately contribute to the
                # same outcome, so day counts can prove coverage but cannot prove that
                # the outcome was duplicated.
                exceeds_maximum = (
                    cadence != "TOTAL" and maximum is not None and count > int(maximum)
                )
                if count >= minimum and not exceeds_maximum:
                    continue
                repair_weeks = {end_week} if cadence == "TOTAL" and count < minimum else group_weeks
                invalid_weeks.update(repair_weeks)
                invalid_scopes.append(f"{commitment['label']}:{cadence}:{sorted(group_weeks)}")
                diagnostics.append(
                    f"{commitment['label']}@{sorted(group_weeks)}={count}, "
                    f"expected={minimum}.."
                    f"{maximum if maximum is not None and cadence != 'TOTAL' else '*'}"
                )
        if not invalid_weeks:
            return
        repair_key = "|".join(sorted(invalid_scopes))
        previous_repairs = list((plan.compiler_report_json or {}).get("automatic_repairs", []))
        if any(
            item.get("code") == "PLAN_GLOBAL_COMMITMENT_INVALID"
            and item.get("repair_key") == repair_key
            for item in previous_repairs
            if isinstance(item, dict)
        ):
            raise HTTPException(
                status_code=503,
                detail={
                    "message": (
                        "同一项阶段要求自动修正后仍未满足，已停止重复回滚。"
                        "现有周计划全部保留，请稍后重试或在讨论中调整这项要求。"
                    ),
                    "code": "PLAN_GLOBAL_COMMITMENT_REPAIR_REPEATED",
                    "diagnostic": "; ".join(diagnostics),
                    "repair_key": repair_key,
                    "retryable": False,
                },
            )
        self._ensure_generation_weeks_mutable(plan, invalid_weeks)
        invalid_day_ids = {day.id for day in days if day.week_index in invalid_weeks}
        for day in days:
            if day.id in invalid_day_ids:
                self.session.delete(day)
        self.session.flush()
        plan.total_estimated_minutes = int(
            self.session.scalar(
                select(func.coalesce(func.sum(PlanTask.estimate_minutes), 0)).where(
                    PlanTask.plan_version_id == plan.id
                )
            )
            or 0
        )
        self.session.commit()
        raise HTTPException(
            status_code=503,
            detail={
                "message": "部分周没有兑现已确认的数量或频率要求，已撤回并准备重新生成。",
                "code": "PLAN_GLOBAL_COMMITMENT_INVALID",
                "diagnostic": "; ".join(diagnostics),
                "weeks": sorted(invalid_weeks),
                "repair_key": repair_key,
            },
        )

    @business_check("plan.hybrid")
    def _validate_hybrid_result(
        self,
        work_items,
        groups,
        dependencies,
        milestones,
        commitments,
        *,
        plan: PlanVersion | None = None,
        requested_weeks: set[int] | None = None,
        days=None,
    ) -> dict[str, object]:
        existing_items = (
            list(
                self.session.scalars(
                    select(PlanWorkItem).where(PlanWorkItem.plan_version_id == plan.id)
                )
            )
            if plan
            else []
        )
        existing_groups = (
            list(
                self.session.scalars(
                    select(PlanWorkItemGroup).where(PlanWorkItemGroup.plan_version_id == plan.id)
                )
            )
            if plan
            else []
        )
        existing_by_id = {item.id: item.item_key for item in existing_items}
        existing_edges = (
            [
                (
                    existing_by_id.get(edge.predecessor_work_item_id, ""),
                    existing_by_id.get(edge.successor_work_item_id, ""),
                )
                for edge in self.session.scalars(
                    select(WorkItemDependency).where(WorkItemDependency.plan_version_id == plan.id)
                )
            ]
            if plan
            else []
        )
        milestone_weeks = {
            int(item.stage_order): (int(item.start_week), int(item.end_week)) for item in milestones
        }
        reused_edges = self._reuse_registered_dependencies(
            dependencies, self._registered_dependency_contracts(plan.id) if plan else [])
        validate_work_item_batch(
            work_items=list(work_items),
            groups=list(groups),
            dependencies=list(dependencies),
            milestone_weeks=milestone_weeks,
            commitment_labels={
                str(item.get("label") if isinstance(item, dict) else item.label)
                for item in commitments
            },
            existing_item_keys=[item.item_key for item in existing_items],
            existing_group_keys=[item.group_key for item in existing_groups],
            existing_group_candidates={group.group_key: {item.item_key for item in existing_items
                if item.group_id == group.id} for group in existing_groups},
            existing_edges=[edge for edge in existing_edges if all(edge)],
            requested_weeks=requested_weeks,
            referenced_item_weeks={key: {day.week_index for day in days or []
                for task in day.tasks if key in task.work_item_keys}
                for key in {key for day in days or [] for task in day.tasks for key in task.work_item_keys}},
        )
        if days:
            from planner_api.work_item_validation import validate_authored_dependency_order

            authored_days = list(days)
            if plan:
                requested = {day.week_index for day in authored_days}
                authored_days = [PlanDayDraft.model_validate(day) for day in self._plan_payload(plan)["days"]
                                 if day["week_index"] not in requested] + authored_days
            validate_authored_dependency_order(authored_days, [
                *(self._registered_dependency_contracts(plan.id) if plan else []),
                *(edge.model_dump(mode="json") for edge in dependencies),
            ])
        return {
            "status": "PASSED",
            "work_item_count": len(work_items),
            "group_count": len(groups),
            "dependency_count": len(dependencies),
            "reused_dependency_count": reused_edges,
            "requested_weeks": sorted(requested_weeks or []),
        }

    def _registered_dependency_contracts(self, plan_id):
        keys = {item.id: item.item_key for item in self.session.scalars(
            select(PlanWorkItem).where(PlanWorkItem.plan_version_id == plan_id))}
        return [{
            'predecessor_key': keys[edge.predecessor_work_item_id],
            'successor_key': keys[edge.successor_work_item_id],
            'dependency_type': edge.dependency_type,
            'matching_mode': edge.matching_mode,
            'minimum_lag_days': edge.minimum_lag_days,
            'maximum_lag_days': edge.maximum_lag_days,
            'condition': {k: v for k, v in (edge.condition_json or {}).items() if k != 'source_refs'},
        } for edge in self.session.scalars(select(WorkItemDependency).where(
            WorkItemDependency.plan_version_id == plan_id))
            if edge.predecessor_work_item_id in keys and edge.successor_work_item_id in keys]

    @staticmethod
    def _reuse_registered_dependencies(dependencies, registered):
        """Only reuse an already persisted, exactly matching semantic edge.

        The database edge and provenance remain untouched. Changes to lags,
        conditions, type or endpoints remain in the batch and fail validation.
        """
        by_pair = {(r['predecessor_key'], r['successor_key']): r for r in registered}
        retained = []
        for dependency in dependencies:
            contract = dependency.model_dump(mode='json', exclude={'source_refs'})
            previous = by_pair.get((dependency.predecessor_key, dependency.successor_key))
            if previous != contract:
                retained.append(dependency)
        reused = len(dependencies) - len(retained)
        dependencies[:] = retained
        return reused

    def _persist_hybrid_work_items(
        self,
        plan: PlanVersion,
        work_items,
        groups,
        dependencies,
    ) -> None:
        milestone_by_order = {item.stage_order: item for item in self._milestones(plan.id)}
        group_by_key = {
            item.group_key: item
            for item in self.session.scalars(
                select(PlanWorkItemGroup).where(PlanWorkItemGroup.plan_version_id == plan.id)
            )
        }
        for draft in groups:
            group = PlanWorkItemGroup(
                plan_version_id=plan.id,
                milestone_id=(
                    milestone_by_order[draft.stage_order].id
                    if draft.stage_order is not None
                    else None
                ),
                scope_type=str(draft.scope_type),
                stage_order=draft.stage_order,
                group_key=draft.group_key,
                title=draft.title,
                selection_mode=draft.selection_mode,
                minimum_selected=draft.minimum_selected,
                maximum_selected=draft.maximum_selected,
                constraints_json={
                    "candidate_item_keys": list(draft.candidate_item_keys),
                    "source_refs": list(draft.source_refs),
                },
                status="DRAFT",
            )
            self.session.add(group)
            self.session.flush()
            group_by_key[draft.group_key] = group

        item_by_key = {
            item.item_key: item
            for item in self.session.scalars(
                select(PlanWorkItem).where(PlanWorkItem.plan_version_id == plan.id)
            )
        }
        for draft in work_items:
            constraints = draft.constraint_set
            group_key = constraints.selection.group_key
            item = PlanWorkItem(
                plan_version_id=plan.id,
                milestone_id=(
                    milestone_by_order[draft.stage_order].id
                    if draft.stage_order is not None
                    else None
                ),
                group_id=group_by_key[group_key].id if group_key else None,
                item_key=draft.item_key,
                scope_type=str(draft.scope_type),
                stage_order=draft.stage_order,
                title=draft.title,
                description=draft.description,
                task_kind=draft.task_kind,
                estimate_minutes=draft.estimate_minutes_per_occurrence,
                priority=draft.priority,
                is_required=draft.is_required,
                is_splittable=draft.is_splittable,
                binding_mode=str(constraints.binding_mode),
                fulfillment_mode=str(constraints.fulfillment_mode),
                target_value=constraints.completion.target_value,
                target_unit=str(constraints.completion.target_unit),
                constraints_json=constraints.model_dump(mode="json"),
                completion_rule_json=constraints.completion.model_dump(mode="json"),
                evidence_json=list(draft.evidence),
                source_refs_json=list(draft.source_refs),
                commitment_refs_json=list(draft.commitment_refs),
                status="DRAFT",
            )
            self.session.add(item)
            self.session.flush()
            item_by_key[draft.item_key] = item

        for draft in dependencies:
            self.session.add(
                WorkItemDependency(
                    plan_version_id=plan.id,
                    predecessor_work_item_id=item_by_key[draft.predecessor_key].id,
                    successor_work_item_id=item_by_key[draft.successor_key].id,
                    dependency_type=draft.dependency_type,
                    matching_mode=draft.matching_mode,
                    minimum_lag_days=draft.minimum_lag_days,
                    maximum_lag_days=draft.maximum_lag_days,
                    condition_json={
                        **draft.condition,
                        "source_refs": list(draft.source_refs),
                    },
                )
            )
        self.session.flush()
        plan.total_estimated_minutes = int(
            self.session.scalar(
                select(func.coalesce(func.sum(PlanTask.estimate_minutes), 0)).where(
                    PlanTask.plan_version_id == plan.id,
                )
            )
            or 0
        )

    def _persist_outline(
        self,
        goal,
        route,
        route_stages,
        result,
        *,
        execution_model: str = "LEGACY_DAY_BASED",
    ) -> PlanVersion:
        window_end, window_basis = self._initial_exploration_boundary(goal, route_stages, result)
        plan = PlanVersion(
            goal_id=goal.id,
            route_version_id=route.id,
            version=1,
            revision=1,
            status="GENERATING",
            summary=result.summary,
            total_weeks=result.total_weeks,
            weekly_execution_days=result.weekly_execution_days,
            weekly_minutes=result.weekly_minutes,
            total_estimated_minutes=0,
            buffer_strategy_json=result.buffer_strategy,
            assumptions_json=result.assumptions,
            open_questions_json=result.open_questions,
            change_summary_json=[],
            temporal_segments_json=[
                item.model_dump(mode="json") for item in result.temporal_segments
            ],
            commitments_json=[item.model_dump(mode="json") for item in result.commitments],
            feedback=None,
            prompt_version=PLAN_PROMPT_VERSION,
            source_document=result.source_document,
            compiled_snapshot_json=result.model_dump(
                mode="json", exclude={"source_document", "compiler_report"}
            ),
            compiler_report_json=(
                result.compiler_report
                or {
                    "status": "VALIDATED",
                    "prompt_version": PLAN_PROMPT_VERSION,
                    "temporal_segment_count": len(result.temporal_segments),
                }
            ),
            planning_mode=goal.planning_mode,
            schedule_type=route.schedule_type,
            execution_model_version=execution_model,
            exploration_window_weeks=window_end,
            next_unplanned_week=(
                window_end + 1 if goal.planning_mode == "EXPLORATORY" else result.total_weeks + 1
            ),
        )
        plan.compiler_report_json = {**plan.compiler_report_json,
            "week_basis": "CALENDAR_MONDAY" if calendar_weeks(plan, from_source=True) else "ROLLING_START"}
        self.session.add(plan)
        self.session.flush()

        stage_by_order = {stage.order: stage for stage in route_stages}
        for item in result.milestones:
            milestone = PlanMilestone(
                plan_version_id=plan.id,
                route_stage_id=stage_by_order[item.stage_order].id,
                stage_order=item.stage_order,
                title=item.title,
                purpose=item.purpose,
                start_week=item.start_week,
                end_week=item.end_week,
                outputs_json=item.outputs,
            )
            self.session.add(milestone)
            self.session.flush()
        self.session.flush()
        if goal.planning_mode == "EXPLORATORY":
            self.session.add(
                PlanWindow(
                    goal_id=goal.id,
                    plan_version_id=plan.id,
                    sequence=1,
                    start_week=1,
                    end_week=window_end,
                    status="DRAFT",
                    trigger="INITIAL",
                    basis_json={
                        "source": "confirmed_discussion_and_route",
                        "route_version_id": route.id,
                        **window_basis,
                    },
                )
            )
        return plan

    def _persist_revision(
        self,
        goal,
        route,
        route_stages,
        current,
        result: PlanRevisionResult,
        feedback: str,
        prompt_version: str = PLAN_DISCUSSION_PROMPT_VERSION,
        schedule_type: str | None = None,
    ) -> PlanVersion:
        plan = PlanVersion(
            goal_id=goal.id,
            route_version_id=route.id,
            version=current.version,
            revision=current.revision + 1,
            status="DRAFT",
            summary=result.summary,
            total_weeks=result.total_weeks,
            weekly_execution_days=result.weekly_execution_days,
            weekly_minutes=result.weekly_minutes,
            total_estimated_minutes=0,
            buffer_strategy_json=result.buffer_strategy,
            assumptions_json=result.assumptions,
            open_questions_json=result.open_questions,
            change_summary_json=result.change_summary,
            temporal_segments_json=[
                item.model_dump(mode="json") for item in result.temporal_segments
            ],
            commitments_json=[item.model_dump(mode="json") for item in result.commitments],
            feedback=feedback,
            prompt_version=prompt_version,
            source_document=current.source_document,
            compiled_snapshot_json=result.model_dump(mode="json"),
            compiler_report_json=dict(current.compiler_report_json),
            planning_mode=current.planning_mode,
            schedule_type=schedule_type or current.schedule_type,
            execution_model_version=current.execution_model_version,
            exploration_window_weeks=current.exploration_window_weeks,
            next_unplanned_week=current.next_unplanned_week,
        )
        self.session.add(plan)
        self.session.flush()

        stage_by_order = {stage.order: stage for stage in route_stages}
        milestone_by_order: dict[int, PlanMilestone] = {}
        for item in result.milestones:
            milestone = PlanMilestone(
                plan_version_id=plan.id,
                route_stage_id=stage_by_order[item.stage_order].id,
                stage_order=item.stage_order,
                title=item.title,
                purpose=item.purpose,
                start_week=item.start_week,
                end_week=item.end_week,
                outputs_json=item.outputs,
            )
            self.session.add(milestone)
            self.session.flush()
            milestone_by_order[item.stage_order] = milestone
        plan.total_estimated_minutes = self._persist_days(
            plan,
            milestone_by_order,
            result.days,
        )
        return plan

    @staticmethod
    def _apply_schedule_type_transition(
        goal: Goal,
        route: RouteVersion,
        plan: PlanVersion,
        target: str | None,
    ) -> None:
        """Keep the active route/goal metadata aligned with an approved type change.

        The route content is unchanged; only its temporal contract changes. A
        future route revision can make this history append-only without changing
        the execution behavior of the local deployment.
        """
        if not target:
            return
        plan.schedule_type = target
        if route.schedule_type != target:
            route.schedule_type = target
        if goal.schedule_type != target:
            goal.schedule_type = target

    def _merge_adjustment_result(self, current, result) -> PlanRevisionResult:
        self._validate_execution_bounds(current, result.total_weeks, result.weekly_execution_days)
        current_payload = self._plan_payload(current)
        regenerated = set(result.regenerated_weeks)
        locked = self._locked_execution_coordinates(current)
        unchanged_days = [
            day
            for day in current_payload["days"]
            if (
                day["week_index"] not in regenerated
                or (day["week_index"], day["day_index"]) in locked
            )
            and day["week_index"] <= result.total_weeks
            and day["day_index"] <= result.weekly_execution_days
        ]
        source_day_by_coordinate = {
            (day["week_index"], day["day_index"]): day for day in current_payload["days"]
        }
        replacement_days = []
        for day in result.replacement_days:
            coordinate = (day.week_index, day.day_index)
            if coordinate in locked:
                continue
            replacement = day.model_dump(mode="json")
            # A stale or failed compiler can emit a day with no tasks. Keep
            # the canonical source day instead of constructing an invalid
            # revision (the plan schema requires at least one task per day).
            if not replacement.get("tasks") and coordinate in source_day_by_coordinate:
                replacement = dict(source_day_by_coordinate[coordinate])
            replacement_days.append(replacement)
        merged_days = sorted(
            [*unchanged_days, *replacement_days],
            key=lambda day: (day["week_index"], day["day_index"]),
        )
        # LLMs occasionally preserve a deleted task's ordinal (for example
        # 1, 3, 4) after merging replacement days.  Ordinals are positional
        # metadata, not task identity; normalize them deterministically before
        # validating/persisting the revision.
        for day in merged_days:
            tasks = day.get("tasks") or []
            for order, task in enumerate(tasks, start=1):
                task["order"] = order
        payload = {
            "assistant_message": result.assistant_message,
            "summary": result.summary,
            "total_weeks": result.total_weeks,
            "weekly_execution_days": result.weekly_execution_days,
            "weekly_minutes": result.weekly_minutes,
            "temporal_segments": (
                [item.model_dump(mode="json") for item in result.temporal_segments]
                if result.temporal_segments is not None
                else current_payload["temporal_segments"]
            ),
            "commitments": (
                [item.model_dump(mode="json") for item in result.commitments]
                if result.commitments is not None
                else current_payload["commitments"]
            ),
            "milestones": [item.model_dump(mode="json") for item in result.milestones],
            "days": merged_days,
            "buffer_strategy": result.buffer_strategy,
            "assumptions": result.assumptions,
            "open_questions": result.open_questions,
            "change_summary": result.change_summary,
        }
        try:
            return PlanRevisionResult.model_validate(payload)
        except ValidationError:
            # Preserve unchanged historical days even if an old capacity label
            # predates the current schema's stricter consistency check.
            for day in merged_days:
                total = sum(int(task.get("estimate_minutes", 0)) for task in day.get("tasks", []))
                day["suggested_minutes"] = max(int(day.get("suggested_minutes", 0)), total)
            payload["days"] = merged_days
            try:
                return PlanRevisionResult.model_validate(payload)
            except ValidationError as error:
                merge_error = error
                if result.total_weeks == current.total_weeks:
                    # Weekday/task reorganizations must not silently rewrite
                    # route-stage boundaries. Restore the source stage ledger
                    # and the stage attached to each existing coordinate.
                    source_stage_by_coordinate = {
                        (day["week_index"], day["day_index"]): day["stage_order"]
                        for day in current_payload["days"]
                    }
                    for day in merged_days:
                        coordinate = (day["week_index"], day["day_index"])
                        if coordinate in source_stage_by_coordinate:
                            day["stage_order"] = source_stage_by_coordinate[coordinate]
                    payload["milestones"] = current_payload["milestones"]
                    payload["days"] = merged_days
                    try:
                        return PlanRevisionResult.model_validate(payload)
                    except ValidationError as repaired_error:
                        merge_error = repaired_error
                raise ProviderUnavailableError(
                    "调整内容已经生成，但阶段结构没有完整对齐；当前计划保持不变。",
                    diagnostic=str(merge_error),
                    code="ADJUSTMENT_MERGE_INVALID",
                ) from None

    def _merge_recovery_result(self, current, result) -> PlanRevisionResult:
        """Compile recovery strategy over the canonical task ledger."""
        source = self._plan_payload(current)
        days = [
            day
            for day in source["days"]
            if day["week_index"] <= result.total_weeks
            and day["day_index"] <= current.weekly_execution_days
        ]
        for day in days:
            for order, task in enumerate(day.get("tasks") or [], start=1):
                task["order"] = order
            day["suggested_minutes"] = max(
                int(day["suggested_minutes"]),
                sum(int(task["estimate_minutes"]) for task in day.get("tasks") or []),
            )
        return PlanRevisionResult.model_validate(
            {
                **source,
                "assistant_message": result.assistant_message,
                "summary": result.summary,
                "days": days,
                "change_summary": result.change_summary,
            }
        )

    @staticmethod
    def _reuse_registered_detail_items(result, registered):
        """Turn redundant declarations into references only when contracts match.

        Never update the registered obligation. Changed dates, duration, gates,
        or completion rules still go through model repair / user discussion.
        """
        by_key = {item.item_key: item for item in registered}
        retained = []
        for item in result.work_items:
            previous = by_key.get(item.item_key)
            if previous is None:
                retained.append(item)
                continue
            old, new = previous.model_dump(mode="json"), item.model_dump(mode="json")
            # Scope/stage redeclarations cannot reclassify an existing identity;
            # keep its registry scope and validate actual stage contributions.
            descriptive = {"description", "evidence", "source_refs", "commitment_refs", "scope_type", "stage_order"}
            for payload in (old, new):
                payload["constraint_set"]["adjustment"].pop("soft_preferences", None)
            compatible = all(old.get(key) == value for key, value in new.items() if key not in descriptive)
            actions = [task for day in result.days for task in day.tasks if item.item_key in task.work_item_keys]
            added_promises = set(item.commitment_refs) - set(previous.commitment_refs)
            represented = {label for task in actions for label in task.satisfies_commitments}
            if not compatible or not actions or not added_promises.issubset(represented):
                raise WorkItemValidationError("HYBRID_REGISTERED_ITEM_CHANGED", (
                    "已注册工作项不能由详情改写: " + item.item_key +
                    "；仅引用原身份，日期/时长/触发/验收规则保持不变，行动新增的承诺须明确写在任务引用中。",))
        result.work_items = retained

    @staticmethod
    def _workload_capacity_lookahead(plan, promises, days, week):
        from planner_api.plan_capacity import week_capacity
        identities = quantified_workloads(promises)
        rows = []
        for item in promises:
            key = item.get("workload_key")
            if not key or not item.get("minimum_total_minutes"):
                continue
            completed = sum(PlanService._task_workload_minutes(task, item["label"], identities)
                for day in days for task in day.tasks if item["label"] in task.satisfies_commitments)
            remaining = max(0, int(item["minimum_total_minutes"]) - completed)
            later = [week_capacity(plan, value)["weekly_minutes"] for value in range(week + 1, int(item["end_week"]) + 1)]
            rows.append({"label": item["label"], "workload_key": key, "remaining_minutes": remaining,
                         "deadline_week": item["end_week"],
                         "minimum_minutes_this_week": max(0, remaining - sum(later)) if all(v is not None for v in later) else None})
        floors = []
        for deadline in sorted({int(row["deadline_week"]) for row in rows}):
            later = [week_capacity(plan, value)["weekly_minutes"] for value in range(week + 1, deadline + 1)]
            if all(value is not None for value in later):
                floors.append({"deadline_week": deadline, "minimum_combined_minutes_this_week": max(0,
                    sum(max(row["remaining_minutes"] for row in rows
                            if row["workload_key"] == key and int(row["deadline_week"]) <= deadline)
                        for key in {row["workload_key"] for row in rows if int(row["deadline_week"]) <= deadline}) - sum(later))})
        return {"week": week, "items": rows, "shared_capacity_floors": floors,
            "policy": "这些是为保留后续可行性计算出的本周工作量下界，不是新增任务。保留全部动作与总量；"
                      "准备/提纲的真实投入若属于成果工作需写对应satisfies_commitments。不得虚报、重复计时或扩大容量。"}

    @staticmethod
    def _validate_remaining_workload_feasibility(plan, promises, days, week):
        from planner_api.plan_capacity import week_capacity
        identities = quantified_workloads(promises)
        by_deadline = {}
        for item in promises:
            key = item.get("workload_key")
            if not key or not item.get("minimum_total_minutes") or int(item["end_week"]) <= week:
                continue
            completed = sum(PlanService._task_workload_minutes(task, item["label"], identities)
                for day in days for task in day.tasks if item["label"] in task.satisfies_commitments)
            remaining = max(0, int(item["minimum_total_minutes"]) - completed)
            by_deadline.setdefault(int(item["end_week"]), []).append((key, remaining))
        for deadline in sorted(by_deadline):
            capacities = [week_capacity(plan, value)["weekly_minutes"] for value in range(week + 1, deadline + 1)]
            remaining_by_key = {}
            for last, entries in by_deadline.items():
                if last <= deadline:
                    for key, minutes in entries:
                        remaining_by_key[key] = max(remaining_by_key.get(key, 0), minutes)
            required = sum(remaining_by_key.values())
            if all(value is not None for value in capacities) and required > sum(capacities):
                raise WorkItemValidationError("PLAN_REMAINING_WINDOW_CAPACITY_INVALID", (
                    f"截至W{week}仍需完成{required}分钟，后续W{week+1}–W{deadline}容量仅{sum(capacities)}分钟。"
                    "请在当前周保留全部行动并合理承接相应成果工作量，或把后续工作提前至当前周；"
                    "准备/提纲若确属该成果累计工作须引用对应工作量承诺，不能重复计数或虚报分钟。",))

    @staticmethod
    def _detail_identity_retry_context(context, outline):
        return PlanContext(requirements=context.requirements, route=context.route,
            source_document=context.source_document, planning_context={**(context.planning_context or {}),
                "detail_identity_contract": outline.get("detail_identity_contract") or {
                    "existing_item_keys": [item["item_key"] for item in outline.get("standing_work_item_registry", [])],
                    "rule": "注册工作项已经存在，详情不能声明它们；保留全部动作并引用原键，work_items允许为空。"}})

    @staticmethod
    def _referenced_action_stages(days, items):
        by_key = {item.item_key: item for item in items}
        return {by_key[key].stage_order for day in days for task in day.tasks
                for key in task.work_item_keys if key in by_key and by_key[key].stage_order is not None}

    def _bind_hybrid_actions(self, plan, result) -> None:
        """Carry author identities through compilation instead of guessing at execution."""
        items = {item.item_key: item for item in result.work_items}
        standing = {item.item_key: item for item in self.session.scalars(
            select(PlanWorkItem).where(PlanWorkItem.plan_version_id == plan.id))}
        labels = {item['label'] for item in plan.commitments_json or []}
        independent = quantified_workloads(plan.commitments_json or [])
        workload_keys = {item['label']: item['workload_key'] for item in plan.commitments_json or []
                         if item.get('workload_key')}
        for key, item in items.items():
            aliases = [old.item_key for old in standing.values()
                       if old.title == item.title and old.binding_mode == str(item.constraint_set.binding_mode)]
            if aliases:
                raise WorkItemValidationError('HYBRID_ACTION_DUPLICATE_OBLIGATION',
                                              (f'工作项 {key} 重复已有义务 {aliases}，请引用原键而非新增身份',))
        for day in result.days:
            for task in day.tasks:
                keys = task.work_item_keys or list(
                    task.scheduling_policy_data.get('work_item_keys') or [])
                # Compatibility only when the author identity is unambiguous.
                if not keys:
                    matches = [key for key, item in items.items()
                               if item.title == task.title]
                    if len(matches) == 1:
                        keys = matches
                if any(key not in items and key not in standing for key in keys):
                    raise WorkItemValidationError('HYBRID_ACTION_IDENTITY_INVALID',
                                                  ('任务引用不存在的工作项: ' + task.title,))
                if not keys:
                    raise WorkItemValidationError('HYBRID_ACTION_IDENTITY_INVALID',
                                                  ('任务缺少明确工作项身份: ' + task.title,))
                for key in keys:
                    related = items.get(key) or standing[key]
                    temporal = (related.constraint_set.temporal.model_dump() if key in items
                                else (related.constraints_json or {}).get('temporal', {}))
                    if ((temporal.get('start_week') is not None and day.week_index < temporal['start_week'])
                            or (temporal.get('end_week') is not None and day.week_index > temporal['end_week'])):
                        raise WorkItemValidationError('HYBRID_ACTION_WINDOW_INVALID',
                            ('引用工作项的周窗口不包含执行周: ' + task.title + ' / ' + key,))
                primary = items.get(keys[0]) or standing[keys[0]]
                temporal = (primary.constraint_set.temporal.model_dump()
                            if keys[0] in items else (primary.constraints_json or {}).get('temporal', {}))
                if ((temporal.get('start_week') is not None and day.week_index < temporal['start_week'])
                        or (temporal.get('end_week') is not None and day.week_index > temporal['end_week'])):
                    raise WorkItemValidationError('HYBRID_ACTION_WINDOW_INVALID',
                                                  ('任务主要身份的周窗口不包含执行周: ' + task.title,))
                gated = []
                for key in keys:
                    item = items.get(key) or standing[key]
                    if key in items:
                        constraints = item.constraint_set
                        is_gated = bool(constraints.selection.group_key) or str(constraints.binding_mode) == 'TRIGGERED'
                    else:
                        is_gated = bool(item.group_id) or item.binding_mode == 'TRIGGERED'
                    if is_gated:
                        gated.append(key)
                # The single gate is authoritative; reordering aliases does
                # not change the action, candidates, or selected membership.
                trigger_gates = [key for key in gated if str((items.get(key) or standing[key]).constraint_set.binding_mode
                    if key in items else standing[key].binding_mode) == 'TRIGGERED']
                if trigger_gates and len(keys) > 1 and keys[0] not in trigger_gates:
                    raise WorkItemValidationError('HYBRID_ACTION_GATE_MIXED', (
                        '普通行动不能混入独立触发工作项以致整项执行被隐藏: ' + task.title +
                        '；已有前提用依赖和stage_contributions表达，不把触发键列为普通动作的完成身份。',))
                if len(gated) == 1 and keys[0] != gated[0]:
                    keys = [gated[0], *[key for key in keys if key != gated[0]]]
                if len(gated) > 1:
                    raise WorkItemValidationError('HYBRID_ACTION_IDENTITY_INVALID',
                                                  ('候选或触发动作必须以唯一受控工作项为主要身份: ' + task.title,))
                if len(set(keys)) != len(keys):
                    raise WorkItemValidationError('HYBRID_ACTION_IDENTITY_INVALID',
                                                  ('任务工作项引用重复: ' + task.title,))
                task.work_item_keys = keys
                refs = list(task.satisfies_commitments)
                allocation = task.scheduling_policy_data.get('workload_minutes')
                if allocation is not None:
                    owned_labels = {label for key in keys for label in (
                        items[key].commitment_refs if key in items else standing[key].commitment_refs_json)}
                    owned_workloads = {workload_keys[label] for label in owned_labels if label in workload_keys}
                    if (not isinstance(allocation, dict) or not allocation
                            or set(allocation) - owned_workloads
                            or any(type(value) is not int or value <= 0 for value in allocation.values())
                            or sum(allocation.values()) > task.estimate_minutes):
                        raise WorkItemValidationError('PLAN_COMMITMENT_MINUTES_INVALID',
                            (f'{task.title}: workload_minutes必须属于引用工作项的已确认成果，且为不超过估时的正整数分配。',),
                            detail={"code": "PLAN_COMMITMENT_MINUTES_INVALID",
                                "task_reference": f'M{day.stage_order}-W{day.week_index}-D{day.day_index}-T{task.order}',
                                "json_path": "/scheduling_policy_data/workload_minutes",
                                "work_item_keys": list(keys),
                                "expected": {"allowed_workload_keys": sorted(owned_workloads),
                                             "positive_integer_minutes": True,
                                             "maximum_allocated_minutes": task.estimate_minutes},
                                "actual": {"allocation": allocation},
                                "diagnostic": [f'{task.title}: 分配键必须属于引用工作项，分钟为正整数且合计不超过{task.estimate_minutes}。']})
                    # Explicit numerical ownership is already model-authored.
                    # Restore redundant labels without guessing from titles or
                    # inheriting unrelated outcome promises.
                    refs += [label for label in workload_keys if label in owned_labels
                             and workload_keys[label] in allocation and label not in refs]
                for key in keys:
                    item = items.get(key) or standing[key]
                    for label in (item.commitment_refs if key in items else item.commitment_refs_json):
                        # A work item may serve multiple final outcomes, but
                        # this action's minutes belong only to the explicitly
                        # declared workload(s). Inheriting all outcome labels
                        # invents double accounting and undoes model repair.
                        if (label not in refs and label not in independent
                                and (allocation is None or label not in workload_keys)):
                            refs.append(label)
                if set(refs) - labels:
                    raise WorkItemValidationError('HYBRID_ACTION_COMMITMENT_INVALID',
                                                  ('任务引用不存在的承诺: ' + task.title,))
                task.satisfies_commitments = refs
                for label in refs:
                    if label in independent:
                        self._task_workload_minutes(task, label, independent)
                task.scheduling_policy_data = {
                    **task.scheduling_policy_data, 'work_item_keys': keys,
                    'stage_contributions': ([item.model_dump(mode="json") for item in task.stage_contributions]
                        if task.stage_contributions else task.scheduling_policy_data.get('stage_contributions', [])),
                    'occurrence_key': task.occurrence_key or task.scheduling_policy_data.get('occurrence_key')
                    or f'W{day.week_index}-D{day.day_index}',
                }

        primary_keys = {task.work_item_keys[0] for day in result.days for task in day.tasks}
        missing_candidates = [key for group in result.work_item_groups for key in group.candidate_item_keys
                              if key in items and str(items[key].constraint_set.binding_mode) != 'ON_DEMAND'
                              and key not in primary_keys]
        if missing_candidates:
            raise WorkItemValidationError('HYBRID_ACTION_CANDIDATE_MISSING',
                                          (f'候选工作项缺少独立动作模板: {missing_candidates}；每项应有以其键为主要身份的任务',))

    def _persist_days(self, plan, milestone_by_order, days) -> int:
        total_minutes = 0
        work_items = {item.item_key: item for item in self.session.scalars(
            select(PlanWorkItem).where(PlanWorkItem.plan_version_id == plan.id))}
        for item in days:
            day = PlanDay(
                plan_version_id=plan.id,
                milestone_id=milestone_by_order[item.stage_order].id,
                stage_order=item.stage_order,
                week_index=item.week_index,
                day_index=item.day_index,
                primary_goal=item.primary_goal,
                suggested_minutes=item.suggested_minutes,
                evidence_json=item.evidence,
                do_not_json=item.do_not,
            )
            self.session.add(day)
            self.session.flush()
            for task in item.tasks:
                total_minutes += task.estimate_minutes
                self.session.add(
                    PlanTask(
                        plan_version_id=plan.id,
                        plan_day_id=day.id,
                        source_work_item_id=(work_items[task.work_item_keys[0]].id
                                             if task.work_item_keys and task.work_item_keys[0] in work_items
                                             else None),
                        origin_stage_order=item.stage_order,
                        lineage_task_id=None,
                        order_in_day=task.order,
                        title=task.title,
                        description=task.description,
                        estimate_minutes=task.estimate_minutes,
                        priority=task.priority,
                        task_kind=task.task_kind,
                        is_required=task.is_required,
                        evidence_json=task.evidence,
                        depends_on_json=task.depends_on,
                        satisfies_commitments_json=task.satisfies_commitments,
                        ai_schedule_requested=False,
                        is_splittable=task.is_splittable,
                        scheduling_policy=task.scheduling_policy,
                        scheduling_policy_json={**task.scheduling_policy_data,
                                                **({'work_item_keys': task.work_item_keys} if task.work_item_keys else {}),
                                                **({'occurrence_key': task.occurrence_key} if task.occurrence_key else {})},
                    )
                )
        self.session.flush()
        return total_minutes

    def _plan_context(
        self,
        requirement,
        route,
        route_stages,
        planning_context: dict[str, object] | None = None,
    ) -> PlanContext:
        return PlanContext(
            requirements=requirement.snapshot_json,
            route={
                "version": route.version,
                "revision": route.revision,
                "planning_mode": route.planning_mode,
                "schedule_type": route.schedule_type,
                "summary": route.summary,
                "stages": [
                    {
                        "order": stage.order,
                        "title": stage.title,
                        "purpose": stage.purpose,
                        "topics": stage.topics_json,
                        "outputs": stage.outputs_json,
                        "depends_on": stage.depends_on_json,
                    }
                    for stage in route_stages
                ],
            },
            planning_context=planning_context,
        )

    def _prepare_plan_research(
        self,
        goal_id: str,
        conversation: Conversation,
        question: str,
        *,
        message_id: str,
        web_research_enabled: bool,
        research_run_id: str | None = None,
        decision_context: dict[str, object] | None = None,
        reuse_latest: bool = True,
    ) -> tuple[dict[str, object] | None, PlanResearchRead | None]:
        """Run optional bounded research without making it a plan prerequisite.

        The research run is persisted before the discussion model is called. A
        provider failure therefore remains visible in the workspace, while the
        existing plan discussion can still answer from its normal context.
        """
        if research_run_id:
            run = self.session.get(ResearchRun, research_run_id)
            if run is None or run.goal_id != goal_id:
                raise HTTPException(status_code=404, detail="Research run not found")
            if run.status in {"QUEUED", "RUNNING", "CANCEL_REQUESTED"}:
                raise HTTPException(status_code=409, detail="Research run is not complete")
        elif not web_research_enabled:
            if not reuse_latest:
                return None, None
            # Reuse goal-scoped research for follow-up plan questions. A new
            # checked run still replaces the active evidence packet.
            run = self.session.scalar(
                select(ResearchRun)
                .where(
                    ResearchRun.goal_id == goal_id,
                    ResearchRun.status.in_({"READY", "PARTIAL", "COMPLETED"}),
                )
                .order_by(ResearchRun.updated_at.desc())
                .limit(1)
            )
        else:
            idempotency_key = f"plan-discussion:{goal_id}:{message_id}"
            run = None
            try:
                run_read = ResearchRunService(self.session).execute(
                    goal_id,
                    question,
                    enabled=True,
                    conversation_id=conversation.id,
                    idempotency_key=idempotency_key,
                    decision_context=decision_context,
                )
                run = self.session.get(ResearchRun, run_read.id)
            except Exception:
                # Research is an optional enhancement. The run service records a
                # failed row when possible; never turn a search outage into a plan
                # discussion outage.
                run = self.session.scalar(
                    select(ResearchRun).where(ResearchRun.idempotency_key == idempotency_key)
                )
                if run is None:
                    return None, None
        if run is None:
            return None, None

        research_read = self._research_read(run)
        packet = None
        payload = run.result_json or run.checkpoint_json or {}
        if payload:
            try:
                packet = ResearchPacket.model_validate(payload)
            except (TypeError, ValueError):
                packet = None
        if packet is None:
            return None, research_read
        conversation_memory = self._research_conversation_memory(conversation.id)
        context = compile_research_context(
            packet,
            max_chars=min(12000, get_settings().context_max_input_chars // 3),
            phase="RECOVERY" if self._active_recovery_context(goal_id) else "PLAN",
            focus=question,
            conversation_memory=conversation_memory,
        )
        return context, research_read

    def _research_decision_context(
        self, goal, requirement, route, plan
    ) -> dict[str, object]:
        """Give the search gate only the compact confirmed context it needs."""
        route_stages = self._route_stages(route.id)
        return {
            "goal": {"title": goal.title[:240], "status": goal.status},
            "confirmed_requirements": requirement.snapshot_json,
            "confirmed_route": self._plan_context(requirement, route, route_stages).route,
            "current_plan": {
                "summary": plan.summary[:500],
                "total_weeks": plan.total_weeks,
                "weekly_execution_days": plan.weekly_execution_days,
                "weekly_minutes": plan.weekly_minutes,
                "milestones": [
                    {
                        "stage_order": item.stage_order,
                        "title": item.title[:160],
                        "purpose": item.purpose[:240],
                    }
                    for item in self._milestones(plan.id)[:8]
                ],
            },
        }

    def _research_conversation_memory(self, conversation_id: str) -> dict[str, object]:
        summary = self.session.scalar(
            select(ConversationSummary)
            .where(
                ConversationSummary.conversation_id == conversation_id,
                ConversationSummary.summary_level == "PHASE",
            )
            .order_by(ConversationSummary.through_message_sequence.desc())
        )
        if summary is None:
            return {}
        return {
            "summary": summary.summary,
            "hard_constraints": list(summary.confirmed_conclusions_json or []),
            "open_questions": list(summary.open_questions_json or []),
            "user_corrections": [
                item.get("change_summary", "")
                for item in (summary.recent_changes_json or [])
                if isinstance(item, dict) and item.get("change_summary")
            ],
        }

    def _latest_research_read(
        self, goal_id: str, conversation_id: str | None
    ) -> PlanResearchRead | None:
        row = self.session.scalar(
            select(ResearchRun)
            .where(
                ResearchRun.goal_id == goal_id,
                ResearchRun.conversation_id == conversation_id if conversation_id else False,
                ResearchRun.status.in_({
                    "READY", "PARTIAL", "COMPLETED", "SKIPPED", "FAILED",
                    "CANCELLED", "DISABLED", "INTERRUPTED",
                }),
            )
            .order_by(ResearchRun.updated_at.desc())
            .limit(1)
        )
        return self._research_read(row) if row else None

    @staticmethod
    def _research_read(row: ResearchRun) -> PlanResearchRead:
        result = row.result_json or row.checkpoint_json or {}
        sources = result.get("sources", []) if isinstance(result, dict) else []
        coverage = result.get("coverage", {}) if isinstance(result, dict) else {}
        unresolved = result.get("uncertain_points", []) if isinstance(result, dict) else []
        claims = result.get("claims", []) if isinstance(result, dict) else []
        bounded_sources = []
        for source in sources[:12] if isinstance(sources, list) else []:
            if not isinstance(source, dict):
                continue
            bounded_sources.append(
                {
                    "title": str(source.get("title", ""))[:160],
                    "url": str(source.get("url", ""))[:1000],
                    "read_status": str(source.get("read_status", ""))[:24],
                    "retrieval_method": str(source.get("retrieval_method", ""))[:40],
                    "ranking_score": source.get("ranking_score", 0),
                    "evidence_level": source.get("evidence_level", "SEARCH_SNIPPET"),
                    "snippet": str(source.get("snippet", ""))[:500],
                    "excerpt": str(source.get("content_excerpt", ""))[:1000],
                }
            )
        return PlanResearchRead(
            id=row.id,
            status=row.status,
            current_stage=row.current_stage,
            question=row.question[:500],
            stop_reason=row.stop_reason,
            error_code=row.error_code,
            error_message=row.error_message[:500] if row.error_message else None,
            result_status=(
                str(result.get("status"))
                if isinstance(result, dict) and result.get("status")
                else None
            ),
            source_count=len(sources) if isinstance(sources, list) else 0,
            read_pages=int(coverage.get("read_pages", 0) or 0) if isinstance(coverage, dict) else 0,
            unresolved_points=[str(item)[:300] for item in unresolved[:10]]
            if isinstance(unresolved, list)
            else [],
            sources=bounded_sources,
            claims=[
                {
                    "claim": str(item.get("claim", ""))[:500],
                    "status": str(item.get("status", "UNVERIFIED"))[:32],
                    "evidence_level": str(item.get("evidence_level", "UNVERIFIED"))[:32],
                    "source_urls": [str(url)[:2000] for url in item.get("source_urls", [])[:4]],
                }
                for item in claims[:12]
                if isinstance(item, dict)
            ],
            coverage=coverage if isinstance(coverage, dict) else {},
        )

    def _discussion_context(
        self,
        requirement,
        route,
        plan,
        user_input: str,
        previous_adjustment: PlanAdjustmentProposal | None = None,
        application_requested_weeks: list[int] | None = None,
        application_outline: dict[str, object] | None = None,
        recovery_context: dict[str, object] | None = None,
        orchestration: dict[str, object] | None = None,
        research_context: dict[str, object] | None = None,
        material_context: dict[str, object] | None = None,
    ) -> PlanDiscussionContext:
        route_stages = self._route_stages(route.id)
        current_plan = self._plan_payload(plan)
        if plan.execution_model_version == ExecutionModelVersion.HYBRID_WORK_ITEM:
            plan_read = self._plan_read(plan)
            current_plan["execution_model_version"] = plan.execution_model_version
            current_plan["work_item_groups"] = [
                item.model_dump(mode="json") for item in plan_read.work_item_groups
            ]
            current_plan["work_items"] = [
                item.model_dump(mode="json") for item in plan_read.work_items
            ]
        locked_coordinates = self._locked_execution_coordinates(plan)
        activation = self.session.scalar(
            select(PlanActivation).where(PlanActivation.plan_version_id == plan.id)
        )
        scheduled_dates = (
            {
                row.plan_day_id: row.scheduled_date.isoformat()
                for row in self.session.scalars(
                    select(PlanDaySchedule).where(PlanDaySchedule.activation_id == activation.id)
                )
            }
            if activation
            else {}
        )
        current_plan["execution_preservation_bounds"] = {
            "minimum_total_weeks": max((w for w, _ in locked_coordinates), default=0),
            "minimum_weekly_execution_days": max((d for _, d in locked_coordinates), default=0),
            "rule": "保留已有日期位置；减少未来频次时可留空或安排休息，不删除执行历史。",
        }
        current_plan["execution_state"] = [
            {
                "week_index": day["week_index"],
                "day_index": day["day_index"],
                "scheduled_date": scheduled_dates.get(day["id"]),
                "task_statuses": [task["status"] for task in day["tasks"]],
                "locked": (day["week_index"], day["day_index"]) in locked_coordinates,
            }
            for day in self._plan_read(plan).model_dump(mode="json")["days"]
        ]
        if application_requested_weeks:
            requested = set(application_requested_weeks)
            # 逐周生成时不要把完整的六周任务树重复发送给模型。
            # 保留当前周与已锁定坐标，显著降低输入 token 和推理耗时。
            current_plan["days"] = [
                day
                for day in current_plan.get("days", [])
                if day.get("week_index") in requested
                or (day.get("week_index"), day.get("day_index")) in locked_coordinates
            ]
        goal = self.session.get(Goal, plan.goal_id)
        if goal is None:
            raise HTTPException(status_code=404, detail="Goal not found")
        locked_day_ids, locked_task_ids = self._locked_execution_ids(goal.id, plan.id)
        planning_context = build_planning_context_snapshot(
            self.session,
            goal=goal,
            requirement=requirement,
            route=route,
            route_stages=route_stages,
            plan=plan,
            milestones=self._milestones(plan.id),
            locked_day_ids=locked_day_ids,
            locked_task_ids=locked_task_ids,
        )
        if orchestration is None and recovery_context:
            candidate = recovery_context.get("_agent_orchestration")
            if isinstance(candidate, dict):
                orchestration = candidate
        if orchestration:
            planning_context = {**planning_context, "agent_orchestration": orchestration}
        capacity_conflict = self._capacity_negotiation(goal.id, plan)
        if capacity_conflict:
            from planner_api.models import FixedActivityMovePreview
            activity_moves = list(self.session.scalars(select(FixedActivityMovePreview).where(
                FixedActivityMovePreview.goal_id == goal.id,
                FixedActivityMovePreview.status == "APPLIED").order_by(FixedActivityMovePreview.applied_at.desc()).limit(8)))
            planning_context = {**planning_context, "capacity_negotiation": capacity_conflict,
                                "confirmed_activity_moves": [item.preview_json for item in activity_moves],
                                "negotiation_history": [
                                    {"role": item.role, "content": item.content}
                                    for item in self._conversation_messages(
                                        self._ensure_plan_conversation(goal.id).id)[-12:]
                                ]}
        return PlanDiscussionContext(
            requirements=requirement.snapshot_json,
            route=self._plan_context(requirement, route, route_stages).route,
            current_plan=current_plan,
            user_input=user_input,
            planning_context=planning_context,
            previous_adjustment=(
                previous_adjustment.model_dump(mode="json") if previous_adjustment else None
            ),
            application_requested_weeks=application_requested_weeks,
            application_outline=application_outline,
            recovery_context=recovery_context,
            research_context=research_context,
            material_context=material_context,
        )

    def _locked_execution_coordinates(self, plan: PlanVersion) -> set[tuple[int, int]]:
        activation = self.session.scalar(
            select(PlanActivation).where(PlanActivation.plan_version_id == plan.id)
        )
        if activation is None:
            return set()
        days = list(self.session.scalars(select(PlanDay).where(PlanDay.plan_version_id == plan.id)))
        day_by_id = {day.id: day for day in days}
        schedules = list(
            self.session.scalars(
                select(PlanDaySchedule).where(PlanDaySchedule.activation_id == activation.id)
            )
        )
        locked: set[tuple[int, int]] = set()
        recorded_days, recorded_tasks = self._locked_execution_ids(plan.goal_id, plan.id)
        recorded_day_ids = set(recorded_days)
        recorded_day_ids.update(
            self.session.scalars(
                select(PlanTask.plan_day_id).where(PlanTask.id.in_(recorded_tasks))
            )
        )
        for schedule in schedules:
            day = day_by_id.get(schedule.plan_day_id)
            if day is None:
                continue
            coordinate = (day.week_index, day.day_index)
            has_progress = self.session.scalar(
                select(PlanTask.id).where(
                    PlanTask.plan_day_id == day.id,
                    PlanTask.status.in_({"IN_PROGRESS", "COMPLETED", "CANCELLED", "CARRIED_OVER"}),
                )
            )
            if has_progress or day.id in recorded_day_ids:
                locked.add(coordinate)
        return locked

    def _validate_execution_bounds(self, current, total_weeks: int, weekly_execution_days: int):
        protected = self._locked_execution_coordinates(current)
        outside = sorted(
            coordinate
            for coordinate in protected
            if coordinate[0] > total_weeks or coordinate[1] > weekly_execution_days
        )
        if outside:
            raise ProviderUnavailableError(
                "缩短周期或减少执行日会删除已有执行记录，当前计划保持不变。"
                "可以减少未来的实际任务次数，但须保留已有记录所在的日期位置。",
                diagnostic=(
                    f"protected_coordinates_outside_bounds={outside}; "
                    f"minimum_total_weeks={max(w for w, _ in protected)}; "
                    f"minimum_weekly_execution_days={max(d for _, d in protected)}"
                ),
                code="EXECUTION_HISTORY_OUT_OF_BOUNDS",
            )

    def _build_recovery_context(
        self,
        goal_id: str,
        missed_dates: list[date],
        backlog_minutes: int,
        original_end_date: date,
        include_non_execution_days: bool,
        selected_task_ids: list[str] | None = None,
    ) -> dict[str, object]:
        """Build deterministic recovery facts before asking the model to reason.

        The model receives a compact ledger and a ranked local window instead of
        having to infer backlog ownership and calendar capacity from the whole
        plan.  The soft target is advisory only; it is deliberately not a hard
        daily limit.
        """
        plan = self._current_plan(goal_id)
        today = self._today_for_goal_id(goal_id)
        if plan is None:
            return {
                "missed_dates": [item.isoformat() for item in missed_dates],
                "backlog_minutes": backlog_minutes,
                "original_end_date": original_end_date.isoformat(),
                "include_non_execution_days": include_non_execution_days,
                "recommended_local_weeks": [],
                "candidate_days": [],
                "backlog_tasks": [],
            }
        activation = self.session.scalar(
            select(PlanActivation).where(PlanActivation.plan_version_id == plan.id)
        )
        days = list(self.session.scalars(select(PlanDay).where(PlanDay.plan_version_id == plan.id)))
        schedules = list(
            self.session.scalars(
                select(PlanDaySchedule).where(PlanDaySchedule.activation_id == activation.id)
                if activation is not None
                else select(PlanDaySchedule).where(PlanDaySchedule.id == "__none__")
            )
        )
        schedule_by_day = {item.plan_day_id: item.scheduled_date for item in schedules}
        backlog_tasks: list[dict[str, object]] = []
        selected_ids = set(selected_task_ids) if selected_task_ids is not None else None
        for day in days:
            scheduled = schedule_by_day.get(day.id)
            if scheduled is None or scheduled >= today:
                continue
            for task in self.session.scalars(
                select(PlanTask)
                .where(PlanTask.plan_day_id == day.id)
                .order_by(PlanTask.order_in_day)
            ):
                if task.status in {"COMPLETED", "CANCELLED", "CARRIED_OVER"}:
                    continue
                if selected_ids is not None and task.id not in selected_ids:
                    continue
                backlog_tasks.append(
                    {
                        "task_id": task.id,
                        "source_date": scheduled.isoformat(),
                        "source_week": day.week_index,
                        "source_day": day.day_index,
                        "order": task.order_in_day,
                        "title": task.title,
                        "estimate_minutes": task.estimate_minutes,
                        "priority": task.priority,
                        "task_kind": task.task_kind,
                        "is_required": task.is_required,
                        "depends_on": task.depends_on_json,
                    }
                )
        dependency_allocator = DayAllocator(self.session)
        dependency_indexes = dependency_allocator._dependency_indexes(plan.id)
        for item in backlog_tasks:
            task = self.session.get(PlanTask, str(item["task_id"]))
            constraints: list[dict[str, object]] = []
            if task is not None:
                for dependency in task.depends_on_json or []:
                    resolved = dependency_allocator._resolve_dependency(
                        dependency, task, dependency_indexes
                    )
                    if resolved is None:
                        constraints.append(
                            {"reference": str(dependency), "task_id": None, "status": "UNKNOWN"}
                        )
                    else:
                        constraints.append(
                            {
                                "reference": str(dependency),
                                "task_id": resolved.id,
                                "status": resolved.status,
                            }
                        )
            item["dependency_constraints"] = constraints
        if not backlog_tasks and backlog_minutes:
            backlog_minutes = 0
        candidate_days: list[dict[str, object]] = []
        for day in days:
            scheduled = schedule_by_day.get(day.id)
            if scheduled is None or scheduled < today:
                continue
            if scheduled > original_end_date + timedelta(days=RECOVERY_END_DATE_GRACE_DAYS):
                continue
            tasks = list(
                self.session.scalars(select(PlanTask).where(PlanTask.plan_day_id == day.id))
            )
            existing_minutes = sum(task.estimate_minutes for task in tasks)
            candidate_days.append(
                {
                    "plan_day_id": day.id,
                    "date": scheduled.isoformat(),
                    "week_index": day.week_index,
                    "day_index": day.day_index,
                    "stage_order": day.stage_order,
                    "existing_minutes": existing_minutes,
                    "existing_task_count": len(tasks),
                    "primary_goal": day.primary_goal,
                    "original_tasks": [
                        {
                            "task_id": task.id,
                            "title": task.title,
                            "estimate_minutes": task.estimate_minutes,
                            "priority": task.priority,
                            "order": task.order_in_day,
                            "status": task.status,
                            # Work already started, completed, or scheduled for
                            # today is locked. Only untouched future work may
                            # be pushed by recovery.
                            "movable": (scheduled > today and task.status == "NOT_STARTED"),
                        }
                        for task in sorted(tasks, key=lambda item: item.order_in_day)
                    ],
                }
            )
        candidate_days.sort(key=lambda item: (item["date"], item["week_index"], item["day_index"]))
        local_window_days = self._recovery_local_window_days(len(candidate_days))
        local_candidates = candidate_days[:local_window_days]
        if local_candidates:
            average_load = sum(int(item["existing_minutes"]) for item in local_candidates) / len(
                local_candidates
            )
        else:
            average_load = 0
        soft_target = max(int(round(average_load * 1.5)), int(average_load) + 30, 60)
        recommended = local_candidates
        projected_loads = {str(item["date"]): int(item["existing_minutes"]) for item in recommended}
        proposed_assignments: list[dict[str, object]] = []
        local_capacity = sum(
            max(0, soft_target - int(item["existing_minutes"])) for item in recommended
        )
        backlog_task_minutes = sum(int(item["estimate_minutes"]) for item in backlog_tasks)
        backlog_by_id = {str(item["task_id"]): item for item in backlog_tasks}
        dependency_order: list[dict[str, object]] = []
        remaining_ids = set(backlog_by_id)
        while remaining_ids:
            eligible = []
            for task_id in remaining_ids:
                constraints = backlog_by_id[task_id].get("dependency_constraints", [])
                ready = all(
                    isinstance(item, dict)
                    and (
                        str(item.get("status")) in {"COMPLETED", "CARRIED_OVER"}
                        or str(item.get("task_id"))
                        in {str(value["task_id"]) for value in dependency_order}
                    )
                    for item in constraints
                )
                if ready:
                    eligible.append(backlog_by_id[task_id])
            if not eligible:
                break
            next_task = min(
                eligible,
                key=lambda item: (
                    -int(item["priority"]),
                    str(item["source_date"]),
                    int(item["order"]),
                ),
            )
            dependency_order.append(next_task)
            remaining_ids.remove(str(next_task["task_id"]))
        dependencies_resolved = len(dependency_order) == len(backlog_tasks)
        ai_required = (
            not bool(recommended)
            or local_capacity < backlog_task_minutes
            or not dependencies_resolved
        )
        direct_tasks = [] if ai_required else dependency_order
        proposed_assignments = self._ordered_balanced_recovery_assignments(
            direct_tasks, recommended
        )
        for assignment in proposed_assignments:
            projected_loads[str(assignment["target_date"])] += int(assignment["estimate_minutes"])
        projected_peak = max(projected_loads.values(), default=0)
        context = {
            "today": today.isoformat(),
            "missed_dates": [item.isoformat() for item in missed_dates],
            "backlog_minutes": backlog_minutes,
            "backlog_task_minutes": sum(int(item["estimate_minutes"]) for item in backlog_tasks),
            "original_end_date": original_end_date.isoformat(),
            "allowed_end_date": (
                original_end_date + timedelta(days=RECOVERY_END_DATE_GRACE_DAYS)
            ).isoformat(),
            "include_non_execution_days": include_non_execution_days,
            "soft_load_reference_minutes": soft_target,
            "backlog_tasks": backlog_tasks,
            "candidate_days": candidate_days,
            "recommended_local_days": recommended,
            "allocation_candidate_days": recommended,
            "recommended_local_weeks": sorted({int(item["week_index"]) for item in recommended}),
            "local_window_ratio": RECOVERY_LOCAL_WINDOW_RATIO,
            "local_window_execution_days": local_window_days,
            "local_window_max_execution_days": RECOVERY_LOCAL_WINDOW_MAX_DAYS,
            "proposed_assignments": proposed_assignments,
            "projected_peak_daily_minutes": projected_peak,
            "local_capacity_minutes": local_capacity,
            "ai_required": ai_required,
            "preserve_future_schedule": True,
            "escalate_only_if_local_window_insufficient": True,
        }
        if proposed_assignments:
            context |= self._plan_recovery_displacement(
                context,
                proposed_assignments,
                soft_target,
            )
        return context

    def _pending_past_weeks(self, plan: PlanVersion) -> set[int]:
        """Return weeks with required work left on an expired execution day."""
        activation = self.session.scalar(
            select(PlanActivation).where(PlanActivation.plan_version_id == plan.id)
        )
        if activation is None:
            return set()
        days = {
            item.id: item
            for item in self.session.scalars(
                select(PlanDay).where(PlanDay.plan_version_id == plan.id)
            )
        }
        today = self._today_for_goal_id(plan.goal_id)
        weeks: set[int] = set()
        for schedule in self.session.scalars(
            select(PlanDaySchedule).where(PlanDaySchedule.activation_id == activation.id)
        ):
            if schedule.scheduled_date >= today:
                continue
            day = days.get(schedule.plan_day_id)
            if day is None:
                continue
            pending = self.session.scalar(
                select(PlanTask.id).where(
                    PlanTask.plan_day_id == day.id,
                    PlanTask.is_required.is_(True),
                    PlanTask.status.not_in(("COMPLETED", "CANCELLED")),
                )
            )
            if pending:
                weeks.add(day.week_index)
        return weeks

    @staticmethod
    def _adjustment_outline(result: PlanAdjustmentApplyResult) -> dict[str, object]:
        return {
            "summary": result.summary,
            "total_weeks": result.total_weeks,
            "weekly_execution_days": result.weekly_execution_days,
            "weekly_minutes": result.weekly_minutes,
            "temporal_segments": (
                [item.model_dump(mode="json") for item in result.temporal_segments]
                if result.temporal_segments is not None
                else None
            ),
            "commitments": (
                [item.model_dump(mode="json") for item in result.commitments]
                if result.commitments is not None
                else None
            ),
            "milestones": [item.model_dump(mode="json") for item in result.milestones],
            "buffer_strategy": result.buffer_strategy,
            "assumptions": result.assumptions,
            "open_questions": result.open_questions,
        }

    @staticmethod
    def _scoped_recovery_result(
        payload: dict[str, object], weeks: list[int]
    ) -> PlanAdjustmentApplyResult:
        """Normalize legacy/full staged output to the exact recovery week scope."""
        data = dict(payload)
        data["regenerated_weeks"] = sorted(set(weeks))
        raw_days = data.get("replacement_days", [])
        data["replacement_days"] = [
            day
            for day in raw_days
            if isinstance(day, dict) and int(day.get("week_index", 0)) in weeks
        ]
        return PlanAdjustmentApplyResult.model_validate(data)

    @staticmethod
    def _validate_adjustment_batch(
        result: PlanAdjustmentApplyResult,
        requested_weeks: list[int],
        expected_total_weeks: int,
    ) -> None:
        if result.regenerated_weeks != requested_weeks:
            raise ProviderUnavailableError(
                "这次没有正确覆盖需要调整的周，当前安排保持不变。",
                diagnostic=(f"expected={requested_weeks}, returned={result.regenerated_weeks}"),
            )
        if result.total_weeks != expected_total_weeks:
            raise ProviderUnavailableError(
                "这次调整的总周期与确认内容不一致，当前安排保持不变。",
                diagnostic=(
                    f"expected_total={expected_total_weeks}, returned={result.total_weeks}"
                ),
            )

    def _validate_future_replacement_days(self, current, result) -> None:
        today = self._today_for_goal_id(current.goal_id)
        activation = self.session.scalar(
            select(PlanActivation).where(PlanActivation.plan_version_id == current.id)
        )
        if activation is None:
            return
        day_ids = {
            (d.week_index, d.day_index): d.id
            for d in self.session.scalars(
                select(PlanDay).where(PlanDay.plan_version_id == current.id)
            )
        }
        dates = {
            x.plan_day_id: x.scheduled_date
            for x in self.session.scalars(
                select(PlanDaySchedule).where(PlanDaySchedule.activation_id == activation.id)
            )
        }
        past = [
            (d.week_index, d.day_index)
            for d in result.replacement_days
            if day_ids.get((d.week_index, d.day_index))
            and dates.get(day_ids[(d.week_index, d.day_index)]) < today
        ]
        if past:
            raise ProviderUnavailableError(
                "调整方案包含已经过去的执行日，无法应用；请重新生成当前及未来安排。",
                diagnostic=f"past_replacement_coordinates={past}",
                code="PAST_RECOVERY_DAYS",
            )

    def _validate_locked_days_omitted(
        self,
        current: PlanVersion,
        result: PlanAdjustmentApplyResult,
    ) -> None:
        locked = self._locked_execution_coordinates(current)
        returned = {(day.week_index, day.day_index) for day in result.replacement_days}
        overlap = sorted(locked & returned)
        if overlap:
            raise ProviderUnavailableError(
                "这次调整碰到了已经完成的内容，因此没有采用；当前安排仍然保留。",
                diagnostic=f"locked_coordinates_returned={overlap}",
            )

    def _plan_payload(self, plan: PlanVersion) -> dict[str, object]:
        value = self._plan_read(plan)
        payload = {
            "assistant_message": "已经准备好当前的时间安排。",
            "summary": value.summary,
            "total_weeks": value.total_weeks,
            "weekly_execution_days": value.weekly_execution_days,
            "weekly_minutes": value.weekly_minutes,
            "temporal_segments": [item.model_dump(mode="json") for item in value.temporal_segments],
            "commitments": [item.model_dump(mode="json") for item in value.commitments],
            "milestones": [
                {
                    "stage_order": item.stage_order,
                    "title": item.title,
                    "purpose": item.purpose,
                    "start_week": item.start_week,
                    "end_week": item.end_week,
                    "outputs": item.outputs,
                }
                for item in value.milestones
            ],
            "days": [
                {
                    "stage_order": day.stage_order,
                    "week_index": day.week_index,
                    "day_index": day.day_index,
                    "primary_goal": day.primary_goal,
                    "suggested_minutes": day.suggested_minutes,
                    "tasks": [
                        {
                            "order": task.order,
                            "title": task.title,
                            "description": task.description,
                            "estimate_minutes": task.estimate_minutes,
                            "priority": task.priority,
                            "task_kind": task.task_kind,
                            "is_required": task.is_required,
                            "evidence": task.evidence,
                            "depends_on": task.depends_on,
                            "satisfies_commitments": task.satisfies_commitments,
                            "is_splittable": task.is_splittable,
                            "scheduling_policy": task.scheduling_policy,
                            "scheduling_policy_data": task.scheduling_policy_data,
                            "work_item_keys": task.scheduling_policy_data.get("work_item_keys", []),
                            "occurrence_key": task.scheduling_policy_data.get("occurrence_key"),
                        }
                        for task in day.tasks
                    ],
                    "evidence": day.evidence,
                    "do_not": day.do_not,
                }
                for day in value.days
            ],
            "buffer_strategy": value.buffer_strategy,
            "assumptions": value.assumptions,
            "open_questions": value.open_questions,
            "change_summary": value.change_summary,
        }
        self._canonicalize_persisted_dependencies(payload)
        return payload

    @staticmethod
    def _canonicalize_persisted_dependencies(payload: dict[str, object]) -> None:
        """Convert legacy persisted dependency labels to revision-safe references.

        Early plan versions stored dependency titles and labels such as ``任务2``.
        New revision schemas deliberately accept only canonical ``M-W-D-T``
        references.  This boundary keeps old plans readable and editable while
        avoiding guesses for ambiguous references: only exact or uniquely
        identifiable earlier tasks are retained.
        """
        canonical = re.compile(r"^M(\d+)-W(\d+)-D(\d+)-T(\d+)$", re.IGNORECASE)
        day_pattern = re.compile(r"(?:第\s*)?(\d+)\s*天(?:的)?(?:任务|task)\s*(\d+)", re.IGNORECASE)
        task_pattern = re.compile(r"^(?:任务|task)\s*(\d+)$", re.IGNORECASE)

        days = payload.get("days")
        if not isinstance(days, list):
            return

        records: list[dict[str, object]] = []
        by_ref: dict[str, dict[str, object]] = {}
        for day in days:
            if not isinstance(day, dict):
                continue
            stage = int(day.get("stage_order") or 1)
            week = int(day.get("week_index") or 1)
            day_index = int(day.get("day_index") or 1)
            tasks = day.get("tasks")
            if not isinstance(tasks, list):
                continue
            for task in tasks:
                if not isinstance(task, dict):
                    continue
                order = int(task.get("order") or 0)
                if order < 1:
                    continue
                ref = f"M{stage}-W{week}-D{day_index}-T{order}"
                record = {
                    "ref": ref,
                    "stage": stage,
                    "week": week,
                    "day": day_index,
                    "order": order,
                    "title": str(task.get("title") or "").strip(),
                    "evidence": [str(item).strip() for item in (task.get("evidence") or [])],
                }
                records.append(record)
                by_ref[ref.upper()] = record

        def earlier(target: dict[str, object], current: dict[str, object]) -> bool:
            target_coord = (int(target["week"]), int(target["day"]), int(target["order"]))
            current_coord = (int(current["week"]), int(current["day"]), int(current["order"]))
            return target_coord < current_coord and int(target["stage"]) <= int(current["stage"])

        for day in days:
            if not isinstance(day, dict):
                continue
            stage = int(day.get("stage_order") or 1)
            week = int(day.get("week_index") or 1)
            day_index = int(day.get("day_index") or 1)
            tasks = day.get("tasks")
            if not isinstance(tasks, list):
                continue
            for task in tasks:
                if not isinstance(task, dict):
                    continue
                order = int(task.get("order") or 0)
                current = {
                    "stage": stage,
                    "week": week,
                    "day": day_index,
                    "order": order,
                }
                raw_dependencies = task.get("depends_on") or []
                if isinstance(raw_dependencies, str):
                    raw_dependencies = [raw_dependencies]
                if not isinstance(raw_dependencies, list):
                    task["depends_on"] = []
                    continue
                resolved: list[str] = []
                for raw in raw_dependencies:
                    label = str(raw or "").strip()
                    if not label:
                        continue
                    target: dict[str, object] | None = None
                    match = canonical.fullmatch(label)
                    if match:
                        normalized = (
                            f"M{int(match.group(1))}-W{int(match.group(2))}-"
                            f"D{int(match.group(3))}-T{int(match.group(4))}"
                        )
                        target = by_ref.get(normalized.upper())
                    else:
                        match = day_pattern.search(label)
                        if match:
                            target_day, target_order = int(match.group(1)), int(match.group(2))
                            candidates = [
                                item for item in records
                                if int(item["stage"]) == stage
                                and int(item["week"]) == week
                                and int(item["day"]) == target_day
                                and int(item["order"]) == target_order
                            ]
                            if len(candidates) == 1:
                                target = candidates[0]
                        else:
                            match = task_pattern.fullmatch(label)
                            if match:
                                target_order = int(match.group(1))
                                candidates = [
                                    item for item in records
                                    if int(item["stage"]) == stage
                                    and int(item["week"]) == week
                                    and int(item["day"]) == day_index
                                    and int(item["order"]) == target_order
                                ]
                                if len(candidates) == 1:
                                    target = candidates[0]

                        if target is None:
                            normalized_label = label.casefold()
                            candidates = [
                                item for item in records
                                if earlier(item, current)
                                and (
                                    str(item["title"]).casefold() == normalized_label
                                    or normalized_label in {
                                        str(evidence).casefold() for evidence in item["evidence"]
                                    }
                                )
                            ]
                            if len(candidates) == 1:
                                target = candidates[0]

                    if target is not None and earlier(target, current):
                        ref = str(target["ref"])
                        if ref not in resolved:
                            resolved.append(ref)
                task["depends_on"] = resolved

    @staticmethod
    def _outline_payload(plan, milestones) -> dict[str, object]:
        return {
            "summary": plan.summary,
            "total_weeks": plan.total_weeks,
            "weekly_execution_days": plan.weekly_execution_days,
            "weekly_minutes": plan.weekly_minutes,
            "temporal_segments": list(plan.temporal_segments_json),
            "commitments": list(plan.commitments_json),
            "source_document": plan.source_document,
            "compiler_report": dict(plan.compiler_report_json),
            "milestones": [
                {
                    "stage_order": item.stage_order,
                    "title": item.title,
                    "purpose": item.purpose,
                    "start_week": item.start_week,
                    "end_week": item.end_week,
                    "outputs": item.outputs_json,
                }
                for item in milestones
            ],
        }

    def _generated_task_registry(self, plan_id: str) -> list[dict[str, object]]:
        rows = self.session.execute(
            select(PlanTask, PlanDay)
            .join(PlanDay, PlanDay.id == PlanTask.plan_day_id)
            .where(PlanTask.plan_version_id == plan_id)
            .order_by(PlanDay.week_index, PlanDay.day_index, PlanTask.order_in_day)
        ).all()
        return [
            {
                "title": task.title,
                "description": task.description,
                "work_item_keys": (task.scheduling_policy_json or {}).get("work_item_keys", []),
                "occurrence_key": (task.scheduling_policy_json or {}).get("occurrence_key"),
                "dependency_period_key": (task.scheduling_policy_json or {}).get("dependency_period_key") or f"week:{day.week_index}",
                "reference": (
                    f"M{day.stage_order}-W{day.week_index}-D{day.day_index}-T{task.order_in_day}"
                ),
            }
            for task, day in rows
        ]

    @staticmethod
    def _generation_failure_message(
        error_code: str | None,
        diagnostic: str | None,
        fallback: str | None,
    ) -> str | None:
        if error_code == "MODEL_REQUEST_REJECTED" and "HTTP 402" in (diagnostic or ""):
            return (
                "模型服务拒绝了请求（HTTP 402，通常是余额或额度不足）。"
                "已生成内容全部保留；检查模型服务额度后继续，只会补齐缺失部分。"
            )
        if error_code in {
            "PLAN_TEMPORAL_SEGMENT_MINUTES_INVALID",
            "PLAN_TEMPORAL_SEGMENT_CAPACITY_INVALID",
        }:
            values = dict(re.findall(
                r"\b(week|weekly_load|weekly_capacity|normal_limit|exception_limit)=(\d+)",
                diagnostic or "",
            ))
            subject = f"第 {values['week']} 周的草稿" if "week" in values else "这一周的草稿"
            reason = "任务量仍超过可用时间。"
            if "weekly_load" in values and "weekly_capacity" in values:
                reason = (f"预计需要 {values['weekly_load']} 分钟，"
                          f"规划中的可用时间为 {values['weekly_capacity']} 分钟，"
                          "仍未通过每周总量或单日时长校验。")
            return (
                f"自动调整后，{subject}{reason}"
                "确认需求、已完成周和设计稿均已保留。可以继续自动修正，"
                "或复核需求、路线中的时间与任务量；系统不会自行放宽限制。"
            )
        return fallback

    @staticmethod
    def _raise_generation_failure(error, phase: str) -> None:
        message = PlanService._generation_failure_message(
            error.code,
            error.diagnostic,
            str(error),
        )
        raise HTTPException(
            status_code=503,
            detail={
                "message": message,
                "code": f"PLAN_{phase}_GENERATION_FAILED",
                "provider_code": error.code,
                "diagnostic": error.diagnostic,
            },
        ) from None

    def _get_inputs(self, goal_id: str) -> tuple[Goal, RequirementVersion, RouteVersion]:
        goal = self.session.get(Goal, goal_id)
        if goal is None:
            raise HTTPException(status_code=404, detail="Goal not found")
        if not goal.current_requirement_version_id or not goal.active_route_version_id:
            raise HTTPException(status_code=409, detail="Confirmed route is required")
        requirement = self.session.get(RequirementVersion, goal.current_requirement_version_id)
        route = self.session.get(RouteVersion, goal.active_route_version_id)
        if requirement is None or requirement.status != "CONFIRMED":
            raise HTTPException(status_code=409, detail="Confirmed requirements are required")
        if route is None or route.status != "CONFIRMED":
            raise HTTPException(status_code=409, detail="Confirmed route is required")
        # Deterministic input errors must fail before any expensive generation,
        # rather than spending multiple model repairs on an unresolved deadline.
        try:
            snapshot_deadline(requirement.snapshot_json, timezone=goal.timezone, require_resolved=True)
        except ValueError as error:
            raise HTTPException(409, detail={
                "code": "PLAN_DEADLINE_UNRESOLVED", "message": str(error), "retryable": False,
            }) from error
        return goal, requirement, route

    def _failed_hybrid_draft_can_be_revised(self, goal_id, plan) -> bool:
        if not plan or plan.execution_model_version != ExecutionModelVersion.HYBRID_WORK_ITEM:
            return False
        batches = list(self.session.scalars(select(GenerationBatch).where(
            GenerationBatch.plan_version_id == plan.id)))
        return (any(batch.status == "FAILED" for batch in batches)
                and not any(batch.status in {"PENDING", "RUNNING", "PROCESSING"} for batch in batches))

    def _get_review_inputs(self, goal_id: str) -> tuple[Goal, RequirementVersion, RouteVersion]:
        goal, requirement, route = self._get_inputs(goal_id)
        if goal.status not in {
            "PLAN_REVIEW",
            "READY_TO_START",
            "ACTIVE",
            "RECOVERY_REQUIRED",
        } and not (goal.status == "PLAN_DRAFTING"
                   and (self._capacity_negotiation(goal_id, self._current_plan(goal_id))
                        or self._failed_hybrid_draft_can_be_revised(goal_id, self._current_plan(goal_id)))):
            raise HTTPException(status_code=409, detail="Plan is not awaiting review")
        return goal, requirement, route

    def _require_review_plan(self, goal_id: str) -> PlanVersion:
        plan = self._current_plan(goal_id)
        goal = self.session.get(Goal, goal_id)
        allowed_statuses = (
            {"DRAFT", "CONFIRMED"}
            if goal and goal.status in {"READY_TO_START", "ACTIVE", "RECOVERY_REQUIRED"}
            else {"DRAFT"}
        )
        if goal and goal.status == "PLAN_DRAFTING" and (
                self._capacity_negotiation(goal_id, plan)
                or self._failed_hybrid_draft_can_be_revised(goal_id, plan)):
            allowed_statuses.add("GENERATING")
        if plan is None or plan.status not in allowed_statuses:
            raise HTTPException(status_code=409, detail="No complete plan draft exists")
        return plan

    def _migrate_active_execution(
        self,
        goal: Goal,
        source: PlanVersion,
        revised: PlanVersion,
        recovery_context: dict[str, object] | None = None,
    ) -> None:
        old_activation = self.session.scalar(
            select(PlanActivation).where(PlanActivation.plan_version_id == source.id)
        )
        if old_activation is None:
            raise ProviderUnavailableError(
                "暂时没有找到当前日历，调整没有应用，原来的安排仍然保留。"
            )

        self._validate_execution_bounds(source, revised.total_weeks, revised.weekly_execution_days)
        protected = self._locked_execution_coordinates(source)
        old_payload = {
            (day["week_index"], day["day_index"]): day for day in self._plan_payload(source)["days"]
        }
        new_payload = {
            (day["week_index"], day["day_index"]): day
            for day in self._plan_payload(revised)["days"]
        }
        for coordinate in protected:
            original = dict(old_payload[coordinate])
            replacement = dict(new_payload.get(coordinate, {}))
            # Capacity labels may be repaired, but recorded actions cannot change.
            original.pop("suggested_minutes", None)
            replacement.pop("suggested_minutes", None)
            # Recovery versions created before ordinal allocation was flushed
            # can contain several tasks numbered 1. Revision schemas repair
            # those display positions; compare the same ordered actions while
            # continuing to protect every other task/day field.
            for payload in (original, replacement):
                payload["tasks"] = [dict(task, order=index) for index, task in
                                    enumerate(payload.get("tasks", []), start=1)]
            if original != replacement:
                raise ProviderUnavailableError(
                    "调整会改变已有执行记录，未发布新安排，原计划保持不变。",
                    diagnostic=f"changed_protected_coordinate={coordinate}",
                    code="EXECUTION_HISTORY_CHANGED",
                )

        old_days = list(
            self.session.scalars(select(PlanDay).where(PlanDay.plan_version_id == source.id))
        )
        new_days = list(
            self.session.scalars(select(PlanDay).where(PlanDay.plan_version_id == revised.id))
        )
        old_by_coordinate = {(item.week_index, item.day_index): item for item in old_days}
        new_by_coordinate = {(item.week_index, item.day_index): item for item in new_days}
        old_by_id = {item.id: item for item in old_days}
        old_schedules = list(
            self.session.scalars(
                select(PlanDaySchedule).where(PlanDaySchedule.activation_id == old_activation.id)
            )
        )
        recovery_scope = (recovery_context or {}).get("recovery_scope")
        selected_recovery_ids = (
            set(recovery_scope["selected_task_ids"]) if recovery_scope else None
        )
        scheduled_by_coordinate = {
            (
                old_by_id[item.plan_day_id].week_index,
                old_by_id[item.plan_day_id].day_index,
            ): item.scheduled_date
            for item in old_schedules
            if item.plan_day_id in old_by_id
        }
        weekdays = old_activation.execution_weekdays_json
        execution_weekdays = set(weekdays)
        excluded = {date.fromisoformat(item) for item in old_activation.excluded_dates_json}
        holidays = HolidayCalendarService(self.session)
        today = self._today_for(goal)
        extension_date_by_coordinate = {
            (int(item["week_index"]), int(item["day_index"])): date.fromisoformat(str(item["date"]))
            for item in (recovery_context or {}).get("direct_extension_schedule", [])
            if isinstance(item, dict)
            and item.get("week_index")
            and item.get("day_index")
            and item.get("date")
        }
        # Keep the calendar's historical coordinates on their original dates.
        # Unfinished work from those days is carried into existing days from
        # today onward below; moving every coordinate to a new unique date
        # would silently extend a keep-deadline recovery by many days.
        bounded_calendar = (
            revised.execution_model_version == ExecutionModelVersion.HYBRID_WORK_ITEM
            and revised.schedule_type == "CALENDAR"
        )
        # New days belong to their authored week, rather than being appended
        # after the entire active plan. Repair unrecorded out-of-week dates
        # from older revisions using the same rule; recorded dates stay locked.
        retained_dates = dict(scheduled_by_coordinate)
        if bounded_calendar:
            for coordinate, scheduled_date in scheduled_by_coordinate.items():
                first, last = plan_week_range(revised, old_activation.requested_start_date, coordinate[0])
                if coordinate not in protected and scheduled_date >= today and not first <= scheduled_date <= last:
                    retained_dates.pop(coordinate)
        cursor = max(scheduled_by_coordinate.values(), default=today) + timedelta(days=1)
        occupied: set[date] = {value for coordinate, value in retained_dates.items() if coordinate in new_by_coordinate}
        mapped_dates: dict[tuple[int, int], date] = {}
        for coordinate in sorted(new_by_coordinate):
            old_date = retained_dates.get(coordinate)
            if old_date is not None:
                mapped_dates[coordinate] = old_date
                continue
            explicit_extension_date = extension_date_by_coordinate.get(coordinate)
            if explicit_extension_date is not None:
                if explicit_extension_date in occupied:
                    raise ProviderUnavailableError("顺延日期与现有执行日冲突，原安排保持不变。")
                mapped_dates[coordinate] = explicit_extension_date
                occupied.add(explicit_extension_date)
                continue
            last = None
            if bounded_calendar:
                first, last = plan_week_range(revised, old_activation.requested_start_date, coordinate[0])
                previous = [value for key, value in mapped_dates.items() if key[0] == coordinate[0] and key < coordinate]
                following = [value for key, value in retained_dates.items() if key[0] == coordinate[0] and key > coordinate and key in new_by_coordinate]
                cursor = max(first, today, max(previous, default=first - timedelta(days=1)) + timedelta(days=1))
                if following:
                    last = min(last, min(following) - timedelta(days=1))
            while (last is None or cursor <= last) and (
                not holidays.is_execution_date(cursor, execution_weekdays, excluded)
                or cursor in occupied
            ):
                cursor += timedelta(days=1)
            if last is not None and cursor > last:
                raise ProviderUnavailableError(
                    f"第 {coordinate[0]} 周没有可容纳新增执行日的日期，未发布调整，原安排保持不变。",
                    code="ADJUSTMENT_WEEK_DATES_EXHAUSTED",
                )
            mapped_dates[coordinate] = cursor
            occupied.add(cursor)
            cursor += timedelta(days=1)

        if revised.schedule_type == "CALENDAR":
            requirement = self.session.get(RequirementVersion, goal.current_requirement_version_id)
            deadline = snapshot_deadline(
                requirement.snapshot_json if requirement else {}, timezone=goal.timezone, require_resolved=True,
            )
            if deadline and max(mapped_dates.values()) > deadline:
                raise ProviderUnavailableError(
                    "调整后的实际日期超过已确认的截止日，未发布调整，原安排保持不变。",
                    code="ADJUSTMENT_DEADLINE_EXCEEDED",
                    diagnostic=f"new_end={max(mapped_dates.values()).isoformat()}, deadline={deadline.isoformat()}",
                )

        activation = PlanActivation(
            goal_id=goal.id,
            plan_version_id=revised.id,
            requested_start_date=old_activation.requested_start_date,
            projected_end_date=max(mapped_dates.values()),
            execution_weekdays_json=weekdays,
            excluded_dates_json=old_activation.excluded_dates_json,
        )
        if (
            not (recovery_context or {}).get("allow_exploration_extension")
            and old_activation.projected_end_date
            and max(mapped_dates.values())
            > old_activation.projected_end_date + timedelta(days=RECOVERY_END_DATE_GRACE_DAYS)
        ):
            allowed_end = old_activation.projected_end_date + timedelta(
                days=RECOVERY_END_DATE_GRACE_DAYS
            )
            raise ProviderUnavailableError(
                "当前积压无法在原定结束日期后 3 天内完成，未发布该 revision。"
                "请改选更长顺延或减少可选任务。",
                diagnostic=(
                    f"new_end={max(mapped_dates.values()).isoformat()}, "
                    f"allowed_end={allowed_end.isoformat()}"
                ),
            )
        self.session.add(activation)
        self.session.flush()
        migrated_task_ids: dict[str, str] = {}
        for coordinate, new_day in new_by_coordinate.items():
            self.session.add(
                PlanDaySchedule(
                    activation_id=activation.id,
                    plan_day_id=new_day.id,
                    scheduled_date=mapped_dates[coordinate],
                )
            )

        for coordinate, new_day in new_by_coordinate.items():
            old_day = old_by_coordinate.get(coordinate)
            if old_day is None or (
                mapped_dates[coordinate] > today and coordinate not in protected
            ):
                continue
            old_tasks = list(
                self.session.scalars(
                    select(PlanTask)
                    .where(PlanTask.plan_day_id == old_day.id)
                    .order_by(PlanTask.order_in_day)
                )
            )
            new_tasks = list(
                self.session.scalars(
                    select(PlanTask)
                    .where(PlanTask.plan_day_id == new_day.id)
                    .order_by(PlanTask.order_in_day)
                )
            )
            for old_task, new_task in zip(old_tasks, new_tasks, strict=False):
                if coordinate in protected or selected_recovery_ids is not None:
                    migrated_task_ids[old_task.id] = new_task.id
                    new_task.status = old_task.status
                    new_task.source_task_id = old_task.id
                    new_task.lineage_task_id = old_task.lineage_task_id or old_task.id
                    new_task.origin_stage_order = old_task.origin_stage_order or old_day.stage_order
                    new_task.ai_schedule_requested = old_task.ai_schedule_requested
                    new_task.is_splittable = old_task.is_splittable
                elif old_task.status in {"IN_PROGRESS", "COMPLETED"}:
                    new_task.status = old_task.status

        # Historical days stay visible as history, but their unfinished work
        # must be actionable on a current/future day.  Fold each pending task
        # into the least-loaded future day instead of creating a second
        # schedule row for the same date.
        future_days = [
            new_day
            for coordinate, new_day in sorted(
                new_by_coordinate.items(), key=lambda item: mapped_dates[item[0]]
            )
            if mapped_dates[coordinate] >= today
        ]
        if not future_days:
            raise ProviderUnavailableError(
                "当前没有可承载积压任务的未来执行日，无法应用调整。请确认是否延长计划。"
            )
        loads = {
            day.id: sum(
                task.estimate_minutes
                for task in self.session.scalars(
                    select(PlanTask).where(PlanTask.plan_day_id == day.id)
                )
            )
            for day in future_days
        }
        cloned_minutes = 0
        assignment_by_task = {
            str(item["backlog_task_id"]): (int(item["target_week"]), int(item["target_day"]))
            for item in (recovery_context or {}).get("proposed_assignments", [])
            if isinstance(item, dict)
            and item.get("backlog_task_id")
            and item.get("target_week")
            and item.get("target_day")
        }
        orphan_coordinates: set[tuple[int, int]] = set()
        for coordinate, old_day in sorted(
            old_by_coordinate.items(),
            key=lambda item: (scheduled_by_coordinate.get(item[0], date.max), item[0]),
        ):
            old_date = scheduled_by_coordinate.get(coordinate)
            if old_date is None or old_date >= today:
                continue
            pending_tasks = [
                task
                for task in self.session.scalars(
                    select(PlanTask)
                    .where(PlanTask.plan_day_id == old_day.id)
                    .order_by(PlanTask.order_in_day)
                )
                if task.status not in {"COMPLETED", "CANCELLED", "CARRIED_OVER"}
                and (selected_recovery_ids is None or task.id in selected_recovery_ids)
            ]
            if not pending_tasks:
                continue
            new_day = new_by_coordinate.get(coordinate)
            if new_day is None:
                orphan_coordinates.add(coordinate)
            # Any task attached to an expired day is historical in the new
            # revision.  Carryover clones below are the actionable copies.
            if new_day is not None:
                new_tasks_by_order = {
                    task.order_in_day: task
                    for task in self.session.scalars(
                        select(PlanTask).where(PlanTask.plan_day_id == new_day.id)
                    )
                }
                for old_task in pending_tasks:
                    historical_task = (
                        self.session.get(PlanTask, migrated_task_ids[old_task.id])
                        if old_task.id in migrated_task_ids
                        else new_tasks_by_order.get(old_task.order_in_day)
                    )
                    if historical_task is not None:
                        # The historical row is an archive.  Preserve a
                        # completed state, but never leave unfinished work
                        # actionable on an expired date.
                        historical_task.status = "CARRIED_OVER"
            for old_task in pending_tasks:
                # This is a soft-cost allocation, not a fixed daily ceiling.
                # Prefer the nearest least-loaded day; users may accept more
                # time when the deadline requires it.
                assigned_coordinate = assignment_by_task.get(old_task.id)
                if recovery_context and recovery_context.get("proposed_assignments"):
                    if assigned_coordinate is None:
                        raise ProviderUnavailableError(
                            "仍有积压任务的前置要求未满足，当前追赶安排没有发布。"
                        )
                assigned_day = (
                    new_by_coordinate.get(assigned_coordinate) if assigned_coordinate else None
                )
                destination = (
                    assigned_day
                    if assigned_day in future_days
                    else min(
                        future_days,
                        key=lambda day: (
                            loads[day.id],
                            mapped_dates[
                                next(
                                    coordinate
                                    for coordinate, candidate in new_by_coordinate.items()
                                    if candidate.id == day.id
                                )
                            ],
                        ),
                    )
                )
                next_order = (
                    max(
                        (
                            task.order_in_day
                            for task in self.session.scalars(
                                select(PlanTask).where(PlanTask.plan_day_id == destination.id)
                            )
                        ),
                        default=0,
                    )
                    + 1
                )
                self.session.add(
                    PlanTask(
                        plan_version_id=revised.id,
                        plan_day_id=destination.id,
                        source_task_id=old_task.id,
                        source_work_item_id=old_task.source_work_item_id,
                        work_item_window_id=old_task.work_item_window_id,
                        occurrence_index=old_task.occurrence_index,
                        materialization_key=remap_materialization_key(
                            old_task.materialization_key, revised.id
                        ),
                        materialized_at=old_task.materialized_at,
                        origin_stage_order=(
                            old_task.origin_stage_order
                            or self.session.get(PlanDay, old_task.plan_day_id).stage_order
                        ),
                        lineage_task_id=old_task.lineage_task_id or old_task.id,
                        order_in_day=next_order,
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
                        status="IN_PROGRESS" if old_task.status == "IN_PROGRESS" else "NOT_STARTED",
                    )
                )
                # Session autoflush is disabled. The next max(order) query
                # must see this clone when several backlog tasks share a day.
                self.session.flush()
                loads[destination.id] += old_task.estimate_minutes
                cloned_minutes += old_task.estimate_minutes
        self.session.flush()
        for new_day in new_days:
            new_day.suggested_minutes = max(
                new_day.suggested_minutes,
                sum(
                    task.estimate_minutes
                    for task in self.session.scalars(
                        select(PlanTask).where(
                            PlanTask.plan_day_id == new_day.id,
                            PlanTask.status != "CARRIED_OVER",
                        )
                    )
                ),
            )
        revised.total_estimated_minutes = sum(
            task.estimate_minutes
            for task in self.session.scalars(
                select(PlanTask).where(
                    PlanTask.plan_version_id == revised.id,
                    PlanTask.status != "CARRIED_OVER",
                )
            )
        )

        # Archive recovered work on expired dates. A scoped recovery must
        # retain deferred tasks so their progress and recovery entry survive.
        for coordinate, new_day in new_by_coordinate.items():
            if mapped_dates[coordinate] >= today:
                continue
            for task in self.session.scalars(
                select(PlanTask).where(PlanTask.plan_day_id == new_day.id)
            ):
                if (
                    selected_recovery_ids is not None
                    and task.source_task_id not in selected_recovery_ids
                ):
                    continue
                if task.status not in {"COMPLETED", "CANCELLED"}:
                    task.status = "CARRIED_OVER"
        self.session.flush()

        active_tasks = list(self.session.scalars(select(PlanTask).where(
            PlanTask.plan_version_id == revised.id,
            PlanTask.status != "CARRIED_OVER",
        )))
        # Split parts share lineage; map exact source IDs to actionable copies.
        active_id_by_source = {
            task.source_task_id: task.id for task in active_tasks if task.source_task_id
        }
        for task in active_tasks:
            task.depends_on_json = [
                active_id_by_source.get(reference, reference)
                for reference in task.depends_on_json or []
            ]
        self.session.flush()

        # Recovered historical rows are archival; deferred rows remain pending.
        # Their actionable clones are created on a current/future day. Do not run a
        # second status check here: SQLAlchemy may expose a stale relationship
        # collection while the newly-added carryover rows are being flushed,
        # which previously produced a false migration failure.

        for coordinate, old_day in old_by_coordinate.items():
            new_day = new_by_coordinate.get(coordinate)
            if new_day is None:
                continue
            feedbacks = list(
                self.session.scalars(select(Feedback).where(Feedback.plan_day_id == old_day.id))
            )
            for feedback in feedbacks:
                feedback.plan_version_id = revised.id
                feedback.plan_day_id = new_day.id
                if feedback.task_id:
                    old_task = self.session.get(PlanTask, feedback.task_id)
                    replacement = (
                        self.session.get(PlanTask, migrated_task_ids[old_task.id])
                        if old_task and old_task.id in migrated_task_ids else
                        self.session.scalar(
                            select(PlanTask).where(
                                PlanTask.plan_day_id == new_day.id,
                                PlanTask.order_in_day == old_task.order_in_day,
                            )
                        )
                        if old_task
                        else None
                    )
                    feedback.task_id = replacement.id if replacement else None
                adjustment = self.session.scalar(
                    select(FeedbackAdjustment).where(FeedbackAdjustment.feedback_id == feedback.id)
                )
                if adjustment:
                    adjustment.plan_version_id = revised.id
            day_log = self.session.scalar(
                select(ExecutionDayLog).where(ExecutionDayLog.plan_day_id == old_day.id)
            )
            if day_log:
                day_log.plan_version_id = revised.id
                day_log.plan_day_id = new_day.id
                day_log.scheduled_date = mapped_dates[coordinate]
            progress_events = list(
                self.session.scalars(
                    select(ProgressEvent).where(ProgressEvent.plan_day_id == old_day.id)
                )
            )
            focus_sessions = list(
                self.session.scalars(
                    select(FocusSession).where(FocusSession.plan_day_id == old_day.id)
                )
            )
            old_tasks = {
                item.id: item
                for item in self.session.scalars(
                    select(PlanTask).where(PlanTask.plan_day_id == old_day.id)
                )
            }
            new_tasks_by_order = {
                item.order_in_day: item
                for item in self.session.scalars(
                    select(PlanTask).where(PlanTask.plan_day_id == new_day.id)
                )
            }
            for event in progress_events:
                event.plan_version_id = revised.id
                event.plan_day_id = new_day.id
                if event.task_id and event.task_id in old_tasks:
                    replacement = (self.session.get(PlanTask, migrated_task_ids[event.task_id])
                                   if event.task_id in migrated_task_ids else
                                   new_tasks_by_order.get(old_tasks[event.task_id].order_in_day))
                    event.task_id = replacement.id if replacement else None
            for focus_session in focus_sessions:
                focus_session.plan_version_id = revised.id
                focus_session.plan_day_id = new_day.id
                if focus_session.task_id and focus_session.task_id in old_tasks:
                    replacement = (self.session.get(PlanTask, migrated_task_ids[focus_session.task_id])
                                   if focus_session.task_id in migrated_task_ids else
                                   new_tasks_by_order.get(old_tasks[focus_session.task_id].order_in_day))
                    focus_session.task_id = replacement.id if replacement else None
        self.session.flush()
        if source.execution_model_version == ExecutionModelVersion.HYBRID_WORK_ITEM:
            from planner_api.work_item_projection import clone_work_item_projection
            clone_work_item_projection(self.session, source, revised, old_activation, activation)

    def _current_plan(self, goal_id: str) -> PlanVersion | None:
        return self.session.scalar(
            select(PlanVersion)
            .where(
                PlanVersion.goal_id == goal_id,
                PlanVersion.status.in_(["GENERATING", "DRAFT", "CONFIRMED"]),
            )
            .order_by(PlanVersion.created_at.desc())
        )

    @staticmethod
    def _generation_target(plan: PlanVersion) -> int:
        if plan.planning_mode == "EXPLORATORY":
            return min(plan.total_weeks, max(0, plan.next_unplanned_week - 1))
        return plan.total_weeks

    def _initial_exploration_boundary(
        self, goal: Goal, route_stages: list[RouteStage], result
    ) -> tuple[int, dict[str, object]]:
        if goal.planning_mode != "EXPLORATORY":
            return result.total_weeks, {"horizon_basis": "COMPLETE_ROUTE"}
        requirement = self.session.get(RequirementVersion, goal.current_requirement_version_id)
        advice = recommend_planning_approach(
            goal.raw_input,
            requirement.snapshot_json if requirement else {},
        )
        clarification = self.session.scalar(
            select(Conversation).where(
                Conversation.goal_id == goal.id,
                Conversation.phase == "CLARIFICATION",
            )
        )
        model_trial_weeks = None
        if clarification:
            for message in reversed(self._conversation_messages(clarification.id)):
                recommendation = message.metadata_json.get("planning_approach")
                if isinstance(recommendation, dict):
                    model_trial_weeks = recommendation.get("explicit_trial_weeks")
                    break
        explicit_trial_weeks = advice.explicit_trial_weeks or model_trial_weeks
        if explicit_trial_weeks:
            end_week = min(result.total_weeks, int(explicit_trial_weeks))
            return end_week, {
                "horizon_basis": "USER_DISCUSSION_TRIAL_PERIOD",
                "explicit_trial_weeks": int(explicit_trial_weeks),
            }

        stage_by_order = {stage.order: stage for stage in route_stages}
        outcome_markers = ("成果", "体验", "实践", "作品", "验证", "效果", "真实")
        milestone = next(
            (
                item
                for item in result.milestones
                if any(
                    marker
                    in " ".join([*item.outputs, *stage_by_order[item.stage_order].outputs_json])
                    for marker in outcome_markers
                )
            ),
            result.milestones[0],
        )
        return milestone.end_week, {
            "horizon_basis": "FIRST_OBSERVABLE_ROUTE_OUTCOME",
            "stage_order": milestone.stage_order,
            "expected_outputs": list(milestone.outputs),
        }

    def _next_exploration_stage_end(self, plan: PlanVersion, start_week: int) -> int:
        milestone = next(
            (item for item in self._milestones(plan.id) if item.end_week >= start_week),
            None,
        )
        return min(plan.total_weeks, milestone.end_week if milestone else plan.total_weeks)

    def _route_stages(self, route_id: str) -> list[RouteStage]:
        return list(
            self.session.scalars(
                select(RouteStage)
                .where(RouteStage.route_version_id == route_id)
                .order_by(RouteStage.order)
            )
        )

    def _milestones(self, plan_id: str) -> list[PlanMilestone]:
        return list(
            self.session.scalars(
                select(PlanMilestone)
                .where(PlanMilestone.plan_version_id == plan_id)
                .order_by(PlanMilestone.stage_order)
            )
        )

    def _generated_weeks(self, plan_id: str) -> set[int]:
        plan = self.session.get(PlanVersion, plan_id)
        if (
            plan is not None
            and plan.execution_model_version == ExecutionModelVersion.HYBRID_WORK_ITEM
        ):
            rows = self.session.scalars(
                select(GenerationBatch).where(
                    GenerationBatch.plan_version_id == plan_id,
                    GenerationBatch.scope_type == "HYBRID_WEEK",
                    GenerationBatch.status == "COMPLETED",
                )
            )
            return {
                int(row.scope_json["week_index"])
                for row in rows
                if row.scope_json.get("week_index") is not None
            }
        return set(
            self.session.scalars(
                select(PlanDay.week_index).where(PlanDay.plan_version_id == plan_id).distinct()
            )
        )

    def _latest_generation_job(self, goal_id: str) -> PlanGenerationJob | None:
        return self.session.scalar(
            select(PlanGenerationJob)
            .where(PlanGenerationJob.goal_id == goal_id)
            .order_by(PlanGenerationJob.created_at.desc())
        )

    @staticmethod
    def _plan_window_read(window: PlanWindow) -> PlanWindowRead:
        return PlanWindowRead(
            id=window.id,
            plan_version_id=window.plan_version_id,
            sequence=window.sequence,
            start_week=window.start_week,
            end_week=window.end_week,
            status=window.status,
            trigger=window.trigger,
            basis=dict(window.basis_json or {}),
            created_at=window.created_at,
            published_at=window.published_at,
        )

    @staticmethod
    def _generation_job_read(job: PlanGenerationJob) -> PlanGenerationJobRead:
        error_message = PlanService._generation_failure_message(
            "MODEL_REQUEST_REJECTED"
            if job.status == "FAILED" and "HTTP 402" in (job.diagnostic or "")
            else job.error_code,
            job.diagnostic,
            job.error_message,
        )
        return PlanGenerationJobRead(
            id=job.id,
            mode=job.mode,
            planning_mode=job.planning_mode,
            schedule_type=job.schedule_type,
            status=job.status,
            phase=job.phase,
            generated_weeks=job.generated_weeks,
            total_weeks=job.total_weeks,
            error_code=job.error_code,
            error_message=error_message,
            created_at=job.created_at,
            updated_at=job.updated_at,
        )

    def _plan_conversation(self, goal_id: str) -> Conversation | None:
        return self.session.scalar(
            select(Conversation).where(
                Conversation.goal_id == goal_id,
                Conversation.phase == "PLAN",
            )
        )

    def _ensure_plan_conversation(self, goal_id: str) -> Conversation:
        conversation = self._plan_conversation(goal_id)
        if conversation:
            return conversation
        conversation = Conversation(goal_id=goal_id, phase="PLAN")
        self.session.add(conversation)
        self.session.flush()
        return conversation

    def _conversation_messages(
        self, conversation_id: str, limit: int | None = None
    ) -> list[Message]:
        query = (
            select(Message)
            .where(Message.conversation_id == conversation_id)
            .order_by(Message.sequence.desc())
            .limit(limit or get_settings().conversation_workspace_message_limit)
        )
        messages = list(self.session.scalars(query))
        messages.reverse()
        return messages

    def _scope_exploratory_adjustment(
        self,
        plan: PlanVersion | None,
        proposal: PlanAdjustmentProposal | None,
    ) -> PlanAdjustmentProposal | None:
        if plan is None or proposal is None or plan.planning_mode != "EXPLORATORY":
            return proposal
        materialized_days = list(
            self.session.execute(
                select(PlanDay.week_index, PlanDay.stage_order).where(
                    PlanDay.plan_version_id == plan.id
                )
            )
        )
        materialized_weeks = {week for week, _ in materialized_days}
        deferred_weeks = set(proposal.affected_weeks) - materialized_weeks
        if not deferred_weeks:
            return proposal
        direct_weeks = set(proposal.affected_weeks) & materialized_weeks
        direct_stage_orders = {
            stage_order for week, stage_order in materialized_days if week in direct_weeks
        }
        return proposal.model_copy(
            update={
                "affected_stage_orders": sorted(
                    set(proposal.affected_stage_orders) & direct_stage_orders
                ),
                "affected_weeks": sorted(direct_weeks),
                "downstream_affected_weeks": sorted(
                    set(proposal.downstream_affected_weeks) | deferred_weeks
                ),
            },
            deep=True,
        )

    @staticmethod
    def _pending_adjustment(
        messages: list[Message], recovery_brief_id: str | None = None
    ) -> PlanAdjustmentProposal | None:
        for message in reversed(messages):
            metadata = message.metadata_json
            if message.role != "ASSISTANT":
                continue
            # A plan conversation contains ordinary revisions and multiple
            # recovery attempts.  A terminal message from another attempt
            # must never hide the proposal belonging to the active brief.
            if (
                recovery_brief_id is not None
                and metadata.get("recovery_brief_id") != recovery_brief_id
            ):
                continue
            if metadata.get("adjustment_applied") or metadata.get("adjustment_dismissed"):
                return None
            if metadata.get("intent") != "REVISE":
                continue
            if message.generation_status == "FAILED":
                continue
            if metadata.get("adjustment_dismissed"):
                return None
            proposal = metadata.get("adjustment_proposal")
            if isinstance(proposal, dict):
                try:
                    return PlanAdjustmentProposal.model_validate(proposal)
                except ValueError:
                    return None
            return None
        return None

    def _add_user_message(self, conversation: Conversation, content: str, intent: str) -> Message:
        message = Message(
            conversation_id=conversation.id,
            sequence=self._next_sequence(conversation.id),
            role="USER",
            content=content,
            metadata_json={"intent": intent},
        )
        self.session.add(message)
        self.session.flush()
        return message

    def _add_assistant_message(
        self,
        conversation: Conversation,
        content: str,
        intent: str,
        metadata: dict[str, object],
        existing: Message | None = None,
    ) -> None:
        if existing is None:
            latest = self.session.scalar(
                select(Message)
                .where(Message.conversation_id == conversation.id)
                .order_by(Message.sequence.desc())
            )
            if (
                latest is not None
                and latest.role == "ASSISTANT"
                and latest.generation_status == "PENDING"
                and (latest.metadata_json or {}).get("regenerating")
            ):
                existing = latest
        message = existing or Message(
                conversation_id=conversation.id,
                sequence=self._next_sequence(conversation.id),
                role="ASSISTANT",
                content=content,
            )
        message.content = content
        message.generation_status = "SUCCEEDED"
        message.metadata_json = {
            "intent": intent,
            "prompt_version": PLAN_DISCUSSION_PROMPT_VERSION,
            **metadata,
        }
        if existing is None:
            self.session.add(message)
        refresh_phase_summary(self.session, conversation)

    def _add_failed_message(
        self,
        conversation: Conversation,
        error: ClarificationProviderError,
        intent: str,
        existing: Message | None = None,
    ) -> None:
        if existing is None:
            latest = self.session.scalar(
                select(Message)
                .where(Message.conversation_id == conversation.id)
                .order_by(Message.sequence.desc())
            )
            if (
                latest is not None
                and latest.role == "ASSISTANT"
                and latest.generation_status == "PENDING"
                and (latest.metadata_json or {}).get("regenerating")
            ):
                existing = latest
        message = existing or Message(
                conversation_id=conversation.id,
                sequence=self._next_sequence(conversation.id),
                role="ASSISTANT",
                content=str(error),
            )
        message.content = str(error)
        message.generation_status = "FAILED"
        message.metadata_json = {
            "intent": intent,
            "prompt_version": PLAN_DISCUSSION_PROMPT_VERSION,
            "error_type": type(error).__name__,
            "error_diagnostic": error.diagnostic,
        }
        if existing is None:
            self.session.add(message)

    def _prepare_discussion_exchange(
        self,
        conversation: Conversation,
        content: str,
        intent: str,
        *,
        allow_existing: bool = False,
    ) -> tuple[Message, Message | None]:
        recent = list(
            self.session.scalars(
                select(Message)
                .where(Message.conversation_id == conversation.id)
                .order_by(Message.sequence.desc())
                .limit(2)
            )
        )
        if len(recent) == 2:
            assistant, user = recent
            saved_intent = (assistant.metadata_json or {}).get("intent") or (user.metadata_json or {}).get("intent")
            if (
                allow_existing
                and assistant.role == "ASSISTANT"
                and assistant.generation_status in {"FAILED", "SUCCEEDED", "PENDING"}
                and user.role == "USER"
                and user.content.strip() == content
                and saved_intent == intent
            ):
                assistant.content = "正在重新整理这次计划回复，当前安排保持不变。"
                assistant.generation_status = "PENDING"
                assistant.metadata_json = {"intent": intent, "regenerating": True}
                return user, assistant
        return self._add_user_message(conversation, content, intent), None

    @staticmethod
    def _message_read(message: Message) -> MessageRead:
        return MessageRead(
            id=message.id,
            sequence=message.sequence,
            role=message.role,
            content=message.content,
            generation_status=message.generation_status,
            metadata=message.metadata_json,
            created_at=message.created_at,
        )

    def _next_sequence(self, conversation_id: str) -> int:
        maximum = self.session.scalar(
            select(func.max(Message.sequence)).where(Message.conversation_id == conversation_id)
        )
        return (maximum or 0) + 1

    @staticmethod
    def _context_pack_read(context: ContextPack) -> ContextPackRead:
        return ContextPackRead(
            id=context.id,
            goal_id=context.goal_id,
            requirement_version_id=context.requirement_version_id,
            route_version_id=context.route_version_id,
            plan_version_id=context.plan_version_id,
            version=context.version,
            facts=context.facts_json,
            locked_day_ids=context.locked_day_ids_json,
            locked_task_ids=context.locked_task_ids_json,
            created_at=context.created_at,
        )

    @classmethod
    def _planning_brief_read(cls, brief: PlanningBrief, context: ContextPack) -> PlanningBriefRead:
        return PlanningBriefRead(
            id=brief.id,
            goal_id=brief.goal_id,
            context_pack_id=brief.context_pack_id,
            plan_version_id=brief.plan_version_id,
            brief_kind=brief.brief_kind,
            version=brief.version,
            status=brief.status,
            brief=brief.brief_json,
            context=cls._context_pack_read(context),
            created_at=brief.created_at,
            confirmed_at=brief.confirmed_at,
        )

    @staticmethod
    def _generation_batch_read(batch: GenerationBatch) -> GenerationBatchRead:
        return GenerationBatchRead(
            id=batch.id,
            goal_id=batch.goal_id,
            plan_version_id=batch.plan_version_id,
            brief_id=batch.brief_id,
            idempotency_key=batch.idempotency_key,
            scope_type=batch.scope_type,
            scope=batch.scope_json,
            status=batch.status,
            attempt_count=batch.attempt_count,
            failure_code=batch.failure_code,
            failure_message=batch.failure_message,
            staged_result=batch.staged_result_json,
            created_at=batch.created_at,
            updated_at=batch.updated_at,
        )

    def _plan_read(self, plan: PlanVersion) -> PlanVersionRead:
        milestones = list(
            self.session.scalars(
                select(PlanMilestone)
                .where(PlanMilestone.plan_version_id == plan.id)
                .order_by(PlanMilestone.stage_order)
            )
        )
        days = list(
            self.session.scalars(
                select(PlanDay)
                .where(PlanDay.plan_version_id == plan.id)
                .order_by(PlanDay.week_index, PlanDay.day_index)
            )
        )
        tasks_by_day: dict[str, list[PlanTask]] = {}
        tasks = list(
            self.session.scalars(
                select(PlanTask)
                .where(PlanTask.plan_version_id == plan.id)
                .order_by(PlanTask.plan_day_id, PlanTask.order_in_day)
            )
        )
        for task in tasks:
            tasks_by_day.setdefault(task.plan_day_id, []).append(task)
        work_item_groups: list[PlanWorkItemGroup] = []
        work_items: list[PlanWorkItem] = []
        dependencies_by_successor: dict[str, list[str]] = {}
        windows_by_item: dict[str, list[WorkItemWindow]] = {}
        if plan.execution_model_version == "HYBRID_WORK_ITEM":
            work_item_groups = list(
                self.session.scalars(
                    select(PlanWorkItemGroup)
                    .where(PlanWorkItemGroup.plan_version_id == plan.id)
                    .order_by(PlanWorkItemGroup.group_key)
                )
            )
            work_items = list(
                self.session.scalars(
                    select(PlanWorkItem)
                    .where(PlanWorkItem.plan_version_id == plan.id)
                    .order_by(
                        PlanWorkItem.stage_order,
                        PlanWorkItem.priority,
                        PlanWorkItem.created_at,
                    )
                )
            )
            for dependency in self.session.scalars(
                select(WorkItemDependency).where(WorkItemDependency.plan_version_id == plan.id)
            ):
                dependencies_by_successor.setdefault(dependency.successor_work_item_id, []).append(
                    dependency.predecessor_work_item_id
                )
        if plan.execution_model_version == "HYBRID_WORK_ITEM" and work_items:
            for window in self.session.scalars(
                select(WorkItemWindow)
                .where(WorkItemWindow.work_item_id.in_([item.id for item in work_items]))
                .order_by(WorkItemWindow.eligible_from, WorkItemWindow.period_key)
            ):
                windows_by_item.setdefault(window.work_item_id, []).append(window)
        return PlanVersionRead(
            id=plan.id,
            route_version_id=plan.route_version_id,
            version=plan.version,
            revision=plan.revision,
            status=plan.status,
            summary=plan.summary,
            total_weeks=plan.total_weeks,
            weekly_execution_days=plan.weekly_execution_days,
            weekly_minutes=plan.weekly_minutes,
            total_estimated_minutes=plan.total_estimated_minutes,
            buffer_strategy=plan.buffer_strategy_json,
            assumptions=plan.assumptions_json,
            open_questions=plan.open_questions_json,
            change_summary=plan.change_summary_json,
            temporal_segments=list(plan.temporal_segments_json),
            commitments=list(plan.commitments_json),
            source_document=plan.source_document,
            compiler_report=dict(plan.compiler_report_json),
            planning_mode=plan.planning_mode,
            schedule_type=plan.schedule_type,
            execution_model_version=plan.execution_model_version,
            exploration_window_weeks=plan.exploration_window_weeks,
            next_unplanned_week=plan.next_unplanned_week,
            work_item_groups=[
                PlanWorkItemGroupRead(
                    id=item.id,
                    milestone_id=item.milestone_id,
                    scope_type=item.scope_type,
                    stage_order=item.stage_order,
                    group_key=item.group_key,
                    title=item.title,
                    selection_mode=item.selection_mode,
                    minimum_selected=item.minimum_selected,
                    maximum_selected=item.maximum_selected,
                    constraints=item.constraints_json,
                    status=item.status,
                )
                for item in work_item_groups
            ],
            work_items=[
                PlanWorkItemRead(
                    id=item.id,
                    item_key=item.item_key,
                    milestone_id=item.milestone_id,
                    group_id=item.group_id,
                    source_work_item_id=item.source_work_item_id,
                    lineage_work_item_id=item.lineage_work_item_id,
                    scope_type=item.scope_type,
                    stage_order=item.stage_order,
                    title=item.title,
                    description=item.description,
                    task_kind=item.task_kind,
                    estimate_minutes=item.estimate_minutes,
                    priority=item.priority,
                    is_required=item.is_required,
                    is_splittable=item.is_splittable,
                    binding_mode=item.binding_mode,
                    fulfillment_mode=item.fulfillment_mode,
                    target_value=item.target_value,
                    target_unit=item.target_unit,
                    constraints=item.constraints_json,
                    completion_rule=item.completion_rule_json,
                    evidence=item.evidence_json,
                    source_refs=item.source_refs_json,
                    commitment_refs=item.commitment_refs_json,
                    dependency_ids=dependencies_by_successor.get(item.id, []),
                    status=item.status,
                    windows=[
                        WorkItemWindowRead(
                            id=window.id,
                            period_key=window.period_key,
                            eligible_from=window.eligible_from,
                            eligible_to=window.eligible_to,
                            required_value=window.required_value,
                            status=window.status,
                            resolution=window.resolution_json,
                            materialization_revision=window.materialization_revision,
                        )
                        for window in windows_by_item.get(item.id, [])
                    ],
                )
                for item in work_items
            ],
            milestones=[
                PlanMilestoneRead(
                    id=item.id,
                    route_stage_id=item.route_stage_id,
                    stage_order=item.stage_order,
                    title=item.title,
                    purpose=item.purpose,
                    start_week=item.start_week,
                    end_week=item.end_week,
                    outputs=item.outputs_json,
                )
                for item in milestones
            ],
            days=[
                PlanDayRead(
                    id=day.id,
                    stage_order=day.stage_order,
                    week_index=day.week_index,
                    day_index=day.day_index,
                    primary_goal=day.primary_goal,
                    suggested_minutes=day.suggested_minutes,
                    evidence=day.evidence_json,
                    do_not=day.do_not_json,
                    tasks=[plan_task_read(task) for task in tasks_by_day.get(day.id, [])],
                )
                for day in days
            ],
        )
