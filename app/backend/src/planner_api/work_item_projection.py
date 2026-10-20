"""Preserve semantic identity and pending obligations across plan revisions."""

from copy import deepcopy

from sqlalchemy import select

from planner_api.models import (
    PlanMilestone, PlanTask, PlanWorkItem, PlanWorkItemGroup,
    WorkItemDependency, WorkItemWindow,
)


def _values(row, excluded):
    return {column.name: deepcopy(getattr(row, column.name))
            for column in row.__table__.columns
            if column.name not in {'id', 'created_at', 'updated_at', *excluded}}


def clone_work_item_projection(session, source, target, source_activation, target_activation):
    """Clone, never move, the source graph and windows; remap concrete actions."""
    stages = {row.stage_order: row.id for row in session.scalars(select(PlanMilestone).where(
        PlanMilestone.plan_version_id == target.id))}
    groups = {row.group_key: row for row in session.scalars(select(PlanWorkItemGroup).where(
        PlanWorkItemGroup.plan_version_id == target.id))}
    group_map = {}
    for old in session.scalars(select(PlanWorkItemGroup).where(PlanWorkItemGroup.plan_version_id == source.id)):
        new = groups.get(old.group_key)
        if new is None:
            new = PlanWorkItemGroup(**_values(old, {'plan_version_id', 'milestone_id'}),
                                    plan_version_id=target.id, milestone_id=stages.get(old.stage_order))
            session.add(new)
            session.flush()
        group_map[old.id] = new.id
    current = {row.item_key: row for row in session.scalars(select(PlanWorkItem).where(
        PlanWorkItem.plan_version_id == target.id))}
    item_map = {}
    for old in session.scalars(select(PlanWorkItem).where(PlanWorkItem.plan_version_id == source.id)):
        new = current.get(old.item_key)
        if new is None:
            new = PlanWorkItem(**_values(old, {'plan_version_id', 'milestone_id', 'group_id',
                                              'source_work_item_id', 'lineage_work_item_id'}),
                               plan_version_id=target.id, milestone_id=stages.get(old.stage_order),
                               group_id=group_map.get(old.group_id), source_work_item_id=old.id,
                               lineage_work_item_id=old.lineage_work_item_id or old.id)
            session.add(new)
            session.flush()
        item_map[old.id] = new.id
    existing = {(row.predecessor_work_item_id, row.successor_work_item_id, row.dependency_type)
                for row in session.scalars(select(WorkItemDependency).where(
                    WorkItemDependency.plan_version_id == target.id))}
    for edge in session.scalars(select(WorkItemDependency).where(WorkItemDependency.plan_version_id == source.id)):
        key = (item_map[edge.predecessor_work_item_id], item_map[edge.successor_work_item_id], edge.dependency_type)
        if key not in existing:
            session.add(WorkItemDependency(**_values(edge, {'plan_version_id', 'predecessor_work_item_id',
                                                           'successor_work_item_id'}),
                                           plan_version_id=target.id, predecessor_work_item_id=key[0],
                                           successor_work_item_id=key[1]))
    windows = {}
    for old in session.scalars(select(WorkItemWindow).where(
        WorkItemWindow.activation_id == source_activation.id)):
        new = WorkItemWindow(**_values(old, {'activation_id', 'work_item_id'}),
                             activation_id=target_activation.id, work_item_id=item_map[old.work_item_id])
        session.add(new)
        session.flush()
        windows[old.id] = new
    for window in windows.values():
        metadata = dict(window.resolution_json or {})
        previous = windows.get(metadata.get('previous_window_id'))
        if previous:
            window.resolution_json = {**metadata, 'previous_window_id': previous.id}
    for task in session.scalars(select(PlanTask).where(PlanTask.plan_version_id == target.id)):
        old = session.get(PlanTask, task.source_task_id) if task.source_task_id else None
        old_item_id = task.source_work_item_id or (old.source_work_item_id if old else None)
        if old_item_id in item_map:
            task.source_work_item_id = item_map[old_item_id]
        old_window_id = task.work_item_window_id or (old.work_item_window_id if old else None)
        if old_window_id in windows:
            task.work_item_window_id = windows[old_window_id].id
        if old and not (task.scheduling_policy_json or {}).get('occurrence_key'):
            identity = (old.scheduling_policy_json or {}).get('occurrence_key') or old.plan_day_id
            task.scheduling_policy_json = {**(task.scheduling_policy_json or {}), 'occurrence_key': identity}
    session.flush()
