"""One week coordinate system for calendar mapping, quotas and task windows.

Legacy plans keep rolling seven-day weeks. A plan explicitly authored with a
partial first week ending Sunday uses Monday calendar weeks throughout.
"""
from datetime import date, timedelta
import re


def calendar_weeks(plan, *, from_source=False) -> bool:
    basis = None if from_source else (getattr(plan, "compiler_report_json", None) or {}).get("week_basis")
    if basis in {"CALENDAR_MONDAY", "ROLLING_START"}:
        return basis == "CALENDAR_MONDAY"
    if getattr(plan, "schedule_type", None) != "CALENDAR":
        return False
    # Compatibility for explicit source contracts compiled before week_basis.
    # CALENDAR alone is insufficient evidence to reinterpret an existing plan.
    for segment in getattr(plan, "temporal_segments_json", None) or []:
        if segment.get("start_week") != 1 or segment.get("end_week") != 1:
            continue
        scope = str(segment.get("date_scope") or "")
        dates = re.findall(r"\d{4}-\d{2}-\d{2}", scope)
        if len(dates) == 2 and not re.search(r"或|建议|可选", scope):
            try:
                first, last = (date.fromisoformat(value) for value in dates)
            except ValueError:
                pass
            else:
                if 0 < first.weekday() < 6 and last.weekday() == 6 and (last - first).days == 6 - first.weekday():
                    return True
        match = re.search(r"(?:周|星期)([二三四五六])\s*(?:至|到|[—–-])\s*(?:周|星期)[日天]", scope)
        if match and not re.search(r"或|建议|可选", scope):
            return True
    return False


def plan_week_range(plan, anchor, week):
    origin = anchor - timedelta(days=anchor.weekday()) if calendar_weeks(plan) else anchor
    first = origin + timedelta(days=(week - 1) * 7)
    return max(anchor, first), first + timedelta(days=6)


def plan_week_index(plan, anchor, value):
    if value < anchor:
        return 0
    origin = anchor - timedelta(days=anchor.weekday()) if calendar_weeks(plan) else anchor
    return (value - origin).days // 7 + 1
