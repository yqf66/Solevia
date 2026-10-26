"""Lossless, bounded *advisory* reads. Never replaces the main planning contract."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from planner_api.context_compiler import deduplicate_context_snapshots
from planner_api.specialist_context import (
    CONTRACT_VERSION,
    TASK_REFS,
    identities,
    typed_units,
    work_item_edges,
    resolve_snapshot_references,
)

VERSION = "advisory-batches-v3-typed-graph-cost"
COLLECTIONS = {
    "all_days",
    "generated_days",
    "days",
    "tasks",
    "backlog_tasks",
    "work_items",
    "generated_task_registry",
}
DEPENDENCIES = {"depends_on", "dependency_refs", "prerequisite_task_ids"}


def encoded(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def fingerprint(value: Any) -> str:
    return hashlib.sha256(encoded(value).encode("utf-8")).hexdigest()


def pointer(path: tuple) -> str:
    return "/" + "/".join(str(key).replace("~", "~0").replace("/", "~1") for key in path)


@dataclass(frozen=True)
class AdvisoryReads:
    inputs: tuple[dict, ...]
    coverage: dict


def build_advisory_reads(
    context: dict,
    *,
    max_chars: int,
    max_batches: int = 4,
    max_input_multiplier: float | None = None,
) -> AdvisoryReads:
    """Split only known structured collections; repeat all other fields verbatim.

    A coverage certificate proves input transport, not semantic understanding.
    If global constraints, dependency closure, or an atomic row cannot fit, no
    partial plan is returned: the caller must keep the complete original input.
    """
    base = {
        "version": VERSION,
        "typed_contract": CONTRACT_VERSION,
        "source_hash": fingerprint(context),
        "source_chars": len(encoded(context)),
        "semantic_completeness": False,
        "max_chars": max_chars,
        "max_batches": max_batches,
    }

    def unsplit(reason: str, **details) -> AdvisoryReads:
        return AdvisoryReads(
            (),
            {
                **base,
                "status": "UNSPLIT",
                "reason": reason,
                "input_coverage_complete": False,
                "diagnostics": details,
            },
        )

    if any(key.startswith("_advisory_") for key in context):
        return unsplit("RESERVED_CONTRACT_KEY_COLLISION")
    try:
        context = resolve_snapshot_references(context)
    except ValueError as error:
        return unsplit(str(error))
    single = deduplicate_context_snapshots(context)
    if len(encoded(single)) <= max_chars:
        return AdvisoryReads(
            (single,),
            {
                **base,
                "status": "SINGLE",
                "input_coverage_complete": True,
                "batch_count": 1,
                "batch_chars": [len(encoded(single))],
                "batch_hashes": [fingerprint(single)],
                "input_multiplier": len(encoded(single)) / max(1, base["source_chars"]),
            },
        )
    skeleton = deepcopy(context)
    units = typed_units(skeleton)
    rows = [(unit.path, unit.value) for unit in units]
    global_chars = len(encoded(skeleton))
    if not rows:
        return unsplit(
            "NO_STRUCTURED_COLLECTION",
            global_chars=global_chars,
            global_over_budget=global_chars > max_chars,
            structured_unit_count=0,
        )
    objects: dict[tuple, dict] = {}
    aliases: dict[str, list[tuple]] = {}
    index = []

    def scan(value: Any, path: tuple):
        if isinstance(value, dict):
            ids = identities(value)
            if ids:
                objects[path] = value
                for alias in ids:
                    aliases.setdefault(alias, []).append(path)
                index.append(
                    {
                        "path": pointer(path),
                        "aliases": ids,
                        "hash": fingerprint(value),
                        "dependencies": {k: value[k] for k in TASK_REFS if k in value},
                    }
                )
            for key, child in value.items():
                scan(child, (*path, key))
        elif isinstance(value, list):
            for i, child in enumerate(value):
                scan(child, (*path, i))

    scan(context, ())
    try:
        edges = work_item_edges(context)
        for _path, edge in edges:
            if any(
                len(aliases.get(edge[key], [])) != 1 for key in ("predecessor_key", "successor_key")
            ):
                return unsplit(
                    "UNRESOLVED_OR_AMBIGUOUS_WORK_ITEM_EDGE",
                    edge_path=pointer(_path),
                    global_chars=global_chars,
                )
    except ValueError as error:
        return unsplit(str(error), global_chars=global_chars)
    # Only detached units need an identity index. Global objects are already
    # present in full; indexing every historical fact just repeats their IDs.
    index = [
        entry
        for entry in index
        if any(
            entry["path"] == pointer(path) or entry["path"].startswith(pointer(path) + "/")
            for path, _ in rows
        )
    ]
    # The external coverage manifest retains raw hashes. The model only needs
    # paths/aliases to identify unread units; repeating each SHA and field
    # name in every batch adds no readable evidence.
    index = [[entry["path"], entry["aliases"]] for entry in index]
    skeleton["_advisory_task_index"] = index
    global_index_chars = len(encoded(skeleton))
    unit_ids = [pointer(path) for path, _ in rows]
    row_manifest = [{"path": pointer(path), "hash": fingerprint(row)} for path, row in rows]

    def make_input(selected: list[tuple[tuple, Any]]) -> dict:
        result = deepcopy(skeleton)
        dependency_objects: dict[tuple, dict] = {}
        resolved_paths: set[tuple] = set()
        selected_paths = [path for path, _value in selected]

        def include(path):
            if path in resolved_paths:
                return
            resolved_paths.add(path)
            # Primary rows already include nested objects verbatim; globals
            # also remain present in full. Only detached missing units need a
            # separate dependency body. Still traverse every dependency.
            detached = any(path[: len(unit_path)] == unit_path for unit_path, _ in rows)
            present = any(path[: len(unit_path)] == unit_path for unit_path in selected_paths)
            if detached and not present:
                dependency_objects[path] = objects[path]
            close(objects[path])

        def close(value: Any):
            if isinstance(value, dict):
                for key in TASK_REFS:
                    if key not in value:
                        continue
                    refs = value[key]
                    if not isinstance(refs, list) or any(not isinstance(ref, str) for ref in refs):
                        raise ValueError("UNSUPPORTED_DEPENDENCY_SHAPE")
                    for ref in refs:
                        matches = aliases.get(ref, [])
                        if len(matches) != 1:
                            raise ValueError("UNRESOLVED_OR_AMBIGUOUS_DEPENDENCY")
                        path = matches[0]
                        include(path)
                own_keys = set(identities(value))
                for _edge_path, edge in edges:
                    if own_keys & {edge["predecessor_key"], edge["successor_key"]}:
                        for endpoint in ("predecessor_key", "successor_key"):
                            path = aliases[edge[endpoint]][0]
                            include(path)
                for child in value.values():
                    close(child)
            elif isinstance(value, list):
                for child in value:
                    close(child)

        for path, value in selected:
            parent = result
            if next(unit.kind for unit in units if unit.path == path) == "HISTORY_LEDGER":
                for key in path[:-1]:
                    parent = parent[key]
                parent[path[-1]] = deepcopy(value)
                close(value)
                continue
            for key in path[:-2]:
                parent = parent[key]
            parent[path[-2]].append(deepcopy(value))
            close(value)
        result["_advisory_dependency_context"] = [
            {"path": pointer(path), "value": deepcopy(value)}
            for path, value in dependency_objects.items()
        ]
        result["_advisory_read_contract"] = {
            "version": VERSION,
            "source_hash": base["source_hash"],
            "primary_units": [pointer(path) for path, _ in selected],
            "total_units": len(rows),
            "global_constraints_repeated": True,
            "authority": "READ_ONLY_PARTIAL_INPUT_MAIN_AGENT_MUST_CHECK_GLOBAL_RELATIONS",
            "typed_contract": CONTRACT_VERSION,
            "identity_index_layout": ["original_path", "aliases"],
            "graph_edges_repeated": True,
            "history_policy": "effective-constraints-global-ordered-change-ledger-atomic",
            "dependency_units": [pointer(path) for path in dependency_objects],
        }
        return deduplicate_context_snapshots(result)

    packed: list[list[tuple[tuple, Any]]] = []
    current: list[tuple[tuple, Any]] = []

    def dimensions(row, value):
        return {
            "unit_path": pointer(row[0]),
            "unit_hash": fingerprint(row[1]),
            "atomic_unit_chars": len(encoded(row[1])),
            "global_chars": global_chars,
            "global_with_index_chars": global_index_chars,
            "identity_index_chars": len(encoded(index)),
            "dependency_context_chars": len(encoded(value["_advisory_dependency_context"])),
            "candidate_chars": len(encoded(value)),
            "structured_unit_count": len(rows),
            "global_over_budget": global_index_chars > max_chars,
            "completed_batches": len(packed),
        }

    # Connected work-item edges are an atomic reading group. Greedy one-row
    # packing otherwise copies the whole graph into several small batches.
    groups = {path: {path} for path, _ in rows}

    def unit_for(path):
        return next(
            (unit_path for unit_path, _ in rows if path[: len(unit_path)] == unit_path), None
        )

    for _edge_path, edge in edges:
        left, right = [unit_for(aliases[edge[k]][0]) for k in ("predecessor_key", "successor_key")]
        if left is not None and right is not None:
            combined = groups[left] | groups[right]
            for path in combined:
                groups[path] = combined
    seen = set()
    bundles = []
    for path, _ in rows:
        if path in seen:
            continue
        bundle = [row for row in rows if row[0] in groups[path]]
        seen.update(groups[path])
        bundles.append(bundle)
    if edges:
        # Read the atomic graph group first, instead of packing cheap unrelated
        # rows into its only available room and multiplying global rereads.
        bundles.sort(key=lambda group: -len(encoded([value for _path, value in group])))
    try:
        for bundle in bundles:
            row = bundle[0]
            if edges:
                # Best-fit atomic groups into existing reads. A closed earlier
                # read may still have room for later independent task rows.
                fits = []
                for i, group in enumerate(packed):
                    candidate_group = [*group, *bundle]
                    size = len(encoded(make_input(candidate_group)))
                    if size <= max_chars:
                        fits.append((max_chars - size, i, candidate_group))
                if fits:
                    _slack, index_, candidate_group = min(fits)
                    packed[index_] = candidate_group
                    continue
                single_input = make_input(bundle)
                if len(encoded(single_input)) > max_chars:
                    return unsplit(
                        "GLOBAL_CONTRACT_OR_DEPENDENCY_CLOSURE_EXCEEDS_BUDGET",
                        **dimensions(row, single_input),
                    )
                if len(packed) >= max_batches:
                    return unsplit("BATCH_LIMIT_EXCEEDED", **dimensions(row, single_input))
                packed.append(bundle)
                continue
            candidate = [*current, *bundle]
            candidate_input = make_input(candidate)
            if len(encoded(candidate_input)) <= max_chars:
                current = candidate
                continue
            if not current:
                return unsplit(
                    "GLOBAL_CONTRACT_OR_ATOMIC_ROW_EXCEEDS_BUDGET",
                    **dimensions(row, candidate_input),
                )
            packed.append(current)
            current = bundle
            single_input = make_input(current)
            if len(encoded(single_input)) > max_chars:
                return unsplit(
                    "GLOBAL_CONTRACT_OR_DEPENDENCY_CLOSURE_EXCEEDS_BUDGET",
                    **dimensions(row, single_input),
                )
            if len(packed) >= max_batches:
                return unsplit("BATCH_LIMIT_EXCEEDED", **dimensions(row, single_input))
        if current:
            packed.append(current)
        inputs = tuple(make_input(group) for group in packed)
    except ValueError as error:
        return unsplit(
            str(error),
            global_chars=global_chars,
            global_with_index_chars=global_index_chars,
            structured_unit_count=len(rows),
            completed_batches=len(packed),
        )
    actual_ids = [
        item for batch in inputs for item in batch["_advisory_read_contract"]["primary_units"]
    ]
    if len(actual_ids) != len(unit_ids) or set(actual_ids) != set(unit_ids):
        raise ValueError("ADVISORY_INPUT_COVERAGE_MISMATCH")
    total_chars = sum(len(encoded(batch)) for batch in inputs)
    multiplier = total_chars / max(1, base["source_chars"])
    if max_input_multiplier is not None and multiplier > max_input_multiplier:
        return unsplit(
            "BATCH_COST_LIMIT_EXCEEDED",
            global_chars=global_chars,
            projected_batches=len(inputs),
            projected_input_chars=total_chars,
            input_multiplier=multiplier,
            maximum_input_multiplier=max_input_multiplier,
            main_input_preserved=True,
        )
    return AdvisoryReads(
        inputs,
        {
            **base,
            "status": "BATCHED",
            "batch_count": len(inputs),
            "input_coverage_complete": True,
            "units": row_manifest,
            "unit_kinds": {pointer(unit.path): unit.kind for unit in units},
            "global_chars": global_chars,
            "global_with_index_chars": global_index_chars,
            "total_read_chars": total_chars,
            "input_multiplier": multiplier,
            "batch_hashes": [fingerprint(batch) for batch in inputs],
            "batch_chars": [len(encoded(batch)) for batch in inputs],
        },
    )
