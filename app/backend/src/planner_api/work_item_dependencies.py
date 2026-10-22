"""Resolve semantic edges into concrete tasks without schedule-based guessing."""

from datetime import timedelta
from types import SimpleNamespace

from sqlalchemy import select

from planner_api.dependency_matching import (
    DependencyBindingError,
    matching_mode,
    select_predecessors,
)
from planner_api.models import (
    PlanDay,
    PlanDaySchedule,
    PlanTask,
    PlanWorkItem,
    ProgressEvent,
    WorkItemDependency,
)


class WorkItemDependencyGraph:
    def __init__(self, session, plan_id):
        self.tasks = list(
            session.scalars(select(PlanTask).where(PlanTask.plan_version_id == plan_id))
        )
        self.items = {
            item.id: item
            for item in session.scalars(
                select(PlanWorkItem).where(PlanWorkItem.plan_version_id == plan_id)
            )
        }
        self.edges = list(
            session.scalars(
                select(WorkItemDependency).where(WorkItemDependency.plan_version_id == plan_id)
            )
        )
        self.period_keys = {
            task.id: f"week:{day.week_index}"
            for task, day in session.execute(
                select(PlanTask, PlanDay)
                .join(PlanDay, PlanTask.plan_day_id == PlanDay.id)
                .where(PlanTask.plan_version_id == plan_id)
            )
        }

    def represents(self, task, item_id):
        item = self.items.get(item_id)
        if item is None:
            return False
        data = task.scheduling_policy_json or {}
        return (
            task.source_work_item_id == item_id
            or item.item_key in (data.get("work_item_keys") or [])
            or item.item_key == data.get("work_item_key")
        )

    def predecessors(self, edge, successor):
        candidates = [
            task for task in self.tasks if self.represents(task, edge.predecessor_work_item_id)
        ]
        predecessor_item = self.items.get(edge.predecessor_work_item_id)
        if not candidates and predecessor_item and predecessor_item.binding_mode == "TRIGGERED":
            # An unmaterialized trigger is a closed gate, not a missing hard
            # dependency to erase. Runtime keeps it blocked until activation.
            return []
        return select_predecessors(edge, successor, candidates, period_keys=self.period_keys)

    def bind(self):
        """Publish concrete references for all existing allocators/recovery paths.

        Explicit authored edges are additive and never overwritten. Call only
        at activation/creation, not during read-only projections.
        """
        for edge in self.edges:
            for task in self.tasks:
                if not self.represents(task, edge.successor_work_item_id):
                    continue
                predecessors = self.predecessors(edge, task)
                task.depends_on_json = sorted(
                    set(task.depends_on_json or []) | {part.id for part in predecessors}
                )

    def validate_dates(self, session, activation_id):
        dates = {
            row.plan_day_id: row.scheduled_date
            for row in session.scalars(
                select(PlanDaySchedule).where(PlanDaySchedule.activation_id == activation_id)
            )
        }
        for edge in self.edges:
            for task in self.tasks:
                if not self.represents(task, edge.successor_work_item_id):
                    continue
                predecessors = self.predecessors(edge, task)
                if not predecessors:
                    continue
                if any(dates.get(part.plan_day_id) is None for part in predecessors):
                    raise DependencyBindingError("依赖任务缺少执行日期，请重新预览")
                finished = max(dates[part.plan_day_id] for part in predecessors)
                for predecessor in predecessors:
                    before, after = dates.get(predecessor.plan_day_id), dates.get(task.plan_day_id)
                    if before is None or after is None:
                        raise DependencyBindingError("依赖任务缺少执行日期，请重新预览")
                    if after < finished + timedelta(days=edge.minimum_lag_days):
                        raise DependencyBindingError(
                            f"“{task.title}”早于对应前置任务或必要等待间隔"
                        )
                    if edge.maximum_lag_days is not None and after > finished + timedelta(
                        days=edge.maximum_lag_days
                    ):
                        raise DependencyBindingError(
                            f"“{task.title}”超过对应前置任务允许的最大间隔"
                        )
                    if (
                        before == after
                        and predecessor.plan_day_id == task.plan_day_id
                        and predecessor.order_in_day >= task.order_in_day
                    ):
                        raise DependencyBindingError(f"“{task.title}”必须排在对应前置任务之后")

    def trigger_subject(self, item_id, window):
        """A trigger may match only a single explicitly authored instance."""
        data = dict(window.resolution_json or {})
        templates = data.get("authored_tasks") or []
        for field in ("occurrence_key", "dependency_period_key"):
            values = {part[field] for part in templates if part.get(field)}
            if not data.get(field) and len(values) == 1:
                data[field] = next(iter(values))
        subject = SimpleNamespace(id=None, source_work_item_id=item_id, scheduling_policy_json=data)
        for edge in self.edges:
            if edge.successor_work_item_id == item_id and matching_mode(edge) != "ALL":
                self.predecessors(edge, subject)  # Explicitly reject missing/ambiguous identity.
        return subject

    def execution_lag_error(self, session, edge, predecessors, activation, today, local_date):
        if not edge.minimum_lag_days and edge.maximum_lag_days is None:
            return None
        dates = {
            row.plan_day_id: row.scheduled_date
            for row in session.scalars(
                select(PlanDaySchedule).where(PlanDaySchedule.activation_id == activation.id)
            )
        }
        completed = {}
        for event in session.scalars(
            select(ProgressEvent).where(
                ProgressEvent.plan_version_id == activation.plan_version_id,
                ProgressEvent.task_id.in_([task.id for task in predecessors]),
                ProgressEvent.event_type == "COMPLETED",
            )
        ):
            stamp = local_date(event.occurred_at)
            completed[event.task_id] = max(stamp, completed.get(event.task_id, stamp))
        finishes = [completed.get(part.id) or dates.get(part.plan_day_id) for part in predecessors]
        if not finishes or any(value is None for value in finishes):
            return "前置任务缺少完成日期，请先确认依赖"
        finished = max(finishes)
        if today < finished + timedelta(days=edge.minimum_lag_days):
            return f"前置完成后仍需等待，最早可在{finished + timedelta(days=edge.minimum_lag_days)}开始"
        if edge.maximum_lag_days is not None and today > finished + timedelta(
            days=edge.maximum_lag_days
        ):
            return "已超过前置完成后的允许间隔，请先讨论调整安排"
        return None


__all__ = ["DependencyBindingError", "WorkItemDependencyGraph"]
