"""Read-only, deterministic facts. These never replace semantic planning."""
from __future__ import annotations

import hashlib
import json
from collections import Counter
from typing import Any


def structural_facts(context: dict[str, Any]) -> dict[str, Any]:
    """Inventory the complete input without sampling its middle entries.

    Counts are structural observations, not completion or feasibility claims.
    The original context remains the authority for values and dependencies.
    """
    counts: Counter[str] = Counter()
    sections: dict[str, dict[str, Any]] = {}

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            counts["objects"] += 1
            for key, child in value.items():
                if isinstance(child, list) and key in {
                    "tasks", "work_items", "days", "generated_days", "execution_days",
                    "effective_facts", "fixed_occupancy_rules", "fixed_occupancy_overrides",
                    "date_exceptions", "dependencies", "source_refs",
                }:
                    counts[key] += len(child)
                visit(child)
        elif isinstance(value, list):
            counts["collections"] += 1
            for child in value:
                visit(child)
        else:
            counts["values"] += 1

    for key, value in context.items():
        if key.startswith("_"):
            continue
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
        sections[key] = {
            "path": f"$.{key}",
            "sha256": hashlib.sha256(encoded.encode()).hexdigest(),
            "chars": len(encoded),
            "item_count": len(value) if isinstance(value, list) else None,
        }
        visit(value)
    return {
        "version": "structural-facts-v1",
        "authority": "READ_ONLY_STRUCTURAL_OBSERVATIONS",
        "semantic_inference": False,
        "complete_input_scanned": True,
        "counts": dict(counts),
        "sections": sections,
        "note": "数量按输入出现次数统计，不等于独立义务或完成次数；主模型仍须读取原始合同。",
    }


def closed_calendar_facts(context: dict[str, Any], role: str) -> bool:
    """Only skip an optional report for a closed, strictly numeric summary.

    Unknown fields, free text, dates, constraints, tasks and dependencies all
    require the normal semantic role. Even a valid summary is not evidence
    that the plan fits: the main planner and local business guards still run.
    """
    if role != "CALENDAR_SPECIALIST" or not context:
        return False
    if set(context) - {"calendar_summary", "schedule_context"}:
        return False
    for section, value in context.items():
        if not isinstance(value, dict) or not value:
            return False
        allowed = (
            {"execution_day_count", "task_count", "policy_counts"}
            if section == "calendar_summary"
            else {"weekday", "minutes", "available_minutes"}
        )
        if set(value) - allowed:
            return False
        for key, number in value.items():
            if key == "policy_counts":
                if not isinstance(number, dict) or set(number) - {
                    "EXACT_DAY", "DATE_WINDOW", "CADENCE", "FLOATING", "CHECKPOINT",
                    "TRIGGER", "CANDIDATE",
                }:
                    return False
                values = number.values()
            else:
                values = [number]
            if any(type(item) is not int or item < 0 for item in values):
                return False
            if key == "weekday" and number > 6:
                return False
    return True
