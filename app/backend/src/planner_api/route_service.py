from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from planner_api.ai import ClarificationProviderError
from planner_api.config import get_settings
from planner_api.conversation_memory import refresh_phase_summary, require_clarification_analysis_complete
from planner_api.models import (
    Conversation,
    Goal,
    Message,
    RequirementVersion,
    ResearchRun,
    RouteStage,
    RouteVersion,
    utc_now,
)
from planner_api.planning_context import build_requirement_memory
from planner_api.research.consumer import ResearchContextService, research_summary_from_run
from planner_api.route_ai import RouteContext, RouteProvider
from planner_api.route_explanation_ai import (
    RouteExplanationContext,
    RouteExplanationProvider,
)
from planner_api.route_explanation_prompts import ROUTE_EXPLANATION_PROMPT_VERSION
from planner_api.route_prompts import ROUTE_PROMPT_VERSION
from planner_api.schemas import (
    MessageRead,
    RouteDraftResult,
    RouteStageRead,
    RouteVersionRead,
    RouteWorkspace,
)


class RouteService:
    def __init__(
        self,
        session: Session,
        provider: RouteProvider,
        explanation_provider: RouteExplanationProvider,
    ) -> None:
        self.session = session
        self.provider = provider
        self.explanation_provider = explanation_provider

    def get_workspace(self, goal_id: str) -> RouteWorkspace:
        goal, requirement = self._get_goal_and_requirement(goal_id)
        route = self._current_route(goal_id)
        pending_requirement = self.session.scalar(
            select(RequirementVersion)
            .where(
                RequirementVersion.goal_id == goal_id,
                RequirementVersion.status == "DRAFT",
            )
            .order_by(RequirementVersion.version.desc())
        )
        conversation = self._route_conversation(goal_id)
        messages: list[Message] = []
        if conversation:
            messages = list(
                self.session.scalars(
                    select(Message)
                    .where(Message.conversation_id == conversation.id)
                    .order_by(Message.sequence.desc())
                    .limit(get_settings().conversation_workspace_message_limit)
                )
            )
            messages.reverse()
        latest_research = self.session.scalar(
            select(ResearchRun)
            .where(
                ResearchRun.goal_id == goal_id,
                ResearchRun.conversation_id == conversation.id if conversation else False,
                ResearchRun.status.in_({"READY", "PARTIAL", "COMPLETED"}),
            )
            .order_by(ResearchRun.updated_at.desc())
            .limit(1)
        )
        return RouteWorkspace(
            goal_id=goal.id,
            goal_title=goal.title,
            status=goal.status,
            requirement_version=requirement.version,
            requirement_snapshot=requirement.snapshot_json,
            route=self._route_read(route) if route else None,
            messages=[
                MessageRead(
                    id=message.id,
                    sequence=message.sequence,
                    role=message.role,
                    content=message.content,
                    generation_status=message.generation_status,
                    metadata=message.metadata_json,
                    created_at=message.created_at,
                )
                for message in messages
            ],
            pending_requirement_version=(
                pending_requirement.version if pending_requirement and route else None
            ),
            requirements_changed_since_route=bool(
                pending_requirement
                and route
                and pending_requirement.snapshot_json != requirement.snapshot_json
            ),
            planning_mode=route.planning_mode if route else goal.planning_mode,
            schedule_type=route.schedule_type if route else goal.schedule_type,
            latest_research=research_summary_from_run(latest_research),
        )

    def draft_route(self, goal_id: str) -> RouteWorkspace:
        require_clarification_analysis_complete(self.session, goal_id)
        goal, requirement = self._get_goal_and_requirement(goal_id)
        if goal.status not in {"ROUTE_DRAFTING", "ROUTE_REVIEW"}:
            raise HTTPException(status_code=409, detail="Goal is not ready for route drafting")
        existing = self._current_route(goal_id)
        if existing:
            return self.get_workspace(goal_id)

        conversation = self._ensure_route_conversation(goal_id)
        retry_message = self._prepare_draft_retry(conversation)
        self.session.commit()
        result = self._generate_or_record_failure(
            conversation,
            RouteContext(
                requirements=requirement.snapshot_json,
                planning_mode=goal.planning_mode,
                schedule_type=goal.schedule_type,
                requirement_memory=build_requirement_memory(self.session, goal_id),
            ),
            retry_message=retry_message,
        )
        if result is None:
            return self.get_workspace(goal_id)
        self._persist_route(goal, requirement, result, revision=1, feedback=None)
        goal.status = "ROUTE_REVIEW"
        goal.last_active_at = utc_now()
        self._add_assistant_message(conversation, result, existing=retry_message)
        self.session.commit()
        return self.get_workspace(goal_id)

    def revise_route(
        self,
        goal_id: str,
        feedback: str,
        *,
        web_research_enabled: bool = False,
        research_run_id: str | None = None,
        regenerate: bool = False,
        use_saved_research: bool = True,
        material_source_ids: list[str] | None = None,
    ) -> RouteWorkspace:
        require_clarification_analysis_complete(self.session, goal_id)
        goal, requirement = self._get_goal_and_requirement(goal_id)
        if goal.status != "ROUTE_REVIEW":
            raise HTTPException(status_code=409, detail="Route is not awaiting review")
        current = self._current_route(goal_id)
        if current is None or current.status != "DRAFT":
            raise HTTPException(status_code=409, detail="No route draft exists")
        conversation = self._ensure_route_conversation(goal_id)
        clean_feedback = feedback.strip()
        from planner_api.materials.conversation import resolve_discussion_sources, remember_discussion_sources
        material_source_ids = resolve_discussion_sources(self.session, conversation, clean_feedback, "REVISE", material_source_ids, regenerate=regenerate)
        from planner_api.materials.analysis_ai import build_material_analysis_provider
        from planner_api.materials.analysis_service import MaterialAnalysisService
        material_context = MaterialAnalysisService(self.session, build_material_analysis_provider(get_settings())).prepare_conversation_context(
            goal_id, clean_feedback, material_source_ids) if material_source_ids else None
        retry_message = self._prepare_exchange(conversation, clean_feedback, "REVISE", allow_existing=regenerate)
        remember_discussion_sources(self.session, conversation, material_source_ids, material_context)
        goal.last_active_at = utc_now()
        self.session.commit()

        research_context, resolved_run_id, research_status = ResearchContextService(
            self.session
        ).prepare(
            goal_id,
            clean_feedback,
            phase="ROUTE",
            enabled=web_research_enabled,
            run_id=research_run_id,
            conversation_id=conversation.id,
            idempotency_key=f"route-revise:{goal_id}:{conversation.id}:{clean_feedback[:80]}",
            reuse_latest=use_saved_research,
        )

        result = self._generate_or_record_failure(
            conversation,
            RouteContext(
                requirements=requirement.snapshot_json,
                planning_mode=goal.planning_mode,
                schedule_type=goal.schedule_type,
                current_route=self._route_payload(current),
                user_feedback=clean_feedback,
                requirement_memory=build_requirement_memory(self.session, goal_id),
                research_context=research_context,
                material_context=material_context,
            ),
            retry_message=retry_message,
            intent="REVISE",
        )
        if result is None:
            return self.get_workspace(goal_id)

        current.status = "SUPERSEDED"
        self._persist_route(
            goal,
            requirement,
            result,
            revision=current.revision + 1,
            feedback=clean_feedback,
        )
        self._add_assistant_message(
            conversation,
            result,
            existing=retry_message,
            intent="REVISE",
            research_run_id=resolved_run_id,
            research_status=research_status,
        )
        self.session.commit()
        return self.get_workspace(goal_id)

    def explain_route(
        self,
        goal_id: str,
        question: str,
        *,
        web_research_enabled: bool = False,
        research_run_id: str | None = None,
        regenerate: bool = False,
        use_saved_research: bool = True,
        material_source_ids: list[str] | None = None,
    ) -> RouteWorkspace:
        goal, requirement = self._get_goal_and_requirement(goal_id)
        if goal.status != "ROUTE_REVIEW":
            raise HTTPException(status_code=409, detail="Route is not awaiting review")
        current = self._current_route(goal_id)
        if current is None or current.status != "DRAFT":
            raise HTTPException(status_code=409, detail="No route draft exists")
        conversation = self._ensure_route_conversation(goal_id)
        clean_question = question.strip()
        from planner_api.materials.conversation import resolve_discussion_sources, remember_discussion_sources
        material_source_ids = resolve_discussion_sources(self.session, conversation, clean_question, "EXPLAIN", material_source_ids, regenerate=regenerate)
        from planner_api.materials.analysis_ai import build_material_analysis_provider
        from planner_api.materials.analysis_service import MaterialAnalysisService
        material_context = MaterialAnalysisService(self.session, build_material_analysis_provider(get_settings())).prepare_conversation_context(
            goal_id, clean_question, material_source_ids) if material_source_ids else None
        retry_message = self._prepare_exchange(conversation, clean_question, "EXPLAIN", allow_existing=regenerate)
        remember_discussion_sources(self.session, conversation, material_source_ids, material_context)
        goal.last_active_at = utc_now()
        self.session.commit()

        research_context, resolved_run_id, research_status = ResearchContextService(
            self.session
        ).prepare(
            goal_id,
            clean_question,
            phase="ROUTE",
            enabled=web_research_enabled,
            run_id=research_run_id,
            conversation_id=conversation.id,
            idempotency_key=f"route-explain:{goal_id}:{conversation.id}:{clean_question[:80]}",
            reuse_latest=use_saved_research,
        )

        try:
            result = self.explanation_provider.explain(
                RouteExplanationContext(
                    requirements=requirement.snapshot_json,
                    current_route=self._route_payload(current),
                    question=clean_question,
                    conversation_memory={**build_requirement_memory(self.session, goal_id).get("route_review", {}),
                        "long_term_memory": build_requirement_memory(self.session, goal_id, clean_question).get("long_term_memory", [])},
                    research_context=research_context,
                    material_context=material_context,
                )
            )
        except ClarificationProviderError as error:
            self._record_failure(
                conversation,
                error,
                prompt_version=ROUTE_EXPLANATION_PROMPT_VERSION,
                intent="EXPLAIN",
                existing=retry_message,
            )
            self.session.commit()
            return self.get_workspace(goal_id)

        answer_message = retry_message or Message(
            conversation_id=conversation.id,
            sequence=self._next_sequence(conversation.id),
            role="ASSISTANT",
            content=result.answer,
        )
        answer_message.content = result.answer
        from planner_api.goal_memory import save_memories
        save_memories(self.session, conversation, result.long_term_memories)
        answer_message.generation_status = "SUCCEEDED"
        answer_message.metadata_json = {
            "prompt_version": ROUTE_EXPLANATION_PROMPT_VERSION,
            "intent": "EXPLAIN",
            "covered_questions": result.covered_questions,
            "needs_clarification": result.needs_clarification,
            "research_run_id": resolved_run_id,
            "research_status": research_status,
        }
        if retry_message is None:
            self.session.add(answer_message)
        refresh_phase_summary(
            self.session,
            conversation,
            open_questions=[question] if result.needs_clarification else [],
        )
        self.session.commit()
        return self.get_workspace(goal_id)

    def confirm_route(self, goal_id: str) -> RouteWorkspace:
        require_clarification_analysis_complete(self.session, goal_id)
        goal, _ = self._get_goal_and_requirement(goal_id)
        if goal.status != "ROUTE_REVIEW":
            raise HTTPException(status_code=409, detail="Route is not awaiting review")
        route = self._current_route(goal_id)
        if route is None or route.status != "DRAFT":
            raise HTTPException(status_code=409, detail="No route draft exists")
        route.status = "CONFIRMED"
        route.confirmed_at = utc_now()
        goal.active_route_version_id = route.id
        goal.status = "PLAN_DRAFTING"
        goal.last_active_at = utc_now()
        self.session.commit()
        return self.get_workspace(goal_id)

    def _persist_route(
        self,
        goal: Goal,
        requirement: RequirementVersion,
        result: RouteDraftResult,
        *,
        revision: int,
        feedback: str | None,
    ) -> RouteVersion:
        latest_confirmed = self.session.scalar(
            select(func.max(RouteVersion.version)).where(
                RouteVersion.goal_id == goal.id,
                RouteVersion.status == "CONFIRMED",
            )
        )
        route = RouteVersion(
            goal_id=goal.id,
            requirement_version_id=requirement.id,
            version=(latest_confirmed or 0) + 1,
            revision=revision,
            status="DRAFT",
            summary=result.summary,
            tradeoffs_json=result.tradeoffs,
            assumptions_json=result.assumptions,
            open_questions_json=result.open_questions,
            optional_questions_json=result.optional_questions,
            change_summary_json=result.change_summary,
            feedback=feedback,
            planning_mode=goal.planning_mode,
            schedule_type=goal.schedule_type,
            prompt_version=ROUTE_PROMPT_VERSION,
            source_document=result.source_document,
            compiled_snapshot_json=result.model_dump(
                mode="json", exclude={"source_document", "compiler_report"}
            ),
            compiler_report_json=result.compiler_report,
        )
        self.session.add(route)
        self.session.flush()
        for stage in result.stages:
            self.session.add(
                RouteStage(
                    route_version_id=route.id,
                    order=stage.order,
                    title=stage.title,
                    purpose=stage.purpose,
                    topics_json=stage.topics,
                    outputs_json=stage.outputs,
                    depends_on_json=stage.depends_on,
                    options_json=[option.model_dump() for option in stage.options],
                )
            )
        self.session.flush()
        return route

    def _generate_or_record_failure(
        self,
        conversation: Conversation,
        context: RouteContext,
        *,
        retry_message: Message | None = None,
        intent: str = "DRAFT",
        research_run_id: str | None = None,
        research_status: str | None = None,
    ) -> RouteDraftResult | None:
        try:
            return self.provider.draft_route(context)
        except ClarificationProviderError as error:
            self._record_failure(
                conversation,
                error,
                prompt_version=ROUTE_PROMPT_VERSION,
                intent=intent,
                existing=retry_message,
            )
            self.session.commit()
            return None

    def _add_assistant_message(
        self,
        conversation: Conversation,
        result: RouteDraftResult,
        *,
        existing: Message | None = None,
        intent: str = "DRAFT",
        research_run_id: str | None = None,
        research_status: str | None = None,
    ) -> None:
        message = existing or Message(
            conversation_id=conversation.id,
            sequence=self._next_sequence(conversation.id),
            role="ASSISTANT",
            content=result.assistant_message,
        )
        message.content = result.assistant_message
        message.generation_status = "SUCCEEDED"
        message.metadata_json = {
            "prompt_version": ROUTE_PROMPT_VERSION,
            "intent": intent,
            "change_summary": result.change_summary,
            "research_run_id": research_run_id,
            "research_status": research_status,
        }
        if existing is None:
            self.session.add(message)
        refresh_phase_summary(
            self.session,
            conversation,
            confirmed_conclusions=result.change_summary,
            open_questions=result.open_questions,
        )

    def _prepare_exchange(
        self, conversation: Conversation, content: str, intent: str, *, allow_existing: bool = False
    ) -> Message | None:
        recent = list(
            self.session.scalars(
                select(Message)
                .where(Message.conversation_id == conversation.id)
                .order_by(Message.sequence.desc())
                .limit(2)
            )
        )
        if len(recent) == 2:
            assistant, user = recent
            saved_intent = assistant.metadata_json.get("intent") or user.metadata_json.get("intent")
            if (
                assistant.role == "ASSISTANT"
                and assistant.generation_status in (
                    {"FAILED", "PENDING", "SUCCEEDED"} if allow_existing else {"FAILED"}
                )
                and user.role == "USER"
                and user.content.strip() == content
                and saved_intent == intent
            ):
                assistant.content = "正在重新尝试，之前的内容和路线都已保留。"
                assistant.generation_status = "PENDING"
                assistant.metadata_json = {"intent": intent, "retrying": True}
                return assistant

        self.session.add(
            Message(
                conversation_id=conversation.id,
                sequence=self._next_sequence(conversation.id),
                role="USER",
                content=content,
                metadata_json={"intent": intent},
            )
        )
        return None

    def _prepare_draft_retry(self, conversation: Conversation) -> Message | None:
        latest = self.session.scalar(
            select(Message)
            .where(Message.conversation_id == conversation.id)
            .order_by(Message.sequence.desc())
        )
        if (
            latest
            and latest.role == "ASSISTANT"
            and latest.generation_status == "FAILED"
            and latest.metadata_json.get("intent") == "DRAFT"
        ):
            latest.content = "正在重新尝试生成路线，已确认需求仍然安全保留。"
            latest.generation_status = "PENDING"
            latest.metadata_json = {"intent": "DRAFT", "retrying": True}
            return latest
        return None

    def regenerate_last_message(self, goal_id: str) -> RouteWorkspace:
        """Regenerate the latest route discussion answer without adding a user turn."""
        conversation = self._ensure_route_conversation(goal_id)
        recent = list(
            self.session.scalars(
                select(Message)
                .where(Message.conversation_id == conversation.id)
                .order_by(Message.sequence.desc())
                .limit(2)
            )
        )
        if len(recent) != 2 or recent[0].role != "ASSISTANT" or recent[1].role != "USER":
            raise HTTPException(status_code=409, detail="No route answer to regenerate")
        assistant, user = recent
        intent = (assistant.metadata_json or {}).get("intent") or (user.metadata_json or {}).get("intent")
        if intent not in {"REVISE", "EXPLAIN"}:
            raise HTTPException(status_code=409, detail="No route answer to regenerate")
        assistant.content = "正在重新整理这次路线回复，当前路线仍保持不变。"
        assistant.generation_status = "PENDING"
        assistant.metadata_json = {"intent": intent, "regenerating": True}
        self.session.commit()
        if intent == "EXPLAIN":
            return self.explain_route(goal_id, user.content, regenerate=True)
        return self.revise_route(goal_id, user.content, regenerate=True)

    def _record_failure(
        self,
        conversation: Conversation,
        error: ClarificationProviderError,
        *,
        prompt_version: str,
        intent: str,
        existing: Message | None = None,
    ) -> None:
        message = existing or Message(
            conversation_id=conversation.id,
            sequence=self._next_sequence(conversation.id),
            role="ASSISTANT",
            content=str(error),
        )
        message.content = str(error)
        message.generation_status = "FAILED"
        message.metadata_json = {
            "error_type": type(error).__name__,
            "error_diagnostic": error.diagnostic,
            "error_code": error.code,
            "prompt_version": prompt_version,
            "intent": intent,
        }
        if existing is None:
            self.session.add(message)

    def _get_goal_and_requirement(self, goal_id: str) -> tuple[Goal, RequirementVersion]:
        goal = self.session.get(Goal, goal_id)
        if goal is None:
            raise HTTPException(status_code=404, detail="Goal not found")
        if goal.planning_mode_confirmed_at is None:
            raise HTTPException(status_code=409, detail="Planning approach has not been confirmed")
        if goal.schedule_type_confirmed_at is None:
            raise HTTPException(status_code=409, detail="Schedule type has not been confirmed")
        if not goal.current_requirement_version_id:
            raise HTTPException(status_code=409, detail="Requirements have not been confirmed")
        requirement = self.session.get(RequirementVersion, goal.current_requirement_version_id)
        if requirement is None or requirement.status != "CONFIRMED":
            raise HTTPException(status_code=409, detail="Confirmed requirement version not found")
        return goal, requirement

    def _current_route(self, goal_id: str) -> RouteVersion | None:
        return self.session.scalar(
            select(RouteVersion)
            .where(
                RouteVersion.goal_id == goal_id,
                RouteVersion.status.in_(["DRAFT", "CONFIRMED"]),
            )
            .order_by(RouteVersion.created_at.desc())
        )

    def _route_conversation(self, goal_id: str) -> Conversation | None:
        return self.session.scalar(
            select(Conversation).where(
                Conversation.goal_id == goal_id,
                Conversation.phase == "ROUTE",
            )
        )

    def _ensure_route_conversation(self, goal_id: str) -> Conversation:
        conversation = self._route_conversation(goal_id)
        if conversation:
            return conversation
        conversation = Conversation(goal_id=goal_id, phase="ROUTE")
        self.session.add(conversation)
        self.session.flush()
        return conversation

    def _route_payload(self, route: RouteVersion) -> dict[str, object]:
        value = self._route_read(route)
        return value.model_dump(exclude={"id", "status"})

    def _route_read(self, route: RouteVersion) -> RouteVersionRead:
        stages = list(
            self.session.scalars(
                select(RouteStage)
                .where(RouteStage.route_version_id == route.id)
                .order_by(RouteStage.order)
            )
        )
        return RouteVersionRead(
            id=route.id,
            version=route.version,
            revision=route.revision,
            status=route.status,
            summary=route.summary,
            stages=[
                RouteStageRead(
                    id=stage.id,
                    order=stage.order,
                    title=stage.title,
                    purpose=stage.purpose,
                    topics=stage.topics_json,
                    outputs=stage.outputs_json,
                    depends_on=stage.depends_on_json,
                    options=stage.options_json,
                )
                for stage in stages
            ],
            tradeoffs=route.tradeoffs_json,
            assumptions=route.assumptions_json,
            open_questions=route.open_questions_json,
            optional_questions=route.optional_questions_json,
            change_summary=route.change_summary_json,
            source_document=route.source_document,
            compiler_report=route.compiler_report_json,
            planning_mode=route.planning_mode,
            schedule_type=route.schedule_type,
        )

    def _next_sequence(self, conversation_id: str) -> int:
        maximum = self.session.scalar(
            select(func.max(Message.sequence)).where(Message.conversation_id == conversation_id)
        )
        return (maximum or 0) + 1
