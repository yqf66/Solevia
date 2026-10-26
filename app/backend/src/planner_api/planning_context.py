from __future__ import annotations

import re
from datetime import date, datetime
from math import ceil
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from planner_api.models import (
    AvailabilityException,
    AvailabilityWindow,
    Conversation,
    ConversationSummary,
    Goal,
    GoalErrorMemory,
    HolidayCalendarYear,
    ManualPlan,
    ManualTask,
    ManualTaskDateOverride,
    Message,
    PlanDay,
    PlanMilestone,
    PlanSchedulePreference,
    PlanTask,
    PlanVersion,
    RequirementChange,
    RequirementFact,
    RequirementVersion,
    RouteStage,
    RouteVersion,
    UserMemoryItem,
    WorkspaceAvailabilityWindow,
    WorkspaceScheduleProfile,
)


def _json_value(value: Any) -> Any:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return value


def _conversation_memory(session: Session, goal_id: str, phase: str) -> dict[str, object]:
    conversation = session.scalar(
        select(Conversation).where(
            Conversation.goal_id == goal_id,
            Conversation.phase == phase,
        )
    )
    if conversation is None:
        return {"summary": "", "recent_messages": []}
    summary = session.scalar(
        select(ConversationSummary)
        .where(ConversationSummary.conversation_id == conversation.id)
        .order_by(ConversationSummary.created_at.desc())
    )
    messages = list(
        session.scalars(
            select(Message)
            .where(Message.conversation_id == conversation.id)
            .order_by(Message.sequence.desc())
            .limit(20)
        )
    )
    messages.reverse()
    return {
        "summary": summary.summary if summary else "",
        "confirmed_conclusions": (list(summary.confirmed_conclusions_json) if summary else []),
        "recent_changes": list(summary.recent_changes_json) if summary else [],
        "open_questions": list(summary.open_questions_json) if summary else [],
        "conflicts": list(summary.conflicts_json) if summary else [],
        "recent_messages": [
            {
                "sequence": message.sequence,
                "role": message.role,
                "content": message.content,
                "intent": message.metadata_json.get("intent"),
                "change_summary": message.metadata_json.get("change_summary", []),
            }
            for message in messages
            if message.generation_status == "SUCCEEDED"
        ],
    }


def build_requirement_memory(session: Session, goal_id: str, question: str = "") -> dict[str, object]:
    """Build the auditable requirement memory shared by route and plan generation."""
    from planner_api.goal_memory import recall_memory
    facts = list(
        session.scalars(
            select(RequirementFact)
            .where(
                RequirementFact.goal_id == goal_id,
                RequirementFact.status.in_(["CONFIRMED", "PROPOSED"]),
            )
            .order_by(RequirementFact.created_at)
        )
    )
    changes = list(
        session.scalars(
            select(RequirementChange)
            .where(RequirementChange.goal_id == goal_id)
            .order_by(RequirementChange.created_at.desc())
            .limit(40)
        )
    )
    changes.reverse()
    today = date.today()
    user_memory = list(
        session.scalars(
            select(UserMemoryItem)
            .where(
                UserMemoryItem.profile_id == "default",
                UserMemoryItem.status == "CONFIRMED",
            )
            .order_by(UserMemoryItem.updated_at.desc())
            .limit(50)
        )
    )
    error_memories = list(
        session.scalars(
            select(GoalErrorMemory)
            .where(
                GoalErrorMemory.goal_id == goal_id,
                GoalErrorMemory.status == "CONFIRMED",
            )
            .order_by(GoalErrorMemory.updated_at.desc())
            .limit(40)
        )
    )
    return {
        "quick_arrangement_handoff": _quick_arrangement_handoff(session, goal_id),
        "long_term_memory": recall_memory(session, goal_id, question),
        "user_profile_memory": [
            {
                "id": item.id,
                "memory_key": item.memory_key,
                "content": item.content,
                "source_type": item.source_type,
                "valid_from": _json_value(item.valid_from),
                "valid_until": _json_value(item.valid_until),
            }
            for item in user_memory
            if item.valid_until is None or item.valid_until >= today
        ],
        "error_avoidance_memory": [
            {
                "id": item.id,
                "rule": item.rule,
                "evidence": item.evidence,
                "scope": item.scope,
                "target_refs": list(item.target_refs_json),
                "valid_until": _json_value(item.valid_until),
                "updated_at": item.updated_at.isoformat(),
            }
            for item in error_memories
            if item.valid_until is None or item.valid_until >= today
        ],
        "effective_facts": [
            {
                "id": fact.id,
                "field_key": fact.field_key,
                "category": fact.category,
                "value": fact.value_json,
                "status": fact.status,
                "source": fact.source,
                "confidence": fact.confidence,
                "operation": fact.operation,
                "change_reason": fact.change_reason,
                "confirmed_at": _json_value(fact.confirmed_at),
            }
            for fact in facts
        ],
        "change_history": [
            {
                "field_key": change.field_key,
                "operation": change.operation,
                "reason": change.reason,
                "old_value": (
                    session.get(RequirementFact, change.old_fact_id).value_json
                    if change.old_fact_id
                    and session.get(RequirementFact, change.old_fact_id) is not None
                    else None
                ),
                "new_value": (
                    session.get(RequirementFact, change.new_fact_id).value_json
                    if change.new_fact_id
                    and session.get(RequirementFact, change.new_fact_id) is not None
                    else None
                ),
                "created_at": change.created_at.isoformat(),
            }
            for change in changes
        ],
        "clarification": _conversation_memory(session, goal_id, "CLARIFICATION"),
        "route_review": _conversation_memory(session, goal_id, "ROUTE"),
        "plan_review": _conversation_memory(session, goal_id, "PLAN"),
        "execution_review": _conversation_memory(session, goal_id, "EXECUTION"),
    }


def _quick_arrangement_handoff(session: Session, goal_id: str) -> list[dict[str, object]]:
    """Retain original recurrence bounds even after the source is archived."""
    sources = [plan for plan in session.scalars(select(ManualPlan).where(ManualPlan.kind == "QUICK"))
               if plan.arrangement_json.get("upgraded_goal_id") == goal_id]
    result = []
    for plan in sources:
        tasks = list(session.scalars(select(ManualTask).where(ManualTask.manual_plan_id == plan.id)))
        for task in tasks:
            overrides = list(session.scalars(select(ManualTaskDateOverride).where(
                ManualTaskDateOverride.manual_task_id == task.id)))
            result.append({
                "source_plan_id": plan.id, "source_task_id": task.id, "title": task.title,
                "status": task.status, "estimate_minutes": task.estimate_minutes,
                "scheduled_date": _json_value(task.scheduled_date), "recurrence": task.recurrence,
                "recurrence_weekdays": list(task.recurrence_weekdays_json),
                "recurrence_end_date": _json_value(task.recurrence_end_date),
                "recorded_occurrences": [{"date": _json_value(item.override_date),
                                          "status": item.status, "is_deleted": item.is_deleted}
                                         for item in overrides],
                "inheritance_rule": "保留原重复截止日期及已完成实例；规划周期变长不代表延长本项重复行动。",
            })
    return result


_DURATION_NUMBER = r"(?:\d{1,2}|[一二两三四五六七八九十]{1,3})"
_DURATION_RANGE = re.compile(
    rf"(?P<minimum>{_DURATION_NUMBER})\s*(?:-|－|—|–|~|～|至|到)\s*"
    rf"(?P<maximum>{_DURATION_NUMBER})\s*(?P<unit>个?月|周)"
)
_PLANNING_DURATION_CUE = re.compile(
    r"计划|规划|准备|期限|周期|阶段|完成|学习|从现在|当前|之前|以内|之内"
)


def _duration_number(value: str) -> int | None:
    if value.isdigit():
        parsed = int(value)
        return parsed if 0 < parsed <= 24 else None
    digits = {
        "一": 1,
        "二": 2,
        "两": 2,
        "三": 3,
        "四": 4,
        "五": 5,
        "六": 6,
        "七": 7,
        "八": 8,
        "九": 9,
    }
    if value == "十":
        return 10
    if "十" in value:
        tens, ones = value.split("十", 1)
        parsed = (digits.get(tens, 1) * 10) + digits.get(ones, 0)
        return parsed if 0 < parsed <= 24 else None
    return digits.get(value)


def _explicit_duration_contract(text: str, source: str) -> dict[str, object] | None:
    match = _DURATION_RANGE.search(text)
    if match is None:
        return None
    minimum = _duration_number(match.group("minimum"))
    maximum = _duration_number(match.group("maximum"))
    if minimum is None or maximum is None:
        return None
    minimum, maximum = sorted((minimum, maximum))
    multiplier = 4 if "月" in match.group("unit") else 1
    minimum_weeks = minimum * multiplier
    target_weeks = maximum * multiplier
    matched_text = match.group(0)
    if target_weeks > 24:
        return None
    return {
        "target_weeks": target_weeks,
        "minimum_weeks": minimum_weeks,
        "maximum_weeks": target_weeks,
        "enforcement": "EXACT_TARGET",
        "basis": "EXPLICIT_DISCUSSION_DURATION",
        "source": source,
        "source_text": matched_text,
        "selection_rule": "使用用户区间上界，确保规划覆盖完整讨论窗口",
        "assumption": "月数按每月 4 个规划周换算。",
    }


def derive_planning_horizon_contract(
    requirement: dict[str, object],
    requirement_memory: dict[str, object],
    local_today: date,
) -> dict[str, object] | None:
    """Compile discussion-level time language into one stable outline horizon."""
    candidates: list[tuple[str, str]] = []
    for phase in ("execution_review", "plan_review", "route_review", "clarification"):
        memory = requirement_memory.get(phase)
        if not isinstance(memory, dict):
            continue
        messages = memory.get("recent_messages")
        if not isinstance(messages, list):
            continue
        for message in reversed(messages):
            if not isinstance(message, dict) or message.get("role") != "USER":
                continue
            content = message.get("content")
            if isinstance(content, str) and _PLANNING_DURATION_CUE.search(content):
                candidates.append((f"{phase}.user_message", content))
    raw_deadline = requirement.get("deadline")
    if isinstance(raw_deadline, str):
        candidates.append(("confirmed_requirement.deadline", raw_deadline))

    for source, text in candidates:
        contract = _explicit_duration_contract(text, source)
        if contract is not None:
            return contract

    deadline_text = raw_deadline if isinstance(raw_deadline, str) else ""
    if "寒假" not in deadline_text or not re.search(r"现在|当前|之前|开始", deadline_text):
        return None
    anchor_year = (
        local_today.year if local_today < date(local_today.year, 1, 10) else local_today.year + 1
    )
    anchor = date(anchor_year, 1, 10)
    target_weeks = ceil((anchor - local_today).days / 7)
    if not 1 <= target_weeks <= 24:
        return None
    return {
        "target_weeks": target_weeks,
        "minimum_weeks": max(1, target_weeks - 4),
        "maximum_weeks": target_weeks,
        "enforcement": "EXACT_TARGET",
        "basis": "SEASONAL_DISCUSSION_ANCHOR",
        "source": "confirmed_requirement.deadline",
        "source_text": deadline_text,
        "selection_rule": "按当前日期到寒假开始的稳定估算锚点换算",
        "assumption": (
            f"未提供具体校历日期，暂按 {anchor.isoformat()} 作为寒假开始估算锚点；"
            "用户确认实际日期后应重新校准。"
        ),
    }


def _schedule_context(session: Session, goal: Goal) -> dict[str, object]:
    profile = session.get(WorkspaceScheduleProfile, "default")
    workspace_windows = list(
        session.scalars(
            select(WorkspaceAvailabilityWindow).order_by(
                WorkspaceAvailabilityWindow.weekday,
                WorkspaceAvailabilityWindow.start_local,
            )
        )
    )
    goal_windows = list(
        session.scalars(
            select(AvailabilityWindow)
            .where(AvailabilityWindow.goal_id == goal.id)
            .order_by(AvailabilityWindow.weekday, AvailabilityWindow.start_local)
        )
    )
    exceptions = list(
        session.scalars(
            select(AvailabilityException)
            .where(AvailabilityException.goal_id == goal.id)
            .order_by(AvailabilityException.exception_date)
        )
    )
    preference = session.scalar(
        select(PlanSchedulePreference).where(PlanSchedulePreference.goal_id == goal.id)
    )
    manual_tasks = list(
        session.scalars(
            select(ManualTask)
            .join(ManualPlan, ManualPlan.id == ManualTask.manual_plan_id)
            .where(
                ManualPlan.status == "ACTIVE",
                ManualTask.status.not_in(["COMPLETED", "CANCELLED", "SKIPPED"]),
                ManualTask.start_local.is_not(None),
                ManualTask.end_local.is_not(None),
            )
            .order_by(ManualTask.scheduled_date, ManualTask.start_local)
            .limit(100)
        )
    )
    task_ids = [task.id for task in manual_tasks]
    overrides = (
        list(
            session.scalars(
                select(ManualTaskDateOverride)
                .where(ManualTaskDateOverride.manual_task_id.in_(task_ids))
                .order_by(ManualTaskDateOverride.override_date)
                .limit(200)
            )
        )
        if task_ids
        else []
    )
    calendars = []
    if profile and profile.holiday_country_code:
        calendars = list(
            session.scalars(
                select(HolidayCalendarYear)
                .where(
                    HolidayCalendarYear.country_code == profile.holiday_country_code,
                    HolidayCalendarYear.subdivision_code
                    == (profile.holiday_subdivision_code or ""),
                )
                .order_by(HolidayCalendarYear.year)
            )
        )
    return {
        "timezone": goal.timezone,
        "local_today": datetime.now(ZoneInfo(goal.timezone)).date().isoformat(),
        "preference": {
            "mode": preference.mode if preference else (profile.default_mode if profile else None),
            "break_minutes": (
                preference.break_minutes
                if preference
                else (profile.break_minutes if profile else None)
            ),
            "allow_cross_window": preference.allow_cross_window if preference else False,
        },
        "workspace_availability": [
            {
                "weekday": item.weekday,
                "start_local": item.start_local,
                "end_local": item.end_local,
                "valid_from": _json_value(item.valid_from),
                "valid_to": _json_value(item.valid_to),
            }
            for item in workspace_windows
        ],
        "goal_availability": [
            {
                "weekday": item.weekday,
                "start_local": item.start_local,
                "end_local": item.end_local,
                "valid_from": _json_value(item.valid_from),
                "valid_to": _json_value(item.valid_to),
                "source": item.source,
            }
            for item in goal_windows
        ],
        "date_exceptions": [
            {
                "date": item.exception_date.isoformat(),
                "kind": item.kind,
                "start_local": item.start_local,
                "end_local": item.end_local,
                "reason": item.reason,
                "source": item.source,
            }
            for item in exceptions
        ],
        "fixed_occupancy_rules": [
            {
                "task_id": task.id,
                "title": task.title,
                "scheduled_date": _json_value(task.scheduled_date),
                "recurrence": task.recurrence,
                "recurrence_weekdays": list(task.recurrence_weekdays_json),
                "recurrence_end_date": _json_value(task.recurrence_end_date),
                "start_local": task.start_local,
                "end_local": task.end_local,
            }
            for task in manual_tasks
        ],
        "fixed_occupancy_overrides": [
            {
                "task_id": item.manual_task_id,
                "date": item.override_date.isoformat(),
                "title": item.title,
                "start_local": item.start_local,
                "end_local": item.end_local,
                "time_overridden": item.time_overridden,
                "is_deleted": item.is_deleted,
            }
            for item in overrides
        ],
        "holiday_region": {
            "country_code": profile.holiday_country_code if profile else None,
            "subdivision_code": profile.holiday_subdivision_code if profile else None,
            "source": profile.holiday_source if profile else "UNSET",
            "confirmed": profile.holiday_confirmed if profile else False,
        },
        "cached_holiday_calendars": [
            {
                "year": calendar.year,
                "source": calendar.source,
                "authoritative": calendar.authoritative,
                "days": list(calendar.days_json),
            }
            for calendar in calendars
        ],
    }


def build_planning_context_snapshot(
    session: Session,
    *,
    goal: Goal,
    requirement: RequirementVersion,
    route: RouteVersion,
    route_stages: list[RouteStage],
    plan: PlanVersion | None,
    milestones: list[PlanMilestone],
    locked_day_ids: list[str],
    locked_task_ids: list[str],
) -> dict[str, object]:
    requirement_memory = build_requirement_memory(session, goal.id)
    schedule_context = _schedule_context(session, goal)
    local_today = date.fromisoformat(str(schedule_context["local_today"]))
    planning_horizon = derive_planning_horizon_contract(
        requirement.snapshot_json,
        requirement_memory,
        local_today,
    )
    generated_days = []
    if plan is not None:
        days = list(
            session.scalars(
                select(PlanDay)
                .where(PlanDay.plan_version_id == plan.id)
                .order_by(PlanDay.week_index, PlanDay.day_index)
            )
        )
        for day in days:
            tasks = list(
                session.scalars(
                    select(PlanTask)
                    .where(PlanTask.plan_day_id == day.id)
                    .order_by(PlanTask.order_in_day)
                )
            )
            generated_days.append(
                {
                    "week_index": day.week_index,
                    "day_index": day.day_index,
                    "stage_order": day.stage_order,
                    "primary_goal": day.primary_goal,
                    "suggested_minutes": day.suggested_minutes,
                    "tasks": [
                        {
                            "title": task.title,
                            "estimate_minutes": task.estimate_minutes,
                            "is_required": task.is_required,
                            "depends_on": list(task.depends_on_json),
                            "status": task.status,
                        }
                        for task in tasks
                    ],
                }
            )
    return {
        "goal": {
            "id": goal.id,
            "title": goal.title,
            "timezone": goal.timezone,
            "status": goal.status,
        },
        "requirement": requirement.snapshot_json,
        "requirement_memory": requirement_memory,
        "planning_horizon_contract": planning_horizon,
        "route": [
            {
                "id": stage.id,
                "order": stage.order,
                "title": stage.title,
                "purpose": stage.purpose,
                "topics": list(stage.topics_json),
                "outputs": list(stage.outputs_json),
                "depends_on": list(stage.depends_on_json),
            }
            for stage in route_stages
        ],
        "schedule_context": schedule_context,
        "capacity": {
            "weekly_minutes": plan.weekly_minutes if plan else None,
            "weekly_execution_days": plan.weekly_execution_days if plan else None,
            "total_weeks": plan.total_weeks if plan else None,
            "total_estimated_minutes": plan.total_estimated_minutes if plan else None,
        },
        "plan": {
            "id": plan.id if plan else None,
            "revision": plan.revision if plan else None,
            "status": plan.status if plan else None,
            "milestones": [
                {
                    "stage_order": item.stage_order,
                    "start_week": item.start_week,
                    "end_week": item.end_week,
                }
                for item in milestones
            ],
            "generated_days": generated_days,
        },
        "locked_execution": {
            "day_ids": locked_day_ids,
            "task_ids": locked_task_ids,
        },
        "precedence_rules": [
            "用户最近明确修改的要求覆盖更早版本",
            "已确认事实优先于推测和默认值",
            "指定日期或时期的例外覆盖通用每周节奏",
            "固定任务占用先于 Agent 任务容量",
            "不同日期区间的容量与频率不得压平成统一平均值",
        ],
    }
