from __future__ import annotations

import hashlib
import re
from datetime import date
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ExecutionModelVersion(StrEnum):
    LEGACY_DAY_BASED = "LEGACY_DAY_BASED"
    HYBRID_WORK_ITEM = "HYBRID_WORK_ITEM"


class BindingMode(StrEnum):
    FIXED = "FIXED"
    WINDOW = "WINDOW"
    CADENCE = "CADENCE"
    ON_DEMAND = "ON_DEMAND"
    TRIGGERED = "TRIGGERED"


class FulfillmentMode(StrEnum):
    ONCE = "ONCE"
    COUNT = "COUNT"
    DURATION = "DURATION"
    EVIDENCE = "EVIDENCE"
    SELECT_K = "SELECT_K"


class TargetUnit(StrEnum):
    COUNT = "COUNT"
    MINUTES = "MINUTES"
    ITEMS = "ITEMS"
    SCORE = "SCORE"
    EVIDENCE = "EVIDENCE"


class AdjustmentAuthority(StrEnum):
    LOCKED = "LOCKED"
    USER_CONFIRM = "USER_CONFIRM"
    AUTO_WITHIN_WINDOW = "AUTO_WITHIN_WINDOW"


class WorkItemStatus(StrEnum):
    DRAFT = "DRAFT"
    ACTIVE = "ACTIVE"
    SATISFIED = "SATISFIED"
    EXPIRED = "EXPIRED"
    CANCELLED = "CANCELLED"
    SUPERSEDED = "SUPERSEDED"


class WorkItemWindowStatus(StrEnum):
    PENDING = "PENDING"
    OPEN = "OPEN"
    AT_RISK = "AT_RISK"
    SATISFIED = "SATISFIED"
    MISSED = "MISSED"
    CLOSED = "CLOSED"


class WorkItemScope(StrEnum):
    PLAN = "PLAN"
    STAGE = "STAGE"
    WEEK = "WEEK"
    CHECKPOINT = "CHECKPOINT"


class TemporalConstraint(BaseModel):
    model_config = ConfigDict(extra="forbid")

    exact_date: date | None = None
    relative_day_ref: str | None = Field(default=None, max_length=80)
    deadline_relative_day_ref: str | None = Field(default=None, max_length=80)
    earliest_date: date | None = None
    latest_date: date | None = None
    start_week: int | None = Field(default=None, ge=1, le=520)
    end_week: int | None = Field(default=None, ge=1, le=520)
    deadline: date | None = None
    allowed_weekdays: list[int] = Field(default_factory=list, max_length=7)
    excluded_dates: list[date] = Field(default_factory=list, max_length=366)

    @model_validator(mode="after")
    def validate_ranges(self) -> TemporalConstraint:
        if self.earliest_date and self.latest_date and self.earliest_date > self.latest_date:
            raise ValueError("earliest_date may not be after latest_date")
        if self.start_week and self.end_week and self.start_week > self.end_week:
            raise ValueError("start_week may not be after end_week")
        if any(day < 0 or day > 6 for day in self.allowed_weekdays):
            raise ValueError("allowed_weekdays must use 0..6")
        if len(set(self.allowed_weekdays)) != len(self.allowed_weekdays):
            raise ValueError("allowed_weekdays may not contain duplicates")
        return self


class RecurrenceConstraint(BaseModel):
    model_config = ConfigDict(extra="forbid")

    period: Literal["ONCE", "DAY", "WEEK", "MONTH", "STAGE", "CUSTOM"] = "ONCE"
    interval: int = Field(default=1, ge=1, le=365)
    target_count: int = Field(default=1, ge=1, le=10000)
    minimum_gap_days: int = Field(default=0, ge=0, le=365)
    maximum_per_day: int = Field(default=1, ge=1, le=100)


class SelectionConstraint(BaseModel):
    model_config = ConfigDict(extra="forbid")

    group_key: str | None = Field(default=None, max_length=80)
    mode: Literal["ALL", "SELECT_K", "OPTIONAL"] = "ALL"
    minimum_selected: int = Field(default=1, ge=0, le=1000)
    maximum_selected: int | None = Field(default=None, ge=1, le=1000)

    @model_validator(mode="after")
    def validate_selection(self) -> SelectionConstraint:
        if self.maximum_selected is not None and self.maximum_selected < self.minimum_selected:
            raise ValueError("maximum_selected may not be below minimum_selected")
        if self.mode == "SELECT_K" and not self.group_key:
            raise ValueError("SELECT_K requires a group_key")
        return self


class CompletionConstraint(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target_value: int = Field(default=1, ge=1, le=100000)
    target_unit: TargetUnit = TargetUnit.COUNT
    evidence_required: bool = False
    evidence_schema: dict[str, Any] = Field(default_factory=dict)


class AdjustmentConstraint(BaseModel):
    model_config = ConfigDict(extra="forbid")

    authority: AdjustmentAuthority = AdjustmentAuthority.USER_CONFIRM
    freeze_days: int = Field(default=0, ge=0, le=365)
    can_cross_window: bool = False
    soft_preferences: list[str] = Field(default_factory=list, max_length=20)


class WorkItemConstraintSet(BaseModel):
    """Validated combination of orthogonal scheduling and fulfillment rules."""

    model_config = ConfigDict(extra="forbid")

    binding_mode: BindingMode
    fulfillment_mode: FulfillmentMode
    temporal: TemporalConstraint = Field(default_factory=TemporalConstraint)
    recurrence: RecurrenceConstraint = Field(default_factory=RecurrenceConstraint)
    selection: SelectionConstraint = Field(default_factory=SelectionConstraint)
    completion: CompletionConstraint = Field(default_factory=CompletionConstraint)
    adjustment: AdjustmentConstraint = Field(default_factory=AdjustmentConstraint)
    trigger: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_combination(self) -> WorkItemConstraintSet:
        if self.binding_mode == BindingMode.FIXED and not (
            self.temporal.exact_date or self.temporal.relative_day_ref
        ):
            raise ValueError("FIXED binding requires an exact date or relative day reference")
        if self.binding_mode == BindingMode.CADENCE and self.recurrence.period == "ONCE":
            raise ValueError("CADENCE binding requires a repeating period")
        if self.binding_mode == BindingMode.TRIGGERED and not self.trigger:
            raise ValueError("TRIGGERED binding requires a trigger description")
        if self.fulfillment_mode == FulfillmentMode.ONCE:
            if self.completion.target_value != 1:
                raise ValueError("ONCE fulfillment must have target_value=1")
        if self.fulfillment_mode == FulfillmentMode.DURATION:
            if self.completion.target_unit != TargetUnit.MINUTES:
                raise ValueError("DURATION fulfillment must use MINUTES")
        if self.fulfillment_mode == FulfillmentMode.EVIDENCE:
            if not self.completion.evidence_required:
                raise ValueError("EVIDENCE fulfillment must require evidence")
        if self.fulfillment_mode == FulfillmentMode.SELECT_K:
            if self.selection.mode != "SELECT_K":
                raise ValueError("SELECT_K fulfillment requires SELECT_K selection")
        return self


WORK_ITEM_KEY_PATTERN = re.compile(r"^[A-Z0-9][A-Z0-9._-]{2,119}$")


def _validate_scope(
    scope_type: WorkItemScope,
    stage_order: int | None,
    constraints: WorkItemConstraintSet | None = None,
) -> None:
    if scope_type == WorkItemScope.PLAN and stage_order is not None:
        raise ValueError("PLAN scope may not declare a stage_order")
    if scope_type in {WorkItemScope.STAGE, WorkItemScope.WEEK} and stage_order is None:
        raise ValueError(f"{scope_type} scope requires a stage_order")
    if scope_type == WorkItemScope.WEEK and constraints is not None:
        temporal = constraints.temporal
        if temporal.start_week is None or temporal.end_week != temporal.start_week:
            raise ValueError("WEEK scope requires one matching start_week and end_week")


class PlanWorkItemDraft(BaseModel):
    """Compiler contract for one semantic obligation before persistence."""

    model_config = ConfigDict(extra="forbid")

    item_key: str = Field(min_length=3, max_length=120)
    scope_type: WorkItemScope
    stage_order: int | None = Field(default=None, ge=1, le=100)
    title: str = Field(min_length=2, max_length=180)
    description: str = Field(min_length=2, max_length=4000)
    task_kind: str = Field(min_length=2, max_length=24)
    estimate_minutes_per_occurrence: int = Field(ge=1, le=960)
    priority: int = Field(ge=1, le=5)
    is_required: bool = True
    is_splittable: bool = True
    constraint_set: WorkItemConstraintSet
    commitment_refs: list[str] = Field(default_factory=list, max_length=24)
    evidence: list[str] = Field(default_factory=list, max_length=16)
    source_refs: list[str] = Field(min_length=1, max_length=24)

    @model_validator(mode="after")
    def validate_identity_and_scope(self) -> PlanWorkItemDraft:
        if not WORK_ITEM_KEY_PATTERN.fullmatch(self.item_key):
            raise ValueError("item_key must be an uppercase stable business key")
        _validate_scope(self.scope_type, self.stage_order, self.constraint_set)
        if re.search(r"连续\s*\d+\s*分钟|不可拆分|不得拆分", self.title):
            self.is_splittable = False
        if len(self.commitment_refs) != len(set(self.commitment_refs)):
            raise ValueError("commitment_refs may not contain duplicates")
        if len(self.source_refs) != len(set(self.source_refs)):
            raise ValueError("source_refs may not contain duplicates")
        return self


class PlanWorkItemGroupDraft(BaseModel):
    """Compiler contract for a SELECT_K boundary over work-item keys."""

    model_config = ConfigDict(extra="forbid")

    group_key: str = Field(min_length=3, max_length=80)
    scope_type: WorkItemScope
    stage_order: int | None = Field(default=None, ge=1, le=100)
    title: str = Field(min_length=2, max_length=180)
    selection_mode: Literal["SELECT_K"] = "SELECT_K"
    minimum_selected: int = Field(ge=1, le=1000)
    maximum_selected: int | None = Field(default=None, ge=1, le=1000)
    candidate_item_keys: list[str] = Field(min_length=1, max_length=1000)
    source_refs: list[str] = Field(min_length=1, max_length=24)

    @model_validator(mode="after")
    def validate_group(self) -> PlanWorkItemGroupDraft:
        if not WORK_ITEM_KEY_PATTERN.fullmatch(self.group_key):
            raise ValueError("group_key must be an uppercase stable business key")
        _validate_scope(self.scope_type, self.stage_order)
        if len(self.candidate_item_keys) != len(set(self.candidate_item_keys)):
            raise ValueError("candidate_item_keys may not contain duplicates")
        if any(not WORK_ITEM_KEY_PATTERN.fullmatch(key) for key in self.candidate_item_keys):
            raise ValueError("candidate_item_keys must contain stable business keys")
        maximum = self.maximum_selected or len(self.candidate_item_keys)
        if maximum < self.minimum_selected:
            raise ValueError("maximum_selected may not be below minimum_selected")
        if self.minimum_selected > len(self.candidate_item_keys) or maximum > len(
            self.candidate_item_keys
        ):
            raise ValueError("selection bounds may not exceed the candidate count")
        if len(self.source_refs) != len(set(self.source_refs)):
            raise ValueError("source_refs may not contain duplicates")
        return self


class WorkItemDependencyDraft(BaseModel):
    """Compiler contract for an identity-safe semantic dependency edge."""

    model_config = ConfigDict(extra="forbid")

    predecessor_key: str = Field(min_length=3, max_length=120)
    successor_key: str = Field(min_length=3, max_length=120)
    dependency_type: Literal["FINISH_TO_START"] = "FINISH_TO_START"
    matching_mode: Literal["ALL", "MATCH_OCCURRENCE", "MATCH_PERIOD"] = "ALL"
    minimum_lag_days: int = Field(default=0, ge=0, le=3650)
    maximum_lag_days: int | None = Field(default=None, ge=0, le=3650)
    condition: dict[str, Any] = Field(default_factory=dict)
    source_refs: list[str] = Field(min_length=1, max_length=24)

    @model_validator(mode="after")
    def validate_dependency(self) -> WorkItemDependencyDraft:
        if self.predecessor_key == self.successor_key:
            raise ValueError("A work item may not depend on itself")
        if not WORK_ITEM_KEY_PATTERN.fullmatch(
            self.predecessor_key
        ) or not WORK_ITEM_KEY_PATTERN.fullmatch(self.successor_key):
            raise ValueError("Dependency endpoints must be stable business keys")
        if self.maximum_lag_days is not None and self.maximum_lag_days < self.minimum_lag_days:
            raise ValueError("maximum_lag_days may not be below minimum_lag_days")
        if len(self.source_refs) != len(set(self.source_refs)):
            raise ValueError("source_refs may not contain duplicates")
        return self


def legacy_policy_to_constraints(
    policy: str,
    data: dict[str, Any] | None,
    *,
    relative_day_ref: str,
) -> WorkItemConstraintSet:
    """Conservatively translate policy hints without inventing missing facts."""

    payload = data or {}
    soft_preferences = [
        str(value)
        for key, value in payload.items()
        if key in {"selection_rule", "preference", "notes"} and value
    ]
    adjustment = AdjustmentConstraint(soft_preferences=soft_preferences)
    if policy == "EXACT_DAY":
        return WorkItemConstraintSet(
            binding_mode=BindingMode.FIXED,
            fulfillment_mode=FulfillmentMode.ONCE,
            temporal=TemporalConstraint(relative_day_ref=relative_day_ref),
            adjustment=AdjustmentConstraint(authority=AdjustmentAuthority.LOCKED),
        )
    if policy in {"DATE_WINDOW", "FLOATING"}:
        return WorkItemConstraintSet(
            binding_mode=BindingMode.WINDOW,
            fulfillment_mode=FulfillmentMode.ONCE,
            temporal=TemporalConstraint(
                start_week=payload.get("start_week"),
                end_week=payload.get("end_week"),
            ),
            adjustment=adjustment,
        )
    if policy == "CADENCE":
        return WorkItemConstraintSet(
            binding_mode=BindingMode.CADENCE,
            fulfillment_mode=FulfillmentMode.COUNT,
            recurrence=RecurrenceConstraint(
                period=str(payload.get("period", "WEEK")).upper(),
                target_count=int(payload.get("target_count", payload.get("count", 1))),
                minimum_gap_days=int(payload.get("minimum_gap_days", 0)),
            ),
            completion=CompletionConstraint(
                target_value=int(payload.get("target_count", payload.get("count", 1)))
            ),
            adjustment=adjustment,
        )
    if policy == "QUOTA":
        target = int(payload.get("target_count", payload.get("count", 1)))
        return WorkItemConstraintSet(
            binding_mode=BindingMode.WINDOW,
            fulfillment_mode=FulfillmentMode.COUNT,
            recurrence=RecurrenceConstraint(target_count=target),
            completion=CompletionConstraint(target_value=target),
            adjustment=adjustment,
        )
    if policy == "TASK_POOL":
        group_key = str(payload.get("group_key") or relative_day_ref)
        count = int(payload.get("select_count", 1))
        return WorkItemConstraintSet(
            binding_mode=BindingMode.ON_DEMAND,
            fulfillment_mode=FulfillmentMode.SELECT_K,
            selection=SelectionConstraint(
                group_key=group_key,
                mode="SELECT_K",
                minimum_selected=count,
                maximum_selected=count,
            ),
            completion=CompletionConstraint(target_value=count),
            adjustment=adjustment,
        )
    if policy == "CHECKPOINT":
        return WorkItemConstraintSet(
            binding_mode=BindingMode.WINDOW,
            fulfillment_mode=FulfillmentMode.EVIDENCE,
            completion=CompletionConstraint(
                target_value=1,
                target_unit=TargetUnit.EVIDENCE,
                evidence_required=True,
                evidence_schema=dict(payload.get("evidence_schema", {})),
            ),
            adjustment=adjustment,
        )
    if policy == "TRIGGERED":
        return WorkItemConstraintSet(
            binding_mode=BindingMode.TRIGGERED,
            fulfillment_mode=FulfillmentMode.ONCE,
            trigger=dict(payload.get("trigger") or {"description": "legacy-trigger"}),
            adjustment=adjustment,
        )
    raise ValueError(f"Unsupported legacy scheduling policy: {policy}")


def work_items_enabled(execution_model_version: str, feature_enabled: bool) -> bool:
    return feature_enabled and execution_model_version == ExecutionModelVersion.HYBRID_WORK_ITEM


def hybrid_rollout_enabled(
    goal_id: str,
    mode: str,
    percentage: int = 0,
    legacy_flag: bool = False,
) -> bool:
    """Return a deterministic rollout decision for new plan generation.

    Existing callers can keep the legacy boolean.  Percentage rollout uses a
    stable hash of the goal id, so retries never move a goal between cohorts.
    """
    if legacy_flag or mode == "ALL":
        return True
    if mode == "OFF":
        return False
    if mode == "NEW_PLANS":
        return True
    if mode == "PERCENTAGE":
        bucket = int(hashlib.sha256(goal_id.encode()).hexdigest()[:8], 16) % 100
        return bucket < max(0, min(100, percentage))
    return False


def remap_materialization_key(source_key: str | None, target_plan_version_id: str) -> str | None:
    """Keep copied occurrences idempotent without violating the global unique key."""

    if not source_key:
        return None
    digest = hashlib.sha256(f"{target_plan_version_id}:{source_key}".encode()).hexdigest()
    return f"copy:{digest}"
