from __future__ import annotations

from planner_api.temporal_contract import relative_day_range
from planner_api.plan_calendar import calendar_weeks, plan_week_range, plan_week_index

import hashlib
import json
import re
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, timedelta
from itertools import combinations

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from planner_api.models import (
    PlanActivation,
    PlanDay,
    PlanDaySchedule,
    PlanMilestone,
    PlanTask,
    PlanVersion,
    PlanWorkItem,
    PlanWorkItemGroup,
    RecoveryOperation,
    WorkItemDependency,
    WorkItemWindow,
    utc_now,
)
from planner_api.schemas import (
    ExecutionPreview,
    ExecutionScheduleRequest,
    ScheduledDayRead,
    WorkItemDailyDayPreviewRead,
    WorkItemDailyPreviewRead,
    WorkItemDailyTaskPreviewRead,
)


@dataclass(frozen=True)
class WindowDraft:
    work_item_id: str
    item_key: str
    period_key: str
    eligible_from: date
    eligible_to: date
    required_value: int
    selected_dates: tuple[date, ...]
    pending_trigger: bool = False


class WorkItemMaterializer:
    """Resolve semantic work-item windows into a deterministic execution projection."""

    preview_mode = "WORK_ITEM_MATERIALIZATION"

    def __init__(
        self,
        session: Session,
        *,
        date_allowed: Callable[[date], bool] | None = None,
    ) -> None:
        self.session = session
        self.date_allowed = date_allowed or (lambda _value: True)

    def preview(
        self,
        goal_id: str,
        plan: PlanVersion,
        payload: ExecutionScheduleRequest,
    ) -> ExecutionPreview:
        source_fingerprint = self._source_fingerprint(plan, payload)
        windows = self._resolve(plan, payload)
        scheduled = self._scheduled_days(windows, payload.requested_start_date, plan)
        if not scheduled:
            raise HTTPException(
                status_code=409,
                detail="当前工作项都在等待触发条件，暂时没有可放入日历的任务。",
            )
        preview_json = {
            "source_fingerprint": source_fingerprint,
            "request": payload.model_dump(mode="json", exclude={"materialization_preview_id"}),
            "windows": [self._window_json(item) for item in windows],
            "scheduled_days": [item.model_dump(mode="json") for item in scheduled],
        }
        digest = hashlib.sha256(
            json.dumps(preview_json, ensure_ascii=False, sort_keys=True).encode()
        ).hexdigest()
        operation = self.session.scalar(
            select(RecoveryOperation).where(
                RecoveryOperation.goal_id == goal_id,
                RecoveryOperation.source_plan_version_id == plan.id,
                RecoveryOperation.mode == self.preview_mode,
                RecoveryOperation.status == "PREVIEW",
                RecoveryOperation.idempotency_key == f"work-item-preview:{digest}",
            )
        )
        if operation is None:
            operation = RecoveryOperation(
                goal_id=goal_id,
                source_plan_version_id=plan.id,
                mode=self.preview_mode,
                status="PREVIEW",
                idempotency_key=f"work-item-preview:{digest}",
                preview_json=preview_json,
            )
            self.session.add(operation)
            self.session.flush()
        return ExecutionPreview(
            requested_start_date=payload.requested_start_date,
            first_execution_date=scheduled[0].scheduled_date,
            projected_end_date=scheduled[-1].scheduled_date,
            execution_weekdays=payload.execution_weekdays,
            excluded_dates=payload.excluded_dates,
            total_execution_days=len(scheduled),
            upcoming_days=scheduled[:10],
            materialization_preview_id=operation.id,
        )

    def recommended_execution_weekdays(
        self, plan: PlanVersion, start_date: date | None = None
    ) -> list[int]:
        items = list(
            self.session.scalars(
                select(PlanWorkItem).where(
                    PlanWorkItem.plan_version_id == plan.id,
                    PlanWorkItem.status.in_(["DRAFT", "ACTIVE"]),
                )
            )
        )
        selected_ids = self._selected_item_ids(plan.id, items)
        weekly_rules: list[tuple[dict, dict]] = []
        for item in items:
            if item.id not in selected_ids or str(item.binding_mode) == "TRIGGERED":
                continue
            constraints = item.constraints_json or {}
            recurrence = constraints.get("recurrence") or {}
            temporal = self.resolved_temporal(plan, item, [])
            # Exact relative days constrain the default selection as strongly
            # as an explicit weekday. Resolve against the same start anchor.
            if start_date and (temporal.get("exact_date") or temporal.get("relative_day_ref")):
                request = ExecutionScheduleRequest(requested_start_date=start_date, execution_weekdays=list(range(7)))
                first, last = self._date_range(plan, request, temporal)
                if first == last:
                    weekday = first.weekday()
                    if temporal.get("allowed_weekdays") and weekday not in temporal["allowed_weekdays"]:
                        raise HTTPException(409, {"code": "PLAN_ITEM_DATE_CONFLICT",
                            "message": "任务的相对日期与星期要求冲突，请讨论调整。"})
                    temporal["allowed_weekdays"] = [weekday]
            if str(item.binding_mode) == "CADENCE" and str(recurrence.get("period") or "ONCE") == "WEEK":
                weekly_rules.append((temporal, recurrence))
            elif temporal.get("allowed_weekdays"):
                weekly_rules.append((temporal, {"target_count": 1}))

        minimum_size = max(1, min(7, max([int(plan.weekly_execution_days)] + [
            int(item.get("available_days_per_week") or item.get("weekly_execution_days") or 1)
            for item in plan.temporal_segments_json or []])))
        candidates: list[tuple[int, tuple[int, ...]]] = []
        for size in range(minimum_size, 8):
            for weekdays in combinations(range(7), size):
                if all(
                    self._weekday_capacity(
                        weekdays,
                        temporal,
                        recurrence,
                        start_weekday=(start_date.weekday() if start_date else 0),
                    )
                    >= int(recurrence.get("target_count") or 1)
                    for temporal, recurrence in weekly_rules
                ):
                    spread_penalty = sum(
                        abs((right - left) - 2)
                        for left, right in zip(weekdays, weekdays[1:], strict=False)
                    )
                    candidates.append((spread_penalty, weekdays))
            if candidates:
                break
        if not candidates:
            return list(range(7))
        return list(min(candidates, key=lambda item: (item[0], item[1]))[1])

    @staticmethod
    def _weekday_capacity(weekdays, temporal, recurrence, *, start_weekday: int) -> int:
        allowed = set(temporal.get("allowed_weekdays") or weekdays)
        eligible = [
            offset
            for offset in range(7)
            if (start_weekday + offset) % 7 in weekdays
            and (start_weekday + offset) % 7 in allowed
        ]
        if not eligible:
            return 0
        minimum_gap = int(recurrence.get("minimum_gap_days") or 0)
        maximum_per_day = max(1, int(recurrence.get("maximum_per_day") or 1))
        if minimum_gap == 0:
            return len(eligible) * maximum_per_day
        best = 0
        for size in range(1, len(eligible) + 1):
            if any(
                all(
                    right - left > minimum_gap
                    for left, right in zip(option, option[1:], strict=False)
                )
                for option in combinations(eligible, size)
            ):
                best = size
        return best

    def daily_preview(
        self,
        plan: PlanVersion,
        payload: ExecutionScheduleRequest,
    ) -> WorkItemDailyPreviewRead:
        """Project review work items into days without persisting execution rows."""
        windows = self._resolve(plan, payload, strict=False)
        item_ids = {window.work_item_id for window in windows}
        items = {
            item.id: item
            for item in self.session.scalars(
                select(PlanWorkItem).where(PlanWorkItem.id.in_(item_ids))
            )
        }
        by_date: dict[date, list[tuple[WindowDraft, int]]] = defaultdict(list)
        pending: list[WorkItemDailyTaskPreviewRead] = []
        warnings: list[str] = []
        for window in windows:
            item = items[window.work_item_id]
            if window.pending_trigger:
                pending.append(self._daily_task_read(item, window, 1))
                continue
            if len(window.selected_dates) < window.required_value:
                warnings.append(
                    f"{item.title}（{self._period_label(window.period_key)}）"
                    f"需要 {window.required_value} 次，"
                    f"当前候选日期只能安排 {len(window.selected_dates)} 次。"
                )
            for occurrence, scheduled_date in enumerate(window.selected_dates, start=1):
                by_date[scheduled_date].append((window, occurrence))

        per_week: dict[int, int] = defaultdict(int)
        days: list[WorkItemDailyDayPreviewRead] = []
        for scheduled_date in sorted(by_date):
            week_index = max(
                1,
                plan_week_index(plan, payload.requested_start_date, scheduled_date),
            )
            per_week[week_index] += 1
            entries = sorted(
                by_date[scheduled_date],
                key=lambda pair: (
                    items[pair[0].work_item_id].priority,
                    pair[0].item_key,
                ),
            )
            tasks = [
                self._daily_task_read(items[window.work_item_id], window, occurrence)
                for window, occurrence in entries
            ]
            days.append(
                WorkItemDailyDayPreviewRead(
                    scheduled_date=scheduled_date,
                    week_index=week_index,
                    day_index=per_week[week_index],
                    code=f"W{week_index}-D{per_week[week_index]}",
                    suggested_minutes=sum(task.estimate_minutes for task in tasks),
                    tasks=tasks,
                )
            )
        return WorkItemDailyPreviewRead(
            plan_id=plan.id,
            candidate_start_date=payload.requested_start_date,
            projected_end_date=days[-1].scheduled_date if days else None,
            execution_weekdays=payload.execution_weekdays,
            days=days,
            pending_trigger_items=pending,
            warnings=warnings,
        )

    @staticmethod
    def _daily_task_read(item, window: WindowDraft, occurrence: int):
        return WorkItemDailyTaskPreviewRead(
            work_item_id=item.id,
            stage_order=item.stage_order,
            title=item.title,
            description=item.description,
            estimate_minutes=item.estimate_minutes,
            priority=item.priority,
            task_kind=item.task_kind,
            is_required=item.is_required,
            binding_mode=str(item.binding_mode),
            evidence=item.evidence_json or [],
            period_key=window.period_key,
            occurrence_index=occurrence,
        )

    @staticmethod
    def _period_label(period_key: str) -> str:
        if period_key.startswith("week:"):
            return f"第 {period_key.split(':', 1)[1]} 周"
        if period_key.startswith("month:"):
            return period_key.split(":", 1)[1]
        if period_key.startswith("day:"):
            return f"第 {period_key.split(':', 1)[1]} 天"
        return "当前窗口"

    def apply(
        self,
        goal_id: str,
        plan: PlanVersion,
        payload: ExecutionScheduleRequest,
    ) -> PlanActivation:
        if not payload.materialization_preview_id:
            raise HTTPException(409, "请先预览工作项的日期安排。")
        operation = self.session.get(RecoveryOperation, payload.materialization_preview_id)
        if (
            operation is None
            or operation.goal_id != goal_id
            or operation.source_plan_version_id != plan.id
            or operation.mode != self.preview_mode
            or operation.status != "PREVIEW"
        ):
            raise HTTPException(409, "日期预览不存在、已应用或已经过期，请重新预览。")
        expected_request = payload.model_dump(mode="json", exclude={"materialization_preview_id"})
        if operation.preview_json.get("request") != expected_request:
            raise HTTPException(409, "开始日期或排除日期已变化，请重新预览。")
        if operation.preview_json.get("source_fingerprint") != self._source_fingerprint(
            plan, payload
        ):
            operation.status = "STALE"
            raise HTTPException(409, "计划工作项已经变化，请重新预览日期安排。")

        windows = [self._window_from_json(item) for item in operation.preview_json["windows"]]
        concrete_dates = sorted(
            {selected for window in windows for selected in window.selected_dates}
        )
        if not concrete_dates:
            raise HTTPException(409, "当前没有可物化的工作项。")
        activation = PlanActivation(
            goal_id=goal_id,
            plan_version_id=plan.id,
            requested_start_date=payload.requested_start_date,
            projected_end_date=concrete_dates[-1],
            execution_weekdays_json=payload.execution_weekdays,
            excluded_dates_json=[item.isoformat() for item in payload.excluded_dates],
        )
        self.session.add(activation)
        self.session.flush()

        items = {
            item.id: item
            for item in self.session.scalars(
                select(PlanWorkItem).where(PlanWorkItem.plan_version_id == plan.id)
            )
        }
        milestones = {
            item.stage_order: item
            for item in self.session.scalars(
                select(PlanMilestone)
                .where(PlanMilestone.plan_version_id == plan.id)
                .order_by(PlanMilestone.stage_order)
            )
        }
        fallback_milestone = next(iter(milestones.values()))
        by_date: dict[date, list[tuple[WindowDraft, int]]] = defaultdict(list)
        window_rows: dict[tuple[str, str], WorkItemWindow] = {}
        for draft in windows:
            row = WorkItemWindow(
                activation_id=activation.id,
                work_item_id=draft.work_item_id,
                period_key=draft.period_key,
                eligible_from=draft.eligible_from,
                eligible_to=draft.eligible_to,
                required_value=draft.required_value,
                status="PENDING" if draft.pending_trigger else "OPEN",
                resolution_json={
                    "selected_dates": [item.isoformat() for item in draft.selected_dates],
                    "resolver": "DETERMINISTIC_V1",
                },
                materialization_revision=1,
                opened_at=None if draft.pending_trigger else utc_now(),
            )
            self.session.add(row)
            self.session.flush()
            window_rows[(draft.work_item_id, draft.period_key)] = row
            for occurrence, scheduled_date in enumerate(draft.selected_dates, start=1):
                by_date[scheduled_date].append((draft, occurrence))

        days_per_week: dict[int, int] = defaultdict(int)
        dependencies = list(self.session.scalars(select(WorkItemDependency).where(
            WorkItemDependency.plan_version_id == plan.id)))
        dependency_order = {item.id: index for index, item in enumerate(
            self._topological(list(items.values()), dependencies))}
        for scheduled_date in sorted(by_date):
            entries = sorted(
                by_date[scheduled_date],
                key=lambda pair: (dependency_order[pair[0].work_item_id],
                                  items[pair[0].work_item_id].priority, pair[0].item_key),
            )
            week_index = max(1, plan_week_index(plan, payload.requested_start_date, scheduled_date))
            days_per_week[week_index] += 1
            primary_item = items[entries[0][0].work_item_id]
            milestone = milestones.get(primary_item.stage_order) or fallback_milestone
            day = PlanDay(
                plan_version_id=plan.id,
                milestone_id=milestone.id,
                stage_order=milestone.stage_order,
                week_index=week_index,
                day_index=days_per_week[week_index],
                primary_goal="；".join(
                    items[draft.work_item_id].title for draft, _ in entries
                )[:500],
                suggested_minutes=sum(
                    items[draft.work_item_id].estimate_minutes for draft, _ in entries
                ),
                evidence_json=[],
                do_not_json=["不要把窗口内的建议日期理解为不可调整的硬截止日。"],
            )
            self.session.add(day)
            self.session.flush()
            self.session.add(
                PlanDaySchedule(
                    activation_id=activation.id,
                    plan_day_id=day.id,
                    scheduled_date=scheduled_date,
                )
            )
            for order, (draft, occurrence) in enumerate(entries, start=1):
                item = items[draft.work_item_id]
                window = window_rows[(draft.work_item_id, draft.period_key)]
                task = PlanTask(
                    plan_version_id=plan.id,
                    plan_day_id=day.id,
                    source_work_item_id=item.id,
                    work_item_window_id=window.id,
                    occurrence_index=occurrence,
                    materialization_key=(
                        f"work-item:{activation.id}:{item.item_key}:{draft.period_key}:{occurrence}"
                    ),
                    materialized_at=utc_now(),
                    origin_stage_order=item.stage_order or milestone.stage_order,
                    lineage_task_id=None,
                    order_in_day=order,
                    title=item.title,
                    description=item.description,
                    estimate_minutes=item.estimate_minutes,
                    priority=item.priority,
                    task_kind=item.task_kind,
                    is_required=item.is_required,
                    evidence_json=item.evidence_json,
                    depends_on_json=[],
                    satisfies_commitments_json=item.commitment_refs_json,
                    ai_schedule_requested=False,
                    is_splittable=item.is_splittable,
                    scheduling_policy="DATE_WINDOW",
                    scheduling_policy_json={
                        "eligible_from": draft.eligible_from.isoformat(),
                        "eligible_to": draft.eligible_to.isoformat(),
                        "work_item_key": item.item_key,
                        "dependency_period_key": draft.period_key if draft.period_key != "once" else f"week:{week_index}",
                    },
                    status="NOT_STARTED",
                )
                self.session.add(task)
                self.session.flush()
                item.status = "ACTIVE"

        self._bind_dependencies(plan, activation)

        operation.status = "APPLIED"
        operation.applied_at = utc_now()
        operation.result_plan_version_id = plan.id
        return activation

    def attach_authored(self, plan, activation, payload) -> None:
        """Initialize the same semantic windows while retaining authored actions.

        This runs only on first activation, before any execution history exists.
        Unselected/triggered templates are retained in the semantic records,
        but never published as today's ordinary executable actions.
        """
        drafts = self._resolve(plan, payload, strict=False)
        items = {item.id: item for item in self.session.scalars(select(PlanWorkItem).where(
            PlanWorkItem.plan_version_id == plan.id))}
        by_key = {item.item_key: item for item in items.values()}
        dependencies = list(self.session.scalars(select(WorkItemDependency).where(WorkItemDependency.plan_version_id == plan.id)))
        selected = {draft.work_item_id for draft in drafts}
        dates = {row.plan_day_id: row.scheduled_date for row in self.session.scalars(
            select(PlanDaySchedule).where(PlanDaySchedule.activation_id == activation.id))}
        windows = []
        for draft in drafts:
            row = WorkItemWindow(
                activation_id=activation.id, work_item_id=draft.work_item_id,
                period_key=draft.period_key, eligible_from=draft.eligible_from,
                eligible_to=draft.eligible_to, required_value=draft.required_value,
                status='PENDING' if draft.pending_trigger else 'OPEN',
                resolution_json={'resolver': 'AUTHORED_ACTIONS_V1', 'authored_tasks': []},
                materialization_revision=1, opened_at=None if draft.pending_trigger else utc_now())
            self.session.add(row)
            windows.append(row)
        self.session.flush()
        removed_ids = set()
        remaining = []
        occurrence_indexes = {}
        for task in self.session.scalars(select(PlanTask).where(
            PlanTask.plan_version_id == plan.id).order_by(PlanTask.plan_day_id, PlanTask.order_in_day)):
            metadata = dict(task.scheduling_policy_json or {})
            keys = metadata.get('work_item_keys') or []
            primary = items.get(task.source_work_item_id)
            if primary is None and keys:
                primary = by_key.get(keys[0])
            if primary is None:
                # Do not invent semantic identity for historical unbound actions.
                remaining.append(task)
                continue
            dated = dates[task.plan_day_id]
            candidates = [window for window in windows if window.work_item_id == primary.id
                          and window.eligible_from <= dated <= window.eligible_to]
            if primary.id not in selected or primary.binding_mode == 'TRIGGERED':
                template = {'title': task.title, 'description': task.description,
                            'estimate_minutes': task.estimate_minutes,
                             'evidence': task.evidence_json, 'work_item_keys': keys,
                             'occurrence_key': metadata.get('occurrence_key'),
                             'dependency_period_key': metadata.get('dependency_period_key') or f'week:{self.session.get(PlanDay, task.plan_day_id).week_index}'}
                if candidates:
                    window = candidates[0]
                    window.resolution_json = {**window.resolution_json,
                                              'authored_tasks': [*window.resolution_json['authored_tasks'], template]}
                elif primary.group_id:
                    group = self.session.get(PlanWorkItemGroup, primary.group_id)
                    saved = dict(group.constraints_json or {})
                    templates = dict(saved.get('authored_task_templates') or {})
                    templates[primary.item_key] = [*templates.get(primary.item_key, []), template]
                    group.constraints_json = {**saved, 'authored_task_templates': templates}
                removed_ids.add(task.id)
                self.session.delete(task)
                continue
            if not candidates:
                raise HTTPException(409, f'“{task.title}”的建议执行日超出工作项窗口，请重新预览。')
            for key in keys or [primary.item_key]:
                related = by_key.get(key)
                if related is None:
                    raise HTTPException(409, "任务引用的工作项已变化，请重新预览。")
                temporal = self.resolved_temporal(plan, related, dependencies)
                start, end = self._date_range(plan, payload, temporal)
                if (not start <= dated <= end
                        or (temporal.get("allowed_weekdays") and dated.weekday() not in temporal["allowed_weekdays"])
                        or dated.isoformat() in {str(value) for value in temporal.get("excluded_dates") or []}):
                    raise HTTPException(409, f'“{task.title}”与已确认的日期/星期约束冲突，请重新预览。')
            window = candidates[0]
            task.source_work_item_id = primary.id
            task.work_item_window_id = window.id
            identity = str(metadata.get('occurrence_key') or f'WEEK-DAY:{task.plan_day_id}')
            index = occurrence_indexes.setdefault((primary.id, window.id), {})
            if identity not in index:
                index[identity] = len(index) + 1
            task.occurrence_index = index[identity]
            task.materialization_key = f'authored:{activation.id}:{task.id}'
            task.materialized_at = utc_now()
            task.scheduling_policy_json = {**metadata, 'eligible_from': window.eligible_from.isoformat(),
                                          'eligible_to': window.eligible_to.isoformat(),
                                          'dependency_period_key': metadata.get('dependency_period_key') or f'week:{self.session.get(PlanDay, task.plan_day_id).week_index}'}
            primary.status = 'ACTIVE'
            remaining.append(task)
        for task in remaining:
            task.depends_on_json = [value for value in task.depends_on_json or []
                                    if value not in removed_ids]
        self.session.flush()
        self._bind_dependencies(plan, activation)

    def _bind_dependencies(self, plan, activation):
        from planner_api.work_item_dependencies import DependencyBindingError, WorkItemDependencyGraph
        try:
            graph = WorkItemDependencyGraph(self.session, plan.id)
            graph.validate_dates(self.session, activation.id)
            graph.bind()
        except DependencyBindingError as error:
            raise HTTPException(409, {"code": "DEPENDENCY_BINDING_INVALID", "message": str(error)}) from error

    def _resolve(
        self,
        plan: PlanVersion,
        payload: ExecutionScheduleRequest,
        *,
        strict: bool = True,
    ) -> list[WindowDraft]:
        items = list(
            self.session.scalars(
                select(PlanWorkItem)
                .where(
                    PlanWorkItem.plan_version_id == plan.id,
                    PlanWorkItem.status.in_(["DRAFT", "ACTIVE"]),
                )
                .order_by(PlanWorkItem.priority, PlanWorkItem.item_key)
            )
        )
        selected_ids = self._selected_item_ids(plan.id, items, plan_selection=payload.selected_work_item_ids)
        items = [item for item in items if item.id in selected_ids]
        dependencies = list(
            self.session.scalars(
                select(WorkItemDependency).where(WorkItemDependency.plan_version_id == plan.id)
            )
        )
        predecessor_edges: dict[str, list[WorkItemDependency]] = defaultdict(list)
        for edge in dependencies:
            predecessor_edges[edge.successor_work_item_id].append(edge)
        ordered = self._topological(items, dependencies)
        milestones = {
            milestone.stage_order: milestone
            for milestone in self.session.scalars(
                select(PlanMilestone).where(PlanMilestone.plan_version_id == plan.id)
            )
        }
        projection_end = None
        if plan.planning_mode == "EXPLORATORY":
            planned_weeks = max(1, min(plan.total_weeks, plan.next_unplanned_week - 1))
            projection_end = plan_week_range(plan, payload.requested_start_date, planned_weeks)[1]
        selected_by_item: dict[str, list[date]] = defaultdict(list)
        windows: list[WindowDraft] = []
        for item in ordered:
            constraints = item.constraints_json or {}
            binding = str(item.binding_mode)
            temporal = self.resolved_temporal(plan, item, dependencies)
            milestone = milestones.get(item.stage_order)
            if milestone is not None:
                temporal["start_week"] = max(
                    int(temporal.get("start_week") or 1), milestone.start_week
                )
                temporal["end_week"] = min(
                    int(temporal.get("end_week") or plan.total_weeks), milestone.end_week
                )
            recurrence = constraints.get("recurrence") or {}
            if binding == "TRIGGERED":
                start, end = self._date_range(plan, payload, temporal)
                if projection_end is not None:
                    end = min(end, projection_end)
                if start > end:
                    continue
                windows.append(
                    WindowDraft(item.id, item.item_key, "trigger:pending", start, end, 1, (), True)
                )
                continue
            ranges = self._period_ranges(plan, payload, item, temporal, recurrence)
            for period_key, start, end, count in ranges:
                if projection_end is not None:
                    end = min(end, projection_end)
                if start > end:
                    continue
                earliest = start
                for edge in predecessor_edges.get(item.id, []):
                    from planner_api.dependency_matching import matching_mode
                    mode = matching_mode(edge)
                    if mode == "MATCH_OCCURRENCE":
                        # Authored actions carry object identity; ordinal
                        # recurrence counts do not. Never invent their pairing.
                        if strict:
                            raise HTTPException(409, "同次依赖需要带稳定实例标识的具体任务，请先生成每日任务。")
                        continue
                    predecessor_dates = selected_by_item.get(edge.predecessor_work_item_id, [])
                    if mode == "MATCH_PERIOD":
                        predecessor_dates = [value for value in predecessor_dates if start <= value <= end]
                        if strict and not predecessor_dates:
                            raise HTTPException(409, "当前周期缺少对应前置任务，请先补齐周期安排。")
                    if predecessor_dates:
                        earliest = max(
                            earliest,
                            max(predecessor_dates) + timedelta(days=edge.minimum_lag_days),
                        )
                chosen = self._choose_dates(
                    earliest,
                    end,
                    count,
                    payload,
                    temporal,
                    minimum_gap_days=int(recurrence.get("minimum_gap_days") or 0),
                    maximum_per_day=max(
                        1, int(recurrence.get("maximum_per_day") or 1)
                    ),
                    fixed=binding == "FIXED",
                )
                for edge in predecessor_edges.get(item.id, []):
                    if edge.maximum_lag_days is None or matching_mode(edge) == "MATCH_OCCURRENCE":
                        continue
                    dates = selected_by_item.get(edge.predecessor_work_item_id, [])
                    if matching_mode(edge) == "MATCH_PERIOD":
                        dates = [value for value in dates if start <= value <= end]
                    if dates and chosen and max(chosen) > max(dates) + timedelta(days=edge.maximum_lag_days) and strict:
                        raise HTTPException(409, "依赖间隔超过允许上限，请调整窗口或具体任务安排。")
                if len(chosen) < count and strict:
                    raise HTTPException(
                        409,
                        f"“{item.title}”在允许窗口内找不到 {count} 个可执行日期，"
                        "请调整开始日或排除日期。",
                    )
                selected_by_item[item.id].extend(chosen)
                windows.append(
                    WindowDraft(
                        item.id,
                        item.item_key,
                        period_key,
                        start,
                        end,
                        count,
                        tuple(chosen),
                    )
                )
        return windows

    def _selected_item_ids(
        self, plan_id: str, items: list[PlanWorkItem], *, plan_selection: list[str] | None = None
    ) -> set[str]:
        selected = {item.id for item in items if item.group_id is None}
        by_group: dict[str, list[PlanWorkItem]] = defaultdict(list)
        for item in items:
            if item.group_id:
                by_group[item.group_id].append(item)
        groups = {
            item.id: item
            for item in self.session.scalars(
                select(PlanWorkItemGroup).where(PlanWorkItemGroup.plan_version_id == plan_id)
            )
        }
        if plan_selection:
            item_by_id = {item.id: item for item in items}
            unknown = set(plan_selection) - set(item_by_id)
            if unknown or any(item_by_id[item_id].group_id is None for item_id in plan_selection if item_id in item_by_id):
                raise HTTPException(422, "只能选择候选任务池中的任务。")
        for group_id, candidates in by_group.items():
            group = groups[group_id]
            requested = set(plan_selection or [])
            unknown = requested - {item.id for item in items}
            if unknown:
                raise HTTPException(422, "候选任务选择包含不属于当前规划的任务。")
            if plan_selection:
                chosen = [item for item in candidates if item.id in requested]
                if len(chosen) < group.minimum_selected:
                    raise HTTPException(
                        422,
                        f"候选组“{group.title}”至少需要选择 {group.minimum_selected} 项。",
                    )
                if group.maximum_selected is not None and len(chosen) > group.maximum_selected:
                    raise HTTPException(
                        422,
                        f"候选组“{group.title}”最多只能选择 {group.maximum_selected} 项。",
                    )
            else:
                count = group.minimum_selected
                chosen = sorted(candidates, key=lambda item: (item.priority, item.item_key))[:count]
            selected.update(item.id for item in chosen)
        if plan_selection:
            grouped_ids = {item.id for group in by_group.values() for item in group}
            invalid = set(plan_selection) - grouped_ids - selected
            if invalid:
                raise HTTPException(422, "只能选择候选任务池中的任务。")
        return selected

    @staticmethod
    def _topological(
        items: list[PlanWorkItem], dependencies: list[WorkItemDependency]
    ) -> list[PlanWorkItem]:
        item_by_id = {item.id: item for item in items}
        incoming = {item.id: 0 for item in items}
        outgoing: dict[str, list[str]] = defaultdict(list)
        for edge in dependencies:
            if (
                edge.predecessor_work_item_id in incoming
                and edge.successor_work_item_id in incoming
            ):
                incoming[edge.successor_work_item_id] += 1
                outgoing[edge.predecessor_work_item_id].append(edge.successor_work_item_id)
        ready = sorted(
            (item for item in items if incoming[item.id] == 0),
            key=lambda item: (item.priority, item.item_key),
        )
        result: list[PlanWorkItem] = []
        while ready:
            item = ready.pop(0)
            result.append(item)
            for successor in outgoing[item.id]:
                incoming[successor] -= 1
                if incoming[successor] == 0:
                    ready.append(item_by_id[successor])
                    ready.sort(key=lambda candidate: (candidate.priority, candidate.item_key))
        if len(result) != len(items):
            raise HTTPException(409, "工作项依赖形成循环，不能安排日期。")
        return result

    def _period_ranges(self, plan, payload, item, temporal, recurrence):
        start, end = self._date_range(plan, payload, temporal)
        period = str(recurrence.get("period") or "ONCE")
        count = max(1, int(recurrence.get("target_count") or item.target_value or 1))
        if str(item.binding_mode) != "CADENCE" or period == "ONCE":
            return [("once", start, end, count)]
        if period == "WEEK":
            ranges = []
            cursor = start
            while cursor <= end:
                boundary = min(end, cursor + timedelta(days=6 - cursor.weekday())
                               if calendar_weeks(plan) else cursor + timedelta(days=6))
                week = max(1, plan_week_index(plan, payload.requested_start_date, cursor))
                ranges.append((f"week:{week}", cursor, boundary, count))
                cursor = boundary + timedelta(days=1)
            return ranges
        if period == "DAY":
            return [
                (f"day:{index}", value, value, count)
                for index, value in enumerate(self._date_span(start, end), start=1)
            ]
        if period == "MONTH":
            grouped: dict[tuple[int, int], list[date]] = defaultdict(list)
            for value in self._date_span(start, end):
                grouped[(value.year, value.month)].append(value)
            return [
                (f"month:{year:04d}-{month:02d}", values[0], values[-1], count)
                for (year, month), values in grouped.items()
            ]
        return [(f"{period.lower()}:1", start, end, count)]

    def resolved_temporal(self, plan, item, dependencies=None):
        temporal = self.item_temporal(plan, item, dependencies)
        relative = temporal.get("relative_day_ref")
        if relative and item.binding_mode == "WINDOW" and not temporal.get("exact_date"):
            # Older compiler output sometimes put an explicitly authored
            # deadline into the exact-relative-date slot. Require independent
            # structured task evidence before interpreting that legacy value.
            references = set()
            for task in self.session.scalars(select(PlanTask).where(PlanTask.plan_version_id == plan.id)):
                policy = task.scheduling_policy_json or {}
                if (task.source_work_item_id == item.id or item.item_key in (policy.get("work_item_keys") or [])):
                    references.add(policy.get("deadline_relative_day_ref"))
            if references == {relative}:
                temporal["relative_day_ref"] = None
                temporal["deadline_relative_day_ref"] = relative
        return temporal

    @staticmethod
    def item_temporal(plan, item, dependencies=None):
        temporal = dict((item.constraints_json or {}).get("temporal") or {})
        named = re.search(r"(?:周|星期)([一二三四五六日天])\s*\d{1,2}[:：]\d{2}", getattr(item, "title", ""))
        if named and not re.search(r"建议|可选|或者|或周", item.title):
            weekday = "一二三四五六日".index(named[1].replace("天", "日"))
            if temporal.get("allowed_weekdays") and weekday not in temporal["allowed_weekdays"]:
                raise HTTPException(409, {"code": "PLAN_ITEM_DATE_CONFLICT", "message": "任务写出的时段与星期规则矛盾，请先讨论调整。"})
            temporal["allowed_weekdays"] = [weekday]
        relative = str(temporal.get("relative_day_ref") or "")
        # Historical AFTER references require a persisted gate, not a guessed
        # first day. Existing dependency ordering determines the actual floor.
        after = re.fullmatch(r"W([1-9]\d*)_AFTER_[A-Z][A-Z0-9_]*", relative)
        if after and dependencies is not None and any(
                edge.successor_work_item_id == item.id for edge in dependencies):
            temporal["relative_day_ref"] = f"W{after[1]}"
        return temporal

    @staticmethod
    def _date_range(plan, payload, temporal):
        relative_deadline = None
        if temporal.get("deadline_relative_day_ref"):
            resolved_deadline = relative_day_range(str(temporal["deadline_relative_day_ref"]), payload.requested_start_date, plan=plan)
            if resolved_deadline is None:
                raise HTTPException(409, "相对截止日期不合法，请重新确认。")
            relative_deadline = resolved_deadline[1]
        exact = temporal.get("exact_date")
        if exact:
            value = date.fromisoformat(str(exact))
            lower = max([value, *[date.fromisoformat(str(temporal[key])) for key in ("earliest_date",) if temporal.get(key)]])
            upper = min([value, *[date.fromisoformat(str(temporal[key])) for key in ("latest_date", "deadline") if temporal.get(key)]])
            if relative_deadline:
                upper = min(upper, relative_deadline)
            return lower, upper
        relative = str(temporal.get("relative_day_ref") or "")
        if relative:
            resolved = relative_day_range(relative, payload.requested_start_date, plan=plan)
            legacy = re.fullmatch(
                r"W([1-9]\d*)_(MONDAY|TUESDAY|WEDNESDAY|THURSDAY|FRIDAY|SATURDAY|SUNDAY)(?:_(\d{4})_(\d{4}))?",
                relative)
            if resolved is None and legacy:
                weekday = ("MONDAY", "TUESDAY", "WEDNESDAY", "THURSDAY", "FRIDAY", "SATURDAY", "SUNDAY").index(legacy[2])
                if temporal.get("allowed_weekdays") and weekday not in temporal["allowed_weekdays"]:
                    raise HTTPException(409, "历史相对日期中的星期与已确认星期冲突，请讨论修正。")
                if legacy[3]:
                    from datetime import time
                    try:
                        start_time = time(int(legacy[3][:2]), int(legacy[3][2:]))
                        end_time = time(int(legacy[4][:2]), int(legacy[4][2:]))
                        if start_time >= end_time:
                            raise ValueError("time range")
                    except ValueError as error:
                        raise HTTPException(409, "历史日期引用的连续时段不合法，请重新确认。") from error
                week_start, week_end = plan_week_range(plan, payload.requested_start_date, int(legacy[1]))
                value = week_start + timedelta(days=(weekday - week_start.weekday()) % 7)
                if value > week_end:
                    raise HTTPException(409, {"code": "PLAN_ITEM_DATE_CONFLICT",
                        "message": "首周剩余日期不包含指定星期，请讨论调整，不能挪到下一周。"})
                resolved = value, value
            if resolved is None:
                raise HTTPException(409, {"code": "WORK_ITEM_RELATIVE_DATE_INVALID",
                    "message": f"相对日期“{relative}”需要重新整理：请明确第几周、星期或先后依赖，不能猜成第一天。"})
            start, end = resolved
        else:
            start_week = int(temporal.get("start_week") or 1)
            end_week = int(temporal.get("end_week") or plan.total_weeks)
            start = plan_week_range(plan, payload.requested_start_date, start_week)[0]
            end = plan_week_range(plan, payload.requested_start_date, end_week)[1]
        if temporal.get("earliest_date"):
            start = max(start, date.fromisoformat(str(temporal["earliest_date"])))
        for key in ("latest_date", "deadline"):
            if temporal.get(key):
                end = min(end, date.fromisoformat(str(temporal[key])))
        if relative_deadline:
            end = min(end, relative_deadline)
        return start, end

    def _choose_dates(
        self,
        start,
        end,
        count,
        payload,
        temporal,
        *,
        minimum_gap_days,
        maximum_per_day,
        fixed,
    ):
        excluded = set(payload.excluded_dates) | {
            date.fromisoformat(str(item)) for item in temporal.get("excluded_dates") or []
        }
        allowed_weekdays = set(temporal.get("allowed_weekdays") or payload.execution_weekdays)
        result: list[date] = []
        for value in self._date_span(start, end):
            if value in excluded:
                continue
            if temporal.get("allowed_weekdays") and value.weekday() not in temporal["allowed_weekdays"]:
                continue
            if not fixed and not self.date_allowed(value):
                continue
            if not fixed and value.weekday() not in allowed_weekdays:
                continue
            if result and (value - result[-1]).days <= minimum_gap_days:
                continue
            result.append(value)
            if len(result) == count:
                return result
        if result and len(result) < count and minimum_gap_days == 0:
            capacity = len(result) * maximum_per_day
            if capacity >= count:
                repetitions, remainder = divmod(count, len(result))
                return sorted(result * repetitions + result[:remainder])
        return result

    @staticmethod
    def _date_span(start: date, end: date):
        cursor = start
        while cursor <= end:
            yield cursor
            cursor += timedelta(days=1)

    @staticmethod
    def _window_json(item: WindowDraft):
        return {
            "work_item_id": item.work_item_id,
            "item_key": item.item_key,
            "period_key": item.period_key,
            "eligible_from": item.eligible_from.isoformat(),
            "eligible_to": item.eligible_to.isoformat(),
            "required_value": item.required_value,
            "selected_dates": [value.isoformat() for value in item.selected_dates],
            "pending_trigger": item.pending_trigger,
        }

    @staticmethod
    def _window_from_json(item):
        return WindowDraft(
            work_item_id=str(item["work_item_id"]),
            item_key=str(item["item_key"]),
            period_key=str(item["period_key"]),
            eligible_from=date.fromisoformat(str(item["eligible_from"])),
            eligible_to=date.fromisoformat(str(item["eligible_to"])),
            required_value=int(item["required_value"]),
            selected_dates=tuple(
                date.fromisoformat(str(value)) for value in item["selected_dates"]
            ),
            pending_trigger=bool(item.get("pending_trigger")),
        )

    def _scheduled_days(
        self, windows: list[WindowDraft], requested_start_date: date, plan=None
    ):
        item_ids = {item.work_item_id for item in windows}
        items = {
            item.id: item
            for item in self.session.scalars(
                select(PlanWorkItem).where(PlanWorkItem.id.in_(item_ids))
            )
        }
        by_date: dict[date, list[WindowDraft]] = defaultdict(list)
        for window in windows:
            for value in window.selected_dates:
                by_date[value].append(window)
        per_week: dict[int, int] = defaultdict(int)
        result = []
        for value in sorted(by_date):
            week = max(1, plan_week_index(plan, requested_start_date, value))
            per_week[week] += 1
            scheduled_items = [items[item.work_item_id] for item in by_date[value]]
            titles = [item.title for item in scheduled_items]
            stage_orders = [item.stage_order for item in scheduled_items if item.stage_order]
            result.append(
                ScheduledDayRead(
                    plan_day_id=f"preview:{value.isoformat()}",
                    stage_order=min(stage_orders, default=1),
                    week_index=week,
                    day_index=per_week[week],
                    code=f"W{week}-D{per_week[week]}",
                    scheduled_date=value,
                    primary_goal="、".join(titles),
                )
            )
        return result

    def _source_fingerprint(self, plan: PlanVersion, payload: ExecutionScheduleRequest) -> str:
        items = list(
            self.session.scalars(
                select(PlanWorkItem)
                .where(PlanWorkItem.plan_version_id == plan.id)
                .order_by(PlanWorkItem.item_key)
            )
        )
        edges = list(
            self.session.scalars(
                select(WorkItemDependency)
                .where(WorkItemDependency.plan_version_id == plan.id)
                .order_by(
                    WorkItemDependency.predecessor_work_item_id,
                    WorkItemDependency.successor_work_item_id,
                )
            )
        )
        source = {
            "plan": [plan.id, plan.revision, plan.status],
            "request": payload.model_dump(mode="json", exclude={"materialization_preview_id"}),
            "items": [
                [
                    item.id,
                    item.item_key,
                    item.status,
                    item.updated_at.isoformat(),
                    item.title,
                    item.description,
                    item.priority,
                    item.estimate_minutes,
                    item.constraints_json,
                ]
                for item in items
            ],
            "edges": [
                [edge.predecessor_work_item_id, edge.successor_work_item_id, edge.dependency_type,
                 edge.matching_mode, edge.minimum_lag_days, edge.maximum_lag_days, edge.condition_json]
                for edge in edges
            ],
        }
        return hashlib.sha256(
            json.dumps(source, ensure_ascii=False, sort_keys=True).encode()
        ).hexdigest()
