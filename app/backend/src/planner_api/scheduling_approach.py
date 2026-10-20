from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Literal

ScheduleType = Literal["OUTCOME", "RHYTHM", "CALENDAR"]


@dataclass(frozen=True)
class SchedulingApproachAdvice:
    recommended_type: ScheduleType
    reasons: list[str]
    confidence: Literal["LOW", "MEDIUM", "HIGH"]


_CALENDAR_MARKERS = (
    "截止",
    "之前完成",
    "考试",
    "开学",
    "面试",
    "报名",
    "发布日",
    "固定日期",
)
_RHYTHM_MARKERS = (
    "每天",
    "每日",
    "每周",
    "规律",
    "坚持",
    "习惯",
    "打卡",
    "频率",
    "每晚",
    "每早",
)
_OUTCOME_MARKERS = (
    "成果",
    "效果",
    "完成目标",
    "做到",
    "拍出",
    "做出",
    "作品",
    "有机会",
    "条件合适",
    "慢慢来",
    "不固定",
)

_NEGATION_MARKERS = ("不是", "无", "没有", "不需要", "不设", "暂无", "不固定", "不代表")


def _positive_marker(source: str, marker: str) -> bool:
    """Avoid treating a negated example (e.g. '不是交付物') as a deadline."""
    start = 0
    while True:
        position = source.find(marker, start)
        if position < 0:
            return False
        window = source[max(0, position - 16) : position]
        if not any(negation in window for negation in _NEGATION_MARKERS):
            return True
        start = position + len(marker)


def recommend_schedule_type(
    raw_input: str,
    requirements: dict[str, object],
    conversation_summary: str = "",
) -> SchedulingApproachAdvice:
    """Return an explainable fallback when the analysis model is unavailable."""

    source = " ".join(
        [raw_input, conversation_summary, json.dumps(requirements, ensure_ascii=False)]
    ).casefold()
    deadline = requirements.get("deadline")
    has_deadline = deadline not in (None, "", [], "无", "暂无", "没有", "NONE")
    calendar_hits = [marker for marker in _CALENDAR_MARKERS if _positive_marker(source, marker)]
    rhythm_hits = [marker for marker in _RHYTHM_MARKERS if _positive_marker(source, marker)]
    outcome_hits = [marker for marker in _OUTCOME_MARKERS if _positive_marker(source, marker)]

    if has_deadline or calendar_hits:
        return SchedulingApproachAdvice(
            recommended_type="CALENDAR",
            reasons=["讨论中存在明确期限或不可错过的日期，适合从日期倒排"],
            confidence="HIGH" if has_deadline else "MEDIUM",
        )
    if len(rhythm_hits) > len(outcome_hits):
        return SchedulingApproachAdvice(
            recommended_type="RHYTHM",
            reasons=["讨论中更强调固定频率、规律练习或习惯维持"],
            confidence="HIGH" if len(rhythm_hits) >= 2 else "MEDIUM",
        )
    return SchedulingApproachAdvice(
        recommended_type="OUTCOME",
        reasons=["当前更适合在一个弹性窗口内验收成果，不必预先锁定具体执行日"],
        confidence="MEDIUM" if outcome_hits else "LOW",
    )
