"""Field domains and a narrow, fail-closed compiler retry classification.

This module never mutates a draft, resolves dependencies, or changes capacity.
"""
from __future__ import annotations

import re
from typing import get_args
from types import SimpleNamespace

from pydantic import ValidationError

from planner_api.ai import ProviderUnavailableError
from planner_api.plan_capacity import week_capacity
from planner_api.schemas import HybridPlanDetailResult, HybridPlanOutlineResult, PlanTaskDraft
from planner_api.work_item_domain import BindingMode


class CompilerRepresentationError(ProviderUnavailableError):
    """All reported errors are enum spellings in explicitly known field domains."""


def detail_capacity_contract(outline: dict, weeks: dict) -> dict | None:
    """Use the business guard's arithmetic, never average phase capacities."""
    start, end = weeks.get("start"), weeks.get("end")
    if not all(type(value) is int for value in (start, end)) or not 1 <= start <= end <= 24:
        return None
    if not isinstance(outline.get("temporal_segments"), list) or not outline.get("weekly_execution_days"):
        return None
    plan = SimpleNamespace(temporal_segments_json=outline["temporal_segments"],
                           weekly_execution_days=outline["weekly_execution_days"])
    try:
        rows = [{"week_index": week, **week_capacity(plan, week)} for week in range(start, end + 1)]
    except (TypeError, ValueError, KeyError):
        # An incomplete outline stays in the input unchanged and follows the
        # existing validation path. No guessed supply or fabricated ledger.
        return None
    return {"derived_from": "plan_outline.temporal_segments", "weeks": rows,
            "meaning": "确认供给上限，不是必须填满的任务量；OUTCOME D1使用周容量，其他模式仍校验日容量"}


def representation_error(error: Exception | None) -> bool:
    if not isinstance(error, ValidationError):
        return False
    rows = error.errors(include_input=False)
    if not rows:
        return False
    # Inspect every error, not the truncated human-readable diagnostic. Missing
    # fields, capacity/model validators and unknown paths retain the old route.
    patterns = (
        r"days\.\d+\.tasks\.\d+\.scheduling_policy",
        r"(?:standing_work_items|work_items)\.\d+\.constraint_set\.binding_mode",
    )
    return all(
        row["type"] in {"literal_error", "enum"}
        and any(re.fullmatch(pattern, ".".join(map(str, row["loc"]))) for pattern in patterns)
        for row in rows
    )


def compiler_instructions(instructions: str, schema_model: type, *, enabled: bool) -> str:
    if not enabled or schema_model not in {HybridPlanDetailResult, HybridPlanOutlineResult}:
        return instructions
    # Historical prompts contain the same three-line set check three times.
    # Keep one complete copy; this is exact text deduplication, not summarizing.
    for block in (
        "输出前必须完成集合自检：所有 item_key 唯一；每条依赖的两个端点都在本次\n"
        "standing_work_items 键集合中且不相同；每个候选键都在该键集合中。不得引用详情批次尚未生成的项。\n",
        "输出前必须完成集合自检：work_items 的 item_key 两两不同；依赖端点只能来自\n"
        "plan_outline.standing_work_item_registry 与本次 work_items 的键集合；候选组不含重复键或集合外键。\n",
    ):
        instructions = instructions.replace(block * 3, block)
    bindings = ", ".join(mode.value for mode in BindingMode)
    policies = ", ".join(get_args(PlanTaskDraft.model_fields["scheduling_policy"].annotation))
    return instructions + f"""
字段领域合同（仍须忠实编译原稿，不能借纠错重新设计）：
工作项 constraint_set.binding_mode 的合法值是 {bindings}。
days.tasks.scheduling_policy 的合法值是 {policies}；WINDOW 不能填入任务字段，
DATE_WINDOW 不能填入工作项字段。窗口起止周、次数和触发条件仍来自原稿；不能只改枚举丢约束。
work_item_dependencies 的端点是 item_key；days.tasks.depends_on 是真实前置任务的
M{{阶段}}-W{{周}}-D{{日}}-T{{序号}}，不能填 item_key。仅有工作项前置且没有唯一已生成任务时，
保留工作项依赖，不猜造任务引用；原稿明确的任务前置必须保留，不能为了校验删除。
workload_minutes 是已确认 workload_key 到实际分钟的字典，不是数字或工作项键。
suggested_minutes 表示本行实际安排的投入；任务估时合计不能超过它，也不能超过该周确认容量。
OUTCOME 的 D1 是整周成果窗口，不能当单日容量；RHYTHM/CALENDAR 仍须检查每日容量。
跨阶段成果、阶段最低工作量、原始时间策略与所有未受影响行动必须完整保留。
"""


DETAIL_AUTHOR_CAPACITY_CONTRACT = """
定稿前逐周核对：列明确认可用容量、本周真实行动估时合计、剩余必做工作量及后续可行窗口。
总量目标与本周投入分开表达；例如准备总量80分钟、W1容量60分钟时，不得把两项40分钟
动作都写入W1；仅在原约束允许时把剩余20分钟明确留到后续窗口，保留完整80分钟目标。
阶段容量不同则逐段核对，不取平均。OUTCOME的D1代表整周窗口；其他模式仍核对逐日上限。
若原约束内无法完成，明确冲突及需确认的调整方案，不能虚报估时、删必做成果或擅自加时间。
"""
