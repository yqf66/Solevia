"""Dates for fixtures whose goals explicitly/default to Asia/Shanghai.

The CI host can be in UTC. Using its date.today() puts fixture schedules one
day behind the goal between 16:00 and 24:00 UTC. Keep real production clocks
and guards intact; construct the fixture in its goal's own calendar instead.
"""
from datetime import datetime
from zoneinfo import ZoneInfo


def shanghai_today():
    return datetime.now(ZoneInfo("Asia/Shanghai")).date()
