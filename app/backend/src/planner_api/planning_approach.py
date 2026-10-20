from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Literal

PlanningMode = Literal["EXPLORATORY", "COMMITTED"]


@dataclass(frozen=True)
class PlanningApproachAdvice:
    recommended_mode: PlanningMode
    reasons: list[str]
    confidence: Literal["LOW", "MEDIUM", "HIGH"]
    explicit_trial_weeks: int | None = None


_EXPLORATORY_MARKERS = (
    "先试试",
    "试试看",
    "尝试",
    "体验",
    "感兴趣",
    "兴趣",
    "探索",
    "了解一下",
    "看看效果",
    "观察效果",
    "还不确定",
    "不确定",
    "入门",
)
_COMMITTED_MARKERS = (
    "必须",
    "一定要",
    "截止",
    "考试",
    "通过",
    "交付",
    "完成",
    "拿到",
    "报名",
    "项目",
    "工作需要",
    "明确",
    "系统",
    "长期",
)
_CHINESE_NUMBERS = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6}


def recommend_planning_approach(
    raw_input: str,
    requirements: dict[str, object],
    conversation_summary: str = "",
) -> PlanningApproachAdvice:
    """Compile a transparent recommendation from confirmed discussion memory.

    This deliberately has no additional model call: the recommendation remains
    available even when the provider is unavailable, while the existing model
    assisted clarification still supplies the structured facts it evaluates.
    """

    source = " ".join(
        [raw_input, conversation_summary, json.dumps(requirements, ensure_ascii=False)]
    ).casefold()
    exploratory_hits = [marker for marker in _EXPLORATORY_MARKERS if marker in source]
    committed_hits = [marker for marker in _COMMITTED_MARKERS if marker in source]
    deadline = requirements.get("deadline")
    has_deadline = deadline not in (None, "", [], "无", "暂无", "没有")
    success_criteria = requirements.get("success_criteria")
    has_concrete_outcome = success_criteria not in (None, "", [])
    explicit_trial_weeks = _parse_trial_weeks(source)

    exploratory_score = len(exploratory_hits) * 2 + (2 if explicit_trial_weeks else 0)
    committed_score = (
        len(committed_hits) + (2 if has_deadline else 0) + (1 if has_concrete_outcome else 0)
    )
    if exploratory_score > committed_score:
        reasons = ["讨论中更强调尝试、兴趣或先观察实际效果"]
        if explicit_trial_weeks:
            reasons.append("你已经表达了一个明确的试行观察周期")
        reasons.append("先形成低门槛的基础闭环，更容易用真实体验决定是否继续投入")
        recommendation: PlanningMode = "EXPLORATORY"
    else:
        reasons = ["讨论中已经形成较明确的目标与验收标准"]
        if has_deadline:
            reasons.append("已有时间边界，需要从一开始统筹完整路线")
        elif committed_hits:
            reasons.append("你的表达更接近确定投入并按规范持续执行")
        reasons.append("采用系统路线更利于完整覆盖依赖关系并稳定推进")
        recommendation = "COMMITTED"

    gap = abs(exploratory_score - committed_score)
    confidence: Literal["LOW", "MEDIUM", "HIGH"] = (
        "HIGH" if gap >= 4 else "MEDIUM" if gap >= 2 else "LOW"
    )
    return PlanningApproachAdvice(
        recommended_mode=recommendation,
        reasons=reasons[:3],
        confidence=confidence,
        explicit_trial_weeks=explicit_trial_weeks,
    )


def _parse_trial_weeks(source: str) -> int | None:
    window = re.search(r"(\d+|[一二两三四五六])\s*周\s*(?:的)?(?:观察|试行|试用|探索)?窗口", source)
    if window:
        value = window.group(1)
        return int(value) if value.isdigit() else _CHINESE_NUMBERS[value]
    match = re.search(
        r"(?:先试试|试试看|尝试|体验|观察|先做).{0,16}?(\d+|[一二两三四五六])\s*(天|周|个月|月)",
        source,
    )
    if not match:
        return None
    raw_value, unit = match.groups()
    value = int(raw_value) if raw_value.isdigit() else _CHINESE_NUMBERS[raw_value]
    if unit == "天":
        return max(1, (value + 6) // 7)
    if unit in {"月", "个月"}:
        return value * 4
    return value
