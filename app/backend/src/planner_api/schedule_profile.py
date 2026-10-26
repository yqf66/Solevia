from __future__ import annotations

from typing import Protocol


class RoutineProfile(Protocol):
    wake_time: str
    available_from: str
    breakfast_enabled: bool
    breakfast_start: str
    breakfast_end: str
    lunch_enabled: bool
    lunch_start: str
    lunch_end: str
    nap_enabled: bool
    nap_start: str
    nap_end: str
    dinner_enabled: bool
    dinner_start: str
    dinner_end: str
    sleep_time: str
    routine_exclusions_json: list


def _minutes(value: str) -> int:
    hour, minute = map(int, value.split(":"))
    return hour * 60 + minute


def _clock(value: int) -> str:
    return f"{value // 60:02d}:{value % 60:02d}"


def routine_windows(profile: RoutineProfile) -> list[tuple[str, str]]:
    """Return awake time minus meals and the configured midday rest."""
    start = max(_minutes(profile.wake_time), _minutes(profile.available_from))
    end = _minutes(profile.sleep_time)
    if start >= end:
        return []
    named_blocks = (
        (profile.breakfast_enabled, profile.breakfast_start, profile.breakfast_end),
        (profile.lunch_enabled, profile.lunch_start, profile.lunch_end),
        (profile.nap_enabled, profile.nap_start, profile.nap_end),
        (profile.dinner_enabled, profile.dinner_start, profile.dinner_end),
    )
    blocked = [
        (_minutes(item_start), _minutes(item_end))
        for enabled, item_start, item_end in named_blocks
        if enabled
    ]
    blocked.extend(
        (_minutes(item["start_local"]), _minutes(item["end_local"]))
        for item in (profile.routine_exclusions_json or [])
        if item.get("enabled", True)
    )
    blocked.sort()
    cursor = start
    result: list[tuple[str, str]] = []
    for blocked_start, blocked_end in blocked:
        blocked_start = max(start, blocked_start)
        blocked_end = min(end, blocked_end)
        if blocked_start > cursor:
            result.append((_clock(cursor), _clock(blocked_start)))
        cursor = max(cursor, blocked_end)
    if cursor < end:
        result.append((_clock(cursor), _clock(end)))
    return result
