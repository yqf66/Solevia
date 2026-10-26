"""Typed advisory paths and evidence identities; unknown input stays authoritative."""

from __future__ import annotations

from dataclasses import dataclass
from copy import deepcopy
from typing import Any
import re
import json

CONTRACT_VERSION = "specialist-context-v1-typed-paths"


def arithmetic_observations(context: dict) -> list[str]:
    """Small, exact arithmetic hints; never infer availability or change a fact.

    Only complete declared commitments and simple capacity segments qualify.
    Unknown/exceptional shapes stay in the original input for semantic review.
    """
    result = []
    plan = context.get("current_plan")
    if isinstance(plan, dict):
        commitments = plan.get("commitments")
        if (
            isinstance(commitments, list)
            and 0 < len(commitments) <= 16
            and all(
                isinstance(row, dict)
                and row.get("cadence") == "TOTAL"
                and type(row.get("minimum_total_minutes")) is int
                and row["minimum_total_minutes"] >= 0
                and type(row.get("start_week")) is int
                and type(row.get("end_week")) is int
                and 0 < row["start_week"] <= row["end_week"]
                for row in commitments
            )
        ):
            values = [row["minimum_total_minutes"] for row in commitments]
            result.append(
                "TOTAL commitment minutes: " + "+".join(map(str, values)) + "=" + str(sum(values))
            )
            segments = plan.get("temporal_segments")
            if isinstance(segments, list) and len(segments) <= 16:
                for segment in segments:
                    if not isinstance(segment, dict) or any(
                        segment.get(key)
                        for key in (
                            "weekly_capacity_overrides",
                            "exception_days_per_week",
                            "exception_weekdays",
                            "exception_daily_minutes",
                        )
                    ):
                        continue
                    keys = (
                        "start_week",
                        "end_week",
                        "available_days_per_week",
                        "suggested_daily_minutes",
                    )
                    if not all(type(segment.get(key)) is int and segment[key] > 0 for key in keys):
                        continue
                    start, end, days, minutes = [segment[key] for key in keys]
                    if start > end or days > 7:
                        continue
                    # Crossing-window obligations make an allocation subtotal ambiguous.
                    if any(
                        row["start_week"] <= end
                        and row["end_week"] >= start
                        and not (start <= row["start_week"] <= row["end_week"] <= end)
                        for row in commitments
                    ):
                        continue
                    capacity = (end - start + 1) * days * minutes
                    demand = sum(
                        row["minimum_total_minutes"]
                        for row in commitments
                        if start <= row["start_week"] <= row["end_week"] <= end
                    )
                    result.append(
                        f"W{start}-W{end} nominal capacity {(end - start + 1)}*{days}*{minutes}"
                        f"={capacity}; contained TOTAL demand={demand};"
                        f" arithmetic difference={capacity - demand} minutes (not verified free time)"
                    )
    # Optional hints never replace any source content when the hint list would grow.
    return result if len(json.dumps(result, ensure_ascii=False)) <= 650 else []


def derived_buffer_conflicts(context: dict) -> list[dict]:
    """Compare explicitly derived segment-summary buffers, not reserved minima.

    A reserve preference is not an equation. Only the segment's own capacity
    summary qualifies; retain it verbatim and surface the discrepancy for review.
    """
    plan = context.get("current_plan", {})
    if not isinstance(plan, dict):
        return []
    observations = arithmetic_observations(context)
    results = []
    segments = plan.get("temporal_segments")
    if not isinstance(segments, list):
        return []
    for row in segments:
        if not isinstance(row, dict):
            continue
        prefix = f"W{row.get('start_week')}-W{row.get('end_week')} nominal capacity "
        arithmetic = next((text for text in observations if text.startswith(prefix)), None)
        if arithmetic is None:
            continue
        stated = re.search(r"约\s*(\d+)\s*分钟\s*仅作缓冲", str(row.get("capacity_summary", "")))
        computed = re.search(r"arithmetic difference=(-?\d+) minutes", arithmetic)
        if stated and computed and int(stated[1]) != int(computed[1]):
            results.append(
                {
                    "window": prefix.split(" nominal")[0],
                    "stated_buffer": int(stated[1]),
                    "arithmetic_difference": int(computed[1]),
                    "authority": "READ_ONLY_SOURCE_DISCREPANCY_NOT_CHANGE_AUTHORIZATION",
                }
            )
    return results[:8]


TASK_COLLECTIONS = {
    "all_days",
    "generated_days",
    "days",
    "tasks",
    "backlog_tasks",
    "generated_task_registry",
}
WORK_COLLECTIONS = {"work_items", "standing_work_item_registry"}
PLAN_ARRAYS = {"stages", "phases", "months", "weeks", "milestones"}
TASK_REFS = {"depends_on", "dependency_refs", "prerequisite_task_ids", "depends_on_json"}
IDENTITY_KEYS = ("id", "reference", "code", "task_id", "work_item_key", "item_key")
EDGE_COLLECTIONS = {"existing_dependency_registry", "work_item_dependencies", "work_item_edges"}


@dataclass(frozen=True)
class ReadUnit:
    path: tuple
    value: Any
    kind: str


def pointer(path: tuple) -> str:
    return "/" + "/".join(str(key).replace("~", "~0").replace("/", "~1") for key in path)


def identities(value: dict) -> list[str]:
    return list(
        dict.fromkeys(
            value[key] for key in IDENTITY_KEYS if isinstance(value.get(key), str) and value[key]
        )
    )


def typed_units(skeleton: dict) -> list[ReadUnit]:
    """Extract complete rows, never slicing free text, facts or history events.

    Only declared plan arrays are traversed. Their phase metadata remains in
    every batch. Unknown lists, effective constraints, change history and graph
    edges remain GLOBAL so conflicting records cannot be read independently.
    """
    units = []

    def visit(node, path=(), inside_plan=False):
        if not isinstance(node, dict):
            return
        for key, child in list(node.items()):
            next_path = (*path, key)
            plan = inside_plan or key in {"plan", "current_plan", "plan_outline"}
            if (
                path == ("requirement_memory",)
                and key == "change_history"
                and isinstance(child, list)
                and child
            ):
                # An entire ordered ledger is atomic: old/new values and their
                # chronology are never split apart or treated as obsolete text.
                # Effective facts, current requirements and precedence stay GLOBAL.
                units.append(ReadUnit(next_path, child, "HISTORY_LEDGER"))
                node[key] = []
            elif key in TASK_COLLECTIONS | WORK_COLLECTIONS and isinstance(child, list) and child:
                kind = "WORK_ITEM" if key in WORK_COLLECTIONS else "TASK_OR_DAY"
                units.extend(
                    ReadUnit((*next_path, i), value, kind) for i, value in enumerate(child)
                )
                node[key] = []
            elif isinstance(child, dict):
                visit(child, next_path, plan)
            elif plan and key in PLAN_ARRAYS and isinstance(child, list):
                for index, row in enumerate(child):
                    visit(row, (*next_path, index), True)

    visit(skeleton)
    return units


def work_item_edges(context: dict) -> list[tuple[tuple, dict]]:
    result = []

    def visit(value, path=()):
        if isinstance(value, dict):
            for key, child in value.items():
                if key in EDGE_COLLECTIONS:
                    if not isinstance(child, list) or any(not isinstance(e, dict) for e in child):
                        raise ValueError("UNSUPPORTED_WORK_ITEM_EDGE_SHAPE")
                    for i, edge in enumerate(child):
                        if not all(
                            isinstance(edge.get(k), str) and edge[k]
                            for k in ("predecessor_key", "successor_key")
                        ):
                            raise ValueError("UNSUPPORTED_WORK_ITEM_EDGE_SHAPE")
                        result.append(((*path, key, i), edge))
                else:
                    visit(child, (*path, key))
        elif isinstance(value, list):
            for i, child in enumerate(value):
                visit(child, (*path, i))

    visit(context)
    return result


def evidence_identities(context: dict) -> set[str]:
    """Only full local objects count as evidence; an unread index entry does not."""
    result = set()

    def visit(value, path=()):
        if path:
            result.add(pointer(path))
        if isinstance(value, dict):
            result.update(identities(value))
            if isinstance(value.get("field_key"), str):
                result.add(value["field_key"])
            if isinstance(value.get("path"), str) and "value" in value:
                result.add(value["path"])
            for key, child in value.items():
                if key not in {
                    "_advisory_task_index",
                    "_advisory_read_contract",
                    "_context_manifest",
                    "_context_references",
                }:
                    visit(child, (*path, key))
        elif isinstance(value, list):
            for i, child in enumerate(value):
                visit(child, (*path, i))

        elif isinstance(value, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            result.add(value)

    visit(resolve_snapshot_references(context))
    return result


def resolve_snapshot_references(context: dict) -> dict:
    """Validate and expand exact same-request references for program checks.

    Transport may keep a small pointer, but its complete target must be inside
    this input. A hash or a reference to an earlier model call is insufficient.
    """
    result = deepcopy(context)
    metadata = result.get("_context_references")
    if metadata is None:
        return result
    if not isinstance(metadata, dict) or not isinstance(metadata.get("references"), list):
        raise ValueError("UNSUPPORTED_SNAPSHOT_REFERENCE_CONTRACT")

    def parts(path):
        if not isinstance(path, str) or not path.startswith("$."):
            raise ValueError("INVALID_SNAPSHOT_REFERENCE_PATH")
        return path[2:].split(".")

    def target(path):
        node = result
        try:
            for key in parts(path):
                node = node[key]
        except (KeyError, TypeError) as error:
            raise ValueError("MISSING_SNAPSHOT_REFERENCE_TARGET") from error
        if isinstance(node, dict) and "$context_ref" in node:
            raise ValueError("UNRESOLVED_SNAPSHOT_REFERENCE_CHAIN")
        return deepcopy(node)

    for ref in metadata["references"]:
        if not isinstance(ref, dict):
            raise ValueError("INVALID_SNAPSHOT_REFERENCE_RECORD")
        keys = parts(ref.get("path"))
        parent = result
        try:
            for key in keys[:-1]:
                parent = parent[key]
            if parent[keys[-1]] != {"$context_ref": ref.get("target")}:
                raise ValueError("SNAPSHOT_REFERENCE_STUB_MISMATCH")
            parent[keys[-1]] = target(ref.get("target"))
        except (KeyError, TypeError) as error:
            raise ValueError("MISSING_SNAPSHOT_REFERENCE_PATH") from error
    result.pop("_context_references")
    return result
