from datetime import date
from types import SimpleNamespace
from unittest.mock import Mock

from planner_api.execution_service import ExecutionService


def service_for(days, deadline):
    service = ExecutionService.__new__(ExecutionService)
    service.session = Mock()
    service.session.scalars.return_value = days
    service._authored_temporal_constraints = lambda *_: {}
    service._holiday_execution_dates = lambda *_: set()
    service._validate_activation_deadline = lambda *_: deadline
    service._is_plan_execution_date = lambda value, weekdays, excluded, *_: value.weekday() in weekdays and value not in excluded
    service._scheduled_read = lambda day, value: SimpleNamespace(plan_day_id=day.id, scheduled_date=value)
    service._tasks = lambda *_: [SimpleNamespace(plan_day_id=day.id, estimate_minutes=day.suggested_minutes) for day in days]
    return service


def test_final_partial_week_stays_inside_confirmed_deadline():
    days = [SimpleNamespace(id=str(i), week_index=4, day_index=i, suggested_minutes=30) for i in range(1, 5)]
    plan = SimpleNamespace(id="plan", total_weeks=4, schedule_type="CALENDAR", weekly_execution_days=7,
        temporal_segments_json=[{"start_week": 1, "end_week": 4, "available_days_per_week": 7, "suggested_daily_minutes": 90}])
    request = SimpleNamespace(smart_preview_id=None, requested_start_date=date(2026, 10, 21), execution_weekdays=list(range(7)), excluded_dates=[])
    result = service_for(days, date(2026, 11, 14))._mapped_days(plan, request)
    assert [item.scheduled_date for item in result] == [date(2026, 11, i) for i in [11, 12, 13, 14]]


def test_long_day_only_receives_named_weekday_capacity_exception():
    days = [SimpleNamespace(id=str(i), week_index=1, day_index=i, suggested_minutes=105 if i == 5 else 30) for i in range(1, 6)]
    plan = SimpleNamespace(id="plan", total_weeks=1, schedule_type="CALENDAR", weekly_execution_days=7,
        temporal_segments_json=[{"start_week": 1, "end_week": 1, "available_days_per_week": 7, "suggested_daily_minutes": 90,
                                 "exception_days_per_week": 1, "exception_daily_minutes": 180, "exception_weekdays": [6]}])
    request = SimpleNamespace(smart_preview_id=None, requested_start_date=date(2026, 10, 21), execution_weekdays=list(range(7)), excluded_dates=[])
    result = service_for(days, None)._mapped_days(plan, request)
    assert result[-1].scheduled_date == date(2026, 10, 25)


def test_explicit_iso_residual_week_is_recognized_when_compiling_new_plan():
    from planner_api.plan_calendar import calendar_weeks, plan_week_range
    plan = SimpleNamespace(schedule_type="CALENDAR", compiler_report_json={"week_basis": "ROLLING_START"},
                           temporal_segments_json=[{"start_week": 1, "end_week": 1,
                                                    "date_scope": "2026-10-21 至 2026-10-25"}])
    assert calendar_weeks(plan, from_source=True)
    # Published legacy coordinates do not silently change after an upgrade.
    assert not calendar_weeks(plan)
    plan.compiler_report_json = {"week_basis": "CALENDAR_MONDAY"}
    assert plan_week_range(plan, date(2026, 10, 21), 4)[0] == date(2026, 11, 9)
