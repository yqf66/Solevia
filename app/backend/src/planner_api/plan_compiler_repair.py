"""Proof-bound bookkeeping patches. No semantic or capacity repair authority."""
from __future__ import annotations

import hashlib
import json
from collections import Counter
from copy import deepcopy
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from planner_api.schemas import HybridPlanDetailResult

POLICY = "compiler-field-patch-v1"


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":")).encode()).hexdigest()


class CompilerFieldPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    task_reference: str
    field: Literal["work_item_keys", "workload_minutes", "satisfies_commitments"]
    old_value_hash: str = Field(min_length=64, max_length=64)
    value: Any
    proof: Literal["UNIQUE_REGISTERED_IDENTITY", "UNIQUE_CONFIRMED_WORKLOAD_OWNER", "EXPLICIT_MINUTE_OWNERSHIP"]


class CompilerPatchEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")
    policy: Literal["compiler-field-patch-v1"] = POLICY
    source_hash: str
    author_hash: str
    contract_hash: str
    patches: list[CompilerFieldPatch] = Field(default_factory=list, max_length=3024)


def task_reference(day, task) -> str:
    return f"M{day.stage_order}-W{day.week_index}-D{day.day_index}-T{task.order}"


def _unique(value, options):
    # Only exact spelling normalization; never substring/title similarity.
    if value in options:
        return value
    matches = {option for option in options if option.strip().casefold() == str(value).strip().casefold()}
    return next(iter(matches)) if len(matches) == 1 else None


def _registry(result, outline):
    rows = list(outline.get("standing_work_item_registry") or [])
    rows += [item.model_dump(mode="json") for item in result.work_items]
    counts = Counter(row["item_key"] for row in rows)
    return {row["item_key"]: row for row in rows if counts[row["item_key"]] == 1}


def propose_bookkeeping_patches(result: HybridPlanDetailResult, outline: dict) -> CompilerPatchEnvelope:
    """Derive every permitted value from the same frozen result and registry."""
    registry = _registry(result, outline)
    commitments = outline.get("commitments") or []
    label_counts = Counter(p["label"] for p in commitments)
    workload = {p["label"]: p["workload_key"] for p in commitments
                if p.get("workload_key") and label_counts[p["label"]] == 1}
    references = Counter(task_reference(day, task) for day in result.days for task in day.tasks)
    patches = []
    for day in result.days:
        for task in day.tasks:
            reference = task_reference(day, task)
            if references[reference] != 1:
                continue

            def add(field, before, after, proof, reference=reference):
                if before != after:
                    patches.append(CompilerFieldPatch(task_reference=reference, field=field,
                        old_value_hash=digest(before), value=after, proof=proof))

            keys = [_unique(key, registry) for key in task.work_item_keys]
            if not keys or None in keys or len(keys) != len(set(keys)):
                continue
            add("work_item_keys", task.work_item_keys, keys, "UNIQUE_REGISTERED_IDENTITY")
            owned_labels = {label for key in keys for label in registry[key].get("commitment_refs", [])}
            owned_workloads = {workload[label] for label in owned_labels if label in workload}
            allocation = task.scheduling_policy_data.get("workload_minutes")
            if (type(allocation) is int and 0 < allocation <= task.estimate_minutes
                    and len(owned_workloads) == 1):
                numeric_allocation = {next(iter(owned_workloads)): allocation}
            elif (isinstance(allocation, dict) and allocation
                    and all(type(minutes) is int and minutes > 0 for minutes in allocation.values())
                    and sum(allocation.values()) <= task.estimate_minutes):
                numeric_allocation = allocation
            else:
                continue
            corrected = {}
            for key, minutes in numeric_allocation.items():
                if key in owned_workloads:
                    target = key
                else:
                    candidates = set()
                    # A label is an explicit alias only inside this action's
                    # registered ownership, never across unrelated tasks.
                    label = _unique(key, owned_labels)
                    if label in workload:
                        candidates.add(workload[label])
                    item_key = _unique(key, keys)
                    if item_key:
                        candidates.update(workload[label] for label in registry[item_key].get("commitment_refs", [])
                                          if label in workload)
                    target = next(iter(candidates)) if len(candidates) == 1 else None
                if target is None or target in corrected:
                    # Never merge two amounts into one identity or silently
                    # drop a collision. Ambiguity requires design review.
                    corrected = {}
                    break
                corrected[target] = minutes
            if not corrected:
                continue
            add("workload_minutes", allocation, corrected, "UNIQUE_CONFIRMED_WORKLOAD_OWNER")
            if set(task.satisfies_commitments) - set(label_counts):
                continue
            labels = [*task.satisfies_commitments,
                      *[label for label in workload if label in owned_labels
                        and workload[label] in corrected and label not in task.satisfies_commitments]]
            if len(labels) <= 12:
                add("satisfies_commitments", task.satisfies_commitments, labels, "EXPLICIT_MINUTE_OWNERSHIP")
    return CompilerPatchEnvelope(source_hash=digest(result.model_dump(mode="json")),
        author_hash=digest(result.source_document), contract_hash=digest(outline), patches=patches)


def _frozen(result):
    value = result.model_dump(mode="json")
    for day in value["days"]:
        for task in day["tasks"]:
            for field in ("work_item_keys", "satisfies_commitments"):
                task.pop(field, None)
            task["scheduling_policy_data"].pop("workload_minutes", None)
    return digest(value)


def apply_bookkeeping_patches(result: HybridPlanDetailResult, outline: dict,
                              envelope: CompilerPatchEnvelope) -> HybridPlanDetailResult:
    """Reject stale/tampered patches, then validate the whole schema atomically.

    This is NOT business approval: callers must run the complete graph, dates,
    workload, stage, capacity and locked-history validation before persistence.
    """
    expected = propose_bookkeeping_patches(result, outline)
    if envelope != expected:
        raise ValueError("COMPILER_PATCH_CONTRACT_MISMATCH")
    draft = result.model_copy(deep=True)
    tasks = {task_reference(day, task): task for day in draft.days for task in day.tasks}
    for patch in envelope.patches:
        task = tasks[patch.task_reference]
        old = (task.scheduling_policy_data.get("workload_minutes") if patch.field == "workload_minutes"
               else getattr(task, patch.field))
        if digest(old) != patch.old_value_hash:
            raise ValueError("COMPILER_PATCH_OLD_VALUE_MISMATCH")
        if patch.field == "workload_minutes":
            task.scheduling_policy_data[patch.field] = deepcopy(patch.value)
        else:
            setattr(task, patch.field, deepcopy(patch.value))
    draft = HybridPlanDetailResult.model_validate(draft.model_dump(mode="json"))
    if _frozen(draft) != _frozen(result):
        raise ValueError("COMPILER_PATCH_FROZEN_CONTENT_CHANGED")
    if envelope.patches:
        draft.compiler_report = {**draft.compiler_report, "bookkeeping_repair": {
            "policy": POLICY, "status": "PATCHED_REQUIRES_FULL_BUSINESS_VALIDATION",
            "source_hash": envelope.source_hash, "author_hash": envelope.author_hash,
            "contract_hash": envelope.contract_hash, "frozen_hash": _frozen(result),
            "patches": [patch.model_dump(mode="json") for patch in envelope.patches],
            "model_calls": 0,
        }}
    return draft


def repair_bookkeeping(result: HybridPlanDetailResult, outline: dict, *, enabled: bool = True) -> HybridPlanDetailResult:
    if not enabled:
        return result
    envelope = propose_bookkeeping_patches(result, outline)
    return apply_bookkeeping_patches(result, outline, envelope) if envelope.patches else result
