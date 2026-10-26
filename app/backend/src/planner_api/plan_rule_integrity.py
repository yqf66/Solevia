"""Validate intent fidelity and work coverage independently of model bookkeeping.

These checks reject contradictions. They never increase capacity, change a
user's frequency or infer completion from a similar task title.
"""
import re

from planner_api.work_item_validation import WorkItemValidationError


_NUMBERS = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
            "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
_WEEKLY = re.compile(r"每(?:一)?周(?:至少|最少|不低于|不少于|要|需|安排|完成|进行|做|练习|练|各|有|\s)*([0-9]+|[一二两三四五六七八九十])\s*次")


def quantified_workloads(promises):
    """Qualitative evidence labels do not create a second minute obligation."""
    return {item["label"]: item["workload_key"] for item in promises
            if item.get("workload_key") and (item.get("minimum_total_minutes")
                                            or item.get("minimum_minutes_per_occurrence"))}


def _weekly_count(text):
    matches = _WEEKLY.findall(text)
    counts = {int(value) if value.isdigit() else _NUMBERS[value] for value in matches}
    return next(iter(counts)) if len(counts) == 1 else None


def _strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)
    elif isinstance(value, dict):
        for key, item in value.items():
            if key not in {"temporal_context", "capacity_source", "open_questions", "assumptions"}:
                yield from _strings(item)


def _identity(text):
    # Remove quantities and boilerplate before comparing provenance. The
    # remaining overlap must describe the activity, not merely '每周/分钟'.
    text = _WEEKLY.sub("", text)
    text = re.sub(r"[0-9一二两三四五六七八九十]+(?:分钟|小时|次)|至少|计入|本周|每次|完成", "", text)
    return {text[i:i + 2] for i in range(len(text) - 1)
            if re.fullmatch(r"[\u4e00-\u9fff]{2}", text[i:i + 2])}


def _weekly_activity(text):
    """Isolate an explicitly named activity from its surrounding constraint list."""
    clauses = [part for part in re.split(r"[，,、：:]|(?:以及|与|和)(?=每(?:一)?周)", text)
               if _weekly_count(part) is not None]
    if len(clauses) != 1:
        return ""
    value = _WEEKLY.sub("", clauses[0])
    value = re.sub(r"(?:每次)?\s*\d+\s*(?:分钟|小时)|仍保留|保持|不变|至少|最少|不少于", "", value)
    value = re.sub(r"[^\u4e00-\u9fff]", "", value)
    return value if 2 <= len(value) <= 12 else ""


def validate_recurrence_contracts(result, requirements):
    """Check explicit weekly quantifiers against their source, including scope.

    This intentionally does not guess the semantics of arbitrary prose. When
    a confirmed activity is uniquely identifiable, its quantifier overrides
    neither silently: a conflict is returned to the existing repair loop.
    """
    confirmed = [(text, _weekly_count(text)) for value in _strings(requirements)
                 for text in re.split(r"[；;。\n]", value) if _weekly_count(text) is not None]
    issues = []
    for item in getattr(result, "commitments", []):
        source = item.source_text or ""
        expected = _weekly_count(source) or _weekly_count(item.label)
        identity = _identity(source + " " + item.label)
        scope_source = source
        candidates = []
        for text, count in confirmed:
            other = _identity(text)
            score = len(identity & other) / max(1, min(len(identity), len(other)))
            activity = _weekly_activity(text)
            named_match = bool(activity and activity in source + item.label)
            if named_match or (len(identity & other) >= 3 and score >= 0.6):
                if named_match:
                    score = 1.0
                candidates.append((score, count, text))
        if candidates:
            best = max(score for score, _, _ in candidates)
            counts = {count for score, count, _ in candidates if score == best}
            if len(counts) == 1:
                expected = counts.pop()
                scope_source = next(text for score, _, text in candidates if score == best)
            else:
                issues.append(f"{item.label}: 已确认原文存在冲突频次，必须先澄清，不能由编译器任选。")
        if expected is None:
            continue
        if item.cadence != "PER_WEEK" or item.minimum_occurrences != expected:
            issues.append(f"{item.label}: 原文频次为每周{expected}次，编译为{item.cadence}/"
                          f"{item.minimum_occurrences}；每周次数不能乘以总周数，也不能改写用户原文。")
        # An unqualified weekly promise covers the plan, including its last
        # week. A phase exception needs an explicit source scope, not an
        # author's newly invented '收尾周不练习' preference.
        scoped = re.search(r"W\d|第[\d一二三四五六七八九十]+周|阶段|期间|如果|触发|仅在|前\d+周|后\d+周", scope_source, re.I)
        if not scoped and (item.start_week != 1 or item.end_week != result.total_weeks):
            issues.append(f"{item.label}: 每周承诺窗口应覆盖W1–W{result.total_weeks}，"
                          f"实际W{item.start_week}–W{item.end_week}；缩短窗口需要用户明确同意。")
    # Also reject deletion of an explicitly quantified activity. A capacity
    # statement ('每周4次，每次60分钟') alone has no activity identity and is
    # deliberately not converted into a new task obligation.
    for text, _ in confirmed:
        identity = _identity(text)
        if len(identity) < 3:
            continue
        represented = False
        for item in getattr(result, "commitments", []):
            other = _identity((item.source_text or "") + " " + item.label)
            overlap = len(identity & other)
            activity = _weekly_activity(text)
            named_match = bool(activity and activity in (item.source_text or "") + item.label)
            if named_match or (overlap >= 3 and overlap / max(1, min(len(identity), len(other))) >= 0.6):
                represented = item.cadence == "PER_WEEK"
                if represented:
                    break
        if not represented:
            issues.append("原文每周义务没有可核对的每周承诺: " + text)
    if issues:
        raise WorkItemValidationError("PLAN_RECURRENCE_SOURCE_CONFLICT", tuple(issues))


def validate_action_coverage(items, days, *, through_week, milestone_weeks=None):
    """Every due required identity needs an explicit, eligible action binding."""
    represented = {}
    for day in days:
        for task in day.tasks:
            if getattr(task, "status", None) in {"CANCELLED", "CARRIED_OVER"}:
                continue
            keys = getattr(task, "work_item_keys", None)
            if not keys:
                policy = getattr(task, "scheduling_policy_json", None) or getattr(task, "scheduling_policy_data", {}) or {}
                keys = policy.get("work_item_keys", [])
            for key in keys:
                represented.setdefault(key, set()).add(day.week_index)
    missing = []
    for item in items:
        if not item.is_required:
            continue
        draft = getattr(item, "constraint_set", None)
        binding = str(draft.binding_mode) if draft else item.binding_mode
        if binding == "ON_DEMAND":
            continue
        temporal = draft.temporal.model_dump() if draft else (item.constraints_json or {}).get("temporal", {})
        bounds = (milestone_weeks or {}).get(item.stage_order, (1, through_week))
        first = temporal.get("start_week") or bounds[0]
        last = temporal.get("end_week") or bounds[1]
        recurrence = draft.recurrence.model_dump() if draft else (item.constraints_json or {}).get("recurrence", {})
        if binding == "CADENCE" and recurrence.get("period") == "WEEK":
            interval = max(1, int(recurrence.get("interval") or 1))
            absent = set(range(first, min(last, through_week) + 1, interval)) - represented.get(item.item_key, set())
            if absent:
                missing.append(f"{item.item_key} (每周义务缺失W{sorted(absent)})")
        elif last <= through_week and not any(first <= week <= last for week in represented.get(item.item_key, ())):
            missing.append(f"{item.item_key} (W{first}–W{last})")
    if missing:
        raise WorkItemValidationError("HYBRID_REQUIRED_ACTION_MISSING", (
            "必须工作项缺少窗口内的执行任务: " + ", ".join(missing) +
            "；须让真实动作明确引用原工作项，不能新增相似身份或跳过前置依赖。",))
