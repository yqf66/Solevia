from __future__ import annotations

from datetime import UTC, date, datetime
from uuid import uuid4

from sqlalchemy import (
    JSON,
    BigInteger,
    CheckConstraint,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from planner_api.database import Base


def utc_now() -> datetime:
    return datetime.now(UTC)


def new_id() -> str:
    return str(uuid4())


class Goal(Base):
    __tablename__ = "goals"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    title: Mapped[str] = mapped_column(String(160))
    raw_input: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(32), default="CLARIFYING", index=True)
    current_requirement_version_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    active_route_version_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    active_plan_version_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    # The user confirms this during clarification, before a route is drafted.
    planning_mode: Mapped[str] = mapped_column(String(16), default="COMMITTED")
    planning_mode_confirmed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # This is orthogonal to planning_mode: it controls how execution binds to time.
    schedule_type: Mapped[str] = mapped_column(String(16), default="RHYTHM")
    schedule_type_confirmed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    timezone: Mapped[str] = mapped_column(String(64), default="Asia/Shanghai")
    pause_until: Mapped[date | None] = mapped_column(Date, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completion_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_active_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )


class Conversation(Base):
    __tablename__ = "conversations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    goal_id: Mapped[str] = mapped_column(ForeignKey("goals.id", ondelete="CASCADE"), index=True)
    phase: Mapped[str] = mapped_column(String(32), default="CLARIFICATION")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    conversation_id: Mapped[str] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), index=True
    )
    sequence: Mapped[int] = mapped_column(Integer)
    role: Mapped[str] = mapped_column(String(16))
    content: Mapped[str] = mapped_column(Text)
    generation_status: Mapped[str] = mapped_column(String(16), default="SUCCEEDED")
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class RequirementFact(Base):
    __tablename__ = "requirement_facts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    goal_id: Mapped[str] = mapped_column(ForeignKey("goals.id", ondelete="CASCADE"), index=True)
    category: Mapped[str] = mapped_column(String(40))
    field_key: Mapped[str] = mapped_column(String(64), index=True)
    value_json: Mapped[object] = mapped_column(JSON)
    source: Mapped[str] = mapped_column(String(24))
    status: Mapped[str] = mapped_column(String(20), index=True)
    confidence: Mapped[float] = mapped_column(Float, default=1.0)
    operation: Mapped[str] = mapped_column(String(24), default="ADD")
    change_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    evidence_message_id: Mapped[str | None] = mapped_column(
        ForeignKey("messages.id", ondelete="SET NULL"), nullable=True
    )
    supersedes_fact_id: Mapped[str | None] = mapped_column(
        ForeignKey("requirement_facts.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )


class ConversationSummary(Base):
    __tablename__ = "conversation_summaries"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    conversation_id: Mapped[str] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), index=True
    )
    through_message_sequence: Mapped[int] = mapped_column(Integer)
    from_message_sequence: Mapped[int] = mapped_column(Integer, default=1)
    summary_level: Mapped[str] = mapped_column(String(16), default="PHASE", index=True)
    summary: Mapped[str] = mapped_column(Text)
    confirmed_conclusions_json: Mapped[list] = mapped_column(JSON, default=list)
    open_questions_json: Mapped[list] = mapped_column(JSON, default=list)
    conflicts_json: Mapped[list] = mapped_column(JSON, default=list)
    current_topic: Mapped[str | None] = mapped_column(Text, nullable=True)
    recent_changes_json: Mapped[list] = mapped_column(JSON, default=list)
    entity_refs_json: Mapped[list] = mapped_column(JSON, default=list)
    readiness_json: Mapped[dict] = mapped_column(JSON, default=dict)
    source_summary_ids_json: Mapped[list] = mapped_column(JSON, default=list)
    content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    superseded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class RequirementChange(Base):
    """Append-only audit record for evolving requirement memory.

    Facts are the current/versioned values; this table records why a value
    appeared, was replaced, merged, or retracted so later conversations can
    explain changes without relying on an LLM to reconstruct history.
    """

    __tablename__ = "requirement_changes"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    goal_id: Mapped[str] = mapped_column(ForeignKey("goals.id", ondelete="CASCADE"), index=True)
    field_key: Mapped[str] = mapped_column(String(64), index=True)
    operation: Mapped[str] = mapped_column(String(32))
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    old_fact_id: Mapped[str | None] = mapped_column(
        ForeignKey("requirement_facts.id", ondelete="SET NULL"), nullable=True
    )
    new_fact_id: Mapped[str | None] = mapped_column(
        ForeignKey("requirement_facts.id", ondelete="SET NULL"), nullable=True
    )
    evidence_message_id: Mapped[str | None] = mapped_column(
        ForeignKey("messages.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class ClarificationAnalysisJob(Base):
    """Durable background job for extracting memory from a chat exchange."""

    __tablename__ = "clarification_analysis_jobs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    goal_id: Mapped[str] = mapped_column(ForeignKey("goals.id", ondelete="CASCADE"), index=True)
    conversation_id: Mapped[str] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), index=True
    )
    user_message_id: Mapped[str] = mapped_column(
        ForeignKey("messages.id", ondelete="CASCADE"), index=True
    )
    assistant_message_id: Mapped[str] = mapped_column(
        ForeignKey("messages.id", ondelete="CASCADE"), index=True
    )
    status: Mapped[str] = mapped_column(String(20), default="PENDING", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=4)
    next_retry_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, index=True
    )
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_error_code: Mapped[str | None] = mapped_column(String(48), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class RequirementVersion(Base):
    __tablename__ = "requirement_versions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    goal_id: Mapped[str] = mapped_column(ForeignKey("goals.id", ondelete="CASCADE"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(20), default="DRAFT", index=True)
    snapshot_json: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class UserMemoryItem(Base):
    """User-approved semantic memory shared across goals in the local workspace."""

    __tablename__ = "user_memory_items"
    __table_args__ = (
        UniqueConstraint("profile_id", "memory_key", name="uq_user_memory_profile_key"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    profile_id: Mapped[str] = mapped_column(String(36), default="default", index=True)
    memory_key: Mapped[str] = mapped_column(String(80))
    content: Mapped[str] = mapped_column(Text)
    source_type: Mapped[str] = mapped_column(String(24), default="EXPLICIT_USER")
    status: Mapped[str] = mapped_column(String(16), default="CONFIRMED", index=True)
    evidence_message_id: Mapped[str | None] = mapped_column(
        ForeignKey("messages.id", ondelete="SET NULL"), nullable=True
    )
    valid_from: Mapped[date | None] = mapped_column(Date, nullable=True)
    valid_until: Mapped[date | None] = mapped_column(Date, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )


class GoalErrorMemory(Base):
    """Confirmed goal-scoped negative constraint learned from user feedback."""

    __tablename__ = "goal_error_memories"
    __table_args__ = (
        UniqueConstraint("goal_id", "content_hash", name="uq_goal_error_memory_hash"),
        Index("ix_goal_error_memories_goal_status", "goal_id", "status"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    goal_id: Mapped[str] = mapped_column(ForeignKey("goals.id", ondelete="CASCADE"), index=True)
    memory_type: Mapped[str] = mapped_column(String(24), default="ERROR_AVOIDANCE")
    rule: Mapped[str] = mapped_column(Text)
    evidence: Mapped[str] = mapped_column(Text)
    source_message_id: Mapped[str | None] = mapped_column(
        ForeignKey("messages.id", ondelete="SET NULL"), nullable=True, index=True
    )
    scope: Mapped[str] = mapped_column(String(20), default="GOAL")
    target_refs_json: Mapped[list] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(16), default="CONFIRMED", index=True)
    content_hash: Mapped[str] = mapped_column(String(64), index=True)
    valid_until: Mapped[date | None] = mapped_column(Date, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )


class RouteVersion(Base):
    __tablename__ = "route_versions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    goal_id: Mapped[str] = mapped_column(ForeignKey("goals.id", ondelete="CASCADE"), index=True)
    requirement_version_id: Mapped[str] = mapped_column(
        ForeignKey("requirement_versions.id", ondelete="RESTRICT"), index=True
    )
    version: Mapped[int] = mapped_column(Integer)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(20), default="DRAFT", index=True)
    summary: Mapped[str] = mapped_column(Text)
    tradeoffs_json: Mapped[list] = mapped_column(JSON, default=list)
    assumptions_json: Mapped[list] = mapped_column(JSON, default=list)
    open_questions_json: Mapped[list] = mapped_column(JSON, default=list)
    optional_questions_json: Mapped[list] = mapped_column(JSON, default=list)
    change_summary_json: Mapped[list] = mapped_column(JSON, default=list)
    feedback: Mapped[str | None] = mapped_column(Text, nullable=True)
    prompt_version: Mapped[str] = mapped_column(String(40))
    source_document: Mapped[str | None] = mapped_column(Text, nullable=True)
    compiled_snapshot_json: Mapped[dict] = mapped_column(JSON, default=dict)
    compiler_report_json: Mapped[dict] = mapped_column(JSON, default=dict)
    planning_mode: Mapped[str] = mapped_column(String(16), default="COMMITTED")
    schedule_type: Mapped[str] = mapped_column(String(16), default="RHYTHM")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class RouteStage(Base):
    __tablename__ = "route_stages"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    route_version_id: Mapped[str] = mapped_column(
        ForeignKey("route_versions.id", ondelete="CASCADE"), index=True
    )
    order: Mapped[int] = mapped_column(Integer)
    title: Mapped[str] = mapped_column(String(160))
    purpose: Mapped[str] = mapped_column(Text)
    topics_json: Mapped[list] = mapped_column(JSON, default=list)
    outputs_json: Mapped[list] = mapped_column(JSON, default=list)
    depends_on_json: Mapped[list] = mapped_column(JSON, default=list)
    options_json: Mapped[list] = mapped_column(JSON, default=list)


class PlanVersion(Base):
    __tablename__ = "plan_versions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    goal_id: Mapped[str] = mapped_column(ForeignKey("goals.id", ondelete="CASCADE"), index=True)
    route_version_id: Mapped[str] = mapped_column(
        ForeignKey("route_versions.id", ondelete="RESTRICT"), index=True
    )
    version: Mapped[int] = mapped_column(Integer, default=1)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(20), default="DRAFT", index=True)
    summary: Mapped[str] = mapped_column(Text)
    total_weeks: Mapped[int] = mapped_column(Integer)
    weekly_execution_days: Mapped[int] = mapped_column(Integer)
    weekly_minutes: Mapped[int] = mapped_column(Integer)
    total_estimated_minutes: Mapped[int] = mapped_column(Integer)
    buffer_strategy_json: Mapped[list] = mapped_column(JSON, default=list)
    assumptions_json: Mapped[list] = mapped_column(JSON, default=list)
    open_questions_json: Mapped[list] = mapped_column(JSON, default=list)
    change_summary_json: Mapped[list] = mapped_column(JSON, default=list)
    temporal_segments_json: Mapped[list] = mapped_column(JSON, default=list)
    commitments_json: Mapped[list] = mapped_column(JSON, default=list)
    feedback: Mapped[str | None] = mapped_column(Text, nullable=True)
    prompt_version: Mapped[str] = mapped_column(String(40))
    source_document: Mapped[str | None] = mapped_column(Text, nullable=True)
    compiled_snapshot_json: Mapped[dict] = mapped_column(JSON, default=dict)
    compiler_report_json: Mapped[dict] = mapped_column(JSON, default=dict)
    planning_mode: Mapped[str] = mapped_column(String(16), default="COMMITTED")
    schedule_type: Mapped[str] = mapped_column(String(16), default="RHYTHM")
    # Execution storage is versioned per plan so existing active plans never
    # change semantics merely because the application schema is upgraded.
    execution_model_version: Mapped[str] = mapped_column(
        String(32), default="LEGACY_DAY_BASED", index=True
    )
    exploration_window_weeks: Mapped[int] = mapped_column(Integer, default=1)
    next_unplanned_week: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PlanGenerationJob(Base):
    __tablename__ = "plan_generation_jobs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    goal_id: Mapped[str] = mapped_column(ForeignKey("goals.id", ondelete="CASCADE"), index=True)
    mode: Mapped[str] = mapped_column(String(16), default="STANDARD")
    planning_mode: Mapped[str] = mapped_column(String(16), default="COMMITTED")
    schedule_type: Mapped[str] = mapped_column(String(16), default="RHYTHM")
    status: Mapped[str] = mapped_column(String(24), default="QUEUED", index=True)
    phase: Mapped[str] = mapped_column(String(24), default="OUTLINE")
    generated_weeks: Mapped[int] = mapped_column(Integer, default=0)
    total_weeks: Mapped[int] = mapped_column(Integer, default=0)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    diagnostic: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )


class PlanWindow(Base):
    """A materialized exploration stage and the evidence that produced it."""

    __tablename__ = "plan_windows"
    __table_args__ = (
        UniqueConstraint("plan_version_id", "sequence", name="uq_plan_window_sequence"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    goal_id: Mapped[str] = mapped_column(ForeignKey("goals.id", ondelete="CASCADE"), index=True)
    plan_version_id: Mapped[str] = mapped_column(
        ForeignKey("plan_versions.id", ondelete="CASCADE"), index=True
    )
    sequence: Mapped[int] = mapped_column(Integer)
    start_week: Mapped[int] = mapped_column(Integer)
    end_week: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16), default="DRAFT", index=True)
    trigger: Mapped[str] = mapped_column(String(24), default="INITIAL")
    basis_json: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class PlanMilestone(Base):
    __tablename__ = "plan_milestones"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    plan_version_id: Mapped[str] = mapped_column(
        ForeignKey("plan_versions.id", ondelete="CASCADE"), index=True
    )
    route_stage_id: Mapped[str] = mapped_column(
        ForeignKey("route_stages.id", ondelete="RESTRICT"), index=True
    )
    stage_order: Mapped[int] = mapped_column(Integer)
    title: Mapped[str] = mapped_column(String(160))
    purpose: Mapped[str] = mapped_column(Text)
    start_week: Mapped[int] = mapped_column(Integer)
    end_week: Mapped[int] = mapped_column(Integer)
    outputs_json: Mapped[list] = mapped_column(JSON, default=list)


class PlanWorkItemGroup(Base):
    """Selection boundary for alternatives such as "choose two of these exercises"."""

    __tablename__ = "plan_work_item_groups"
    __table_args__ = (
        UniqueConstraint("plan_version_id", "group_key", name="uq_plan_work_item_group_key"),
        CheckConstraint(
            "(scope_type = 'PLAN' AND milestone_id IS NULL AND stage_order IS NULL) OR "
            "(scope_type IN ('STAGE', 'WEEK') AND milestone_id IS NOT NULL "
            "AND stage_order IS NOT NULL) OR "
            "(scope_type = 'CHECKPOINT' AND ((milestone_id IS NULL AND stage_order IS NULL) "
            "OR (milestone_id IS NOT NULL AND stage_order IS NOT NULL)))",
            name="ck_plan_work_item_group_scope",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    plan_version_id: Mapped[str] = mapped_column(
        ForeignKey("plan_versions.id", ondelete="CASCADE"), index=True
    )
    milestone_id: Mapped[str | None] = mapped_column(
        ForeignKey("plan_milestones.id", ondelete="CASCADE"), nullable=True, index=True
    )
    scope_type: Mapped[str] = mapped_column(String(24), default="STAGE", index=True)
    stage_order: Mapped[int | None] = mapped_column(Integer, nullable=True)
    group_key: Mapped[str] = mapped_column(String(80))
    title: Mapped[str] = mapped_column(String(180))
    selection_mode: Mapped[str] = mapped_column(String(24), default="SELECT_K")
    minimum_selected: Mapped[int] = mapped_column(Integer, default=1)
    maximum_selected: Mapped[int | None] = mapped_column(Integer, nullable=True)
    constraints_json: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(24), default="DRAFT", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )


class PlanWorkItem(Base):
    """A semantic obligation that may exist before any execution date is chosen."""

    __tablename__ = "plan_work_items"
    __table_args__ = (
        UniqueConstraint("plan_version_id", "item_key", name="uq_plan_work_item_key"),
        Index("idx_plan_work_items_plan_stage", "plan_version_id", "stage_order"),
        CheckConstraint(
            "(scope_type = 'PLAN' AND milestone_id IS NULL AND stage_order IS NULL) OR "
            "(scope_type IN ('STAGE', 'WEEK') AND milestone_id IS NOT NULL "
            "AND stage_order IS NOT NULL) OR "
            "(scope_type = 'CHECKPOINT' AND ((milestone_id IS NULL AND stage_order IS NULL) "
            "OR (milestone_id IS NOT NULL AND stage_order IS NOT NULL)))",
            name="ck_plan_work_item_scope",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    plan_version_id: Mapped[str] = mapped_column(
        ForeignKey("plan_versions.id", ondelete="CASCADE"), index=True
    )
    milestone_id: Mapped[str | None] = mapped_column(
        ForeignKey("plan_milestones.id", ondelete="CASCADE"), nullable=True, index=True
    )
    group_id: Mapped[str | None] = mapped_column(
        ForeignKey("plan_work_item_groups.id", ondelete="SET NULL"), nullable=True, index=True
    )
    source_work_item_id: Mapped[str | None] = mapped_column(
        ForeignKey("plan_work_items.id", ondelete="SET NULL"), nullable=True, index=True
    )
    lineage_work_item_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    item_key: Mapped[str] = mapped_column(String(120))
    scope_type: Mapped[str] = mapped_column(String(24), default="STAGE", index=True)
    stage_order: Mapped[int | None] = mapped_column(Integer, nullable=True)
    title: Mapped[str] = mapped_column(String(180))
    description: Mapped[str] = mapped_column(Text)
    task_kind: Mapped[str] = mapped_column(String(24))
    estimate_minutes: Mapped[int] = mapped_column(Integer)
    priority: Mapped[int] = mapped_column(Integer)
    is_required: Mapped[bool] = mapped_column(default=True)
    is_splittable: Mapped[bool] = mapped_column(default=True)
    binding_mode: Mapped[str] = mapped_column(String(24), default="WINDOW", index=True)
    fulfillment_mode: Mapped[str] = mapped_column(String(24), default="ONCE", index=True)
    target_value: Mapped[int] = mapped_column(Integer, default=1)
    target_unit: Mapped[str] = mapped_column(String(24), default="COUNT")
    constraints_json: Mapped[dict] = mapped_column(JSON, default=dict)
    completion_rule_json: Mapped[dict] = mapped_column(JSON, default=dict)
    evidence_json: Mapped[list] = mapped_column(JSON, default=list)
    source_refs_json: Mapped[list] = mapped_column(JSON, default=list)
    commitment_refs_json: Mapped[list] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(24), default="DRAFT", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )


class WorkItemDependency(Base):
    """Normalized dependency edge between semantic work items."""

    __tablename__ = "work_item_dependencies"
    __table_args__ = (
        UniqueConstraint(
            "predecessor_work_item_id",
            "successor_work_item_id",
            "dependency_type",
            name="uq_work_item_dependency_edge",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    plan_version_id: Mapped[str] = mapped_column(
        ForeignKey("plan_versions.id", ondelete="CASCADE"), index=True
    )
    predecessor_work_item_id: Mapped[str] = mapped_column(
        ForeignKey("plan_work_items.id", ondelete="CASCADE"), index=True
    )
    successor_work_item_id: Mapped[str] = mapped_column(
        ForeignKey("plan_work_items.id", ondelete="CASCADE"), index=True
    )
    dependency_type: Mapped[str] = mapped_column(String(24), default="FINISH_TO_START")
    minimum_lag_days: Mapped[int] = mapped_column(Integer, default=0)
    maximum_lag_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    condition_json: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class WorkItemWindow(Base):
    """A concrete obligation window resolved for one plan activation."""

    __tablename__ = "work_item_windows"
    __table_args__ = (
        UniqueConstraint(
            "activation_id", "work_item_id", "period_key", name="uq_work_item_window_period"
        ),
        Index(
            "idx_work_item_windows_activation_dates",
            "activation_id",
            "eligible_from",
            "eligible_to",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    activation_id: Mapped[str] = mapped_column(
        ForeignKey("plan_activations.id", ondelete="CASCADE"), index=True
    )
    work_item_id: Mapped[str] = mapped_column(
        ForeignKey("plan_work_items.id", ondelete="CASCADE"), index=True
    )
    period_key: Mapped[str] = mapped_column(String(80))
    eligible_from: Mapped[date] = mapped_column(Date, index=True)
    eligible_to: Mapped[date] = mapped_column(Date, index=True)
    required_value: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(24), default="PENDING", index=True)
    resolution_json: Mapped[dict] = mapped_column(JSON, default=dict)
    materialization_revision: Mapped[int] = mapped_column(Integer, default=0)
    opened_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    fulfilled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )


class PlanDay(Base):
    __tablename__ = "plan_days"
    __table_args__ = (
        Index(
            "idx_plan_days_plan_week_day",
            "plan_version_id",
            "week_index",
            "day_index",
            unique=True,
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    plan_version_id: Mapped[str] = mapped_column(
        ForeignKey("plan_versions.id", ondelete="CASCADE"), index=True
    )
    milestone_id: Mapped[str] = mapped_column(
        ForeignKey("plan_milestones.id", ondelete="CASCADE"), index=True
    )
    stage_order: Mapped[int] = mapped_column(Integer)
    week_index: Mapped[int] = mapped_column(Integer)
    day_index: Mapped[int] = mapped_column(Integer)
    primary_goal: Mapped[str] = mapped_column(Text)
    suggested_minutes: Mapped[int] = mapped_column(Integer)
    evidence_json: Mapped[list] = mapped_column(JSON, default=list)
    do_not_json: Mapped[list] = mapped_column(JSON, default=list)


class PlanTask(Base):
    __tablename__ = "plan_tasks"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    plan_version_id: Mapped[str] = mapped_column(
        ForeignKey("plan_versions.id", ondelete="CASCADE"), index=True
    )
    plan_day_id: Mapped[str] = mapped_column(
        ForeignKey("plan_days.id", ondelete="CASCADE"), index=True
    )
    source_task_id: Mapped[str | None] = mapped_column(
        ForeignKey("plan_tasks.id", ondelete="SET NULL"), nullable=True, index=True
    )
    source_work_item_id: Mapped[str | None] = mapped_column(
        ForeignKey("plan_work_items.id", ondelete="SET NULL"), nullable=True, index=True
    )
    work_item_window_id: Mapped[str | None] = mapped_column(
        ForeignKey("work_item_windows.id", ondelete="SET NULL"), nullable=True, index=True
    )
    occurrence_index: Mapped[int | None] = mapped_column(Integer, nullable=True)
    materialization_key: Mapped[str | None] = mapped_column(
        String(160), nullable=True, unique=True, index=True
    )
    materialized_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # The stage a work unit originally belonged to.  Dates may move during
    # recovery, but progress must remain attributed to the original stage.
    origin_stage_order: Mapped[int | None] = mapped_column(Integer, nullable=True)
    lineage_task_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    order_in_day: Mapped[int] = mapped_column(Integer)
    title: Mapped[str] = mapped_column(String(180))
    description: Mapped[str] = mapped_column(Text)
    estimate_minutes: Mapped[int] = mapped_column(Integer)
    priority: Mapped[int] = mapped_column(Integer)
    task_kind: Mapped[str] = mapped_column(String(24))
    is_required: Mapped[bool] = mapped_column(default=True)
    evidence_json: Mapped[list] = mapped_column(JSON, default=list)
    depends_on_json: Mapped[list] = mapped_column(JSON, default=list)
    satisfies_commitments_json: Mapped[list] = mapped_column(JSON, default=list)
    ai_schedule_requested: Mapped[bool] = mapped_column(default=False)
    is_splittable: Mapped[bool] = mapped_column(default=True)
    # The relative day remains the compatibility anchor. This policy tells
    # calendar/time allocators how freely the work may move around that anchor.
    scheduling_policy: Mapped[str] = mapped_column(String(24), default="EXACT_DAY")
    scheduling_policy_json: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(24), default="NOT_STARTED", index=True)


class PlanActivation(Base):
    __tablename__ = "plan_activations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    goal_id: Mapped[str] = mapped_column(ForeignKey("goals.id", ondelete="CASCADE"), index=True)
    plan_version_id: Mapped[str] = mapped_column(
        ForeignKey("plan_versions.id", ondelete="RESTRICT"), unique=True, index=True
    )
    requested_start_date: Mapped[date] = mapped_column(Date)
    projected_end_date: Mapped[date] = mapped_column(Date)
    execution_weekdays_json: Mapped[list] = mapped_column(JSON, default=list)
    excluded_dates_json: Mapped[list] = mapped_column(JSON, default=list)
    activated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class PlanDaySchedule(Base):
    __tablename__ = "plan_day_schedules"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    activation_id: Mapped[str] = mapped_column(
        ForeignKey("plan_activations.id", ondelete="CASCADE"), index=True
    )
    plan_day_id: Mapped[str] = mapped_column(
        ForeignKey("plan_days.id", ondelete="CASCADE"), unique=True, index=True
    )
    scheduled_date: Mapped[date] = mapped_column(Date, index=True)


class PlanSchedulePreference(Base):
    """User's preferred level of execution-time detail for a plan.

    FLEXIBLE keeps only the day and estimated effort, BLOCKS assigns concrete
    time blocks per day, and FIXED represents a recurring weekly timetable.
    """

    __tablename__ = "plan_schedule_preferences"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    goal_id: Mapped[str] = mapped_column(
        ForeignKey("goals.id", ondelete="CASCADE"), unique=True, index=True
    )
    mode: Mapped[str] = mapped_column(String(16), default="FLEXIBLE")
    timezone: Mapped[str] = mapped_column(String(64), default="Asia/Shanghai")
    break_minutes: Mapped[int] = mapped_column(Integer, default=10)
    allow_cross_window: Mapped[bool] = mapped_column(default=False)
    reminders_enabled: Mapped[bool] = mapped_column(default=True)
    supervision_mode: Mapped[str] = mapped_column(String(16), default="STANDARD")
    cross_plan_priority_mode: Mapped[str] = mapped_column(String(16), default="BALANCED")
    quiet_start_local: Mapped[str | None] = mapped_column(String(5), nullable=True)
    quiet_end_local: Mapped[str | None] = mapped_column(String(5), nullable=True)
    muted_commitments_json: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )


class HybridMigrationAudit(Base):
    """Durable migration and rollout checkpoint for a plan version."""

    __tablename__ = "hybrid_migration_audits"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    plan_version_id: Mapped[str] = mapped_column(
        ForeignKey("plan_versions.id", ondelete="CASCADE"), unique=True, index=True
    )
    source_execution_model: Mapped[str] = mapped_column(String(32), default="LEGACY_DAY_BASED")
    target_execution_model: Mapped[str] = mapped_column(String(32), default="HYBRID_WORK_ITEM")
    status: Mapped[str] = mapped_column(String(16), default="PENDING", index=True)
    rollout_bucket: Mapped[int] = mapped_column(Integer, default=0)
    details_json: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )


class ReminderEvent(Base):
    """A persisted supervision signal for a commitment risk."""

    __tablename__ = "reminder_events"
    __table_args__ = (
        UniqueConstraint("goal_id", "dedupe_key", name="uq_reminder_events_goal_dedupe"),
        Index("idx_reminder_events_goal_status", "goal_id", "status"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    goal_id: Mapped[str] = mapped_column(ForeignKey("goals.id", ondelete="CASCADE"), index=True)
    plan_version_id: Mapped[str | None] = mapped_column(
        ForeignKey("plan_versions.id", ondelete="SET NULL"), nullable=True, index=True
    )
    dedupe_key: Mapped[str] = mapped_column(String(255))
    label: Mapped[str] = mapped_column(String(160))
    risk: Mapped[str] = mapped_column(String(24))
    severity: Mapped[str] = mapped_column(String(16))
    title: Mapped[str] = mapped_column(String(255))
    message: Mapped[str] = mapped_column(Text)
    action: Mapped[str] = mapped_column(String(32))
    period_end: Mapped[date | None] = mapped_column(Date, nullable=True)
    remaining_occurrences: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(16), default="OPEN", index=True)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    dismissed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AvailabilityWindow(Base):
    """A local-time execution window available on a weekday."""

    __tablename__ = "availability_windows"
    __table_args__ = (Index("idx_availability_windows_goal_weekday", "goal_id", "weekday"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    goal_id: Mapped[str] = mapped_column(ForeignKey("goals.id", ondelete="CASCADE"), index=True)
    weekday: Mapped[int] = mapped_column(Integer)
    start_local: Mapped[str] = mapped_column(String(5))
    end_local: Mapped[str] = mapped_column(String(5))
    timezone: Mapped[str] = mapped_column(String(64), default="Asia/Shanghai")
    valid_from: Mapped[date | None] = mapped_column(Date, nullable=True)
    valid_to: Mapped[date | None] = mapped_column(Date, nullable=True)
    source: Mapped[str] = mapped_column(String(16), default="USER")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )


class AvailabilityException(Base):
    """A date-specific override to the recurring availability windows."""

    __tablename__ = "availability_exceptions"
    __table_args__ = (Index("idx_availability_exceptions_goal_date", "goal_id", "exception_date"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    goal_id: Mapped[str] = mapped_column(ForeignKey("goals.id", ondelete="CASCADE"), index=True)
    exception_date: Mapped[date] = mapped_column(Date)
    kind: Mapped[str] = mapped_column(String(16), default="UNAVAILABLE")
    start_local: Mapped[str | None] = mapped_column(String(5), nullable=True)
    end_local: Mapped[str | None] = mapped_column(String(5), nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    source: Mapped[str] = mapped_column(String(16), default="USER")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class TaskTimeBlock(Base):
    """A concrete local-time slot assigned to one plan task."""

    __tablename__ = "task_time_blocks"
    __table_args__ = (
        Index("idx_task_time_blocks_goal_date", "goal_id", "scheduled_date"),
        Index("idx_task_time_blocks_plan_day", "plan_day_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    goal_id: Mapped[str] = mapped_column(ForeignKey("goals.id", ondelete="CASCADE"), index=True)
    plan_version_id: Mapped[str] = mapped_column(
        ForeignKey("plan_versions.id", ondelete="CASCADE"), index=True
    )
    plan_day_id: Mapped[str] = mapped_column(
        ForeignKey("plan_days.id", ondelete="CASCADE"), index=True
    )
    task_id: Mapped[str] = mapped_column(
        ForeignKey("plan_tasks.id", ondelete="CASCADE"), index=True
    )
    scheduled_date: Mapped[date] = mapped_column(Date, index=True)
    start_local: Mapped[str] = mapped_column(String(5))
    end_local: Mapped[str] = mapped_column(String(5))
    timezone: Mapped[str] = mapped_column(String(64), default="Asia/Shanghai")
    source: Mapped[str] = mapped_column(String(16), default="SYSTEM")
    status: Mapped[str] = mapped_column(String(16), default="PLANNED", index=True)
    parallel_confirmed: Mapped[bool] = mapped_column(default=False)
    parallel_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    change_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )


class ScheduleParallelConsent(Base):
    """Consent belongs to a pair of task slots, never to a task globally."""

    __tablename__ = "schedule_parallel_consents"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    first_task_id: Mapped[str] = mapped_column(String(36), index=True)
    second_task_id: Mapped[str] = mapped_column(String(36), index=True)
    scheduled_date: Mapped[date] = mapped_column(Date, index=True)
    first_start: Mapped[str] = mapped_column(String(5))
    first_end: Mapped[str] = mapped_column(String(5))
    second_start: Mapped[str] = mapped_column(String(5))
    second_end: Mapped[str] = mapped_column(String(5))
    reason: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class ScheduleChange(Base):
    """Append-only audit record for schedule preference/window/block changes."""

    __tablename__ = "schedule_changes"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    goal_id: Mapped[str] = mapped_column(ForeignKey("goals.id", ondelete="CASCADE"), index=True)
    entity_type: Mapped[str] = mapped_column(String(24))
    entity_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    change_kind: Mapped[str] = mapped_column(String(24))
    before_json: Mapped[dict] = mapped_column(JSON, default=dict)
    after_json: Mapped[dict] = mapped_column(JSON, default=dict)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class WorkspaceScheduleProfile(Base):
    """Shared schedule settings used by every plan in this workspace."""

    __tablename__ = "workspace_schedule_profiles"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    timezone: Mapped[str] = mapped_column(String(64), default="Asia/Shanghai")
    default_mode: Mapped[str] = mapped_column(String(16), default="FLEXIBLE")
    break_minutes: Mapped[int] = mapped_column(Integer, default=10)
    availability_mode: Mapped[str] = mapped_column(String(16), default="ROUTINE")
    wake_time: Mapped[str] = mapped_column(String(5), default="07:00")
    available_from: Mapped[str] = mapped_column(String(5), default="08:00")
    breakfast_enabled: Mapped[bool] = mapped_column(default=True)
    breakfast_start: Mapped[str] = mapped_column(String(5), default="07:30")
    breakfast_end: Mapped[str] = mapped_column(String(5), default="08:00")
    lunch_enabled: Mapped[bool] = mapped_column(default=True)
    lunch_start: Mapped[str] = mapped_column(String(5), default="12:00")
    lunch_end: Mapped[str] = mapped_column(String(5), default="13:00")
    nap_enabled: Mapped[bool] = mapped_column(default=True)
    nap_start: Mapped[str] = mapped_column(String(5), default="13:00")
    nap_end: Mapped[str] = mapped_column(String(5), default="14:00")
    dinner_enabled: Mapped[bool] = mapped_column(default=True)
    dinner_start: Mapped[str] = mapped_column(String(5), default="18:00")
    dinner_end: Mapped[str] = mapped_column(String(5), default="19:00")
    sleep_time: Mapped[str] = mapped_column(String(5), default="23:00")
    routine_exclusions_json: Mapped[list] = mapped_column(JSON, default=list)
    schedule_style: Mapped[str] = mapped_column(Text, default="")
    holiday_country_code: Mapped[str | None] = mapped_column(String(2), nullable=True)
    holiday_subdivision_code: Mapped[str | None] = mapped_column(String(16), nullable=True)
    holiday_locale: Mapped[str | None] = mapped_column(String(32), nullable=True)
    holiday_source: Mapped[str] = mapped_column(String(24), default="UNSET")
    holiday_confidence: Mapped[float] = mapped_column(Float, default=0.0)
    holiday_confirmed: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )


class WorkspaceAvailabilityWindow(Base):
    """Recurring availability shared by all plans."""

    __tablename__ = "workspace_availability_windows"
    __table_args__ = (Index("idx_workspace_availability_weekday", "profile_id", "weekday"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    profile_id: Mapped[str] = mapped_column(
        ForeignKey("workspace_schedule_profiles.id", ondelete="CASCADE"), index=True
    )
    weekday: Mapped[int] = mapped_column(Integer)
    start_local: Mapped[str] = mapped_column(String(5))
    end_local: Mapped[str] = mapped_column(String(5))
    valid_from: Mapped[date | None] = mapped_column(Date, nullable=True)
    valid_to: Mapped[date | None] = mapped_column(Date, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class HolidayCalendarYear(Base):
    """Cached holiday calendar for one country/subdivision and year."""

    __tablename__ = "holiday_calendar_years"
    __table_args__ = (
        UniqueConstraint(
            "country_code", "subdivision_code", "year", name="uq_holiday_calendar_region_year"
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    country_code: Mapped[str] = mapped_column(String(2), index=True)
    subdivision_code: Mapped[str] = mapped_column(String(16), default="")
    year: Mapped[int] = mapped_column(Integer, index=True)
    days_json: Mapped[list] = mapped_column(JSON, default=list)
    source: Mapped[str] = mapped_column(String(80))
    authoritative: Mapped[bool] = mapped_column(default=False)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class ManualPlan(Base):
    """A quick plan; the Python name is retained for source compatibility."""

    __tablename__ = "quick_plans"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    title: Mapped[str] = mapped_column(String(160))
    description: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(24), default="ACTIVE", index=True)
    timezone: Mapped[str] = mapped_column(String(64), default="Asia/Shanghai")
    kind: Mapped[str] = mapped_column(String(16), default="QUICK")
    arrangement_json: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )


class LightArrangementDraft(Base):
    __tablename__ = "quick_plan_drafts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    raw_input: Mapped[str] = mapped_column(Text)
    timezone: Mapped[str] = mapped_column(String(64))
    start_date: Mapped[date] = mapped_column(Date)
    horizon_days: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16), default="DRAFT")
    revision: Mapped[int] = mapped_column(Integer, default=1)
    result_json: Mapped[dict] = mapped_column(JSON, default=dict)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    manual_plan_id: Mapped[str | None] = mapped_column(
        "quick_plan_id",
        ForeignKey("quick_plans.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        onupdate=utc_now,
    )


class ManualTask(Base):
    """Task belonging to a quick plan; dates and blocks are optional."""

    __tablename__ = "quick_plan_tasks"
    __table_args__ = (
        Index("idx_quick_plan_tasks_plan_order", "quick_plan_id", "order_in_plan"),
        Index("idx_manual_tasks_scheduled_date", "scheduled_date"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    manual_plan_id: Mapped[str] = mapped_column(
        "quick_plan_id", ForeignKey("quick_plans.id", ondelete="CASCADE"), index=True
    )
    order_in_plan: Mapped[int] = mapped_column(Integer, default=1)
    title: Mapped[str] = mapped_column(String(180))
    description: Mapped[str] = mapped_column(Text, default="")
    estimate_minutes: Mapped[int] = mapped_column(Integer)
    priority: Mapped[int] = mapped_column(Integer, default=3)
    scheduled_date: Mapped[date | None] = mapped_column(Date, nullable=True, index=True)
    recurrence: Mapped[str] = mapped_column(String(16), default="ONCE")
    recurrence_weekdays_json: Mapped[list] = mapped_column(JSON, default=list)
    recurrence_end_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    start_local: Mapped[str | None] = mapped_column(String(5), nullable=True)
    end_local: Mapped[str | None] = mapped_column(String(5), nullable=True)
    parallel_confirmed: Mapped[bool] = mapped_column(default=False)
    parallel_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(24), default="NOT_STARTED", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )


class ManualTaskDateOverride(Base):
    """Per-date exception for a recurring quick-plan task."""

    __tablename__ = "quick_plan_task_date_overrides"
    __table_args__ = (
        UniqueConstraint(
            "quick_plan_task_id", "override_date", name="uq_quick_plan_task_override_date"
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    manual_task_id: Mapped[str] = mapped_column(
        "quick_plan_task_id",
        ForeignKey("quick_plan_tasks.id", ondelete="CASCADE"),
        index=True,
    )
    override_date: Mapped[date] = mapped_column(Date, index=True)
    title: Mapped[str | None] = mapped_column(String(180), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    estimate_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    priority: Mapped[int | None] = mapped_column(Integer, nullable=True)
    start_local: Mapped[str | None] = mapped_column(String(5), nullable=True)
    end_local: Mapped[str | None] = mapped_column(String(5), nullable=True)
    time_overridden: Mapped[bool] = mapped_column(default=False)
    status: Mapped[str | None] = mapped_column(String(24), nullable=True)
    is_deleted: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )


class ProgressEvent(Base):
    __tablename__ = "progress_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    goal_id: Mapped[str] = mapped_column(ForeignKey("goals.id", ondelete="CASCADE"), index=True)
    plan_version_id: Mapped[str] = mapped_column(
        ForeignKey("plan_versions.id", ondelete="CASCADE"), index=True
    )
    plan_day_id: Mapped[str] = mapped_column(
        ForeignKey("plan_days.id", ondelete="CASCADE"), index=True
    )
    task_id: Mapped[str | None] = mapped_column(
        ForeignKey("plan_tasks.id", ondelete="CASCADE"), nullable=True, index=True
    )
    event_type: Mapped[str] = mapped_column(String(24), index=True)
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class Feedback(Base):
    __tablename__ = "feedbacks"
    __table_args__ = (
        Index("idx_feedbacks_goal_day_created", "goal_id", "plan_day_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    goal_id: Mapped[str] = mapped_column(ForeignKey("goals.id", ondelete="CASCADE"), index=True)
    plan_version_id: Mapped[str] = mapped_column(
        ForeignKey("plan_versions.id", ondelete="CASCADE"), index=True
    )
    plan_day_id: Mapped[str] = mapped_column(
        ForeignKey("plan_days.id", ondelete="CASCADE"), index=True
    )
    task_id: Mapped[str | None] = mapped_column(
        ForeignKey("plan_tasks.id", ondelete="CASCADE"), nullable=True, index=True
    )
    feedback_kind: Mapped[str] = mapped_column(String(24), index=True)
    reason_code: Mapped[str] = mapped_column(String(40), index=True)
    intent_code: Mapped[str | None] = mapped_column(String(40), nullable=True)
    detail_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(24), default="RECORDED", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class ExecutionInsight(Base):
    __tablename__ = "execution_insights"
    __table_args__ = (Index("idx_execution_insights_goal_status", "goal_id", "status"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    goal_id: Mapped[str] = mapped_column(ForeignKey("goals.id", ondelete="CASCADE"), index=True)
    plan_version_id: Mapped[str] = mapped_column(
        ForeignKey("plan_versions.id", ondelete="CASCADE"), index=True
    )
    source_message_id: Mapped[str] = mapped_column(
        ForeignKey("messages.id", ondelete="CASCADE"), index=True
    )
    insight_type: Mapped[str] = mapped_column(String(32), index=True)
    content: Mapped[str] = mapped_column(Text)
    scope: Mapped[str] = mapped_column(String(32), default="GOAL")
    confidence: Mapped[float] = mapped_column(Float, default=0.5)
    status: Mapped[str] = mapped_column(String(16), default="PROPOSED", index=True)
    source_type: Mapped[str] = mapped_column(String(24), default="AGENT_INFERENCE")
    evidence_span: Mapped[str | None] = mapped_column(Text, nullable=True)
    confirmation_policy: Mapped[str] = mapped_column(String(24), default="USER_REVIEW")
    valid_from: Mapped[date | None] = mapped_column(Date, nullable=True)
    valid_until: Mapped[date | None] = mapped_column(Date, nullable=True)
    memory_key: Mapped[str | None] = mapped_column(String(80), nullable=True, index=True)
    supersedes_id: Mapped[str | None] = mapped_column(
        ForeignKey("execution_insights.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ExecutionReview(Base):
    __tablename__ = "execution_reviews"
    __table_args__ = (Index("idx_execution_reviews_goal_status", "goal_id", "status"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    goal_id: Mapped[str] = mapped_column(ForeignKey("goals.id", ondelete="CASCADE"), index=True)
    plan_version_id: Mapped[str] = mapped_column(
        ForeignKey("plan_versions.id", ondelete="CASCADE"), index=True
    )
    source_message_id: Mapped[str | None] = mapped_column(
        ForeignKey("messages.id", ondelete="SET NULL"), nullable=True, index=True
    )
    trigger_type: Mapped[str] = mapped_column(String(32), index=True)
    status: Mapped[str] = mapped_column(String(16), default="SUGGESTED", index=True)
    reason: Mapped[str] = mapped_column(Text)
    evidence_json: Mapped[dict] = mapped_column(JSON, default=dict)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    decision: Mapped[str | None] = mapped_column(String(32), nullable=True)
    not_before: Mapped[date | None] = mapped_column(Date, nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )


class FeedbackAdjustment(Base):
    __tablename__ = "feedback_adjustments"
    __table_args__ = (UniqueConstraint("feedback_id", name="uq_feedback_adjustments_feedback_id"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    feedback_id: Mapped[str] = mapped_column(
        ForeignKey("feedbacks.id", ondelete="CASCADE"), index=True
    )
    goal_id: Mapped[str] = mapped_column(ForeignKey("goals.id", ondelete="CASCADE"), index=True)
    plan_version_id: Mapped[str] = mapped_column(
        ForeignKey("plan_versions.id", ondelete="CASCADE"), index=True
    )
    adjustment_kind: Mapped[str] = mapped_column(String(32), index=True)
    status: Mapped[str] = mapped_column(String(24), default="PENDING_REVIEW", index=True)
    title: Mapped[str] = mapped_column(String(180))
    summary: Mapped[str] = mapped_column(Text)
    rationale: Mapped[str] = mapped_column(Text)
    impact_json: Mapped[list] = mapped_column(JSON, default=list)
    operation_json: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ExecutionDayLog(Base):
    __tablename__ = "execution_day_logs"
    __table_args__ = (UniqueConstraint("plan_day_id", name="uq_execution_day_logs_plan_day_id"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    goal_id: Mapped[str] = mapped_column(ForeignKey("goals.id", ondelete="CASCADE"), index=True)
    plan_version_id: Mapped[str] = mapped_column(
        ForeignKey("plan_versions.id", ondelete="CASCADE"), index=True
    )
    plan_day_id: Mapped[str] = mapped_column(
        ForeignKey("plan_days.id", ondelete="CASCADE"), index=True
    )
    scheduled_date: Mapped[date] = mapped_column(Date, index=True)
    status: Mapped[str] = mapped_column(String(24), default="IN_PROGRESS", index=True)
    summary: Mapped[str] = mapped_column(Text)
    metrics_json: Mapped[dict] = mapped_column(JSON, default=dict)
    adjustments_json: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )


class FocusSession(Base):
    __tablename__ = "focus_sessions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    goal_id: Mapped[str] = mapped_column(ForeignKey("goals.id", ondelete="CASCADE"), index=True)
    plan_version_id: Mapped[str] = mapped_column(
        ForeignKey("plan_versions.id", ondelete="CASCADE"), index=True
    )
    plan_day_id: Mapped[str] = mapped_column(
        ForeignKey("plan_days.id", ondelete="CASCADE"), index=True
    )
    task_id: Mapped[str | None] = mapped_column(
        ForeignKey("plan_tasks.id", ondelete="SET NULL"), nullable=True, index=True
    )
    task_title: Mapped[str | None] = mapped_column(String(180), nullable=True)
    planned_minutes: Mapped[int] = mapped_column(Integer)
    actual_seconds: Mapped[int] = mapped_column(Integer)
    interruptions: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(24), index=True)
    completed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class QuickCapture(Base):
    __tablename__ = "quick_captures"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    goal_id: Mapped[str] = mapped_column(ForeignKey("goals.id", ondelete="CASCADE"), index=True)
    content: Mapped[str] = mapped_column(String(500))
    status: Mapped[str] = mapped_column(String(24), default="INBOX", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class ContextPack(Base):
    __tablename__ = "context_packs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    goal_id: Mapped[str] = mapped_column(ForeignKey("goals.id", ondelete="CASCADE"), index=True)
    requirement_version_id: Mapped[str] = mapped_column(
        ForeignKey("requirement_versions.id", ondelete="CASCADE"), index=True
    )
    route_version_id: Mapped[str] = mapped_column(
        ForeignKey("route_versions.id", ondelete="CASCADE"), index=True
    )
    plan_version_id: Mapped[str | None] = mapped_column(
        ForeignKey("plan_versions.id", ondelete="SET NULL"), nullable=True, index=True
    )
    version: Mapped[int] = mapped_column(Integer, default=1)
    facts_json: Mapped[dict] = mapped_column(JSON, default=dict)
    locked_day_ids_json: Mapped[list] = mapped_column(JSON, default=list)
    locked_task_ids_json: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class PlanningBrief(Base):
    __tablename__ = "planning_briefs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    goal_id: Mapped[str] = mapped_column(ForeignKey("goals.id", ondelete="CASCADE"), index=True)
    context_pack_id: Mapped[str] = mapped_column(
        ForeignKey("context_packs.id", ondelete="CASCADE"), index=True
    )
    plan_version_id: Mapped[str | None] = mapped_column(
        ForeignKey("plan_versions.id", ondelete="SET NULL"), nullable=True, index=True
    )
    brief_kind: Mapped[str] = mapped_column(String(24), index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(24), default="DRAFT", index=True)
    brief_json: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class GenerationBatch(Base):
    __tablename__ = "generation_batches"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_generation_batches_idempotency_key"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    goal_id: Mapped[str] = mapped_column(ForeignKey("goals.id", ondelete="CASCADE"), index=True)
    plan_version_id: Mapped[str | None] = mapped_column(
        ForeignKey("plan_versions.id", ondelete="SET NULL"), nullable=True, index=True
    )
    brief_id: Mapped[str] = mapped_column(
        ForeignKey("planning_briefs.id", ondelete="CASCADE"), index=True
    )
    idempotency_key: Mapped[str] = mapped_column(String(160), unique=True, index=True)
    scope_type: Mapped[str] = mapped_column(String(24), index=True)
    scope_json: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(24), default="QUEUED", index=True)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    failure_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    failure_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    staged_result_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )


class GenerationRun(Base):
    """Durable audit envelope for model-assisted planning operations."""

    __tablename__ = "generation_runs"
    __table_args__ = (
        Index("idx_generation_runs_goal_task_created", "goal_id", "task_type", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    goal_id: Mapped[str] = mapped_column(ForeignKey("goals.id", ondelete="CASCADE"), index=True)
    plan_version_id: Mapped[str | None] = mapped_column(
        ForeignKey("plan_versions.id", ondelete="SET NULL"), nullable=True, index=True
    )
    context_pack_id: Mapped[str | None] = mapped_column(
        ForeignKey("context_packs.id", ondelete="SET NULL"), nullable=True, index=True
    )
    task_type: Mapped[str] = mapped_column(String(40), index=True)
    tier: Mapped[str] = mapped_column(String(8))
    status: Mapped[str] = mapped_column(String(24), default="RUNNING", index=True)
    context_hash: Mapped[str] = mapped_column(String(64), index=True)
    decision_json: Mapped[dict] = mapped_column(JSON, default=dict)
    validation_json: Mapped[dict] = mapped_column(JSON, default=dict)
    input_units: Mapped[int] = mapped_column(Integer, default=0)
    output_units: Mapped[int] = mapped_column(Integer, default=0)
    estimated_cost_units: Mapped[int] = mapped_column(Integer, default=0)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )


class AgentReport(Base):
    """Read-only specialist output consumed by the authoritative generator."""

    __tablename__ = "agent_reports"
    __table_args__ = (
        UniqueConstraint(
            "generation_run_id", "role", "input_hash", name="uq_agent_report_run_role_input"
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    generation_run_id: Mapped[str] = mapped_column(
        ForeignKey("generation_runs.id", ondelete="CASCADE"), index=True
    )
    role: Mapped[str] = mapped_column(String(40), index=True)
    status: Mapped[str] = mapped_column(String(24), default="RUNNING", index=True)
    input_hash: Mapped[str] = mapped_column(String(64), index=True)
    report_json: Mapped[dict] = mapped_column(JSON, default=dict)
    evidence_json: Mapped[list] = mapped_column(JSON, default=list)
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    input_units: Mapped[int] = mapped_column(Integer, default=0)
    output_units: Mapped[int] = mapped_column(Integer, default=0)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class MaterialRoot(Base):
    """A user-approved local directory from which files may be referenced."""

    __tablename__ = "material_roots"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    display_name: Mapped[str] = mapped_column(String(160))
    canonical_path: Mapped[str] = mapped_column(Text, unique=True)
    status: Mapped[str] = mapped_column(String(16), default="ACTIVE", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )


class MaterialSource(Base):
    """Logical material identity; mutable content is stored as immutable revisions."""

    __tablename__ = "material_sources"
    __table_args__ = (
        UniqueConstraint("root_id", "relative_path", name="uq_material_source_local_path"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    kind: Mapped[str] = mapped_column(String(24), default="LOCAL_FILE", index=True)
    root_id: Mapped[str | None] = mapped_column(
        ForeignKey("material_roots.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    relative_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    canonical_uri: Mapped[str] = mapped_column(Text)
    title: Mapped[str] = mapped_column(String(300))
    mime_type: Mapped[str | None] = mapped_column(String(160), nullable=True)
    status: Mapped[str] = mapped_column(String(24), default="ACTIVE", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )


class MaterialRevision(Base):
    """Immutable fingerprint and processing state for one source version."""

    __tablename__ = "material_revisions"
    __table_args__ = (
        UniqueConstraint("source_id", "revision", name="uq_material_revision_number"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    source_id: Mapped[str] = mapped_column(
        ForeignKey("material_sources.id", ondelete="CASCADE"), index=True
    )
    revision: Mapped[int] = mapped_column(Integer)
    content_hash: Mapped[str] = mapped_column(String(64), index=True)
    file_size: Mapped[int] = mapped_column(BigInteger)
    source_modified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    parser_name: Mapped[str] = mapped_column(String(64), default="UNASSIGNED")
    parser_version: Mapped[str | None] = mapped_column(String(40), nullable=True)
    status: Mapped[str] = mapped_column(String(24), default="QUEUED", index=True)
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class MaterialProcessingUnit(Base):
    """Retryable page, section, sheet, or row-range unit for long materials."""

    __tablename__ = "material_processing_units"
    __table_args__ = (
        UniqueConstraint("revision_id", "sequence", name="uq_material_processing_unit_sequence"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    revision_id: Mapped[str] = mapped_column(
        ForeignKey("material_revisions.id", ondelete="CASCADE"), index=True
    )
    sequence: Mapped[int] = mapped_column(Integer)
    unit_type: Mapped[str] = mapped_column(String(24))
    range_start: Mapped[int | None] = mapped_column(Integer, nullable=True)
    range_end: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(24), default="QUEUED", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    checkpoint_json: Mapped[dict] = mapped_column(JSON, default=dict)
    result_artifact_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )


class MaterialProcessingJob(Base):
    """Durable queue and lease for processing one immutable material revision."""

    __tablename__ = "material_processing_jobs"
    __table_args__ = (
        UniqueConstraint("revision_id", name="uq_material_processing_job_revision"),
        Index("ix_material_processing_jobs_status_created_at", "status", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    revision_id: Mapped[str] = mapped_column(
        ForeignKey("material_revisions.id", ondelete="CASCADE"), index=True
    )
    status: Mapped[str] = mapped_column(String(24), default="QUEUED", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    queued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    queue_wait_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    processing_duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cancel_requested_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class MaterialChunk(Base):
    """Bounded searchable text extracted from one immutable material revision."""

    __tablename__ = "material_chunks"
    __table_args__ = (
        UniqueConstraint("revision_id", "sequence", name="uq_material_chunk_sequence"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    revision_id: Mapped[str] = mapped_column(
        ForeignKey("material_revisions.id", ondelete="CASCADE"), index=True
    )
    sequence: Mapped[int] = mapped_column(Integer)
    section_title: Mapped[str] = mapped_column(String(300), default="")
    text: Mapped[str] = mapped_column(Text)
    char_start: Mapped[int] = mapped_column(Integer, default=0)
    char_end: Mapped[int] = mapped_column(Integer, default=0)
    locator_json: Mapped[list] = mapped_column(JSON, default=list)
    content_hash: Mapped[str] = mapped_column(String(64), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class MaterialEmbedding(Base):
    """Model-specific normalized vectors; extracted text remains the source of truth."""

    __tablename__ = "material_embeddings"
    __table_args__ = (UniqueConstraint("chunk_id", "model", name="uq_material_embedding_model"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    chunk_id: Mapped[str] = mapped_column(ForeignKey("material_chunks.id", ondelete="CASCADE"), index=True)
    model: Mapped[str] = mapped_column(String(300), index=True)
    content_hash: Mapped[str] = mapped_column(String(64))
    vector_json: Mapped[list] = mapped_column(JSON)


class GoalMaterialLink(Base):
    """Goal-scoped access and usage policy for a material source."""

    __tablename__ = "goal_material_links"
    __table_args__ = (
        UniqueConstraint("goal_id", "source_id", name="uq_goal_material_link"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    goal_id: Mapped[str] = mapped_column(
        ForeignKey("goals.id", ondelete="CASCADE"), index=True
    )
    source_id: Mapped[str] = mapped_column(
        ForeignKey("material_sources.id", ondelete="CASCADE"), index=True
    )
    scope: Mapped[str] = mapped_column(String(24), default="ALL")
    usage_policy: Mapped[str] = mapped_column(String(24), default="AUTO")
    pinned: Mapped[bool] = mapped_column(default=False)
    status: Mapped[str] = mapped_column(String(16), default="ACTIVE", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )


class MaterialAnalysis(Base):
    """A bounded AI reading result over retrieved local material chunks."""

    __tablename__ = "material_analyses"
    __table_args__ = (
        Index("ix_material_analyses_goal_created_at", "goal_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    goal_id: Mapped[str] = mapped_column(ForeignKey("goals.id", ondelete="CASCADE"), index=True)
    query: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(24), default="COMPLETED", index=True)
    result_json: Mapped[dict] = mapped_column(JSON, default=dict)
    source_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )


class ResearchRun(Base):
    """Durable state for one bounded web-research workflow."""

    __tablename__ = "research_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    goal_id: Mapped[str] = mapped_column(
        ForeignKey("goals.id", ondelete="CASCADE"), index=True
    )
    conversation_id: Mapped[str | None] = mapped_column(
        ForeignKey("conversations.id", ondelete="SET NULL"), nullable=True, index=True
    )
    question: Mapped[str] = mapped_column(Text)
    enabled: Mapped[bool] = mapped_column(default=True)
    status: Mapped[str] = mapped_column(String(24), default="CREATED", index=True)
    current_stage: Mapped[str] = mapped_column(String(24), default="CREATED")
    resume_stage: Mapped[str | None] = mapped_column(String(24), nullable=True)
    schema_version: Mapped[int] = mapped_column(Integer, default=1)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    recovery_count: Mapped[int] = mapped_column(Integer, default=0)
    idempotency_key: Mapped[str | None] = mapped_column(
        String(180), nullable=True, unique=True, index=True
    )
    lease_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    last_heartbeat_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    cancel_requested_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    cancel_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
    decision_json: Mapped[dict] = mapped_column(JSON, default=dict)
    budget_json: Mapped[dict] = mapped_column(JSON, default=dict)
    checkpoint_json: Mapped[dict] = mapped_column(JSON, default=dict)
    result_json: Mapped[dict] = mapped_column(JSON, default=dict)
    stop_reason: Mapped[str | None] = mapped_column(String(48), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ResearchResponseRequest(Base):
    """The durable handoff from optional research to one phase response."""

    __tablename__ = "research_response_requests"
    __table_args__ = (Index(
        "uq_research_response_active_goal", "goal_id", unique=True,
        sqlite_where=text("status IN ('WAITING_RESEARCH', 'QUEUED_ANSWER', 'ANSWERING')"),
        postgresql_where=text("status IN ('WAITING_RESEARCH', 'QUEUED_ANSWER', 'ANSWERING')"),
    ),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    goal_id: Mapped[str] = mapped_column(ForeignKey("goals.id", ondelete="CASCADE"), index=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("research_runs.id", ondelete="CASCADE"))
    conversation_id: Mapped[str] = mapped_column(ForeignKey("conversations.id", ondelete="CASCADE"))
    phase: Mapped[str] = mapped_column(String(24))
    intent: Mapped[str] = mapped_column(String(24))
    question: Mapped[str] = mapped_column(Text)
    idempotency_key: Mapped[str] = mapped_column(String(160), unique=True)
    status: Mapped[str] = mapped_column(String(24), default="WAITING_RESEARCH", index=True)
    snapshot_json: Mapped[dict] = mapped_column(JSON, default=dict)
    fallback: Mapped[bool] = mapped_column(default=False)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    response_message_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)


class ResearchEvidence(Base):
    """A deduplicated source observed during a research run."""

    __tablename__ = "research_evidence"
    __table_args__ = (
        UniqueConstraint("run_id", "canonical_url", name="uq_research_evidence_url"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    run_id: Mapped[str] = mapped_column(
        ForeignKey("research_runs.id", ondelete="CASCADE"), index=True
    )
    canonical_url: Mapped[str] = mapped_column(Text)
    url: Mapped[str] = mapped_column(Text)
    title: Mapped[str] = mapped_column(String(300), default="")
    source: Mapped[str] = mapped_column(String(160), default="")
    engines_json: Mapped[list] = mapped_column(JSON, default=list)
    snippet: Mapped[str] = mapped_column(Text, default="")
    content_excerpt: Mapped[str] = mapped_column(Text, default="")
    relevance_score: Mapped[float] = mapped_column(Float, default=0.0)
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    ranking_json: Mapped[dict] = mapped_column(JSON, default=dict)
    read_status: Mapped[str] = mapped_column(String(24), default="NOT_SELECTED", index=True)
    retrieval_method: Mapped[str] = mapped_column(String(40), default="")
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )


class RecoveryOperation(Base):
    __tablename__ = "recovery_operations"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_recovery_operations_idempotency_key"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    goal_id: Mapped[str] = mapped_column(ForeignKey("goals.id", ondelete="CASCADE"), index=True)
    source_plan_version_id: Mapped[str] = mapped_column(
        ForeignKey("plan_versions.id", ondelete="CASCADE"), index=True
    )
    result_plan_version_id: Mapped[str | None] = mapped_column(
        ForeignKey("plan_versions.id", ondelete="SET NULL"), nullable=True, index=True
    )
    mode: Mapped[str] = mapped_column(String(32), index=True)
    status: Mapped[str] = mapped_column(String(24), default="PREVIEW", index=True)
    idempotency_key: Mapped[str] = mapped_column(String(180), unique=True, index=True)
    preview_json: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class PlanAdjustmentPreview(Base):
    __tablename__ = "plan_adjustment_previews"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    goal_id: Mapped[str] = mapped_column(ForeignKey("goals.id", ondelete="CASCADE"), index=True)
    source_plan_version_id: Mapped[str] = mapped_column(
        ForeignKey("plan_versions.id", ondelete="CASCADE"), index=True
    )
    status: Mapped[str] = mapped_column(String(24), default="PREVIEW", index=True)
    fingerprint: Mapped[str] = mapped_column(String(64), index=True)
    preview_json: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class FixedActivityMovePreview(Base):
    __tablename__ = "fixed_activity_move_previews"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    goal_id: Mapped[str] = mapped_column(ForeignKey("goals.id", ondelete="CASCADE"), index=True)
    status: Mapped[str] = mapped_column(String(16), default="PREVIEW")
    fingerprint: Mapped[str] = mapped_column(String(64))
    preview_json: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class GoalLongTermMemory(Base):
    """Durable user context; evictable vectors are only a retrieval projection."""
    __tablename__ = "goal_long_term_memories"
    __table_args__ = (UniqueConstraint("goal_id", "memory_key", name="uq_goal_long_memory_key"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    goal_id: Mapped[str] = mapped_column(ForeignKey("goals.id", ondelete="CASCADE"), index=True)
    memory_key: Mapped[str] = mapped_column(String(100))
    content: Mapped[str] = mapped_column(Text)
    evidence_quote: Mapped[str] = mapped_column(Text)
    source_message_id: Mapped[str] = mapped_column(ForeignKey("messages.id", ondelete="CASCADE"))
    active: Mapped[bool] = mapped_column(default=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class KnowledgeDocument(Base):
    """Evictable original-text cache, independent of source ownership and truth status."""

    __tablename__ = "knowledge_documents"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    identity: Mapped[str] = mapped_column(String(64), unique=True)
    kind: Mapped[str] = mapped_column(String(16))
    source_uri: Mapped[str] = mapped_column(Text)
    title: Mapped[str] = mapped_column(String(300))
    content_hash: Mapped[str] = mapped_column(String(64))
    material_revision_id: Mapped[str | None] = mapped_column(
        ForeignKey("material_revisions.id", ondelete="CASCADE"), nullable=True, index=True)
    body: Mapped[str] = mapped_column(Text, default="")
    shared: Mapped[bool] = mapped_column(default=False)
    current: Mapped[bool] = mapped_column(default=True)
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict)
    byte_size: Mapped[int] = mapped_column(BigInteger, default=0)
    chunk_count: Mapped[int] = mapped_column(Integer, default=0)
    accessed_count: Mapped[int] = mapped_column(Integer, default=0)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    last_accessed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class KnowledgeGoalLink(Base):
    __tablename__ = "knowledge_goal_links"
    __table_args__ = (UniqueConstraint("goal_id", "document_id", name="uq_knowledge_goal_document"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    goal_id: Mapped[str] = mapped_column(ForeignKey("goals.id", ondelete="CASCADE"), index=True)
    document_id: Mapped[str] = mapped_column(ForeignKey("knowledge_documents.id", ondelete="CASCADE"), index=True)


class KnowledgeChunk(Base):
    __tablename__ = "knowledge_chunks"
    __table_args__ = (UniqueConstraint("document_id", "sequence", name="uq_knowledge_chunk_sequence"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    document_id: Mapped[str] = mapped_column(ForeignKey("knowledge_documents.id", ondelete="CASCADE"), index=True)
    sequence: Mapped[int] = mapped_column(Integer)
    text: Mapped[str] = mapped_column(Text)
    heading: Mapped[str] = mapped_column(String(300), default="")
    char_start: Mapped[int] = mapped_column(Integer)
    char_end: Mapped[int] = mapped_column(Integer)
    locator_json: Mapped[dict] = mapped_column(JSON, default=dict)
    content_hash: Mapped[str] = mapped_column(String(64))
    vector_json: Mapped[list] = mapped_column(JSON, default=list)
    vector_model: Mapped[str] = mapped_column(String(300), default="")
    index_status: Mapped[str] = mapped_column(String(16), default="PENDING", index=True)
    milvus_status: Mapped[str] = mapped_column(String(16), default="PENDING", index=True)
    milvus_attempts: Mapped[int] = mapped_column(Integer, default=0)
    milvus_target: Mapped[str] = mapped_column(String(64), default="")
    milvus_lease_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    milvus_lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    index_attempts: Mapped[int] = mapped_column(Integer, default=0)
    lease_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
