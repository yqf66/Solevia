from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from contextvars import ContextVar
from typing import Literal, Protocol, TypeVar, cast
from urllib.error import HTTPError, URLError

from pydantic import BaseModel, ValidationError

from planner_api.ai import (
    JSONTransport,
    ProviderConfigurationError,
    ProviderUnavailableError,
    _replace_retry_feedback,
    _safe_validation_feedback,
    _urllib_json_transport,
    configured_json_transport,
)
from planner_api.config import Settings
from planner_api.plan_compiler_repair import digest
from planner_api.plan_compiler_contract import (
    CompilerRepresentationError,
    DETAIL_AUTHOR_CAPACITY_CONTRACT,
    compiler_instructions,
    detail_capacity_contract,
    representation_error,
)
from planner_api.context_compiler import compile_context_payload, deduplicate_context_snapshots
from planner_api.plan_prompts import (
    HYBRID_PLAN_DETAIL_AUTHOR_INSTRUCTIONS,
    HYBRID_PLAN_DETAIL_COMPILER_INSTRUCTIONS,
    HYBRID_PLAN_OUTLINE_AUTHOR_INSTRUCTIONS,
    HYBRID_PLAN_OUTLINE_COMPILER_INSTRUCTIONS,
    HYBRID_PLAN_PROMPT_VERSION,
    PLAN_DETAIL_AUTHOR_INSTRUCTIONS,
    PLAN_DETAIL_COMPILER_INSTRUCTIONS,
    PLAN_DETAIL_INSTRUCTIONS,
    PLAN_OUTLINE_AUTHOR_INSTRUCTIONS,
    PLAN_OUTLINE_COMPILER_INSTRUCTIONS,
    PLAN_OUTLINE_INSTRUCTIONS,
    PLAN_PROMPT_VERSION,
)
from planner_api.schemas import (
    HybridPlanDetailResult,
    HybridPlanOutlineResult,
    PlanCommitmentDraft,
    PlanDayDraft,
    PlanDetailResult,
    PlanMilestoneDraft,
    PlanOutlineResult,
    PlanTaskDraft,
    PlanTemporalSegmentDraft,
)
from planner_api.work_item_domain import (
    BindingMode,
    CompletionConstraint,
    FulfillmentMode,
    PlanWorkItemDraft,
    RecurrenceConstraint,
    TargetUnit,
    TemporalConstraint,
    WorkItemConstraintSet,
    WorkItemScope,
)
from planner_api.model_telemetry import model_node, observed_model_call, mark_attempt_failure, trace_policy

PlanResult = TypeVar("PlanResult", bound=BaseModel)
GenerationMode = Literal["STANDARD", "DEEP"]
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PlanContext:
    requirements: dict[str, object]
    route: dict[str, object]
    planning_context: dict[str, object] | None = None
    source_document: str | None = None


class PlanProvider(Protocol):
    @property
    def supported_generation_modes(self) -> tuple[GenerationMode, ...]: ...

    def draft_outline(
        self, context: PlanContext, generation_mode: GenerationMode = "STANDARD"
    ) -> PlanOutlineResult: ...

    def draft_details(
        self,
        context: PlanContext,
        outline: dict[str, object],
        start_week: int,
        end_week: int,
        generation_mode: GenerationMode = "STANDARD",
    ) -> PlanDetailResult: ...

    def draft_hybrid_outline(
        self,
        context: PlanContext,
        generation_mode: GenerationMode = "STANDARD",
        checkpoint: Callable[[str], None] | None = None,
    ) -> HybridPlanOutlineResult: ...

    def draft_hybrid_details(
        self,
        context: PlanContext,
        outline: dict[str, object],
        start_week: int,
        end_week: int,
        generation_mode: GenerationMode = "STANDARD",
        checkpoint: Callable[[str], None] | None = None,
    ) -> HybridPlanDetailResult: ...


def _plan_input(
    context: PlanContext,
    *,
    prompt_version: str = PLAN_PROMPT_VERSION,
    source_feedback_enabled: bool = True,
    compiler_contract_enabled: bool = False,
    **extra: object,
) -> str:
    if (compiler_contract_enabled and isinstance(extra.get("plan_outline"), dict)
            and isinstance(extra.get("requested_weeks"), dict)):
        capacity = detail_capacity_contract(extra["plan_outline"], extra["requested_weeks"])
        if capacity is not None:
            extra["compiled_week_capacity"] = capacity
    payload = deduplicate_context_snapshots(
        {
            "prompt_version": prompt_version,
            "confirmed_requirements": context.requirements,
            "confirmed_route": context.route,
            "planning_context": context.planning_context,
            "source_document": context.source_document,
            "calendar_binding": "RELATIVE_ONLY",
            **extra,
        },
        include_source_feedback=source_feedback_enabled,
    )
    # The compiler preserves the complete contract. Measure the actual wire
    # snapshots after lossless same-request references, not duplicated copies.
    payload = compile_context_payload(
        payload, task_type=f"PLAN_{str(extra.get('generation_step', 'GENERATION'))}",
    )
    return json.dumps(
        payload,
        ensure_ascii=False,
    )


def _diagnostic(error: Exception | None) -> str | None:
    if isinstance(error, HTTPError):
        return f"HTTP {error.code}"
    return _safe_validation_feedback(error) if error else None


class _PlanOutputTruncated(ValueError):
    """An explicit provider length stop, separate from malformed JSON."""


def _expand_truncated_plan_output(
    payload: dict[str, object], ceiling: int, *, final_retry: bool,
) -> dict[str, object]:
    token_field = "max_output_tokens" if "max_output_tokens" in payload else "max_tokens"
    current = int(payload[token_field])
    effective_ceiling = max(current, ceiling)
    budget = effective_ceiling if final_retry else min(effective_ceiling, current * 2)
    correction = (
        "上一次输出达到长度上限被截断，已临时提高本次输出额度。请重新输出完整结果，"
        "压缩重复说明、使用简洁表达；保留全部任务、次数、窗口、前置关系和必需字段。"
        "不得删任务、减少必做次数或修改用户时间容量来缩短输出。"
    )
    expanded = {**payload, token_field: budget}
    if "instructions" in payload:
        expanded["instructions"] = _replace_retry_feedback(
            str(payload["instructions"]), correction
        )
    else:
        messages = [
            item
            for item in payload["messages"]  # type: ignore[union-attr]
            if not (
                isinstance(item, dict)
                and item.get("role") == "system"
                and "[RETRY_FEEDBACK]" in str(item.get("content", ""))
            )
        ]
        expanded["messages"] = [
            *messages,
            {"role": "system", "content": _replace_retry_feedback("", correction)},
        ]
    logger.info("Retrying truncated plan output with token budget %s -> %s", current, budget)
    return expanded


_semantic_retry_owner: ContextVar[bool] = ContextVar("plan_semantic_retry_owner", default=False)


def _plan_generation_error(error: Exception | None) -> ProviderUnavailableError:
    if isinstance(error, _PlanOutputTruncated):
        return ProviderUnavailableError(
            "模型输出在自动扩容后仍被截断。已完成内容保留，可以调整模型输出上限后继续生成。",
            diagnostic=str(error), code="MODEL_OUTPUT_TRUNCATED",
        )
    if isinstance(error, HTTPError) and error.code == 504:
        return ProviderUnavailableError(
            "这次时间安排等待得有些久。已经确认和完成的内容仍然保留，可以直接重试。",
            diagnostic="HTTP 504",
            code="MODEL_GATEWAY_TIMEOUT",
        )
    if isinstance(error, (ValueError, ValidationError)):
        error_type = CompilerRepresentationError if representation_error(error) else ProviderUnavailableError
        return error_type(
            "这次时间安排没有整理完整。已经确认的内容仍然保留，可以直接重试。",
            diagnostic=_diagnostic(error),
            code="INVALID_MODEL_RESPONSE",
        )
    return ProviderUnavailableError(
        "Solevia 暂时没有完成时间安排。已经确认的内容仍然保留，可以稍后重试。",
        diagnostic=_diagnostic(error),
        code="PROVIDER_ERROR",
    )


def _strip_code_fence(content: str) -> str:
    normalized = content.strip()
    if normalized.startswith("```") and normalized.endswith("```"):
        first_line_end = normalized.find("\n")
        if first_line_end != -1:
            return normalized[first_line_end + 1 : -3].strip()
    return normalized


def _validate_plan_response[ResponseModel: BaseModel](
    schema_model: type[ResponseModel],
    content: str,
    dependency_registry: list[dict[str, str]] | None = None,
) -> ResponseModel:
    raw = json.loads(_strip_code_fence(content))
    if isinstance(raw, dict):
        if issubclass(schema_model, PlanOutlineResult):
            normalize_compiled_outline_capacity(raw)
        if schema_model in {HybridPlanOutlineResult, HybridPlanDetailResult}:
            normalize_hybrid_contract(raw)
        if schema_model == HybridPlanDetailResult:
            # Omission must preserve flexibility, never silently create a
            # fixed obligation just because the compatibility schema has D1.
            for day in raw.get("days", []) or []:
                if not isinstance(day, dict):
                    continue
                for task in day.get("tasks", []) or []:
                    if not isinstance(task, dict):
                        continue
                    task.setdefault("scheduling_policy", "FLOATING")
                    if task["scheduling_policy"] == "FLOATING":
                        task.setdefault("scheduling_policy_data", {
                            "start_week": day["week_index"], "end_week": day["week_index"],
                        })
        if schema_model in {PlanDetailResult, HybridPlanDetailResult}:
            normalize_compiled_plan_days(raw, "days", dependency_registry)
        allowed = set(schema_model.model_fields)
        dropped = sorted(set(raw) - allowed)
        if dropped:
            logger.warning(
                "Dropped unexpected top-level fields from %s: %s",
                schema_model.__name__,
                ", ".join(dropped),
            )
            raw = {key: value for key, value in raw.items() if key in allowed}
    return schema_model.model_validate(raw)


def normalize_hybrid_group_bounds(raw: dict[str, object]) -> None:
    """Repair only arithmetic SELECT_K bounds; never invent or drop candidates."""
    groups = raw.get("work_item_groups")
    if not isinstance(groups, list):
        return
    for group in groups:
        if not isinstance(group, dict):
            continue
        candidates = group.get("candidate_item_keys")
        if not isinstance(candidates, list) or not candidates:
            continue
        candidates = list(dict.fromkeys(str(candidate) for candidate in candidates))
        group["candidate_item_keys"] = candidates
        candidate_count = len(candidates)
        minimum = min(
            candidate_count,
            max(1, int(group.get("minimum_selected", 1) or 1)),
        )
        group["minimum_selected"] = minimum
        maximum = group.get("maximum_selected")
        if maximum is not None:
            group["maximum_selected"] = min(
                candidate_count,
                max(minimum, int(maximum or minimum)),
            )


def normalize_hybrid_contract(raw: dict[str, object]) -> None:
    """Canonicalize representation-only Hybrid fields before strict validation.

    These repairs are deterministic consequences of an explicit enum choice or an
    unambiguous registry relationship. Semantic constraints such as cadence periods,
    trigger conditions and dependency endpoints remain model-owned and fail closed.
    """
    normalize_hybrid_group_bounds(raw)
    _normalize_hybrid_keys(raw)
    total_weeks = _positive_int(raw.get("total_weeks"))
    milestones = raw.get("milestones")
    milestone_rows = (
        [item for item in milestones if isinstance(item, dict)]
        if isinstance(milestones, list)
        else []
    )
    stage_order_map: dict[int, int] = {}
    for index, milestone in enumerate(milestone_rows, start=1):
        previous_order = _positive_int(milestone.get("stage_order"))
        if previous_order is not None:
            stage_order_map.setdefault(previous_order, index)
        milestone["stage_order"] = index
    if total_weeks and milestone_rows:
        last_end = _positive_int(milestone_rows[-1].get("end_week"))
        if last_end and last_end < total_weeks:
            milestone_rows[-1]["end_week"] = total_weeks
    segments = raw.get("temporal_segments")
    segment_rows = (
        [item for item in segments if isinstance(item, dict)] if isinstance(segments, list) else []
    )
    if total_weeks and segment_rows and total_weeks >= len(segment_rows):
        cursor = 1
        for index, segment in enumerate(segment_rows):
            remaining = len(segment_rows) - index - 1
            latest_end = total_weeks - remaining
            proposed_end = _positive_int(segment.get("end_week")) or cursor
            segment["start_week"] = cursor
            segment["end_week"] = min(latest_end, max(cursor, proposed_end))
            cursor = int(segment["end_week"]) + 1
            normal_minutes = _positive_int(segment.get("suggested_daily_minutes"))
            exception_minutes = _positive_int(segment.get("exception_daily_minutes"))
            # Missing exception days/minutes are an incomplete supply contract,
            # not authorization to invent another ordinary day.
            if normal_minutes:
                segment["suggested_daily_minutes"] = normal_minutes
                normal_minutes = int(segment["suggested_daily_minutes"])
            if exception_minutes:
                segment["exception_daily_minutes"] = exception_minutes
                exception_minutes = int(segment["exception_daily_minutes"])
            # An exception may be shorter than an ordinary day. Formatting
            # normalization must never increase the user's confirmed supply.
            execution_days = _positive_int(segment.get("weekly_execution_days"))
            if execution_days:
                segment["weekly_execution_days"] = min(7, execution_days)
        segment_rows[-1]["end_week"] = total_weeks

    groups = raw.get("work_item_groups")
    group_rows = (
        [item for item in groups if isinstance(item, dict)] if isinstance(groups, list) else []
    )
    candidate_groups: dict[str, list[str]] = {}
    for group in group_rows:
        stage_order = _positive_int(group.get("stage_order"))
        if stage_order in stage_order_map:
            group["stage_order"] = stage_order_map[stage_order]
        group_key = str(group.get("group_key") or "").strip()
        candidates = group.get("candidate_item_keys")
        if not group_key or not isinstance(candidates, list):
            continue
        for candidate in candidates:
            candidate_groups.setdefault(str(candidate), []).append(group_key)

    for collection_name in ("standing_work_items", "work_items"):
        collection = raw.get(collection_name)
        if not isinstance(collection, list):
            continue
        for item in collection:
            if not isinstance(item, dict):
                continue
            constraints = item.get("constraint_set")
            if not isinstance(constraints, dict):
                continue
            stage_order = _positive_int(item.get("stage_order"))
            if stage_order in stage_order_map:
                item["stage_order"] = stage_order_map[stage_order]
            estimate = _positive_int(item.get("estimate_minutes_per_occurrence"))
            if estimate:
                item["estimate_minutes_per_occurrence"] = min(960, estimate)
            temporal = constraints.get("temporal")
            if isinstance(temporal, dict):
                weekdays = temporal.get("allowed_weekdays")
                if (
                    isinstance(weekdays, list)
                    and weekdays
                    and 7 in weekdays
                    and all(isinstance(day, int) and 1 <= day <= 7 for day in weekdays)
                ):
                    temporal["allowed_weekdays"] = [day - 1 for day in weekdays]
            binding_mode = str(constraints.get("binding_mode") or "")
            trigger = constraints.get("trigger")
            if binding_mode == "TRIGGERED" and not trigger:
                description = str(item.get("description") or "").strip()
                if description:
                    constraints["trigger"] = {"description": description}

            recurrence = constraints.get("recurrence")
            if not isinstance(recurrence, dict):
                recurrence = {}
                constraints["recurrence"] = recurrence
            misplaced_weekdays = recurrence.pop("allowed_weekdays", None)
            if isinstance(misplaced_weekdays, list):
                temporal = constraints.get("temporal")
                if not isinstance(temporal, dict):
                    temporal = {}
                    constraints["temporal"] = temporal
                if not temporal.get("allowed_weekdays"):
                    temporal["allowed_weekdays"] = misplaced_weekdays
                    if 7 in misplaced_weekdays and all(
                        isinstance(day, int) and 1 <= day <= 7 for day in misplaced_weekdays
                    ):
                        temporal["allowed_weekdays"] = [day - 1 for day in misplaced_weekdays]
            if binding_mode == "CADENCE" and recurrence.get("period") in {None, "ONCE"}:
                recurrence["period"] = "WEEK"

            fulfillment = str(constraints.get("fulfillment_mode") or "")
            selection = constraints.get("selection")
            if (
                fulfillment != "SELECT_K"
                and isinstance(selection, dict)
                and selection.get("mode") == "SELECT_K"
                and not selection.get("group_key")
            ):
                # Completion and selection are independent axes: a chosen
                # candidate may still require evidence/count/minutes. Never
                # erase its explicit group gate because it is not SELECT_K
                # fulfillment; business validation checks the membership.
                constraints["selection"] = {
                    "group_key": None,
                    "mode": "ALL",
                    "minimum_selected": 1,
                    "maximum_selected": None,
                }
            completion = constraints.get("completion")
            if not isinstance(completion, dict):
                completion = {}
                constraints["completion"] = completion
            if fulfillment == "ONCE":
                completion["target_value"] = 1
            elif fulfillment == "DURATION":
                completion["target_unit"] = "MINUTES"
            elif fulfillment == "EVIDENCE":
                completion["target_unit"] = "EVIDENCE"
                completion["evidence_required"] = True
            elif fulfillment == "SELECT_K":
                selection = constraints.get("selection")
                if not isinstance(selection, dict):
                    selection = {}
                    constraints["selection"] = selection
                selection["mode"] = "SELECT_K"
                if not selection.get("group_key"):
                    matches = candidate_groups.get(str(item.get("item_key") or ""), [])
                    if len(matches) == 1:
                        selection["group_key"] = matches[0]
                if not selection.get("group_key"):
                    target = (
                        _positive_int(selection.get("minimum_selected"))
                        or _positive_int(completion.get("target_value"))
                        or 1
                    )
                    constraints["fulfillment_mode"] = "COUNT"
                    constraints["selection"] = {
                        "group_key": None,
                        "mode": "ALL",
                        "minimum_selected": 1,
                        "maximum_selected": None,
                    }
                    completion["target_value"] = target
                    completion["target_unit"] = "COUNT"

            if item.get("scope_type") != "PLAN" and item.get("stage_order") is not None:
                inferred = _stage_for_temporal(constraints.get("temporal"), milestone_rows)
                if inferred is not None:
                    item["stage_order"] = inferred
                elif item.get("scope_type") == "CHECKPOINT":
                    item["stage_order"] = None
                elif item.get("scope_type") == "STAGE":
                    item["scope_type"] = "PLAN"
                    item["stage_order"] = None
            elif item.get("scope_type") in {"STAGE", "WEEK"}:
                inferred = _stage_for_temporal(constraints.get("temporal"), milestone_rows)
                if inferred is not None:
                    item["stage_order"] = inferred


def _positive_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _normalize_hybrid_keys(raw: dict[str, object]) -> None:
    item_key_map: dict[str, str] = {}
    group_key_map: dict[str, str] = {}
    used_item_keys: set[str] = set()
    for collection_name in ("standing_work_items", "work_items"):
        collection = raw.get(collection_name)
        if not isinstance(collection, list):
            continue
        for item in collection:
            if not isinstance(item, dict):
                continue
            original = str(item.get("item_key") or "")
            base_key = _stable_hybrid_key(original, "ITEM", 120)
            normalized = base_key
            suffix = 2
            while normalized in used_item_keys:
                suffix_text = f".{suffix}"
                normalized = f"{base_key[: 120 - len(suffix_text)]}{suffix_text}"
                suffix += 1
            used_item_keys.add(normalized)
            item["item_key"] = normalized
            item_key_map.setdefault(original, normalized)
    groups = raw.get("work_item_groups")
    if isinstance(groups, list):
        for group in groups:
            if not isinstance(group, dict):
                continue
            original = str(group.get("group_key") or "")
            normalized = _stable_hybrid_key(original, "GROUP", 80)
            group["group_key"] = normalized
            group_key_map[original] = normalized
            candidates = group.get("candidate_item_keys")
            if isinstance(candidates, list):
                group["candidate_item_keys"] = [
                    item_key_map.get(str(candidate), _stable_hybrid_key(candidate, "ITEM", 120))
                    for candidate in candidates
                ]
    # Resolve spelling-only aliases through the explicit registry. A dot and
    # an underscore are both legal in keys, so normalizing each string alone
    # does not make THEME.STILL_LIFE and THEME_STILL_LIFE identical.
    group_aliases: dict[str, list[str]] = {}
    for key in group_key_map.values():
        group_aliases.setdefault(re.sub(r"[^A-Z0-9]", "", key), []).append(key)
    declared_memberships: dict[str, list[str]] = {}
    for group in groups or []:
        if isinstance(group, dict):
            for member in group.get("candidate_item_keys") or []:
                declared_memberships.setdefault(str(member), []).append(str(group["group_key"]))
    for collection_name in ("standing_work_items", "work_items"):
        collection = raw.get(collection_name)
        if not isinstance(collection, list):
            continue
        for item in collection:
            if not isinstance(item, dict):
                continue
            constraints = item.get("constraint_set")
            if not isinstance(constraints, dict):
                continue
            selection = constraints.get("selection")
            declared = declared_memberships.get(str(item.get("item_key")), [])
            if len(declared) == 1 and (selection is None or
                    isinstance(selection, dict) and not selection.get("group_key")):
                # The group already explicitly declares this candidate. Fill
                # only the missing reverse reference, never choose a group or
                # overwrite a conflicting declaration.
                selection = {**(selection or {}), "group_key": declared[0]}
                constraints["selection"] = selection
            if isinstance(selection, dict) and selection.get("group_key"):
                original = str(selection["group_key"])
                selection["group_key"] = group_key_map.get(
                    original, _stable_hybrid_key(original, "GROUP", 80)
                )
                if selection["group_key"] not in group_key_map.values():
                    aliases = group_aliases.get(re.sub(r"[^A-Z0-9]", "", str(selection["group_key"])), [])
                    if len(aliases) == 1:
                        selection["group_key"] = aliases[0]
                for group in groups or []:
                    if (
                        not isinstance(group, dict)
                        or group.get("group_key") != selection["group_key"]
                    ):
                        continue
                    candidates = group.get("candidate_item_keys")
                    if isinstance(candidates, list) and item.get("item_key") not in candidates:
                        candidates.append(item["item_key"])
    dependencies = raw.get("work_item_dependencies")
    if isinstance(dependencies, list):
        normalized_dependencies: list[dict[str, object]] = []
        seen_edges: dict[str, dict[str, object]] = {}
        for dependency in dependencies:
            if not isinstance(dependency, dict):
                continue
            for field in ("predecessor_key", "successor_key"):
                original = str(dependency.get(field) or "")
                dependency[field] = item_key_map.get(
                    original, _stable_hybrid_key(original, "ITEM", 120)
                )
            # Endpoints alone do not prove the same dependency: type, lag,
            # condition and unknown fields must survive semantic validation.
            edge = json.dumps({k: v for k, v in dependency.items() if k != "source_refs"},
                              sort_keys=True, ensure_ascii=False)
            prior_edge = seen_edges.get(edge)
            if prior_edge is not None and isinstance(prior_edge.get("source_refs", []), list) and isinstance(dependency.get("source_refs", []), list):
                prior_edge["source_refs"] = list(dict.fromkeys(
                    [*prior_edge.get("source_refs", []), *dependency.get("source_refs", [])]))
                continue
            seen_edges[edge] = dependency
            normalized_dependencies.append(dependency)
        raw["work_item_dependencies"] = normalized_dependencies


def _stable_hybrid_key(value: object, prefix: str, max_length: int) -> str:
    normalized = re.sub(r"[^A-Z0-9._-]+", "_", str(value).strip().upper())
    normalized = re.sub(r"_+", "_", normalized).strip("._-")
    if not normalized or not normalized[0].isalnum():
        normalized = f"{prefix}.{normalized or 'UNNAMED'}"
    if len(normalized) < 3:
        normalized = f"{prefix}.{normalized}"
    return normalized[:max_length].rstrip("._-")


def _stage_for_temporal(temporal: object, milestones: list[dict[str, object]]) -> int | None:
    if not isinstance(temporal, dict):
        return None
    start = _positive_int(temporal.get("start_week"))
    end = _positive_int(temporal.get("end_week")) or start
    if start is None or end is None:
        return None
    matches = [
        _positive_int(milestone.get("stage_order"))
        for milestone in milestones
        if (_positive_int(milestone.get("start_week")) or 10**9) <= start
        and (_positive_int(milestone.get("end_week")) or -1) >= end
    ]
    matches = [item for item in matches if item is not None]
    return matches[0] if len(matches) == 1 else None


def normalize_compiled_outline_capacity(raw: dict[str, object]) -> None:
    """Normalize promise semantics without increasing the user's capacity."""
    segments = raw.get("temporal_segments")
    commitments = raw.get("commitments")
    if not isinstance(segments, list) or not isinstance(commitments, list):
        return
    for commitment in commitments:
        if not isinstance(commitment, dict):
            continue
        source_text = str(commitment.get("source_text", ""))
        requires_separate_day = any(
            phrase in source_text
            for phrase in (
                "单独一天",
                "独立一天",
                "当天只",
                "只在每周日",
                "周日只",
                "周日独立",
                "单独安排一天",
            )
        )
        adds_to_baseline = not requires_separate_day and any(
            phrase in source_text for phrase in ("单独安排", "另安排一次", "单独一次", "另留一次")
        )
        commitment["requires_separate_execution_day"] = requires_separate_day
        commitment["adds_to_daily_baseline"] = adds_to_baseline

    # Task obligations describe demand, never authorization to enlarge supply.
    # In particular, a 150-minute promise cannot turn a confirmed 90-minute
    # day into 210 minutes. Preserve all numeric capacities for validation and
    # negotiation; only normalize the semantic flags above.


def normalize_compiled_plan_days(
    raw: dict[str, object],
    days_key: str,
    dependency_registry: list[dict[str, str]] | None = None,
) -> None:
    """Repair schema bookkeeping without changing the authored work plan."""
    raw_days = raw.get(days_key)
    if not isinstance(raw_days, list):
        return
    canonical = re.compile(r"^M(\d+)-W(\d+)-D(\d+)-T(\d+)$", re.IGNORECASE)
    local_coordinate = re.compile(r"^(?:W(\d+)-)?D(\d+)-T(\d+)$", re.IGNORECASE)
    prior: list[tuple[str, str]] = [
        (str(item.get("title", "")).strip().casefold(), str(item.get("reference", "")).strip())
        for item in (dependency_registry or [])
        if str(item.get("title", "")).strip() and str(item.get("reference", "")).strip()
    ]
    known = [*prior]
    reference_repairs = []
    for candidate_day in raw_days:
        if not isinstance(candidate_day, dict):
            continue
        for index, candidate_task in enumerate(candidate_day.get("tasks", []) or [], 1):
            if isinstance(candidate_task, dict):
                known.append((str(candidate_task.get("title", "")).strip().casefold(),
                    f"M{candidate_day.get('stage_order')}-W{candidate_day.get('week_index')}-"
                    f"D{candidate_day.get('day_index')}-T{candidate_task.get('order', index)}"))
    for raw_day in raw_days:
        if not isinstance(raw_day, dict):
            continue
        tasks = raw_day.get("tasks")
        if not isinstance(tasks, list):
            continue
        estimate_total = sum(
            int(task.get("estimate_minutes", 0)) for task in tasks if isinstance(task, dict)
        )
        if estimate_total:
            raw_day["suggested_minutes"] = max(
                int(raw_day.get("suggested_minutes", 0)), estimate_total
            )
        if not isinstance(raw_day.get("evidence"), list):
            task_evidence = [
                str(item).strip()
                for task in tasks
                if isinstance(task, dict)
                for item in task.get("evidence", [])
                if str(item).strip()
            ]
            raw_day["evidence"] = list(dict.fromkeys(task_evidence))[:6] or [
                str(raw_day.get("primary_goal") or "完成当天可检查成果")
            ]
        stage = int(raw_day.get("stage_order", 0))
        week = int(raw_day.get("week_index", 0))
        day = int(raw_day.get("day_index", 0))
        for fallback_order, task in enumerate(tasks, start=1):
            if not isinstance(task, dict):
                continue
            order = int(task.get("order", fallback_order))
            dependencies = task.get("depends_on")
            if isinstance(dependencies, list):
                normalized_dependencies = []
                for dependency in dependencies:
                    value = str(dependency).strip()
                    canonical_match = canonical.fullmatch(value)
                    if canonical_match:
                        _stage, dependency_week, dependency_day, dependency_order = map(
                            int, canonical_match.groups()
                        )
                        coordinate_suffix = (
                            f"-W{dependency_week}-D{dependency_day}-T{dependency_order}"
                        )
                        registered = {
                            reference
                            for _title, reference in known
                            if reference.upper().endswith(coordinate_suffix)
                        }
                        exact_registered = {reference.upper() for _title, reference in known}
                        normalized_dependencies.append(
                            next(iter(registered)) if value.upper() not in exact_registered and len(registered) == 1 else value.upper()
                        )
                        continue
                    local_match = local_coordinate.fullmatch(value)
                    if local_match:
                        dependency_week, dependency_day, dependency_order = local_match.groups()
                        suffix = (f"-W{int(dependency_week or week)}-D{int(dependency_day)}-"
                                  f"T{int(dependency_order)}")
                        matches = {reference for _title, reference in known
                                   if reference.upper().endswith(suffix)}
                        normalized_dependencies.append(
                            next(iter(matches)) if len(matches) == 1 else value
                        )
                        continue
                    folded = value.casefold()
                    matches = {
                        reference
                        for title, reference in known
                        if title == folded
                    }
                    normalized_dependencies.append(next(iter(matches)) if len(matches) == 1 else value)
                task["depends_on"] = list(dict.fromkeys(normalized_dependencies))
                if task["depends_on"] != dependencies:
                    reference_repairs.append({"task_reference": f"M{stage}-W{week}-D{day}-T{order}",
                        "old_hash": digest(dependencies), "new_hash": digest(task["depends_on"]),
                        "proof": "EXACT_UNIQUE_REFERENCE_OR_COORDINATE", "field": "depends_on"})
            title = str(task.get("title", "")).strip().casefold()
            if title:
                prior.append((title, f"M{stage}-W{week}-D{day}-T{order}"))
    if reference_repairs and isinstance(raw.get("compiler_report", {}), dict):
        raw["compiler_report"] = {**raw.get("compiler_report", {}),
            "reference_bookkeeping": {"policy": "exact-unique-reference-v1",
                "patches": reference_repairs, "model_calls": 0,
                "status": "PATCHED_REQUIRES_FULL_BUSINESS_VALIDATION"}}


class _PlanProviderBase:
    def __init__(
        self,
        settings: Settings,
        *,
        transport: JSONTransport,
        sleeper: Callable[[float], None],
    ) -> None:
        api_key = settings.llm_api_key.get_secret_value().strip()
        if not settings.model_runtime_configured:
            raise ProviderConfigurationError("Solevia 暂时无法开始安排时间，请稍后再试。")
        self.api_key = api_key
        self.base_url = settings.llm_base_url.rstrip("/")
        self.model = settings.llm_model
        self.reasoning_effort = settings.llm_reasoning_effort
        self.compiler_reasoning_effort = settings.plan_compiler_reasoning_effort
        self.timeout = settings.plan_timeout_seconds
        self.deep_timeout = settings.plan_deep_timeout_seconds
        self.max_retries = settings.plan_max_retries
        self.outline_max_retries = settings.plan_outline_max_retries
        self.detail_max_retries = settings.plan_detail_max_retries
        self.outline_max_output_tokens = settings.plan_outline_max_output_tokens
        self.detail_max_output_tokens = settings.plan_max_output_tokens
        self.hybrid_compiler_max_output_tokens = settings.plan_hybrid_compiler_max_output_tokens
        self.output_retry_max_tokens = settings.plan_output_retry_max_tokens
        self._source_feedback_dedup_enabled = settings.context_source_feedback_dedup_enabled
        self._compiler_contract_enabled = settings.plan_compiler_contract_enabled
        self._compiler_field_retry_enabled = settings.plan_compiler_field_retry_enabled
        self._model_trace_policy = trace_policy(settings)
        self.transport = configured_json_transport(settings, transport)
        self.sleeper = sleeper

    def draft_outline(
        self, context: PlanContext, generation_mode: GenerationMode = "STANDARD"
    ) -> PlanOutlineResult:
        return self._generate(
            PlanOutlineResult,
            "relative_plan_outline",
            (
                PLAN_OUTLINE_COMPILER_INSTRUCTIONS
                if context.source_document
                else PLAN_OUTLINE_INSTRUCTIONS
            ),
            _plan_input(
                context,
                source_feedback_enabled=getattr(self, "_source_feedback_dedup_enabled", True),
                compiler_contract_enabled=getattr(self, "_compiler_contract_enabled", False),
                generation_step="OUTLINE",
            ),
            self.outline_max_output_tokens,
            generation_mode,
        )

    def draft_details(
        self,
        context: PlanContext,
        outline: dict[str, object],
        start_week: int,
        end_week: int,
        generation_mode: GenerationMode = "STANDARD",
    ) -> PlanDetailResult:
        return self._generate(
            PlanDetailResult,
            "relative_plan_details",
            (
                PLAN_DETAIL_COMPILER_INSTRUCTIONS
                if context.source_document
                else PLAN_DETAIL_INSTRUCTIONS
            ),
            _plan_input(
                context, source_feedback_enabled=getattr(self, "_source_feedback_dedup_enabled", True),
                compiler_contract_enabled=getattr(self, "_compiler_contract_enabled", False),
                generation_step="DETAILS",
                plan_outline=outline,
                requested_weeks={"start": start_week, "end": end_week},
            ),
            self.detail_max_output_tokens,
            generation_mode,
            [
                {"title": str(item["title"]), "reference": str(item["reference"])}
                for item in outline.get("generated_task_registry", [])
                if isinstance(item, dict) and item.get("title") and item.get("reference")
            ],
        )

    def draft_hybrid_outline(
        self,
        context: PlanContext,
        generation_mode: GenerationMode = "STANDARD",
        checkpoint: Callable[[str], None] | None = None,
    ) -> HybridPlanOutlineResult:
        del checkpoint
        return self._generate(
            HybridPlanOutlineResult,
            "hybrid_plan_outline",
            HYBRID_PLAN_OUTLINE_COMPILER_INSTRUCTIONS,
            _plan_input(
                context, source_feedback_enabled=getattr(self, "_source_feedback_dedup_enabled", True),
                compiler_contract_enabled=getattr(self, "_compiler_contract_enabled", False),
                prompt_version=HYBRID_PLAN_PROMPT_VERSION,
                generation_step="HYBRID_OUTLINE_COMPILER",
            ),
            self.hybrid_compiler_max_output_tokens,
            generation_mode,
        )

    def draft_hybrid_details(
        self,
        context: PlanContext,
        outline: dict[str, object],
        start_week: int,
        end_week: int,
        generation_mode: GenerationMode = "STANDARD",
        checkpoint: Callable[[str], None] | None = None,
    ) -> HybridPlanDetailResult:
        del checkpoint
        return self._generate(
            HybridPlanDetailResult,
            "hybrid_plan_details",
            HYBRID_PLAN_DETAIL_COMPILER_INSTRUCTIONS,
            _plan_input(
                context, source_feedback_enabled=getattr(self, "_source_feedback_dedup_enabled", True),
                compiler_contract_enabled=getattr(self, "_compiler_contract_enabled", False),
                prompt_version=HYBRID_PLAN_PROMPT_VERSION,
                generation_step="HYBRID_DETAILS_COMPILER",
                plan_outline=outline,
                requested_weeks={"start": start_week, "end": end_week},
            ),
            self.hybrid_compiler_max_output_tokens,
            generation_mode,
            [
                {"title": str(item["title"]), "reference": str(item["reference"])}
                for item in outline.get("generated_task_registry", [])
                if isinstance(item, dict) and item.get("title") and item.get("reference")
            ],
        )

    def _generate(
        self,
        schema_model: type[PlanResult],
        schema_name: str,
        instructions: str,
        input_text: str,
        max_output_tokens: int,
        generation_mode: GenerationMode,
        dependency_registry: list[dict[str, str]] | None = None,
    ) -> PlanResult:
        raise NotImplementedError


class DeepSeekPlanProvider(_PlanProviderBase):
    supported_generation_modes: tuple[GenerationMode, ...] = ("STANDARD", "DEEP")

    def __init__(
        self,
        settings: Settings,
        *,
        transport: JSONTransport = _urllib_json_transport,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        super().__init__(settings, transport=transport, sleeper=sleeper)
        self.endpoint = f"{self.base_url}/responses"
        self.headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    @model_node('plan_ai.DeepSeekPlanProvider._generate', operation_arg='schema_model')
    def _generate(
        self,
        schema_model: type[PlanResult],
        schema_name: str,
        instructions: str,
        input_text: str,
        max_output_tokens: int,
        generation_mode: GenerationMode,
        dependency_registry: list[dict[str, str]] | None = None,
    ) -> PlanResult:
        is_hybrid = schema_model in {HybridPlanOutlineResult, HybridPlanDetailResult}
        instructions = compiler_instructions(
            instructions, schema_model, enabled=self._compiler_contract_enabled,
        )
        standard_effort = self.compiler_reasoning_effort if is_hybrid else "none"
        payload: dict[str, object] = {
            "model": self.model,
            "instructions": instructions,
            "input": input_text,
            "reasoning": {"effort": standard_effort if generation_mode == "STANDARD" else "high"},
            "max_output_tokens": max_output_tokens,
            "metadata": {"prompt_version": PLAN_PROMPT_VERSION},
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": schema_name,
                    "schema": schema_model.model_json_schema(),
                }
            },
        }
        last_error: Exception | None = None
        max_retries = (
            self.outline_max_retries
            if issubclass(schema_model, PlanOutlineResult)
            else self.detail_max_retries
        )
        for attempt in range(max_retries + 1):
            try:
                timeout = self.deep_timeout if generation_mode == "DEEP" else self.timeout
                response = observed_model_call(self.transport, self.endpoint, self.headers, payload, timeout)
                return _validate_plan_response(
                    schema_model,
                    self._extract_text(response),
                    dependency_registry,
                )
            except HTTPError as error:
                mark_attempt_failure(error)
                if error.code not in {408, 409, 429, 500, 502, 503, 504}:
                    raise ProviderUnavailableError(
                        "Solevia 暂时无法完成这次时间安排，请稍后再试。",
                        diagnostic=f"HTTP {error.code}",
                        code="MODEL_REQUEST_REJECTED",
                    ) from None
                last_error = error
            except (URLError, TimeoutError, OSError, ValueError, ValidationError) as error:
                mark_attempt_failure(error)
                last_error = error
            # Schema corrections have one owner when author/compiler orchestration
            # is active. Otherwise inner and outer repair loops multiply calls.
            # Transport retries and explicit output truncation retain their policy.
            if (_semantic_retry_owner.get() and isinstance(last_error, (ValueError, ValidationError))
                    and not isinstance(last_error, _PlanOutputTruncated)):
                break
            if (
                isinstance(last_error, HTTPError)
                and last_error.code == 504
                and attempt >= min(max_retries, 1)
            ):
                break
            if attempt < max_retries:
                if isinstance(last_error, _PlanOutputTruncated):
                    payload = _expand_truncated_plan_output(
                        payload, self.output_retry_max_tokens, final_retry=attempt == max_retries - 1,
                    )
                elif isinstance(last_error, (ValueError, ValidationError)):
                    payload = dict(payload)
                    payload["instructions"] = _replace_retry_feedback(
                        str(payload["instructions"]),
                        "上一次输出未通过 Schema 校验，请修正："
                        f"{_safe_validation_feedback(last_error)}",
                    )
                self.sleeper(0.5 * (2**attempt))
        raise _plan_generation_error(last_error) from None

    @staticmethod
    def _extract_text(response: dict[str, object]) -> str:
        details = response.get("incomplete_details")
        if (response.get("status") == "incomplete" and isinstance(details, dict)
                and details.get("reason") == "max_output_tokens"):
            raise _PlanOutputTruncated("Provider output truncated: max_output_tokens")
        if response.get("status") != "completed" or not isinstance(response.get("output"), list):
            raise ValueError(
                f"Provider response status={response.get('status')}; "
                f"details={response.get('incomplete_details') or response.get('error')}"
            )
        for item in response["output"]:  # type: ignore[index]
            if not isinstance(item, dict) or item.get("type") != "message":
                continue
            content = item.get("content")
            if not isinstance(content, list):
                continue
            for part in content:
                if isinstance(part, dict) and part.get("type") == "output_text":
                    text = part.get("text")
                    if isinstance(text, str):
                        return text
        raise ValueError("Provider response did not contain output_text")


class OpenAICompatiblePlanProvider(_PlanProviderBase):
    supported_generation_modes: tuple[GenerationMode, ...] = ("STANDARD",)

    def __init__(
        self,
        settings: Settings,
        *,
        transport: JSONTransport = _urllib_json_transport,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        super().__init__(settings, transport=transport, sleeper=sleeper)
        self.endpoint = f"{self.base_url}/chat/completions"
        self.headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "User-Agent": "Elastic-Planner/0.1",
        }

    @model_node('plan_ai.OpenAICompatiblePlanProvider._generate', operation_arg='schema_model')
    def _generate(
        self,
        schema_model: type[PlanResult],
        _schema_name: str,
        instructions: str,
        input_text: str,
        max_output_tokens: int,
        generation_mode: GenerationMode,
        dependency_registry: list[dict[str, str]] | None = None,
    ) -> PlanResult:
        instructions = compiler_instructions(
            instructions, schema_model, enabled=self._compiler_contract_enabled,
        )
        schema = json.dumps(schema_model.model_json_schema(), ensure_ascii=False)
        payload: dict[str, object] = {
            "model": self.model,
            "messages": [
                {
                    "role": "system",
                    "content": f"{instructions}\n只输出 JSON。JSON Schema：{schema}",
                },
                {"role": "user", "content": input_text},
            ],
            "response_format": {"type": "json_object"},
            "max_tokens": max_output_tokens,
            "stream": False,
        }
        last_error: Exception | None = None
        max_retries = (
            self.outline_max_retries
            if issubclass(schema_model, PlanOutlineResult)
            else self.detail_max_retries
        )
        for attempt in range(max_retries + 1):
            try:
                response = observed_model_call(self.transport, self.endpoint, self.headers, payload, self.timeout)
                content = self._extract_content(response)
                return _validate_plan_response(schema_model, content, dependency_registry)
            except HTTPError as error:
                mark_attempt_failure(error)
                if error.code not in {408, 409, 429, 500, 502, 503, 504}:
                    raise ProviderUnavailableError(
                        "Solevia 暂时无法完成这次时间安排，请稍后再试。",
                        diagnostic=f"HTTP {error.code}",
                        code="MODEL_REQUEST_REJECTED",
                    ) from None
                last_error = error
            except (URLError, TimeoutError, OSError, ValueError, ValidationError) as error:
                mark_attempt_failure(error)
                last_error = error
            # Schema corrections have one owner when author/compiler orchestration
            # is active. Otherwise inner and outer repair loops multiply calls.
            # Transport retries and explicit output truncation retain their policy.
            if (_semantic_retry_owner.get() and isinstance(last_error, (ValueError, ValidationError))
                    and not isinstance(last_error, _PlanOutputTruncated)):
                break
            if (
                isinstance(last_error, HTTPError)
                and last_error.code == 504
                and attempt >= min(max_retries, 1)
            ):
                break
            if attempt < max_retries:
                if isinstance(last_error, _PlanOutputTruncated):
                    payload = _expand_truncated_plan_output(
                        payload, self.output_retry_max_tokens, final_retry=attempt == max_retries - 1,
                    )
                elif isinstance(last_error, (ValueError, ValidationError)):
                    messages = [
                        item
                        for item in list(payload["messages"])  # type: ignore[arg-type]
                        if not (
                            isinstance(item, dict)
                            and item.get("role") == "system"
                            and "[RETRY_FEEDBACK]" in str(item.get("content", ""))
                        )
                    ]
                    messages.append({
                        "role": "system",
                        "content": _replace_retry_feedback(
                            "",
                            "上一次输出未通过 Schema 校验，请重新输出完整 JSON 并修正："
                            f"{_safe_validation_feedback(last_error)}",
                        ),
                    })
                    payload = {**payload, "messages": messages}
                self.sleeper(0.5 * (2**attempt))
        raise _plan_generation_error(last_error) from None

    @staticmethod
    def _extract_content(response: dict[str, object]) -> str:
        choices = response.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            raise ValueError("Provider response did not contain choices")
        if choices[0].get("finish_reason") in {"length", "max_tokens"}:
            raise _PlanOutputTruncated("Provider output truncated: finish_reason=length")
        message = choices[0].get("message")
        if not isinstance(message, dict) or not isinstance(message.get("content"), str):
            raise ValueError("Provider response did not contain message content")
        return message["content"]


class NaturalPlanOutlineAuthor:
    """Generate the planning argument before any outline schema is imposed."""

    def __init__(
        self,
        settings: Settings,
        *,
        transport: JSONTransport = _urllib_json_transport,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        api_key = settings.llm_api_key.get_secret_value().strip()
        if not settings.model_runtime_configured:
            raise ProviderConfigurationError("Solevia 暂时无法开始安排时间，请稍后再试。")
        self.provider = settings.llm_provider
        self.endpoint = (
            f"{settings.llm_base_url.rstrip('/')}/responses"
            if self.provider == "deepseek"
            else f"{settings.llm_base_url.rstrip('/')}/chat/completions"
        )
        self.headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "User-Agent": "Elastic-Planner/0.1",
        }
        self.model = settings.llm_model
        self.reasoning_effort = settings.llm_reasoning_effort
        self.timeout = settings.plan_timeout_seconds
        self.deep_timeout = settings.plan_deep_timeout_seconds
        self.max_retries = settings.plan_outline_max_retries
        self.max_output_tokens = settings.plan_outline_max_output_tokens
        self.hybrid_max_output_tokens = settings.plan_hybrid_author_max_output_tokens
        self.output_retry_max_tokens = settings.plan_output_retry_max_tokens
        self._source_feedback_dedup_enabled = settings.context_source_feedback_dedup_enabled
        self._compiler_contract_enabled = settings.plan_compiler_contract_enabled
        self._model_trace_policy = trace_policy(settings)
        self.transport = configured_json_transport(settings, transport)
        self.sleeper = sleeper

    def draft_document(
        self, context: PlanContext, generation_mode: GenerationMode = "STANDARD"
    ) -> str:
        return self._draft_document(
            context,
            generation_mode,
            instructions=PLAN_OUTLINE_AUTHOR_INSTRUCTIONS,
            generation_step="OUTLINE_AUTHOR",
        )

    def draft_hybrid_document(
        self, context: PlanContext, generation_mode: GenerationMode = "STANDARD"
    ) -> str:
        return self._draft_document(
            context,
            generation_mode,
            instructions=HYBRID_PLAN_OUTLINE_AUTHOR_INSTRUCTIONS,
            generation_step="HYBRID_OUTLINE_AUTHOR",
            prompt_version=HYBRID_PLAN_PROMPT_VERSION,
            max_output_tokens=self.hybrid_max_output_tokens,
        )

    @model_node('plan_ai.NaturalPlanOutlineAuthor._draft_document', operation_arg='generation_step')
    def _draft_document(
        self,
        context: PlanContext,
        generation_mode: GenerationMode,
        *,
        instructions: str,
        generation_step: str,
        prompt_version: str = PLAN_PROMPT_VERSION,
        max_output_tokens: int | None = None,
        **extra: object,
    ) -> str:
        if self._compiler_contract_enabled and generation_step in {"DETAILS_AUTHOR", "HYBRID_DETAILS_AUTHOR"}:
            instructions += DETAIL_AUTHOR_CAPACITY_CONTRACT
        source_context = PlanContext(
            requirements=context.requirements,
            route=context.route,
            planning_context=context.planning_context,
        )
        if self.provider == "deepseek":
            payload: dict[str, object] = {
                "model": self.model,
                "instructions": instructions,
                "input": _plan_input(
                    source_context, source_feedback_enabled=getattr(self, "_source_feedback_dedup_enabled", True),
                compiler_contract_enabled=getattr(self, "_compiler_contract_enabled", False),
                    prompt_version=prompt_version,
                    generation_step=generation_step,
                    **extra,
                ),
                "reasoning": {"effort": "none" if generation_mode == "STANDARD" else "high"},
                "max_output_tokens": max_output_tokens or self.max_output_tokens,
                "metadata": {"prompt_version": prompt_version, "stage": "AUTHOR"},
            }
        else:
            payload = {
                "model": self.model,
                "messages": [
                    {"role": "system", "content": instructions},
                    {
                        "role": "user",
                        "content": _plan_input(
                            source_context, source_feedback_enabled=getattr(self, "_source_feedback_dedup_enabled", True),
                compiler_contract_enabled=getattr(self, "_compiler_contract_enabled", False),
                            prompt_version=prompt_version,
                            generation_step=generation_step,
                            **extra,
                        ),
                    },
                ],
                "max_tokens": max_output_tokens or self.max_output_tokens,
                "stream": False,
            }
        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                timeout = self.deep_timeout if generation_mode == "DEEP" else self.timeout
                response = observed_model_call(self.transport, self.endpoint, self.headers, payload, timeout)
                content = (
                    DeepSeekPlanProvider._extract_text(response)
                    if self.provider == "deepseek"
                    else OpenAICompatiblePlanProvider._extract_content(response)
                )
                if not content.strip():
                    raise ValueError("Plan outline author returned an empty document")
                return content.strip()
            except HTTPError as error:
                mark_attempt_failure(error)
                if error.code not in {408, 409, 429, 500, 502, 503, 504}:
                    raise ProviderUnavailableError(
                        "Solevia 暂时无法完成这次总体安排，请稍后再试。",
                        diagnostic=f"HTTP {error.code}",
                        code="MODEL_REQUEST_REJECTED",
                    ) from None
                last_error = error
            except (URLError, TimeoutError, OSError, ValueError) as error:
                mark_attempt_failure(error)
                last_error = error
            if attempt < self.max_retries:
                if isinstance(last_error, _PlanOutputTruncated):
                    payload = _expand_truncated_plan_output(
                        payload, self.output_retry_max_tokens,
                        final_retry=attempt == self.max_retries - 1,
                    )
                self.sleeper(0.5 * (2**attempt))
        raise _plan_generation_error(last_error) from None


class NaturalPlanDetailAuthor(NaturalPlanOutlineAuthor):
    """Write one requested week range before its task schema is imposed."""

    def __init__(
        self,
        settings: Settings,
        *,
        transport: JSONTransport = _urllib_json_transport,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        super().__init__(settings, transport=transport, sleeper=sleeper)
        self.max_retries = settings.plan_detail_max_retries
        self.max_output_tokens = settings.plan_max_output_tokens

    def draft_document(
        self,
        context: PlanContext,
        outline: dict[str, object],
        start_week: int,
        end_week: int,
        generation_mode: GenerationMode = "STANDARD",
    ) -> str:
        return self._draft_document(
            context,
            generation_mode,
            instructions=PLAN_DETAIL_AUTHOR_INSTRUCTIONS,
            generation_step="DETAILS_AUTHOR",
            plan_outline=outline,
            requested_weeks={"start": start_week, "end": end_week},
        )

    def draft_hybrid_document(
        self,
        context: PlanContext,
        outline: dict[str, object],
        start_week: int,
        end_week: int,
        generation_mode: GenerationMode = "STANDARD",
    ) -> str:
        return self._draft_document(
            context,
            generation_mode,
            instructions=HYBRID_PLAN_DETAIL_AUTHOR_INSTRUCTIONS,
            generation_step="HYBRID_DETAILS_AUTHOR",
            prompt_version=HYBRID_PLAN_PROMPT_VERSION,
            plan_outline=outline,
            requested_weeks={"start": start_week, "end": end_week},
        )


class SemanticPlanProvider:
    requires_sequential_details = True
    semantic_validation_retries = 2

    def __init__(
        self,
        outline_author: NaturalPlanOutlineAuthor,
        detail_author: NaturalPlanDetailAuthor,
        compiler: PlanProvider,
    ) -> None:
        self.outline_author = outline_author
        self.detail_author = detail_author
        self.compiler = compiler

    @property
    def supported_generation_modes(self) -> tuple[GenerationMode, ...]:
        return self.compiler.supported_generation_modes

    def draft_outline(
        self, context: PlanContext, generation_mode: GenerationMode = "STANDARD"
    ) -> PlanOutlineResult:
        attempt_context = context
        document = context.source_document
        for attempt in range(self.semantic_validation_retries + 1):
            document = document or self.outline_author.draft_document(attempt_context, generation_mode)
            try:
                compiled = self._compile(self.compiler.draft_outline,
                    PlanContext(
                        requirements=context.requirements,
                        route=context.route,
                        planning_context=attempt_context.planning_context,
                        source_document=document,
                    ),
                    generation_mode,
                )
                return compiled.model_copy(
                    update={
                        "source_document": document,
                        "compiler_report": {
                            "status": "VALIDATED",
                            "source_length": len(document),
                            "temporal_segment_count": len(compiled.temporal_segments),
                            "commitment_count": len(compiled.commitments),
                            "unresolved_questions": list(compiled.open_questions),
                            "prompt_version": PLAN_PROMPT_VERSION,
                        },
                    }
                )
            except ProviderUnavailableError as error:
                if (
                    error.code != "INVALID_MODEL_RESPONSE"
                    or attempt >= self.semantic_validation_retries
                ):
                    raise
                attempt_context = self._retry_context(
                    context, "OUTLINE", attempt + 1, error, document
                )
                if attempt > 0 and not self._keep_author_for_field_error(error):
                    document = None
        raise RuntimeError("Unreachable semantic outline generation state")

    def draft_details(
        self,
        context: PlanContext,
        outline: dict[str, object],
        start_week: int,
        end_week: int,
        generation_mode: GenerationMode = "STANDARD",
    ) -> PlanDetailResult:
        attempt_context = context
        document = context.source_document
        for attempt in range(self.semantic_validation_retries + 1):
            document = document or self.detail_author.draft_document(
                attempt_context, outline, start_week, end_week, generation_mode
            )
            try:
                compiled = self._compile(self.compiler.draft_details,
                    PlanContext(
                        requirements=context.requirements,
                        route=context.route,
                        planning_context=attempt_context.planning_context,
                        source_document=document,
                    ),
                    outline,
                    start_week,
                    end_week,
                    generation_mode,
                )
                return compiled.model_copy(
                    update={
                        "source_document": document,
                        "compiler_report": {
                            "status": "VALIDATED",
                            "source_length": len(document),
                            "week_range": {"start": start_week, "end": end_week},
                            "day_count": len(compiled.days),
                            "task_count": sum(len(day.tasks) for day in compiled.days),
                            "prompt_version": PLAN_PROMPT_VERSION,
                        },
                    }
                )
            except ProviderUnavailableError as error:
                if (
                    error.code != "INVALID_MODEL_RESPONSE"
                    or attempt >= self.semantic_validation_retries
                ):
                    raise
                attempt_context = self._retry_context(
                    context, "DETAILS", attempt + 1, error, document
                )
                if attempt > 0 and not self._keep_author_for_field_error(error):
                    document = None
        raise RuntimeError("Unreachable semantic detail generation state")

    def draft_hybrid_outline(
        self,
        context: PlanContext,
        generation_mode: GenerationMode = "STANDARD",
        checkpoint: Callable[[str], None] | None = None,
    ) -> HybridPlanOutlineResult:
        attempt_context = context
        document = context.source_document
        for attempt in range(self.semantic_validation_retries + 1):
            if not document:
                document = self.outline_author.draft_hybrid_document(attempt_context, generation_mode)
                if checkpoint:
                    checkpoint(document)
            try:
                compiled = self._compile(self.compiler.draft_hybrid_outline,
                    PlanContext(
                        requirements=context.requirements,
                        route=context.route,
                        planning_context=attempt_context.planning_context,
                        source_document=document,
                    ),
                    generation_mode,
                )
                return compiled.model_copy(
                    update={
                        "source_document": document,
                        "compiler_report": {
                            **compiled.compiler_report,
                            "status": "VALIDATED",
                            "source_length": len(document),
                            "semantic_repair_rounds": attempt,
                            "work_item_count": len(compiled.standing_work_items),
                            "group_count": len(compiled.work_item_groups),
                            "dependency_count": len(compiled.work_item_dependencies),
                            "prompt_version": HYBRID_PLAN_PROMPT_VERSION,
                        },
                    }
                )
            except ProviderUnavailableError as error:
                if (
                    error.code != "INVALID_MODEL_RESPONSE"
                    or attempt >= self.semantic_validation_retries
                ):
                    raise
                attempt_context = self._retry_context(
                    context, "HYBRID_OUTLINE", attempt + 1, error, document
                )
                if attempt > 0 and not self._keep_author_for_field_error(error):
                    document = None
        raise RuntimeError("Unreachable Hybrid outline generation state")

    def draft_hybrid_details(
        self,
        context: PlanContext,
        outline: dict[str, object],
        start_week: int,
        end_week: int,
        generation_mode: GenerationMode = "STANDARD",
        checkpoint: Callable[[str], None] | None = None,
    ) -> HybridPlanDetailResult:
        attempt_context = context
        document = context.source_document
        for attempt in range(self.semantic_validation_retries + 1):
            if not document:
                document = self.detail_author.draft_hybrid_document(
                    attempt_context, outline, start_week, end_week, generation_mode
                )
                if checkpoint:
                    checkpoint(document)
            try:
                compiled = self._compile(self.compiler.draft_hybrid_details,
                    PlanContext(
                        requirements=context.requirements,
                        route=context.route,
                        planning_context=attempt_context.planning_context,
                        source_document=document,
                    ),
                    outline,
                    start_week,
                    end_week,
                    generation_mode,
                )
                return compiled.model_copy(
                    update={
                        "source_document": document,
                        "compiler_report": {
                            **compiled.compiler_report,
                            "status": "VALIDATED",
                            "source_length": len(document),
                            "semantic_repair_rounds": attempt,
                            "week_range": {"start": start_week, "end": end_week},
                            "work_item_count": len(compiled.work_items),
                            "group_count": len(compiled.work_item_groups),
                            "dependency_count": len(compiled.work_item_dependencies),
                            "prompt_version": HYBRID_PLAN_PROMPT_VERSION,
                        },
                    }
                )
            except ProviderUnavailableError as error:
                if (
                    error.code != "INVALID_MODEL_RESPONSE"
                    or attempt >= self.semantic_validation_retries
                ):
                    raise
                attempt_context = self._retry_context(
                    context, "HYBRID_DETAILS", attempt + 1, error, document
                )
                if attempt > 0 and not self._keep_author_for_field_error(error):
                    document = None
        raise RuntimeError("Unreachable Hybrid detail generation state")

    @staticmethod
    def _compile(method, *args):
        token = _semantic_retry_owner.set(True)
        try:
            return method(*args)
        finally:
            _semantic_retry_owner.reset(token)

    def _keep_author_for_field_error(self, error: ProviderUnavailableError) -> bool:
        return bool(getattr(self.compiler, "_compiler_field_retry_enabled", True)) and isinstance(
            error, CompilerRepresentationError,
        )

    def _retry_context(
        self,
        context: PlanContext,
        phase: str,
        attempt: int,
        error: ProviderUnavailableError,
        previous_source_document: str,
    ) -> PlanContext:
        planning_context = dict(context.planning_context or {})
        planning_context["generation_validation_feedback"] = {
            "phase": phase,
            "attempt": attempt,
            "code": error.code,
            "diagnostic": error.diagnostic,
            "message": str(error),
            "previous_source_document": previous_source_document,
        }
        if self._keep_author_for_field_error(error):
            planning_context["generation_validation_feedback"].update({
                "failure_class": "COMPILER_FIELD_DOMAIN",
                "compile_only_repair": True,
                "repair_policy": "只修正诊断中的字段领域；保留完整原稿、行动、估时、容量、窗口与依赖。不得猜造或删除前置关系。",
            })
        return PlanContext(
            requirements=context.requirements,
            route=context.route,
            planning_context=planning_context,
        )


class FakePlanProvider:
    supported_generation_modes: tuple[GenerationMode, ...] = ("STANDARD", "DEEP")

    def draft_outline(
        self, context: PlanContext, generation_mode: GenerationMode = "STANDARD"
    ) -> PlanOutlineResult:
        stages = context.route.get("stages")
        if not isinstance(stages, list) or not stages:
            raise ValueError("Confirmed route must contain stages")
        milestones = []
        for index, raw_stage in enumerate(stages, start=1):
            stage = raw_stage if isinstance(raw_stage, dict) else {}
            milestones.append(
                PlanMilestoneDraft(
                    stage_order=index,
                    title=str(stage.get("title") or f"阶段 {index}"),
                    purpose=str(stage.get("purpose") or "完成这个阶段的核心成果。"),
                    start_week=index,
                    end_week=index,
                    outputs=[str(item) for item in stage.get("outputs", ["阶段成果"])],
                )
            )
        outcome_based = context.route.get("schedule_type") == "OUTCOME"
        execution_days = 1 if outcome_based else 2
        daily_minutes = 120 if outcome_based else 90
        return PlanOutlineResult(
            assistant_message=(
                "成果窗口已经整理完成，接下来会细化每个窗口的目标与验收。"
                if outcome_based
                else "总体周分配已经完成，接下来会自动细化每天的任务。"
            ),
            summary=(
                f"建议用 {len(stages)} 个成果窗口推进，每个窗口设置一个检查点。"
                if outcome_based
                else f"建议用 {len(stages)} 个相对周推进，每周安排 2 个执行日。"
            ),
            total_weeks=len(stages),
            weekly_execution_days=execution_days,
            weekly_minutes=execution_days * daily_minutes,
            milestones=milestones,
            temporal_segments=[
                PlanTemporalSegmentDraft(
                    label="默认推进期",
                    start_week=1,
                    end_week=len(stages),
                    date_scope="从实际开始日期起的全部计划周期",
                    capacity_summary=(
                        "每个窗口设置一个成果检查点，窗口内自行选择行动时机。"
                        if outcome_based
                        else "每周安排 2 个执行日，每个执行日建议 90 分钟。"
                    ),
                    weekly_execution_days=execution_days,
                    suggested_daily_minutes=daily_minutes,
                    frequency_rules=(
                        ["以窗口成果是否达成为准，不锁定具体行动日期。"]
                        if outcome_based
                        else ["每周完成两个可检查的小成果。"]
                    ),
                    rest_rules=["其余日期保留为休息或机动时间。"],
                    holiday_rules=[],
                )
            ],
            commitments=[]
            if outcome_based
            else [
                PlanCommitmentDraft(
                    label="每周形成阶段成果",
                    start_week=1,
                    end_week=len(stages),
                    cadence="PER_WEEK",
                    minimum_occurrences=1,
                    maximum_occurrences=None,
                    match_terms=["成果", "检查"],
                    source_text="每周完成至少一个可以检查的小成果。",
                )
            ],
            buffer_strategy=["每周未排满的容量用于处理延期或难度偏差。"],
            assumptions=["当前为相对计划，开始时再映射到具体日期。"],
            open_questions=[],
            source_document=(
                "# 总体策略\n按成果窗口逐步推进。\n\n"
                "# 时间策略\n窗口内灵活行动，在检查点按可观察成果验收。"
                if outcome_based
                else "# 总体节奏\n按三个阶段逐步推进。\n\n"
                "# 时间策略\n整个计划每周安排两个执行日，其余时间作为机动。"
            ),
            compiler_report={"status": "VALIDATED", "provider": "FAKE"},
        )

    def draft_details(
        self,
        context: PlanContext,
        outline: dict[str, object],
        start_week: int,
        end_week: int,
        generation_mode: GenerationMode = "STANDARD",
    ) -> PlanDetailResult:
        milestones = cast(list[dict[str, object]], outline["milestones"])
        days = []
        for week_index in range(start_week, end_week + 1):
            milestone = next(
                item
                for item in milestones
                if int(item["start_week"]) <= week_index <= int(item["end_week"])
            )
            stage_order = int(milestone["stage_order"])
            title = str(milestone["title"])
            outputs = [str(item) for item in cast(list[object], milestone["outputs"])]
            day_indexes = (1,) if context.route.get("schedule_type") == "OUTCOME" else (1, 2)
            for day_index in day_indexes:
                days.append(
                    PlanDayDraft(
                        stage_order=stage_order,
                        week_index=week_index,
                        day_index=day_index,
                        primary_goal=f"围绕“{title}”完成一个可以检查的小成果。",
                        suggested_minutes=120 if len(day_indexes) == 1 else 90,
                        tasks=[
                            PlanTaskDraft(
                                order=1,
                                title="形成阶段成果" if day_index == 1 else "检查并修正成果",
                                description=f"按照阶段目标推进“{title}”，并留下可检查结果。",
                                estimate_minutes=90 if len(day_indexes) == 1 else 60,
                                priority=1,
                                task_kind="BUILD" if day_index == 1 else "REVIEW",
                                is_required=True,
                                evidence=outputs[:2],
                                depends_on=[],
                            )
                        ],
                        evidence=outputs[:3],
                        do_not=["不要临时扩大已经确认的路线范围。"],
                    )
                )
        return PlanDetailResult(
            days=days,
            source_document=f"# W{start_week}-W{end_week}\n按大纲生成可执行任务。",
            compiler_report={"status": "VALIDATED", "provider": "FAKE"},
        )

    def draft_hybrid_outline(
        self,
        context: PlanContext,
        generation_mode: GenerationMode = "STANDARD",
        checkpoint: Callable[[str], None] | None = None,
    ) -> HybridPlanOutlineResult:
        legacy = self.draft_outline(context, generation_mode)
        document = context.source_document or (
            "# Hybrid 总体设计\n保留阶段边界，具体义务以工作项表达。"
        )
        if checkpoint and not context.source_document:
            checkpoint(document)
        standing: list[PlanWorkItemDraft] = []
        if context.route.get("schedule_type") != "OUTCOME":
            standing.append(
                PlanWorkItemDraft(
                    item_key="PLAN.WEEKLY.REVIEW",
                    scope_type=WorkItemScope.PLAN,
                    title="每周回看推进效果",
                    description="每周结合完成情况和实际感受，决定下周是否调整节奏。",
                    task_kind="REVIEW",
                    estimate_minutes_per_occurrence=15,
                    priority=3,
                    constraint_set=WorkItemConstraintSet(
                        binding_mode=BindingMode.CADENCE,
                        fulfillment_mode=FulfillmentMode.COUNT,
                        recurrence=RecurrenceConstraint(period="WEEK", target_count=1),
                        completion=CompletionConstraint(target_value=1),
                    ),
                    commitment_refs=["每周形成阶段成果"],
                    evidence=["本周推进结论"],
                    source_refs=["confirmed-route:schedule-type"],
                )
            )
        return HybridPlanOutlineResult.model_validate(
            {
                **legacy.model_dump(mode="json"),
                "source_document": document,
                "standing_work_items": [item.model_dump(mode="json") for item in standing],
                "work_item_groups": [],
                "work_item_dependencies": [],
                "compiler_report": {
                    "status": "VALIDATED",
                    "provider": "FAKE",
                    "prompt_version": HYBRID_PLAN_PROMPT_VERSION,
                },
            }
        )

    def draft_hybrid_details(
        self,
        context: PlanContext,
        outline: dict[str, object],
        start_week: int,
        end_week: int,
        generation_mode: GenerationMode = "STANDARD",
        checkpoint: Callable[[str], None] | None = None,
    ) -> HybridPlanDetailResult:
        del generation_mode
        milestones = cast(list[dict[str, object]], outline["milestones"])
        document = context.source_document or (
            f"# W{start_week}-W{end_week}\n按阶段目标编排弹性工作项。"
        )
        if checkpoint and not context.source_document:
            checkpoint(document)
        work_items: list[PlanWorkItemDraft] = []
        days: list[PlanDayDraft] = []
        outcome_based = context.route.get("schedule_type") == "OUTCOME"
        for week_index in range(start_week, end_week + 1):
            milestone = next(
                item
                for item in milestones
                if int(item["start_week"]) <= week_index <= int(item["end_week"])
            )
            stage_order = int(milestone["stage_order"])
            outputs = [str(item) for item in cast(list[object], milestone["outputs"])]
            work_items.append(
                PlanWorkItemDraft(
                    item_key=f"M{stage_order}.W{week_index:02d}.PRIMARY",
                    scope_type=(WorkItemScope.CHECKPOINT if outcome_based else WorkItemScope.WEEK),
                    stage_order=stage_order,
                    title=(
                        f"检查“{milestone['title']}”的阶段成果"
                        if outcome_based
                        else f"推进“{milestone['title']}”的本周成果"
                    ),
                    description=("在本周窗口内选择合适时机推进，并以可观察结果判断是否完成。"),
                    task_kind="REVIEW" if outcome_based else "BUILD",
                    estimate_minutes_per_occurrence=90,
                    priority=1,
                    constraint_set=WorkItemConstraintSet(
                        binding_mode=BindingMode.WINDOW,
                        fulfillment_mode=(
                            FulfillmentMode.EVIDENCE if outcome_based else FulfillmentMode.ONCE
                        ),
                        temporal=TemporalConstraint(
                            start_week=week_index,
                            end_week=week_index,
                        ),
                        completion=CompletionConstraint(
                            target_value=1,
                            target_unit=(
                                TargetUnit.EVIDENCE if outcome_based else TargetUnit.COUNT
                            ),
                            evidence_required=outcome_based,
                        ),
                    ),
                    commitment_refs=([] if outcome_based else ["每周形成阶段成果"]),
                    evidence=outputs[:3],
                    source_refs=[f"route-stage:{stage_order}"],
                )
            )
            day_count = 1 if outcome_based else min(int(outline.get("weekly_execution_days", 2)), 3)
            progression = (
                ("明确本周切入点", "完成最小准备并记录当前基线。", "PREPARE"),
                ("完成核心练习", "围绕本周目标完成一次有明确产出的核心练习。", "PRACTICE"),
                ("检查成果并修正", "对照阶段产出检查结果，记录问题并完成必要修正。", "REVIEW"),
            )
            if outcome_based:
                progression = (
                    (
                        "完成成果检查",
                        "汇总窗口内的行动，用可观察结果判断是否达到阶段目标。",
                        "REVIEW",
                    ),
                )
            for day_index in range(1, day_count + 1):
                task_title, task_description, task_kind = progression[day_index - 1]
                estimate = max(30, min(90, int(outline.get("weekly_minutes", 180)) // day_count))
                days.append(
                    PlanDayDraft(
                        stage_order=stage_order,
                        week_index=week_index,
                        day_index=day_index,
                        primary_goal=f"{task_title}：{milestone['title']}",
                        suggested_minutes=estimate,
                        tasks=[
                            PlanTaskDraft(
                                order=1,
                                title=task_title,
                                work_item_keys=[f"M{stage_order}.W{week_index:02d}.PRIMARY",
                                    *(["PLAN.WEEKLY.REVIEW"] if not outcome_based and day_index == day_count else [])],
                                occurrence_key=f"W{week_index}-D{day_index}",
                                description=f"{task_description} 当前阶段：{milestone['title']}。",
                                estimate_minutes=estimate,
                                priority=1,
                                task_kind=task_kind,
                                is_required=True,
                                evidence=outputs[:2],
                                depends_on=(
                                    [f"M{stage_order}-W{week_index}-D{day_index - 1}-T1"]
                                    if day_index > 1
                                    else []
                                ),
                                satisfies_commitments=(
                                    [] if outcome_based else ["每周形成阶段成果"]
                                ),
                            )
                        ],
                        evidence=outputs[:3],
                        do_not=["不要在完成本周核心产出前扩大范围。"],
                    )
                )
        return HybridPlanDetailResult(
            work_items=work_items,
            days=days,
            source_document=document,
            compiler_report={
                "status": "VALIDATED",
                "provider": "FAKE",
                "prompt_version": HYBRID_PLAN_PROMPT_VERSION,
            },
        )


def build_plan_provider(settings: Settings) -> PlanProvider:
    if settings.llm_provider == "fake":
        return FakePlanProvider()
    if settings.llm_provider == "codex":
        from planner_api.codex_runtime.providers import CodexPlanProvider

        return CodexPlanProvider(settings)
    if settings.llm_provider == "deepseek":
        compiler: PlanProvider = DeepSeekPlanProvider(settings)
    else:
        compiler = OpenAICompatiblePlanProvider(settings)
    return SemanticPlanProvider(
        NaturalPlanOutlineAuthor(settings),
        NaturalPlanDetailAuthor(settings),
        compiler,
    )
