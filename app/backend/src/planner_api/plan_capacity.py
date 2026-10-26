"""Capacity supply is independent of task demand and compatibility day cards."""
import re
from planner_api.plan_calendar import plan_week_index


def capacity_assessment(plan, source=None) -> dict:
    capacities = [week_capacity(plan, week)["weekly_minutes"] for week in range(1, plan.total_weeks + 1)]
    total_limit = confirmed_total_capacity(source)
    independent = {}
    for promise in plan.commitments_json or []:
        if promise.get("minimum_total_minutes") and promise.get("workload_key"):
            key = promise["workload_key"]
            independent[key] = max(independent.get(key, 0), int(promise["minimum_total_minutes"]))
    conflicts = capacity_conflicts(plan)
    demand = sum(independent.values())
    if total_limit is not None and demand > total_limit:
        conflicts.append({"label": "用户确认总供给与独立成果合计", "cadence": "TOTAL",
            "weeks": list(range(1, plan.total_weeks + 1)), "minimum_minutes": demand,
            "available_minutes": total_limit, "deficit_minutes": demand - total_limit})
    return {"state": "INFEASIBLE" if conflicts else "UNKNOWN" if any(value is None for value in capacities) else "LOWER_BOUND_VALID",
            "unknown_weeks": [week for week, value in enumerate(capacities, 1) if value is None],
            "confirmed_total_minutes": total_limit, "verified_conflicts": conflicts}


def confirmed_total_capacity(source) -> int | None:
    """A daily/weekly 'total' is not the entire plan's supply.

    Text is a compatibility fallback. Scoped supply remains in temporal segments;
    do not multiply ambiguous prose or convert its units here.
    """
    text = str(source or "")
    totals = []
    for match in re.finditer(r"(?:总供给|总可用(?:时间)?|总容量|总计可投入)\s*(\d+)\s*分钟", text):
        clause_prefix = re.split(r"[，,；;。\n]", text[:match.start()])[-1]
        scoped = re.search(
            r"(?:每天|每日|每次|单日|当日|当天|每个执行日|每周|每月|本周|本月|"
            r"第[\d一二三四五六七八九十]+周)\s*(?:的|可用|可投入|时间|实际|确认|最大|上限|已确认|目前|最多|合计)*\s*$",
            clause_prefix,
        )
        if not scoped:
            totals.append(int(match[1]))
    # Preserve the existing first-explicit-total contract; conflicting totals
    # belong to fact revision/clarification, never infer a larger allowance here.
    return totals[0] if totals else None

def capacity_segment_summary(segment: dict) -> str:
    days = int(segment.get("available_days_per_week") or segment.get("weekly_execution_days") or 0)
    exceptions = int(segment.get("exception_days_per_week") or 0)
    ordinary = days - exceptions
    text = f"W{segment['start_week']}–W{segment['end_week']}：普通日{ordinary}天×{segment.get('suggested_daily_minutes')}分钟"
    if exceptions:
        weekdays = "/".join("一二三四五六日"[day] for day in segment.get("exception_weekdays") or [])
        text += f"，例外日{exceptions}天×{segment.get('exception_daily_minutes')}分钟"
        if weekdays:
            text += f"（周{weekdays}）"
    for override in segment.get("weekly_capacity_overrides") or []:
        merged = {**segment, **override, "start_week": override["week_index"], "end_week": override["week_index"],
                  "weekly_capacity_overrides": []}
        text += "；指定周：" + capacity_segment_summary(merged)
    return text


def week_capacity(plan, week: int) -> dict:
    segment = next((item for item in plan.temporal_segments_json or []
                    if int(item["start_week"]) <= week <= int(item["end_week"])), {})
    if not segment and any(item.get("available_days_per_week") for item in plan.temporal_segments_json or []):
        return {"days": 0, "daily_minutes": 0, "weekly_minutes": 0}
    override = next((item for item in segment.get("weekly_capacity_overrides", []) or []
                     if int(item.get("week_index", 0)) == week), None)
    values = override or segment
    days = int(values.get("available_days_per_week") or values.get("weekly_execution_days")
               or plan.weekly_execution_days)
    daily = values.get("suggested_daily_minutes")
    if daily is None:
        return {"days": days, "daily_minutes": None, "weekly_minutes": None}
    exceptions = min(days, int(values.get("exception_days_per_week", 0) or 0))
    extra = int(values.get("exception_daily_minutes") or daily)
    return {"days": days, "daily_minutes": int(daily),
            "weekly_minutes": int(daily) * (days - exceptions) + extra * exceptions,
            "exception_days": exceptions, "exception_minutes": extra,
            "exception_weekdays": values.get("exception_weekdays") or []}


def calendar_capacity(plan, anchor, value) -> dict:
    """Only the named weekday receives a scoped exception's time allowance."""
    week = plan_week_index(plan, anchor, value)
    if week < 1 or week > plan.total_weeks:
        return {"days": 0, "daily_minutes": 0, "weekly_minutes": 0}
    capacity = week_capacity(plan, week)
    if value.weekday() in capacity.get("exception_weekdays", []):
        return {**capacity, "daily_minutes": capacity["exception_minutes"]}
    return capacity


def capacity_conflicts(plan) -> list[dict]:
    """Prove deficits conservatively, without double-counting shared obligations.

    Each obligation is checked on its own. Passing this lower bound does not
    prove a schedule is feasible: dependencies and complete task loads are
    checked when generating the actual preview.
    """
    conflicts = []
    independent = []
    for promise in plan.commitments_json or []:
        key = promise.get("workload_key")
        minutes = promise.get("minimum_total_minutes")
        if key and minutes and promise["cadence"] == "TOTAL":
            independent.append((int(promise["start_week"]), int(promise["end_week"]), key, int(minutes)))
    # Different release weeks still compete for the same later capacity.
    # Test every relevant enclosing interval, deduplicating shared work keys.
    for start, end in sorted({(left[0], right[1]) for left in independent for right in independent
                              if left[0] <= right[1]}):
        workloads = {}
        for first, last, key, minutes in independent:
            if start <= first and last <= end:
                workloads[key] = max(workloads.get(key, 0), minutes)
        if len(workloads) < 2:
            continue
        capacities = [week_capacity(plan, week)["weekly_minutes"] for week in range(start, end + 1)]
        if all(value is not None for value in capacities) and sum(workloads.values()) > sum(capacities):
            required, available = sum(workloads.values()), sum(capacities)
            conflicts.append({"label": "独立成果工作量合计", "weeks": list(range(start, end + 1)),
                              "cadence": "TOTAL", "minimum_minutes": required,
                              "available_minutes": available, "deficit_minutes": required - available})
    for week in range(1, plan.total_weeks + 1):
        daily = [item for item in plan.commitments_json or []
                 if item["cadence"] == "PER_EXECUTION_DAY"
                 and int(item["start_week"]) <= week <= int(item["end_week"])]
        required = sum(int(item.get("minimum_minutes_per_occurrence") or 0)
                       * int(item.get("minimum_occurrences", 0)) for item in daily)
        available = week_capacity(plan, week)["daily_minutes"]
        if available is not None and required > available:
            conflicts.append({"label": "每日义务合计", "weeks": [week],
                              "cadence": "PER_EXECUTION_DAY", "minimum_minutes": required,
                              "available_minutes": available, "deficit_minutes": required - available})
    for promise in plan.commitments_json or []:
        count = int(promise.get("minimum_occurrences", 0))
        minutes = promise.get("minimum_minutes_per_occurrence")
        total_minutes = promise.get("minimum_total_minutes")
        if promise["cadence"] == "TOTAL" and total_minutes:
            minutes, count = total_minutes, 1
        if not minutes or not count or promise["cadence"] == "PER_EXECUTION_DAY":
            continue
        amount = count * int(minutes)
        start, end = int(promise["start_week"]), int(promise["end_week"])
        capacity = [week_capacity(plan, week) for week in range(start, end + 1)]
        if any(item["weekly_minutes"] is None for item in capacity):
            continue
        cadence = promise["cadence"]
        groups = ([(list(range(start, end + 1)), amount, sum(item["weekly_minutes"] for item in capacity))]
                  if cadence == "TOTAL" else [
                      ([week], amount, item["daily_minutes"] if cadence == "PER_EXECUTION_DAY" else item["weekly_minutes"])
                      for week, item in zip(range(start, end + 1), capacity, strict=True)
                  ])
        for weeks, required, available in groups:
            if required > available:
                conflicts.append({"label": promise["label"], "weeks": weeks,
                                  "cadence": cadence, "minimum_minutes": required,
                                  "available_minutes": available,
                                  "deficit_minutes": required - available})
    return conflicts
