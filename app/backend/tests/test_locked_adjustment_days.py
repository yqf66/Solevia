from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from planner_api.plan_service import PlanService
from planner_api.schemas import PlanDayDraft


def day(index, goal="保留已有执行记录"):
    return PlanDayDraft(stage_order=1, week_index=1, day_index=index,
                        primary_goal=goal, suggested_minutes=10, tasks=[], evidence=[])


def test_locked_day_is_restored_before_contiguous_week_validation():
    recorded = day(1)
    restored = PlanService._merge_locked_adjustment_days([day(2), day(3)], [recorded], 1)
    assert restored[0] is recorded
    assert [item.day_index for item in restored] == [1, 2, 3]


def test_model_cannot_rewrite_recorded_day_or_hide_an_unlocked_gap():
    recorded = day(1)
    restored = PlanService._merge_locked_adjustment_days([day(1, "模型重写已完成内容"), day(3)], [recorded], 1)
    assert restored[0].primary_goal == "保留已有执行记录"
    with pytest.raises(HTTPException) as error:
        PlanService._validate_detail_result(SimpleNamespace(), {}, [1], restored)
    assert error.value.detail["code"] == "PLAN_DETAIL_DAY_INDEX_INVALID"


def test_other_weeks_are_not_inserted_into_current_week():
    recorded = day(1).model_copy(update={"week_index": 2})
    assert PlanService._merge_locked_adjustment_days([day(1)], [recorded], 1) == [day(1)]
