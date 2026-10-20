"""Reviewable initial calendars and small dependency-safe execution operations."""

from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict
from datetime import date, timedelta

from fastapi import HTTPException
from sqlalchemy import select

from planner_api.ai import ProviderUnavailableError
from planner_api.config import get_settings
from planner_api.dependency_resolver import PlanDependencyResolver
from planner_api.execution_calendar_ai import ExecutionCalendarProvider
from planner_api.execution_service import ExecutionService, serialize_recovery_transaction
from planner_api.generation_orchestration import GenerationOrchestrator
from planner_api.model_telemetry import business_check
from planner_api.holiday_calendar import HolidayCalendarService
from planner_api.temporal_contract import local_date
from planner_api.models import (
    AvailabilityException,
    Conversation,
    ConversationSummary,
    ExecutionInsight,
    Message,
    PlanDay,
    PlanDaySchedule,
    ProgressEvent,
    RecoveryOperation,
    RequirementFact,
    RouteStage,
    ScheduleChange,
    TaskTimeBlock,
    utc_now,
)
from planner_api.schedule_reconciliation import allocate_plan_schedule
from planner_api.schemas import (
    CalendarIntentResult,
    ExecutionDayMoveRead,
    ExecutionDayMoveRequest,
    ExecutionPreview,
    ExecutionScheduleRequest,
    SmartCalendarRead,
    SmartCalendarRequest,
    SmartCalendarResult,
)

TERMINAL = {"COMPLETED", "CANCELLED", "CARRIED_OVER"}


def _compact_text(value, limit=600):
    text = str(value or "").strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _compact_json(value):
    if isinstance(value, str):
        return _compact_text(value, 180)
    if isinstance(value, list):
        return [_compact_json(item) for item in value[:12]]
    if isinstance(value, dict):
        return {str(key): _compact_json(item) for key, item in list(value.items())[:24]}
    return value


class ExecutionCalendarService:
    def __init__(self, session, provider=None, orchestrator=None):
        self.session = session
        self.execution = ExecutionService(session)
        self.provider = provider
        self.orchestrator = orchestrator or GenerationOrchestrator(session)

    def _days(self, plan):
        return list(
            self.session.scalars(
                select(PlanDay)
                .where(
                    PlanDay.plan_version_id == plan.id,
                )
                .order_by(PlanDay.week_index, PlanDay.day_index)
            )
        )

    def _fingerprint(self, goal, plan):
        rows = [goal, plan, *self._days(plan), *self.execution._tasks(plan.id)]
        for model in (TaskTimeBlock, AvailabilityException):
            rows.extend(self.session.scalars(select(model).where(model.goal_id == goal.id)))
        activation = self.execution._activation(plan.id)
        if activation:
            rows.append(activation)
            rows.extend(
                self.session.scalars(
                    select(PlanDaySchedule).where(
                        PlanDaySchedule.activation_id == activation.id,
                    )
                )
            )
        state = sorted(
            [
                (
                    row.__tablename__,
                    row.id,
                    {
                        attribute.key: getattr(row, attribute.key)
                        for attribute in row.__mapper__.column_attrs
                    },
                )
                for row in rows
            ],
            key=lambda item: (item[0], item[1]),
        )
        return hashlib.sha256(
            json.dumps(
                [str(self.execution._today_for(goal)), state],
                default=str,
                sort_keys=True,
            ).encode()
        ).hexdigest()

    def _dependency_review(
        self, plan, dates, completed_ids=frozenset(), affected_ids=None, *, baseline_dates=None
    ):
        resolver = PlanDependencyResolver(self.session)
        indexes = resolver.indexes(plan.id)
        tasks = self.execution._tasks(plan.id)
        blockers = []
        warnings = []
        for task in tasks:
            if task.status in TERMINAL or task.id in completed_ids:
                continue
            if affected_ids is not None and task.plan_day_id not in affected_ids:
                # A moved prerequisite can break an untouched downstream day.
                relevant = any(
                    (resolved := resolver.resolve(ref, task, indexes)) is not None
                    and resolved.plan_day_id in affected_ids
                    for ref in task.depends_on_json or []
                )
                if not relevant:
                    continue
            for ref in task.depends_on_json or []:
                predecessor = resolver.resolve(ref, task, indexes)
                if predecessor is None:
                    blockers.append(f"{task.title}：无法核实前置条件「{ref}」，请与助手讨论。")
                elif predecessor.status not in TERMINAL and predecessor.id not in completed_ids:
                    before, after = dates.get(predecessor.plan_day_id), dates.get(task.plan_day_id)
                    if after is None:
                        continue
                    if (
                        before is None
                        or before > after
                        or (before == after and predecessor.order_in_day >= task.order_in_day)
                    ):
                        blockers.append(f"{task.title} 必须在「{predecessor.title}」之后执行。")
        days = {day.id: day for day in self._days(plan)}
        active_day_ids = {
            task.plan_day_id
            for task in tasks
            if task.status not in TERMINAL and task.id not in completed_ids
        }

        def stage_order_broken(preceding_day, current_day):
            gap = dates[preceding_day.id] - dates[current_day.id]
            if gap < timedelta(0):
                return False
            if (
                baseline_dates is not None
                and preceding_day.id in baseline_dates
                and current_day.id in baseline_dates
            ):
                old_gap = baseline_dates[preceding_day.id] - baseline_dates[current_day.id]
                # Preserve existing interleaving, but never introduce or worsen an inversion.
                return old_gap < timedelta(0) or gap > old_gap
            return True

        for stage in self.session.scalars(
            select(RouteStage).where(
                RouteStage.route_version_id == plan.route_version_id,
            )
        ):
            current = [
                day
                for day in days.values()
                if day.stage_order == stage.order and day.id in dates and day.id in active_day_ids
            ]
            if not current:
                continue
            for prerequisite_order in stage.depends_on_json or []:
                preceding = [
                    day
                    for day in days.values()
                    if day.stage_order == prerequisite_order
                    and day.id in dates
                    and day.id in active_day_ids
                ]
                if any(
                    stage_order_broken(preceding_day, current_day)
                    for preceding_day in preceding
                    for current_day in current
                ):
                    warnings.append(
                        f"这次调整会让阶段「{stage.title}」与 M{prerequisite_order} 阶段交错推进；"
                        "如果这是有意并行，可以继续确认。"
                    )
        return list(dict.fromkeys(blockers)), list(dict.fromkeys(warnings))

    def _dependency_blockers(
        self, plan, dates, completed_ids=frozenset(), affected_ids=None, *, baseline_dates=None
    ):
        blockers, _ = self._dependency_review(
            plan,
            dates,
            completed_ids,
            affected_ids,
            baseline_dates=baseline_dates,
        )
        return blockers

    def _context(self, goal, plan):
        discussions = []
        for conversation in self.session.scalars(
            select(Conversation)
            .where(
                Conversation.goal_id == goal.id,
            )
            .order_by(Conversation.created_at)
        ):
            summary = self.session.scalar(
                select(ConversationSummary)
                .where(
                    ConversationSummary.conversation_id == conversation.id,
                )
                .order_by(ConversationSummary.created_at.desc())
            )
            messages = list(
                self.session.scalars(
                    select(Message)
                    .where(
                        Message.conversation_id == conversation.id,
                        Message.generation_status == "SUCCEEDED",
                    )
                    .order_by(Message.sequence.desc())
                    .limit(12)
                )
            )
            discussions.append(
                {
                    "phase": conversation.phase,
                    "summary": _compact_text(summary.summary, 1200) if summary else "",
                    "conclusions": summary.confirmed_conclusions_json if summary else [],
                    "messages": [
                        {"role": m.role, "content": _compact_text(m.content, 800),
                         "reference_date": local_date(m.created_at, goal.timezone).isoformat(),
                         "created_at": m.created_at.isoformat()}
                        for m in reversed(messages)
                    ],
                }
            )
        tasks = self.execution._tasks(plan.id)
        tasks_by_day = defaultdict(list)
        policy_counts = Counter()
        for task in tasks:
            tasks_by_day[task.plan_day_id].append(task)
            policy_counts[task.scheduling_policy or "EXACT_DAY"] += 1
        days = self._days(plan)
        all_days = []
        for day in days:
            day_tasks = tasks_by_day[day.id]
            policy_hints = []
            for task in day_tasks:
                if task.scheduling_policy_json:
                    policy_hints.append(
                        {
                            "policy": task.scheduling_policy,
                            "data": _compact_json(task.scheduling_policy_json),
                        }
                    )
            policy_hints = list(
                {
                    json.dumps(item, sort_keys=True, ensure_ascii=False): item
                    for item in policy_hints
                }.values()
            )
            all_days.append(
                {
                    "code": self.execution._day_code(day),
                    "primary_goal": _compact_text(day.primary_goal, 180),
                    "suggested_minutes": day.suggested_minutes,
                    "task_titles": [_compact_text(task.title, 120) for task in day_tasks[:6]],
                    "task_kinds": sorted({task.task_kind for task in day_tasks}),
                    "scheduling_policies": sorted(
                        {task.scheduling_policy or "EXACT_DAY" for task in day_tasks}
                    ),
                    "policy_hints": policy_hints[:6],
                }
            )
        return {
            "current_date": str(self.execution._today_for(goal)),
            "timezone": goal.timezone,
            "raw_input": goal.raw_input,
            "discussions": discussions,
            "facts": [
                {"key": f.field_key, "value": f.value_json, "status": f.status}
                for f in self.session.scalars(
                    select(RequirementFact).where(
                        RequirementFact.goal_id == goal.id,
                        RequirementFact.status.in_(["CONFIRMED", "PROPOSED"]),
                    )
                )
            ],
            "memories": [
                _compact_text(i.content, 600)
                for i in self.session.scalars(
                    select(ExecutionInsight).where(
                        ExecutionInsight.goal_id == goal.id,
                        ExecutionInsight.status == "CONFIRMED",
                    )
                )
            ],
            "plan": {
                "id": plan.id,
                "summary": plan.summary,
                "schedule_type": plan.schedule_type,
                "total_weeks": plan.total_weeks,
                "weekly_execution_days": plan.weekly_execution_days,
                "commitments": plan.commitments_json,
            },
            "stages": [
                {"order": s.order, "title": s.title, "depends_on": s.depends_on_json}
                for s in self.session.scalars(
                    select(RouteStage).where(
                        RouteStage.route_version_id == plan.route_version_id,
                    )
                )
            ],
            "calendar_summary": {
                "execution_day_count": len(days),
                "task_count": len(tasks),
                "stage_count": len({day.stage_order for day in days}),
                "policy_counts": dict(sorted(policy_counts.items())),
            },
            "all_days": all_days,
            "fixed_constraints": [
                {
                    "date": str(e.exception_date),
                    "kind": e.kind,
                    "start": e.start_local,
                    "end": e.end_local,
                    "reason": e.reason,
                }
                for e in self.session.scalars(
                    select(AvailabilityException).where(
                        AvailabilityException.goal_id == goal.id,
                    )
                )
            ],
        }

    @staticmethod
    def _orchestration_context(context):
        """Give advisory agents the decision surface, not the full day-by-day payload."""
        return {
            "goal": context.get("raw_input"),
            "instructions": context.get("instructions", ""),
            "plan": context.get("plan"),
            "calendar_summary": context.get("calendar_summary"),
            "fixed_constraints": context.get("fixed_constraints", []),
            "holiday_calendar": context.get("holiday_calendar", []),
            "discussion": [
                {
                    "phase": item.get("phase"),
                    "summary": item.get("summary"),
                    "conclusions": item.get("conclusions", []),
                    "recent_messages": item.get("messages", [])[-4:],
                }
                for item in context.get("discussions", [])
            ],
        }

    @business_check("calendar.arrangement")
    def _validate_calendar(
        self,
        goal,
        plan,
        result,
        expected_ids,
        prior=None,
        preferred=None,
        intent=None,
    ):
        assignments = result.assignments
        ids = [item.plan_day_id for item in assignments]
        if len(ids) != len(set(ids)) or set(ids) != set(expected_ids):
            raise ValueError("必须为本批次每个 plan_day_id 恰好分配一个日期，不能遗漏或增加 ID")
        today = self.execution._today_for(goal)
        if not today <= result.requested_start_date <= today + timedelta(days=730):
            raise ValueError("开始日期必须在今天至两年内")
        if preferred and result.requested_start_date != preferred:
            raise ValueError("必须保留用户指定或前批次确定的开始日期")
        weekdays = result.execution_weekdays
        if weekdays != sorted(set(weekdays)) or any(w < 0 or w > 6 for w in weekdays):
            raise ValueError("默认星期必须按 0-6 排序且唯一")
        if result.excluded_dates != sorted(set(result.excluded_dates)):
            raise ValueError("排除日期必须排序且唯一")
        dates = dict(prior or {})
        for item in assignments:
            if (
                not result.requested_start_date
                <= item.scheduled_date
                <= today + timedelta(days=730)
            ):
                raise ValueError("执行日期必须在开始日期之后且不超过两年")
            if item.scheduled_date in result.excluded_dates:
                raise ValueError("任务被分配到了用户排除的日期")
            dates[item.plan_day_id] = item.scheduled_date
        payload = ExecutionScheduleRequest(requested_start_date=result.requested_start_date,
            execution_weekdays=result.execution_weekdays, excluded_dates=result.excluded_dates)
        try:
            self.execution._validate_activation_deadline(plan, payload, max(dates.values()))
            constraints = self.execution._authored_temporal_constraints(plan, payload)
            if any(not self.execution._authored_date_allowed(day_id, value, constraints)
                   for day_id, value in dates.items()):
                raise ValueError("排期违反工作项已确认的星期、绝对期限或相对窗口，请保留任务重新安排。")
        except HTTPException as error:
            raise ValueError(str(error.detail)) from error
        if len(set(dates.values())) != len(dates):
            raise ValueError("一天最多安排一个执行日，不能重复日期")
        for exception in self.session.scalars(
            select(AvailabilityException).where(
                AvailabilityException.goal_id == goal.id,
                AvailabilityException.kind == "UNAVAILABLE",
            )
        ):
            if (
                not exception.start_local
                and not exception.end_local
                and exception.exception_date in dates.values()
            ):
                raise ValueError(f"{exception.exception_date} 是已确认的整日不可用日期")
        # The confirmed plan order is the canonical baseline. Route stages may
        # intentionally overlap while individual task prerequisites remain strict.
        blockers = self._dependency_blockers(plan, dates, baseline_dates=dates)
        if blockers:
            raise ValueError("；".join(blockers[:12]))
        if intent:
            by_code = {
                code: constraint
                for constraint in intent.constraints
                for code in constraint.day_codes
            }
            for day in self._days(plan):
                assigned = dates.get(day.id)
                if assigned is None:
                    continue
                code = self.execution._day_code(day)
                constraint = by_code.get(code)
                allowed_weekdays = (
                    constraint.allowed_weekdays
                    if constraint is not None
                    else intent.execution_weekdays
                )
                if assigned.weekday() not in allowed_weekdays:
                    raise ValueError(
                        f"{code}「{day.primary_goal}」的日期 {assigned}"
                        f" 不符合星期约束 {allowed_weekdays}"
                    )
                if constraint and constraint.not_before and assigned < constraint.not_before:
                    raise ValueError(f"{code} 不得早于 {constraint.not_before}")
                if constraint and constraint.not_after and assigned > constraint.not_after:
                    raise ValueError(f"{code} 超过已确认日期上限 {constraint.not_after}")

    def _validate_intent(self, goal, plan, intent, preferred):
        codes = [code for group in intent.constraints for code in group.day_codes]
        expected = {self.execution._day_code(day) for day in self._days(plan)}
        if len(codes) != len(set(codes)):
            raise ValueError("同一个执行日不能出现在多个日期例外规则中")
        unknown = set(codes) - expected
        if unknown:
            raise ValueError(f"日期例外规则包含不存在的执行日：{sorted(unknown)[:8]}")
        today = self.execution._today_for(goal)
        if not today <= intent.requested_start_date <= today + timedelta(days=730):
            raise ValueError("开始日期必须从今天起且不超过两年")
        if preferred and intent.requested_start_date != preferred:
            raise ValueError("必须保留用户指定的开始日期")
        for weekdays in [
            intent.execution_weekdays,
            *(c.allowed_weekdays for c in intent.constraints),
        ]:
            if weekdays != sorted(set(weekdays)) or any(w < 0 or w > 6 for w in weekdays):
                raise ValueError("星期约束必须是按0到6排序且唯一的整数")
        if intent.excluded_dates != sorted(set(intent.excluded_dates)):
            raise ValueError("排除日期必须排序且唯一")
        for constraint in intent.constraints:
            if (
                constraint.not_before
                and constraint.not_after
                and constraint.not_before > constraint.not_after
            ):
                raise ValueError("日期约束起点不能晚于上限")

    def _allocate_calendar(self, goal, plan, intent):
        """Expand semantic rules into a complete, stable one-day-per-date calendar."""
        by_code = {
            code: constraint for constraint in intent.constraints for code in constraint.day_codes
        }
        excluded = set(intent.excluded_dates)
        excluded.update(
            exception.exception_date
            for exception in self.session.scalars(
                select(AvailabilityException).where(
                    AvailabilityException.goal_id == goal.id,
                    AvailabilityException.kind == "UNAVAILABLE",
                )
            )
            if not exception.start_local and not exception.end_local
        )
        last_possible = self.execution._today_for(goal) + timedelta(days=730)
        cursor = intent.requested_start_date
        assignments = []
        for day in self._days(plan):
            code = self.execution._day_code(day)
            constraint = by_code.get(code)
            allowed_weekdays = set(
                constraint.allowed_weekdays if constraint else intent.execution_weekdays
            )
            if constraint and constraint.not_before:
                cursor = max(cursor, constraint.not_before)
            deadline = min(
                last_possible,
                constraint.not_after if constraint and constraint.not_after else last_possible,
            )
            while cursor <= deadline and (
                cursor.weekday() not in allowed_weekdays or cursor in excluded
            ):
                cursor += timedelta(days=1)
            if cursor > deadline:
                raise ValueError(
                    f"{code}「{day.primary_goal}」在允许星期、排除日期和日期上限内没有可用日期"
                )
            assignments.append({"plan_day_id": day.id, "scheduled_date": cursor})
            cursor += timedelta(days=1)
        return SmartCalendarResult(
            requested_start_date=intent.requested_start_date,
            execution_weekdays=intent.execution_weekdays,
            excluded_dates=intent.excluded_dates,
            assignments=assignments,
            rationale=intent.rationale,
            rules=intent.rules,
            assumptions=intent.assumptions,
            questions=intent.questions,
            requires_clarification=intent.requires_clarification,
        )

    def smart_preview(self, goal_id, request: SmartCalendarRequest) -> SmartCalendarRead:
        goal, plan = self.execution._goal_and_plan(goal_id)
        if goal.status != "READY_TO_START":
            raise HTTPException(409, "只有尚未开始的已确认规划可以智能排布初始日期")
        context = self._context(goal, plan)
        context.update(request.model_dump(mode="json"))
        context["prompt_version"] = "execution-calendar-v3-semantic-rules"
        today = self.execution._today_for(goal)
        context["holiday_calendar"] = HolidayCalendarService(self.session).days(
            today,
            today + timedelta(days=730),
        )
        context_key = hashlib.sha256(
            json.dumps(context, sort_keys=True, default=str).encode()
        ).hexdigest()
        fingerprint = self._fingerprint(goal, plan)
        all_days = context["all_days"]
        if not all_days:
            raise HTTPException(409, "规划中没有可安排的执行日")
        idempotency_key = (
            "calendar:"
            + hashlib.sha256(
                f"{goal.id}:{context_key}:{fingerprint}".encode(),
            ).hexdigest()
        )
        operation = self.session.scalar(
            select(RecoveryOperation).where(
                RecoveryOperation.idempotency_key == idempotency_key,
            )
        )
        if operation and operation.status == "PREVIEW":
            return self._smart_read(operation, plan)
        previous_payload = dict(operation.preview_json or {}) if operation else {}
        reusable_intent = None
        if (
            operation
            and operation.status == "FAILED"
            and previous_payload.get("failure", {}).get("phase") == "EXPANDING_DATES"
            and previous_payload.get("intent")
            and previous_payload.get("orchestration", {}).get("run_id")
        ):
            try:
                reusable_intent = CalendarIntentResult.model_validate(previous_payload["intent"])
            except ValueError:
                reusable_intent = None
        attempt_count = int(previous_payload.get("attempt_count", 0)) + 1
        request_payload = request.model_dump(mode="json")
        operation_payload = {
            "fingerprint": fingerprint,
            "context_key": context_key,
            "request": request_payload,
            "instructions": request.instructions,
            "attempt_count": attempt_count,
            "progress": {"phase": "CONTEXT_READY", "completed": 1, "total": 4},
            "context_stats": {
                **context["calendar_summary"],
                "encoded_characters": len(json.dumps(context, ensure_ascii=False, default=str)),
            },
        }
        if reusable_intent:
            operation_payload.update(
                {
                    "intent": previous_payload["intent"],
                    "model_call": previous_payload.get("model_call", {}),
                    "orchestration": previous_payload["orchestration"],
                    "reused_intent": True,
                    "progress": {"phase": "EXPANDING_DATES", "completed": 3, "total": 4},
                }
            )
        if operation is None:
            operation = RecoveryOperation(
                goal_id=goal.id,
                source_plan_version_id=plan.id,
                mode="SMART_CALENDAR",
                status="RUNNING",
                idempotency_key=idempotency_key,
                preview_json=operation_payload,
            )
            self.session.add(operation)
        else:
            operation.status = "RUNNING"
            operation.preview_json = operation_payload
        self.session.commit()

        orchestration = None
        phase = "AGENT_PREPARATION"
        try:

            def validate_intent(intent):
                self._validate_intent(goal, plan, intent, request.preferred_start_date)

            if reusable_intent:
                orchestration = previous_payload["orchestration"]
                intent = reusable_intent
                validate_intent(intent)
            else:
                orchestration = self.orchestrator.prepare(
                    goal.id,
                    "DATE_SCHEDULING",
                    self._orchestration_context(context),
                    plan_version_id=plan.id,
                    minimum_tier="L1",
                )
                context["agent_orchestration"] = orchestration
                operation.preview_json = {
                    **operation.preview_json,
                    "orchestration": orchestration,
                    "progress": {
                        "phase": "UNDERSTANDING_RULES",
                        "completed": 2,
                        "total": 4,
                    },
                }
                self.session.commit()
                provider = self.provider or ExecutionCalendarProvider(get_settings())
                phase = "UNDERSTANDING_RULES"
                intent = provider.interpret(context, validate_intent)
                validate_intent(intent)
                operation.preview_json = {
                    **operation.preview_json,
                    "intent": intent.model_dump(mode="json"),
                    "model_call": getattr(provider, "last_call_meta", {}),
                    "progress": {
                        "phase": "EXPANDING_DATES",
                        "completed": 3,
                        "total": 4,
                    },
                }
                self.session.commit()
            phase = "EXPANDING_DATES"
            final = self._allocate_calendar(goal, plan, intent)
            self._validate_calendar(
                goal,
                plan,
                final,
                [day.id for day in self._days(plan)],
                intent=intent,
            )
        except (ProviderUnavailableError, ValueError) as error:
            if orchestration:
                self.orchestrator.fail(orchestration["run_id"], error)
            diagnostic = getattr(error, "diagnostic", None) or str(error)
            code = getattr(
                error,
                "code",
                "CALENDAR_LOCAL_VALIDATION_FAILED",
            )
            operation.status = "FAILED"
            operation.preview_json = {
                **operation.preview_json,
                "failure": {
                    "phase": phase,
                    "code": code,
                    "message": str(error),
                    "diagnostic": diagnostic,
                    "retryable": isinstance(error, ProviderUnavailableError),
                },
                "progress": {"phase": "FAILED", "completed": 0, "total": 4},
            }
            self.session.commit()
            message = (
                str(error)
                if isinstance(error, ProviderUnavailableError)
                else "日期规则无法展开为有效日历，原规划未改变；请调整说明后重试。"
            )
            raise HTTPException(
                503,
                {
                    "code": code,
                    "message": message,
                    "diagnostic": diagnostic,
                    "preview_id": operation.id,
                    "retryable": isinstance(error, ProviderUnavailableError),
                },
            ) from None
        self.session.expire_all()
        if goal.status != "READY_TO_START" or self._fingerprint(goal, plan) != fingerprint:
            operation = self.session.get(RecoveryOperation, operation.id)
            operation.status = "FAILED"
            operation.preview_json = {
                **operation.preview_json,
                "failure": {
                    "phase": "STALE_CHECK",
                    "code": "CALENDAR_SOURCE_CHANGED",
                    "message": "生成期间规划发生变化，请刷新后重新排布",
                    "retryable": True,
                },
            }
            self.session.commit()
            raise HTTPException(409, "生成期间规划发生变化，请刷新后重新排布")
        operation = self.session.get(RecoveryOperation, operation.id)
        operation.status = "PREVIEW"
        operation.preview_json = {
            **operation.preview_json,
            "result": final.model_dump(mode="json"),
            "progress": {"phase": "COMPLETE", "completed": 4, "total": 4},
        }
        if orchestration and orchestration.get("run_id"):
            self.orchestrator.complete(
                orchestration["run_id"],
                {
                    "status": "PASSED",
                    "assignment_count": len(final.assignments),
                    "questions": final.questions,
                },
            )
        self.session.commit()
        return self._smart_read(operation, plan)

    def _smart_read(self, operation, plan):
        result = SmartCalendarResult.model_validate(operation.preview_json["result"])
        days = {day.id: day for day in self._days(plan)}
        mapped = sorted(
            [
                self.execution._scheduled_read(days[a.plan_day_id], a.scheduled_date)
                for a in result.assignments
            ],
            key=lambda d: d.scheduled_date,
        )
        payload = ExecutionScheduleRequest(
            requested_start_date=result.requested_start_date,
            execution_weekdays=result.execution_weekdays,
            excluded_dates=result.excluded_dates,
            smart_preview_id=operation.id,
        )
        return SmartCalendarRead(
            preview_id=operation.id,
            input=payload,
            preview=ExecutionPreview(
                requested_start_date=result.requested_start_date,
                first_execution_date=mapped[0].scheduled_date,
                projected_end_date=mapped[-1].scheduled_date,
                execution_weekdays=result.execution_weekdays,
                excluded_dates=result.excluded_dates,
                total_execution_days=len(mapped),
                upcoming_days=mapped[:10],
            ),
            days=mapped,
            rationale=result.rationale,
            rules=result.rules,
            assumptions=result.assumptions,
            questions=result.questions,
            can_apply=not result.requires_clarification,
        )

    def resolve_smart_input(self, goal, plan, payload):
        operation = self.session.get(RecoveryOperation, payload.smart_preview_id)
        if (
            not operation
            or operation.goal_id != goal.id
            or operation.source_plan_version_id != plan.id
            or operation.mode != "SMART_CALENDAR"
        ):
            raise HTTPException(404, "智能日期预览不存在或不属于当前规划")
        if operation.status != "PREVIEW" or operation.preview_json[
            "fingerprint"
        ] != self._fingerprint(goal, plan):
            raise HTTPException(409, "日期预览已过期，请重新排布")
        read = self._smart_read(operation, plan)
        if read.input != payload or not read.can_apply:
            raise HTTPException(409, "请先解决草案中的问题，或重新生成修改后的日期预览")
        return read.days

    def _move_preview(self, goal, plan, request):
        activation = self.execution._activation(plan.id)
        if goal.status != "ACTIVE" or activation is None:
            raise HTTPException(409, "只有执行中的规划可以调整执行日")
        today = self.execution._today_for(goal)
        schedules = list(
            self.session.scalars(
                select(PlanDaySchedule)
                .where(
                    PlanDaySchedule.activation_id == activation.id,
                )
                .order_by(PlanDaySchedule.scheduled_date)
            )
        )
        selected = next((s for s in schedules if s.plan_day_id == request.selected_day_id), None)
        if selected is None or selected.scheduled_date <= today:
            raise HTTPException(422, "请选择今天之后的执行日")
        dates = {s.plan_day_id: s.scheduled_date for s in schedules}
        original_dates = dates.copy()
        tasks = self.execution._tasks(plan.id)
        completed_ids = set()
        changes = []
        if request.mode == "SWAP":
            current = next((s for s in schedules if s.scheduled_date == today), None)
            if current is None:
                raise HTTPException(422, "今天是休息日，没有可互换的执行日；可以使用从指定日开始")
            dates[current.plan_day_id], dates[selected.plan_day_id] = selected.scheduled_date, today
            moved = [current, selected]
        else:
            completed_ids = {
                t.id
                for t in tasks
                if t.status not in TERMINAL
                and dates.get(t.plan_day_id, selected.scheduled_date) < selected.scheduled_date
            }
            shift = selected.scheduled_date - today
            moved = [s for s in schedules if s.scheduled_date >= selected.scheduled_date]
            for schedule in moved:
                dates[schedule.plan_day_id] -= shift
        days = {d.id: d for d in self._days(plan)}
        blockers = []
        if request.mode == "SWAP" and any(
            t.status != "NOT_STARTED"
            for t in tasks
            if t.plan_day_id in {s.plan_day_id for s in moved}
        ):
            blockers.append("已开始或已完成的执行日不能整日互换，以免改写执行记录。")
        if request.mode == "ADVANCE" and any(
            task.status in {"IN_PROGRESS", "COMPLETED"}
            for task in tasks
            if task.plan_day_id in {schedule.plan_day_id for schedule in moved}
        ):
            blockers.append(
                "要前移的后续日期包含已开始或已完成内容，请与助手讨论，以保留执行历史。"
            )
        for schedule in moved:
            day = days[schedule.plan_day_id]
            changes.append(
                {
                    "plan_day_id": day.id,
                    "code": self.execution._day_code(day),
                    "primary_goal": day.primary_goal,
                    "from_date": str(schedule.scheduled_date),
                    "to_date": str(dates[day.id]),
                }
            )
        affected = {s.plan_day_id for s in moved}
        for block in self.session.scalars(
            select(TaskTimeBlock).where(
                TaskTimeBlock.plan_version_id == plan.id,
                TaskTimeBlock.plan_day_id.in_(affected),
                TaskTimeBlock.status == "PLANNED",
                TaskTimeBlock.source == "USER",
            )
        ):
            blockers.append(
                f"{days[block.plan_day_id].primary_goal} 有手动固定时段，请先与助手讨论调整。"
            )
        for exception in self.session.scalars(
            select(AvailabilityException).where(
                AvailabilityException.goal_id == goal.id,
                AvailabilityException.kind == "UNAVAILABLE",
            )
        ):
            if (
                not exception.start_local
                and not exception.end_local
                and any(dates[day_id] == exception.exception_date for day_id in affected)
            ):
                blockers.append(f"{exception.exception_date} 已标记为整日不可执行。")
        dependency_blockers, dependency_warnings = self._dependency_review(
            plan, dates, completed_ids, affected, baseline_dates=original_dates
        )
        blockers.extend(dependency_blockers)
        return {
            "mode": request.mode,
            "selected_day_id": request.selected_day_id,
            "can_apply": not blockers,
            "blockers": list(dict.fromkeys(blockers)),
            "warnings": dependency_warnings,
            "completed_task_count": len(completed_ids),
            "completed_task_ids": sorted(completed_ids),
            "changes": changes,
            "fingerprint": self._fingerprint(goal, plan),
        }

    @serialize_recovery_transaction
    def preview_move(self, goal_id, request: ExecutionDayMoveRequest):
        goal, plan = self.execution._goal_and_plan(goal_id)
        preview = self._move_preview(goal, plan, request)
        key = (
            "day-move:"
            + hashlib.sha256(json.dumps([goal.id, preview], sort_keys=True).encode()).hexdigest()
        )
        operation = self.session.scalar(
            select(RecoveryOperation).where(RecoveryOperation.idempotency_key == key)
        )
        if operation is None:
            operation = RecoveryOperation(
                goal_id=goal.id,
                source_plan_version_id=plan.id,
                mode=request.mode,
                status="PREVIEW",
                idempotency_key=key,
                preview_json=preview,
            )
            self.session.add(operation)
            self.session.commit()
        return ExecutionDayMoveRead(preview_id=operation.id, **preview)

    @serialize_recovery_transaction
    def apply_move(self, goal_id, preview_id):
        goal, plan = self.execution._goal_and_plan(goal_id)
        operation = self.session.get(RecoveryOperation, preview_id)
        if (
            not operation
            or operation.goal_id != goal.id
            or operation.source_plan_version_id != plan.id
            or operation.mode not in {"ADVANCE", "SWAP"}
        ):
            raise HTTPException(404, "执行日调整预览不存在")
        if operation.status == "APPLIED":
            return self.execution.get_today(goal_id)
        stored = operation.preview_json
        preview = self._move_preview(
            goal,
            plan,
            ExecutionDayMoveRequest(
                mode=stored["mode"],
                selected_day_id=stored["selected_day_id"],
            ),
        )
        if stored != preview or not preview["can_apply"]:
            raise HTTPException(409, "安排或完成状态发生变化，请重新审核调整")
        activation = self.execution._activation(plan.id)
        schedules = {
            s.plan_day_id: s
            for s in self.session.scalars(
                select(PlanDaySchedule).where(
                    PlanDaySchedule.activation_id == activation.id,
                )
            )
        }
        tasks = self.execution._tasks(plan.id)
        affected_ids = {c["plan_day_id"] for c in preview["changes"]}
        completed_ids = set(preview["completed_task_ids"])
        for task in tasks:
            if task.id in completed_ids:
                task.status = "COMPLETED"
                self.session.add(
                    ProgressEvent(
                        goal_id=goal.id,
                        plan_version_id=plan.id,
                        plan_day_id=task.plan_day_id,
                        task_id=task.id,
                        event_type="COMPLETED",
                        metadata_json={
                            "source": "USER_ADVANCE_ACKNOWLEDGEMENT",
                            "operation_id": operation.id,
                        },
                    )
                )
        for change in preview["changes"]:
            schedules[change["plan_day_id"]].scheduled_date = date.fromisoformat(change["to_date"])
        acknowledged_days = {t.plan_day_id for t in tasks if t.id in completed_ids}
        for day_id in acknowledged_days:
            if schedules[day_id].scheduled_date > self.execution._today_for(goal):
                schedules[day_id].scheduled_date = self.execution._today_for(goal)
        for block in self.session.scalars(
            select(TaskTimeBlock).where(
                TaskTimeBlock.plan_version_id == plan.id,
                TaskTimeBlock.status.in_(["PLANNED", "REQUIRES_REVIEW"]),
            )
        ):
            if block.task_id in completed_ids:
                block.status = "COMPLETED"
            elif block.plan_day_id in affected_ids:
                block.status = "CANCELLED"
                block.change_reason = "执行日调整后按目标日期重新排时"
            else:
                continue
            block.revision += 1
        activation.projected_end_date = max(s.scheduled_date for s in schedules.values())
        self.session.flush()
        for day in self._days(plan):
            if any(t.id in completed_ids and t.plan_day_id == day.id for t in tasks):
                self.execution._apply_automatic_completions(goal, plan, day)
                self.execution._refresh_day_log(goal, plan, day)
        self.session.add(
            ScheduleChange(
                goal_id=goal.id,
                entity_type="PLAN_ACTIVATION",
                entity_id=activation.id,
                change_kind=preview["mode"],
                before_json={"changes": preview["changes"]},
                after_json={
                    "operation_id": operation.id,
                    "completed_task_ids": sorted(completed_ids),
                },
                reason="用户确认此前内容已完成并前移后续安排"
                if preview["mode"] == "ADVANCE"
                else "用户确认互换执行日，已通过前置审核",
            )
        )
        operation.status = "APPLIED"
        operation.applied_at = utc_now()
        goal.last_active_at = utc_now()
        self.session.flush()
        allocate_plan_schedule(
            self.session,
            goal,
            plan.id,
            affected_dates={date.fromisoformat(c["to_date"]) for c in preview["changes"]},
            use_ai=False,
        )
        self.session.commit()
        return self.execution.get_today(goal_id)
