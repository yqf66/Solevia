"""Deterministic allocation over the shared calendar; previews never write."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi import HTTPException
from sqlalchemy import select

from planner_api.dependency_resolver import PlanDependencyResolver
from planner_api.config import get_settings
from planner_api.holiday_calendar import HolidayCalendarService
from planner_api.models import (
    AvailabilityException,
    AvailabilityWindow,
    PlanSchedulePreference,
    PlanTask,
    WorkspaceAvailabilityWindow,
    WorkspaceScheduleProfile,
)
from planner_api.schedule_constraints import (
    confirmed_unavailable_windows,
    shared_unavailable_windows,
)
from planner_api.schedule_profile import routine_windows
from planner_api.schemas import (
    AvailabilityWindowInput,
    DayAllocationApply,
    DayAllocationPreview,
    DayAllocationRequest,
    TodayTaskScheduleRequest,
    UnifiedTodayRead,
)
from planner_api.today_service import TodayService


def minutes(value: str) -> int:
    hour, minute = map(int, value.split(":"))
    return hour * 60 + minute


def clock(value: int) -> str:
    return f"{value // 60:02d}:{value % 60:02d}"


def merge(windows: list[tuple[int, int]]) -> list[tuple[int, int]]:
    result: list[tuple[int, int]] = []
    for start, end in sorted(windows):
        if result and start <= result[-1][1]:
            result[-1] = (result[-1][0], max(end, result[-1][1]))
        elif start < end:
            result.append((start, end))
    return result


def subtract(windows: list[tuple[int, int]], occupied: list[tuple[int, int]]):
    for left, right in occupied:
        windows = [
            piece
            for start, end in windows
            for piece in ((start, min(end, left)), (max(start, right), end))
            if piece[0] < piece[1]
        ]
    return windows


class DayAllocator:
    def __init__(self, session):
        self.session = session
        self.today = TodayService(session)
        self.dependencies = PlanDependencyResolver(session)

    @staticmethod
    def _chinese_number(value: str) -> int | None:
        return PlanDependencyResolver._chinese_number(value)

    def _dependency_indexes(self, plan_version_id: str) -> dict[str, object]:
        if not hasattr(self, "dependencies"):
            self.dependencies = PlanDependencyResolver(self.session)
        return self.dependencies.indexes(plan_version_id)

    def _resolve_dependency(
        self, dependency: object, task: PlanTask, indexes: dict[str, object]
    ) -> PlanTask | None:
        if not hasattr(self, "dependencies"):
            self.dependencies = PlanDependencyResolver(getattr(self, "session", None))
        return self.dependencies.resolve(dependency, task, indexes)

    def preview(
        self,
        request: DayAllocationRequest,
        *,
        board: UnifiedTodayRead | None = None,
        reserved_task_ids: set[str] | None = None,
        availability_goal_ids: set[str] | None = None,
    ) -> DayAllocationPreview:
        self.session.flush()
        board = board or self.today.get_today(request.scheduled_date)
        reserved_task_ids = reserved_task_ids or set()
        now = datetime.now(ZoneInfo(board.timezone))
        if request.scheduled_date < now.date():
            raise HTTPException(422, "不能为过去的日期自动排课")
        profile = self.session.get(WorkspaceScheduleProfile, "default")
        gap = profile.break_minutes if profile else 10
        day = request.scheduled_date

        def valid(window):
            return (
                window.weekday == day.weekday()
                and (window.valid_from is None or window.valid_from <= day)
                and (window.valid_to is None or window.valid_to >= day)
            )

        stored = list(self.session.scalars(select(WorkspaceAvailabilityWindow)))
        selected = request.windows
        availability_source = "REQUEST"
        if not selected and profile and profile.availability_mode == "ROUTINE":
            selected = [
                AvailabilityWindowInput(
                    weekday=day.weekday(),
                    start_local=start,
                    end_local=end,
                )
                for start, end in routine_windows(profile)
            ]
            availability_source = "ROUTINE"
        elif not selected and profile and profile.availability_mode == "CUSTOM":
            selected = stored
            availability_source = "CUSTOM"
        if not selected:
            # Existing plans may have collected availability before workspace settings existed.
            selected = list(self.session.scalars(select(AvailabilityWindow).where(
                AvailabilityWindow.timezone == board.timezone,
                AvailabilityWindow.goal_id.in_({t.plan_id for t in board.flexible_tasks
                                              if t.source == "AGENT"} | (availability_goal_ids or set())),
            )))
            availability_source = "PLAN" if selected else "NONE"
        configured_windows = merge([
            (minutes(w.start_local), minutes(w.end_local)) for w in selected if valid(w)
        ])
        floor = now.hour * 60 + now.minute + 1 if day == now.date() else 0
        expired_windows = sum(end <= floor for start, end in configured_windows)
        trimmed_windows = sum(start < floor < end for start, end in configured_windows)
        windows = [
            (max(start, floor), end)
            for start, end in configured_windows
            if end > floor
        ]
        occupied = [
            (minutes(t.start_local) - gap, minutes(t.end_local) + gap)
            for t in board.fixed_tasks if t.start_local and t.end_local
        ]
        # Confirmed course/lesson facts are hard calendar blocks.  They are
        # kept separate from generic routine exclusions so date and weekday
        # scopes (e.g. weekdays only, through Oct 1) remain accurate.
        course_blocks = shared_unavailable_windows(self.session, day)
        course_goal_ids = {request.goal_id} if request.goal_id else {
            task.plan_id for task in board.flexible_tasks if task.plan_id
        }
        course_goal_ids |= availability_goal_ids or set()
        for course_goal_id in course_goal_ids:
            course_blocks.extend(confirmed_unavailable_windows(self.session, course_goal_id, day))
        course_blocks = sorted(set(course_blocks))
        occupied.extend((start - gap, end + gap) for start, end, _label in course_blocks)
        free = subtract(windows, occupied)
        initial_free = list(free)
        available = sum(end - start for start, end in free)
        preferences = {
            p.goal_id: p for p in self.session.scalars(select(PlanSchedulePreference))
        }
        candidates = [
            t for t in board.flexible_tasks
            if t.status == "NOT_STARTED"
            and (request.goal_id is None or t.plan_id == request.goal_id)
            and (request.manual_plan_id is None or (
                t.source == "QUICK" and t.plan_id == request.manual_plan_id
            ))
            and (request.include_flexible or (
                t.plan_id in preferences and preferences[t.plan_id].mode != "FLEXIBLE"
            ))
        ]
        model_order = {
            task_id: index for index, task_id in enumerate(request.model_task_order)
        }
        priority_mode = get_settings().cross_plan_priority_mode
        if request.goal_id:
            configured = preferences.get(request.goal_id)
            if configured is not None:
                priority_mode = getattr(configured, "cross_plan_priority_mode", priority_mode)

        def plan_sort_key(task):
            deadline = str((task.scheduling_policy_data or {}).get("eligible_to") or "9999-12-31")
            if priority_mode == "DEADLINE":
                return (deadline, -task.priority, task.plan_title, task.title, task.id)
            if priority_mode == "PRIORITY":
                return (-task.priority, deadline, task.plan_title, task.title, task.id)
            return (-task.priority, deadline, task.plan_title, task.title, task.id)

        candidates.sort(key=lambda t: (
            0 if t.id in reserved_task_ids else 1,
            0 if t.source == "AGENT" and t.ai_schedule_requested else (
                1 if t.source == "AGENT" else 2
            ),
            model_order.get(t.id, len(model_order) + 1),
            plan_sort_key(t), t.description, t.estimate_minutes, t.plan_id,
        ))
        base_order = {task.id: index for index, task in enumerate(candidates)}
        fixed_by_id = {task.id: task for task in board.fixed_tasks}
        ordered_candidates = []
        ordered_ids: set[str] = set()
        remaining = {task.id: task for task in candidates}
        while remaining:
            ready = []
            for task in remaining.values():
                raw_task = self.session.get(PlanTask, task.id) if task.source == "AGENT" else None
                if raw_task is None:
                    ready.append(task)
                    continue
                indexes = self._dependency_indexes(raw_task.plan_version_id)
                dependencies_ready = True
                for dependency in raw_task.depends_on_json or []:
                    resolved = self._resolve_dependency(dependency, raw_task, indexes)
                    if resolved is None:
                        dependencies_ready = False
                        break
                    if resolved.status in {"COMPLETED", "CARRIED_OVER"}:
                        continue
                    if resolved.id in ordered_ids or resolved.id in fixed_by_id:
                        continue
                    dependencies_ready = False
                    break
                if dependencies_ready:
                    ready.append(task)
            if not ready:
                ordered_candidates.extend(
                    sorted(remaining.values(), key=lambda item: base_order[item.id])
                )
                break
            next_task = min(ready, key=lambda item: base_order[item.id])
            ordered_candidates.append(next_task)
            ordered_ids.add(next_task.id)
            remaining.pop(next_task.id)
        candidates = ordered_candidates
        assignments = []
        unallocated = []
        unallocated_ids: set[str] = set()
        unallocated_reasons: dict[str, str] = {}

        def mark_unallocated(task, reason: str) -> None:
            if task.id not in unallocated_ids:
                unallocated.append(task)
                unallocated_ids.add(task.id)
            unallocated_reasons[task.id] = reason

        assigned_ids: set[str] = set()
        assigned_end_by_id = {
            task.id: minutes(task.end_local)
            for task in board.fixed_tasks
            if task.start_local and task.end_local
        }
        notes = [] if windows else ["尚无当天可用时间，请先填写可执行时间段。"]
        if course_blocks:
            notes.append("已扣除讨论中确认的课程时间：" + "、".join(
                f"{clock(start)}–{clock(end)} {label}" for start, end, label in course_blocks
            ))
        if expired_windows:
            notes.append(f"已忽略 {expired_windows} 个已经过去的时间段。")
        if trimmed_windows:
            notes.append("当前时间段已从现在之后开始计算。")
        if any(t.timezone != board.timezone for t in board.fixed_tasks):
            free = []
            notes.append("已有固定任务使用不同的时区，请先核对共享日历时区再排课。")
        constraints = []
        for task in candidates:
            allowed = free
            preference = preferences.get(task.plan_id)
            task_windows = list(self.session.scalars(
                select(AvailabilityWindow).where(AvailabilityWindow.goal_id == task.plan_id)
            )) if task.source == "AGENT" else []
            exceptions = list(self.session.scalars(select(AvailabilityException).where(
                AvailabilityException.goal_id == task.plan_id,
                AvailabilityException.exception_date == day,
            ))) if task.source == "AGENT" else []
            raw_task = self.session.get(PlanTask, task.id) if task.source == "AGENT" else None
            dependencies = raw_task.depends_on_json if raw_task else []
            indexes = self._dependency_indexes(raw_task.plan_version_id) if raw_task else {}
            resolved_dependencies = []
            for dependency in dependencies:
                resolved = self._resolve_dependency(dependency, raw_task, indexes)
                fixed = fixed_by_id.get(resolved.id) if resolved is not None else None
                resolved_dependencies.append({
                    "reference": str(dependency),
                    "task_id": resolved.id if resolved is not None else None,
                    "status": resolved.status if resolved is not None else "UNKNOWN",
                    "scheduled_today": bool(fixed),
                    "start_local": fixed.start_local if fixed else None,
                    "end_local": fixed.end_local if fixed else None,
                })
            constraints.append({
                "id": task.id,
                "scheduling_policy": raw_task.scheduling_policy if raw_task else None,
                "scheduling_policy_data": raw_task.scheduling_policy_json if raw_task else {},
                "preference": [preference.mode, preference.break_minutes,
                               preference.timezone] if preference else None,
                "windows": [[w.start_local, w.end_local, str(w.valid_from), str(w.valid_to),
                             w.weekday, w.timezone] for w in task_windows],
                "exceptions": [[e.kind, e.start_local, e.end_local] for e in exceptions],
                "dependencies": resolved_dependencies,
            })
            if task.timezone != board.timezone or any(
                w.timezone != board.timezone for w in task_windows
            ):
                mark_unallocated(task, "TIMEZONE_CONFLICT")
                notes.append(f"{task.title} 的时区与共享日历不同，请先核对时区。")
                continue
            unresolved = []
            dependency_floor = 0
            for dependency in dependencies:
                resolved = self._resolve_dependency(dependency, raw_task, indexes)
                if resolved is None:
                    unresolved.append(dependency)
                    continue
                if resolved.status in {"COMPLETED", "CARRIED_OVER"}:
                    continue
                if resolved.id not in assigned_end_by_id:
                    unresolved.append(dependency)
                    continue
                dependency_floor = max(dependency_floor, assigned_end_by_id[resolved.id] + gap)
            if unresolved:
                mark_unallocated(task, "DEPENDENCY_BLOCKED")
                notes.append(f"{task.title} 有未完成或无法识别的前置要求，需要确认完成顺序后安排。")
                continue
            if task_windows:
                allowed = merge([
                    (max(a, minutes(w.start_local)), min(b, minutes(w.end_local)))
                    for a, b in free for w in task_windows if valid(w)
                ])
            overrides = [e for e in exceptions if e.kind == "OVERRIDE"]
            if overrides:
                allowed = merge([
                    (max(a, minutes(e.start_local)), min(b, minutes(e.end_local)))
                    for a, b in free for e in overrides if e.start_local and e.end_local
                ])
            if dependency_floor:
                allowed = [
                    (max(start, dependency_floor), end)
                    for start, end in allowed
                    if max(start, dependency_floor) < end
                ]
            for exception in exceptions:
                if exception.kind == "UNAVAILABLE":
                    allowed = subtract(allowed, [(
                        minutes(exception.start_local) if exception.start_local else 0,
                        minutes(exception.end_local) if exception.end_local else 1440,
                    )])
            preferred_start = request.model_preferred_starts.get(task.id)
            preferred_minute = minutes(preferred_start) if preferred_start else None
            slot = None
            if preferred_minute is not None:
                if any(a <= preferred_minute and preferred_minute + task.estimate_minutes <= b
                       for a, b in allowed):
                    slot = (preferred_minute, preferred_minute + task.estimate_minutes)
                else:
                    notes.append(
                        f"{task.title} 的建议时间不在当天可用空档内，已按本地规则重新安排。"
                    )
            if slot is None:
                slot = next(((a, a + task.estimate_minutes) for a, b in allowed
                             if b - a >= task.estimate_minutes), None)
            if slot is None:
                mark_unallocated(
                    task,
                    "CONTINUOUS_SLOT_REQUIRED" if sum(b - a for a, b in allowed) >= task.estimate_minutes
                    else "FIXED_CONSTRAINT" if course_blocks or occupied else "CAPACITY_INSUFFICIENT",
                )
                continue
            assignments.append(TodayTaskScheduleRequest(
                source=task.source, task_id=task.id, scheduled_date=day,
                start_local=clock(slot[0]), end_local=clock(slot[1]),
            ))
            assigned_ids.add(task.id)
            assigned_end_by_id[task.id] = slot[1]
            task_gap = max(gap, preference.break_minutes if preference else 0)
            free = subtract(free, [(slot[0] - task_gap, slot[1] + task_gap)])
        if unallocated:
            notes.append("未安排任务继续保留。可以补充可用时间，或逐项选择顺延、拆分或并行。")
        state = {
            "board": board.model_dump(mode="json"), "constraints": constraints,
            "occupied_constraints": course_blocks,
            "windows": windows, "gap": gap,
            "request": request.model_dump(mode="json"),
            "assignments": [a.model_dump(mode="json") for a in assignments],
        }
        fingerprint = hashlib.sha256(json.dumps(
            state, sort_keys=True, ensure_ascii=False,
        ).encode()).hexdigest()
        scheduling_context = {
            "date": day.isoformat(),
            "timezone": board.timezone,
            "holiday_calendar": HolidayCalendarService(self.session).day(day),
            "break_minutes": gap,
            "available_windows": [[clock(start), clock(end)] for start, end in windows],
            "availability_source": availability_source,
            "user_schedule_style": profile.schedule_style if profile else "",
            "occupied_fixed_tasks": [
                {
                    "task_id": task.id,
                    "title": task.title,
                    "start_local": task.start_local,
                    "end_local": task.end_local,
                    "source": task.source,
                }
                for task in board.fixed_tasks
                if task.start_local and task.end_local
            ],
            "occupied_constraints": [
                {
                    "title": label,
                    "start_local": clock(start),
                    "end_local": clock(end),
                    "source": "CONFIRMED_DISCUSSION",
                }
                for start, end, label in course_blocks
            ],
            "free_windows_before_assignment": [
                [clock(start), clock(end)] for start, end in initial_free
            ],
            "tasks": [
                {
                    "task_id": task.id,
                    "source": task.source,
                    "plan_id": task.plan_id,
                    "title": task.title,
                    "estimate_minutes": task.estimate_minutes,
                    "priority": task.priority,
                    "ai_schedule_requested": task.ai_schedule_requested,
                    "scheduling_policy": next(
                        (
                            item["scheduling_policy"]
                            for item in constraints
                            if item["id"] == task.id
                        ),
                        None,
                    ),
                    "scheduling_policy_data": next(
                        (
                            item["scheduling_policy_data"]
                            for item in constraints
                            if item["id"] == task.id
                        ),
                        {},
                    ),
                    "dependencies": next(
                        (item["dependencies"] for item in constraints if item["id"] == task.id),
                        [],
                    ),
                    "allocated": task.id in assigned_ids,
                }
                for task in candidates
            ],
            "priority_order": [task.id for task in candidates],
            "allocation_policy": [
                "保留手动固定任务",
                "优先安排请求 AI 排课的 Agent 任务",
                "其次安排未请求 AI 排课的 Agent 任务",
                "最后尝试自由任务",
                "前置任务未完成时不安排后置任务",
                "用户安排风格只用于同等可行方案间的轻量排序，不得突破硬约束",
            ],
        }
        plan_groups: dict[str, dict[str, object]] = {}
        for task in candidates:
            group = plan_groups.setdefault(
                task.plan_id,
                {
                    "plan_id": task.plan_id,
                    "plan_title": task.plan_title,
                    "required_minutes": 0,
                    "assigned_minutes": 0,
                    "unallocated_minutes": 0,
                    "task_count": 0,
                    "unallocated_task_ids": [],
                },
            )
            group["required_minutes"] = int(group["required_minutes"]) + task.estimate_minutes
            group["task_count"] = int(group["task_count"]) + 1
            if task.id in assigned_ids:
                group["assigned_minutes"] = int(group["assigned_minutes"]) + task.estimate_minutes
            else:
                group["unallocated_minutes"] = int(group["unallocated_minutes"]) + task.estimate_minutes
                group["unallocated_task_ids"].append(task.id)
        competition_summary: list[dict[str, object]] = []
        if len(plan_groups) > 1:
            required = sum(int(group["required_minutes"]) for group in plan_groups.values())
            over_capacity = max(0, required - available)
            competition_summary.append({
                "scheduled_date": day.isoformat(),
                "available_minutes": available,
                "required_minutes": required,
                "assigned_minutes": sum(int(group["assigned_minutes"]) for group in plan_groups.values()),
                "over_capacity_minutes": over_capacity,
                "priority_mode": priority_mode,
                "priority_order": [str(group["plan_id"]) for group in sorted(
                    plan_groups.values(),
                    key=lambda group: (
                        -int(group["assigned_minutes"])
                        if priority_mode == "PRIORITY"
                        else str(group["plan_title"])
                    ),
                )],
                "plans": sorted(plan_groups.values(), key=lambda group: (-int(group["assigned_minutes"]), str(group["plan_title"]))),
                "tradeoffs": [
                    "同一日期的多个规划共享可用时间",
                    "系统先按固定安排、请求智能排课、任务优先级和前置关系排序",
                    "未安排任务保留在原规划中，不会被静默删除",
                ],
            })
            notes.append(
                f"当天有 {len(plan_groups)} 个规划共享时间，已展示各规划占用与未安排取舍。"
            )
        return DayAllocationPreview(
            fingerprint=fingerprint, request=request, timezone=board.timezone,
            assignments=assignments, unallocated=unallocated,
            available_minutes=available,
            required_minutes=sum(t.estimate_minutes for t in candidates), notes=notes,
            assigned_minutes=sum(t.estimate_minutes for t in candidates if t.id in assigned_ids),
            unallocated_minutes=sum(t.estimate_minutes for t in unallocated),
            unallocated_reasons=unallocated_reasons,
            competition_summary=competition_summary,
            strategy=("MODEL_ASSISTED" if request.model_task_order or request.model_preferred_starts
                       else "LOCAL_VALIDATED"),
            scheduling_context=scheduling_context | {
                "model_notes": request.model_notes,
            },
        )

    def apply(self, payload: DayAllocationApply):
        # Acquire a database write lock before revalidating the shared calendar.
        from sqlalchemy import text

        if self.session.bind.dialect.name == "sqlite":
            self.session.execute(text("BEGIN IMMEDIATE"))
        try:
            preview = self.preview(payload.request)
            if preview.fingerprint != payload.fingerprint:
                raise HTTPException(409, "时间表或任务已变化，请重新生成排课预览。")
            for assignment in preview.assignments:
                self.today.schedule_task(assignment, commit=False)
                self.session.flush()
            self.session.commit()
        except Exception:
            self.session.rollback()
            raise
        return self.today.get_today(payload.request.scheduled_date)
