from __future__ import annotations

import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from planner_api.models import GenerationBatch, PlanDay, PlanTask, ProgressEvent


class PlanDependencyResolver:
    """Resolve persisted task dependencies without guessing ambiguous references."""

    def __init__(self, session: Session | None) -> None:
        self.session = session
        self._cache: dict[str, dict[str, object]] = {}

    @staticmethod
    def _chinese_number(value: str) -> int | None:
        if value.isdigit():
            return int(value)
        digits = {
            "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
            "六": 6, "七": 7, "八": 8, "九": 9, "零": 0,
        }
        if value in digits:
            return digits[value]
        if value == "十":
            return 10
        if value.startswith("十"):
            return 10 + digits.get(value[1:], 0)
        if value.endswith("十") and value[:-1] in digits:
            return digits[value[:-1]] * 10
        if (
            len(value) == 3
            and value[1] == "十"
            and value[0] in digits
            and value[2] in digits
        ):
            return digits[value[0]] * 10 + digits[value[2]]
        return None

    def indexes(self, plan_version_id: str) -> dict[str, object]:
        if self.session is None:
            raise RuntimeError("A database session is required to build dependency indexes")
        cached = self._cache.get(plan_version_id)
        if cached is not None:
            return cached
        days = list(self.session.scalars(
            select(PlanDay).where(PlanDay.plan_version_id == plan_version_id)
        ))
        tasks = list(self.session.scalars(
            select(PlanTask).where(PlanTask.plan_version_id == plan_version_id)
        ))
        ordered_days = sorted(days, key=lambda item: (item.week_index, item.day_index))
        by_day = {
            index: sorted(
                [task for task in tasks if task.plan_day_id == day.id],
                key=lambda task: task.order_in_day,
            )
            for index, day in enumerate(ordered_days, start=1)
        }
        tasks_by_title: dict[str, list[PlanTask]] = {}
        tasks_by_evidence: dict[str, list[PlanTask]] = {}
        for task in tasks:
            tasks_by_title.setdefault(task.title.strip(), []).append(task)
            for evidence in task.evidence_json or []:
                if isinstance(evidence, str) and evidence.strip():
                    tasks_by_evidence.setdefault(evidence.strip(), []).append(task)
        # Canonical references describe authored coordinates. A task moved in
        # the same version must not disappear (or resolve to a new occupant).
        # Recover legacy moves only from the saved compiler output AND an
        # explicit move event; ambiguous or edited identities fail closed.
        day_by_id = {day.id: day for day in days}
        move_origins: dict[str, set[tuple[int, int, int]]] = {}
        for event in self.session.scalars(select(ProgressEvent).where(
            ProgressEvent.plan_version_id == plan_version_id,
            ProgressEvent.event_type.in_(("CARRY_FORWARD", "RESCHEDULED")),
        )):
            source = day_by_id.get((event.metadata_json or {}).get("source_day_id"))
            if source is not None and not (event.metadata_json or {}).get("partial"):
                move_origins.setdefault(event.task_id, set()).add(
                    (source.stage_order, source.week_index, source.day_index)
                )
        authored: dict[tuple[int, int, int, int], set[str]] = {}
        for batch in self.session.scalars(select(GenerationBatch).where(
            GenerationBatch.plan_version_id == plan_version_id,
            GenerationBatch.scope_type == "WEEK", GenerationBatch.status == "COMPLETED",
        )):
            for raw_day in (batch.staged_result_json or {}).get("days", []):
                coordinate = tuple(raw_day.get(key) for key in ("stage_order", "week_index", "day_index"))
                for raw in raw_day.get("tasks", []):
                    key = (*coordinate, raw.get("order"))
                    matches = []
                    for task in tasks_by_title.get(str(raw.get("title", "")).strip(), []):
                        current_day = day_by_id.get(task.plan_day_id)
                        current = ((current_day.stage_order, current_day.week_index, current_day.day_index)
                                   if current_day else None)
                        if (task.description == raw.get("description")
                            and (task.evidence_json or []) == raw.get("evidence", [])
                            and ((current == coordinate and task.order_in_day == raw.get("order"))
                                 or coordinate in move_origins.get(task.id, set()))):
                            matches.append(task.id)
                    authored.setdefault(key, set()).update(matches)
        cached = {
            "tasks": tasks,
            "tasks_by_id": {task.id: task for task in tasks},
            "tasks_by_title": tasks_by_title,
            "tasks_by_evidence": tasks_by_evidence,
            "by_day": by_day,
            "by_authored_coordinate": authored,
            "by_coordinate": {
                (day.stage_order, day.week_index, day.day_index, task.order_in_day): task
                for day in days
                for task in tasks
                if task.plan_day_id == day.id
            },
        }
        self._cache[plan_version_id] = cached
        return cached

    @staticmethod
    def _unique_match(indexes: dict[str, object], key: str, value: str) -> PlanTask | None:
        index = indexes.get(key, {})
        if not isinstance(index, dict):
            return None
        matches = index.get(value, [])
        return matches[0] if isinstance(matches, list) and len(matches) == 1 else None

    def _resolve_in_plan(
        self,
        normalized: str,
        task: PlanTask,
        indexes: dict[str, object],
    ) -> PlanTask | None:
        tasks_by_id = indexes.get("tasks_by_id", {})
        if isinstance(tasks_by_id, dict) and normalized in tasks_by_id:
            return tasks_by_id[normalized]

        canonical = re.fullmatch(
            r"M\s*(\d+)\s*-\s*W\s*(\d+)\s*-\s*D\s*(\d+)\s*-\s*T\s*(\d+)",
            normalized,
            re.IGNORECASE,
        )
        if canonical:
            coordinate = tuple(map(int, canonical.groups()))
            authored = indexes.get("by_authored_coordinate", {})
            if coordinate in authored:
                matches = authored[coordinate]
                return tasks_by_id.get(next(iter(matches))) if len(matches) == 1 else None
            by_coordinate = indexes.get("by_coordinate", {})
            if isinstance(by_coordinate, dict):
                return by_coordinate.get(coordinate)

        resolved = self._unique_match(indexes, "tasks_by_title", normalized)
        if resolved is not None:
            return resolved
        resolved = self._unique_match(indexes, "tasks_by_evidence", normalized)
        if resolved is not None:
            return resolved

        day_reference = re.search(
            r"第\s*([一二三四五六七八九十百两\d]+)\s*[天日].*?任务\s*(\d+)",
            normalized,
        )
        if day_reference:
            day_number = self._chinese_number(day_reference.group(1))
            task_number = int(day_reference.group(2))
            by_day = indexes.get("by_day", {})
            if isinstance(by_day, dict) and day_number in by_day:
                return next(
                    (item for item in by_day[day_number] if item.order_in_day == task_number),
                    None,
                )

        generic = re.fullmatch(r"任务\s*(\d+)", normalized)
        if generic:
            task_number = int(generic.group(1))
            by_day = indexes.get("by_day", {})
            if isinstance(by_day, dict):
                current_day_tasks = next(
                    (
                        items for items in by_day.values()
                        if items and items[0].plan_day_id == task.plan_day_id
                    ),
                    [],
                )
                matches = [item for item in current_day_tasks if item.order_in_day == task_number]
                return matches[0] if len(matches) == 1 else None
        return None

    def _map_source_to_current(
        self,
        source: PlanTask,
        current_plan_id: str,
        indexes: dict[str, object],
    ) -> PlanTask | None:
        tasks = indexes.get("tasks", [])
        if not isinstance(tasks, list):
            return None
        lineage = source.lineage_task_id or source.id
        candidates = [
            item for item in tasks
            if item.id == source.id
            or item.source_task_id == source.id
            or item.lineage_task_id == lineage
        ]
        if not candidates:
            return source if source.plan_version_id != current_plan_id else None
        candidates.sort(key=lambda item: (
            item.status == "CARRIED_OVER",
            item.title.startswith("补回：") is False,
            item.order_in_day,
        ))
        return candidates[0]

    def resolve(
        self,
        dependency: object,
        task: PlanTask,
        indexes: dict[str, object] | None = None,
    ) -> PlanTask | None:
        if not isinstance(dependency, str) or not dependency.strip():
            return None
        normalized = dependency.strip()
        current_indexes = indexes or self.indexes(task.plan_version_id)

        # Relative references copied into recovery tasks belong to the source
        # task's original day, not the new recovery day.
        source_task_id = getattr(task, "source_task_id", None)
        source_task = (
            self.session.get(PlanTask, source_task_id)
            if source_task_id and self.session is not None
            else None
        )
        is_relative = bool(re.fullmatch(r"任务\s*\d+", normalized))
        if source_task is not None and is_relative:
            source_match = self._resolve_in_plan(
                normalized, source_task, self.indexes(source_task.plan_version_id)
            )
            if source_match is not None:
                return self._map_source_to_current(
                    source_match, task.plan_version_id, current_indexes
                )

        resolved = self._resolve_in_plan(normalized, task, current_indexes)
        if resolved is not None:
            return resolved

        if source_task is not None:
            source_match = self._resolve_in_plan(
                normalized, source_task, self.indexes(source_task.plan_version_id)
            )
            if source_match is not None:
                return self._map_source_to_current(
                    source_match, task.plan_version_id, current_indexes
                )
        return None
