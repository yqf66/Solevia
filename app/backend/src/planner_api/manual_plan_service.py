from __future__ import annotations

from datetime import date, timedelta

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from planner_api.holiday_calendar import HolidayCalendarService
from planner_api.models import (
    ManualPlan,
    ManualTask,
    ManualTaskDateOverride,
    TaskTimeBlock,
    WorkspaceAvailabilityWindow,
    WorkspaceScheduleProfile,
    new_id,
)
from planner_api.schedule_profile import routine_windows
from planner_api.schemas import (
    HolidayCalendarRead,
    ManualPlanCreate,
    ManualPlanRead,
    ManualTaskCreate,
    ManualTaskRead,
    ManualTaskUpdate,
    WorkspaceAvailabilityUpdate,
    WorkspaceAvailabilityWindowRead,
    WorkspaceEffectiveAvailabilityRead,
    WorkspaceScheduleProfileRead,
    WorkspaceScheduleProfileUpdate,
)
from planner_api.today_service import TodayService


class ManualPlanService:
    """CRUD for the user-authored plan entry and shared schedule profile."""

    def __init__(self, session: Session) -> None:
        self.session = session

    @staticmethod
    def _minutes(value: str) -> int:
        hour, minute = map(int, value.split(":"))
        return hour * 60 + minute

    @staticmethod
    def _clock(value: int) -> str:
        return f"{value // 60:02d}:{value % 60:02d}"

    def _first_workspace_slot(
        self,
        *,
        on_date: date,
        duration: int,
        displaced_task_id: str,
        reserved: list[tuple[str, str]],
        additionally_excluded: set[str] | None = None,
    ) -> tuple[str, str] | None:
        """Find the first complete free slot in the user's shared availability."""
        effective = self.get_effective_windows(on_date)
        windows = effective.windows
        if not windows:
            return None
        self.session.flush()
        board = TodayService(self.session).get_today(on_date)
        excluded = {displaced_task_id} | (additionally_excluded or set())
        occupied = [
            (self._minutes(item.start_local), self._minutes(item.end_local))
            for item in board.fixed_tasks
            if item.id not in excluded and item.start_local and item.end_local
        ]
        occupied.extend((self._minutes(start), self._minutes(end)) for start, end in reserved)
        occupied.sort()
        for window in windows:
            cursor = self._minutes(window.start_local)
            window_end = self._minutes(window.end_local)
            for start, end in occupied:
                if end <= cursor or start >= window_end:
                    continue
                if cursor + duration <= start:
                    return self._clock(cursor), self._clock(cursor + duration)
                cursor = max(cursor, end)
            if cursor + duration <= window_end:
                return self._clock(cursor), self._clock(cursor + duration)
        return None

    def _relocate_displaced_task(
        self,
        *,
        task_id: str,
        on_date: date,
        original_start: str,
        original_end: str,
        reserved: list[tuple[str, str]],
        additionally_excluded: set[str] | None = None,
    ) -> tuple[str, str] | None:
        duration = self._minutes(original_end) - self._minutes(original_start)
        slot = self._first_workspace_slot(
            on_date=on_date,
            duration=duration,
            displaced_task_id=task_id,
            reserved=reserved,
            additionally_excluded=additionally_excluded,
        )
        old_manual = self.session.get(ManualTask, task_id)
        if old_manual is not None:
            override = self.session.scalar(
                select(ManualTaskDateOverride).where(
                    ManualTaskDateOverride.manual_task_id == task_id,
                    ManualTaskDateOverride.override_date == on_date,
                )
            )
            if override is None:
                override = ManualTaskDateOverride(manual_task_id=task_id, override_date=on_date)
                self.session.add(override)
            override.start_local = slot[0] if slot else None
            override.end_local = slot[1] if slot else None
            override.time_overridden = True
            old_manual.parallel_reason = (
                f"该日被新固定任务推开，已自动改排至 {slot[0]}–{slot[1]}"
                if slot
                else "该日被新固定任务推开，共享可用时段无完整空档，已转为自由任务"
            )
            return slot
        blocks = list(
            self.session.scalars(
                select(TaskTimeBlock).where(
                    TaskTimeBlock.task_id == task_id,
                    TaskTimeBlock.scheduled_date == on_date,
                    TaskTimeBlock.status != "CANCELLED",
                )
            )
        )
        for block in blocks:
            if slot:
                block.start_local, block.end_local = slot
                block.revision += 1
                block.change_reason = (
                    f"被新固定任务推开，已在共享可用时段改排至 {slot[0]}–{slot[1]}"
                )
            else:
                block.status = "CANCELLED"
                block.change_reason = "被新固定任务推开，共享可用时段无完整空档，已转为自由任务"
        return slot

    def _profile(self) -> WorkspaceScheduleProfile:
        profile = self.session.get(WorkspaceScheduleProfile, "default")
        if profile is None:
            profile = WorkspaceScheduleProfile(id="default")
            self.session.add(profile)
            self.session.flush()
        return profile

    @staticmethod
    def _profile_read(profile: WorkspaceScheduleProfile) -> WorkspaceScheduleProfileRead:
        return WorkspaceScheduleProfileRead(
            id=profile.id,
            timezone=profile.timezone,
            default_mode=profile.default_mode,
            break_minutes=profile.break_minutes,
            availability_mode=profile.availability_mode,
            wake_time=profile.wake_time,
            available_from=profile.available_from,
            breakfast_enabled=profile.breakfast_enabled,
            breakfast_start=profile.breakfast_start,
            breakfast_end=profile.breakfast_end,
            lunch_enabled=profile.lunch_enabled,
            lunch_start=profile.lunch_start,
            lunch_end=profile.lunch_end,
            nap_enabled=profile.nap_enabled,
            nap_start=profile.nap_start,
            nap_end=profile.nap_end,
            dinner_enabled=profile.dinner_enabled,
            dinner_start=profile.dinner_start,
            dinner_end=profile.dinner_end,
            sleep_time=profile.sleep_time,
            routine_exclusions=profile.routine_exclusions_json or [],
            schedule_style=profile.schedule_style,
            holiday_country_code=profile.holiday_country_code,
            holiday_subdivision_code=profile.holiday_subdivision_code,
            holiday_locale=profile.holiday_locale,
            holiday_source=profile.holiday_source,
            holiday_confidence=profile.holiday_confidence,
            holiday_confirmed=profile.holiday_confirmed,
            created_at=profile.created_at,
            updated_at=profile.updated_at,
        )

    @staticmethod
    def _window_read(
        item: WorkspaceAvailabilityWindow, timezone: str
    ) -> WorkspaceAvailabilityWindowRead:
        return WorkspaceAvailabilityWindowRead(
            id=item.id,
            profile_id=item.profile_id,
            weekday=item.weekday,
            start_local=item.start_local,
            end_local=item.end_local,
            valid_from=item.valid_from,
            valid_to=item.valid_to,
            timezone=timezone,
            created_at=item.created_at,
        )

    def get_profile(self) -> WorkspaceScheduleProfileRead:
        profile = self._profile()
        self.session.commit()
        return self._profile_read(profile)

    def get_holidays(self, start_date: date, end_date: date) -> HolidayCalendarRead:
        if end_date < start_date or (end_date - start_date).days > 1095:
            raise HTTPException(
                status_code=422,
                detail="Holiday range must be between 0 and 1095 days",
            )
        profile = self._profile()
        days, missing_years, sources = HolidayCalendarService(self.session).resolve(
            start_date, end_date
        )
        self.session.commit()
        return HolidayCalendarRead(
            country_code=profile.holiday_country_code,
            subdivision_code=profile.holiday_subdivision_code,
            start_date=start_date,
            end_date=end_date,
            days=days,
            coverage=(
                "UNAVAILABLE"
                if not days and missing_years
                else "PARTIAL"
                if missing_years
                else "COMPLETE"
            ),
            missing_years=missing_years,
            sources=sources,
        )

    def update_profile(
        self, payload: WorkspaceScheduleProfileUpdate
    ) -> WorkspaceScheduleProfileRead:
        profile = self._profile()
        values = payload.model_dump(exclude_unset=True)
        if "routine_exclusions" in values:
            profile.routine_exclusions_json = values.pop("routine_exclusions") or []
        for key, value in values.items():
            if value is not None:
                setattr(profile, key, value)
        ordered_pairs = (
            (profile.wake_time, profile.sleep_time, "起床时间必须早于睡觉时间"),
            (profile.available_from, profile.sleep_time, "最早可安排时间必须早于睡觉时间"),
            (profile.breakfast_start, profile.breakfast_end, "早餐结束时间必须晚于开始时间"),
            (profile.lunch_start, profile.lunch_end, "午餐结束时间必须晚于开始时间"),
            (profile.nap_start, profile.nap_end, "午休结束时间必须晚于开始时间"),
            (profile.dinner_start, profile.dinner_end, "晚餐结束时间必须晚于开始时间"),
        )
        for start, end, message in ordered_pairs:
            if start >= end:
                raise HTTPException(status_code=422, detail=message)
        self.session.commit()
        return self._profile_read(profile)

    def replace_windows(
        self, payload: WorkspaceAvailabilityUpdate
    ) -> list[WorkspaceAvailabilityWindowRead]:
        profile = self._profile()
        existing = list(
            self.session.scalars(
                select(WorkspaceAvailabilityWindow).where(
                    WorkspaceAvailabilityWindow.profile_id == profile.id
                )
            )
        )
        for item in existing:
            self.session.delete(item)
        created = [
            WorkspaceAvailabilityWindow(
                profile_id=profile.id,
                weekday=item.weekday,
                start_local=item.start_local,
                end_local=item.end_local,
                valid_from=item.valid_from,
                valid_to=item.valid_to,
            )
            for item in payload.windows
        ]
        self.session.add_all(created)
        profile.availability_mode = "CUSTOM"
        self.session.commit()
        return [self._window_read(item, profile.timezone) for item in created]

    def get_windows(self) -> list[WorkspaceAvailabilityWindowRead]:
        profile = self._profile()
        windows = list(
            self.session.scalars(
                select(WorkspaceAvailabilityWindow)
                .where(WorkspaceAvailabilityWindow.profile_id == profile.id)
                .order_by(
                    WorkspaceAvailabilityWindow.weekday,
                    WorkspaceAvailabilityWindow.start_local,
                )
            )
        )
        self.session.commit()
        return [self._window_read(item, profile.timezone) for item in windows]

    def get_effective_windows(self, on_date: date) -> WorkspaceEffectiveAvailabilityRead:
        profile = self._profile()
        if profile.availability_mode == "ROUTINE":
            windows = [
                {
                    "weekday": on_date.weekday(),
                    "start_local": start,
                    "end_local": end,
                    "valid_from": None,
                    "valid_to": None,
                }
                for start, end in routine_windows(profile)
            ]
            return WorkspaceEffectiveAvailabilityRead(source="ROUTINE", windows=windows)
        stored = list(
            self.session.scalars(
                select(WorkspaceAvailabilityWindow)
                .where(
                    WorkspaceAvailabilityWindow.profile_id == profile.id,
                    WorkspaceAvailabilityWindow.weekday == on_date.weekday(),
                )
                .order_by(WorkspaceAvailabilityWindow.start_local)
            )
        )
        return WorkspaceEffectiveAvailabilityRead(
            source="CUSTOM",
            windows=[
                {
                    "weekday": item.weekday,
                    "start_local": item.start_local,
                    "end_local": item.end_local,
                    "valid_from": item.valid_from,
                    "valid_to": item.valid_to,
                }
                for item in stored
                if (item.valid_from is None or item.valid_from <= on_date)
                and (item.valid_to is None or item.valid_to >= on_date)
            ],
        )

    def reset_windows_to_routine(self) -> WorkspaceEffectiveAvailabilityRead:
        profile = self._profile()
        for item in self.session.scalars(
            select(WorkspaceAvailabilityWindow).where(
                WorkspaceAvailabilityWindow.profile_id == profile.id
            )
        ):
            self.session.delete(item)
        profile.availability_mode = "ROUTINE"
        self.session.commit()
        return self.get_effective_windows(date.today())

    def _task_read(self, item: ManualTask) -> ManualTaskRead:
        overrides = list(
            self.session.scalars(
                select(ManualTaskDateOverride)
                .where(ManualTaskDateOverride.manual_task_id == item.id)
                .order_by(ManualTaskDateOverride.override_date)
            )
        )
        return ManualTaskRead(
            id=item.id,
            manual_plan_id=item.manual_plan_id,
            quick_plan_id=item.manual_plan_id,
            order_in_plan=item.order_in_plan,
            title=item.title,
            description=item.description,
            estimate_minutes=item.estimate_minutes,
            priority=item.priority,
            scheduled_date=item.scheduled_date,
            start_local=item.start_local,
            end_local=item.end_local,
            recurrence=item.recurrence,
            recurrence_weekdays=item.recurrence_weekdays_json,
            recurrence_end_date=item.recurrence_end_date,
            flexible_recurrence=item.recurrence != "ONCE" and item.start_local is None,
            status=item.status,
            created_at=item.created_at,
            updated_at=item.updated_at,
            overrides=[
                {
                    "override_date": value.override_date,
                    "title": value.title,
                    "description": value.description,
                    "estimate_minutes": value.estimate_minutes,
                    "priority": value.priority,
                    "start_local": value.start_local,
                    "end_local": value.end_local,
                    "time_overridden": value.time_overridden,
                    "status": value.status,
                    "is_deleted": value.is_deleted,
                }
                for value in overrides
            ],
        )

    def _plan_read(self, plan: ManualPlan) -> ManualPlanRead:
        tasks = list(
            self.session.scalars(
                select(ManualTask)
                .where(ManualTask.manual_plan_id == plan.id)
                .order_by(ManualTask.order_in_plan)
            )
        )
        return ManualPlanRead(
            id=plan.id,
            title=plan.title,
            description=plan.description,
            status=plan.status,
            timezone=plan.timezone,
            kind=plan.kind,
            arrangement=plan.arrangement_json,
            tasks=[self._task_read(item) for item in tasks],
            created_at=plan.created_at,
            updated_at=plan.updated_at,
        )

    def create_plan(self, payload: ManualPlanCreate, *, commit: bool = True) -> ManualPlanRead:
        fixed_new = [
            task
            for task in payload.tasks
            if task.start_local and task.end_local and task.scheduled_date
        ]
        conflicts = []
        for incoming in fixed_new:
            # The unified board also includes Agent-generated fixed blocks.
            days = [incoming.scheduled_date]
            if incoming.recurrence == "DAILY":
                days = [incoming.scheduled_date + timedelta(days=i) for i in range(31)]
            elif incoming.recurrence == "WEEKLY":
                days = [
                    incoming.scheduled_date + timedelta(days=i)
                    for i in range(31)
                    if (incoming.scheduled_date + timedelta(days=i)).weekday()
                    in incoming.recurrence_weekdays
                ]
            for day in days:
                if incoming.recurrence_end_date and day > incoming.recurrence_end_date:
                    continue
                board = TodayService(self.session).get_today(day)
                for old in board.fixed_tasks:
                    if (
                        old.start_local
                        and old.end_local
                        and incoming.start_local < old.end_local
                        and old.start_local < incoming.end_local
                    ):
                        item = {
                            "date": day.isoformat(),
                            "new_title": incoming.title,
                            "new_start": incoming.start_local,
                            "new_end": incoming.end_local,
                            "existing_task_id": old.id,
                            "existing_title": old.title,
                            "existing_start": old.start_local,
                            "existing_end": old.end_local,
                        }
                        if not any(
                            x["date"] == item["date"]
                            and x["existing_task_id"] == item["existing_task_id"]
                            for x in conflicts
                        ):
                            conflicts.append(item)
        if conflicts and payload.conflict_mode == "NONE":
            raise HTTPException(
                409, detail={"code": "MANUAL_PLAN_CONFLICT", "conflicts": conflicts}
            )
        if conflicts and payload.conflict_mode == "PUSH":
            reserved_by_date: dict[str, list[tuple[str, str]]] = {}
            displaced_by_date: dict[str, set[str]] = {}
            for conflict in conflicts:
                reserved = reserved_by_date.setdefault(conflict["date"], [])
                new_slot = (conflict["new_start"], conflict["new_end"])
                if new_slot not in reserved:
                    reserved.append(new_slot)
                displaced_by_date.setdefault(conflict["date"], set()).add(
                    conflict["existing_task_id"]
                )
            for conflict in conflicts:
                slot = self._relocate_displaced_task(
                    task_id=conflict["existing_task_id"],
                    on_date=date.fromisoformat(conflict["date"]),
                    original_start=conflict["existing_start"],
                    original_end=conflict["existing_end"],
                    reserved=reserved_by_date[conflict["date"]],
                    additionally_excluded=displaced_by_date[conflict["date"]],
                )
                if slot:
                    reserved_by_date[conflict["date"]].append(slot)
        plan = ManualPlan(
            id=new_id(),
            title=payload.title,
            description=payload.description,
            timezone=payload.timezone,
        )
        self.session.add(plan)
        self.session.flush()
        for order, task in enumerate(payload.tasks, start=1):
            self.session.add(
                ManualTask(
                    manual_plan_id=plan.id,
                    order_in_plan=order,
                    title=task.title,
                    description=task.description,
                    estimate_minutes=task.estimate_minutes,
                    priority=task.priority,
                    scheduled_date=task.scheduled_date,
                    start_local=task.start_local,
                    end_local=task.end_local,
                    recurrence=task.recurrence,
                    recurrence_weekdays_json=task.recurrence_weekdays,
                    recurrence_end_date=task.recurrence_end_date,
                    parallel_confirmed=bool(conflicts and payload.conflict_mode == "PARALLEL"),
                    parallel_reason="用户确认与现有任务并行"
                    if conflicts and payload.conflict_mode == "PARALLEL"
                    else None,
                )
            )
        if commit:
            self.session.commit()
        else:
            self.session.flush()
        return self._plan_read(plan)

    def list_plans(self) -> list[ManualPlanRead]:
        plans = list(
            self.session.scalars(select(ManualPlan).order_by(ManualPlan.updated_at.desc()))
        )
        return [self._plan_read(plan) for plan in plans]

    def get_plan(self, plan_id: str) -> ManualPlanRead:
        plan = self.session.get(ManualPlan, plan_id)
        if plan is None:
            raise HTTPException(status_code=404, detail="Quick plan not found")
        return self._plan_read(plan)

    def add_light_task(self, plan_id: str, payload: ManualTaskCreate) -> ManualPlanRead:
        plan = self.session.get(ManualPlan, plan_id)
        if plan is None or plan.kind != "QUICK":
            raise HTTPException(404, "Quick plan not found")
        self._ensure_editable(plan)
        current = self._plan_read(plan)
        if len(current.tasks) >= 30:
            raise HTTPException(422, "一份轻量安排最多包含 30 项任务。")
        task_data = self._validate_light_task(plan, payload.model_dump())
        task = ManualTask(
            manual_plan_id=plan.id,
            order_in_plan=max((item.order_in_plan for item in current.tasks), default=0) + 1,
            **{
                key: value
                for key, value in task_data.items()
                if key not in {"flexible_recurrence", "recurrence_weekdays"}
            },
            recurrence_weekdays_json=task_data["recurrence_weekdays"],
        )
        self.session.add(task)
        self.session.commit()
        return self._plan_read(plan)

    def _validate_light_task(self, plan: ManualPlan, data: dict, task_id: str | None = None):
        from planner_api.light_arrangement_ai import LightArrangementProvider
        from planner_api.light_schemas import LightDraftCreate, LightDraftResult, LightTaskDraft
        from planner_api.schedule_constraints import shared_unavailable_windows

        arrangement = plan.arrangement_json or {}
        task_start = data.get("scheduled_date") or arrangement.get("start_date")
        if task_start is None:
            raise HTTPException(422, "任务需要一个开始日期。")
        has_bounded_horizon = bool(
            arrangement.get("start_date") and arrangement.get("horizon_days")
        )
        request = LightDraftCreate(
            raw_input=plan.description or plan.title,
            timezone=plan.timezone,
            start_date=task_start,
            horizon_days=int(arrangement.get("horizon_days", 14)),
        )
        try:
            candidate = LightTaskDraft.model_validate(data)
            end = request.start_date + timedelta(days=request.horizon_days - 1)
            if (
                has_bounded_horizon
                and candidate.recurrence != "ONCE"
                and candidate.recurrence_end_date is None
            ):
                candidate = candidate.model_copy(update={"recurrence_end_date": end})
            if has_bounded_horizon:
                LightArrangementProvider._validate_scope(
                    request,
                    LightDraftResult(title=plan.title, tasks=[candidate]),
                )
        except ValueError as error:
            raise HTTPException(422, "任务日期、耗时或时间范围不合法。") from error
        for offset in range(request.horizon_days):
            day = request.start_date + timedelta(days=offset)
            if not candidate.start_local or not candidate.scheduled_date:
                continue
            if day < candidate.scheduled_date or (
                candidate.recurrence_end_date and day > candidate.recurrence_end_date
            ):
                continue
            if candidate.recurrence == "ONCE" and day != candidate.scheduled_date:
                continue
            if (
                candidate.recurrence == "WEEKLY"
                and day.weekday() not in candidate.recurrence_weekdays
            ):
                continue
            board = TodayService(self.session).get_today(day)
            occupied = [
                (item.start_local, item.end_local)
                for item in board.fixed_tasks
                if item.id != task_id
            ]
            occupied += [
                (f"{start // 60:02d}:{start % 60:02d}", f"{end // 60:02d}:{end % 60:02d}")
                for start, end, _ in shared_unavailable_windows(self.session, day)
            ]
            if any(
                candidate.start_local < finish and begin < candidate.end_local
                for begin, finish in occupied
            ):
                raise HTTPException(409, "这段时间已有安排，请修改时间或保持自由安排。")
        return candidate.model_dump()

    def update_task(self, plan_id: str, task_id: str, payload: ManualTaskUpdate) -> ManualPlanRead:
        self._ensure_editable(self.session.get(ManualPlan, plan_id))
        task = self.session.get(ManualTask, task_id)
        if task is None or task.manual_plan_id != plan_id:
            raise HTTPException(404, "Manual task not found")
        values = payload.model_dump(exclude_unset=True)
        mode = values.pop("conflict_mode", "NONE")
        scope = values.pop("scope", "FUTURE")
        if any(
            values.get(key) is None
            for key in values
            if key in {"title", "description", "estimate_minutes", "priority", "status"}
        ):
            raise HTTPException(422, "任务属性不能为空。")
        effective_start = values.get("start_local", task.start_local)
        effective_end = values.get("end_local", task.end_local)
        if (effective_start is None) != (effective_end is None):
            raise HTTPException(422, "开始和结束时间必须一起设置。")
        if effective_start and effective_start >= effective_end:
            raise HTTPException(422, "Task end must be later than its start")
        plan = self.session.get(ManualPlan, plan_id)
        if plan.kind == "QUICK":
            if (
                task.recurrence != "ONCE"
                and scope == "TODAY"
                and (
                    not values.get("scheduled_date")
                    or not TodayService._manual_task_occurs_on(task, values["scheduled_date"])
                )
            ):
                raise HTTPException(422, "这一天没有该重复活动，请选择生效日期。")
            if task.recurrence != "ONCE" and scope == "FUTURE" and "status" in values:
                raise HTTPException(422, "请按日期完成或跳过重复活动。")
            candidate = self._task_read(task).model_dump(include=set(ManualTaskCreate.model_fields))
            candidate["flexible_recurrence"] = True
            candidate.update({key: value for key, value in values.items() if key != "status"})
            if scope == "TODAY" and task.recurrence != "ONCE":
                # A date override validates one instance, not the entire series.
                candidate.update(recurrence="ONCE", recurrence_end_date=None)
            self._validate_light_task(plan, candidate, task_id)
        elif (
            task.recurrence != "ONCE" and values.get("scheduled_date", task.scheduled_date) is None
        ):
            raise HTTPException(422, "重复任务需要开始日期。")
        elif task.recurrence != "ONCE" and scope == "FUTURE" and effective_start is None:
            raise HTTPException(422, "固定重复任务需要固定时段。")
        conflicts = []
        target_date = values.get("scheduled_date", task.scheduled_date)
        if values.get("start_local") and target_date:
            board = TodayService(self.session).get_today(target_date)
            conflicts = [
                old
                for old in board.fixed_tasks
                if old.id != task_id
                and old.start_local
                and old.end_local
                and effective_start < old.end_local
                and old.start_local < effective_end
            ]
            if conflicts and mode == "NONE":
                raise HTTPException(
                    409,
                    {
                        "code": "MANUAL_PLAN_CONFLICT",
                        "conflicts": [
                            {
                                "date": target_date.isoformat(),
                                "existing_task_id": old.id,
                                "existing_title": old.title,
                                "existing_start": old.start_local,
                                "existing_end": old.end_local,
                            }
                            for old in conflicts
                        ],
                    },
                )
            if conflicts and mode == "PUSH":
                reserved = [(effective_start, effective_end)]
                displaced = {old.id for old in conflicts}
                for old in conflicts:
                    slot = self._relocate_displaced_task(
                        task_id=old.id,
                        on_date=target_date,
                        original_start=old.start_local,
                        original_end=old.end_local,
                        reserved=reserved,
                        additionally_excluded=displaced | {task_id},
                    )
                    if slot:
                        reserved.append(slot)
            task.parallel_confirmed = bool(conflicts and mode == "PARALLEL")
            task.parallel_reason = (
                "用户确认与现有任务并行" if conflicts and mode == "PARALLEL" else None
            )
        if scope == "TODAY" and values.get("scheduled_date"):
            override = self.session.scalar(
                select(ManualTaskDateOverride).where(
                    ManualTaskDateOverride.manual_task_id == task_id,
                    ManualTaskDateOverride.override_date == values["scheduled_date"],
                )
            )
            if override is None:
                override = ManualTaskDateOverride(
                    manual_task_id=task_id, override_date=values["scheduled_date"]
                )
                self.session.add(override)
            for key in (
                "title",
                "description",
                "estimate_minutes",
                "priority",
                "start_local",
                "end_local",
                "status",
            ):
                if key in values:
                    setattr(override, key, values[key])
            if "start_local" in values or "end_local" in values:
                override.time_overridden = True
        else:
            effective_date = values.get("scheduled_date")
            if (
                task.recurrence != "ONCE"
                and effective_date
                and task.scheduled_date
                and effective_date > task.scheduled_date
            ):
                original_end = task.recurrence_end_date
                task.recurrence_end_date = effective_date - timedelta(days=1)
                successor = ManualTask(
                    manual_plan_id=task.manual_plan_id,
                    order_in_plan=task.order_in_plan,
                    title=values.get("title", task.title),
                    description=values.get("description", task.description),
                    estimate_minutes=values.get("estimate_minutes", task.estimate_minutes),
                    priority=values.get("priority", task.priority),
                    scheduled_date=effective_date,
                    recurrence=task.recurrence,
                    recurrence_weekdays_json=task.recurrence_weekdays_json,
                    recurrence_end_date=original_end,
                    start_local=values.get("start_local", task.start_local),
                    end_local=values.get("end_local", task.end_local),
                    status=values.get("status", "NOT_STARTED"),
                )
                self.session.add(successor)
                self.session.flush()
                for override in self.session.scalars(
                    select(ManualTaskDateOverride).where(
                        ManualTaskDateOverride.manual_task_id == task.id,
                        ManualTaskDateOverride.override_date >= effective_date,
                    )
                ):
                    override.manual_task_id = successor.id
            else:
                for key, value in values.items():
                    setattr(task, key, value)
        self.session.commit()
        return self._plan_read(self.session.get(ManualPlan, plan_id))

    def delete_task(
        self, plan_id: str, task_id: str, scope: str = "FUTURE", override_date: date | None = None
    ) -> ManualPlanRead:
        self._ensure_editable(self.session.get(ManualPlan, plan_id))
        task = self.session.get(ManualTask, task_id)
        if task is None or task.manual_plan_id != plan_id:
            raise HTTPException(404, "Manual task not found")
        if scope == "TODAY" and override_date:
            override = self.session.scalar(
                select(ManualTaskDateOverride).where(
                    ManualTaskDateOverride.manual_task_id == task_id,
                    ManualTaskDateOverride.override_date == override_date,
                )
            )
            if override is None:
                override = ManualTaskDateOverride(
                    manual_task_id=task_id, override_date=override_date
                )
                self.session.add(override)
            override.is_deleted = True
        else:
            if (
                override_date
                and task.recurrence != "ONCE"
                and task.scheduled_date
                and override_date > task.scheduled_date
            ):
                task.recurrence_end_date = override_date - timedelta(days=1)
            else:
                self.session.delete(task)
        self.session.commit()
        return self._plan_read(self.session.get(ManualPlan, plan_id))

    @staticmethod
    def _ensure_editable(plan: ManualPlan | None) -> None:
        if plan is None:
            raise HTTPException(404, "Quick plan not found")
        if (plan.arrangement_json or {}).get("handoff_at"):
            raise HTTPException(409, "原安排已升级并归档，请在完整规划中调整，历史记录只读。")
