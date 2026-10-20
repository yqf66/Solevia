from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Any
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import func, select, update, exists
from sqlalchemy.orm import Session, aliased

from planner_api.ai import (
    ClarificationContext,
    ClarificationProvider,
    ClarificationProviderError,
)
from planner_api.config import get_settings
from planner_api.context_compiler import compile_user_text
from planner_api.goal_error_memory import (
    explicit_error_memory_candidates,
    persist_explicit_error_memories,
)
from planner_api.materials.analysis_ai import build_material_analysis_provider
from planner_api.materials.analysis_service import MaterialAnalysisService
from planner_api.models import (
    ClarificationAnalysisJob,
    Conversation,
    ConversationSummary,
    Goal,
    GoalErrorMemory,
    Message,
    RequirementChange,
    RequirementFact,
    RequirementVersion,
    ResearchRun,
    RouteVersion,
    UserMemoryItem,
    utc_now,
)
from planner_api.planning_approach import recommend_planning_approach
from planner_api.temporal_contract import local_date, resolve_deadline, snapshot_deadline
from planner_api.prompts import CLARIFICATION_PROMPT_VERSION
from planner_api.research.consumer import ResearchContextService, research_summary_from_run
from planner_api.scheduling_approach import recommend_schedule_type
from planner_api.schemas import (
    CORE_FIELDS,
    ClarificationAnalysisJobRead,
    ClarificationResult,
    ClarificationWorkspace,
    ConversationMemoryRead,
    ConversationReply,
    FactRead,
    GoalListItem,
    MessageRead,
    PlanningApproachRecommendation,
    ReadinessResult,
    RequirementAnalysis,
    RequirementChangeRead,
    RequirementDraftRead,
    ScheduleTypeRecommendation,
)

FIELD_CATEGORY = {
    "objective": "OBJECTIVE",
    "current_state": "CURRENT_STATE",
    "desired_outcomes": "OUTCOME",
    "success_criteria": "SUCCESS",
    "available_capacity": "CAPACITY",
    "deadline": "DEADLINE",
}

CORE_FIELD_QUESTIONS = {
    "objective": "你最终最想完成的目标是什么？可以先用一句大白话描述。",
    "current_state": "你目前已经具备哪些相关基础，或者已经进行到哪一步？",
    "desired_outcomes": "完成这件事后，你最希望拿到什么具体成果？",
    "success_criteria": "到什么程度时，你会认为这个目标已经完成？",
    "available_capacity": "你每周大概可以投入多少时间？不需要特别精确。",
    "deadline": "有没有硬截止日期或希望完成的大致时间？没有也可以直接说没有。",
}

CORE_FIELD_LABELS = {
    "objective": "目标",
    "current_state": "当前基础",
    "desired_outcomes": "预期成果",
    "success_criteria": "完成标准",
    "available_capacity": "可用时间",
    "deadline": "目标时间",
}

_ADDITIVE_FACT_FIELDS = {
    "available_capacity",
    "constraints",
    "preferences",
    "scope_in",
    "scope_out",
    "existing_resources",
    "risks",
}
_EXPLICIT_REPLACEMENT_PATTERN = re.compile(
    r"整体改为|全部改为|用.{1,30}替换|覆盖之前|之前.{0,20}(?:不用|作废)|"
    r"原(?:规则|要求|安排).{0,20}作废|以.{1,30}为准"
)
_REPLACEMENT_FIELD_HINTS = {
    "available_capacity": r"容量|投入|可用时间|每周|每次|小时|分钟",
    "constraints": r"约束|限制|课程|课表|规则|禁用|不可用",
    "preferences": r"偏好|习惯|方式|节奏",
    "scope_in": r"范围|功能|内容|项目",
    "scope_out": r"范围|功能|内容|不做|排除",
    "existing_resources": r"资源|资料|设备|工具",
    "risks": r"风险|困难|阻碍",
}
_TRANSIENT_PREFERENCE_PATTERN = re.compile(
    r"本轮只|这一轮只|这次只|现在(?:请)?只|不要(?:提出|新增)(?:问题|建议)|"
    r"(?:做|进行)(?:一次)?(?:记忆)?(?:校验|检查)|只(?:复述|输出)"
)


class ClarificationService:
    def __init__(self, session: Session, provider: ClarificationProvider, *, defer_analysis: bool = False) -> None:
        self.session = session
        self.provider = provider
        self.defer_analysis = defer_analysis

    def create_goal(
        self,
        raw_input: str,
        timezone: str = "Asia/Shanghai",
        *,
        defer_initial_response: bool = False,
        web_research_enabled: bool = False,
        commit: bool = True,
    ) -> ClarificationWorkspace:
        title = raw_input.strip().split("。", 1)[0][:60]
        goal = Goal(
            title=title,
            raw_input=raw_input.strip(),
            status="CLARIFYING",
            timezone=timezone,
        )
        self.session.add(goal)
        self.session.flush()
        conversation = Conversation(goal_id=goal.id)
        self.session.add(conversation)
        self.session.flush()

        user_message = Message(
            conversation_id=conversation.id,
            sequence=1,
            role="USER",
            content=raw_input.strip(),
        )
        self.session.add(user_message)
        self.session.flush()
        objective_fact = RequirementFact(
            goal_id=goal.id,
            category="OBJECTIVE",
            field_key="objective",
            value_json=goal.raw_input,
            source="USER",
            status="CONFIRMED",
            confidence=1,
            operation="ADD",
            confirmed_at=utc_now(),
            evidence_message_id=user_message.id,
        )
        self.session.add(objective_fact)
        self.session.flush()
        self.session.add(
            RequirementChange(
                goal_id=goal.id,
                field_key="objective",
                operation="ADD",
                reason="用户创建目标时的原始表达",
                new_fact_id=objective_fact.id,
                evidence_message_id=user_message.id,
            )
        )
        if defer_initial_response:
            pending_assistant = Message(
                conversation_id=conversation.id,
                sequence=2,
                role="ASSISTANT",
                content="Solevia 正在理解你的想法。",
                generation_status="PENDING",
                metadata_json={
                    "initial_response": True,
                    "web_research_enabled": web_research_enabled,
                },
            )
            self.session.add(pending_assistant)
        if commit:
            self.session.commit()
        else:
            self.session.flush()

        if defer_initial_response:
            return self.get_workspace(goal.id)

        context = self._build_context(goal, conversation, latest_user_message=None)
        result, analysis, analysis_error = self._clarify_or_record_failure(conversation, context)
        if result is None:
            return self.get_workspace(goal.id)
        self._apply_result(
            goal,
            conversation,
            result,
            user_message,
            analysis=analysis,
            analysis_error=analysis_error,
        )
        self.session.commit()
        return self.get_workspace(goal.id)

    def complete_initial_response(self, goal_id: str) -> ClarificationWorkspace:
        goal, conversation = self._get_goal_and_conversation(goal_id)
        pending = self.session.scalar(
            select(Message)
            .where(
                Message.conversation_id == conversation.id,
                Message.role == "ASSISTANT",
                Message.generation_status == "PENDING",
            )
            .order_by(Message.sequence)
        )
        if pending is None:
            return self.get_workspace(goal_id)
        user_message = self.session.scalar(
            select(Message)
            .where(
                Message.conversation_id == conversation.id,
                Message.role == "USER",
                Message.sequence < pending.sequence,
            )
            .order_by(Message.sequence.desc())
        )
        if user_message is None:
            raise HTTPException(status_code=409, detail="Initial user message is missing")

        research_context, research_run_id, research_status = ResearchContextService(
            self.session
        ).prepare(
            goal_id,
            user_message.content,
            phase="CLARIFICATION",
            enabled=bool(pending.metadata_json.get("web_research_enabled")),
            run_id=pending.metadata_json.get("research_run_id"),
            conversation_id=conversation.id,
            idempotency_key=f"clarification-initial:{goal_id}:{user_message.id}",
        )
        context = self._build_context(
            goal,
            conversation,
            latest_user_message=None,
            research_context=research_context,
        )
        result, analysis, analysis_error = self._clarify_or_record_failure(
            conversation,
            context,
            existing_message=pending,
        )
        if result is None:
            return self.get_workspace(goal_id)
        self._apply_result(
            goal,
            conversation,
            result,
            user_message,
            existing_assistant_message=pending,
            analysis=analysis,
            analysis_error=analysis_error,
            research_run_id=research_run_id,
            research_status=research_status,
        )
        self.session.commit()
        return self.get_workspace(goal_id)

    def list_goals(self) -> list[GoalListItem]:
        goals = self.session.scalars(select(Goal).order_by(Goal.last_active_at.desc()))
        return [
            GoalListItem(
                id=goal.id,
                title=goal.title,
                status=goal.status,
                last_active_at=goal.last_active_at,
                timezone=goal.timezone,
                pause_until=goal.pause_until,
                completed_at=goal.completed_at,
                archived_at=goal.archived_at,
            )
            for goal in goals
        ]

    def add_user_message(
        self,
        goal_id: str,
        content: str,
        *,
        web_research_enabled: bool = False,
        research_run_id: str | None = None,
        reuse_latest_assistant: bool = False,
        material_source_ids: list[str] | None = None,
        use_saved_research: bool = True,
    ) -> ClarificationWorkspace:
        goal, conversation = self._get_goal_and_conversation(goal_id)
        if goal.status not in {
            "CLARIFYING",
            "REQUIREMENT_REVIEW",
            "ROUTE_DRAFTING",
            "ROUTE_REVIEW",
        }:
            raise HTTPException(status_code=409, detail="This goal is no longer being clarified")

        clean_content = content.strip()
        material_context = None
        if material_source_ids:
            material_context = MaterialAnalysisService(
                self.session, build_material_analysis_provider(get_settings())
            ).prepare_conversation_context(goal_id, clean_content, material_source_ids)

        previous_status = goal.status
        previous_values = self._active_fact_values(goal.id)
        if goal.status in {"REQUIREMENT_REVIEW", "ROUTE_DRAFTING", "ROUTE_REVIEW"}:
            goal.status = "CLARIFYING"

        user_message, retry_message = self._prepare_failed_exchange_retry(
            conversation,
            clean_content,
            allow_existing=reuse_latest_assistant,
        )
        if material_source_ids is not None:
            user_message.metadata_json = {
                **(user_message.metadata_json or {}),
                "material_source_ids": list(dict.fromkeys(material_source_ids)),
                "material_context": material_context,
                "material_titles": list(
                    dict.fromkeys(
                        source["relative_path"] or source["title"]
                        for source in (material_context or {}).get("sources", [])
                    )
                ),
            }
        else:
            material_context = (user_message.metadata_json or {}).get("material_context")
        previous_assistant = self.session.scalar(
            select(Message)
            .where(
                Message.conversation_id == conversation.id,
                Message.role == "ASSISTANT",
                Message.generation_status == "SUCCEEDED",
                Message.sequence < user_message.sequence,
            )
            .order_by(Message.sequence.desc())
        )
        answer_for_keys = (
            list(previous_assistant.metadata_json.get("question_keys", []))
            if previous_assistant
            else []
        )
        goal.last_active_at = utc_now()
        self.session.commit()

        research_context, used_research_run_id, used_research_status = ResearchContextService(self.session).prepare(
            goal_id, clean_content, phase="CLARIFICATION", enabled=web_research_enabled,
            run_id=research_run_id, conversation_id=conversation.id,
            idempotency_key=f"clarification:{goal_id}:{user_message.id}", reuse_latest=use_saved_research,
        )
        context = self._build_context(
            goal, conversation, latest_user_message=clean_content,
            answer_for_keys=answer_for_keys, material_context=material_context,
            research_context=research_context,
        )
        result, analysis, analysis_error = self._clarify_or_record_failure(
            conversation,
            context,
            existing_message=retry_message,
        )
        if result is None:
            return self.get_workspace(goal.id)
        self._apply_result(
            goal,
            conversation,
            result,
            user_message,
            previous_values=previous_values,
            preserve_status_if_unchanged=(
                previous_status if previous_status in {"ROUTE_DRAFTING", "ROUTE_REVIEW"} else None
            ),
            existing_assistant_message=retry_message,
            analysis=analysis,
            analysis_error=analysis_error,
            research_run_id=used_research_run_id,
            research_status=used_research_status,
        )
        self.session.commit()
        return self.get_workspace(goal.id)

    def retry_last_failed_message(self, goal_id: str) -> ClarificationWorkspace:
        _, conversation = self._get_goal_and_conversation(goal_id)
        recent = list(
            self.session.scalars(
                select(Message)
                .where(Message.conversation_id == conversation.id)
                .where(Message.generation_status != "PENDING")
                .order_by(Message.sequence.desc())
                .limit(2)
            )
        )
        if len(recent) != 2:
            raise HTTPException(status_code=409, detail="No failed clarification to retry")
        failed, user_message = recent
        if (
            failed.role != "ASSISTANT"
            or failed.generation_status != "FAILED"
            or user_message.role != "USER"
        ):
            raise HTTPException(status_code=409, detail="No failed clarification to retry")
        return self.add_user_message(goal_id, user_message.content)

    def regenerate_last_message(self, goal_id: str) -> ClarificationWorkspace:
        """Regenerate the latest clarification answer without duplicating the user turn."""
        _, conversation = self._get_goal_and_conversation(goal_id)
        recent = list(
            self.session.scalars(
                select(Message)
                .where(Message.conversation_id == conversation.id)
                .order_by(Message.sequence.desc())
                .limit(2)
            )
        )
        if len(recent) != 2:
            raise HTTPException(status_code=409, detail="No clarification answer to regenerate")
        assistant, user_message = recent
        if assistant.role != "ASSISTANT" or user_message.role != "USER":
            raise HTTPException(status_code=409, detail="No clarification answer to regenerate")
        return self.add_user_message(
            goal_id,
            user_message.content,
            reuse_latest_assistant=True,
        )

    def _prepare_failed_exchange_retry(
        self,
        conversation: Conversation,
        content: str,
        *,
        allow_existing: bool = False,
    ) -> tuple[Message, Message | None]:
        recent = list(
            self.session.scalars(
                select(Message)
                .where(Message.conversation_id == conversation.id)
                .order_by(Message.sequence.desc())
                .limit(2)
            )
        )
        if allow_existing and recent and recent[0].role == "USER" and recent[0].content.strip() == content:
            return recent[0], None
        if len(recent) == 2:
            assistant, user_message = recent
            if (
                assistant.role == "ASSISTANT"
                and assistant.generation_status
                in ({"FAILED", "PENDING", "SUCCEEDED"} if allow_existing else {"FAILED"})
                and user_message.role == "USER"
                and user_message.content.strip() == content
            ):
                assistant.content = "Solevia 正在重新整理这次回复，之前的内容都已保留。"
                assistant.generation_status = "PENDING"
                assistant.metadata_json = {"retrying": True}
                return user_message, assistant

        user_message = Message(
            conversation_id=conversation.id,
            sequence=self._next_sequence(conversation.id),
            role="USER",
            content=content,
        )
        self.session.add(user_message)
        self.session.flush()
        return user_message, None

    def update_fact(
        self, goal_id: str, fact_id: str, action: str, value: Any
    ) -> ClarificationWorkspace:
        goal, _ = self._get_goal_and_conversation(goal_id)
        if goal.status not in {
            "CLARIFYING",
            "REQUIREMENT_REVIEW",
            "ROUTE_DRAFTING",
            "ROUTE_REVIEW",
        }:
            raise HTTPException(status_code=409, detail="Confirmed requirements cannot be edited")
        previous_values = self._active_fact_values(goal_id)
        previous_status = goal.status
        fact = self.session.scalar(
            select(RequirementFact).where(
                RequirementFact.id == fact_id,
                RequirementFact.goal_id == goal_id,
                RequirementFact.status.in_(["PROPOSED", "CONFIRMED"]),
            )
        )
        if fact is None:
            raise HTTPException(status_code=404, detail="Active fact not found")

        if action == "CONFIRM":
            prior_confirmed = self.session.scalar(
                select(RequirementFact).where(
                    RequirementFact.goal_id == goal_id,
                    RequirementFact.field_key == fact.field_key,
                    RequirementFact.status == "CONFIRMED",
                    RequirementFact.id != fact.id,
                )
            )
            if prior_confirmed:
                prior_confirmed.status = "SUPERSEDED"
            fact.status = "CONFIRMED"
            fact.source = "USER"
            fact.confidence = 1
            fact.confirmed_at = utc_now()
            self.session.add(
                RequirementChange(
                    goal_id=goal_id,
                    field_key=fact.field_key,
                    operation="CONFIRM",
                    reason="用户在需求面板中确认",
                    old_fact_id=prior_confirmed.id if prior_confirmed else None,
                    new_fact_id=fact.id,
                )
            )
        elif action == "REJECT":
            fact.status = "REJECTED"
            self.session.add(
                RequirementChange(
                    goal_id=goal_id,
                    field_key=fact.field_key,
                    operation="RETRACT",
                    reason="用户在需求面板中否定",
                    old_fact_id=fact.id,
                )
            )
        else:
            fact.status = "SUPERSEDED"
            replacement = RequirementFact(
                goal_id=goal_id,
                category=fact.category,
                field_key=fact.field_key,
                value_json=value,
                source="USER",
                status="CONFIRMED",
                confidence=1,
                operation="REPLACE",
                change_reason="用户在需求面板中修改",
                confirmed_at=utc_now(),
                supersedes_fact_id=fact.id,
            )
            self.session.add(replacement)
            self.session.flush()
            self.session.add(
                RequirementChange(
                    goal_id=goal_id,
                    field_key=fact.field_key,
                    operation="REPLACE",
                    reason="用户在需求面板中修改",
                    old_fact_id=fact.id,
                    new_fact_id=replacement.id,
                )
            )
        self.session.flush()
        facts_changed = self._active_fact_values(goal_id) != previous_values
        if not facts_changed and previous_status in {"ROUTE_DRAFTING", "ROUTE_REVIEW"}:
            goal.status = previous_status
        elif facts_changed and previous_status in {"ROUTE_DRAFTING", "ROUTE_REVIEW"}:
            self._supersede_drafts(goal_id)
            goal.status = (
                "REQUIREMENT_REVIEW" if self._readiness(goal_id).ready_for_review else "CLARIFYING"
            )
            if goal.status == "REQUIREMENT_REVIEW":
                self._ensure_draft(goal_id)
        elif facts_changed and previous_status == "REQUIREMENT_REVIEW":
            goal.status = (
                "REQUIREMENT_REVIEW" if self._readiness(goal_id).ready_for_review else "CLARIFYING"
            )
            if goal.status == "REQUIREMENT_REVIEW":
                self._ensure_draft(goal_id)
        elif self._readiness(goal_id).ready_for_review:
            goal.status = "REQUIREMENT_REVIEW"
            self._ensure_draft(goal_id)
        else:
            self._supersede_drafts(goal_id)
            goal.status = "CLARIFYING"
        self.session.commit()
        return self.get_workspace(goal_id)

    def _clarify_or_record_failure(
        self,
        conversation: Conversation,
        context: ClarificationContext,
        *,
        existing_message: Message | None = None,
    ) -> tuple[Any | None, RequirementAnalysis | None, Exception | None]:
        try:
            if hasattr(self.provider, "respond") and hasattr(self.provider, "analyze"):
                reply = self.provider.respond(context)
                if self.defer_analysis:
                    return reply, None, None
                try:
                    analysis = self.provider.analyze(context, reply.assistant_message)
                except ClarificationProviderError as error:
                    return reply, None, error
                return reply, analysis, None
            result = self.provider.clarify(context)
            analysis = RequirementAnalysis(
                proposed_facts=result.proposed_facts,
                conflicts=result.conflicts,
                open_questions=(
                    result.open_questions
                    or ([result.readiness.reason] if not result.readiness.ready_for_review else [])
                ),
                current_topic=None,
                summary="；".join(
                    f"{key}: {value}"
                    for key, value in self._active_fact_values_from_result(context, result).items()
                )
                or "正在理解用户的初步想法。",
                readiness=result.readiness,
            )
            return result, analysis, None
        except ClarificationProviderError as error:
            message = existing_message or Message(
                conversation_id=conversation.id,
                sequence=self._next_sequence(conversation.id),
                role="ASSISTANT",
                content=str(error),
            )
            message.content = str(error)
            message.generation_status = "FAILED"
            message.metadata_json = {
                "error_type": type(error).__name__,
                "error_code": error.code,
                "error_diagnostic": error.diagnostic,
                "prompt_version": CLARIFICATION_PROMPT_VERSION,
            }
            if existing_message is None:
                self.session.add(message)
            self.session.commit()
            return None, None, error

    def _build_context(
        self,
        goal: Goal,
        conversation: Conversation,
        *,
        latest_user_message: str | None,
        answer_for_keys: list[str] | None = None,
        research_context: dict[str, object] | None = None,
        material_context: dict[str, object] | None = None,
        through_sequence: int | None = None,
    ) -> ClarificationContext:
        summary = self.session.scalar(
            select(ConversationSummary)
            .where(ConversationSummary.conversation_id == conversation.id,
                   ConversationSummary.through_message_sequence <= (through_sequence or 2**31))
            .order_by(ConversationSummary.created_at.desc())
        )
        recent = list(
            self.session.scalars(
                select(Message)
                .where(Message.conversation_id == conversation.id,
                       Message.sequence <= (through_sequence or 2**31))
                .order_by(Message.sequence.desc())
                .limit(12)
            )
        )
        recent.reverse()
        if through_sequence is not None:
            # A replacement extraction must actually see every failed turn it
            # recovers, even when that turn has fallen out of the chat window.
            failed_messages = list(self.session.scalars(select(Message).join(
                ClarificationAnalysisJob, ClarificationAnalysisJob.user_message_id == Message.id
            ).where(
                ClarificationAnalysisJob.conversation_id == conversation.id,
                ClarificationAnalysisJob.status == "FAILED",
                Message.sequence <= through_sequence,
            )))
            recent = sorted({item.id: item for item in [*failed_messages, *recent]}.values(),
                            key=lambda item: item.sequence)
        if material_context is None and latest_user_message is not None:
            material_context = next(
                (
                    (item.metadata_json or {}).get("material_context")
                    for item in reversed(recent)
                    if item.role == "USER" and item.content.strip() == latest_user_message.strip()
                ),
                None,
            )
        if material_context is not None and research_context and research_context.get("knowledge_cache"):
            # An explicit material selection remains the entire reading scope.
            research_context = None
        user_memory = list(
            self.session.scalars(
                select(UserMemoryItem)
                .where(
                    UserMemoryItem.profile_id == "default",
                    UserMemoryItem.status == "CONFIRMED",
                )
                .order_by(UserMemoryItem.updated_at.desc())
                .limit(50)
            )
        )
        error_memory = list(
            self.session.scalars(
                select(GoalErrorMemory)
                .where(
                    GoalErrorMemory.goal_id == goal.id,
                    GoalErrorMemory.status == "CONFIRMED",
                )
                .order_by(GoalErrorMemory.updated_at.desc())
                .limit(40)
            )
        )
        from planner_api.goal_memory import recall_memory
        return ClarificationContext(
            raw_input=goal.raw_input,
            long_term_memory=recall_memory(self.session, goal.id, latest_user_message or goal.raw_input),
            active_facts=self._confirmed_fact_values(goal.id),
            latest_user_message=latest_user_message,
            answer_for_keys=answer_for_keys or [],
            recent_messages=[
                {"sequence": item.sequence, "role": item.role, "content": item.content,
                 "message_id": item.id, "created_at": item.created_at.isoformat(),
                 "reference_date": local_date(item.created_at, goal.timezone).isoformat()}
                for item in recent
            ],
            summary=summary.summary if summary else None,
            current_topic=summary.current_topic if summary else None,
            proposed_facts=self._proposed_fact_values(goal.id),
            recent_changes=(summary.recent_changes_json if summary else []),
            user_profile_memory=[
                {
                    "memory_key": item.memory_key,
                    "content": item.content,
                    "source_type": item.source_type,
                }
                for item in user_memory
                if item.valid_until is None or item.valid_until >= date.today()
            ],
            error_avoidance_memory=[
                {
                    "id": item.id,
                    "rule": item.rule,
                    "evidence": item.evidence,
                    "scope": item.scope,
                    "target_refs": item.target_refs_json,
                }
                for item in error_memory
                if item.valid_until is None or item.valid_until >= date.today()
            ],
            research_context=research_context,
            material_context=material_context,
            temporal_context={"timezone": goal.timezone,
                "current_date": local_date(utc_now(), goal.timezone).isoformat(),
                "original_reference_date": local_date(goal.created_at, goal.timezone).isoformat(),
                "policy": "今天/本周等按原用户消息reference_date解释；启动后第N周才随启动平移。"},
        )

    @staticmethod
    def _active_fact_values_from_result(
        context: ClarificationContext, result: ClarificationResult
    ) -> dict[str, object]:
        values = dict(context.active_facts)
        for fact in result.proposed_facts:
            if fact.status == "CONFIRMED":
                values[fact.field_key] = fact.value
        return values

    def create_requirement_draft(self, goal_id: str) -> ClarificationWorkspace:
        self._require_analysis_complete(goal_id)
        goal, _ = self._get_goal_and_conversation(goal_id)
        readiness = self._readiness(goal_id)
        if not readiness.ready_for_review:
            raise HTTPException(
                status_code=409,
                detail={
                    "message": "Requirements are not ready",
                    "fields": readiness.blocking_fields,
                },
            )
        draft = self._ensure_draft(goal_id)
        goal.status = "REQUIREMENT_REVIEW"
        self.session.commit()
        return self.get_workspace(goal_id, draft=draft)

    def confirm_requirement(self, goal_id: str) -> ClarificationWorkspace:
        self._require_analysis_complete(goal_id)
        goal, _ = self._get_goal_and_conversation(goal_id)
        if goal.status != "REQUIREMENT_REVIEW":
            raise HTTPException(status_code=409, detail="Requirement is not awaiting review")
        readiness = self._readiness(goal_id)
        if not readiness.ready_for_review:
            raise HTTPException(status_code=409, detail="Core requirements are no longer complete")
        if goal.planning_mode_confirmed_at is None:
            raise HTTPException(
                status_code=409,
                detail="请先确认是‘先试试看’还是‘直接固定规划’，路线会按这个选择生成",
            )
        if goal.schedule_type_confirmed_at is None:
            raise HTTPException(
                status_code=409,
                detail="请先确认按成果、按节奏还是按日期推进，路线和计划会按这个选择生成",
            )
        draft = self.session.scalar(
            select(RequirementVersion)
            .where(
                RequirementVersion.goal_id == goal_id,
                RequirementVersion.status == "DRAFT",
            )
            .order_by(RequirementVersion.version.desc())
        )
        if draft is None:
            draft = self._ensure_draft(goal_id)

        try:
            deadline = snapshot_deadline(draft.snapshot_json, timezone=goal.timezone,
                                         fallback_time=draft.created_at, require_resolved=True)
        except ValueError as error:
            raise HTTPException(409, {"code": "REQUIREMENT_DEADLINE_UNRESOLVED", "message": str(error)}) from error
        if deadline and deadline < local_date(utc_now(), goal.timezone):
            raise HTTPException(409, {"code": "REQUIREMENT_DEADLINE_EXPIRED",
                "message": f"原先确认的截止日期{deadline}已经过去。请先讨论保留成果并调整期限或可用时间，再确认需求。"})
        draft.status = "CONFIRMED"
        draft.confirmed_at = utc_now()
        downstream_routes = self.session.scalars(
            select(RouteVersion).where(
                RouteVersion.goal_id == goal_id,
                RouteVersion.status == "DRAFT",
                RouteVersion.requirement_version_id != draft.id,
            )
        )
        for route in downstream_routes:
            route.status = "SUPERSEDED"
        goal.active_route_version_id = None
        goal.current_requirement_version_id = draft.id
        goal.status = "ROUTE_DRAFTING"
        self.session.commit()
        return self.get_workspace(goal_id, draft=draft)

    def select_planning_approach(self, goal_id: str, planning_mode: str) -> ClarificationWorkspace:
        goal, _ = self._get_goal_and_conversation(goal_id)
        if goal.status not in {"CLARIFYING", "REQUIREMENT_REVIEW"}:
            raise HTTPException(status_code=409, detail="规划方式只能在生成路线前确认")
        if planning_mode not in {"EXPLORATORY", "COMMITTED"}:
            raise HTTPException(status_code=422, detail="未知的规划方式")
        goal.planning_mode = planning_mode
        goal.planning_mode_confirmed_at = utc_now()
        goal.last_active_at = utc_now()
        self.session.commit()
        return self.get_workspace(goal_id)

    def select_schedule_type(self, goal_id: str, schedule_type: str) -> ClarificationWorkspace:
        goal, _ = self._get_goal_and_conversation(goal_id)
        if goal.status not in {"CLARIFYING", "REQUIREMENT_REVIEW"}:
            raise HTTPException(status_code=409, detail="时间组织方式只能在生成路线前确认")
        if schedule_type not in {"OUTCOME", "RHYTHM", "CALENDAR"}:
            raise HTTPException(status_code=422, detail="未知的时间组织方式")
        goal.schedule_type = schedule_type
        goal.schedule_type_confirmed_at = utc_now()
        goal.last_active_at = utc_now()
        self.session.commit()
        return self.get_workspace(goal_id)

    def reopen_requirement(self, goal_id: str) -> ClarificationWorkspace:
        goal, _ = self._get_goal_and_conversation(goal_id)
        if goal.status not in {"ROUTE_DRAFTING", "ROUTE_REVIEW", "REQUIREMENT_REVIEW"}:
            raise HTTPException(status_code=409, detail="Requirements cannot be reopened here")
        if not goal.current_requirement_version_id:
            raise HTTPException(status_code=409, detail="No confirmed requirement exists")
        confirmed = self.session.get(RequirementVersion, goal.current_requirement_version_id)
        if confirmed is None or confirmed.status != "CONFIRMED":
            raise HTTPException(status_code=409, detail="Confirmed requirement version not found")

        return self.get_workspace(goal_id)

    def discard_requirement_changes(self, goal_id: str) -> ClarificationWorkspace:
        goal, _ = self._get_goal_and_conversation(goal_id)
        if goal.status != "REQUIREMENT_REVIEW" or not goal.current_requirement_version_id:
            raise HTTPException(status_code=409, detail="No downstream requirement changes exist")
        confirmed = self.session.get(RequirementVersion, goal.current_requirement_version_id)
        if confirmed is None or confirmed.status != "CONFIRMED":
            raise HTTPException(status_code=409, detail="Confirmed requirement version not found")
        route = self.session.scalar(
            select(RouteVersion)
            .where(
                RouteVersion.goal_id == goal_id,
                RouteVersion.status.in_(["DRAFT", "CONFIRMED"]),
            )
            .order_by(RouteVersion.created_at.desc())
        )
        if route is None:
            raise HTTPException(status_code=409, detail="No preserved route exists")

        active_by_key = {fact.field_key: fact for fact in self._active_facts(goal_id)}
        snapshot = confirmed.snapshot_json
        for field_key in set(active_by_key) | set(snapshot):
            existing = active_by_key.get(field_key)
            target = snapshot.get(field_key)
            has_target = target is not None and target != "" and target != []
            if existing and existing.value_json == target:
                continue
            if existing:
                existing.status = "SUPERSEDED"
            if has_target:
                self.session.add(
                    RequirementFact(
                        goal_id=goal_id,
                        category=(
                            existing.category
                            if existing
                            else FIELD_CATEGORY.get(field_key, "CONTEXT")
                        ),
                        field_key=field_key,
                        value_json=target,
                        source="SYSTEM_RESTORE",
                        status="CONFIRMED",
                        confidence=1,
                        supersedes_fact_id=existing.id if existing else None,
                    )
                )
        self._supersede_drafts(goal_id)
        goal.status = "ROUTE_REVIEW" if route.status == "DRAFT" else "PLAN_DRAFTING"
        goal.last_active_at = utc_now()
        self.session.commit()
        return self.get_workspace(goal_id, draft=confirmed)

    def get_workspace(
        self, goal_id: str, draft: RequirementVersion | None = None
    ) -> ClarificationWorkspace:
        goal, conversation = self._get_goal_and_conversation(goal_id)
        messages = list(
            self.session.scalars(
                select(Message)
                .where(Message.conversation_id == conversation.id)
                .order_by(Message.sequence.desc())
                .limit(get_settings().conversation_workspace_message_limit)
            )
        )
        messages.reverse()
        facts = self._active_facts(goal_id)
        changes = list(
            self.session.scalars(
                select(RequirementChange)
                .where(RequirementChange.goal_id == goal_id)
                .order_by(RequirementChange.created_at.desc())
                .limit(50)
            )
        )
        analysis_jobs = list(
            self.session.scalars(
                select(ClarificationAnalysisJob)
                .where(ClarificationAnalysisJob.goal_id == goal_id)
                .order_by((ClarificationAnalysisJob.status != "SUCCEEDED").desc(),
                          ClarificationAnalysisJob.created_at.desc())
                .limit(20)
            )
        )
        summary = self.session.scalar(
            select(ConversationSummary)
            .where(ConversationSummary.conversation_id == conversation.id)
            .order_by(ConversationSummary.created_at.desc())
        )
        latest_research = self.session.scalar(
            select(ResearchRun)
            .where(
                ResearchRun.goal_id == goal_id,
                ResearchRun.conversation_id == conversation.id,
                ResearchRun.status.in_({"READY", "PARTIAL", "COMPLETED"}),
            )
            .order_by(ResearchRun.updated_at.desc())
            .limit(1)
        )
        if draft is None:
            draft = self.session.scalar(
                select(RequirementVersion)
                .where(
                    RequirementVersion.goal_id == goal_id,
                    RequirementVersion.status.in_(["DRAFT", "CONFIRMED"]),
                )
                .order_by(RequirementVersion.version.desc())
            )
        readiness = self._readiness(goal_id)
        downstream_route = self.session.scalar(
            select(RouteVersion)
            .where(
                RouteVersion.goal_id == goal_id,
                RouteVersion.status.in_(["DRAFT", "CONFIRMED"]),
            )
            .order_by(RouteVersion.created_at.desc())
        )
        route_requirement = (
            self.session.get(RequirementVersion, downstream_route.requirement_version_id)
            if downstream_route
            else None
        )
        requirements_changed_since_route = bool(
            downstream_route
            and draft
            and draft.status == "DRAFT"
            and route_requirement
            and draft.snapshot_json != route_requirement.snapshot_json
        )
        recommendation_source = draft.snapshot_json if draft else self._active_fact_values(goal_id)
        approach = recommend_planning_approach(
            goal.raw_input,
            recommendation_source,
            summary.summary if summary else "",
        )
        model_approach = next(
            (
                item.metadata_json.get("planning_approach")
                for item in reversed(messages)
                if item.role == "ASSISTANT" and item.metadata_json.get("planning_approach")
            ),
            None,
        )
        approach_read = (
            PlanningApproachRecommendation.model_validate(model_approach)
            if model_approach
            else PlanningApproachRecommendation(
                recommended_mode=approach.recommended_mode,
                reasons=approach.reasons,
                confidence=approach.confidence,
                explicit_trial_weeks=approach.explicit_trial_weeks,
            )
        )
        schedule_advice = recommend_schedule_type(
            goal.raw_input,
            recommendation_source,
            summary.summary if summary else "",
        )
        model_schedule_advice = next(
            (
                item.metadata_json.get("schedule_type_recommendation")
                for item in reversed(messages)
                if item.role == "ASSISTANT"
                and isinstance(item.metadata_json.get("schedule_type_recommendation"), dict)
            ),
            None,
        )
        # Calendar is governed by a real deadline, not by the mere presence of
        # a date-like phrase.  The model classifies date semantics; the fallback
        # only protects the boundary when structured analysis is unavailable.
        hard_deadline_requires_calendar = (
            schedule_advice.recommended_type == "CALENDAR" and schedule_advice.confidence == "HIGH"
        )
        parsed_model_schedule = (
            ScheduleTypeRecommendation.model_validate(model_schedule_advice)
            if model_schedule_advice
            else None
        )
        model_has_hard_deadline = bool(
            parsed_model_schedule
            and any(
                anchor.kind == "HARD_DEADLINE" and anchor.binding
                for anchor in parsed_model_schedule.temporal_anchors
            )
        )
        fallback_schedule = ScheduleTypeRecommendation(
            recommended_type=schedule_advice.recommended_type,
            reasons=schedule_advice.reasons,
            confidence=schedule_advice.confidence,
        )
        if hard_deadline_requires_calendar or parsed_model_schedule is None:
            schedule_advice_read = fallback_schedule
        elif model_has_hard_deadline and parsed_model_schedule.recommended_type != "CALENDAR":
            schedule_advice_read = parsed_model_schedule.model_copy(
                update={
                    "recommended_type": "CALENDAR",
                    "reasons": ["讨论中识别到不可错过的硬截止日期，应优先按日期倒排"],
                    "confidence": "HIGH",
                }
            )
        elif parsed_model_schedule.recommended_type == "CALENDAR" and not model_has_hard_deadline:
            # A review date or a holiday opportunity can coexist with an
            # outcome plan.  Do not let an ungrounded CALENDAR label override it.
            schedule_advice_read = fallback_schedule
        else:
            schedule_advice_read = parsed_model_schedule
        return ClarificationWorkspace(
            goal_id=goal.id,
            goal_title=goal.title,
            raw_input=goal.raw_input,
            status=goal.status,
            messages=[
                MessageRead(
                    id=item.id,
                    sequence=item.sequence,
                    role=item.role,
                    content=item.content,
                    generation_status=item.generation_status,
                    metadata=item.metadata_json,
                    created_at=item.created_at,
                )
                for item in messages
            ],
            facts=[
                FactRead(
                    id=item.id,
                    category=item.category,
                    field_key=item.field_key,
                    value=item.value_json,
                    source=item.source,
                    status=item.status,
                    confidence=item.confidence,
                    operation=item.operation,
                    change_reason=item.change_reason,
                    evidence_message_id=item.evidence_message_id,
                    supersedes_fact_id=item.supersedes_fact_id,
                    created_at=item.created_at,
                )
                for item in facts
            ],
            fact_changes=[
                RequirementChangeRead(
                    id=item.id,
                    field_key=item.field_key,
                    operation=item.operation,
                    reason=item.reason,
                    old_fact_id=item.old_fact_id,
                    new_fact_id=item.new_fact_id,
                    old_value=(
                        self.session.get(RequirementFact, item.old_fact_id).value_json
                        if item.old_fact_id and self.session.get(RequirementFact, item.old_fact_id)
                        else None
                    ),
                    new_value=(
                        self.session.get(RequirementFact, item.new_fact_id).value_json
                        if item.new_fact_id and self.session.get(RequirementFact, item.new_fact_id)
                        else None
                    ),
                    evidence_message_id=item.evidence_message_id,
                    created_at=item.created_at,
                )
                for item in changes
            ],
            memory=ConversationMemoryRead(
                summary=summary.summary if summary else "",
                current_topic=summary.current_topic if summary else None,
                recent_changes=list(summary.recent_changes_json) if summary else [],
                entity_refs=list(summary.entity_refs_json) if summary else [],
                through_message_sequence=summary.through_message_sequence if summary else 0,
            ),
            analysis_jobs=[
                ClarificationAnalysisJobRead(
                    id=item.id,
                    status=item.status,
                    attempts=item.attempts,
                    max_attempts=item.max_attempts,
                    next_retry_at=item.next_retry_at,
                    last_error=item.last_error,
                    last_error_code=item.last_error_code,
                    created_at=item.created_at,
                    updated_at=item.updated_at,
                    completed_at=item.completed_at,
                )
                for item in analysis_jobs
            ],
            open_questions=list(summary.open_questions_json) if summary else [],
            conflicts=list(summary.conflicts_json) if summary else [],
            readiness=readiness,
            requirement_draft=(
                RequirementDraftRead(
                    id=draft.id,
                    version=draft.version,
                    status=draft.status,
                    snapshot=draft.snapshot_json,
                )
                if draft
                else None
            ),
            downstream_route_preserved=downstream_route is not None,
            requirements_changed_since_route=requirements_changed_since_route,
            planning_approach_recommendation=approach_read,
            selected_planning_mode=(
                goal.planning_mode if goal.planning_mode_confirmed_at is not None else None
            ),
            schedule_type_recommendation=schedule_advice_read,
            selected_schedule_type=(
                goal.schedule_type if goal.schedule_type_confirmed_at is not None else None
            ),
            latest_research=research_summary_from_run(latest_research),
        )

    def _apply_result(  # type: ignore[no-untyped-def]
        self,
        goal,
        conversation,
        result,
        evidence_message,
        *,
        previous_values: dict[str, object] | None = None,
        preserve_status_if_unchanged: str | None = None,
        existing_assistant_message: Message | None = None,
        analysis: RequirementAnalysis | None = None,
        analysis_error: Exception | None = None,
        research_run_id: str | None = None,
        research_status: str | None = None,
    ) -> None:
        recent_changes: list[dict[str, object]] = []
        for candidate in analysis.proposed_facts if analysis else []:
            change = self._apply_fact_candidate(goal.id, candidate, evidence_message.id)
            if change:
                recent_changes.append(change)
        persist_explicit_error_memories(
            self.session,
            goal_id=goal.id,
            source_message_id=evidence_message.id,
            user_content=evidence_message.content,
            candidates=explicit_error_memory_candidates(evidence_message.content),
        )
        self.session.flush()

        sequence = (
            existing_assistant_message.sequence
            if existing_assistant_message
            else self._next_sequence(conversation.id)
        )
        assistant_message = existing_assistant_message or Message(
            conversation_id=conversation.id,
            sequence=sequence,
            role="ASSISTANT",
            content=result.assistant_message,
        )
        assistant_message.content = result.assistant_message
        assistant_message.generation_status = "SUCCEEDED"
        deferred = self.defer_analysis and analysis is None and analysis_error is None
        assistant_message.metadata_json = {
            **(assistant_message.metadata_json or {}),
            "question_keys": (
                getattr(result, "question_keys", [])
                or ([analysis.current_topic] if analysis and analysis.current_topic else [])
            ),
            "prompt_version": CLARIFICATION_PROMPT_VERSION,
            "analysis_status": "PENDING" if deferred else ("FAILED" if analysis_error else "SUCCEEDED"),
            "analysis_revision": str(uuid4()) if deferred else (assistant_message.metadata_json or {}).get("analysis_revision"),
            "analysis_error_code": getattr(analysis_error, "code", None),
            "analysis_error_diagnostic": getattr(analysis_error, "diagnostic", None),
            "server_ready_for_review": analysis.readiness.ready_for_review if analysis else False,
            "planning_approach": (
                analysis.planning_approach.model_dump(mode="json")
                if analysis and analysis.planning_approach
                else None
            ),
            "schedule_type_recommendation": (
                analysis.schedule_type_recommendation.model_dump(mode="json")
                if analysis and analysis.schedule_type_recommendation
                else None
            ),
            "research_run_id": research_run_id or (assistant_message.metadata_json or {}).get("research_run_id"),
            "research_status": research_status or (assistant_message.metadata_json or {}).get("research_status"),
        }
        if existing_assistant_message is None:
            self.session.add(assistant_message)
        self.session.flush()

        if analysis_error is not None or deferred:
            self._ensure_analysis_job(
                goal.id,
                conversation.id,
                evidence_message.id,
                assistant_message.id,
                analysis_error,
            )

        if deferred:
            # The reply is durable now. Leave facts and summaries to the ordered
            # analysis job instead of publishing an unanalysed placeholder summary.
            goal.last_active_at = utc_now()
            return

        if analysis is not None:
            from planner_api.goal_memory import save_memories
            save_memories(self.session, conversation, analysis.long_term_memories, source_message=evidence_message)
        active_values = self._confirmed_fact_values(goal.id)
        conclusions = [f"{key}: {value}" for key, value in active_values.items()]
        prior_summary = self.session.scalar(
            select(ConversationSummary)
            .where(ConversationSummary.conversation_id == conversation.id)
            .order_by(ConversationSummary.created_at.desc())
        )
        readiness = self._validated_readiness(goal.id, analysis.readiness if analysis else None)
        summary_source = "当前有效事实：\n" + "\n".join(conclusions)
        if not conclusions:
            summary_source = (
                analysis.summary
                if analysis
                else (prior_summary.summary if prior_summary else "正在理解用户的初步想法。")
            )
        durable_summary, _summary_manifest = compile_user_text(
            summary_source,
            6000,
            include_header=False,
        )
        self.session.add(
            ConversationSummary(
                conversation_id=conversation.id,
                through_message_sequence=sequence,
                summary=durable_summary,
                confirmed_conclusions_json=conclusions,
                open_questions_json=(
                    (
                        analysis.open_questions
                        or readiness.blocking_topics
                        or ([readiness.reason] if not readiness.ready_for_review else [])
                    )
                    if analysis
                    else (prior_summary.open_questions_json if prior_summary else [])
                ),
                conflicts_json=(
                    analysis.conflicts
                    if analysis
                    else (prior_summary.conflicts_json if prior_summary else [])
                ),
                current_topic=(
                    analysis.current_topic
                    if analysis
                    else (prior_summary.current_topic if prior_summary else None)
                ),
                recent_changes_json=recent_changes,
                entity_refs_json=(
                    analysis.entity_refs
                    if analysis
                    else (prior_summary.entity_refs_json if prior_summary else [])
                ),
                readiness_json=readiness.model_dump(),
            )
        )
        goal.last_active_at = utc_now()
        facts_changed = previous_values is None or active_values != previous_values
        if preserve_status_if_unchanged and not facts_changed:
            goal.status = preserve_status_if_unchanged
            return
        if facts_changed and goal.status == "REQUIREMENT_REVIEW":
            self._supersede_drafts(goal.id)
            goal.status = "CLARIFYING"

    def _apply_fact_candidate(  # type: ignore[no-untyped-def]
        self, goal_id: str, candidate, evidence_message_id: str
    ) -> dict[str, object] | None:
        evidence_message = self.session.get(Message, evidence_message_id)
        evidence_content = evidence_message.content if evidence_message else ""
        if (candidate.field_key == "available_capacity" and candidate.status == "CONFIRMED"
                and candidate.operation != "RETRACT"
                and re.search(r"可以讨论|尚未授权|还没同意|未同意|先不调整|如果.{0,60}(?:挪|移|腾)", evidence_content)):
            # Conditional activity changes are proposed supply, never confirmed
            # time. Keep the previous confirmed capacity until explicit consent.
            candidate = candidate.model_copy(update={"status": "PROPOSED"})
        if candidate.field_key == "preferences" and self._is_transient_preference(
            candidate.value, evidence_content
        ):
            return None
        current = list(
            self.session.scalars(
                select(RequirementFact)
                .where(
                    RequirementFact.goal_id == goal_id,
                    RequirementFact.field_key == candidate.field_key,
                    RequirementFact.status.in_(["PROPOSED", "CONFIRMED"]),
                )
                .order_by(RequirementFact.created_at.desc())
            )
        )
        confirmed = next((item for item in current if item.status == "CONFIRMED"), None)
        proposed = next((item for item in current if item.status == "PROPOSED"), None)

        if candidate.operation == "RETRACT":
            old = confirmed or proposed
            if old is None:
                return None
            old.status = "REJECTED"
            change = RequirementChange(
                goal_id=goal_id,
                field_key=candidate.field_key,
                operation="RETRACT",
                reason=candidate.change_reason,
                old_fact_id=old.id,
                evidence_message_id=evidence_message_id,
            )
            self.session.add(change)
            self.session.flush()
            return {
                "id": change.id,
                "field_key": candidate.field_key,
                "operation": "RETRACT",
                "reason": candidate.change_reason,
            }

        candidate_value = candidate.value
        merge_with_confirmed = (
            confirmed is not None
            and candidate.status == "CONFIRMED"
            and candidate.field_key in _ADDITIVE_FACT_FIELDS
            and not self._explicit_replacement_for_field(candidate.field_key, evidence_content)
        )
        if merge_with_confirmed:
            candidate_value = self._merge_fact_values(
                confirmed.value_json,
                candidate.value,
                field_key=candidate.field_key,
                evidence_content=evidence_content,
            )

        if confirmed and confirmed.value_json == candidate_value:
            return None
        if proposed and proposed.value_json == candidate_value:
            if candidate.status == "CONFIRMED" and candidate.source == "USER":
                if confirmed:
                    confirmed.status = "SUPERSEDED"
                proposed.status = "CONFIRMED"
                proposed.source = "USER"
                proposed.confidence = 1
                proposed.operation = "REPLACE" if confirmed else "ADD"
                proposed.change_reason = candidate.change_reason
                proposed.confirmed_at = utc_now()
                change = RequirementChange(
                    goal_id=goal_id,
                    field_key=candidate.field_key,
                    operation="CONFIRM",
                    reason=candidate.change_reason,
                    old_fact_id=confirmed.id if confirmed else None,
                    new_fact_id=proposed.id,
                    evidence_message_id=evidence_message_id,
                )
                self.session.add(change)
                self.session.flush()
                return {
                    "id": change.id,
                    "field_key": candidate.field_key,
                    "operation": "CONFIRM",
                    "reason": candidate.change_reason,
                }
            return None

        old = confirmed if candidate.status == "CONFIRMED" else proposed
        if candidate.status == "CONFIRMED":
            for item in current:
                item.status = "SUPERSEDED"
            operation = (
                "MERGE"
                if merge_with_confirmed
                else ("REPLACE" if confirmed else candidate.operation)
            )
        else:
            if proposed:
                proposed.status = "SUPERSEDED"
            operation = (
                "REQUEST_CONFIRMATION"
                if confirmed or candidate.operation == "REQUEST_CONFIRMATION"
                else candidate.operation
            )
        new_fact = RequirementFact(
            goal_id=goal_id,
            category=candidate.category,
            field_key=candidate.field_key,
            value_json=candidate_value,
            source=candidate.source,
            status=candidate.status,
            confidence=candidate.confidence,
            operation=operation,
            change_reason=candidate.change_reason,
            confirmed_at=utc_now() if candidate.status == "CONFIRMED" else None,
            evidence_message_id=evidence_message_id,
            supersedes_fact_id=old.id if old else None,
        )
        self.session.add(new_fact)
        self.session.flush()
        change = RequirementChange(
            goal_id=goal_id,
            field_key=candidate.field_key,
            operation=operation,
            reason=candidate.change_reason,
            old_fact_id=old.id if old else None,
            new_fact_id=new_fact.id,
            evidence_message_id=evidence_message_id,
        )
        self.session.add(change)
        self.session.flush()
        return {
            "id": change.id,
            "field_key": candidate.field_key,
            "operation": operation,
            "reason": candidate.change_reason,
        }

    @staticmethod
    def _is_transient_preference(value: object, evidence_content: str) -> bool:
        candidate_text = value if isinstance(value, str) else ""
        return bool(
            _TRANSIENT_PREFERENCE_PATTERN.search(evidence_content)
            or _TRANSIENT_PREFERENCE_PATTERN.search(candidate_text)
        )

    @staticmethod
    def _explicit_replacement_for_field(field_key: str, evidence: str) -> bool:
        # A scope correction must not clear independent constraints in the same turn.
        hint = _REPLACEMENT_FIELD_HINTS.get(field_key)
        return any(
            _EXPLICIT_REPLACEMENT_PATTERN.search(part)
            and (not hint or re.search(hint, part) or re.search(r"全部需求|所有事实|全部信息", part))
            for part in re.split(r"[。；;\n]+", evidence)
        )

    @staticmethod
    def _merge_fact_values(
        old: object, new: object, *, field_key: str | None = None, evidence_content: str = ""
    ) -> object:
        if old == new:
            return old
        if isinstance(old, str) and isinstance(new, str):

            def normalize(value: str) -> str:
                return re.sub(r"[\s，。；,;]", "", value).casefold()

            if field_key == "available_capacity":
                return ClarificationService._merge_capacity_values(old, new, evidence_content)
            old_key = normalize(old)
            new_key = normalize(new)
            if old_key and old_key in new_key:
                return new
            if new_key and new_key in old_key:
                return old
            return f"{old.rstrip('；;。 ')}；{new.strip()}"
        if isinstance(old, list) and isinstance(new, list):
            merged = list(old)
            for item in new:
                if item not in merged:
                    merged.append(item)
            return merged
        if isinstance(old, dict) and isinstance(new, dict):
            return {**old, **new}
        return new

    @staticmethod
    def _merge_capacity_values(old: str, new: str, evidence_content: str = "") -> str:
        """Keep one current value per month while retaining different periods."""
        splitter = re.compile(r"[；;。\n]+")
        old_parts = [part.strip() for part in splitter.split(old) if part.strip()]
        new_parts = [part.strip() for part in splitter.split(new) if part.strip()]

        def period_keys(value: str) -> set[tuple[str, str]]:
            keys: set[tuple[str, str]] = set()
            for year, month in re.findall(r"(?:(\d{4})年)?(\d{1,2})月", value):
                keys.add((year, month.zfill(2)))
            return keys

        new_keys = set().union(*(period_keys(part) for part in new_parts))
        if not new_keys:
            corrected = re.search(r"更正|纠正|修正|改为|调整为|不是.{1,20}(?:小时|分钟)", evidence_content)
            quota = re.compile(r"每(?:周|天|日|次|晚)[^；;。\n]{0,80}(?:小时|分钟)")
            if corrected and quota.search(new):
                kinds = {match.group(0)[:2] for match in quota.finditer(new)}
                old_parts = [part for part in old_parts if period_keys(part) or not any(
                    match.group(0)[:2] in kinds for match in quota.finditer(part))]
                return "；".join(dict.fromkeys([*old_parts, *new_parts]))
            return ClarificationService._merge_fact_values(old, new, field_key=None)  # type: ignore[return-value]
        retained = [part for part in old_parts if not period_keys(part) & new_keys]
        merged = [*retained, *new_parts]
        result: list[str] = []
        for part in merged:
            if part not in result:
                result.append(part)
        return "；".join(result)

    def _ensure_analysis_job(
        self,
        goal_id: str,
        conversation_id: str,
        user_message_id: str,
        assistant_message_id: str,
        error: Exception | None,
    ) -> ClarificationAnalysisJob:
        existing = self.session.scalar(
            select(ClarificationAnalysisJob).where(
                ClarificationAnalysisJob.assistant_message_id == assistant_message_id,
                ClarificationAnalysisJob.status.in_(["PENDING", "PROCESSING", "FAILED"]),
            )
        )
        if existing:
            if existing.status != "PROCESSING":
                existing.status = "PENDING"
                if error is None:
                    existing.attempts = 0
                    existing.completed_at = None
            existing.next_retry_at = utc_now()
            existing.last_error = str(error) if error else None
            existing.last_error_code = getattr(error, "code", None)
            return existing
        job = ClarificationAnalysisJob(
            goal_id=goal_id,
            conversation_id=conversation_id,
            user_message_id=user_message_id,
            assistant_message_id=assistant_message_id,
            status="PENDING",
            attempts=1 if error else 0,
            next_retry_at=utc_now(),
            last_error=str(error) if error else None,
            last_error_code=getattr(error, "code", None),
        )
        self.session.add(job)
        self.session.flush()
        return job

    def process_pending_analysis(self, job_id: str | None = None, *, goal_id: str | None = None) -> tuple[bool, float | None]:
        """Run one durable analysis attempt and return (worked, retry_delay)."""
        now = utc_now()
        from planner_api.analysis_claim import claim_owner, owner_is_gone

        for abandoned in self.session.scalars(select(ClarificationAnalysisJob).where(
            ClarificationAnalysisJob.status == "PROCESSING",
        )):
            reply = self.session.get(Message, abandoned.assistant_message_id)
            if reply and owner_is_gone((reply.metadata_json or {}).get("analysis_claim_owner")):
                abandoned.status = "PENDING"
                abandoned.next_retry_at = now
                abandoned.attempts = max(0, abandoned.attempts - 1)
        # A process restart must not strand a claimed job forever. This lease is
        # longer than the bounded model call and all of its transport retries.
        stale = self.session.scalar(select(ClarificationAnalysisJob.id).where(
            ClarificationAnalysisJob.status == "PROCESSING",
            ClarificationAnalysisJob.updated_at < now - timedelta(minutes=15),
        ).limit(1))
        if stale:
            self.session.execute(update(ClarificationAnalysisJob).where(
                ClarificationAnalysisJob.status == "PROCESSING",
                ClarificationAnalysisJob.updated_at < now - timedelta(minutes=15),
            ).values(status="PENDING", next_retry_at=now))
        self.session.commit()
        earlier = aliased(ClarificationAnalysisJob)
        blocked = exists(select(earlier.id).where(
            earlier.goal_id == ClarificationAnalysisJob.goal_id,
            earlier.id != ClarificationAnalysisJob.id,
            (earlier.status == "PROCESSING") | (
                (earlier.status.in_(["PENDING", "FAILED"]) & (earlier.attempts < earlier.max_attempts))
                & ((earlier.created_at < ClarificationAnalysisJob.created_at)
                   | ((earlier.created_at == ClarificationAnalysisJob.created_at)
                      & (earlier.id < ClarificationAnalysisJob.id)))
            ),
        ))
        query = select(ClarificationAnalysisJob).where(
            ClarificationAnalysisJob.status.in_(["PENDING", "FAILED"]),
            ClarificationAnalysisJob.attempts < ClarificationAnalysisJob.max_attempts,
            ClarificationAnalysisJob.next_retry_at <= now,
            ~blocked,
        )
        if job_id:
            query = query.where(ClarificationAnalysisJob.id == job_id)
        if goal_id:
            query = query.where(ClarificationAnalysisJob.goal_id == goal_id)
        job = self.session.scalar(query.order_by(ClarificationAnalysisJob.created_at))
        if job is None:
            return False, None

        claimed = self.session.execute(update(ClarificationAnalysisJob).where(
            ClarificationAnalysisJob.id == job.id,
            ClarificationAnalysisJob.status.in_(["PENDING", "FAILED"]),
            ~blocked,
        ).values(status="PROCESSING", attempts=ClarificationAnalysisJob.attempts + 1,
                 updated_at=now), execution_options={"synchronize_session": False})
        if claimed.rowcount:
            claimed_reply = self.session.get(Message, job.assistant_message_id)
            if claimed_reply:
                claimed_reply.metadata_json = {**claimed_reply.metadata_json,
                    "analysis_claim_owner": claim_owner()}
        self.session.commit()
        if not claimed.rowcount:
            return False, None
        self.session.refresh(job)

        user_message = self.session.get(Message, job.user_message_id)
        assistant_message = self.session.get(Message, job.assistant_message_id)
        goal = self.session.get(Goal, job.goal_id)
        conversation = self.session.get(Conversation, job.conversation_id)
        if not all((user_message, assistant_message, goal, conversation)):
            job.status = "FAILED"
            job.last_error = "分析任务关联的对话消息不存在"
            job.last_error_code = "ANALYSIS_CONTEXT_MISSING"
            job.completed_at = now
            self.session.commit()
            return True, None

        try:
            previous_values = self._active_fact_values(goal.id)
            metadata = dict(assistant_message.metadata_json)
            research_context, _, _ = ResearchContextService(self.session).prepare(
                goal.id, user_message.content, phase="CLARIFICATION",
                run_id=metadata.get("research_run_id"), conversation_id=conversation.id,
                reuse_latest=False,
            ) if metadata.get("research_run_id") else (None, None, None)
            context = self._build_context(
                goal,
                conversation,
                latest_user_message=user_message.content,
                answer_for_keys=list(assistant_message.metadata_json.get("question_keys", [])),
                through_sequence=user_message.sequence,
                research_context=research_context,
            )
            # Release the read transaction during the external model request.
            # The job's committed claim prevents another worker taking this turn.
            self.session.commit()
            analysis = self.provider.analyze(context, assistant_message.content)
            self.session.expire_all()
            if assistant_message.metadata_json.get("analysis_revision") != metadata.get("analysis_revision"):
                job.status = "PENDING"
                job.next_retry_at = utc_now()
                job.attempts = 0
                self.session.commit()
                return True, None
            # Direct edits in the requirement panel outrank an older extraction.
            newer_edits = set(self.session.scalars(select(RequirementChange.field_key).where(
                RequirementChange.goal_id == goal.id,
                RequirementChange.evidence_message_id.is_(None),
                RequirementChange.created_at > user_message.created_at,
            )))
            analysis = analysis.model_copy(update={"proposed_facts": [
                fact for fact in analysis.proposed_facts if fact.field_key not in newer_edits
            ]})
            self._apply_result(
                goal,
                conversation,
                ConversationReply(assistant_message=assistant_message.content),
                user_message,
                previous_values=previous_values,
                existing_assistant_message=assistant_message,
                analysis=analysis,
            )
            # This successful extraction saw the complete conversation through
            # this turn, including earlier failed turns. Retire those failures
            # only now; never skip their user conditions before a successful
            # replacement analysis has actually processed them.
            older_jobs = self.session.scalars(select(ClarificationAnalysisJob).join(
                Message, Message.id == ClarificationAnalysisJob.user_message_id
            ).where(
                ClarificationAnalysisJob.conversation_id == conversation.id,
                ClarificationAnalysisJob.status == "FAILED",
                ClarificationAnalysisJob.attempts >= ClarificationAnalysisJob.max_attempts,
                Message.sequence < user_message.sequence,
            ))
            for older in older_jobs:
                older.status = "SUCCEEDED"
                older.completed_at = utc_now()
                old_reply = self.session.get(Message, older.assistant_message_id)
                if old_reply:
                    old_reply.metadata_json = {**old_reply.metadata_json,
                        "analysis_status": "SUCCEEDED", "analysis_error_code": None,
                        "analysis_error_diagnostic": None, "recovered_by_analysis_job_id": job.id}
            job.status = "SUCCEEDED"
            job.last_error = None
            job.last_error_code = None
            job.completed_at = utc_now()
            self.session.commit()
            return True, None
        except ClarificationProviderError as error:
            return self._record_analysis_failure(job.id, assistant_message.id, error)
        except Exception as error:  # pragma: no cover - defensive boundary for provider adapters
            # Do not leave a durable job stuck in PROCESSING if an adapter or parser raises an
            # unexpected exception. The next retry starts from the last committed state.
            self.session.rollback()
            wrapped = ClarificationProviderError(
                "这轮信息整理暂时失败，后台会继续重试。",
                diagnostic=f"{type(error).__name__}: {error}",
                code="ANALYSIS_INTERNAL_ERROR",
            )
            return self._record_analysis_failure(job.id, assistant_message.id, wrapped)

    def _require_analysis_complete(self, goal_id: str) -> None:
        from planner_api.conversation_memory import require_clarification_analysis_complete
        require_clarification_analysis_complete(self.session, goal_id)

    def _record_analysis_failure(
        self,
        job_id: str,
        assistant_message_id: str,
        error: ClarificationProviderError,
    ) -> tuple[bool, float | None]:
        job = self.session.get(ClarificationAnalysisJob, job_id)
        if job is None:
            return True, None
        retryable = job.attempts < job.max_attempts
        delay = min(30, 2**job.attempts)
        now = utc_now()
        job.status = "PENDING" if retryable else "FAILED"
        job.last_error = str(error)
        job.last_error_code = error.code
        job.next_retry_at = now + timedelta(seconds=delay)
        job.completed_at = None if retryable else now
        assistant_message = self.session.get(Message, assistant_message_id)
        if assistant_message:
            metadata = dict(assistant_message.metadata_json)
            metadata.update(
                {
                    "analysis_status": "PENDING" if retryable else "FAILED",
                    "analysis_error_code": error.code,
                    "analysis_error_diagnostic": error.diagnostic,
                    "analysis_attempts": job.attempts,
                }
            )
            assistant_message.metadata_json = metadata
        self.session.commit()
        return True, delay if retryable else None

    def _readiness(self, goal_id: str) -> ReadinessResult:
        conversation = self.session.scalar(
            select(Conversation).where(
                Conversation.goal_id == goal_id,
                Conversation.phase == "CLARIFICATION",
            )
        )
        summary = (
            self.session.scalar(
                select(ConversationSummary)
                .where(ConversationSummary.conversation_id == conversation.id)
                .order_by(ConversationSummary.created_at.desc())
            )
            if conversation
            else None
        )
        if summary and summary.readiness_json:
            try:
                return self._validated_readiness(
                    goal_id, ReadinessResult.model_validate(summary.readiness_json)
                )
            except ValueError:
                pass
        values = self._confirmed_fact_values(goal_id)
        missing = [key for key in CORE_FIELDS if key not in values]
        return ReadinessResult(
            ready_for_review=not missing,
            blocking_fields=missing,
            reason=(
                "已有目标包含完整的旧版核心信息，可以进入总结确认。"
                if not missing
                else "还在讨论中；信息分析会在后续对话中持续更新。"
            ),
            confidence=1 if not missing else 0,
        )

    def _validated_readiness(
        self, goal_id: str, readiness: ReadinessResult | None
    ) -> ReadinessResult:
        if readiness is None:
            return ReadinessResult(
                ready_for_review=False,
                reason="本轮对话已保存，但需求分析暂时未完成。",
                confidence=0,
            )
        confirmed = self._confirmed_fact_values(goal_id)
        route_shaping = {
            "current_state",
            "desired_outcomes",
            "success_criteria",
            "available_capacity",
            "deadline",
            "scope_in",
            "scope_out",
            "constraints",
            "preferences",
            "existing_resources",
            "risks",
        }
        has_minimum_evidence = (
            "objective" in confirmed and len(route_shaping & confirmed.keys()) >= 2
        )
        all_legacy_core_fields = all(key in confirmed for key in CORE_FIELDS)
        if all_legacy_core_fields and not readiness.ready_for_review:
            return readiness.model_copy(
                update={
                    "ready_for_review": True,
                    "blocking_fields": [],
                    "blocking_topics": [],
                    "reason": "已确认的信息足以先形成一级路线，可以进入总结确认。",
                    "confidence": max(readiness.confidence, 0.9),
                }
            )
        if readiness.ready_for_review and not has_minimum_evidence:
            return readiness.model_copy(
                update={
                    "ready_for_review": False,
                    "blocking_fields": ([] if "objective" in confirmed else ["objective"]),
                    "blocking_topics": ["会明显改变路线方向的关键信息"],
                    "reason": "目前只有初步想法，还需要通过讨论形成更多路线依据。",
                    "confidence": 0,
                }
            )
        return readiness

    def _ensure_draft(self, goal_id: str) -> RequirementVersion:
        existing = self.session.scalar(
            select(RequirementVersion)
            .where(
                RequirementVersion.goal_id == goal_id,
                RequirementVersion.status == "DRAFT",
            )
            .order_by(RequirementVersion.version.desc())
        )
        snapshot = self._build_snapshot(goal_id)
        if existing:
            existing.snapshot_json = snapshot
            return existing
        latest_version = self.session.scalar(
            select(func.max(RequirementVersion.version)).where(
                RequirementVersion.goal_id == goal_id,
            )
        )
        draft = RequirementVersion(
            goal_id=goal_id,
            version=(latest_version or 0) + 1,
            status="DRAFT",
            snapshot_json=snapshot,
        )
        self.session.add(draft)
        self.session.flush()
        return draft

    def _build_snapshot(self, goal_id: str) -> dict[str, Any]:
        values = self._active_fact_values(goal_id)
        snapshot = {
            "objective": values.get("objective"),
            "motivation": values.get("motivation"),
            "current_state": values.get("current_state"),
            "desired_outcomes": values.get("desired_outcomes"),
            "success_criteria": values.get("success_criteria"),
            "available_capacity": values.get("available_capacity"),
            "deadline": values.get("deadline"),
            "scope_in": values.get("scope_in", []),
            "scope_out": values.get("scope_out", []),
            "constraints": values.get("constraints", []),
            "preferences": values.get("preferences", []),
            "existing_resources": values.get("existing_resources", []),
            "risks": values.get("risks", []),
            "open_questions": [],
            "assumptions": [],
        }
        goal = self.session.get(Goal, goal_id)
        deadline_fact = next((fact for fact in reversed(self._active_facts(goal_id))
                              if fact.field_key == "deadline" and fact.status == "CONFIRMED"), None)
        message = (self.session.get(Message, deadline_fact.evidence_message_id)
                   if deadline_fact and deadline_fact.evidence_message_id else None)
        reference_time = message.created_at if message else (deadline_fact.created_at if deadline_fact else goal.created_at)
        reference = local_date(reference_time, goal.timezone)
        resolved = resolve_deadline(snapshot["deadline"], reference)
        snapshot["temporal_context"] = {"timezone": goal.timezone,
            "deadline": {"source_text": snapshot["deadline"],
                "source_message_id": message.id if message else None,
                "reference_date": reference.isoformat(),
                "resolved_date": resolved.isoformat() if resolved else None}}
        capacity_fact = next((fact for fact in reversed(self._active_facts(goal_id))
                              if fact.field_key == "available_capacity" and fact.status == "CONFIRMED"), None)
        snapshot["capacity_source"] = {"status": "CONFIRMED" if capacity_fact else "UNKNOWN",
            "fact_id": capacity_fact.id if capacity_fact else None,
            "source_message_id": capacity_fact.evidence_message_id if capacity_fact else None,
            "source_text": values.get("available_capacity")}
        return snapshot

    def _active_facts(self, goal_id: str) -> list[RequirementFact]:
        return list(
            self.session.scalars(
                select(RequirementFact)
                .where(
                    RequirementFact.goal_id == goal_id,
                    RequirementFact.status.in_(["PROPOSED", "CONFIRMED"]),
                )
                .order_by(RequirementFact.created_at)
            )
        )

    def _active_fact_values(self, goal_id: str) -> dict[str, object]:
        return self._confirmed_fact_values(goal_id)

    def _confirmed_fact_values(self, goal_id: str) -> dict[str, object]:
        return {
            fact.field_key: fact.value_json
            for fact in self._active_facts(goal_id)
            if fact.status == "CONFIRMED"
        }

    def _proposed_fact_values(self, goal_id: str) -> dict[str, object]:
        return {
            fact.field_key: fact.value_json
            for fact in self._active_facts(goal_id)
            if fact.status == "PROPOSED"
        }

    def _supersede_drafts(self, goal_id: str) -> None:
        drafts = self.session.scalars(
            select(RequirementVersion).where(
                RequirementVersion.goal_id == goal_id,
                RequirementVersion.status == "DRAFT",
            )
        )
        for draft in drafts:
            draft.status = "SUPERSEDED"
        self.session.flush()

    def _next_sequence(self, conversation_id: str) -> int:
        maximum = self.session.scalar(
            select(func.max(Message.sequence)).where(Message.conversation_id == conversation_id)
        )
        return (maximum or 0) + 1

    def _get_goal_and_conversation(self, goal_id: str) -> tuple[Goal, Conversation]:
        goal = self.session.get(Goal, goal_id)
        if goal is None:
            raise HTTPException(status_code=404, detail="Goal not found")
        conversation = self.session.scalar(
            select(Conversation).where(
                Conversation.goal_id == goal_id,
                Conversation.phase == "CLARIFICATION",
            )
        )
        if conversation is None:
            raise HTTPException(status_code=404, detail="Clarification conversation not found")
        return goal, conversation


def run_pending_clarification_analysis(goal_id: str) -> None:
    """Retry queued memory extraction without tying it to the request session."""
    import time

    from planner_api.ai import build_clarification_provider
    from planner_api.config import get_settings
    from planner_api.database import SessionLocal

    for _ in range(4):
        with SessionLocal() as session:
            service = ClarificationService(session, build_clarification_provider(get_settings()))
            worked, delay = service.process_pending_analysis(goal_id=goal_id)
        if not worked:
            return
        if delay is not None:
            time.sleep(delay)
