import re
from datetime import date, datetime
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator

from planner_api.work_item_domain import (
    PlanWorkItemDraft,
    PlanWorkItemGroupDraft,
    WorkItemDependencyDraft,
)

CORE_FIELDS = (
    "objective",
    "current_state",
    "desired_outcomes",
    "success_criteria",
    "available_capacity",
    "deadline",
)

REQUIREMENT_FIELD_KEYS = (
    *CORE_FIELDS,
    "motivation",
    "scope_in",
    "scope_out",
    "constraints",
    "preferences",
    "existing_resources",
    "risks",
)

AdjustmentReasonCode = Literal[
    "MISSED_EXECUTION",
    "DEADLINE_PROTECTION",
    "CAPACITY_INSUFFICIENT",
    "HISTORY_LOCKED",
    "CONSTRAINT_CHANGE",
    "USER_REQUESTED",
]

RequirementFieldKey = Literal[
    "objective",
    "current_state",
    "desired_outcomes",
    "success_criteria",
    "available_capacity",
    "deadline",
    "motivation",
    "scope_in",
    "scope_out",
    "constraints",
    "preferences",
    "existing_resources",
    "risks",
]

FIELD_CATEGORIES = {
    "objective": "OBJECTIVE",
    "current_state": "CURRENT_STATE",
    "desired_outcomes": "OUTCOME",
    "success_criteria": "SUCCESS",
    "available_capacity": "CAPACITY",
    "deadline": "DEADLINE",
    "motivation": "MOTIVATION",
    "scope_in": "SCOPE",
    "scope_out": "SCOPE",
    "constraints": "CONSTRAINT",
    "preferences": "PREFERENCE",
    "existing_resources": "RESOURCE",
    "risks": "RISK",
}


class GoalCreate(BaseModel):
    raw_input: str = Field(min_length=1)
    timezone: str = Field(default="Asia/Shanghai", min_length=1, max_length=64)
    web_research_enabled: bool = False

    @field_validator("raw_input")
    @classmethod
    def goal_must_contain_text(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("Goal must contain text")
        return normalized

    @field_validator("timezone")
    @classmethod
    def timezone_must_exist(cls, value: str) -> str:
        normalized = value.strip()
        try:
            ZoneInfo(normalized)
        except ZoneInfoNotFoundError as error:
            raise ValueError("Unknown timezone") from error
        return normalized


class GoalListItem(BaseModel):
    id: str
    title: str
    status: str
    last_active_at: datetime
    timezone: str = "Asia/Shanghai"
    pause_until: date | None = None
    completed_at: datetime | None = None
    archived_at: datetime | None = None


class GoalLifecycleRead(BaseModel):
    id: str
    title: str
    status: str
    timezone: str
    pause_until: date | None = None
    completed_at: datetime | None = None
    archived_at: datetime | None = None
    completion_summary: str | None = None
    remaining_required_tasks: int = 0


class GoalPauseRequest(BaseModel):
    pause_until: date | None = None


class GoalRenameRequest(BaseModel):
    title: str = Field(min_length=1, max_length=160)

    @field_validator("title")
    @classmethod
    def title_must_contain_text(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("Goal title cannot be blank")
        return normalized


class GoalSettingsUpdate(BaseModel):
    timezone: str = Field(min_length=1, max_length=64)

    @field_validator("timezone")
    @classmethod
    def timezone_must_exist(cls, value: str) -> str:
        normalized = value.strip()
        try:
            ZoneInfo(normalized)
        except ZoneInfoNotFoundError as error:
            raise ValueError("Unknown timezone") from error
        return normalized


class GoalCompleteRequest(BaseModel):
    note: str | None = Field(default=None, max_length=1000)


class GoalDeleteRequest(BaseModel):
    confirmation: str = Field(min_length=1, max_length=36)


class MessageCreate(BaseModel):
    content: str = Field(min_length=1)
    web_research_enabled: bool = False
    research_run_id: str | None = None
    regenerate: bool = False
    material_source_ids: list[str] | None = Field(default=None, max_length=500)

    @field_validator("content")
    @classmethod
    def message_must_contain_text(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("Message must contain text")
        return normalized


class ProposedFact(BaseModel):
    model_config = ConfigDict(extra="forbid")

    category: Literal[
        "OBJECTIVE",
        "CURRENT_STATE",
        "OUTCOME",
        "SUCCESS",
        "CAPACITY",
        "DEADLINE",
        "MOTIVATION",
        "SCOPE",
        "CONSTRAINT",
        "PREFERENCE",
        "RESOURCE",
        "RISK",
    ]
    field_key: RequirementFieldKey
    value: Any
    source: Literal["USER", "AGENT_INFERENCE", "SYSTEM"]
    status: Literal["PROPOSED", "CONFIRMED"]
    confidence: float = Field(ge=0, le=1)
    operation: Literal["ADD", "REPLACE", "MERGE", "RETRACT", "REQUEST_CONFIRMATION"] = "ADD"
    change_reason: str | None = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def inference_cannot_confirm_itself(self) -> "ProposedFact":
        if self.source == "AGENT_INFERENCE" and self.status == "CONFIRMED":
            raise ValueError("Agent inference cannot be confirmed without user action")
        if self.category != FIELD_CATEGORIES[self.field_key]:
            raise ValueError("Fact category does not match field key")
        return self


class ReadinessResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ready_for_review: bool
    blocking_fields: list[RequirementFieldKey] = Field(default_factory=list)
    reason: str
    confidence: float = Field(default=0, ge=0, le=1)
    blocking_topics: list[str] = Field(default_factory=list)
    recommended_next_action: str | None = None


class ConversationReply(BaseModel):
    """Natural-language output for the visible conversation call."""

    model_config = ConfigDict(extra="forbid")

    assistant_message: str = Field(min_length=1)


class PlanningApproachRecommendation(BaseModel):
    recommended_mode: Literal["EXPLORATORY", "COMMITTED"]
    reasons: list[str] = Field(min_length=1, max_length=3)
    confidence: Literal["LOW", "MEDIUM", "HIGH"]
    explicit_trial_weeks: int | None = Field(default=None, ge=1, le=24)


class TemporalAnchor(BaseModel):
    """A date-like statement classified by what it means, not by its wording."""

    model_config = ConfigDict(extra="forbid")

    label: str = Field(min_length=1, max_length=160)
    kind: Literal["HARD_DEADLINE", "REVIEW_CHECKPOINT", "OPPORTUNITY_WINDOW", "CADENCE"]
    binding: bool = False
    reason: str = Field(min_length=2, max_length=300)


class ScheduleTypeRecommendation(BaseModel):
    recommended_type: Literal["OUTCOME", "RHYTHM", "CALENDAR"]
    reasons: list[str] = Field(min_length=1, max_length=3)
    confidence: Literal["LOW", "MEDIUM", "HIGH"]
    temporal_anchors: list[TemporalAnchor] = Field(default_factory=list, max_length=8)


class LongTermMemoryCandidate(BaseModel):
    """Optional user-grounded context, never an executable planning constraint."""
    model_config = ConfigDict(extra="forbid")
    memory_key: str = Field(min_length=2, max_length=100)
    content: str = Field(min_length=2, max_length=600)
    evidence_quote: str = Field(min_length=2, max_length=800)
    operation: Literal["UPSERT", "RETRACT"] = "UPSERT"


class RequirementAnalysis(BaseModel):
    """Structured memory update produced independently from the visible reply."""

    model_config = ConfigDict(extra="forbid")

    goal_title: str | None = None
    proposed_facts: list[ProposedFact] = Field(default_factory=list)
    long_term_memories: list[LongTermMemoryCandidate] = Field(default_factory=list, max_length=6)
    conflicts: list[str] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)
    current_topic: str | None = None
    summary: str = Field(min_length=1)
    entity_refs: list[str] = Field(default_factory=list)
    readiness: ReadinessResult
    planning_approach: PlanningApproachRecommendation | None = None
    schedule_type_recommendation: ScheduleTypeRecommendation | None = None


class ClarificationResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    response_type: Literal["CLARIFY", "VERIFY", "REVIEW_READY"]
    assistant_message: str = Field(min_length=1)
    proposed_facts: list[ProposedFact] = Field(default_factory=list)
    conflicts: list[str] = Field(default_factory=list)
    questions: list[str] = Field(default_factory=list, max_length=3)
    question_keys: list[RequirementFieldKey] = Field(default_factory=list, max_length=3)
    open_questions: list[str] = Field(default_factory=list)
    readiness: ReadinessResult

    @model_validator(mode="after")
    def review_has_no_blockers(self) -> "ClarificationResult":
        if self.response_type == "REVIEW_READY" and self.readiness.blocking_fields:
            raise ValueError("Review-ready output cannot contain blocking fields")
        if self.question_keys and len(self.questions) != len(self.question_keys):
            raise ValueError("Every question must have a matching field key")
        if len(set(self.question_keys)) != len(self.question_keys):
            raise ValueError("Question keys must be unique")
        if any(question not in self.assistant_message for question in self.questions):
            raise ValueError("Assistant message must contain every question")
        if self.response_type == "REVIEW_READY":
            if not self.readiness.ready_for_review or self.questions:
                raise ValueError("Review-ready output cannot ask more questions")
        return self


class FactRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    category: str
    field_key: str
    value: Any
    source: str
    status: str
    confidence: float
    operation: str = "ADD"
    change_reason: str | None = None
    evidence_message_id: str | None = None
    supersedes_fact_id: str | None = None
    created_at: datetime | None = None


class RequirementChangeRead(BaseModel):
    id: str
    field_key: str
    operation: str
    reason: str | None = None
    old_fact_id: str | None = None
    new_fact_id: str | None = None
    old_value: Any | None = None
    new_value: Any | None = None
    evidence_message_id: str | None = None
    created_at: datetime


class ConversationMemoryRead(BaseModel):
    summary: str = ""
    current_topic: str | None = None
    recent_changes: list[dict[str, Any]] = Field(default_factory=list)
    entity_refs: list[str] = Field(default_factory=list)
    through_message_sequence: int = 0


class ClarificationAnalysisJobRead(BaseModel):
    id: str
    status: Literal["PENDING", "PROCESSING", "SUCCEEDED", "FAILED"]
    attempts: int
    max_attempts: int
    next_retry_at: datetime
    last_error: str | None = None
    last_error_code: str | None = None
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None = None


class MessageRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    sequence: int
    role: str
    content: str
    generation_status: str
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime


class ConversationMessagePage(BaseModel):
    items: list[MessageRead]
    next_before_sequence: int | None = None
    has_more: bool = False
    total: int = 0


class RequirementDraftRead(BaseModel):
    id: str
    version: int
    status: str
    snapshot: dict[str, Any]


class PlanningApproachSelection(BaseModel):
    planning_mode: Literal["EXPLORATORY", "COMMITTED"]


class ScheduleTypeSelection(BaseModel):
    schedule_type: Literal["OUTCOME", "RHYTHM", "CALENDAR"]


class ResearchSourceRead(BaseModel):
    title: str = ""
    url: str
    read_status: str = ""
    retrieval_method: str = ""
    evidence_level: str = "SEARCH_SNIPPET"
    snippet: str = ""
    excerpt: str = ""
    ranking_score: float = 0.0


class ResearchSummaryRead(BaseModel):
    id: str
    status: str
    current_stage: str
    question: str
    stop_reason: str | None = None
    error_code: str | None = None
    result_status: str | None = None
    source_count: int = 0
    read_pages: int = 0
    unresolved_points: list[str] = Field(default_factory=list, max_length=10)
    sources: list[ResearchSourceRead] = Field(default_factory=list, max_length=20)
    claims: list[dict[str, Any]] = Field(default_factory=list, max_length=12)
    coverage: dict[str, Any] = Field(default_factory=dict)


class ClarificationWorkspace(BaseModel):
    goal_id: str
    goal_title: str
    raw_input: str
    status: str
    messages: list[MessageRead]
    facts: list[FactRead]
    fact_changes: list[RequirementChangeRead] = Field(default_factory=list)
    memory: ConversationMemoryRead = Field(default_factory=ConversationMemoryRead)
    analysis_jobs: list[ClarificationAnalysisJobRead] = Field(default_factory=list)
    open_questions: list[str]
    conflicts: list[str]
    readiness: ReadinessResult
    requirement_draft: RequirementDraftRead | None = None
    downstream_route_preserved: bool = False
    requirements_changed_since_route: bool = False
    planning_approach_recommendation: PlanningApproachRecommendation
    selected_planning_mode: Literal["EXPLORATORY", "COMMITTED"] | None = None
    schedule_type_recommendation: ScheduleTypeRecommendation
    selected_schedule_type: Literal["OUTCOME", "RHYTHM", "CALENDAR"] | None = None
    latest_research: ResearchSummaryRead | None = None


class FactUpdate(BaseModel):
    action: Literal["CONFIRM", "REJECT", "REPLACE"]
    value: Any | None = None

    @model_validator(mode="after")
    def replacement_requires_value(self) -> "FactUpdate":
        if self.action == "REPLACE" and self.value is None:
            raise ValueError("Replacement requires a value")
        return self


class RouteOptionDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str = Field(min_length=1, max_length=60)
    description: str = Field(min_length=3, max_length=300)
    tradeoff: str = Field(min_length=3, max_length=240)
    recommended: bool = False


class RouteStageDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    order: int = Field(ge=1, le=7)
    title: str = Field(min_length=2, max_length=80)
    purpose: str = Field(min_length=5, max_length=500)
    topics: list[str] = Field(min_length=1, max_length=8)
    outputs: list[str] = Field(min_length=1, max_length=5)
    depends_on: list[int] = Field(default_factory=list, max_length=6)
    options: list[RouteOptionDraft] = Field(default_factory=list, max_length=4)

    @model_validator(mode="after")
    def options_have_one_recommendation(self) -> "RouteStageDraft":
        if len({option.label for option in self.options}) != len(self.options):
            raise ValueError("Route option labels must be unique within a stage")
        if sum(option.recommended for option in self.options) > 1:
            raise ValueError("A stage may recommend at most one option")
        return self


class RouteDraftResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    assistant_message: str = Field(min_length=1, max_length=2000)
    summary: str = Field(min_length=10, max_length=1000)
    stages: list[RouteStageDraft] = Field(min_length=3, max_length=7)
    tradeoffs: list[str] = Field(default_factory=list, max_length=6)
    assumptions: list[str] = Field(default_factory=list, max_length=6)
    open_questions: list[str] = Field(default_factory=list, max_length=3)
    optional_questions: list[str] = Field(default_factory=list, max_length=3)
    change_summary: list[str] = Field(default_factory=list, max_length=8)
    source_document: str | None = Field(default=None, max_length=30000)
    compiler_report: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def stages_are_ordered_and_acyclic(self) -> "RouteDraftResult":
        orders = [stage.order for stage in self.stages]
        if orders != list(range(1, len(self.stages) + 1)):
            raise ValueError("Route stage order must be contiguous and start at one")
        for stage in self.stages:
            if len(set(stage.depends_on)) != len(stage.depends_on):
                raise ValueError("Stage dependencies must be unique")
            if any(dependency < 1 or dependency >= stage.order for dependency in stage.depends_on):
                raise ValueError("A stage may only depend on an earlier stage")
        return self


class RouteFeedback(BaseModel):
    content: str = Field(min_length=1)
    web_research_enabled: bool = False
    research_run_id: str | None = None
    regenerate: bool = False

    @field_validator("content")
    @classmethod
    def feedback_must_contain_text(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("Route feedback must contain text")
        return normalized


class RouteExplanationResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    answer: str = Field(min_length=20, max_length=6000)
    covered_questions: list[str] = Field(min_length=1, max_length=8)
    needs_clarification: bool = False
    long_term_memories: list[LongTermMemoryCandidate] = Field(default_factory=list, max_length=6)


class RouteStageRead(BaseModel):
    id: str
    order: int
    title: str
    purpose: str
    topics: list[str]
    outputs: list[str]
    depends_on: list[int]
    options: list[RouteOptionDraft]


class RouteVersionRead(BaseModel):
    id: str
    version: int
    revision: int
    status: str
    summary: str
    stages: list[RouteStageRead]
    tradeoffs: list[str]
    assumptions: list[str]
    open_questions: list[str]
    optional_questions: list[str] = Field(default_factory=list)
    change_summary: list[str]
    source_document: str | None = None
    compiler_report: dict[str, Any] = Field(default_factory=dict)
    planning_mode: Literal["EXPLORATORY", "COMMITTED"] = "COMMITTED"
    schedule_type: Literal["OUTCOME", "RHYTHM", "CALENDAR"] = "RHYTHM"


class RouteWorkspace(BaseModel):
    goal_id: str
    goal_title: str
    status: str
    requirement_version: int
    requirement_snapshot: dict[str, Any]
    route: RouteVersionRead | None = None
    messages: list[MessageRead] = Field(default_factory=list)
    pending_requirement_version: int | None = None
    requirements_changed_since_route: bool = False
    planning_mode: Literal["EXPLORATORY", "COMMITTED"]
    schedule_type: Literal["OUTCOME", "RHYTHM", "CALENDAR"]
    latest_research: ResearchSummaryRead | None = None


class PlanStageContributionDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")
    stage_order: int = Field(ge=1, le=7)
    action: str = Field(min_length=3, max_length=240)


class PlanTaskDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    order: int = Field(ge=1, le=24)
    title: str = Field(min_length=2, max_length=100)
    description: str = Field(min_length=3, max_length=400)
    estimate_minutes: int = Field(ge=5, le=240)
    priority: int = Field(ge=1, le=5)
    task_kind: Literal["PREPARE", "LEARN", "PRACTICE", "BUILD", "REVIEW", "BUFFER"]
    is_required: bool = True
    evidence: list[str] = Field(max_length=4)
    # Semantic group gates may resolve to more than six concrete tasks. Keep
    # reference existence/order validation; a UI-sized cap must not make an
    # otherwise valid persisted dependency graph unreadable on the next edit.
    depends_on: list[str] = Field(default_factory=list)
    satisfies_commitments: list[str] = Field(default_factory=list, max_length=12)
    work_item_keys: list[str] = Field(default_factory=list, max_length=12)
    stage_contributions: list[PlanStageContributionDraft] = Field(default_factory=list, max_length=7)
    occurrence_key: str | None = Field(default=None, min_length=1, max_length=120)
    is_splittable: bool = True
    scheduling_policy: Literal[
        "EXACT_DAY",
        "DATE_WINDOW",
        "CADENCE",
        "QUOTA",
        "TASK_POOL",
        "CHECKPOINT",
        "TRIGGERED",
        "FLOATING",
    ] = "EXACT_DAY"
    scheduling_policy_data: dict[str, Any] = Field(default_factory=dict)


class PlanDayDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    stage_order: int = Field(ge=1, le=7)
    week_index: int = Field(ge=1, le=24)
    day_index: int = Field(ge=1, le=7)
    primary_goal: str = Field(min_length=5, max_length=240)
    # OUTCOME uses this row as a weekly window, not a single execution day.
    # The service checks its actual confirmed phase capacity, and separately
    # enforces daily ceilings for RHYTHM plans.
    suggested_minutes: int = Field(ge=10, le=6720)
    # A day with no tasks is a valid rest/buffer day. The database already
    # stores these days, so adjustment previews must be able to round-trip them.
    # Keep the normal-generation prompt conservative, but allow legacy plans
    # with denser days to pass through revision and preview validation without
    # losing persisted tasks.
    tasks: list[PlanTaskDraft] = Field(max_length=24)
    evidence: list[str] = Field(max_length=6)
    do_not: list[str] = Field(default_factory=list, max_length=5)

    @model_validator(mode="after")
    def tasks_are_ordered_and_fit_the_day(self) -> "PlanDayDraft":
        if [task.order for task in self.tasks] != list(range(1, len(self.tasks) + 1)):
            raise ValueError("Plan task order must be contiguous and start at one")
        if sum(task.estimate_minutes for task in self.tasks) > self.suggested_minutes:
            raise ValueError("Plan task estimates may not exceed the suggested day capacity")
        for task in self.tasks:
            if re.search(r"连续\s*\d+\s*分钟|不可拆分|不得拆分", task.title):
                task.is_splittable = False
        return self


def _validate_relative_task_dependencies(days: list[PlanDayDraft]) -> None:
    canonical = re.compile(r"^M(\d+)-W(\d+)-D(\d+)-T(\d+)$", re.IGNORECASE)
    for day in days:
        for task in day.tasks:
            current = (day.week_index, day.day_index, task.order)
            for dependency in task.depends_on:
                match = canonical.fullmatch(dependency.strip())
                if match is None:
                    raise ValueError("Task dependencies must use canonical M1-W1-D1-T1 references")
                dependency_stage, week, execution_day, order = map(int, match.groups())
                if (week, execution_day, order) >= current:
                    raise ValueError("Task dependencies must point to an earlier task")
                if dependency_stage > day.stage_order:
                    raise ValueError("Task dependencies may not point to a later stage")


class PlanMilestoneDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    stage_order: int = Field(ge=1, le=7)
    title: str = Field(min_length=2, max_length=100)
    purpose: str = Field(min_length=5, max_length=400)
    start_week: int = Field(ge=1, le=24)
    end_week: int = Field(ge=1, le=24)
    outputs: list[str] = Field(min_length=1, max_length=5)

    @model_validator(mode="after")
    def week_range_is_valid(self) -> "PlanMilestoneDraft":
        if self.end_week < self.start_week:
            raise ValueError("Milestone end week may not be before its start week")
        return self


class PlanWeekCapacityOverrideDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    week_index: int = Field(ge=1, le=24)
    reason: str = Field(min_length=2, max_length=240)
    weekly_execution_days: int = Field(ge=1, le=7)
    available_days_per_week: int | None = Field(default=None, ge=1, le=7)
    suggested_daily_minutes: int = Field(ge=10, le=960)
    exception_days_per_week: int = Field(default=0, ge=0, le=7)
    exception_daily_minutes: int | None = Field(default=None, ge=10, le=960)
    exception_weekdays: list[int] = Field(default_factory=list, max_length=7,
        description="Explicit exception weekdays: Monday=0, Tuesday=1, Wednesday=2, Thursday=3, Friday=4, Saturday=5, Sunday=6. Never use 1..7.")

    @model_validator(mode="after")
    def exceptions_are_scoped(self) -> "PlanWeekCapacityOverrideDraft":
        _validate_capacity_exception(self)
        return self


def _validate_capacity_exception(value) -> None:
    if bool(value.exception_days_per_week) != (value.exception_daily_minutes is not None):
        raise ValueError("Capacity exceptions require both count and minutes")
    weekdays = value.exception_weekdays
    if len(weekdays) != len(set(weekdays)) or any(day < 0 or day > 6 for day in weekdays):
        raise ValueError("Capacity exception weekdays must be unique and use 0..6")
    if weekdays and len(weekdays) != value.exception_days_per_week:
        raise ValueError("Capacity exception weekdays must match exception day count")


class PlanTemporalSegmentDraft(BaseModel):
    """A planning rhythm that is effective for only part of the timeline."""

    model_config = ConfigDict(extra="forbid")

    label: str = Field(min_length=2, max_length=100)
    start_week: int = Field(ge=1, le=24)
    end_week: int = Field(ge=1, le=24)
    date_scope: str = Field(min_length=2, max_length=200)
    effective_from: date | None = None
    effective_to: date | None = None
    capacity_summary: str = Field(min_length=2, max_length=400)
    weekly_execution_days: int | None = Field(default=None, ge=1, le=7)
    # OUTCOME uses D1 as a checkpoint, not as the number of days available.
    available_days_per_week: int | None = Field(default=None, ge=1, le=7)
    suggested_daily_minutes: int | None = Field(default=None, ge=10, le=960)
    exception_days_per_week: int = Field(default=0, ge=0, le=7)
    exception_daily_minutes: int | None = Field(default=None, ge=10, le=960)
    exception_weekdays: list[int] = Field(default_factory=list, max_length=7,
        description="Explicit exception weekdays: Monday=0, Tuesday=1, Wednesday=2, Thursday=3, Friday=4, Saturday=5, Sunday=6. Never use 1..7.")
    weekly_capacity_overrides: list[PlanWeekCapacityOverrideDraft] = Field(
        default_factory=list, max_length=24
    )
    frequency_rules: list[str] = Field(default_factory=list, max_length=8)
    rest_rules: list[str] = Field(default_factory=list, max_length=6)
    holiday_rules: list[str] = Field(default_factory=list, max_length=6)

    @model_validator(mode="after")
    def range_is_valid(self) -> "PlanTemporalSegmentDraft":
        if self.end_week < self.start_week:
            raise ValueError("Temporal segment end week may not be before its start week")
        if self.effective_from and self.effective_to and self.effective_to < self.effective_from:
            raise ValueError("Temporal segment end date may not be before its start date")
        _validate_capacity_exception(self)
        override_weeks = [item.week_index for item in self.weekly_capacity_overrides]
        if len(override_weeks) != len(set(override_weeks)) or any(
            week < self.start_week or week > self.end_week for week in override_weeks
        ):
            raise ValueError("Weekly capacity overrides must be unique and inside the segment")
        return self


class PlanCommitmentDraft(BaseModel):
    """A measurable promise extracted from the natural planning document."""

    model_config = ConfigDict(extra="forbid")

    label: str = Field(min_length=2, max_length=160)
    start_week: int = Field(ge=1, le=24)
    end_week: int = Field(ge=1, le=24)
    cadence: Literal["TOTAL", "PER_WEEK", "PER_EXECUTION_DAY"]
    minimum_occurrences: int = Field(ge=0, le=168)
    maximum_occurrences: int | None = Field(default=None, ge=0, le=168)
    minimum_minutes_per_occurrence: int | None = Field(default=None, ge=5, le=960)
    minimum_total_minutes: int | None = Field(default=None, ge=5, le=230400)
    # Equal keys share work; different keys may be added without double counting.
    workload_key: str | None = Field(default=None, min_length=2, max_length=100)
    requires_separate_execution_day: bool = False
    adds_to_daily_baseline: bool = False
    match_terms: list[str] = Field(min_length=1, max_length=12)
    source_text: str = Field(min_length=2, max_length=400)

    @model_validator(mode="after")
    def commitment_is_valid(self) -> "PlanCommitmentDraft":
        if self.end_week < self.start_week:
            raise ValueError("Commitment end week may not be before its start week")
        if (
            self.maximum_occurrences is not None
            and self.maximum_occurrences < self.minimum_occurrences
        ):
            raise ValueError("Commitment maximum may not be below its minimum")
        return self


def _validate_commitment_capacity(
    segments: list[PlanTemporalSegmentDraft],
    commitments: list[PlanCommitmentDraft],
) -> None:
    for segment in segments:
        weekly_commitments = [
            commitment
            for commitment in commitments
            if commitment.cadence == "PER_WEEK"
            and commitment.start_week <= segment.end_week
            and commitment.end_week >= segment.start_week
        ]
        separate_days = max(
            (
                commitment.minimum_occurrences
                for commitment in weekly_commitments
                if commitment.requires_separate_execution_day
            ),
            default=0,
        )
        # Ordinary weekly occurrences may share an execution day. Only commitments
        # explicitly marked as separate imply a minimum number of distinct days.
        required_days = separate_days
        if (
            segment.weekly_execution_days is not None
            and required_days > segment.weekly_execution_days
        ):
            raise ValueError(
                "Temporal segment execution days are below its weekly commitments: "
                f"segment={segment.label}, required={required_days}, "
                f"available={segment.weekly_execution_days}"
            )
        additive_days = max(
            (
                commitment.minimum_occurrences
                for commitment in weekly_commitments
                if commitment.adds_to_daily_baseline
                and not commitment.requires_separate_execution_day
            ),
            default=0,
        )
        additional_capacity_days = separate_days + additive_days
        if additional_capacity_days > segment.exception_days_per_week:
            raise ValueError(
                "Temporal segment capacity exception days are below additive commitments: "
                f"segment={segment.label}, required={additional_capacity_days}, "
                f"available={segment.exception_days_per_week}"
            )
        daily_minimum = sum(
            int(commitment.minimum_minutes_per_occurrence or 0) * commitment.minimum_occurrences
            for commitment in commitments
            if commitment.cadence == "PER_EXECUTION_DAY"
            and commitment.start_week <= segment.end_week
            and commitment.end_week >= segment.start_week
        )
        if (
            segment.suggested_daily_minutes is not None
            and daily_minimum > segment.suggested_daily_minutes
        ):
            raise ValueError(
                "Temporal segment normal capacity is below its per-day commitments: "
                f"segment={segment.label}, required={daily_minimum}, "
                f"available={segment.suggested_daily_minutes}"
            )


class PlanDraftResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    assistant_message: str = Field(min_length=1, max_length=2000)
    summary: str = Field(min_length=10, max_length=1000)
    total_weeks: int = Field(ge=1, le=24)
    weekly_execution_days: int = Field(ge=1, le=7)
    weekly_minutes: int = Field(ge=30, le=3360)
    milestones: list[PlanMilestoneDraft] = Field(min_length=1, max_length=7)
    temporal_segments: list[PlanTemporalSegmentDraft] = Field(default_factory=list, max_length=12)
    commitments: list[PlanCommitmentDraft] = Field(default_factory=list, max_length=24)
    days: list[PlanDayDraft] = Field(min_length=1, max_length=168)
    buffer_strategy: list[str] = Field(min_length=1, max_length=6)
    assumptions: list[str] = Field(default_factory=list, max_length=8)
    open_questions: list[str] = Field(default_factory=list, max_length=3)

    @model_validator(mode="after")
    def relative_timeline_is_consistent(self) -> "PlanDraftResult":
        milestone_orders = [item.stage_order for item in self.milestones]
        if milestone_orders != list(range(1, len(self.milestones) + 1)):
            raise ValueError("Plan milestone stages must be contiguous and start at one")
        coordinates = [(day.week_index, day.day_index) for day in self.days]
        if coordinates != sorted(coordinates) or len(coordinates) != len(set(coordinates)):
            raise ValueError("Plan days must be unique and ordered by relative week and day")
        if any(day.week_index > self.total_weeks for day in self.days):
            raise ValueError("Plan day exceeds total plan weeks")
        if any(day.day_index > self.weekly_execution_days for day in self.days):
            raise ValueError("Plan day exceeds weekly execution day count")
        if any(item.end_week > self.total_weeks for item in self.commitments):
            raise ValueError("Plan commitment exceeds the plan timeline")
        if self.temporal_segments:
            if self.temporal_segments[0].start_week != 1:
                raise ValueError("The first temporal segment must start in week one")
            if self.temporal_segments[-1].end_week != self.total_weeks:
                raise ValueError("The last temporal segment must end in the final plan week")
            for previous, current in zip(
                self.temporal_segments, self.temporal_segments[1:], strict=False
            ):
                if current.start_week != previous.end_week + 1:
                    raise ValueError("Temporal segments must be contiguous and non-overlapping")
            _validate_commitment_capacity(self.temporal_segments, self.commitments)
        _validate_relative_task_dependencies(self.days)
        return self


class PlanOutlineResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    assistant_message: str = Field(min_length=1, max_length=2000)
    summary: str = Field(min_length=10, max_length=1000)
    total_weeks: int = Field(ge=1, le=24)
    weekly_execution_days: int = Field(ge=1, le=7)
    weekly_minutes: int = Field(ge=30, le=3360)
    milestones: list[PlanMilestoneDraft] = Field(min_length=1, max_length=7)
    temporal_segments: list[PlanTemporalSegmentDraft] = Field(default_factory=list, max_length=12)
    commitments: list[PlanCommitmentDraft] = Field(default_factory=list, max_length=24)
    buffer_strategy: list[str] = Field(min_length=1, max_length=6)
    assumptions: list[str] = Field(default_factory=list, max_length=8)
    open_questions: list[str] = Field(default_factory=list, max_length=3)
    source_document: str | None = Field(default=None, max_length=40000)
    compiler_report: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def milestones_cover_the_relative_timeline(self) -> "PlanOutlineResult":
        orders = [item.stage_order for item in self.milestones]
        if orders != list(range(1, len(self.milestones) + 1)):
            raise ValueError("Plan milestone stages must be contiguous and start at one")
        if self.milestones[0].start_week != 1:
            raise ValueError("The first milestone must start in week one")
        if self.milestones[-1].end_week != self.total_weeks:
            raise ValueError("The last milestone must end in the final plan week")
        for previous, current in zip(self.milestones, self.milestones[1:], strict=False):
            if current.start_week < previous.start_week:
                raise ValueError("Milestones must start in route order")
            if current.start_week > previous.end_week + 1:
                raise ValueError("Milestones may not leave empty weeks")
            if current.end_week < previous.end_week:
                raise ValueError("Milestones must end in route order")
        if self.temporal_segments:
            if self.temporal_segments[0].start_week != 1:
                raise ValueError("The first temporal segment must start in week one")
            if self.temporal_segments[-1].end_week != self.total_weeks:
                raise ValueError("The last temporal segment must end in the final plan week")
            for previous, current in zip(
                self.temporal_segments, self.temporal_segments[1:], strict=False
            ):
                if current.start_week != previous.end_week + 1:
                    raise ValueError("Temporal segments must be contiguous and non-overlapping")
        if any(item.end_week > self.total_weeks for item in self.commitments):
            raise ValueError("Plan commitment exceeds the plan timeline")
        # An outline may describe a genuine demand/capacity conflict. Keep both
        # sides intact so the service can persist it for user negotiation;
        # executable drafts and revision results remain strictly validated.
        return self


class PlanDetailResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    days: list[PlanDayDraft] = Field(min_length=1, max_length=42)
    source_document: str | None = Field(default=None, max_length=60000)
    compiler_report: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def days_are_unique_and_ordered(self) -> "PlanDetailResult":
        coordinates = [(day.week_index, day.day_index) for day in self.days]
        if coordinates != sorted(coordinates) or len(coordinates) != len(set(coordinates)):
            raise ValueError("Detailed plan days must be unique and ordered")
        _validate_relative_task_dependencies(self.days)
        return self


class HybridPlanOutlineResult(PlanOutlineResult):
    """Hybrid outline with plan- and stage-scoped standing obligations."""

    standing_work_items: list[PlanWorkItemDraft] = Field(default_factory=list, max_length=240)
    work_item_groups: list[PlanWorkItemGroupDraft] = Field(default_factory=list, max_length=80)
    work_item_dependencies: list[WorkItemDependencyDraft] = Field(
        default_factory=list, max_length=480
    )

    @model_validator(mode="after")
    def outline_work_item_keys_are_unique(self) -> "HybridPlanOutlineResult":
        keys = [item.item_key for item in self.standing_work_items]
        if len(keys) != len(set(keys)):
            raise ValueError("Hybrid outline work-item keys must be unique")
        group_keys = [item.group_key for item in self.work_item_groups]
        if len(group_keys) != len(set(group_keys)):
            raise ValueError("Hybrid outline group keys must be unique")
        return self


class HybridPlanDetailResult(BaseModel):
    """Hybrid detail batch with constraints and its reviewable daily plan."""

    model_config = ConfigDict(extra="forbid")

    work_items: list[PlanWorkItemDraft] = Field(default_factory=list, max_length=240)
    work_item_groups: list[PlanWorkItemGroupDraft] = Field(default_factory=list, max_length=80)
    work_item_dependencies: list[WorkItemDependencyDraft] = Field(
        default_factory=list, max_length=480
    )
    days: list[PlanDayDraft] = Field(min_length=1, max_length=42)
    source_document: str | None = Field(default=None, max_length=60000)
    compiler_report: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def detail_work_item_keys_are_unique(self) -> "HybridPlanDetailResult":
        keys = [item.item_key for item in self.work_items]
        if len(keys) != len(set(keys)):
            raise ValueError("Hybrid detail work-item keys must be unique")
        group_keys = [item.group_key for item in self.work_item_groups]
        if len(group_keys) != len(set(group_keys)):
            raise ValueError("Hybrid detail group keys must be unique")
        coordinates = [(day.week_index, day.day_index) for day in self.days]
        if coordinates != sorted(coordinates) or len(coordinates) != len(set(coordinates)):
            raise ValueError("Hybrid detail days must be unique and ordered")
        _validate_relative_task_dependencies(self.days)
        return self


class RecoveryDecisionRequest(BaseModel):
    decision: Literal["ALLOW_EXTENSION", "KEEP_DEADLINE", "ACCEPT_LOAD"]
    token: str = Field(min_length=1, max_length=64)


class PlanFeedback(BaseModel):
    content: str = Field(min_length=1)
    # Research is an explicit per-message opt-in. Keeping the default off
    # preserves the existing local-only discussion behavior.
    web_research_enabled: bool = False
    research_run_id: str | None = Field(default=None, min_length=1, max_length=36)
    regenerate: bool = False

    @field_validator("content")
    @classmethod
    def feedback_must_contain_text(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("Plan feedback must contain text")
        return normalized


class PlanAdjustmentApply(BaseModel):
    preview_id: str | None = Field(default=None, min_length=1, max_length=36)
    accept_unallocated: bool = False
    schedule_overrides: list["ScheduleOverride"] = Field(default_factory=list, max_length=200)


class ScheduleOverride(BaseModel):
    scheduled_date: date
    title: str = Field(min_length=1, max_length=180)
    start_local: str | None = None
    end_local: str | None = None

    @field_validator("start_local", "end_local")
    @classmethod
    def override_time_must_be_valid(cls, value: str | None) -> str | None:
        return _validate_local_time(value) if value is not None else None

    @model_validator(mode="after")
    def override_times_must_be_ordered(self) -> "ScheduleOverride":
        if (self.start_local is None) != (self.end_local is None):
            raise ValueError("Schedule override must include both start and end")
        if self.start_local is not None and self.start_local >= self.end_local:
            raise ValueError("Schedule override end must be later than start")
        return self


class PlanExplanationResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    answer: str = Field(min_length=20, max_length=6000)
    covered_questions: list[str] = Field(min_length=1, max_length=8)
    needs_clarification: bool = False
    long_term_memories: list[LongTermMemoryCandidate] = Field(default_factory=list, max_length=6)


class ErrorAvoidanceMemoryCandidate(BaseModel):
    """A goal-scoped failure pattern explicitly identified by the user."""

    model_config = ConfigDict(extra="forbid")

    rule: str = Field(min_length=5, max_length=800)
    evidence_quote: str = Field(min_length=2, max_length=500)
    scope: Literal["GOAL", "STAGE", "WEEK", "RECOVERY"] = "GOAL"
    target_refs: list[str] = Field(default_factory=list, max_length=24)
    valid_days: int | None = Field(default=None, ge=1, le=365)
    operation: Literal["UPSERT", "RETRACT"] = "UPSERT"
    memory_id: str | None = Field(default=None, min_length=36, max_length=36)

    @model_validator(mode="after")
    def retraction_targets_existing_memory(self) -> "ErrorAvoidanceMemoryCandidate":
        if self.operation == "RETRACT" and not self.memory_id:
            raise ValueError("Error-memory retraction requires memory_id")
        return self


class PlanAdjustmentProposal(BaseModel):
    model_config = ConfigDict(extra="forbid")
    long_term_memories: list[LongTermMemoryCandidate] = Field(default_factory=list, max_length=6)

    assistant_message: str = Field(min_length=20, max_length=2400)
    user_intent_summary: str = Field(min_length=5, max_length=400)
    scope: Literal["OVERALL", "STAGE", "WEEK", "MULTI_SCOPE"]
    affected_stage_orders: list[int] = Field(default_factory=list, max_length=7)
    affected_weeks: list[int] = Field(default_factory=list, max_length=24)
    downstream_affected_weeks: list[int] = Field(default_factory=list, max_length=24)
    proposed_changes: list[str] = Field(min_length=1, max_length=10)
    tradeoffs: list[str] = Field(default_factory=list, max_length=6)
    unchanged_guarantees: list[str] = Field(min_length=1, max_length=8)
    total_weeks_change: int = Field(default=0, ge=-23, le=23)
    weekly_intensity_change: Literal["LOWER", "UNCHANGED", "HIGHER", "MIXED"]
    buffer_change: Literal["LESS", "UNCHANGED", "MORE", "MIXED"]
    target_schedule_type: Literal["OUTCOME", "RHYTHM", "CALENDAR"] | None = None
    capacity_segments: list[PlanTemporalSegmentDraft] | None = Field(default=None, max_length=12)
    proposed_deadline: date | None = None
    reason_codes: list[AdjustmentReasonCode] = Field(
        default_factory=lambda: ["USER_REQUESTED"], max_length=6
    )
    needs_clarification: bool = False
    clarifying_question: str | None = Field(default=None, max_length=500)
    can_apply: bool
    error_memory_candidates: list[ErrorAvoidanceMemoryCandidate] = Field(
        default_factory=list, max_length=4
    )

    @model_validator(mode="after")
    def proposal_is_reviewable(self) -> "PlanAdjustmentProposal":
        for values in (
            self.affected_stage_orders,
            self.affected_weeks,
            self.downstream_affected_weeks,
        ):
            if values != sorted(set(values)) or any(value < 1 for value in values):
                raise ValueError("Affected stage and week indexes must be sorted unique positives")
        if self.needs_clarification and not self.clarifying_question:
            raise ValueError("A clarifying question is required when clarification is needed")
        if self.needs_clarification and self.can_apply:
            raise ValueError("A proposal needing clarification cannot be applied")
        return self


class PlanAdjustmentApplyResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    assistant_message: str = Field(min_length=20, max_length=2400)
    summary: str = Field(min_length=10, max_length=1000)
    total_weeks: int = Field(ge=1, le=24)
    weekly_execution_days: int = Field(ge=1, le=7)
    weekly_minutes: int = Field(ge=30, le=3360)
    milestones: list[PlanMilestoneDraft] = Field(min_length=1, max_length=7)
    temporal_segments: list[PlanTemporalSegmentDraft] | None = Field(default=None, max_length=12)
    commitments: list[PlanCommitmentDraft] | None = Field(default=None, max_length=24)
    regenerated_weeks: list[int] = Field(min_length=1, max_length=24)
    replacement_days: list[PlanDayDraft] = Field(min_length=1, max_length=168)
    buffer_strategy: list[str] = Field(min_length=1, max_length=6)
    assumptions: list[str] = Field(default_factory=list, max_length=8)
    open_questions: list[str] = Field(default_factory=list, max_length=3)
    change_summary: list[str] = Field(min_length=1, max_length=10)
    source_document: str | None = Field(default=None, max_length=60000)
    compiler_report: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def partial_revision_is_consistent(self) -> "PlanAdjustmentApplyResult":
        orders = [item.stage_order for item in self.milestones]
        if orders != list(range(1, len(self.milestones) + 1)):
            raise ValueError("Plan milestone stages must be contiguous and start at one")
        if self.milestones[0].start_week != 1:
            raise ValueError("The first milestone must start in week one")
        if self.milestones[-1].end_week != self.total_weeks:
            raise ValueError("The last milestone must end in the final plan week")
        if self.temporal_segments:
            if self.temporal_segments[0].start_week != 1:
                raise ValueError("The first temporal segment must start in week one")
            if self.temporal_segments[-1].end_week != self.total_weeks:
                raise ValueError("The last temporal segment must end in the final plan week")
            for previous, current in zip(
                self.temporal_segments, self.temporal_segments[1:], strict=False
            ):
                if current.start_week != previous.end_week + 1:
                    raise ValueError("Temporal segments must be contiguous and non-overlapping")
        if self.commitments and any(item.end_week > self.total_weeks for item in self.commitments):
            raise ValueError("Plan commitment exceeds the revised timeline")
        if self.temporal_segments is not None and self.commitments is not None:
            _validate_commitment_capacity(self.temporal_segments, self.commitments)
        if self.regenerated_weeks != sorted(set(self.regenerated_weeks)):
            raise ValueError("Regenerated weeks must be sorted and unique")
        if any(week < 1 or week > self.total_weeks for week in self.regenerated_weeks):
            raise ValueError("Regenerated week exceeds the revised plan timeline")
        returned_weeks = {day.week_index for day in self.replacement_days}
        if returned_weeks != set(self.regenerated_weeks):
            raise ValueError("Replacement days must cover exactly the regenerated weeks")
        coordinates = [(day.week_index, day.day_index) for day in self.replacement_days]
        if coordinates != sorted(coordinates) or len(coordinates) != len(set(coordinates)):
            raise ValueError("Replacement days must be unique and ordered")
        if any(day.day_index > self.weekly_execution_days for day in self.replacement_days):
            raise ValueError("Replacement day exceeds weekly execution day count")
        _validate_relative_task_dependencies(self.replacement_days)
        return self


class PlanAdjustmentPreviewRead(BaseModel):
    id: str
    goal_id: str
    source_plan_version_id: str
    status: Literal["PREVIEW", "APPLIED"]
    fingerprint: str
    plan: dict[str, Any]
    schedule: list[dict[str, Any]] = Field(default_factory=list)
    unallocated_count: int = 0
    reason_codes: list[AdjustmentReasonCode] = Field(default_factory=list)
    created_at: datetime


class RecoveryTaskAssignment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    backlog_task_id: str = Field(min_length=1, max_length=36)
    target_plan_day_id: str = Field(min_length=1, max_length=36)
    rationale: str = Field(min_length=2, max_length=240)


class RecoveryAllocationResult(BaseModel):
    long_term_memories: list[LongTermMemoryCandidate] = Field(default_factory=list, max_length=6)
    model_config = ConfigDict(extra="forbid")

    assistant_message: str = Field(min_length=20, max_length=2400)
    assignments: list[RecoveryTaskAssignment] = Field(default_factory=list, max_length=120)
    unassigned_task_ids: list[str] = Field(default_factory=list, max_length=120)
    tradeoffs: list[str] = Field(default_factory=list, max_length=8)
    needs_clarification: bool = False
    clarifying_question: str | None = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def allocation_is_consistent(self) -> "RecoveryAllocationResult":
        task_ids = [item.backlog_task_id for item in self.assignments]
        if len(task_ids) != len(set(task_ids)):
            raise ValueError("Recovery tasks may only be assigned once")
        if self.needs_clarification and not self.clarifying_question:
            raise ValueError("Clarifying question is required")
        return self


class PlanRevisionResult(PlanDraftResult):
    change_summary: list[str] = Field(min_length=1, max_length=10)

    @model_validator(mode="after")
    def revised_plan_is_fully_consistent(self) -> "PlanRevisionResult":
        if self.milestones[0].start_week != 1:
            raise ValueError("The first milestone must start in week one")
        if self.milestones[-1].end_week != self.total_weeks:
            raise ValueError("The last milestone must end in the final plan week")
        for previous, current in zip(self.milestones, self.milestones[1:], strict=False):
            if current.start_week > previous.end_week + 1:
                raise ValueError("Milestones may not leave empty weeks")
            if current.end_week < previous.end_week:
                raise ValueError("Milestones must end in route order")
        milestone_by_order = {item.stage_order: item for item in self.milestones}
        for day in self.days:
            milestone = milestone_by_order.get(day.stage_order)
            if milestone is None or not (
                milestone.start_week <= day.week_index <= milestone.end_week
            ):
                raise ValueError("Every plan day must belong to its milestone week range")
        return self


class PlanTaskRead(BaseModel):
    id: str
    order: int
    title: str
    description: str
    estimate_minutes: int
    priority: int
    task_kind: str
    is_required: bool
    evidence: list[str]
    depends_on: list[str]
    dependency_titles: dict[str, str] = Field(default_factory=dict)
    satisfies_commitments: list[str] = Field(default_factory=list)
    ai_schedule_requested: bool = False
    is_splittable: bool = True
    scheduling_policy: str = "EXACT_DAY"
    scheduling_policy_data: dict[str, Any] = Field(default_factory=dict)
    status: str
    source_kind: Literal["LEGACY_PLAN_TASK", "WORK_ITEM_OCCURRENCE"] = "LEGACY_PLAN_TASK"
    source_work_item_id: str | None = None
    work_item_window_id: str | None = None
    occurrence_index: int | None = None
    materialization_key: str | None = None
    skip_guidance: list[str] = Field(default_factory=list)
    blocked_reason: str | None = None


class WorkItemWindowRead(BaseModel):
    id: str
    period_key: str
    eligible_from: date
    eligible_to: date
    required_value: int
    status: str
    resolution: dict[str, Any] = Field(default_factory=dict)
    materialization_revision: int = 0


class PlanWorkItemGroupRead(BaseModel):
    id: str
    milestone_id: str | None = None
    scope_type: str
    stage_order: int | None = None
    group_key: str
    title: str
    selection_mode: str
    minimum_selected: int
    maximum_selected: int | None = None
    constraints: dict[str, Any] = Field(default_factory=dict)
    status: str


class CandidatePoolItemRead(BaseModel):
    id: str
    item_key: str
    title: str
    description: str
    estimate_minutes: int
    priority: int
    status: str


class CandidatePoolPeriodRead(BaseModel):
    period_key: str
    eligible_from: date
    eligible_to: date
    selected_item_ids: list[str] = Field(default_factory=list)
    completed_selected: int = 0
    remaining_required: int = 0
    period_status: Literal["NOT_STARTED", "IN_PROGRESS", "COMPLETE"] = "NOT_STARTED"


class CandidatePoolRead(BaseModel):
    id: str
    group_key: str
    title: str
    selection_mode: str
    minimum_selected: int
    maximum_selected: int | None = None
    selected_item_ids: list[str] = Field(default_factory=list)
    candidates: list[CandidatePoolItemRead] = Field(default_factory=list)
    completed_selected: int = 0
    remaining_required: int = 0
    period_status: Literal["NOT_STARTED", "IN_PROGRESS", "COMPLETE"] = "NOT_STARTED"
    periods: list[CandidatePoolPeriodRead] = Field(default_factory=list)


class CandidateReplacementRequest(BaseModel):
    group_id: str = Field(min_length=1, max_length=36)
    candidate_item_id: str = Field(min_length=1, max_length=36)
    target_date: date
    reason: str = Field(min_length=1, max_length=1000)
    period_key: str | None = Field(default=None, min_length=1, max_length=120)

    @field_validator("reason")
    @classmethod
    def replacement_reason_must_not_be_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("候选替换需要填写原因")
        return value


class HybridMigrationAuditRead(BaseModel):
    plan_version_id: str
    source_execution_model: str
    target_execution_model: str
    status: Literal["PENDING", "ELIGIBLE", "MIGRATED", "QUARANTINED"]
    rollout_bucket: int
    details: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    updated_at: datetime


class CandidateReplacementPreviewRead(BaseModel):
    id: str
    status: Literal["PREVIEW", "APPLIED"]
    group_id: str
    group_title: str
    candidate_item_id: str
    candidate_title: str
    replaced_item_ids: list[str] = Field(default_factory=list)
    target_date: date
    planned_minutes: int = 0
    candidate_minutes: int = 0
    capacity_minutes: int = 0
    over_capacity_minutes: int = 0
    blockers: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    can_apply: bool = False
    fingerprint: str


class TriggeredTaskRead(BaseModel):
    window_id: str
    work_item_id: str
    title: str
    description: str
    estimate_minutes: int
    condition: str
    eligible_from: date
    eligible_to: date
    status: str
    can_activate: bool = False
    blocked_reason: str | None = None
    fulfillment_mode: str = "ONCE"
    target_value: int = 1
    target_unit: str = "COUNT"
    completed_value: int = 0
    remaining_value: int = 0
    explanation: str = ""


class TriggeredTaskActivationRequest(BaseModel):
    window_id: str
    target_date: date
    evidence: str = Field(min_length=1, max_length=1000)

    @field_validator("evidence")
    @classmethod
    def evidence_must_be_non_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("请填写触发依据")
        return value


class TriggeredTaskActivationApply(BaseModel):
    preview_id: str = Field(min_length=1, max_length=36)


class TriggeredTaskActivationPreviewRead(BaseModel):
    id: str
    status: Literal["PREVIEW", "APPLIED"]
    window_id: str
    work_item_id: str
    title: str
    condition: str
    eligible_from: date
    eligible_to: date
    target_date: date | None = None
    fulfillment_mode: str = "ONCE"
    target_value: int = 1
    target_unit: str = "COUNT"
    completed_value: int = 0
    remaining_value: int = 0
    blockers: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    can_apply: bool = False
    fingerprint: str


class PlanWorkItemRead(BaseModel):
    id: str
    item_key: str
    milestone_id: str | None = None
    group_id: str | None = None
    source_work_item_id: str | None = None
    lineage_work_item_id: str | None = None
    scope_type: str
    stage_order: int | None = None
    title: str
    description: str
    task_kind: str
    estimate_minutes: int
    priority: int
    is_required: bool
    is_splittable: bool
    binding_mode: str
    fulfillment_mode: str
    target_value: int
    target_unit: str
    constraints: dict[str, Any] = Field(default_factory=dict)
    completion_rule: dict[str, Any] = Field(default_factory=dict)
    evidence: list[str] = Field(default_factory=list)
    source_refs: list[str] = Field(default_factory=list)
    commitment_refs: list[str] = Field(default_factory=list)
    dependency_ids: list[str] = Field(default_factory=list)
    status: str
    windows: list[WorkItemWindowRead] = Field(default_factory=list)


class AgentReportRead(BaseModel):
    id: str
    role: str
    status: str
    report: dict[str, Any] = Field(default_factory=dict)
    evidence: list[str] = Field(default_factory=list)
    confidence: float = 0.0
    input_units: int = 0
    output_units: int = 0
    latency_ms: int = 0
    error_code: str | None = None


class GenerationRunRead(BaseModel):
    id: str
    goal_id: str
    plan_version_id: str | None = None
    task_type: str
    tier: Literal["L0", "L1", "L2", "L3"]
    status: str
    context_hash: str
    decision: dict[str, Any] = Field(default_factory=dict)
    validation: dict[str, Any] = Field(default_factory=dict)
    input_units: int = 0
    output_units: int = 0
    estimated_cost_units: int = 0
    latency_ms: int = 0
    error_code: str | None = None
    error_message: str | None = None
    reports: list[AgentReportRead] = Field(default_factory=list)
    created_at: datetime
    completed_at: datetime | None = None


class PlanDayRead(BaseModel):
    id: str
    stage_order: int
    week_index: int
    day_index: int
    primary_goal: str
    suggested_minutes: int
    evidence: list[str]
    do_not: list[str]
    tasks: list[PlanTaskRead]


class PlanMilestoneRead(BaseModel):
    id: str
    route_stage_id: str
    stage_order: int
    title: str
    purpose: str
    start_week: int
    end_week: int
    outputs: list[str]


class PlanVersionRead(BaseModel):
    id: str
    route_version_id: str
    version: int
    revision: int
    status: str
    summary: str
    total_weeks: int
    weekly_execution_days: int
    weekly_minutes: int
    total_estimated_minutes: int
    buffer_strategy: list[str]
    assumptions: list[str]
    open_questions: list[str]
    change_summary: list[str]
    temporal_segments: list[PlanTemporalSegmentDraft] = Field(default_factory=list)
    commitments: list[PlanCommitmentDraft] = Field(default_factory=list)
    source_document: str | None = None
    compiler_report: dict[str, Any] = Field(default_factory=dict)
    planning_mode: Literal["EXPLORATORY", "COMMITTED"] = "COMMITTED"
    schedule_type: Literal["OUTCOME", "RHYTHM", "CALENDAR"] = "RHYTHM"
    execution_model_version: Literal["LEGACY_DAY_BASED", "HYBRID_WORK_ITEM"] = "LEGACY_DAY_BASED"
    exploration_window_weeks: int = 1
    next_unplanned_week: int = 1
    work_item_groups: list[PlanWorkItemGroupRead] = Field(default_factory=list)
    work_items: list[PlanWorkItemRead] = Field(default_factory=list)
    milestones: list[PlanMilestoneRead]
    days: list[PlanDayRead]


class PlanRevisionRead(BaseModel):
    id: str
    version: int
    revision: int
    status: str
    change_summary: list[str]
    feedback: str | None
    created_at: datetime


class PlanGenerationRequest(BaseModel):
    mode: Literal["STANDARD", "DEEP"]


class PlanGenerationJobRead(BaseModel):
    id: str
    mode: Literal["STANDARD", "DEEP"]
    planning_mode: Literal["EXPLORATORY", "COMMITTED"] = "COMMITTED"
    schedule_type: Literal["OUTCOME", "RHYTHM", "CALENDAR"] = "RHYTHM"
    status: Literal["QUEUED", "RUNNING", "COMPLETED", "FAILED"]
    phase: Literal["OUTLINE", "DETAILS", "COMPLETE"]
    generated_weeks: int
    total_weeks: int
    error_code: str | None = None
    error_message: str | None = None
    created_at: datetime
    updated_at: datetime


class ExplorationWindowRequest(BaseModel):
    mode: Literal["STANDARD", "DEEP"] = "STANDARD"


class PlanWindowRead(BaseModel):
    id: str
    plan_version_id: str
    sequence: int
    start_week: int
    end_week: int
    status: Literal["DRAFT", "ACTIVE", "CLOSED", "DISCARDED"]
    trigger: str
    basis: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    published_at: datetime | None = None


class ContextPackRead(BaseModel):
    id: str
    goal_id: str
    requirement_version_id: str
    route_version_id: str
    plan_version_id: str | None
    version: int
    facts: dict[str, Any]
    locked_day_ids: list[str] = Field(default_factory=list)
    locked_task_ids: list[str] = Field(default_factory=list)
    created_at: datetime


class PlanningBriefRead(BaseModel):
    id: str
    goal_id: str
    context_pack_id: str
    plan_version_id: str | None
    brief_kind: str
    version: int
    status: str
    brief: dict[str, Any]
    context: ContextPackRead
    created_at: datetime
    confirmed_at: datetime | None


class GenerationBatchRead(BaseModel):
    id: str
    goal_id: str
    plan_version_id: str | None
    brief_id: str
    idempotency_key: str
    scope_type: str
    scope: dict[str, Any]
    status: str
    attempt_count: int
    failure_code: str | None
    failure_message: str | None
    staged_result: dict[str, Any] | None
    created_at: datetime
    updated_at: datetime


class GenerationBatchCreate(BaseModel):
    idempotency_key: str = Field(min_length=1, max_length=160)
    scope_type: str = Field(min_length=1, max_length=24)
    scope: dict[str, Any] = Field(default_factory=dict)


class GenerationBatchRetry(BaseModel):
    batch_id: str = Field(min_length=1, max_length=36)


class GenerationArtifactEnvelope(BaseModel):
    """Durable checkpoint shared by Hybrid author, compiler, and validator stages."""

    model_config = ConfigDict(extra="forbid")

    artifact_version: Literal[1] = 1
    execution_model: Literal["HYBRID_WORK_ITEM"] = "HYBRID_WORK_ITEM"
    phase: Literal[
        "CONTEXT_READY",
        "AUTHOR_COMPLETE",
        "COMPILED",
        "VALIDATED",
        "COMPLETED",
    ]
    context_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    prompt_version: str = Field(min_length=1, max_length=80)
    source_document: str | None = Field(default=None, max_length=60000)
    compiled_result: dict[str, Any] | None = None
    validation: dict[str, Any] = Field(default_factory=dict)
    retry_scope: dict[str, Any] = Field(default_factory=dict)


class PlanResearchRead(BaseModel):
    """Bounded research status projected into the plan workspace."""

    id: str
    status: str
    current_stage: str
    question: str
    stop_reason: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    result_status: str | None = None
    source_count: int = 0
    read_pages: int = 0
    unresolved_points: list[str] = Field(default_factory=list, max_length=10)
    sources: list[dict[str, Any]] = Field(default_factory=list, max_length=12)
    claims: list[dict[str, Any]] = Field(default_factory=list, max_length=12)
    coverage: dict[str, Any] = Field(default_factory=dict)


class PlanRecoveryGuardRead(BaseModel):
    required: bool = False
    message: str = ""
    missed_execution_days: int = 0
    interrupted_execution_days: int = 0
    missed_dates: list[date] = Field(default_factory=list, max_length=60)
    recovery_deferred: bool = False


class PlanGenerationRepairRead(BaseModel):
    week_index: int | None = None
    attempt: int = Field(ge=2, le=3)
    maximum_attempts: int = 3
    message: str


class PlanWorkspace(BaseModel):
    goal_id: str
    goal_title: str
    status: str
    requirement_version: int
    requirement_snapshot: dict[str, Any]
    route_version: int
    route_revision: int
    plan: PlanVersionRead | None = None
    messages: list[MessageRead] = Field(default_factory=list)
    revision_history: list[PlanRevisionRead] = Field(default_factory=list)
    pending_adjustment: PlanAdjustmentProposal | None = None
    recovery_review: dict[str, Any] | None = None
    generation_phase: Literal["OUTLINE", "DETAILS", "COMPLETE"]
    generated_weeks: int = 0
    total_weeks: int = 0
    generation_target_weeks: int = 0
    planning_mode: Literal["EXPLORATORY", "COMMITTED"] = "COMMITTED"
    schedule_type: Literal["OUTCOME", "RHYTHM", "CALENDAR"] = "RHYTHM"
    exploration_windows: list[PlanWindowRead] = Field(default_factory=list)
    pending_exploration_window: PlanWindowRead | None = None
    can_extend_exploration: bool = False
    available_generation_modes: list[Literal["STANDARD", "DEEP"]] = Field(
        default_factory=lambda: ["STANDARD"]
    )
    generation_job: PlanGenerationJobRead | None = None
    generation_repair: PlanGenerationRepairRead | None = None
    capacity_negotiation: dict[str, Any] | None = None
    latest_generation_run: GenerationRunRead | None = None
    latest_research: PlanResearchRead | None = None
    recovery_guard: PlanRecoveryGuardRead = Field(default_factory=PlanRecoveryGuardRead)
    pending_adjustment_preview: PlanAdjustmentPreviewRead | None = None


class ExecutionScheduleRequest(BaseModel):
    requested_start_date: date
    execution_weekdays: list[int] = Field(min_length=1, max_length=7)
    excluded_dates: list[date] = Field(default_factory=list, max_length=60)
    smart_preview_id: str | None = None
    materialization_preview_id: str | None = None
    selected_work_item_ids: list[str] = Field(default_factory=list, max_length=200)

    @model_validator(mode="after")
    def schedule_values_are_unique(self) -> "ExecutionScheduleRequest":
        if self.execution_weekdays != sorted(set(self.execution_weekdays)):
            raise ValueError("Execution weekdays must be sorted and unique")
        if any(day < 0 or day > 6 for day in self.execution_weekdays):
            raise ValueError("Execution weekday must be between zero and six")
        if self.excluded_dates != sorted(set(self.excluded_dates)):
            raise ValueError("Excluded dates must be sorted and unique")
        return self


class SmartCalendarRequest(BaseModel):
    instructions: str = Field(default="", max_length=4000)
    preferred_start_date: date | None = None


class CalendarDateAssignment(BaseModel):
    plan_day_id: str
    scheduled_date: date


class SmartCalendarResult(BaseModel):
    requested_start_date: date
    execution_weekdays: list[int] = Field(min_length=1, max_length=7)
    excluded_dates: list[date] = Field(default_factory=list, max_length=60)
    assignments: list[CalendarDateAssignment] = Field(min_length=1, max_length=730)
    rationale: str = Field(min_length=1, max_length=2000)
    rules: list[str] = Field(default_factory=list, max_length=30)
    assumptions: list[str] = Field(default_factory=list, max_length=20)
    questions: list[str] = Field(default_factory=list, max_length=10)
    requires_clarification: bool = False


class CalendarContentConstraint(BaseModel):
    day_codes: list[str] = Field(min_length=1, max_length=730)
    allowed_weekdays: list[int] = Field(min_length=1, max_length=7)
    not_before: date | None = None
    not_after: date | None = None


class CalendarIntentResult(BaseModel):
    requested_start_date: date
    execution_weekdays: list[int] = Field(min_length=1, max_length=7)
    excluded_dates: list[date] = Field(default_factory=list, max_length=60)
    constraints: list[CalendarContentConstraint] = Field(default_factory=list, max_length=60)
    rationale: str = Field(min_length=1, max_length=2000)
    rules: list[str] = Field(default_factory=list, max_length=30)
    assumptions: list[str] = Field(default_factory=list, max_length=20)
    questions: list[str] = Field(default_factory=list, max_length=10)
    requires_clarification: bool = False


class SmartCalendarRead(BaseModel):
    preview_id: str
    input: ExecutionScheduleRequest
    preview: "ExecutionPreview"
    days: list["ScheduledDayRead"]
    rationale: str
    rules: list[str]
    assumptions: list[str]
    questions: list[str]
    can_apply: bool


class ExecutionDayMoveRequest(BaseModel):
    mode: Literal["ADVANCE", "SWAP"]
    selected_day_id: str


class ExecutionDayMoveRead(BaseModel):
    preview_id: str
    mode: Literal["ADVANCE", "SWAP"]
    can_apply: bool
    blockers: list[str]
    warnings: list[str] = Field(default_factory=list)
    completed_task_count: int
    changes: list["RecoveryDayChangeRead"]


class ExecutionDayMoveApply(BaseModel):
    preview_id: str


ScheduleMode = Literal["FLEXIBLE", "BLOCKS", "FIXED"]
SupervisionMode = Literal["QUIET", "STANDARD", "INTENSIVE"]


def _validate_local_time(value: str) -> str:
    try:
        hour, minute = value.split(":", 1)
        if len(hour) != 2 or len(minute) != 2:
            raise ValueError
        parsed_hour, parsed_minute = int(hour), int(minute)
        if not (0 <= parsed_hour <= 23 and 0 <= parsed_minute <= 59):
            raise ValueError
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError("Time must use HH:MM format") from exc
    return value


class SchedulePreferenceRead(BaseModel):
    id: str
    goal_id: str
    mode: ScheduleMode
    timezone: str
    break_minutes: int
    allow_cross_window: bool
    reminders_enabled: bool
    supervision_mode: SupervisionMode
    cross_plan_priority_mode: Literal["PRIORITY", "DEADLINE", "BALANCED"] = "BALANCED"
    quiet_start_local: str | None = None
    quiet_end_local: str | None = None
    muted_commitments: list[str] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime


class SchedulePreferenceUpdate(BaseModel):
    mode: ScheduleMode | None = None
    timezone: str | None = Field(default=None, min_length=1, max_length=64)
    break_minutes: int | None = Field(default=None, ge=0, le=120)
    allow_cross_window: bool | None = None
    reminders_enabled: bool | None = None
    supervision_mode: SupervisionMode | None = None
    cross_plan_priority_mode: Literal["PRIORITY", "DEADLINE", "BALANCED"] | None = None
    quiet_start_local: str | None = None
    quiet_end_local: str | None = None

    @field_validator("timezone")
    @classmethod
    def timezone_must_be_valid(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:
            raise ValueError("Timezone must be a valid IANA timezone") from exc
        return value

    @field_validator("quiet_start_local", "quiet_end_local")
    @classmethod
    def quiet_time_must_be_valid(cls, value: str | None) -> str | None:
        return _validate_local_time(value) if value is not None else None


class CommitmentReminderUpdate(BaseModel):
    label: str = Field(min_length=1, max_length=200)
    enabled: bool = True


class AvailabilityWindowInput(BaseModel):
    weekday: int = Field(ge=0, le=6)
    start_local: str
    end_local: str
    valid_from: date | None = None
    valid_to: date | None = None

    @field_validator("start_local", "end_local")
    @classmethod
    def local_time_must_be_valid(cls, value: str) -> str:
        return _validate_local_time(value)

    @model_validator(mode="after")
    def window_must_be_ordered(self) -> "AvailabilityWindowInput":
        if self.start_local >= self.end_local:
            raise ValueError("Window end must be later than its start")
        if self.valid_from and self.valid_to and self.valid_from > self.valid_to:
            raise ValueError("valid_from must be on or before valid_to")
        return self


class AvailabilityWindowRead(AvailabilityWindowInput):
    id: str
    goal_id: str
    timezone: str
    source: str
    created_at: datetime
    updated_at: datetime


class AvailabilityWindowsUpdate(BaseModel):
    timezone: str | None = None
    windows: list[AvailabilityWindowInput] = Field(default_factory=list, max_length=100)

    @field_validator("timezone")
    @classmethod
    def timezone_must_be_valid(cls, value: str | None) -> str | None:
        if value is None:
            return value
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:
            raise ValueError("Timezone must be a valid IANA timezone") from exc
        return value


class AvailabilityExceptionRead(BaseModel):
    id: str
    goal_id: str
    exception_date: date
    kind: Literal["UNAVAILABLE", "OVERRIDE"]
    start_local: str | None
    end_local: str | None
    reason: str | None
    source: str
    created_at: datetime


class ScheduleFoundationRead(BaseModel):
    preference: SchedulePreferenceRead
    availability_windows: list[AvailabilityWindowRead] = Field(default_factory=list)
    availability_exceptions: list[AvailabilityExceptionRead] = Field(default_factory=list)


class PlanScheduleAllocationRead(BaseModel):
    scheduled_days: int
    assigned_tasks: int
    unallocated_tasks: int
    schedule: list[dict[str, Any]] = Field(default_factory=list)


class WorkspaceScheduleProfileRead(BaseModel):
    id: str
    timezone: str
    default_mode: ScheduleMode
    break_minutes: int
    availability_mode: Literal["ROUTINE", "CUSTOM"] = "ROUTINE"
    wake_time: str = "07:00"
    available_from: str = "08:00"
    breakfast_enabled: bool = True
    breakfast_start: str = "07:30"
    breakfast_end: str = "08:00"
    lunch_enabled: bool = True
    lunch_start: str = "12:00"
    lunch_end: str = "13:00"
    nap_enabled: bool = True
    nap_start: str = "13:00"
    nap_end: str = "14:00"
    dinner_enabled: bool = True
    dinner_start: str = "18:00"
    dinner_end: str = "19:00"
    sleep_time: str = "23:00"
    routine_exclusions: list[dict[str, Any]] = Field(default_factory=list)
    schedule_style: str = ""
    holiday_country_code: str | None = None
    holiday_subdivision_code: str | None = None
    holiday_locale: str | None = None
    holiday_source: str = "UNSET"
    holiday_confidence: float = 0.0
    holiday_confirmed: bool = False
    created_at: datetime
    updated_at: datetime


class WorkspaceScheduleProfileUpdate(BaseModel):
    timezone: str | None = Field(default=None, min_length=1, max_length=64)
    default_mode: ScheduleMode | None = None
    break_minutes: int | None = Field(default=None, ge=0, le=120)
    availability_mode: Literal["ROUTINE", "CUSTOM"] | None = None
    wake_time: str | None = None
    available_from: str | None = None
    breakfast_enabled: bool | None = None
    breakfast_start: str | None = None
    breakfast_end: str | None = None
    lunch_enabled: bool | None = None
    lunch_start: str | None = None
    lunch_end: str | None = None
    nap_enabled: bool | None = None
    nap_start: str | None = None
    nap_end: str | None = None
    dinner_enabled: bool | None = None
    dinner_start: str | None = None
    dinner_end: str | None = None
    sleep_time: str | None = None
    routine_exclusions: list[dict[str, Any]] | None = Field(default=None, max_length=20)
    schedule_style: str | None = Field(default=None, max_length=2000)
    holiday_country_code: str | None = Field(default=None, min_length=2, max_length=2)
    holiday_subdivision_code: str | None = Field(default=None, max_length=16)
    holiday_locale: str | None = Field(default=None, max_length=32)
    holiday_source: Literal["UNSET", "AUTO", "USER"] | None = None
    holiday_confidence: float | None = Field(default=None, ge=0, le=1)
    holiday_confirmed: bool | None = None

    @field_validator(
        "wake_time",
        "available_from",
        "breakfast_start",
        "breakfast_end",
        "lunch_start",
        "lunch_end",
        "nap_start",
        "nap_end",
        "dinner_start",
        "dinner_end",
        "sleep_time",
    )
    @classmethod
    def routine_time_must_be_valid(cls, value: str | None) -> str | None:
        return _validate_local_time(value) if value is not None else None

    @field_validator("routine_exclusions")
    @classmethod
    def routine_exclusions_must_be_valid(cls, value: list[dict[str, Any]] | None):
        if value is None:
            return value
        normalized = []
        for item in value:
            label = str(item.get("label", "")).strip()
            start = _validate_local_time(str(item.get("start_local", "")))
            end = _validate_local_time(str(item.get("end_local", "")))
            if not label or len(label) > 40 or start >= end:
                raise ValueError("Each unavailable period needs a short name and valid time range")
            normalized.append(
                {
                    "label": label,
                    "start_local": start,
                    "end_local": end,
                    "enabled": bool(item.get("enabled", True)),
                }
            )
        return normalized

    @field_validator("holiday_country_code")
    @classmethod
    def country_code_must_be_iso_like(cls, value: str | None) -> str | None:
        if value is None:
            return value
        normalized = value.upper()
        if not normalized.isalpha():
            raise ValueError("Holiday country must use a two-letter country code")
        return normalized

    @field_validator("timezone")
    @classmethod
    def timezone_must_be_valid(cls, value: str | None) -> str | None:
        if value is None:
            return value
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:
            raise ValueError("Timezone must be a valid IANA timezone") from exc
        return value


class WorkspaceAvailabilityUpdate(BaseModel):
    windows: list[AvailabilityWindowInput] = Field(default_factory=list, max_length=200)


class WorkspaceAvailabilityWindowRead(AvailabilityWindowInput):
    id: str
    profile_id: str
    timezone: str
    created_at: datetime


class WorkspaceEffectiveAvailabilityRead(BaseModel):
    source: Literal["ROUTINE", "CUSTOM"]
    windows: list[AvailabilityWindowInput] = Field(default_factory=list)


class ModelConnectionRead(BaseModel):
    provider: Literal["fake", "deepseek", "openai_compatible"]
    base_url: str
    proxy_url: str
    model: str
    api_key_configured: bool
    api_key_hint: str | None = None


class ModelConnectionUpdate(BaseModel):
    provider: Literal["fake", "deepseek", "openai_compatible"]
    base_url: str = Field(min_length=8, max_length=500)
    proxy_url: str = Field(default="", max_length=500)
    model: str = Field(min_length=1, max_length=160)
    api_key: SecretStr | None = None
    clear_api_key: bool = False

    @field_validator("base_url")
    @classmethod
    def base_url_must_be_http(cls, value: str) -> str:
        normalized = value.strip().rstrip("/")
        if not normalized.startswith(("http://", "https://")):
            raise ValueError("Model base URL must start with http:// or https://")
        return normalized

    @field_validator("proxy_url")
    @classmethod
    def proxy_url_must_be_http(cls, value: str) -> str:
        normalized = value.strip().rstrip("/")
        if normalized and not normalized.startswith(("http://", "https://")):
            raise ValueError("Proxy URL must start with http:// or https://")
        return normalized


class HolidayCalendarDayRead(BaseModel):
    date: date
    kind: Literal["HOLIDAY", "WORKDAY"]
    name: str
    local_name: str
    country_code: str
    subdivision_code: str | None = None
    source: str
    authoritative: bool


class HolidayCalendarRead(BaseModel):
    country_code: str | None
    subdivision_code: str | None
    start_date: date
    end_date: date
    days: list[HolidayCalendarDayRead] = Field(default_factory=list)
    coverage: Literal["COMPLETE", "PARTIAL", "UNAVAILABLE"] = "UNAVAILABLE"
    missing_years: list[int] = Field(default_factory=list)
    sources: list[str] = Field(default_factory=list)


class HolidayRegionSuggestionRead(BaseModel):
    country_code: str | None
    timezone_country_code: str | None
    locale_country_code: str | None
    confidence: float
    requires_confirmation: bool
    reason: Literal[
        "TIMEZONE_LOCALE_AGREE",
        "TIMEZONE_LOCALE_CONFLICT",
        "TIMEZONE_ONLY",
        "LOCALE_ONLY",
        "UNKNOWN",
    ]


class ManualTaskCreate(BaseModel):
    title: str = Field(min_length=1, max_length=180)
    description: str = ""
    estimate_minutes: int = Field(ge=1, le=1440)
    priority: int = Field(default=3, ge=1, le=5)
    scheduled_date: date | None = None
    start_local: str | None = None
    end_local: str | None = None
    recurrence: Literal["ONCE", "DAILY", "WEEKLY"] = "ONCE"
    recurrence_weekdays: list[int] = Field(default_factory=list, max_length=7)
    recurrence_end_date: date | None = None
    flexible_recurrence: bool = False

    @field_validator("start_local", "end_local")
    @classmethod
    def optional_time_must_be_valid(cls, value: str | None) -> str | None:
        return _validate_local_time(value) if value is not None else None

    @model_validator(mode="after")
    def block_must_be_complete(self) -> "ManualTaskCreate":
        if (self.start_local is None) != (self.end_local is None):
            raise ValueError("start_local and end_local must be provided together")
        if self.start_local is not None and self.start_local >= self.end_local:
            raise ValueError("Task end must be later than its start")
        if self.recurrence != "ONCE" and self.scheduled_date is None:
            raise ValueError("Recurring tasks require a start date")
        if (
            self.recurrence != "ONCE"
            and not self.flexible_recurrence
            and (self.start_local is None or self.end_local is None)
        ):
            raise ValueError("Recurring tasks require a fixed time block")
        if self.recurrence == "WEEKLY" and not self.recurrence_weekdays:
            raise ValueError("Weekly tasks require at least one weekday")
        if any(day < 0 or day > 6 for day in self.recurrence_weekdays):
            raise ValueError("Weekdays must be between 0 and 6")
        if (
            self.recurrence_end_date
            and self.scheduled_date
            and self.recurrence_end_date < self.scheduled_date
        ):
            raise ValueError("Recurrence end date must be on or after the start date")
        return self


class ManualPlanCreate(BaseModel):
    title: str = Field(min_length=1, max_length=160)
    description: str = ""
    timezone: str = Field(default="Asia/Shanghai", min_length=1, max_length=64)
    tasks: list[ManualTaskCreate] = Field(default_factory=list, max_length=200)
    conflict_mode: Literal["NONE", "PARALLEL", "PUSH"] = "NONE"

    @field_validator("timezone")
    @classmethod
    def timezone_must_be_valid(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:
            raise ValueError("Timezone must be a valid IANA timezone") from exc
        return value


class ManualTaskDateOverrideRead(BaseModel):
    override_date: date
    title: str | None = None
    description: str | None = None
    estimate_minutes: int | None = None
    priority: int | None = None
    start_local: str | None = None
    end_local: str | None = None
    time_overridden: bool = False
    status: str | None = None
    is_deleted: bool = False


class ManualTaskRead(ManualTaskCreate):
    id: str
    manual_plan_id: str
    quick_plan_id: str
    order_in_plan: int
    status: str
    created_at: datetime
    updated_at: datetime
    overrides: list[ManualTaskDateOverrideRead] = Field(default_factory=list)


class ManualTaskUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=180)
    scheduled_date: date | None = None
    start_local: str | None = None
    end_local: str | None = None
    conflict_mode: Literal["NONE", "PARALLEL", "PUSH"] = "NONE"
    scope: Literal["TODAY", "FUTURE"] = "FUTURE"
    status: Literal["NOT_STARTED", "COMPLETED", "SKIPPED"] | None = None
    description: str | None = Field(default=None, max_length=4000)
    estimate_minutes: int | None = Field(default=None, ge=1, le=1440)
    priority: int | None = Field(default=None, ge=1, le=5)


class ManualTaskDelete(BaseModel):
    scope: Literal["TODAY", "FUTURE"] = "FUTURE"
    override_date: date | None = None


class ManualPlanRead(BaseModel):
    id: str
    title: str
    description: str
    status: str
    timezone: str
    kind: Literal["QUICK"] = "QUICK"
    arrangement: dict[str, object] = Field(default_factory=dict)
    tasks: list[ManualTaskRead] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime


class ScheduledDayRead(BaseModel):
    plan_day_id: str
    stage_order: int
    week_index: int
    day_index: int
    code: str
    scheduled_date: date
    primary_goal: str


class WorkItemDailyTaskPreviewRead(BaseModel):
    work_item_id: str
    stage_order: int | None = None
    title: str
    description: str
    estimate_minutes: int
    priority: int
    task_kind: str
    is_required: bool
    binding_mode: str
    evidence: list[str] = Field(default_factory=list)
    period_key: str
    occurrence_index: int


class WorkItemDailyDayPreviewRead(BaseModel):
    scheduled_date: date
    week_index: int
    day_index: int
    code: str
    suggested_minutes: int
    tasks: list[WorkItemDailyTaskPreviewRead] = Field(default_factory=list)


class WorkItemDailyPreviewRead(BaseModel):
    plan_id: str
    candidate_start_date: date
    projected_end_date: date | None = None
    execution_weekdays: list[int]
    days: list[WorkItemDailyDayPreviewRead] = Field(default_factory=list)
    pending_trigger_items: list[WorkItemDailyTaskPreviewRead] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class CalendarTaskRead(BaseModel):
    id: str
    title: str
    status: str
    is_required: bool
    estimate_minutes: int
    origin_stage_order: int | None = None
    scheduling_policy: str = "EXACT_DAY"
    scheduling_policy_data: dict[str, Any] = Field(default_factory=dict)
    can_arrange_today: bool = False
    arrange_blocked_reason: str | None = None
    recommendation_group_key: str | None = None
    recommendation_position: int | None = None
    recommendation_total: int | None = None
    is_recommendation_primary: bool = True


class CalendarChangeRead(BaseModel):
    adjustment_kind: str
    direction: str
    summary: str
    rationale: str
    counterpart_date: date | None = None


class ExecutionCalendarDayRead(BaseModel):
    plan_day_id: str
    scheduled_date: date
    code: str
    task_stage_orders: list[int] = Field(default_factory=list)
    primary_goal: str
    status: Literal["PLANNED", "IN_PROGRESS", "BLOCKED", "COMPLETED", "PARTIAL", "MISSED"]
    suggested_minutes: int
    tasks: list[CalendarTaskRead] = Field(default_factory=list)
    changes: list[CalendarChangeRead] = Field(default_factory=list)


class ExecutionCalendarRead(BaseModel):
    first_date: date
    last_date: date
    days: list[ExecutionCalendarDayRead] = Field(default_factory=list)


class ExecutionPreview(BaseModel):
    requested_start_date: date
    first_execution_date: date
    projected_end_date: date
    execution_weekdays: list[int]
    excluded_dates: list[date]
    total_execution_days: int
    upcoming_days: list[ScheduledDayRead]
    materialization_preview_id: str | None = None


class ExecutionSetup(BaseModel):
    has_phased_capacity: bool = False
    goal_id: str
    goal_title: str
    status: str
    plan_id: str
    plan_revision: int
    total_weeks: int
    weekly_execution_days: int
    schedule_type: Literal["OUTCOME", "RHYTHM", "CALENDAR"] = "RHYTHM"
    execution_model_version: Literal["LEGACY_DAY_BASED", "HYBRID_WORK_ITEM"] = "LEGACY_DAY_BASED"
    has_daily_plan: bool = True
    minimum_execution_days: int = Field(default=1, ge=1, le=7)
    suggested_weekdays: list[int]
    activation: ExecutionPreview | None = None
    timezone: str = "Asia/Shanghai"
    current_date: date | None = None
    suggested_start_date: date | None = None
    schedule_preference: SchedulePreferenceRead | None = None
    availability_windows: list[AvailabilityWindowRead] = Field(default_factory=list)


class ProgressSummary(BaseModel):
    completed_tasks: int
    total_tasks: int
    completed_required_tasks: int
    total_required_tasks: int
    percentage: int


class TodayCapacityRead(BaseModel):
    planned_minutes: int
    remaining_minutes: int
    remaining_tasks: int
    load_status: str


class CommitmentProgressRead(BaseModel):
    """Current progress for a measurable commitment inside a mixed-rule plan."""

    label: str
    cadence: Literal["TOTAL", "PER_WEEK", "PER_EXECUTION_DAY"]
    period_start: date | None = None
    period_end: date | None = None
    target_occurrences: int = Field(ge=0)
    completed_occurrences: int = Field(ge=0)
    remaining_occurrences: int = Field(ge=0)
    completed_minutes: int = Field(default=0, ge=0)
    partial_minutes: int = Field(default=0, ge=0)
    remaining_minutes: int = Field(default=0, ge=0)
    risk: Literal["COMPLETE", "ON_TRACK", "DUE_SOON", "AT_RISK", "NO_DEADLINE"]
    source_text: str = ""


class CommitmentAlertRead(BaseModel):
    """Actionable supervision signal derived from commitment progress."""

    label: str
    risk: Literal["DUE_SOON", "AT_RISK"]
    severity: Literal["WARNING", "URGENT"]
    title: str
    message: str
    action: Literal["DISCUSS", "REVIEW_PLAN"]
    period_end: date | None = None
    remaining_occurrences: int = Field(ge=0)
    dedupe_key: str = ""
    event_id: str | None = None
    status: Literal["OPEN", "READ", "DISMISSED"] = "OPEN"


class ReminderEventRead(BaseModel):
    id: str
    goal_id: str
    plan_version_id: str | None = None
    dedupe_key: str
    label: str
    risk: Literal["DUE_SOON", "AT_RISK"]
    severity: Literal["WARNING", "URGENT"]
    title: str
    message: str
    action: Literal["DISCUSS", "REVIEW_PLAN"]
    period_end: date | None = None
    remaining_occurrences: int = Field(ge=0)
    status: Literal["OPEN", "READ", "DISMISSED"]
    first_seen_at: datetime
    last_seen_at: datetime
    read_at: datetime | None = None
    dismissed_at: datetime | None = None


class ReminderEventAction(BaseModel):
    action: Literal["READ", "DISMISS", "REOPEN"]


class FocusSessionCreate(BaseModel):
    task_id: str | None = None
    planned_minutes: int = Field(ge=1, le=180)
    actual_seconds: int = Field(ge=1, le=86400)
    interruptions: int = Field(default=0, ge=0, le=999)


class FocusSessionRead(BaseModel):
    id: str
    task_id: str | None
    task_title: str | None
    planned_minutes: int
    actual_seconds: int
    interruptions: int
    status: str
    completed_at: datetime


class QuickCaptureCreate(BaseModel):
    content: str = Field(min_length=1, max_length=500)

    @field_validator("content")
    @classmethod
    def normalize_content(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Capture content cannot be blank")
        return value


class QuickCaptureRead(BaseModel):
    id: str
    content: str
    status: str
    created_at: datetime


TaskFeedbackReason = Literal[
    "TEMPORARY_CONFLICT",
    "TOO_HARD",
    "NOT_ENOUGH_TIME",
    "UNCLEAR",
    "DO_NOT_WANT",
    "OTHER",
]
DayPaceReason = Literal[
    "EARLY_FINISH",
    "TOO_EASY",
    "TOO_LIGHT",
    "JUST_RIGHT",
    "TOO_HARD_OVERALL",
    "TOO_HEAVY",
    "CUSTOM_ADJUSTMENT",
]
FeedbackIntent = Literal[
    "RESCHEDULE_ONLY",
    "RECOVER_ON_REST_DAY",
    "SPLIT_TASK",
    "LOWER_NEAR_TERM",
    "EXPLAIN_FIRST",
    "CHANGE_METHOD",
    "REDUCE_SCOPE",
    "SKIP_FOR_NOW",
    "REVISIT_ROUTE",
    "ACCELERATE",
    "DEEPEN",
    "MORE_PRACTICE",
    "OBSERVE",
]


class TaskFeedbackCreate(BaseModel):
    reason_code: TaskFeedbackReason
    intent_code: FeedbackIntent | None = None
    detail_text: str | None = Field(default=None, max_length=2000)
    remaining_minutes: int | None = Field(default=None, ge=1, le=1440)

    @model_validator(mode="after")
    def other_reason_has_a_detail(self) -> "TaskFeedbackCreate":
        if self.reason_code == "OTHER" and not (self.detail_text or "").strip():
            raise ValueError("Other feedback requires a short detail")
        if self.detail_text is not None:
            self.detail_text = self.detail_text.strip() or None
        return self


class TaskProgressUpdate(BaseModel):
    action: Literal["PARTIAL", "SKIP", "DEFER"]
    amount: int | None = Field(default=None, ge=1, le=1440)
    unit: Literal["COUNT", "MINUTES"] = "MINUTES"
    reason: str | None = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def validate_progress_action(self) -> "TaskProgressUpdate":
        if self.action == "PARTIAL" and self.amount is None:
            raise ValueError("Partial progress requires an amount")
        if self.action in {"SKIP", "DEFER"} and not (self.reason or "").strip():
            raise ValueError("Skip or defer requires a reason")
        self.reason = self.reason.strip() if self.reason else None
        return self


class DayPaceFeedbackCreate(BaseModel):
    reason_code: DayPaceReason
    intent_code: FeedbackIntent | None = None
    detail_text: str | None = Field(default=None, max_length=2000)

    @field_validator("detail_text")
    @classmethod
    def normalize_optional_detail(cls, value: str | None) -> str | None:
        return value.strip() or None if value is not None else None

    @model_validator(mode="after")
    def custom_adjustment_has_a_detail(self) -> "DayPaceFeedbackCreate":
        if self.reason_code == "CUSTOM_ADJUSTMENT" and not self.detail_text:
            raise ValueError("Custom adjustment feedback requires a short detail")
        return self


class FeedbackRead(BaseModel):
    id: str
    task_id: str | None
    feedback_kind: str
    reason_code: str
    intent_code: str | None
    detail_text: str | None
    status: str
    created_at: datetime


class FeedbackAdjustmentRead(BaseModel):
    id: str
    feedback_id: str
    adjustment_kind: str
    status: str
    title: str
    summary: str
    rationale: str
    impact: list[str] = Field(default_factory=list)
    created_at: datetime
    decided_at: datetime | None


class ExecutionDayLogRead(BaseModel):
    id: str
    scheduled_date: date
    status: str
    summary: str
    metrics: dict[str, int | str]
    adjustments: list[str] = Field(default_factory=list)
    updated_at: datetime


class ExecutionProgressDayRead(BaseModel):
    scheduled_date: date
    code: str
    stage_order: int
    stage_title: str
    primary_goal: str
    status: str
    completed_tasks: int
    total_tasks: int
    carried_over_tasks: int = 0
    focus_minutes: int
    feedback_count: int
    summary: str | None = None


class ExecutionFeedbackTimelineRead(BaseModel):
    id: str
    scheduled_date: date
    task_title: str | None = None
    feedback_kind: str
    reason_code: str
    intent_code: str | None = None
    detail_text: str | None = None
    status: str
    created_at: datetime
    adjustment: FeedbackAdjustmentRead | None = None


class ExecutionPlanRevisionRead(BaseModel):
    id: str
    revision: int
    status: str
    feedback: str | None = None
    changes: list[str] = Field(default_factory=list)
    created_at: datetime


class ExecutionTrendRead(BaseModel):
    window_days: int
    completed_days: int
    completion_rate: int
    consecutive_struggle_days: int
    consecutive_easy_days: int
    planned_minutes: int
    focus_minutes: int
    status: str
    summary: str
    should_offer_adjustment: bool = False
    recommendation_title: str | None = None
    recommendation_request: str | None = None


class RhythmProgressRead(BaseModel):
    cycle_start: date
    cycle_end: date
    target_sessions: int
    completed_sessions: int
    remaining_sessions: int
    observed_cycles: int
    average_completed_sessions: float
    adherence_rate: int
    planned_minutes: int
    focus_minutes: int
    pace_status: Literal["LEARNING", "BELOW", "SUSTAINABLE", "ABOVE"]
    forecast_summary: str


class ExecutionReviewSignalRead(BaseModel):
    recommended: bool = False
    trigger_type: Literal[
        "NONE",
        "OUTCOME_CHECKPOINT",
        "RHYTHM_SAMPLE_READY",
        "RHYTHM_MISMATCH",
        "DEADLINE_RISK",
        "USER_REQUEST",
        "DIRECTION_CHANGE",
    ] = "NONE"
    reason: str | None = None
    timing: Literal["NOW", "FUTURE"] = "NOW"
    not_before: date | None = None

    @model_validator(mode="after")
    def future_review_has_a_date(self) -> "ExecutionReviewSignalRead":
        if self.timing == "FUTURE" and self.not_before is None:
            raise ValueError("A future review signal needs not_before")
        return self


InactivityLevel = Literal["NONE", "MISSED_ONCE", "PAUSED_3_DAYS", "PAUSED_7_DAYS"]


class ExecutionInactivityRead(BaseModel):
    level: InactivityLevel
    inactive_days: int = 0
    missed_execution_days: int = 0
    missed_dates: list[date] = Field(default_factory=list)
    partial_execution_days: int = 0
    partial_dates: list[date] = Field(default_factory=list)
    interrupted_execution_days: int = 0
    interrupted_dates: list[date] = Field(default_factory=list)
    last_activity_date: date | None = None
    latest_missed_date: date | None = None
    recovery_deferred: bool = False
    has_new_missed_work: bool = False


class RecoveryDayChangeRead(BaseModel):
    plan_day_id: str
    code: str
    primary_goal: str
    from_date: date
    to_date: date


class RecoveryTimeItem(BaseModel):
    title: str
    date: date
    start: str | None = None
    end: str | None = None
    minutes: int
    state: Literal["SCHEDULED", "UNALLOCATED", "FLEXIBLE"]
    source_plan_version_id: str | None = None
    source_task_id: str | None = None
    lineage_task_id: str | None = None
    source_work_item_id: str | None = None
    work_item_window_id: str | None = None
    source_date: date | None = None
    remaining_minutes: int | None = Field(default=None, ge=0)
    reason_code: str | None = None
    reason_detail: str | None = None
    allowed_actions: list[Literal[
        "ADJUST_AVAILABILITY", "CHANGE_RECOVERY_DATE", "REVIEW_DEPENDENCY",
        "REVIEW_PLAN", "KEEP_PENDING",
    ]] = Field(default_factory=list)


class RecoveryTimePreview(BaseModel):
    mode: str
    timezone: str
    before: list[RecoveryTimeItem] = Field(default_factory=list)
    after: list[RecoveryTimeItem] = Field(default_factory=list)
    unallocated_count: int = 0
    fingerprint: str
    diagnostics_only: bool = False


class RecoveryShiftPreviewRead(BaseModel):
    time_preview: RecoveryTimePreview | None = None
    id: str
    status: Literal["PREVIEW", "APPLIED"]
    mode: Literal["SHIFT_FORWARD"]
    source_plan_version_id: str
    result_plan_version_id: str | None = None
    anchor_date: date
    original_end_date: date
    projected_end_date: date
    missed_execution_days: int
    locked_day_ids: list[str] = Field(default_factory=list)
    locked_task_ids: list[str] = Field(default_factory=list)
    carryover_task_ids: list[str] = Field(default_factory=list)
    changes: list[RecoveryDayChangeRead] = Field(default_factory=list)
    content_changed: bool = False
    model_called: bool = False
    reason_codes: list[AdjustmentReasonCode] = Field(default_factory=list)


class RecoveryShiftApply(BaseModel):
    preview_id: str = Field(min_length=1, max_length=36)
    accept_unallocated: bool = False


class RecoveryDeferRequest(BaseModel):
    missed_dates: list[date] = Field(default_factory=list)


class RetrospectiveCompletionRequest(BaseModel):
    task_ids: list[str] = Field(min_length=1, max_length=100)


class RecoveryCatchupDayLoadRead(BaseModel):
    scheduled_date: date
    day_type: Literal["EXECUTION", "NON_EXECUTION"]
    added_tasks: int
    added_minutes: int
    original_minutes: int
    total_minutes: int


class RecoveryCatchupPreviewRead(BaseModel):
    time_preview: RecoveryTimePreview | None = None
    id: str
    status: Literal["PREVIEW", "APPLIED"]
    mode: Literal["KEEP_DEADLINE"]
    source_plan_version_id: str
    result_plan_version_id: str | None = None
    anchor_date: date
    original_end_date: date
    missed_dates: list[date]
    backlog_tasks: int
    backlog_minutes: int
    remaining_execution_days: int
    available_non_execution_days: int
    required_extra_minutes_per_day: int
    include_non_execution_days: bool
    programmatic_feasible: bool
    model_required: bool
    backlog_cleared_date: date | None = None
    allocation: list[RecoveryCatchupDayLoadRead] = Field(default_factory=list)
    reason_codes: list[AdjustmentReasonCode] = Field(default_factory=list)
    recovery_scope: "RecoveryScopeRead | None" = None


class RecoveryScopeItem(BaseModel):
    source_task_id: str
    lineage_task_id: str
    title: str
    source_date: date
    minutes: int
    remaining_minutes: int
    selected: bool
    prerequisite_task_ids: list[str] = Field(default_factory=list)
    can_split: bool = False
    split_reason: str | None = None


class RecoveryScopeRead(BaseModel):
    source_plan_version_id: str
    items: list[RecoveryScopeItem] = Field(default_factory=list)
    selected_task_ids: list[str] = Field(default_factory=list)
    deferred_task_ids: list[str] = Field(default_factory=list)
    selected_minutes: int = 0
    deferred_minutes: int = 0
    fingerprint: str


class RecoveryCatchupRequest(BaseModel):
    anchor_date: date | None = None
    include_non_execution_days: bool = False
    source_plan_version_id: str = Field(min_length=1, max_length=36)
    selected_task_ids: list[str] = Field(min_length=1, max_length=200)

    @field_validator("selected_task_ids")
    @classmethod
    def selected_sources_are_unique(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)) or any(not item.strip() for item in value):
            raise ValueError("Recovery selection must contain unique source task IDs")
        return value


class RecoveryCatchupApply(BaseModel):
    preview_id: str = Field(min_length=1, max_length=36)
    accept_unallocated: bool = False


class RecoveryAIRequest(BaseModel):
    missed_dates: list[date] = Field(default_factory=list)
    backlog_minutes: int = Field(ge=0)
    original_end_date: date
    include_non_execution_days: bool = False
    preview_id: str | None = Field(default=None, min_length=1, max_length=36)
    instruction: str = Field(default="", max_length=1000)
    research_run_id: str | None = Field(default=None, min_length=1, max_length=36)


class RecoverySplitRequest(BaseModel):
    preview_id: str = Field(min_length=1, max_length=36)
    source_task_id: str = Field(min_length=1, max_length=36)
    part_minutes: list[int] = Field(min_length=2, max_length=8)

    @field_validator("part_minutes")
    @classmethod
    def parts_are_bounded(cls, value: list[int]) -> list[int]:
        if any(part < 5 or part > 1440 for part in value):
            raise ValueError("每个分片必须为 5–1440 分钟")
        return value


class RecoverySplitPreviewRead(BaseModel):
    id: str
    status: Literal["PREVIEW", "APPLIED"]
    source_plan_version_id: str
    result_plan_version_id: str | None = None
    source_task_id: str
    title: str
    original_minutes: int
    part_minutes: list[int]
    result_selected_task_ids: list[str] = Field(default_factory=list)


class RecoveryDiscussionRequest(BaseModel):
    preview_id: str = Field(min_length=1, max_length=36)


class ExplorationExecutionStateRead(BaseModel):
    generated_through_week: int
    total_weeks: int
    next_unplanned_week: int | None = None
    current_window_completed: bool
    pending_window_start_week: int | None = None
    pending_window_end_week: int | None = None


class ExecutionProgressRead(BaseModel):
    total_focus_minutes: int = 0
    applied_adjustments: int = 0
    goal_id: str
    goal_title: str
    plan_revision: int
    schedule_type: Literal["OUTCOME", "RHYTHM", "CALENDAR"] = "RHYTHM"
    current_stage_order: int | None = None
    current_stage_title: str | None = None
    day_progress: ProgressSummary
    stage_progress: ProgressSummary
    overall_progress: ProgressSummary
    exploration: ExplorationExecutionStateRead | None = None
    stage_summary: str
    overall_summary: str
    trend: ExecutionTrendRead
    rhythm: RhythmProgressRead | None = None
    review_signal: ExecutionReviewSignalRead = Field(default_factory=ExecutionReviewSignalRead)
    inactivity: ExecutionInactivityRead
    recent_days: list[ExecutionProgressDayRead] = Field(default_factory=list)
    feedback_timeline: list[ExecutionFeedbackTimelineRead] = Field(default_factory=list)
    revision_history: list[ExecutionPlanRevisionRead] = Field(default_factory=list)


ExecutionDiscussionIntentType = Literal[
    "QUESTION",
    "INSIGHT",
    "CONSTRAINT_CHANGE",
    "PROGRESS_REPORT",
    "DIFFICULTY",
    "PLAN_CHANGE_REQUEST",
    "ROUTE_CHANGE_SIGNAL",
    "REVIEW_REQUEST",
    "NO_ACTION",
]


class ExecutionDiscussionIntent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: ExecutionDiscussionIntentType
    summary: str = Field(min_length=2, max_length=300)
    confidence: float = Field(ge=0, le=1)


class ExecutionMemoryCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    insight_type: Literal[
        "LEARNING",
        "PREFERENCE",
        "CONSTRAINT",
        "CAPABILITY",
        "DIFFICULTY",
        "STRATEGY",
    ]
    content: str = Field(min_length=2, max_length=500)
    scope: Literal["USER", "GOAL", "STAGE", "PLAN", "TEMPORARY"] = "GOAL"
    confidence: float = Field(ge=0, le=1)
    # Default keeps previously stored analysis payloads readable while new model
    # responses are instructed to provide a stable semantic key.
    memory_key: str = Field(
        default="execution_context", min_length=2, max_length=80, pattern=r"^[a-z0-9_:-]+$"
    )
    operation: Literal["UPSERT", "RETRACT"] = "UPSERT"
    supersedes_id: str | None = Field(default=None, max_length=36)
    source_type: Literal["EXPLICIT_USER", "AGENT_INFERENCE", "SYSTEM_OBSERVATION"] = (
        "AGENT_INFERENCE"
    )
    evidence_span: str | None = Field(default=None, max_length=500)
    confirmation_policy: Literal["AUTO_CONFIRM_EXPLICIT", "USER_REVIEW", "TEMPORARY"] = (
        "USER_REVIEW"
    )
    valid_days: int | None = Field(default=None, ge=1, le=365)


class ExecutionAdjustmentSignal(BaseModel):
    model_config = ConfigDict(extra="forbid")

    recommended: bool = False
    scope: Literal["NEAR_TERM", "STAGE", "REMAINING_PLAN", "ROUTE"] | None = None
    reason: str | None = Field(default=None, max_length=500)
    request: str | None = Field(default=None, max_length=1000)
    target_schedule_type: Literal["OUTCOME", "RHYTHM", "CALENDAR"] | None = None


class ExecutionDiscussionResult(BaseModel):
    long_term_memories: list[LongTermMemoryCandidate] = Field(default_factory=list, max_length=6)
    model_config = ConfigDict(extra="forbid")

    assistant_message: str = Field(min_length=2, max_length=3000)
    intents: list[ExecutionDiscussionIntent] = Field(default_factory=list, max_length=8)
    memory_candidates: list[ExecutionMemoryCandidate] = Field(default_factory=list, max_length=6)
    error_memory_candidates: list[ErrorAvoidanceMemoryCandidate] = Field(
        default_factory=list, max_length=4
    )
    review_signal: ExecutionReviewSignalRead = Field(default_factory=ExecutionReviewSignalRead)
    adjustment_signal: ExecutionAdjustmentSignal = Field(default_factory=ExecutionAdjustmentSignal)
    questions: list[str] = Field(default_factory=list, max_length=2)


class ExecutionDiscussionCreate(BaseModel):
    content: str = Field(min_length=1, max_length=4000)
    web_research_enabled: bool = False
    research_run_id: str | None = None
    use_saved_research: bool = True
    regenerate: bool = False
    material_source_ids: list[str] | None = Field(default=None, max_length=500)

    @field_validator("content")
    @classmethod
    def normalize_content(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("Discussion message cannot be blank")
        return normalized


class ExecutionInsightRead(BaseModel):
    id: str
    insight_type: str
    content: str
    scope: str
    confidence: float
    status: Literal["PROPOSED", "CONFIRMED", "DISMISSED", "SUPERSEDED"]
    source_message_id: str
    created_at: datetime
    decided_at: datetime | None = None
    memory_key: str | None = None
    source_type: str = "AGENT_INFERENCE"
    evidence_span: str | None = None
    confirmation_policy: str = "USER_REVIEW"
    valid_from: date | None = None
    valid_until: date | None = None


class ExecutionInsightDecision(BaseModel):
    action: Literal["CONFIRM", "DISMISS"]


class ExecutionReviewRead(BaseModel):
    id: str
    trigger_type: str
    status: Literal["SCHEDULED", "SUGGESTED", "DISCUSSING", "DECIDED", "DISMISSED"]
    reason: str
    evidence: dict[str, Any] = Field(default_factory=dict)
    summary: str | None = None
    decision: str | None = None
    created_at: datetime
    updated_at: datetime
    not_before: date | None = None


class ExecutionDiscussionWorkspace(BaseModel):
    goal_id: str
    goal_title: str
    goal_status: str
    planning_mode: Literal["EXPLORATORY", "COMMITTED"]
    schedule_type: Literal["OUTCOME", "RHYTHM", "CALENDAR"]
    messages: list[MessageRead] = Field(default_factory=list)
    insights: list[ExecutionInsightRead] = Field(default_factory=list)
    active_review: ExecutionReviewRead | None = None
    progress: ExecutionProgressRead
    latest_analysis: ExecutionDiscussionResult | None = None
    inherited_discussion: bool = True
    latest_research: ResearchSummaryRead | None = None


class CarryForwardCandidateRead(BaseModel):
    task_id: str
    title: str
    description: str
    estimate_minutes: int
    scheduled_date: date
    day_code: str
    can_carry: bool = True
    blocked_reason: str | None = None
    can_split: bool = True
    split_reason: str | None = None


class TodayWorkspace(BaseModel):
    goal_id: str
    goal_title: str
    status: str
    today: date
    schedule_type: Literal["OUTCOME", "RHYTHM", "CALENDAR"] = "RHYTHM"
    is_execution_day: bool
    next_execution_date: date | None = None
    milestone_title: str | None = None
    scheduled_day: ScheduledDayRead | None = None
    day: PlanDayRead | None = None
    day_completed: bool = False
    day_progress: ProgressSummary
    stage_progress: ProgressSummary
    overall_progress: ProgressSummary
    exploration: ExplorationExecutionStateRead | None = None
    capacity: TodayCapacityRead
    commitment_progress: list[CommitmentProgressRead] = Field(default_factory=list)
    commitment_alerts: list[CommitmentAlertRead] = Field(default_factory=list)
    inactivity: ExecutionInactivityRead
    feedbacks: list[FeedbackRead] = Field(default_factory=list)
    adjustments: list[FeedbackAdjustmentRead] = Field(default_factory=list)
    day_log: ExecutionDayLogRead | None = None
    carry_forward_tasks: list[CarryForwardCandidateRead] = Field(default_factory=list)


class UnifiedTodayDependencyRead(BaseModel):
    reference: str
    task_id: str | None = None
    title: str | None = None
    status: Literal["COMPLETED", "PENDING", "UNKNOWN"]


class UnifiedTodayTaskRead(BaseModel):
    id: str
    source: Literal["AGENT", "QUICK"]
    plan_id: str
    plan_title: str
    plan_kind: Literal["AGENT", "QUICK"] = "AGENT"
    title: str
    description: str = ""
    estimate_minutes: int
    priority: int
    status: str
    scheduled_date: date
    start_local: str | None = None
    end_local: str | None = None
    timezone: str
    is_fixed: bool
    parallel_confirmed: bool = False
    schedule_status: Literal["PLANNED", "REQUIRES_REVIEW", "CANCELLED", "COMPLETED"] = "PLANNED"
    schedule_review_required: bool = False
    schedule_change_reason: str | None = None
    dependencies: list[UnifiedTodayDependencyRead] = Field(default_factory=list)
    blocked_reason: str | None = None
    ai_schedule_requested: bool = False
    # The task's semantic timing rule is distinct from whether a concrete
    # time block exists today.  This lets the Today board explain why a task
    # is flexible, cadence-driven, or deadline-bound instead of treating all
    # tasks without a block as the same kind of work.
    scheduling_policy: Literal[
        "EXACT_DAY",
        "DATE_WINDOW",
        "CADENCE",
        "QUOTA",
        "TASK_POOL",
        "CHECKPOINT",
        "TRIGGERED",
        "FLOATING",
    ] = "EXACT_DAY"
    scheduling_policy_data: dict[str, Any] = Field(default_factory=dict)
    skip_guidance: list[str] = Field(default_factory=list)


class TodayScheduleConflictRead(BaseModel):
    first_task_id: str
    second_task_id: str
    overlap_start: str
    overlap_end: str
    reason: str


class UnifiedTodayRead(BaseModel):
    date: date
    timezone: str
    fixed_tasks: list[UnifiedTodayTaskRead] = Field(default_factory=list)
    flexible_tasks: list[UnifiedTodayTaskRead] = Field(default_factory=list)
    conflicts: list[TodayScheduleConflictRead] = Field(default_factory=list)


class TodayTaskScheduleRequest(BaseModel):
    source: Literal["AGENT", "QUICK", "MANUAL"]
    task_id: str = Field(min_length=1, max_length=36)
    scheduled_date: date
    start_local: str
    end_local: str
    allow_parallel: bool = False
    parallel_reason: str | None = Field(default=None, max_length=500)

    @field_validator("start_local", "end_local")
    @classmethod
    def schedule_time_must_be_valid(cls, value: str) -> str:
        return _validate_local_time(value)

    @model_validator(mode="after")
    def schedule_must_be_ordered(self) -> "TodayTaskScheduleRequest":
        if self.start_local >= self.end_local:
            raise ValueError("Task end must be later than its start")
        if self.allow_parallel and not (self.parallel_reason or "").strip():
            raise ValueError("A reason is required when parallel execution is confirmed")
        return self


class CarryForwardRequest(BaseModel):
    """Voluntarily pull a future task into the selected day.

    ``minutes`` may be less than the task estimate; the service then creates a
    separate partial work unit and leaves the remainder on its original day.
    """

    target_date: date
    task_id: str = Field(min_length=1, max_length=36)
    minutes: int | None = Field(default=None, ge=1, le=1440)


class FlexibleTaskArrangeRequest(BaseModel):
    """Explicitly bring an outstanding floating task into today's execution day."""

    task_id: str = Field(min_length=1, max_length=36)


class FlexibleTaskRescheduleRequest(BaseModel):
    """Move an outstanding flexible task to a user-selected calendar date."""

    task_id: str = Field(min_length=1, max_length=36)
    target_date: date
    reason: str = Field(min_length=1, max_length=500)

    @field_validator("reason")
    @classmethod
    def reason_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Rescheduling requires a reason")
        return value.strip()


class FlexibleTaskDecisionRequest(BaseModel):
    """Request a preview for a user decision on a non-fixed task."""

    action: Literal["DEFER", "ARRANGE_TODAY", "RESCHEDULE"]
    task_id: str = Field(min_length=1, max_length=36)
    target_date: date | None = None
    reason: str | None = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def validate_decision(self) -> "FlexibleTaskDecisionRequest":
        if self.action == "RESCHEDULE" and self.target_date is None:
            raise ValueError("Rescheduling requires a target date")
        if self.action in {"DEFER", "RESCHEDULE"} and not (self.reason or "").strip():
            raise ValueError("This decision requires a reason")
        self.reason = self.reason.strip() if self.reason else None
        return self


class FlexibleTaskDecisionApply(BaseModel):
    preview_id: str = Field(min_length=1, max_length=36)


class FlexibleTaskDecisionPreviewRead(BaseModel):
    id: str
    status: Literal["PREVIEW", "APPLIED"]
    action: Literal["DEFER", "ARRANGE_TODAY", "RESCHEDULE"]
    task_id: str
    title: str
    scheduling_policy: str
    source_date: date | None = None
    target_date: date | None = None
    planned_minutes: int = 0
    capacity_minutes: int = 0
    over_capacity_minutes: int = 0
    competing_tasks: list[str] = Field(default_factory=list)
    shared_available_minutes: int | None = None
    proposed_start_local: str | None = None
    proposed_end_local: str | None = None
    blockers: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    affected_task_ids: list[str] = Field(default_factory=list)
    remaining_commitments: list[CommitmentProgressRead] = Field(default_factory=list)
    can_apply: bool = False
    fingerprint: str


class TodayTaskScheduleRead(BaseModel):
    task_id: str
    source: Literal["AGENT", "QUICK"]
    scheduled_date: date
    start_local: str
    end_local: str
    parallel_confirmed: bool
    parallel_reason: str | None = None


class TodayTaskScheduleResult(BaseModel):
    scheduled: TodayTaskScheduleRead
    conflicts: list[TodayScheduleConflictRead] = Field(default_factory=list)


class DayAllocationRequest(BaseModel):
    scheduled_date: date
    goal_id: str | None = None
    manual_plan_id: str | None = None
    windows: list[AvailabilityWindowInput] = Field(default_factory=list, max_length=20)
    include_flexible: bool = True
    use_ai: bool = True
    model_task_order: list[str] = Field(default_factory=list, max_length=200)
    model_preferred_starts: dict[str, str] = Field(default_factory=dict)
    model_notes: list[str] = Field(default_factory=list, max_length=20)

    @field_validator("model_preferred_starts")
    @classmethod
    def model_start_times_must_be_valid(cls, value: dict[str, str]) -> dict[str, str]:
        return {task_id: _validate_local_time(start) for task_id, start in value.items()}


class DayScheduleSuggestion(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: str = Field(min_length=1, max_length=36)
    preferred_start_local: str | None = None
    rationale: str = Field(default="", max_length=300)

    @field_validator("preferred_start_local")
    @classmethod
    def preferred_start_must_be_valid(cls, value: str | None) -> str | None:
        return _validate_local_time(value) if value is not None else None


class DayScheduleAIResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    suggestions: list[DayScheduleSuggestion] = Field(default_factory=list, max_length=200)
    summary: str = Field(min_length=1, max_length=1000)


class DayAllocationPreview(BaseModel):
    fingerprint: str
    request: DayAllocationRequest
    timezone: str
    assignments: list[TodayTaskScheduleRequest]
    unallocated: list[UnifiedTodayTaskRead]
    available_minutes: int
    required_minutes: int
    assigned_minutes: int = 0
    unallocated_minutes: int = 0
    unallocated_reasons: dict[str, str] = Field(default_factory=dict)
    competition_summary: list[dict[str, Any]] = Field(default_factory=list)
    notes: list[str]
    # The scheduler is intentionally day-scoped.  This context is returned with
    # every preview so a future model provider can reason over exactly the same
    # facts as the deterministic validator, without regenerating the plan.
    strategy: Literal["LOCAL_VALIDATED", "MODEL_ASSISTED"] = "LOCAL_VALIDATED"
    scheduling_context: dict[str, Any] = Field(default_factory=dict)


class DayAllocationApply(BaseModel):
    request: DayAllocationRequest
    fingerprint: str


class DayAllocationBatchRequest(BaseModel):
    requests: list[DayAllocationRequest] = Field(min_length=1, max_length=31)

    @model_validator(mode="after")
    def dates_must_be_unique(self) -> "DayAllocationBatchRequest":
        dates = [item.scheduled_date for item in self.requests]
        if len(dates) != len(set(dates)):
            raise ValueError("Batch scheduling dates must be unique")
        return self
