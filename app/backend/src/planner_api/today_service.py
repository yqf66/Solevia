from __future__ import annotations

from datetime import date, datetime
from itertools import combinations
from zoneinfo import ZoneInfo

from fastapi import HTTPException
from sqlalchemy import delete, or_, select, text
from sqlalchemy.orm import Session

from planner_api.dependency_resolver import PlanDependencyResolver
from planner_api.models import (
    Goal,
    ManualPlan,
    ManualTask,
    ManualTaskDateOverride,
    PlanActivation,
    PlanDaySchedule,
    PlanSchedulePreference,
    PlanTask,
    PlanVersion,
    ScheduleChange,
    ScheduleParallelConsent,
    TaskTimeBlock,
    WorkspaceScheduleProfile,
)
from planner_api.schedule_constraints import shared_unavailable_windows
from planner_api.schedule_reconciliation import SCHEDULE_PLANNED, SCHEDULE_REQUIRES_REVIEW
from planner_api.schemas import (
    TodayScheduleConflictRead,
    TodayTaskScheduleRead,
    TodayTaskScheduleRequest,
    TodayTaskScheduleResult,
    UnifiedTodayDependencyRead,
    UnifiedTodayRead,
    UnifiedTodayTaskRead,
)


class TodayService:
    """Read-only projection of all plans into one cross-plan Today board."""

    def __init__(self, session: Session) -> None:
        self.session = session
        self.dependencies = PlanDependencyResolver(session)

    def _dependencies_for_task(
        self,
        task: PlanTask,
        plan_id: str,
        cache: dict[str, list[UnifiedTodayDependencyRead]],
    ) -> list[UnifiedTodayDependencyRead]:
        if task.id in cache:
            return cache[task.id]
        if not task.depends_on_json:
            cache[task.id] = []
            return []
        indexes = self.dependencies.indexes(plan_id)
        result: list[UnifiedTodayDependencyRead] = []
        for raw in task.depends_on_json:
            reference = str(raw)
            resolved = self.dependencies.resolve(raw, task, indexes)
            status = (
                "UNKNOWN"
                if resolved is None
                else (
                    "COMPLETED" if resolved.status in {"COMPLETED", "CARRIED_OVER"} else "PENDING"
                )
            )
            result.append(
                UnifiedTodayDependencyRead(
                    reference=reference,
                    task_id=resolved.id if resolved else None,
                    title=resolved.title if resolved else None,
                    status=status,
                )
            )
        cache[task.id] = result
        return result

    def get_today(self, on_date: date | None = None) -> UnifiedTodayRead:
        from planner_api.execution_service import ExecutionService

        execution = ExecutionService(self.session)
        profile = self.session.get(WorkspaceScheduleProfile, "default")
        timezone = profile.timezone if profile else "Asia/Shanghai"
        today = on_date or datetime.now(ZoneInfo(timezone)).date()
        tasks: list[UnifiedTodayTaskRead] = []
        dependency_cache: dict[str, list[UnifiedTodayDependencyRead]] = {}

        goals = list(
            self.session.scalars(
                select(Goal).where(
                    Goal.status.in_(["ACTIVE", "RECOVERY_REQUIRED"]),
                    Goal.active_plan_version_id.is_not(None),
                )
            )
        )
        for goal in goals:
            plan = self.session.get(PlanVersion, goal.active_plan_version_id)
            if plan is None:
                continue
            activation = self.session.scalar(
                select(PlanActivation).where(PlanActivation.plan_version_id == plan.id)
            )
            if activation is None:
                continue
            day_schedule = self.session.scalar(
                select(PlanDaySchedule).where(
                    PlanDaySchedule.activation_id == activation.id,
                    PlanDaySchedule.scheduled_date == today,
                )
            )
            if day_schedule is None:
                continue
            preference = self.session.scalar(
                select(PlanSchedulePreference).where(
                    PlanSchedulePreference.goal_id == goal.id,
                )
            )
            blocks = {
                block.task_id: block
                for block in self.session.scalars(
                    select(TaskTimeBlock).where(
                        TaskTimeBlock.goal_id == goal.id,
                        TaskTimeBlock.scheduled_date == today,
                        TaskTimeBlock.plan_day_id == day_schedule.plan_day_id,
                        TaskTimeBlock.status.in_([SCHEDULE_PLANNED, SCHEDULE_REQUIRES_REVIEW]),
                    )
                )
            }
            for task in self.session.scalars(
                select(PlanTask)
                .where(
                    PlanTask.plan_day_id == day_schedule.plan_day_id,
                    PlanTask.status.not_in(["CANCELLED", "CARRIED_OVER"]),
                )
                .order_by(PlanTask.order_in_day)
            ):
                block = blocks.get(task.id)
                active_block = block if block and block.status == SCHEDULE_PLANNED else None
                tasks.append(
                    UnifiedTodayTaskRead(
                        id=task.id,
                        source="AGENT",
                        plan_id=goal.id,
                        plan_title=goal.title,
                        title=task.title,
                        description=task.description,
                        estimate_minutes=task.estimate_minutes,
                        priority=task.priority,
                        status=task.status,
                        scheduled_date=today,
                        start_local=active_block.start_local if active_block else None,
                        end_local=active_block.end_local if active_block else None,
                        timezone=active_block.timezone if active_block else goal.timezone,
                        is_fixed=active_block is not None,
                        parallel_confirmed=(
                            active_block.parallel_confirmed if active_block else False
                        ),
                        schedule_status=block.status if block else SCHEDULE_PLANNED,
                        schedule_review_required=bool(
                            block
                            and block.status == SCHEDULE_REQUIRES_REVIEW
                            or not active_block
                            and preference
                            and preference.mode != "FLEXIBLE"
                            and task.status == "NOT_STARTED"
                        ),
                        schedule_change_reason=block.change_reason if block else None,
                        dependencies=self._dependencies_for_task(task, plan.id, dependency_cache),
                        blocked_reason=execution._hybrid_predecessor_blocked_reason(plan, task)
                        if task.status != "COMPLETED" else None,
                        ai_schedule_requested=task.ai_schedule_requested,
                        scheduling_policy=task.scheduling_policy or "EXACT_DAY",
                        scheduling_policy_data=task.scheduling_policy_json or {},
                        skip_guidance={
                            "TASK_POOL": ["补选其他候选任务", "也可改到其他日期"],
                            "TRIGGERED": ["保留触发条件，满足条件后重新安排"],
                            "CADENCE": ["保留本周期剩余配额", "转入弹性补做"],
                            "QUOTA": ["保留本周期剩余配额", "转入弹性补做"],
                            "DATE_WINDOW": ["加入弹性待安排", "改到允许日期"],
                            "FLOATING": ["加入弹性待安排", "改到其他日期"],
                            "EXACT_DAY": ["记录跳过", "需要时通过恢复安排补做"],
                        }.get(task.scheduling_policy or "EXACT_DAY", ["记录跳过"]),
                    )
                )

        for plan in self.session.scalars(select(ManualPlan).where(ManualPlan.status == "ACTIVE")):
            for task in self.session.scalars(
                select(ManualTask)
                .where(
                    ManualTask.manual_plan_id == plan.id,
                )
                .order_by(ManualTask.order_in_plan)
            ):
                override = self.session.scalar(
                    select(ManualTaskDateOverride).where(
                        ManualTaskDateOverride.manual_task_id == task.id,
                        ManualTaskDateOverride.override_date == today,
                    )
                )
                if not self._manual_task_occurs_on(task, today) and not (
                    override and override.time_overridden and not override.is_deleted
                ):
                    continue
                if override and override.is_deleted:
                    continue
                status = (
                    override.status if override and override.status is not None else task.status
                )
                if status in {"SKIPPED", "CANCELLED"}:
                    continue
                tasks.append(
                    UnifiedTodayTaskRead(
                        id=task.id,
                        source="QUICK",
                        plan_id=plan.id,
                        plan_title=plan.title,
                        plan_kind=plan.kind,
                        title=override.title
                        if override and override.title is not None
                        else task.title,
                        description=override.description
                        if override and override.description is not None
                        else task.description,
                        estimate_minutes=override.estimate_minutes
                        if override and override.estimate_minutes is not None
                        else task.estimate_minutes,
                        priority=override.priority
                        if override and override.priority is not None
                        else task.priority,
                        status=status,
                        scheduled_date=today,
                        start_local=override.start_local
                        if override and override.time_overridden
                        else task.start_local,
                        end_local=override.end_local
                        if override and override.time_overridden
                        else task.end_local,
                        timezone=plan.timezone,
                        is_fixed=(
                            override.start_local is not None and override.end_local is not None
                        )
                        if override and override.time_overridden
                        else (task.start_local is not None and task.end_local is not None),
                        parallel_confirmed=task.parallel_confirmed,
                        dependencies=[],
                        ai_schedule_requested=False,
                        scheduling_policy=(
                            "EXACT_DAY"
                            if (
                                (override.start_local is not None and override.end_local is not None)
                                if override and override.time_overridden
                                else (task.start_local is not None and task.end_local is not None)
                            )
                            else "FLOATING"
                        ),
                        scheduling_policy_data={
                            "source": "QUICK_PLAN",
                            "recurrence": task.recurrence,
                            "recurrence_weekdays": task.recurrence_weekdays_json or [],
                        },
                    )
                )

        fixed = sorted(
            [item for item in tasks if item.is_fixed],
            key=lambda item: (item.start_local or "", item.end_local or "", item.title),
        )
        flexible = sorted(
            [item for item in tasks if not item.is_fixed],
            key=lambda item: (-item.priority, item.title),
        )
        conflicts: list[TodayScheduleConflictRead] = []
        consents = list(
            self.session.scalars(
                select(ScheduleParallelConsent).where(
                    ScheduleParallelConsent.scheduled_date == today,
                )
            )
        )

        def consented(first, second):
            return any(
                c.first_task_id == a.id
                and c.second_task_id == b.id
                and (c.first_start, c.first_end) == (a.start_local, a.end_local)
                and (c.second_start, c.second_end) == (b.start_local, b.end_local)
                for c in consents
                for a, b in [(first, second), (second, first)]
            )

        for first, second in combinations(fixed, 2):
            if (first.end_local or "") > (second.start_local or "") and not consented(
                first,
                second,
            ):
                conflicts.append(
                    TodayScheduleConflictRead(
                        first_task_id=first.id,
                        second_task_id=second.id,
                        overlap_start=second.start_local or "",
                        overlap_end=min(first.end_local or "", second.end_local or ""),
                        reason="两个固定时间任务重叠，需要顺延、拆分或确认并行",
                    )
                )
        return UnifiedTodayRead(
            date=today,
            timezone=timezone,
            fixed_tasks=fixed,
            flexible_tasks=flexible,
            conflicts=conflicts,
        )

    @staticmethod
    def _manual_task_occurs_on(task: ManualTask, day: date) -> bool:
        if task.scheduled_date is None or day < task.scheduled_date:
            return False
        if task.recurrence_end_date and day > task.recurrence_end_date:
            return False
        if task.recurrence == "DAILY":
            return True
        if task.recurrence == "WEEKLY":
            return day.weekday() in (task.recurrence_weekdays_json or [])
        return day == task.scheduled_date

    @staticmethod
    def _overlaps(first_start: str, first_end: str, second_start: str, second_end: str) -> bool:
        return first_start < second_end and second_start < first_end

    def schedule_task(
        self,
        payload: TodayTaskScheduleRequest,
        *,
        commit: bool = True,
    ) -> TodayTaskScheduleResult:
        if commit and self.session.bind.dialect.name == "sqlite":
            self.session.execute(text("BEGIN IMMEDIATE"))
        board = self.get_today(payload.scheduled_date)
        requested_source = "QUICK" if payload.source == "MANUAL" else payload.source
        target = next(
            (
                item
                for item in [*board.fixed_tasks, *board.flexible_tasks]
                if item.id == payload.task_id and item.source == requested_source
            ),
            None,
        )
        if target is None:
            raise HTTPException(409, "任务不属于当前生效计划的这个执行日，请刷新后重试。")
        if target.status != "NOT_STARTED":
            raise HTTPException(409, "只可调整尚未开始的任务时间。")
        start_minutes = int(payload.start_local[:2]) * 60 + int(payload.start_local[3:])
        end_minutes = int(payload.end_local[:2]) * 60 + int(payload.end_local[3:])
        if any(
            start_minutes < end and start < end_minutes
            for start, end, _ in shared_unavailable_windows(self.session, payload.scheduled_date)
        ):
            raise HTTPException(409, "这段时间已有课程或时间约定，请选择其他时段。")
        conflicts = [
            TodayScheduleConflictRead(
                first_task_id=item.id,
                second_task_id=payload.task_id,
                overlap_start=max(item.start_local or payload.start_local, payload.start_local),
                overlap_end=min(item.end_local or payload.end_local, payload.end_local),
                reason="该时间段已被其他规划占用",
            )
            for item in [*board.fixed_tasks]
            if item.id != payload.task_id
            and self._overlaps(
                item.start_local or "",
                item.end_local or "",
                payload.start_local,
                payload.end_local,
            )
        ]
        if conflicts and not payload.allow_parallel:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "SCHEDULE_CONFLICT",
                    "conflicts": [item.model_dump() for item in conflicts],
                },
            )

        parallel_reason = payload.parallel_reason.strip() if payload.parallel_reason else None
        self.session.execute(
            delete(ScheduleParallelConsent).where(
                or_(
                    ScheduleParallelConsent.first_task_id == payload.task_id,
                    ScheduleParallelConsent.second_task_id == payload.task_id,
                )
            )
        )
        if conflicts and payload.allow_parallel:
            for conflict in conflicts:
                other = next(t for t in board.fixed_tasks if t.id == conflict.first_task_id)
                self.session.add(
                    ScheduleParallelConsent(
                        first_task_id=other.id,
                        second_task_id=payload.task_id,
                        scheduled_date=payload.scheduled_date,
                        first_start=other.start_local,
                        first_end=other.end_local,
                        second_start=payload.start_local,
                        second_end=payload.end_local,
                        reason=parallel_reason,
                    )
                )
                existing_manual = self.session.get(ManualTask, conflict.first_task_id)
                if existing_manual is not None:
                    existing_manual.parallel_confirmed = True
                    existing_manual.parallel_reason = parallel_reason
                existing_agent = self.session.get(PlanTask, conflict.first_task_id)
                if existing_agent is not None:
                    existing_block = self.session.scalar(
                        select(TaskTimeBlock).where(
                            TaskTimeBlock.task_id == existing_agent.id,
                            TaskTimeBlock.scheduled_date == payload.scheduled_date,
                            TaskTimeBlock.status != "CANCELLED",
                        )
                    )
                    if existing_block is not None:
                        existing_block.parallel_confirmed = True
                        existing_block.parallel_reason = parallel_reason
        if requested_source == "QUICK":
            task = self.session.get(ManualTask, payload.task_id)
            if task is None:
                raise HTTPException(status_code=404, detail="Manual task not found")
            plan = self.session.get(ManualPlan, task.manual_plan_id)
            if plan is None:
                raise HTTPException(status_code=404, detail="Manual plan not found")
            if task.recurrence != "ONCE":
                override = self.session.scalar(
                    select(ManualTaskDateOverride).where(
                        ManualTaskDateOverride.manual_task_id == task.id,
                        ManualTaskDateOverride.override_date == payload.scheduled_date,
                    )
                )
                if override is None:
                    override = ManualTaskDateOverride(
                        manual_task_id=task.id,
                        override_date=payload.scheduled_date,
                    )
                    self.session.add(override)
                override.start_local = payload.start_local
                override.end_local = payload.end_local
                override.time_overridden = True
            else:
                task.scheduled_date = payload.scheduled_date
                task.start_local = payload.start_local
                task.end_local = payload.end_local
            task.parallel_confirmed = bool(conflicts and payload.allow_parallel)
            task.parallel_reason = parallel_reason
        else:
            task = self.session.get(PlanTask, payload.task_id)
            if task is None:
                raise HTTPException(status_code=404, detail="Agent task not found")
            plan = self.session.get(PlanVersion, task.plan_version_id)
            if plan is None:
                raise HTTPException(status_code=404, detail="Agent plan not found")
            goal = self.session.get(Goal, plan.goal_id)
            if goal is None:
                raise HTTPException(status_code=404, detail="Goal not found")
            existing = self.session.scalar(
                select(TaskTimeBlock).where(
                    TaskTimeBlock.task_id == task.id,
                    TaskTimeBlock.scheduled_date == payload.scheduled_date,
                    TaskTimeBlock.status.in_([SCHEDULE_PLANNED, SCHEDULE_REQUIRES_REVIEW]),
                )
            )
            block = existing or TaskTimeBlock(
                goal_id=goal.id,
                plan_version_id=plan.id,
                plan_day_id=task.plan_day_id,
                task_id=task.id,
                scheduled_date=payload.scheduled_date,
                timezone=goal.timezone,
            )
            block.start_local = payload.start_local
            block.end_local = payload.end_local
            block.source = "USER"
            block.status = SCHEDULE_PLANNED
            if existing is not None:
                block.revision += 1
            block.change_reason = "用户重新确认时间安排"
            block.parallel_confirmed = bool(conflicts and payload.allow_parallel)
            block.parallel_reason = parallel_reason
            if existing is None:
                self.session.add(block)
            self.session.flush()
            self.session.add(
                ScheduleChange(
                    goal_id=goal.id,
                    entity_type="TASK_TIME_BLOCK",
                    entity_id=block.id,
                    change_kind="USER_SCHEDULE",
                    before_json=target.model_dump(mode="json"),
                    after_json=payload.model_dump(mode="json"),
                    reason=parallel_reason or "确认时间安排",
                )
            )

        if commit:
            self.session.commit()
        return TodayTaskScheduleResult(
            scheduled=TodayTaskScheduleRead(
                task_id=payload.task_id,
                source=requested_source,
                scheduled_date=payload.scheduled_date,
                start_local=payload.start_local,
                end_local=payload.end_local,
                parallel_confirmed=bool(conflicts and payload.allow_parallel),
                parallel_reason=parallel_reason,
            ),
            conflicts=conflicts,
        )
