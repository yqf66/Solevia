from __future__ import annotations

from collections.abc import Iterable
from collections import Counter
from dataclasses import dataclass
import re

from planner_api.work_item_domain import (
    PlanWorkItemDraft,
    PlanWorkItemGroupDraft,
    WorkItemDependencyDraft,
)
from planner_api.temporal_contract import relative_day_range
from datetime import date


@dataclass(frozen=True)
class WorkItemValidationError(ValueError):
    code: str
    issues: tuple[str, ...]
    detail: dict | None = None

    def __str__(self) -> str:
        return "; ".join(self.issues)


def validate_authored_dependency_order(days, dependencies) -> None:
    """Validate the same explicit binding that execution will enforce."""
    from planner_api.dependency_matching import (
        DependencyBindingError, matching_mode, select_predecessors,
    )

    by_item, positions, actions = {}, {}, {}
    atomic = set()
    for day in days:
        for task in day.tasks:
            position = (day.week_index, day.day_index, task.order)
            positions[id(task)] = position
            actions[id(task)] = {"week": day.week_index, "day": day.day_index,
                                 "task_order": task.order, "title": getattr(task, "title", ""),
                                 "work_item_keys": list(task.work_item_keys)}
            for key in task.work_item_keys:
                by_item.setdefault(key, []).append(task)
            if not task.is_splittable and len(task.stage_contributions) > 1:
                atomic.add(id(task))
    # Drafts have no database ID; use explicit/default period metadata on a
    # light wrapper so cross-week matches do not share the None dictionary key.
    from types import SimpleNamespace
    wrappers = {}
    for day in days:
        for task in day.tasks:
            data = dict(getattr(task, "scheduling_policy_data", None) or {})
            data.setdefault("dependency_period_key", f"week:{day.week_index}")
            wrappers[id(task)] = SimpleNamespace(
                occurrence_key=getattr(task, "occurrence_key", None),
                scheduling_policy_data=data, original=task)
    issues, conflicts = [], []
    for edge in dependencies:
        if edge.get("dependency_type", "FINISH_TO_START") != "FINISH_TO_START":
            continue
        before = by_item.get(edge["predecessor_key"], [])
        after = by_item.get(edge["successor_key"], [])
        try:
            mode = matching_mode(edge)
            if not after or (not before and mode == "ALL"):
                continue
            for successor in after:
                matched = ([wrappers[id(task)] for task in before] if mode == "ALL" else
                    select_predecessors(edge, wrappers[id(successor)],
                        [wrappers[id(task)] for task in before]))
                if not matched:
                    continue
                predecessor = max((part.original for part in matched), key=lambda task: positions[id(task)])
                last, first = positions[id(predecessor)], positions[id(successor)]
                own_atomic = (predecessor is successor and id(successor) in atomic
                              and len(before) == 1 and not edge.get("minimum_lag_days")
                              and not edge.get("condition"))
                if last >= first and not own_atomic:
                    label = "全局前置" if mode == "ALL" else "对应前置"
                    issues.append(f"{label} {edge['predecessor_key']} 尚未完成，后置 {edge['successor_key']} 已被安排执行；请修正关联实例的先后顺序，不能阻断早期练习。")
                    conflicts.append({"predecessor_key": edge["predecessor_key"],
                        "successor_key": edge["successor_key"], "matching_mode": mode,
                        "last_prerequisite_action": actions[id(predecessor)],
                        "first_dependent_action": actions[id(successor)]})
        except DependencyBindingError as error:
            issues.append(f"{edge['predecessor_key']} -> {edge['successor_key']}: {error}")
            conflicts.append({**edge, "binding_error": str(error)})
    if issues:
        raise WorkItemValidationError("WORK_ITEM_EXECUTION_ORDER_INVALID", tuple(issues), {
            "code": "WORK_ITEM_EXECUTION_ORDER_INVALID", "diagnostic": issues,
            "conflicts": conflicts,
            "repair_policy": "按matching_mode修复关联：同次用稳定occurrence_key，同周期用dependency_period_key，整组ALL保留全部前置。仅修复冲突实例及受影响后续，保留总投入、全部产出和硬约束；不能删除依赖或把逐次关系改成整组等待。",
        })


def validate_work_item_batch(
    *,
    work_items: list[PlanWorkItemDraft],
    groups: list[PlanWorkItemGroupDraft],
    dependencies: list[WorkItemDependencyDraft],
    milestone_weeks: dict[int, tuple[int, int]],
    commitment_labels: set[str],
    existing_item_keys: Iterable[str] = (),
    existing_group_keys: Iterable[str] = (),
    existing_group_candidates: dict[str, set[str]] | None = None,
    existing_edges: Iterable[tuple[str, str]] = (),
    requested_weeks: set[int] | None = None,
    referenced_item_weeks: dict[str, set[int]] | None = None,
) -> None:
    issues: list[str] = []
    prior_items = set(existing_item_keys)
    prior_groups = set(existing_group_keys)
    local_items = {item.item_key for item in work_items}
    local_groups = {group.group_key for group in groups}

    duplicate_items = [key for key, count in Counter(item.item_key for item in work_items).items() if count > 1]
    duplicate_groups = [key for key, count in Counter(group.group_key for group in groups).items() if count > 1]
    if duplicate_items:
        issues.append(f"工作项键重复: {sorted(duplicate_items)[:8]}")
    if duplicate_groups:
        issues.append(f"候选组键重复: {sorted(duplicate_groups)[:8]}")

    if prior_items & local_items:
        issues.append(f"工作项键重复: {sorted(prior_items & local_items)[:8]}")
    if prior_groups & local_groups:
        issues.append(f"候选组键重复: {sorted(prior_groups & local_groups)[:8]}")

    all_items = prior_items | local_items
    all_groups = prior_groups | local_groups
    group_candidates = {
        **(existing_group_candidates or {}),
        **{
        group.group_key: set(group.candidate_item_keys)
        for group in groups
        },
    }
    for group in groups:
        missing = set(group.candidate_item_keys) - all_items
        if missing:
            issues.append(f"候选组 {group.group_key} 引用了不存在的工作项: {sorted(missing)}")
        _validate_stage_scope(
            group.group_key,
            group.stage_order,
            None,
            milestone_weeks,
            issues,
        )

    represented_weeks: set[int] = set()
    for item in work_items:
        temporal = item.constraint_set.temporal
        if temporal.relative_day_ref and relative_day_range(temporal.relative_day_ref, date(2000, 1, 3)) is None:
            issues.append(f"工作项 {item.item_key} 的相对日期必须完整使用W1或W1-D1；星期/考试后请用明确星期与依赖，不可部分匹配: {temporal.relative_day_ref}")
        if temporal.deadline_relative_day_ref and relative_day_range(temporal.deadline_relative_day_ref, date(2000, 1, 3)) is None:
            issues.append(f"工作项 {item.item_key} 的相对截止日期不合法: {temporal.deadline_relative_day_ref}")
        _validate_stage_scope(
            item.item_key,
            item.stage_order,
            (
                (temporal.start_week, temporal.end_week)
                if temporal.start_week is not None and temporal.end_week is not None
                else None
            ),
            milestone_weeks,
            issues,
        )
        if temporal.start_week is not None and temporal.end_week is not None:
            represented_weeks.update(range(temporal.start_week, temporal.end_week + 1))
        unknown_commitments = set(item.commitment_refs) - commitment_labels
        if unknown_commitments:
            issues.append(
                f"工作项 {item.item_key} 引用了未知承诺: {sorted(unknown_commitments)}"
            )
        group_key = item.constraint_set.selection.group_key
        explicit_choice = bool(re.search(r'[二三四五六七八九十0-9]+选[一二三四五0-9]+',
                                         item.title + item.description))
        explicit_choice = explicit_choice or bool(re.search(
            r'从.{2,80}(?:中|里)(?:只)?选(?:且只选)?[一二三1-3](?:种|项|个)',
            item.title + item.description))
        text = (item.title + " " + item.description).upper()
        referenced_pools = [key for key, members in group_candidates.items() if members and
                            re.search(r"(?<![A-Z0-9._-])" + re.escape(key) + r"(?![A-Z0-9._-])", text)]
        # A task can operate an explicitly defined pool without itself being
        # an alternative in that pool. Requiring reverse membership here would
        # turn "choose a topic then learn exposure" into a fourth topic.
        # Missing/ambiguous pools and inconsistent candidate membership still
        # fail the original gates below.
        selection_is_represented = len(referenced_pools) == 1
        if (item.task_kind.upper() in {'SELECT', 'SELECTION', 'CHOOSE'} or explicit_choice) and not group_key and not selection_is_represented:
            issues.append(
                f"选择型工作项 {item.item_key} 缺少候选组；必须声明 SELECT_K 和候选集合"
            )
        declared_groups = [key for key, candidates in group_candidates.items() if item.item_key in candidates]
        if declared_groups and (len(declared_groups) != 1 or group_key != declared_groups[0]):
            issues.append(f"候选工作项 {item.item_key} 的 selection.group_key 与组成员声明不一致: "
                          f"actual={group_key!r}, declared_groups={declared_groups}")
        if group_key:
            if group_key not in all_groups:
                issues.append(f"工作项 {item.item_key} 引用了不存在的候选组 {group_key}")
            elif group_key in group_candidates and item.item_key not in group_candidates[group_key]:
                issues.append(f"工作项 {item.item_key} 不在候选组 {group_key} 的候选集合中")

    if requested_weeks:
        for key, weeks in (referenced_item_weeks or {}).items():
            if key not in all_items:
                issues.append(f"执行动作引用不存在的工作项: {key}")
            else:
                represented_weeks.update(weeks)
        missing_weeks = requested_weeks - represented_weeks
        outside_weeks = represented_weeks - requested_weeks
        if missing_weeks:
            issues.append(f"本批次没有覆盖请求周: {sorted(missing_weeks)}")
        if outside_weeks:
            issues.append(f"本批次包含请求范围外的周: {sorted(outside_weeks)}")

    edges = set(existing_edges)
    for dependency in dependencies:
        edge = (dependency.predecessor_key, dependency.successor_key)
        if dependency.predecessor_key not in all_items:
            issues.append(f"依赖前置项不存在: {dependency.predecessor_key}")
        if dependency.successor_key not in all_items:
            issues.append(f"依赖后置项不存在: {dependency.successor_key}")
        if edge in edges:
            issues.append(f"依赖边重复: {edge[0]} -> {edge[1]}")
        edges.add(edge)
    if not issues and _contains_cycle(all_items, edges):
        issues.append("工作项依赖图存在环")

    if issues:
        raise WorkItemValidationError("HYBRID_WORK_ITEM_BATCH_INVALID", tuple(issues))


def _validate_stage_scope(
    key: str,
    stage_order: int | None,
    week_range: tuple[int | None, int | None] | None,
    milestone_weeks: dict[int, tuple[int, int]],
    issues: list[str],
) -> None:
    if stage_order is None:
        return
    boundary = milestone_weeks.get(stage_order)
    if boundary is None:
        issues.append(f"{key} 引用了不存在的阶段 {stage_order}")
        return
    if week_range is None:
        return
    start, end = week_range
    if start is None or end is None:
        return
    if start < boundary[0] or end > boundary[1]:
        issues.append(
            f"{key} 的周范围 W{start}-W{end} 超出阶段 {stage_order} 的 "
            f"W{boundary[0]}-W{boundary[1]}"
        )


def _contains_cycle(nodes: set[str], edges: set[tuple[str, str]]) -> bool:
    adjacency: dict[str, set[str]] = {node: set() for node in nodes}
    indegree = {node: 0 for node in nodes}
    for predecessor, successor in edges:
        if predecessor not in nodes or successor not in nodes:
            continue
        if successor not in adjacency[predecessor]:
            adjacency[predecessor].add(successor)
            indegree[successor] += 1
    queue = [node for node, degree in indegree.items() if degree == 0]
    visited = 0
    while queue:
        node = queue.pop()
        visited += 1
        for successor in adjacency[node]:
            indegree[successor] -= 1
            if indegree[successor] == 0:
                queue.append(successor)
    return visited != len(nodes)
