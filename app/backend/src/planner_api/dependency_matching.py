"""One dependency binding rule shared by compilation and execution.

Identity is explicit and independent of display titles and scheduled dates.
An unknown binding fails closed; it must never degrade to a global gate or
silently drop a prerequisite.
"""

from collections.abc import Mapping


class DependencyBindingError(ValueError):
    pass


def matching_mode(edge) -> str:
    value = (
        edge.get("matching_mode", "ALL")
        if isinstance(edge, Mapping)
        else getattr(edge, "matching_mode", None)
    )
    mode = value or "ALL"  # SQLAlchemy defaults are assigned on flush.
    if mode not in {"ALL", "MATCH_OCCURRENCE", "MATCH_PERIOD"}:
        raise DependencyBindingError("无法识别依赖匹配方式，请重新确认依赖")
    return mode


def metadata(task) -> dict:
    return (
        getattr(task, "scheduling_policy_json", None)
        or getattr(task, "scheduling_policy_data", None)
        or {}
    )


def binding_key(task, mode, *, period_keys=None) -> str:
    data = metadata(task)
    if mode == "MATCH_OCCURRENCE":
        value = getattr(task, "occurrence_key", None) or data.get("occurrence_key")
    elif mode == "MATCH_PERIOD":
        value = data.get("dependency_period_key")
        if not value and period_keys is not None:
            value = period_keys.get(getattr(task, "id", None))
    else:
        return "ALL"
    if not isinstance(value, str) or not value.strip():
        label = "同次任务标识 occurrence_key" if mode == "MATCH_OCCURRENCE" else "依赖周期标识"
        raise DependencyBindingError(f"缺少{label}，不能猜测前置关系")
    return value


def select_predecessors(edge, successor, candidates, *, period_keys=None):
    """Candidates must already be restricted to this plan and predecessor item.

    More than one action per occurrence is legitimate (split work). All of
    those actions remain prerequisites. Cancellation is not completion.
    """
    mode = matching_mode(edge)
    candidates = [task for task in candidates if task is not successor]
    if mode == "ALL":
        # Preserve the historical contract for existing plans.
        return [
            task
            for task in candidates
            if getattr(task, "status", None) not in {"CANCELLED", "CARRIED_OVER"}
        ]
    key = binding_key(successor, mode, period_keys=period_keys)
    matched = [
        task for task in candidates if binding_key(task, mode, period_keys=period_keys) == key
    ]
    # A carried-over source is superseded only by its explicit replacement;
    # an orphaned source must continue blocking rather than disappear.
    replaced = {getattr(task, "source_task_id", None) for task in matched}
    matched = [
        task
        for task in matched
        if not (
            getattr(task, "status", None) == "CARRIED_OVER"
            and getattr(task, "id", None) in replaced
        )
    ]
    if not matched:
        raise DependencyBindingError(f"找不到对应前置任务（{key}），请补齐或确认关联")
    return matched
