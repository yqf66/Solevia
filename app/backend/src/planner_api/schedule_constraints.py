"""Convert confirmed conversational time commitments into hard calendar blocks.

The discussion flow intentionally stores the user's wording as a fact.  This
module is the small deterministic bridge to the scheduler: it only consumes
confirmed facts and never lets an unconfirmed model guess block a calendar.
"""

from __future__ import annotations

import json
import re
from datetime import date

from sqlalchemy import select

from planner_api.models import Goal, ManualPlan, RequirementFact

_TIME = r"(\d{1,2})\s*[:：]\s*(\d{2})"
_RANGE = re.compile(_TIME + r"\s*[-—至到]\s*" + _TIME)
_DATE_RANGE = re.compile(r"(\d{1,2})\s*[./月](\d{1,2})\s*[-—至到]\s*(\d{1,2})\s*[./月](\d{1,2})")


def _minutes(hour: str, minute: str) -> int:
    return int(hour) * 60 + int(minute)


def _ranges(text: str) -> list[tuple[int, int]]:
    result = []
    for match in _RANGE.finditer(text):
        start = _minutes(match.group(1), match.group(2))
        end = _minutes(match.group(3), match.group(4))
        if 0 <= start < end <= 24 * 60:
            result.append((start, end))
    return result


def _date_in_range(day: date, match: re.Match[str]) -> bool:
    start_month, start_day, end_month, end_day = map(int, match.groups())
    start = date(day.year, start_month, start_day)
    end = date(day.year, end_month, end_day)
    if end < start:  # ranges crossing New Year
        end = date(day.year + 1, end_month, end_day)
    return start <= day <= end


def confirmed_unavailable_windows(session, goal_id: str, day: date) -> list[tuple[int, int, str]]:
    """Return hard blocks inferred from confirmed course/commitment facts.

    This deliberately targets explicit course/lesson wording.  Generic
    capacity prose may mention times but must not silently block the calendar.
    """
    # A later conversational revision may supersede a broad fact (for
    # example, replacing the capacity paragraph) without retracting a
    # concrete course commitment inside it.  Keep explicit course facts from
    # both active and superseded history; an explicit delete/retraction is
    # still excluded by the text/operation checks below.
    facts = session.scalars(
        select(RequirementFact).where(
            RequirementFact.goal_id == goal_id,
            RequirementFact.status.in_(("CONFIRMED", "SUPERSEDED")),
            RequirementFact.operation != "DELETE",
        )
    )
    blocks: list[tuple[int, int, str]] = []
    for fact in facts:
        value = fact.value_json
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        if not re.search(r"(上课|课程|课|培训|授课)", text):
            continue
        # "除周日每天...有课" / "周一至周六...有课" means Sunday is free.
        weekdays = set(range(7))
        if "除周日" in text or "周一至周六" in text or "周一到周六" in text:
            weekdays = set(range(6))
        date_match = _DATE_RANGE.search(text)
        date_scoped = bool(date_match and ("每天下午" in text or "每天" in text))
        if not date_scoped and day.weekday() not in weekdays:
            continue
        if date_match and not _date_in_range(day, date_match):
            continue
        # Restrict to the explicit course sentence, not unrelated capacity
        # figures elsewhere in the same fact.
        course_text = text
        marker = re.search(r"(?:除周日|周一至周六|周一到周六).{0,180}?(?:有课|课程)", text)
        if marker:
            course_text = marker.group(0)
        if day.weekday() in weekdays:
            for start, end in _ranges(course_text):
                label = "课程占用"
                if "政治" in course_text or "理论" in course_text:
                    label = "政治理论课"
                blocks.append((start, end, label))
        # Date-scoped afternoon course often appears after the first sentence.
        if date_match and ("下午" in text or "政治" in text):
            suffix = text[date_match.end() :]
            for start, end in _ranges(suffix[:100]):
                blocks.append((start, end, "政治理论课" if "政治" in suffix else "课程占用"))
    # Deduplicate overlapping facts while keeping labels useful to diagnostics.
    unique: dict[tuple[int, int], str] = {}
    for start, end, label in blocks:
        unique[(start, end)] = label
    return [(start, end, label) for (start, end), label in sorted(unique.items())]


def shared_unavailable_windows(session, day: date) -> list[tuple[int, int, str]]:
    """Explicit commitments are shared, even when allocating only one plan."""
    blocks = []
    for goal_id in session.scalars(
        select(Goal.id).where(
            Goal.status.in_(["ACTIVE", "RECOVERY_REQUIRED"]),
        )
    ):
        blocks.extend(confirmed_unavailable_windows(session, goal_id, day))
    for plan in session.scalars(
        select(ManualPlan).where(
            ManualPlan.kind == "QUICK",
            ManualPlan.status == "ACTIVE",
        )
    ):
        for window in plan.arrangement_json.get("unavailable_windows", []):
            if window.get("date") == day.isoformat():
                start = _minutes(*window["start_local"].split(":"))
                end = _minutes(*window["end_local"].split(":"))
                blocks.append((start, end, str(window["reason"])))
    return sorted(set(blocks))
