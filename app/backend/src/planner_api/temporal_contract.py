"""Resolve user dates against their source time, never the later confirmation."""
from datetime import UTC, date, datetime, timedelta
import re
from zoneinfo import ZoneInfo


def local_date(timestamp: datetime, timezone: str) -> date:
    return timestamp.replace(tzinfo=timestamp.tzinfo or UTC).astimezone(ZoneInfo(timezone)).date()


def resolve_deadline(value, reference: date | None = None) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if not isinstance(value, str):
        return None
    text = value.strip()
    # Only resolve unambiguous expressions. Durations/conditional dates require
    # clarification; matching a date buried in a conditional would invent consent.
    iso = re.fullmatch(r"(\d{4}-\d{2}-\d{2})(?:截止|之前|前|完成)?", text)
    if iso:
        try:
            return date.fromisoformat(iso[1])
        except ValueError:
            return None
    absolute = _absolute_completion_date(text, reference)
    if absolute:
        return absolute
    if reference is None:
        return None
    relative = re.fullmatch(r"(今天|今日|明天|后天)(?:截止|之前|前|完成)?", text)
    if relative:
        return reference + timedelta(days={"今天": 0, "今日": 0, "明天": 1, "后天": 2}[relative[1]])
    weekday = re.fullmatch(r"(本|这|下)周([一二三四五六日天])(?:截止|之前|前|完成)?", text)
    if weekday:
        offset = "一二三四五六日".index(weekday[2].replace("天", "日"))
        return reference - timedelta(days=reference.weekday()) + timedelta(
            days=offset + (7 if weekday[1] == "下" else 0))
    return None


def _absolute_completion_date(text: str, reference: date | None) -> date | None:
    # Select the preparation/completion boundary, never the exam/event date.
    # Conditional or alternative dates need clarification, not a guess.
    if re.search(r"如果|若(?:是|有|通过|未|能)|视情况|待定|或者|或|可能", text):
        return None
    # Whitespace between complete date components is formatting, not ambiguity.
    # Do not strip all spaces: that could join separate numbers into a new date.
    pattern = r"(?<!\d)(?:(\d{4})\s*(?:年|-|/)\s*)?(\d{1,2})\s*(?:月|-|/)\s*(\d{1,2})(?!\d)\s*(?:日|号)?"
    matches = list(re.finditer(pattern, text))
    years = {int(match[1]) for match in matches if match[1]}
    candidates = set()
    for match in matches:
        year = int(match[1]) if match[1] else (next(iter(years)) if len(years) == 1 else None)
        # Month/day alone is intentionally unresolved without a dated source.
        year = year or (reference.year if reference else None)
        if year is None:
            continue
        before = text[max(0, match.start() - 12):match.start()].rstrip()
        after = re.split(r"[，,；;。]", text[match.end():], maxsplit=1)[0].lstrip()
        sole = len(matches) == 1 and not text[:match.start()].strip() and not after.strip()
        explicit = bool(re.match(r"(?:晚|晚上|夜间)?(?:之前|以前|前|截止|完成|结束|交付|提交)", after)
                        or re.search(r"(?:截止(?:日期|时间|日)?(?:为|是|到)?|最晚(?:在|于)?)$", before))
        if sole or explicit:
            try:
                candidates.add(date(year, int(match[2]), int(match[3])))
            except ValueError:
                return None
    return next(iter(candidates)) if len(candidates) == 1 else None


def snapshot_deadline(snapshot: dict, *, timezone: str, fallback_time: datetime | None = None,
                      require_resolved: bool = False) -> date | None:
    context = snapshot.get("temporal_context") or {}
    # Guard against stale metadata after the deadline fact was superseded.
    source = context.get("deadline") or {}
    if source.get("source_text") == snapshot.get("deadline"):
        resolved = resolve_deadline(source.get("resolved_date"))
        if resolved:
            return resolved
    reference = local_date(fallback_time, timezone) if fallback_time else None
    if source.get("source_text") == snapshot.get("deadline") and source.get("reference_date"):
        reference = resolve_deadline(source["reference_date"])
    resolved = resolve_deadline(snapshot.get("deadline"), reference)
    value = snapshot.get("deadline")
    no_hard_deadline = isinstance(value, str) and bool(re.search(
        r"无(?:明确|固定|硬性|硬)?截止|没有(?:明确|固定|硬性|硬)?截止|不设.*截止|无硬期限|没有硬期限|无硬性期限|不设硬期限|不是硬性.*截止|没有硬截止|\bNONE\b|no (?:hard )?deadline", value, re.I))
    if isinstance(value, str):
        no_hard_deadline = no_hard_deadline or value.strip() in {"无", "没有", "暂无", "不确定", "未定"}
        # The deadline fact also stores aspirational timeframes. Do not turn
        # '希望两个月后开始投递' into a fabricated hard completion boundary.
        soft_timeframe = bool(re.search(r"希望|大约|大致|估计|复盘点|观察周期|目标周期", value))
        hard_marker = bool(re.search(r"必须|最晚|硬性|截止|之前|以前|前完成|前提交", value))
        no_hard_deadline = no_hard_deadline or (soft_timeframe and not hard_marker)
        estimated_range = bool(re.search(r"约\s*\d+\s*[-–到至]\s*\d+\s*个?[月周]", value))
        no_hard_deadline = no_hard_deadline or (estimated_range and not re.search(r"必须|最晚|硬性|截止", value))
    if require_resolved and value and not resolved and not no_hard_deadline:
        raise ValueError("截止日期尚未解析为唯一日期，请先澄清完成边界；不能按无截止日期继续排期。")
    return resolved


def relative_day_range(relative: str, anchor: date, *, plan=None) -> tuple[date, date] | None:
    """Canonical relative calendar days only; reject partial/opaque matches."""
    match = re.fullmatch(r"(?:M\d+[-.])?W(\d+)(?:[-.]D(\d+))?", relative.strip(), re.IGNORECASE)
    if not match:
        return None
    week, day = int(match[1]), int(match[2]) if match[2] else None
    if week < 1 or (day is not None and not 1 <= day <= 7):
        return None
    from planner_api.plan_calendar import plan_week_range
    first, last = plan_week_range(plan, anchor, week)
    if day is None:
        return first, last
    value = first + timedelta(days=day - 1)
    if value > last:
        return None
    return value, value
