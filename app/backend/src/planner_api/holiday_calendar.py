"""Workspace holiday calendars shared by date mapping, AI scheduling and UI."""
from __future__ import annotations

import json
import re
from datetime import date, timedelta
from importlib.resources import files
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from sqlalchemy import select
from sqlalchemy.orm import Session

from planner_api.config import get_settings
from planner_api.models import HolidayCalendarYear, WorkspaceScheduleProfile


def _date_range(start: str, end: str) -> list[str]:
    cursor, last = date.fromisoformat(start), date.fromisoformat(end)
    result = []
    while cursor <= last:
        result.append(cursor.isoformat())
        cursor += timedelta(days=1)
    return result


def _china_2026() -> list[dict[str, object]]:
    groups = [
        ("元旦", "2026-01-01", "2026-01-03"),
        ("春节", "2026-02-15", "2026-02-23"),
        ("清明节", "2026-04-04", "2026-04-06"),
        ("劳动节", "2026-05-01", "2026-05-05"),
        ("端午节", "2026-06-19", "2026-06-21"),
        ("中秋节", "2026-09-25", "2026-09-27"),
        ("国庆节", "2026-10-01", "2026-10-07"),
    ]
    days = [
        {"date": value, "kind": "HOLIDAY", "name": name, "local_name": name}
        for name, start, end in groups
        for value in _date_range(start, end)
    ]
    workdays = {
        "2026-01-04": "元旦调休上班",
        "2026-02-14": "春节调休上班",
        "2026-02-28": "春节调休上班",
        "2026-05-09": "劳动节调休上班",
        "2026-09-20": "国庆节调休上班",
        "2026-10-10": "国庆节调休上班",
    }
    days.extend(
        {"date": value, "kind": "WORKDAY", "name": name, "local_name": name}
        for value, name in workdays.items()
    )
    return sorted(days, key=lambda item: str(item["date"]))


BUILTIN_CALENDARS: dict[tuple[str, int], tuple[list[dict[str, object]], str]] = {
    ("CN", 2026): (
        _china_2026(),
        "国务院办公厅关于2026年部分节假日安排的通知",
    ),
}


def _timezone_country(timezone: str) -> str | None:
    try:
        contents = files("tzdata.zoneinfo").joinpath("zone.tab").read_text(encoding="utf-8")
    except (FileNotFoundError, ModuleNotFoundError, OSError):
        return None
    for line in contents.splitlines():
        if not line or line.startswith("#"):
            continue
        fields = line.split("\t")
        if len(fields) >= 3 and fields[2] == timezone:
            return fields[0].split(",", 1)[0]
    return None


def _locale_country(locale: str) -> str | None:
    for part in re.split(r"[-_]", locale)[1:]:
        if len(part) == 2 and part.isalpha():
            return part.upper()
    return None


def suggest_holiday_region(timezone: str, locale: str) -> dict[str, object]:
    timezone_country = _timezone_country(timezone)
    locale_country = _locale_country(locale)
    if timezone_country and locale_country == timezone_country:
        confidence, requires_confirmation, reason = 0.98, False, "TIMEZONE_LOCALE_AGREE"
    elif timezone_country and locale_country and timezone_country != locale_country:
        confidence, requires_confirmation, reason = 0.35, True, "TIMEZONE_LOCALE_CONFLICT"
    elif timezone_country:
        confidence, requires_confirmation, reason = 0.8, True, "TIMEZONE_ONLY"
    elif locale_country:
        confidence, requires_confirmation, reason = 0.62, True, "LOCALE_ONLY"
    else:
        confidence, requires_confirmation, reason = 0.0, True, "UNKNOWN"
    return {
        "country_code": timezone_country or locale_country,
        "timezone_country_code": timezone_country,
        "locale_country_code": locale_country,
        "confidence": confidence,
        "requires_confirmation": requires_confirmation,
        "reason": reason,
    }


class HolidayCalendarService:
    def __init__(self, session: Session) -> None:
        self.session = session
        self._year_cache: dict[tuple[str, str, int], HolidayCalendarYear | None] = {}

    def _profile(self) -> WorkspaceScheduleProfile | None:
        return self.session.get(WorkspaceScheduleProfile, "default")

    def _stored(self, country: str, subdivision: str, year: int) -> HolidayCalendarYear | None:
        return self.session.scalar(
            select(HolidayCalendarYear).where(
                HolidayCalendarYear.country_code == country,
                HolidayCalendarYear.subdivision_code == subdivision,
                HolidayCalendarYear.year == year,
            )
        )

    def _fetch_public_holidays(
        self, country: str, subdivision: str, year: int
    ) -> tuple[list[dict[str, object]], str] | None:
        settings = get_settings()
        url = f"{settings.holiday_api_base_url.rstrip('/')}/{year}/{country}"
        try:
            request = Request(url, headers={"User-Agent": "Solevia/0.1"})
            with urlopen(request, timeout=settings.holiday_api_timeout_seconds) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except (HTTPError, URLError, TimeoutError, json.JSONDecodeError, OSError):
            return None
        if not isinstance(payload, list):
            return None
        days = []
        for item in payload:
            if not isinstance(item, dict) or not isinstance(item.get("date"), str):
                continue
            subdivisions = item.get("counties") or item.get("subdivisionCodes") or []
            if subdivision and subdivisions and subdivision not in subdivisions:
                continue
            days.append(
                {
                    "date": item["date"],
                    "kind": "HOLIDAY",
                    "name": str(item.get("name") or item.get("localName") or "Public holiday"),
                    "local_name": str(item.get("localName") or item.get("name") or "公共假日"),
                }
            )
        return days, "Nager.Date Community API"

    def _ensure_year(self, country: str, subdivision: str, year: int) -> HolidayCalendarYear | None:
        cache_key = (country, subdivision, year)
        if cache_key in self._year_cache:
            return self._year_cache[cache_key]
        stored = self._stored(country, subdivision, year)
        if stored is not None:
            self._year_cache[cache_key] = stored
            return stored
        builtin = BUILTIN_CALENDARS.get((country, year))
        authoritative = builtin is not None
        # A generic public-holiday list is unsafe for China because it omits
        # make-up workdays. Wait for the annual official calendar instead.
        loaded = builtin if country == "CN" else builtin or self._fetch_public_holidays(
            country, subdivision, year
        )
        if loaded is None:
            self._year_cache[cache_key] = None
            return None
        days, source = loaded
        stored = HolidayCalendarYear(
            country_code=country,
            subdivision_code=subdivision,
            year=year,
            days_json=days,
            source=source,
            authoritative=authoritative,
        )
        self.session.add(stored)
        self.session.flush()
        self._year_cache[cache_key] = stored
        return stored

    def resolve(
        self, start_date: date, end_date: date
    ) -> tuple[list[dict[str, object]], list[int], list[str]]:
        profile = self._profile()
        if profile is None or not profile.holiday_country_code:
            return [], list(range(start_date.year, end_date.year + 1)), []
        country = profile.holiday_country_code
        subdivision = profile.holiday_subdivision_code or ""
        result = []
        missing_years = []
        sources = set()
        for year in range(start_date.year, end_date.year + 1):
            calendar = self._ensure_year(country, subdivision, year)
            if calendar is None:
                missing_years.append(year)
                continue
            sources.add(calendar.source)
            result.extend(
                {
                    **item,
                    "country_code": country,
                    "subdivision_code": subdivision or None,
                    "source": calendar.source,
                    "authoritative": calendar.authoritative,
                }
                for item in calendar.days_json
                if start_date <= date.fromisoformat(str(item["date"])) <= end_date
            )
        return sorted(result, key=lambda item: str(item["date"])), missing_years, sorted(sources)

    def days(self, start_date: date, end_date: date) -> list[dict[str, object]]:
        return self.resolve(start_date, end_date)[0]

    def day(self, value: date) -> dict[str, object] | None:
        return next(iter(self.days(value, value)), None)

    def is_execution_date(
        self, value: date, execution_weekdays: set[int], excluded_dates: set[date]
    ) -> bool:
        if value in excluded_dates:
            return False
        calendar_day = self.day(value)
        if calendar_day and calendar_day["kind"] == "HOLIDAY":
            return False
        if calendar_day and calendar_day["kind"] == "WORKDAY":
            return True
        return value.weekday() in execution_weekdays
