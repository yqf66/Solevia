"""Read-only candidate projection validated against the existing shared allocator."""

import hashlib
import json
import logging
from datetime import date, timedelta
from zoneinfo import ZoneInfo

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from planner_api.ai import ProviderConfigurationError, ProviderUnavailableError
from planner_api.config import get_settings
from planner_api.model_telemetry import record_decision
from planner_api.recommendation_ai import (
    PROMPT_VERSION,
    RecommendationProvider,
    recommendation_policy_fingerprint,
)

from planner_api.day_allocation import DayAllocator, minutes, subtract
from planner_api.execution_service import ExecutionService
from planner_api.plan_calendar import plan_week_range
from planner_api.models import (
    PlanDay,
    PlanDaySchedule,
    PlanWorkItem,
    ProgressEvent,
    RecoveryOperation,
    WorkItemWindow,
)
from planner_api.recommendation_schemas import (
    RecommendationBlocked,
    RecommendationCapacity,
    RecommendationItem,
    RecommendationPreview,
    RecommendationRequest,
)
from planner_api.schemas import DayAllocationRequest, UnifiedTodayTaskRead
from planner_api.today_service import TodayService

POLICIES = {"FLOATING", "DATE_WINDOW", "CADENCE", "QUOTA"}
ENDED = {"COMPLETED", "CANCELLED", "CARRIED_OVER"}
logger = logging.getLogger(__name__)


class RecommendationService:
    def __init__(self, session):
        self.session = session
        self.execution = ExecutionService(session)
        self.allocator = DayAllocator(session)

    def preview(self, goal_id: str, request: RecommendationRequest) -> RecommendationPreview:
        if request.use_ai:
            return self._ai_preview(goal_id, request)
        return self._rules_preview(goal_id, request)

    def _ai_preview(self, goal_id, request):
        rules_request = request.model_copy(update={"use_ai": False})
        baseline = self._rules_preview(goal_id, rules_request)
        pool = self._rules_preview(goal_id, rules_request, candidate_pool=True)
        settings = get_settings()
        state = {
            "prompt": PROMPT_VERSION,
            "baseline": baseline.fingerprint,
            "pool": pool.fingerprint,
            "request": request.model_dump(mode="json"),
            "provider": settings.llm_provider,
            "model": settings.llm_model,
            "endpoint": settings.llm_base_url,
            "model_policy": recommendation_policy_fingerprint(settings),
        }
        fingerprint = hashlib.sha256(json.dumps(state, sort_keys=True).encode()).hexdigest()
        key = "recommendation-ai:" + fingerprint
        existing = self.session.scalar(
            select(RecoveryOperation).where(RecoveryOperation.idempotency_key == key)
        )
        retry_operation_id = existing.id if existing else None
        if existing and existing.preview_json.get("strategy") != "AI_FALLBACK":
            record_decision(settings, "recommendation.preview", "cache", "HIT")
            return RecommendationPreview.model_validate(existing.preview_json)
        record_decision(settings, "recommendation.preview", "cache", "MISS")
        # Close the read transaction before waiting on the external provider.
        candidates = [item.model_dump(mode="json") for item in pool.items]
        for candidate in candidates:
            candidate["title"] = candidate["title"][:300]
            candidate["reason"] = candidate["reason"][:300]
            candidate["priority"] = self._candidate_priorities[candidate["task_id"]]
        break_minutes = self._break_minutes
        self.session.rollback()
        try:
            if not candidates:
                record_decision(settings, "recommendation.preview", "fallback", "EMPTY_CANDIDATES")
                result = baseline.model_copy(deep=True)
                result.warnings.append("当前没有符合条件的候选，未请求模型。")
            else:
                model = RecommendationProvider(settings).rank(
                    candidates,
                    request.instruction,
                    request.max_items,
                    capacity={
                        "spare_minutes": baseline.capacity.spare_minutes,
                        "date": str(baseline.date),
                        "break_minutes": break_minutes,
                    },
                )
                ids = [selection.task_id for selection in model.selections]
                allowed = {item["task_id"] for item in candidates}
                if (
                    len(ids) > request.max_items
                    or len(ids) != len(set(ids))
                    or not set(ids) <= allowed
                ):
                    raise ValueError("模型选择的任务不在候选内或重复")
                current = self._rules_preview(goal_id, rules_request, candidate_pool=True)
                if current.fingerprint != pool.fingerprint:
                    raise HTTPException(409, "任务或可用时间已变化，请刷新后重新推荐")
                result = self._rules_preview(goal_id, rules_request, selected_ids=ids)
                if {item.task_id for item in result.items} != set(ids):
                    raise ValueError("模型选择组合超出可执行容量")
                reasons = {selection.task_id: selection.rationale for selection in model.selections}
                result.items.sort(key=lambda item: ids.index(item.task_id))
                for item in result.items:
                    item.reason = reasons[item.task_id]
                result.strategy = "AI"
                result.summary = model.summary
        except (ProviderConfigurationError, ProviderUnavailableError, ValueError) as error:
            record_decision(settings, "recommendation.preview", "fallback", "FALLBACK")
            logger.warning(
                "Recommendation fallback: %s (%s)",
                type(error).__name__,
                getattr(error, "diagnostic", None) or str(error),
            )
            # Recalculate after any wait; never present an old baseline as current.
            result = self._rules_preview(goal_id, rules_request).model_copy(deep=True)
            result.strategy = "AI_FALLBACK"
            result.warnings.append("智能推荐未完成或结果未通过校验，已显示规则建议。")
        result.id = ""
        result.fingerprint = fingerprint
        operation = (
            self.session.get(RecoveryOperation, retry_operation_id)
            if retry_operation_id
            else RecoveryOperation(
                goal_id=goal_id,
                source_plan_version_id=self.execution._goal_and_plan(goal_id)[1].id,
                mode="TASK_RECOMMENDATION",
                idempotency_key=key,
            )
        )
        self.session.add(operation)
        try:
            self.session.flush()
            result.id = operation.id
            operation.preview_json = result.model_dump(mode="json")
            self.session.commit()
        except IntegrityError:
            self.session.rollback()
            existing = self.session.scalar(
                select(RecoveryOperation).where(RecoveryOperation.idempotency_key == key)
            )
            if existing is None:
                raise
            return RecommendationPreview.model_validate(existing.preview_json)
        return result

    def _rules_preview(
        self,
        goal_id: str,
        request: RecommendationRequest,
        *,
        candidate_pool=False,
        selected_ids=None,
    ) -> RecommendationPreview:
        goal, plan = self.execution._goal_and_plan(goal_id)
        today = self.execution._today_for(goal)
        if request.date is not None and request.date != today:
            raise HTTPException(422, "今日建议只针对当前日期；其他日期请使用日历排程预览")
        if goal.status != "ACTIVE":
            raise HTTPException(409, "请先恢复规划，再查看今日建议")
        calendar = self.execution.get_calendar(goal_id)
        activation = self.execution._activation(plan.id)
        tasks = self.execution._tasks(plan.id)
        task_by_id = {task.id: task for task in tasks}
        self._candidate_priorities = {task.id: task.priority for task in tasks}
        schedules = list(
            self.session.scalars(
                select(PlanDaySchedule).where(
                    PlanDaySchedule.activation_id == activation.id,
                )
            )
        )
        dates = {row.plan_day_id: row.scheduled_date for row in schedules}
        days = list(self.session.scalars(select(PlanDay).where(PlanDay.plan_version_id == plan.id)))
        day_by_id = {row.id: row for row in days}
        events = list(
            self.session.scalars(
                select(ProgressEvent).where(
                    ProgressEvent.plan_version_id == plan.id,
                )
            )
        )
        commitments = self.execution._commitment_progress(
            today, plan, schedules, days, tasks, events
        )
        entries = [(day, task) for day in calendar.days for task in day.tasks]
        current_groups = {
            task.recommendation_group_key
            for day, task in entries
            if day.scheduled_date == today
            and task.status not in ENDED
            and task.recommendation_group_key
        }
        indexes = self.allocator._dependency_indexes(plan.id)
        blocked = []
        eligible = []
        suppressed_groups = set()
        for day, projected in entries:
            task = task_by_id[projected.id]
            if task.scheduling_policy not in POLICIES or task.status in ENDED:
                continue
            if day.scheduled_date == today:
                continue
            current_group = projected.recommendation_group_key in current_groups
            if current_group:
                if projected.recommendation_group_key in suppressed_groups:
                    continue
                suppressed_groups.add(projected.recommendation_group_key)
            elif not projected.is_recommendation_primary:
                continue
            reason = None
            if task.status != "NOT_STARTED":
                reason = ("TASK_NOT_READY", "任务已开始或受阻，请从原执行日处理")
            elif current_group:
                reason = ("CURRENT_SERIES_PENDING", "今天已有同组任务，请先完成当前项")
            eligibility = self._eligibility(
                goal,
                plan,
                activation,
                task,
                today,
                tasks,
                dates,
                day_by_id,
                events,
                projected.recommendation_group_key,
                {t.id: t.recommendation_group_key for _, t in entries},
            )
            if reason is None:
                reason = eligibility.get("blocked")
            if reason is None:
                for reference in task.depends_on_json or []:
                    predecessor = self.allocator._resolve_dependency(reference, task, indexes)
                    if predecessor is None:
                        reason = ("UNRESOLVED_DEPENDENCY", f"待确认前置条件：{reference}")
                        break
                    if predecessor.status != "COMPLETED":
                        reason = ("DEPENDENCY_BLOCKED", f"需先完成：{predecessor.title}")
                        break
            if reason is None:
                for progress in commitments:
                    if (
                        progress.label in (task.satisfies_commitments_json or [])
                        and progress.cadence != "PER_EXECUTION_DAY"
                        and (
                            progress.period_start is None
                            or day.scheduled_date >= progress.period_start
                        )
                        and (
                            progress.period_end is None or day.scheduled_date <= progress.period_end
                        )
                        and progress.remaining_occurrences <= 0
                    ):
                        reason = ("QUOTA_SATISFIED", f"“{progress.label}”当前要求已达标")
                        break
            if reason:
                blocked.append(
                    RecommendationBlocked(
                        task_id=task.id,
                        title=task.title,
                        scheduled_date=day.scheduled_date,
                        reason_code=reason[0],
                        reason=reason[1],
                    )
                )
                continue
            due = eligibility.get("eligible_to")
            eligible.append(
                RecommendationItem(
                    task_id=task.id,
                    title=task.title,
                    scheduling_policy=task.scheduling_policy,
                    scheduled_date=day.scheduled_date,
                    estimate_minutes=task.estimate_minutes,
                    group_key=projected.recommendation_group_key,
                    eligible_from=eligibility.get("eligible_from"),
                    eligible_to=due,
                    remaining_value=eligibility.get("remaining_value"),
                    target_unit=eligibility.get("target_unit"),
                    reason=(
                        f"允许窗口至 {due}，可结合今天余量推进。"
                        if due
                        else "前置条件已完成，可结合今天余量推进。"
                    ),
                    allowed_actions=["ARRANGE_TODAY", "RESCHEDULE", "VIEW_DATE"]
                    if task.scheduling_policy == "FLOATING"
                    else ["RESCHEDULE", "VIEW_DATE"],
                )
            )
        eligible.sort(
            key=lambda item: (
                item.eligible_to or activation.projected_end_date,
                -task_by_id[item.task_id].priority,
                item.scheduled_date,
                item.task_id,
            )
        )
        if selected_ids is not None:
            eligible = [item for item in eligible if item.task_id in selected_ids]
            eligible.sort(key=lambda item: selected_ids.index(item.task_id))
        board = TodayService(self.session).get_today(today)
        # In-progress tasks without a slot still reserve effort before extra work.
        board = board.model_copy(
            update={
                "flexible_tasks": [
                    t.model_copy(update={"status": "NOT_STARTED"})
                    if t.status == "IN_PROGRESS"
                    else t
                    for t in board.flexible_tasks
                ]
            }
        )
        reserved = {t.id for t in board.flexible_tasks if t.status == "NOT_STARTED"}
        allocation_request = DayAllocationRequest(scheduled_date=today, use_ai=False)
        baseline = self.allocator.preview(
            allocation_request,
            board=board,
            reserved_task_ids=reserved,
            availability_goal_ids={goal.id},
        )
        warnings = []
        if baseline.unallocated:
            warnings.append("今天已有任务尚无可用时段，先处理当前负荷或补充可用时间。")
        free = [
            tuple(map(minutes, pair))
            for pair in baseline.scheduling_context["free_windows_before_assignment"]
        ]
        gap = baseline.scheduling_context["break_minutes"]
        self._break_minutes = gap
        for assignment in baseline.assignments:
            free = subtract(
                free, [(minutes(assignment.start_local) - gap, minutes(assignment.end_local) + gap)]
            )
        spare = sum(end - start for start, end in free)
        selected = []
        remaining_free = free
        trial_fingerprints = []
        for item in eligible:
            if len(trial_fingerprints) >= 12:
                blocked.append(
                    RecommendationBlocked(
                        task_id=item.task_id,
                        title=item.title,
                        scheduled_date=item.scheduled_date,
                        reason_code="CANDIDATE_LIMIT",
                        reason="本次先检查最紧迫的任务，完成后再刷新建议。",
                    )
                )
                continue
            task = task_by_id[item.task_id]
            projection = UnifiedTodayTaskRead(
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
                timezone=goal.timezone,
                is_fixed=False,
                scheduling_policy=task.scheduling_policy,
                scheduling_policy_data=task.scheduling_policy_json or {},
            )
            trial_board = board.model_copy(
                update={
                    "flexible_tasks": [
                        *board.flexible_tasks,
                        *(p for _, p in selected if not candidate_pool),
                        projection,
                    ]
                }
            )
            trial = self.allocator.preview(
                allocation_request,
                board=trial_board,
                reserved_task_ids=reserved,
                availability_goal_ids={goal.id},
            )
            trial_fingerprints.append(trial.fingerprint)
            proposed_ids = ({p.id for _, p in selected} if not candidate_pool else set()) | {
                task.id
            }
            assigned = {row.task_id for row in trial.assignments}
            code = trial.unallocated_reasons.get(task.id, "CAPACITY_INSUFFICIENT")
            if (
                baseline.unallocated
                or not proposed_ids <= assigned
                or (not candidate_pool and len(selected) >= request.max_items)
            ):
                reason = "今天余量不足，建议先完成已安排的任务。"
                if len(selected) >= request.max_items:
                    code, reason = (
                        "RECOMMENDATION_LIMIT",
                        "本次先推荐少量任务，完成后再查看下一项。",
                    )
                elif code == "CAPACITY_INSUFFICIENT" and task.estimate_minutes <= spare:
                    code, reason = (
                        "CONTINUOUS_SLOT_REQUIRED",
                        "剩余时间分散，缺少足够长的连续空档。",
                    )
                elif code == "FIXED_CONSTRAINT":
                    reason = "当前空档受固定占用或课程限制，无法容纳这项任务。"
                elif code == "TIMEZONE_CONFLICT":
                    reason = "任务时区与共享日历不一致，请先核对时区。"
                blocked.append(
                    RecommendationBlocked(
                        task_id=task.id,
                        title=task.title,
                        scheduled_date=item.scheduled_date,
                        reason_code=code,
                        reason=reason,
                    )
                )
            else:
                selected.append((item, projection))
                remaining_free = [
                    tuple(map(minutes, pair))
                    for pair in trial.scheduling_context["free_windows_before_assignment"]
                ]
                for assignment in trial.assignments:
                    remaining_free = subtract(
                        remaining_free,
                        [
                            (
                                minutes(assignment.start_local) - gap,
                                minutes(assignment.end_local) + gap,
                            )
                        ],
                    )
        state = {
            "version": "recommendation-rules-v1",
            "candidate_pool": candidate_pool,
            "selected_ids": selected_ids,
            "goal_id": goal.id,
            "plan_id": plan.id,
            "calendar": calendar.model_dump(mode="json"),
            "request": request.model_dump(mode="json"),
            "baseline": baseline.fingerprint,
            "trials": trial_fingerprints,
            "events": [[e.id, e.event_type, e.metadata_json, str(e.occurred_at)] for e in events],
            "items": [item.model_dump(mode="json") for item, _ in selected],
            "blocked": [item.model_dump(mode="json") for item in blocked],
        }
        fingerprint = hashlib.sha256(json.dumps(state, sort_keys=True).encode()).hexdigest()
        key = "recommendation:" + fingerprint
        existing = self.session.scalar(
            select(RecoveryOperation).where(
                RecoveryOperation.idempotency_key == key,
            )
        )
        if existing:
            return RecommendationPreview.model_validate(existing.preview_json)
        result = RecommendationPreview(
            id="",
            fingerprint=fingerprint,
            date=today,
            capacity=RecommendationCapacity(
                available_minutes=baseline.available_minutes,
                current_task_minutes=baseline.assigned_minutes,
                recommended_minutes=sum(item.estimate_minutes for item, _ in selected),
                spare_minutes=spare,
                remaining_minutes=sum(end - start for start, end in remaining_free),
            ),
            items=[item for item, _ in selected],
            blocked_summary=blocked[:50],
            blocked_count=len(blocked),
            warnings=warnings,
        )
        if candidate_pool or selected_ids is not None:
            return result
        operation = RecoveryOperation(
            goal_id=goal.id,
            source_plan_version_id=plan.id,
            mode="TASK_RECOMMENDATION",
            idempotency_key=key,
        )
        self.session.add(operation)
        try:
            self.session.flush()
            result.id = operation.id
            operation.preview_json = result.model_dump(mode="json")
            self.session.commit()
        except IntegrityError:
            self.session.rollback()
            existing = self.session.scalar(
                select(RecoveryOperation).where(
                    RecoveryOperation.idempotency_key == key,
                )
            )
            if existing is None:
                raise
            return RecommendationPreview.model_validate(existing.preview_json)
        return result

    def _eligibility(
        self, goal, plan, activation, task, today, tasks, dates, days, events, group_key, group_keys
    ):
        window = (
            self.session.get(WorkItemWindow, task.work_item_window_id)
            if task.work_item_window_id
            else None
        )
        item = (
            self.session.get(PlanWorkItem, task.source_work_item_id)
            if task.source_work_item_id
            else None
        )
        data = task.scheduling_policy_json or {}
        constraints = item.constraints_json if item else {}
        if not isinstance(data, dict) or not isinstance(constraints or {}, dict):
            return {"blocked": ("INVALID_RULE", "任务规则格式无法识别，请先调整规则")}
        temporal = (constraints or {}).get("temporal") or {}
        recurrence = (constraints or {}).get("recurrence") or {}
        if not isinstance(temporal, dict) or not isinstance(recurrence, dict):
            return {"blocked": ("INVALID_RULE", "时间或频率规则格式无法识别，请先调整规则")}
        if task.source_work_item_id and (item is None or window is None):
            return {"blocked": ("INVALID_RULE", "工作项或履约窗口缺失，请先调整规划")}
        if item and item.status not in {"ACTIVE", "DRAFT"}:
            return {"blocked": ("TASK_NOT_READY", "原工作项已结束或取消")}
        start, end = (
            window.eligible_from if window else None,
            window.eligible_to if window else None,
        )
        try:
            starts = [
                date.fromisoformat(str(v))
                for v in (
                    data.get("not_before"),
                    data.get("eligible_from"),
                    temporal.get("earliest_date"),
                )
                if v
            ]
            ends = [
                date.fromisoformat(str(v))
                for v in (
                    data.get("not_after"),
                    data.get("eligible_to"),
                    temporal.get("latest_date"),
                    temporal.get("deadline"),
                )
                if v
            ]
            for value in (data.get("start_week"), temporal.get("start_week")):
                if value is not None:
                    starts.append(
                        plan_week_range(plan, activation.requested_start_date, int(value))[0]
                    )
            for value in (data.get("end_week"), temporal.get("end_week")):
                if value is not None:
                    ends.append(
                        plan_week_range(plan, activation.requested_start_date, int(value))[1]
                    )
            if not window and task.scheduling_policy == "CADENCE":
                source_day = days[task.plan_day_id]
                starts.append(
                    plan_week_range(plan, activation.requested_start_date, source_day.week_index)[0]
                )
                ends.append(
                    plan_week_range(plan, activation.requested_start_date, source_day.week_index)[1]
                )
            start = max([*starts, *([start] if start else [])], default=None)
            end = min([*ends, *([end] if end else [])], default=None)
            gap = int(recurrence.get("minimum_gap_days", data.get("minimum_gap_days", 0)))
            per_day = int(recurrence.get("maximum_per_day", data.get("maximum_per_day", 10000)))
            if gap < 0 or per_day < 1 or (start and end and start > end):
                raise ValueError("Invalid recurrence or window")
        except (ValueError, TypeError, OverflowError):
            return {"blocked": ("INVALID_RULE", "任务日期或间隔规则无法识别，请先调整规则")}
        result = {"eligible_from": start, "eligible_to": end}
        if start and today < start:
            return {**result, "blocked": ("WINDOW_NOT_OPEN", f"执行窗口从 {start} 开始")}
        if end and today > end:
            return {**result, "blocked": ("WINDOW_EXPIRED", f"执行窗口已于 {end} 结束，请调整窗口")}
        if today > activation.projected_end_date:
            return {
                **result,
                "blocked": ("WINDOW_EXPIRED", "规划执行范围已结束，请先恢复或调整规划"),
            }
        allowed = temporal.get("allowed_weekdays") or data.get("allowed_weekdays") or []
        if not isinstance(allowed, list) or any(
            type(v) is not int or not 0 <= v <= 6 for v in allowed
        ):
            return {"blocked": ("INVALID_RULE", "允许星期规则无法识别，请先调整规则")}
        exclusions = [
            activation.excluded_dates_json or [],
            temporal.get("excluded_dates") or [],
            data.get("excluded_dates") or [],
        ]
        if any(not isinstance(values, list) for values in exclusions):
            return {"blocked": ("INVALID_RULE", "排除日期规则无法识别，请先调整规则")}
        excluded = {str(v) for values in exclusions for v in values}
        if today.isoformat() in excluded or (allowed and today.weekday() not in allowed):
            return {**result, "blocked": ("DATE_EXCLUDED", "今天不符合允许的星期或排除日期")}
        if temporal.get("exact_date") and str(temporal["exact_date"]) != str(today):
            return {**result, "blocked": ("FIXED_CONSTRAINT", "该工作项有固定执行日期")}
        peers = [
            t
            for t in tasks
            if (task.source_work_item_id and t.source_work_item_id == task.source_work_item_id)
            or (not task.source_work_item_id and group_key and group_keys.get(t.id) == group_key)
        ]
        completed_dates = []
        for peer in peers:
            if peer.status != "COMPLETED":
                continue
            evidence = [
                e
                for e in events
                if e.task_id == peer.id
                and e.event_type in {"COMPLETED", "TASK_COMPLETED", "RETROSPECTIVE_COMPLETION"}
            ]
            completed_dates.append(
                max(
                    (
                        e.occurred_at.replace(tzinfo=e.occurred_at.tzinfo or ZoneInfo("UTC"))
                        .astimezone(ZoneInfo(goal.timezone))
                        .date()
                        for e in evidence
                    ),
                    default=dates.get(peer.plan_day_id, today),
                )
            )
        if completed_dates and gap and today < max(completed_dates) + timedelta(days=gap):
            earliest = max(completed_dates) + timedelta(days=gap)
            return {
                **result,
                "blocked": ("SPACING_REQUIRED", f"需要间隔 {gap} 天，最早可在 {earliest} 继续"),
            }
        if completed_dates.count(today) >= per_day:
            return {**result, "blocked": ("DAILY_LIMIT", "今天该系列的完成次数已达上限")}
        if window:
            period_peers = [t for t in peers if t.work_item_window_id == window.id]
            if item and item.target_unit == "MINUTES":
                fulfilled = 0
                for peer in period_peers:
                    partial = sum(
                        max(0, int(e.metadata_json.get("amount") or 0))
                        for e in events
                        if e.task_id == peer.id
                        and e.event_type == "PARTIAL_COMPLETION"
                        and e.metadata_json.get("unit", "MINUTES") == "MINUTES"
                    )
                    fulfilled += max(
                        partial, peer.estimate_minutes if peer.status == "COMPLETED" else 0
                    )
            else:
                occurrences = {}
                for peer in period_peers:
                    if peer.status not in {"CANCELLED", "CARRIED_OVER"}:
                        occurrences.setdefault(peer.occurrence_index or peer.id, []).append(peer)
                fulfilled = sum(
                    all(peer.status == "COMPLETED" for peer in cards)
                    for cards in occurrences.values()
                )
            result.update(
                remaining_value=max(0, window.required_value - fulfilled),
                target_unit=item.target_unit if item else "COUNT",
            )
            if window.status in {"CANCELLED", "EXPIRED"}:
                return {**result, "blocked": ("WINDOW_EXPIRED", "原履约窗口已关闭，请先调整规划")}
            if result["remaining_value"] <= 0:
                return {**result, "blocked": ("QUOTA_SATISFIED", "当前窗口的完成要求已达标")}
        if item:
            blockers, _ = self.execution._work_item_dependency_preview(
                plan, activation, item, today, today
            )
            if blockers:
                return {**result, "blocked": ("DEPENDENCY_BLOCKED", "；".join(blockers))}
        return result
