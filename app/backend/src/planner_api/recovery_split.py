"""Explicit backlog splitting; dates and obligations remain pending until recovery."""

import hashlib
import json
from datetime import date
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import select

from planner_api.dependency_resolver import PlanDependencyResolver
from planner_api.models import (
    PlanDay, PlanTask, PlanWorkItem, ProgressEvent, RecoveryOperation, utc_now,
)
from planner_api.recovery_time_preview import preview_fingerprint, preview_time
from planner_api.schedule_reconciliation import mark_plan_schedule_review


def validated_preview(service, goal_id, preview_id):
    operation = service.session.get(RecoveryOperation, preview_id)
    goal, plan = service._goal_and_plan(goal_id)
    if operation is None or operation.goal_id != goal_id or operation.mode != "KEEP_DEADLINE":
        raise HTTPException(404, "恢复预览不存在")
    if operation.status != "PREVIEW" or operation.source_plan_version_id != plan.id:
        raise HTTPException(409, "恢复范围已变化，请重新预览")
    if goal.status not in {"ACTIVE", "RECOVERY_REQUIRED"}:
        raise HTTPException(409, "当前规划不能进行恢复")
    value = operation.preview_json
    if not value.get("recovery_scope"):
        raise HTTPException(409, "请重新生成带任务范围的恢复预览")
    try:
        current = service._build_recovery_catchup_preview(
            plan, service._activation(plan.id), date.fromisoformat(value["anchor_date"]),
            value["include_non_execution_days"], value["recovery_scope"]["selected_task_ids"],
        )
        current["time_preview"] = preview_time(service, goal, plan, current, catchup=True)
    except HTTPException as error:
        raise HTTPException(409, "恢复任务状态已变化，请重新预览") from error
    if preview_fingerprint(current) != preview_fingerprint(value):
        raise HTTPException(409, "恢复任务、进度或时间设置已变化，请重新预览")
    return operation, goal, plan


def split_policy(service, plan, task):
    allowed, reason = service._task_split_policy(plan, task)
    if not allowed:
        return allowed, reason
    if task.status != "NOT_STARTED":
        return False, "已有执行状态，请先核对剩余工作，不能直接拆分"
    if task.estimate_minutes < 10:
        return False, "任务太短，无需拆分"
    if task.satisfies_commitments_json:
        return False, "该任务兑现次数或完整产出承诺，不能把分片计为多次完成"
    work_item = service.session.get(PlanWorkItem, task.source_work_item_id) if task.source_work_item_id else None
    if work_item and (work_item.fulfillment_mode != "DURATION" or work_item.target_unit != "MINUTES"):
        return False, "次数、候选或完整产出任务必须作为一项完成"
    if task.scheduling_policy == "QUOTA" and (task.scheduling_policy_json or {}).get("unit", "COUNT") != "MINUTES":
        return False, "次数型训练不能拆成多次完成"
    if service.session.scalar(select(ProgressEvent.id).where(ProgressEvent.task_id == task.id)):
        return False, "已有执行记录，请先核对剩余工作，不能直接拆分"
    return True, None


def preview_split(service, goal_id, payload):
    source_preview, goal, plan = validated_preview(service, goal_id, payload.preview_id)
    if payload.source_task_id not in source_preview.preview_json["recovery_scope"]["selected_task_ids"]:
        raise HTTPException(422, "只能拆分本次已选的积压任务")
    task = service.session.get(PlanTask, payload.source_task_id)
    allowed, reason = split_policy(service, plan, task)
    if not allowed:
        raise HTTPException(422, reason)
    if sum(payload.part_minutes) != task.estimate_minutes:
        raise HTTPException(422, "分片总时长必须等于原任务时长")
    value = {
        "source_preview_id": source_preview.id, "source_task_id": task.id,
        "title": task.title, "original_minutes": task.estimate_minutes,
        "part_minutes": payload.part_minutes,
        "source_fingerprint": preview_fingerprint(source_preview.preview_json),
    }
    key = "split:" + hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()
    operation = service.session.scalar(select(RecoveryOperation).where(
        RecoveryOperation.goal_id == goal_id, RecoveryOperation.mode == "SPLIT_BACKLOG",
        RecoveryOperation.status == "PREVIEW", RecoveryOperation.idempotency_key.like(key + "%"),
    ))
    for previous in service.session.scalars(select(RecoveryOperation).where(
        RecoveryOperation.goal_id == goal_id, RecoveryOperation.mode == "SPLIT_BACKLOG",
        RecoveryOperation.status == "PREVIEW",
    )):
        if operation is None or previous.id != operation.id:
            previous.status = "SUPERSEDED"
    if operation is None:
        operation = RecoveryOperation(goal_id=goal.id, source_plan_version_id=plan.id,
                                      mode="SPLIT_BACKLOG", status="PREVIEW",
                                      idempotency_key=key + ":" + str(uuid4()), preview_json=value)
        service.session.add(operation)
    service.session.commit()
    return read_split(operation)


def read_split(operation):
    return {"id": operation.id, "status": operation.status,
            "source_plan_version_id": operation.source_plan_version_id,
            "result_plan_version_id": operation.result_plan_version_id,
            **{key: operation.preview_json[key] for key in
               ("source_task_id", "title", "original_minutes", "part_minutes")},
            "result_selected_task_ids": operation.preview_json.get("result_selected_task_ids", [])}


def apply_split(service, goal_id, preview_id):
    session = service.session
    operation = session.get(RecoveryOperation, preview_id)
    if operation is None or operation.goal_id != goal_id or operation.mode != "SPLIT_BACKLOG":
        raise HTTPException(404, "拆分预览不存在")
    if operation.status == "APPLIED":
        return read_split(operation)
    if operation.status != "PREVIEW":
        raise HTTPException(409, "拆分选择已变化，请重新预览")
    value = operation.preview_json
    source_preview, goal, current = validated_preview(service, goal_id, value["source_preview_id"])
    source = session.get(PlanTask, value["source_task_id"])
    allowed, reason = split_policy(service, current, source)
    if not allowed or source.estimate_minutes != sum(value["part_minutes"]):
        raise HTTPException(409, reason or "原任务时长已变化")
    activation = service._activation(current.id)
    revised = service._clone_plan_for_recovery(goal, current, {
        "mode": "SPLIT_BACKLOG", "projected_end_date": str(activation.projected_end_date),
        "changes": [], "carryover_task_ids": [], "carryovers": [], "redistributions": [],
    })
    old_days = {day.id: day for day in session.scalars(select(PlanDay).where(PlanDay.plan_version_id == current.id))}
    new_days = {(day.week_index, day.day_index): day for day in session.scalars(select(PlanDay).where(PlanDay.plan_version_id == revised.id))}
    copies = {}
    old_tasks = list(session.scalars(select(PlanTask).where(PlanTask.plan_version_id == current.id)))
    for task in old_tasks:
        day = old_days[task.plan_day_id]
        copied = session.scalar(select(PlanTask).where(
            PlanTask.plan_day_id == new_days[(day.week_index, day.day_index)].id,
            PlanTask.order_in_day == task.order_in_day,
        ))
        copies[task.id] = copied
    archived = copies[source.id]
    archived.status = "CARRIED_OVER"
    parts = []
    for index, minutes in enumerate(value["part_minutes"]):
        part = PlanTask(
            plan_version_id=revised.id, plan_day_id=archived.plan_day_id,
            source_task_id=archived.id, lineage_task_id=archived.lineage_task_id,
            source_work_item_id=archived.source_work_item_id, work_item_window_id=archived.work_item_window_id,
            origin_stage_order=archived.origin_stage_order, occurrence_index=source.occurrence_index,
            order_in_day=service._next_task_order(archived.plan_day_id),
            title=f"{source.title}（第 {index + 1}/{len(value['part_minutes'])} 部分）",
            description=f"{source.description}\n这是同一任务的分片，按顺序完成全部分片。",
            estimate_minutes=minutes, priority=source.priority, task_kind=source.task_kind,
            is_required=source.is_required, evidence_json=source.evidence_json,
            is_splittable=False, scheduling_policy=source.scheduling_policy,
            scheduling_policy_json=source.scheduling_policy_json,
        )
        session.add(part)
        session.flush()
        parts.append(part)
    resolver = PlanDependencyResolver(session)
    indexes = resolver.indexes(current.id)
    def remap_dependencies(task):
        dependencies = []
        for reference in task.depends_on_json or []:
            parent = resolver.resolve(reference, task, indexes)
            dependencies.append(parts[-1].id if parent and parent.id == source.id
                                else copies[parent.id].id if parent and parent.id in copies
                                else reference)
        return dependencies
    for task in old_tasks:
        copies[task.id].depends_on_json = remap_dependencies(task)
    parts[0].depends_on_json = remap_dependencies(source)
    for index, part in enumerate(parts[1:], start=1):
        part.depends_on_json = [parts[index - 1].id]
    selected = []
    for task_id in source_preview.preview_json["recovery_scope"]["selected_task_ids"]:
        selected.extend([part.id for part in parts] if task_id == source.id else [copies[task_id].id])
    revised.total_estimated_minutes = sum(
        task.estimate_minutes for task in session.scalars(
            select(PlanTask).where(PlanTask.plan_version_id == revised.id)
        ) if task.status not in {"CARRIED_OVER", "CANCELLED"}
    )
    mark_plan_schedule_review(session, goal, current.id, revised.id, "已确认拆分积压，后续日期仍待恢复安排。")
    current.status = "SUPERSEDED"
    goal.active_plan_version_id = revised.id
    goal.status = "ACTIVE"
    goal.last_active_at = utc_now()
    session.add(ProgressEvent(goal_id=goal.id, plan_version_id=revised.id,
                              plan_day_id=archived.plan_day_id, task_id=archived.id,
                              event_type="RECOVERY_SPLIT", metadata_json={
                                  "source_task_id": source.id, "part_task_ids": [part.id for part in parts],
                                  "part_minutes": value["part_minutes"], "preview_id": operation.id,
                              }))
    operation.status = "APPLIED"
    operation.applied_at = utc_now()
    operation.result_plan_version_id = revised.id
    operation.preview_json = {**value, "result_selected_task_ids": selected}
    source_preview.status = "SUPERSEDED"
    session.commit()
    return read_split(operation)
