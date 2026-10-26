from __future__ import annotations

from datetime import date, datetime, timedelta

from fastapi import HTTPException
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from planner_api.config import get_settings
from planner_api.materials.analysis_ai import build_material_analysis_provider
from planner_api.materials.analysis_service import MaterialAnalysisService
from planner_api.materials.service import MaterialService
from planner_api.conversation_memory import refresh_phase_summary
from planner_api.execution_service import ExecutionService
from planner_api.goal_error_memory import persist_explicit_error_memories
from planner_api.models import (
    Conversation,
    ConversationSummary,
    ExecutionInsight,
    ExecutionReview,
    Goal,
    Message,
    PlanActivation,
    PlanDay,
    PlanDaySchedule,
    PlanTask,
    PlanVersion,
    PlanWindow,
    RequirementVersion,
    ResearchRun,
    RouteVersion,
    UserMemoryItem,
    utc_now,
)
from planner_api.plan_discussion_ai import PlanDiscussionContext, PlanDiscussionProvider
from planner_api.planning_context import build_requirement_memory
from planner_api.research.consumer import ResearchContextService, research_summary_from_run
from planner_api.schemas import (
    ExecutionDiscussionResult,
    ExecutionDiscussionWorkspace,
    ExecutionInsightRead,
    ExecutionReviewRead,
    MessageRead,
)


class ExecutionDiscussionService:
    def __init__(self, session: Session, provider: PlanDiscussionProvider) -> None:
        self.session = session
        self.provider = provider

    def get_workspace(self, goal_id: str) -> ExecutionDiscussionWorkspace:
        goal, plan = self._goal_and_plan(goal_id)
        self._activate_due_reviews(goal_id)
        conversation = self._conversation(goal_id)
        if conversation:
            self._recover_stale_generations(conversation.id)
        messages = self._messages(conversation.id, limit=30) if conversation else []
        insights = list(
            self.session.scalars(
                select(ExecutionInsight)
                .where(
                    ExecutionInsight.goal_id == goal_id,
                    ExecutionInsight.status.in_(["CONFIRMED", "PROPOSED"]),
                )
                .order_by(ExecutionInsight.created_at.desc())
                .limit(30)
            )
        )
        review = self.session.scalar(
            select(ExecutionReview)
            .where(
                ExecutionReview.goal_id == goal_id,
                ExecutionReview.status.in_(["SUGGESTED", "DISCUSSING"]),
            )
            .order_by(ExecutionReview.created_at.desc())
        )
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
        progress = ExecutionService(self.session).get_progress(goal_id)
        return ExecutionDiscussionWorkspace(
            goal_id=goal.id,
            goal_title=goal.title,
            goal_status=goal.status,
            planning_mode=plan.planning_mode,
            schedule_type=plan.schedule_type,
            messages=[self._message_read(item) for item in messages[-30:]],
            insights=[self._insight_read(item) for item in insights],
            active_review=self._review_read(review) if review else None,
            progress=progress,
            latest_analysis=self._latest_analysis(messages),
            latest_research=research_summary_from_run(latest_research),
        )

    def discuss(
        self,
        goal_id: str,
        content: str,
        *,
        web_research_enabled: bool = False,
        research_run_id: str | None = None,
        use_saved_research: bool = True,
        regenerate: bool = False,
        material_source_ids: list[str] | None = None,
    ) -> ExecutionDiscussionWorkspace:
        goal, plan = self._goal_and_plan(goal_id)
        conversation = self._ensure_conversation(goal_id)
        existing_assistant = None
        if regenerate:
            recent = self._messages(conversation.id, limit=2)
            if len(recent) == 2 and recent[-1].role == "ASSISTANT" and recent[-2].role == "USER":
                user_message = recent[-2]
                existing_assistant = recent[-1]
            elif recent and recent[-1].role == "USER":
                # A generation can be interrupted after the durable user turn
                # is committed but before an assistant row exists. Regenerate
                # that exact turn in place instead of creating a duplicate.
                user_message = recent[-1]
            else:
                user_message = Message(
                    conversation_id=conversation.id,
                    sequence=self._next_sequence(conversation.id),
                    role="USER",
                    content=content.strip(),
                    metadata_json={"intent": "EXECUTION_DISCUSSION"},
                )
                self.session.add(user_message)
        else:
            user_message = Message(
                conversation_id=conversation.id,
                sequence=self._next_sequence(conversation.id),
                role="USER",
                content=content.strip(),
                metadata_json={"intent": "EXECUTION_DISCUSSION"},
            )
            self.session.add(user_message)
        if not regenerate or not user_message.metadata_json.get("material_context"):
            material_context = MaterialAnalysisService(self.session, build_material_analysis_provider(get_settings())).prepare_conversation_context(goal_id, user_message.content, material_source_ids) if material_source_ids else None
            user_message.metadata_json = {**user_message.metadata_json, "material_source_ids": list(dict.fromkeys(material_source_ids or [])), "material_context": material_context, "material_titles": [source["title"] for source in (material_context or {}).get("sources", [])]}
        self.session.flush()
        from planner_api.codex_runtime.conversation_files import bind_file_message
        bind_file_message(user_message.metadata_json.get("material_context"), user_message)
        goal.last_active_at = utc_now()
        self.session.commit()
        # Persist the in-flight assistant turn before entering the provider.
        # If the client disconnects or the process is interrupted while the
        # model is waiting, the next workspace load can still offer recovery.
        if existing_assistant is None:
            existing_assistant = Message(
                conversation_id=conversation.id,
                sequence=self._next_sequence(conversation.id),
                role="ASSISTANT",
                content="",
                generation_status="GENERATING",
                metadata_json={
                    "intent": "EXECUTION_DISCUSSION",
                    "retryable": True,
                    "generation_started_at": utc_now().isoformat(),
                },
            )
            self.session.add(existing_assistant)
        else:
            existing_assistant.content = ""
            existing_assistant.generation_status = "GENERATING"
            existing_assistant.metadata_json = {
                "intent": "EXECUTION_DISCUSSION",
                "retryable": True,
                "generation_started_at": utc_now().isoformat(),
            }
        self.session.commit()
        return self._respond(
            goal,
            plan,
            conversation,
            user_message,
            web_research_enabled=web_research_enabled,
            research_run_id=research_run_id,
            use_saved_research=use_saved_research,
            existing_assistant=existing_assistant,
        )

    def retry_last(self, goal_id: str) -> ExecutionDiscussionWorkspace:
        goal, plan = self._goal_and_plan(goal_id)
        conversation = self._conversation(goal_id)
        if conversation is None:
            return self.get_workspace(goal_id)
        messages = self._messages(conversation.id, limit=100)
        if messages and messages[-1].role == "USER" and messages[-1].metadata_json.get("intent") == "EXECUTION_DISCUSSION":
            # A process interruption can leave the durable user turn without
            # an assistant row. Treat that as a recoverable pending reply.
            return self._respond(
                goal,
                plan,
                conversation,
                messages[-1],
            )
        failed = next(
            (
                item
                for item in reversed(messages)
                if item.role == "ASSISTANT"
                and item.generation_status == "FAILED"
                and item.metadata_json.get("intent") == "EXECUTION_DISCUSSION"
            ),
            None,
        )
        if failed is None:
            return self.get_workspace(goal_id)
        user_message = next(
            (
                item
                for item in reversed(messages)
                if item.role == "USER" and item.sequence < failed.sequence
            ),
            None,
        )
        if user_message is None:
            return self.get_workspace(goal_id)
        metadata = failed.metadata_json or {}
        return self._respond(
            goal,
            plan,
            conversation,
            user_message,
            research_run_id=metadata.get("research_run_id"),
            existing_assistant=failed,
        )

    def decide_insight(
        self, goal_id: str, insight_id: str, action: str
    ) -> ExecutionDiscussionWorkspace:
        self._goal_and_plan(goal_id)
        insight = self.session.get(ExecutionInsight, insight_id)
        if insight is None or insight.goal_id != goal_id or insight.status != "PROPOSED":
            return self.get_workspace(goal_id)
        if action == "CONFIRM" and insight.supersedes_id:
            previous = self.session.get(ExecutionInsight, insight.supersedes_id)
            if (previous is None or previous.goal_id != goal_id or previous.scope != insight.scope
                    or previous.status != "CONFIRMED"):
                raise HTTPException(409, "这条记忆已有更新，请查看最新内容后再确认。")
        insight.status = "CONFIRMED" if action == "CONFIRM" else "DISMISSED"
        insight.decided_at = utc_now()
        if action == "CONFIRM" and insight.supersedes_id:
            previous = self.session.get(ExecutionInsight, insight.supersedes_id)
            if previous and previous.goal_id == goal_id and previous.scope == insight.scope:
                previous.status = "SUPERSEDED"
                previous.decided_at = utc_now()
        if action == "CONFIRM" and insight.scope == "USER" and insight.memory_key:
            memory = self.session.scalar(
                select(UserMemoryItem).where(
                    UserMemoryItem.profile_id == "default",
                    UserMemoryItem.memory_key == insight.memory_key,
                )
            )
            if memory is None:
                memory = UserMemoryItem(
                    profile_id="default",
                    memory_key=insight.memory_key,
                    content=insight.content,
                    source_type=insight.source_type,
                    evidence_message_id=insight.source_message_id,
                    valid_from=insight.valid_from,
                    valid_until=insight.valid_until,
                )
                self.session.add(memory)
            else:
                memory.content = insight.content
                memory.source_type = insight.source_type
                memory.evidence_message_id = insight.source_message_id
                memory.valid_from = insight.valid_from
                memory.valid_until = insight.valid_until
                memory.status = "CONFIRMED"
        self.session.commit()
        return self.get_workspace(goal_id)

    def dismiss_review(self, goal_id: str, review_id: str) -> ExecutionDiscussionWorkspace:
        self._goal_and_plan(goal_id)
        review = self.session.get(ExecutionReview, review_id)
        if (
            review is not None
            and review.goal_id == goal_id
            and review.status
            in {
                "SUGGESTED",
                "DISCUSSING",
            }
        ):
            review.status = "DISMISSED"
            review.updated_at = utc_now()
            self.session.commit()
        return self.get_workspace(goal_id)

    def _respond(
        self,
        goal: Goal,
        plan: PlanVersion,
        conversation: Conversation,
        user_message: Message,
        *,
        web_research_enabled: bool = False,
        research_run_id: str | None = None,
        use_saved_research: bool = True,
        existing_assistant: Message | None = None,
    ) -> ExecutionDiscussionWorkspace:
        if existing_assistant is not None:
            existing_assistant.generation_status = "GENERATING"
            existing_assistant.metadata_json = {
                **(existing_assistant.metadata_json or {}),
                "generation_started_at": utc_now().isoformat(),
            }
            self.session.commit()
        material_context = (user_message.metadata_json or {}).get("material_context")
        from planner_api.codex_runtime.conversation_files import bind_file_message
        bind_file_message(material_context, user_message)
        if material_context:
            for source_id in material_context.get("source_ids", []):
                MaterialService(self.session)._active_link(goal.id, source_id)
        research_context, resolved_run_id, research_status = ResearchContextService(
            self.session
        ).prepare(
            goal.id,
            user_message.content,
            phase="EXECUTION",
            enabled=web_research_enabled,
            run_id=research_run_id,
            reuse_latest=use_saved_research,
            conversation_id=conversation.id,
            idempotency_key=f"execution:{goal.id}:{user_message.id}",
        )
        # Build the bounded context and end the read transaction before
        # entering the potentially slow network/model call. Keeping a
        # database connection open during generation blocks refreshes and can
        # make the local SQLite-backed UI appear frozen.
        discussion_context = self._context(
            goal,
            plan,
            conversation,
            user_message.content,
            research_context=research_context,
            material_context=material_context,
        )
        self.session.commit()
        goal_id = goal.id
        plan_id = plan.id
        conversation_id = conversation.id
        user_message_id = user_message.id
        existing_assistant_id = existing_assistant.id if existing_assistant is not None else None
        # Session.commit() ends the transaction, but the Session may still
        # retain its SQLite connection until it is closed. Release it while
        # waiting on the model so workspace reads remain responsive.
        self.session.close()
        try:
            result = self.provider.discuss_execution(discussion_context)
        except Exception as error:
            goal = self.session.get(Goal, goal_id)
            conversation = self.session.get(Conversation, conversation_id)
            failed_message = (
                self.session.get(Message, existing_assistant_id)
                if existing_assistant_id
                else Message(
                    conversation_id=conversation_id,
                    sequence=self._next_sequence(conversation_id),
                    role="ASSISTANT",
                    content=str(error),
                )
            )
            failed_message.content = (
                str(error).strip()
                or "这次回复没有在限定时间内完成，你的内容已经保存，可以重新生成。"
            )
            failed_message.generation_status = "FAILED"
            failed_message.metadata_json = {
                "intent": "EXECUTION_DISCUSSION",
                "error_code": getattr(error, "code", "MODEL_GENERATION_FAILED"),
                "error_diagnostic": getattr(error, "diagnostic", str(error)[:500]),
                "retryable": True,
                "research_run_id": resolved_run_id,
                "research_status": research_status,
            }
            if existing_assistant is None:
                self.session.add(failed_message)
            self.session.commit()
            return self.get_workspace(goal.id)

        goal = self.session.get(Goal, goal_id)
        plan = self.session.get(PlanVersion, plan_id)
        conversation = self.session.get(Conversation, conversation_id)
        user_message = self.session.get(Message, user_message_id)
        assistant = (
            self.session.get(Message, existing_assistant_id)
            if existing_assistant_id
            else Message(
            conversation_id=conversation_id,
            sequence=self._next_sequence(conversation.id),
            role="ASSISTANT",
            content=result.assistant_message,
            metadata_json={
                "intent": "EXECUTION_DISCUSSION",
                "execution_analysis": result.model_dump(mode="json"),
                "research_run_id": resolved_run_id,
                "research_status": research_status,
            },
        )
        )
        assistant.content = result.assistant_message
        assistant.generation_status = "SUCCEEDED"
        assistant.metadata_json = {
            "intent": "EXECUTION_DISCUSSION",
            "execution_analysis": result.model_dump(mode="json"),
            "research_run_id": resolved_run_id,
            "research_status": research_status,
        }
        if existing_assistant is None:
            self.session.add(assistant)
        self.session.flush()
        self._persist_insights(goal, plan, user_message, result)
        from planner_api.goal_memory import save_memories
        save_memories(self.session, conversation, result.long_term_memories, source_message=user_message)
        persist_explicit_error_memories(
            self.session,
            goal_id=goal.id,
            source_message_id=user_message.id,
            user_content=user_message.content,
            candidates=result.error_memory_candidates,
        )
        self._persist_review(goal, plan, user_message, result)
        refresh_phase_summary(
            self.session,
            conversation,
            open_questions=result.questions,
        )
        self.session.commit()
        return self.get_workspace(goal.id)

    def _recover_stale_generations(self, conversation_id: str) -> None:
        messages = self._messages(conversation_id, limit=30)
        settings = get_settings()
        stale_after = max(
            180,
            int(settings.plan_explanation_timeout_seconds
                * (settings.plan_explanation_max_retries + 1) + 30),
        )
        cutoff = utc_now() - timedelta(seconds=stale_after)
        changed = False
        for message in messages:
            created_at = message.created_at
            started_at = (message.metadata_json or {}).get("generation_started_at")
            if started_at:
                try:
                    created_at = datetime.fromisoformat(started_at)
                except (TypeError, ValueError):
                    pass
            if created_at.tzinfo is None:
                created_at = created_at.replace(tzinfo=utc_now().tzinfo)
            if (
                message.role == "ASSISTANT"
                and message.generation_status in {"GENERATING", "PENDING"}
                and created_at < cutoff
            ):
                message.generation_status = "FAILED"
                message.content = "这次回复等待超时，但你的问题已经保存，可以重新生成。"
                message.metadata_json = {
                    **(message.metadata_json or {}),
                    "error_code": "MODEL_GENERATION_TIMEOUT",
                    "retryable": True,
                    "recovered_at": utc_now().isoformat(),
                }
                changed = True
        if changed:
            self.session.commit()

    def _context(
        self,
        goal: Goal,
        plan: PlanVersion,
        conversation: Conversation,
        user_input: str,
        *,
        research_context: dict[str, object] | None = None,
        material_context: dict[str, object] | None = None,
    ) -> PlanDiscussionContext:
        route = self.session.get(RouteVersion, plan.route_version_id)
        requirement = (
            self.session.get(RequirementVersion, route.requirement_version_id) if route else None
        )
        messages = self._messages(conversation.id, limit=12)
        clarification = self.session.scalar(
            select(Conversation).where(
                Conversation.goal_id == goal.id,
                Conversation.phase == "CLARIFICATION",
            )
        )
        clarification_messages = self._messages(clarification.id, limit=60) if clarification else []
        clarification_summary = (
            self.session.scalar(
                select(ConversationSummary)
                .where(ConversationSummary.conversation_id == clarification.id)
                .order_by(ConversationSummary.created_at.desc())
            )
            if clarification
            else None
        )
        confirmed_insights = list(
            self.session.scalars(
                select(ExecutionInsight)
                .where(
                    ExecutionInsight.goal_id == goal.id,
                    ExecutionInsight.status == "CONFIRMED",
                    or_(
                        ExecutionInsight.valid_until.is_(None),
                        ExecutionInsight.valid_until >= date.today(),
                    ),
                )
                .order_by(ExecutionInsight.created_at.desc())
                .limit(20)
            )
        )
        execution = ExecutionService(self.session)
        requirement_memory = build_requirement_memory(self.session, goal.id, user_input)
        progress = execution.get_progress(goal.id)
        today = execution._today_for(goal)
        execution_days = {}
        for day, scheduled_date, task in self.session.execute(
            select(PlanDay, PlanDaySchedule.scheduled_date, PlanTask)
            .join(PlanDaySchedule, PlanDaySchedule.plan_day_id == PlanDay.id)
            .join(PlanActivation, PlanActivation.id == PlanDaySchedule.activation_id)
            .outerjoin(PlanTask, PlanTask.plan_day_id == PlanDay.id)
            .where(PlanActivation.plan_version_id == plan.id)
            .order_by(PlanDaySchedule.scheduled_date, PlanTask.order_in_day)
        ):
            entry = execution_days.setdefault(
                day.id,
                {
                    "week_index": day.week_index,
                    "day_index": day.day_index,
                    "scheduled_date": scheduled_date.isoformat(),
                    "primary_goal": day.primary_goal,
                    "tasks": [],
                },
            )
            if task:
                entry["tasks"].append({"id": task.id, "title": task.title,
                                       "description": task.description,
                                       "estimate_minutes": task.estimate_minutes, "status": task.status})
        visible_execution_days = [
            entry
            for entry in execution_days.values()
            if (today - timedelta(days=7)).isoformat()
            <= entry["scheduled_date"]
            <= (today + timedelta(days=14)).isoformat()
            or any(task["status"] in {"COMPLETED", "IN_PROGRESS"} for task in entry["tasks"])
        ]
        # Publishing is a separate plan conversation. Old execution replies
        # are suggestions, and cannot establish whether a preview was applied.
        from planner_api.plan_service import PlanService
        plan_conversation = self.session.scalar(select(Conversation).where(
            Conversation.goal_id == goal.id, Conversation.phase == "PLAN"))
        plan_messages = self._messages(plan_conversation.id, limit=30) if plan_conversation else []
        pending = PlanService._pending_adjustment(plan_messages)
        published = next((item for item in reversed(plan_messages)
                          if item.role == "ASSISTANT" and item.generation_status == "SUCCEEDED"
                          and item.metadata_json.get("adjustment_applied")), None)
        adjustment_state = {
            "authority": "CURRENT_DATABASE_STATE",
            "active_plan_id": plan.id, "active_plan_version": plan.version,
            "active_plan_revision": plan.revision,
            "has_pending_adjustment": pending is not None,
            "pending_adjustment": pending.model_dump(mode="json") if pending else None,
            "last_published_adjustment": ({"message_id": published.id, "content": published.content,
                "plan_revision": published.metadata_json.get("plan_revision"),
                "published_at": published.created_at.isoformat()} if published else None),
        }
        upcoming_tasks = [{"scheduled_date": day["scheduled_date"], **task}
                          for day in execution_days.values() if day["scheduled_date"] >= today.isoformat()
                          for task in day["tasks"]]
        pending_window = self.session.scalar(select(PlanWindow).where(
            PlanWindow.goal_id == goal.id, PlanWindow.status == "DRAFT",
            PlanWindow.trigger == "EXECUTION_FEEDBACK",
        ).order_by(PlanWindow.created_at.desc()).limit(1))
        exploration_state = {
            "authority": "CURRENT_ACTIVE_PLAN",
            "generated_through_week": min(plan.total_weeks, max(0, plan.next_unplanned_week - 1)),
            "next_unplanned_week": plan.next_unplanned_week if plan.next_unplanned_week <= plan.total_weeks else None,
            "route_fully_expanded": plan.next_unplanned_week > plan.total_weeks,
            "has_pending_window": pending_window is not None,
            "pending_window": ({"start_week": pending_window.start_week, "end_week": pending_window.end_week,
                                "status": "DRAFT_NOT_PUBLISHED"} if pending_window else None),
        } if plan.planning_mode == "EXPLORATORY" else None
        return PlanDiscussionContext(
            requirements=dict(requirement.snapshot_json or {}) if requirement else {},
            route={
                "summary": route.summary if route else "",
                "planning_mode": plan.planning_mode,
                "schedule_type": plan.schedule_type,
            },
            current_plan={
                "id": plan.id,
                "version": plan.version,
                "revision": plan.revision,
                "summary": plan.summary,
                "total_weeks": plan.total_weeks,
                "weekly_execution_days": plan.weekly_execution_days,
                "weekly_minutes": plan.weekly_minutes,
                "current_stage_order": progress.current_stage_order,
                "current_stage_title": progress.current_stage_title,
                "execution_days": visible_execution_days,
                "upcoming_task_index": upcoming_tasks[:168],
                "upcoming_task_index_truncated": len(upcoming_tasks) > 168,
                "adjustment_state": adjustment_state,
                "exploration_state": exploration_state,
            },
            user_input=user_input,
            research_context=research_context,
            material_context=material_context,
            planning_context={
                "current_date": today.isoformat(),
                "user_profile_memory": requirement_memory.get("user_profile_memory", []),
                "error_avoidance_memory": requirement_memory.get("error_avoidance_memory", []),
                "long_term_memory": requirement_memory.get("long_term_memory", []),
                "progress": progress.model_dump(mode="json"),
                "clarification_memory": {
                    "summary": clarification_summary.summary if clarification_summary else "",
                    "confirmed_conclusions": (
                        list(clarification_summary.confirmed_conclusions_json)
                        if clarification_summary
                        else []
                    ),
                    "open_questions": (
                        list(clarification_summary.open_questions_json)
                        if clarification_summary
                        else []
                    ),
                    # Preserve original wording for semantic reinterpretation.
                    # The rolling summary carries older context if a very long
                    # discussion has to be clipped for the model context window.
                    "messages": [
                        {"role": item.role, "content": item.content}
                        for item in clarification_messages[-60:]
                    ],
                    "message_count": len(clarification_messages),
                    "history_truncated": len(clarification_messages) > 60,
                },
                "confirmed_execution_insights": [
                    {
                        "id": item.id,
                        "memory_key": item.memory_key,
                        "type": item.insight_type,
                        "content": item.content,
                        "scope": item.scope,
                        "created_at": item.created_at.isoformat(),
                    }
                    for item in confirmed_insights
                ],
                "recent_execution_discussion": [
                    {"role": item.role, "content": item.content} for item in messages
                ],
            },
        )

    def _persist_insights(
        self,
        goal: Goal,
        plan: PlanVersion,
        user_message: Message,
        result: ExecutionDiscussionResult,
    ) -> None:
        for candidate in result.memory_candidates:
            current = self.session.scalar(
                select(ExecutionInsight)
                .where(
                    ExecutionInsight.goal_id == goal.id,
                    ExecutionInsight.memory_key == candidate.memory_key,
                    ExecutionInsight.scope == candidate.scope,
                    ExecutionInsight.status == "CONFIRMED",
                )
                .order_by(ExecutionInsight.created_at.desc())
            )
            if candidate.supersedes_id:
                previous = self.session.get(ExecutionInsight, candidate.supersedes_id)
                # Model-supplied references never authorize changing another
                # goal, scope, or an already superseded record.
                if (previous and previous.goal_id == goal.id and previous.scope == candidate.scope
                        and previous.status == "CONFIRMED"):
                    current = previous
                else:
                    continue
            explicit = self._is_explicit_candidate(user_message.content, candidate)
            if candidate.operation == "RETRACT":
                if current and explicit:
                    current.status = "SUPERSEDED"
                    current.decided_at = utc_now()
                continue
            if current and current.content == candidate.content:
                continue
            if current and explicit:
                current.status = "SUPERSEDED"
                current.decided_at = utc_now()
            self.session.add(
                ExecutionInsight(
                    goal_id=goal.id,
                    plan_version_id=plan.id,
                    source_message_id=user_message.id,
                    insight_type=candidate.insight_type,
                    content=candidate.content,
                    scope=candidate.scope,
                    confidence=candidate.confidence,
                    memory_key=current.memory_key if current else candidate.memory_key,
                    status=(
                        "CONFIRMED"
                        if self._is_explicit_candidate(user_message.content, candidate)
                        else "PROPOSED"
                    ),
                    source_type=(
                        "EXPLICIT_USER"
                        if self._is_explicit_candidate(user_message.content, candidate)
                        else candidate.source_type
                    ),
                    evidence_span=candidate.evidence_span,
                    confirmation_policy=candidate.confirmation_policy,
                    valid_from=date.today() if candidate.scope == "TEMPORARY" else None,
                    valid_until=(
                        date.today() + timedelta(days=candidate.valid_days or 14)
                        if candidate.scope == "TEMPORARY"
                        else None
                    ),
                    supersedes_id=current.id if current else None,
                    decided_at=(
                        utc_now()
                        if self._is_explicit_candidate(user_message.content, candidate)
                        else None
                    ),
                )
            )

    @staticmethod
    def _is_explicit_candidate(user_content: str, candidate) -> bool:  # type: ignore[no-untyped-def]
        evidence = (candidate.evidence_span or "").strip()
        return bool(
            candidate.source_type == "EXPLICIT_USER"
            and candidate.confirmation_policy in {"AUTO_CONFIRM_EXPLICIT", "TEMPORARY"}
            and evidence
            and evidence in user_content
        )

    def _persist_review(
        self,
        goal: Goal,
        plan: PlanVersion,
        user_message: Message,
        result: ExecutionDiscussionResult,
    ) -> None:
        if not result.review_signal.recommended or not result.review_signal.reason:
            return
        existing = self.session.scalar(
            select(ExecutionReview)
            .where(
                ExecutionReview.goal_id == goal.id,
                ExecutionReview.status.in_(["SCHEDULED", "SUGGESTED", "DISCUSSING"]),
            )
            .order_by(ExecutionReview.created_at.desc())
        )
        requested = any(item.type == "REVIEW_REQUEST" for item in result.intents)
        evidence = ExecutionService(self.session).get_progress(goal.id).model_dump(mode="json")
        future = result.review_signal.timing == "FUTURE"
        status = "SCHEDULED" if future else ("DISCUSSING" if requested else "SUGGESTED")
        if existing:
            existing.status = status
            existing.reason = result.review_signal.reason
            existing.evidence_json = evidence
            existing.source_message_id = user_message.id
            existing.not_before = result.review_signal.not_before if future else None
            existing.updated_at = utc_now()
            return
        self.session.add(
            ExecutionReview(
                goal_id=goal.id,
                plan_version_id=plan.id,
                source_message_id=user_message.id,
                trigger_type=result.review_signal.trigger_type,
                status=status,
                reason=result.review_signal.reason,
                evidence_json=evidence,
                not_before=result.review_signal.not_before if future else None,
            )
        )

    def _activate_due_reviews(self, goal_id: str) -> None:
        due = list(
            self.session.scalars(
                select(ExecutionReview).where(
                    ExecutionReview.goal_id == goal_id,
                    ExecutionReview.status == "SCHEDULED",
                    ExecutionReview.not_before.is_not(None),
                    ExecutionReview.not_before <= date.today(),
                )
            )
        )
        for review in due:
            review.status = "SUGGESTED"
            review.updated_at = utc_now()
        if due:
            self.session.commit()

    def _goal_and_plan(self, goal_id: str) -> tuple[Goal, PlanVersion]:
        goal = self.session.get(Goal, goal_id)
        if goal is None or not goal.active_plan_version_id:
            from fastapi import HTTPException

            raise HTTPException(404, "Goal or active plan not found")
        if goal.status not in {"ACTIVE", "RECOVERY_REQUIRED", "PAUSED", "COMPLETED", "ARCHIVED"}:
            from fastapi import HTTPException

            raise HTTPException(409, "Execution discussion is only available after the plan starts")
        plan = self.session.get(PlanVersion, goal.active_plan_version_id)
        if plan is None:
            from fastapi import HTTPException

            raise HTTPException(404, "Active plan not found")
        return goal, plan

    def _conversation(self, goal_id: str) -> Conversation | None:
        return self.session.scalar(
            select(Conversation).where(
                Conversation.goal_id == goal_id,
                Conversation.phase == "EXECUTION",
            )
        )

    def _ensure_conversation(self, goal_id: str) -> Conversation:
        conversation = self._conversation(goal_id)
        if conversation:
            return conversation
        conversation = Conversation(goal_id=goal_id, phase="EXECUTION")
        self.session.add(conversation)
        self.session.flush()
        return conversation

    def _messages(self, conversation_id: str, *, limit: int | None = None) -> list[Message]:
        query = (
            select(Message)
            .where(Message.conversation_id == conversation_id)
            .order_by(Message.sequence.desc())
            .limit(limit or get_settings().conversation_workspace_message_limit)
        )
        messages = list(self.session.scalars(query))
        messages.reverse()
        return messages

    def _next_sequence(self, conversation_id: str) -> int:
        return (
            int(
                self.session.scalar(
                    select(func.coalesce(func.max(Message.sequence), 0)).where(
                        Message.conversation_id == conversation_id
                    )
                )
                or 0
            )
            + 1
        )

    @staticmethod
    def _latest_analysis(messages: list[Message]) -> ExecutionDiscussionResult | None:
        for message in reversed(messages):
            raw = (message.metadata_json or {}).get("execution_analysis")
            if message.role == "ASSISTANT" and isinstance(raw, dict):
                try:
                    return ExecutionDiscussionResult.model_validate(raw)
                except ValueError:
                    return None
        return None

    @staticmethod
    def _message_read(message: Message) -> MessageRead:
        return MessageRead(
            id=message.id,
            sequence=message.sequence,
            role=message.role,
            content=message.content,
            generation_status=message.generation_status,
            metadata=dict(message.metadata_json or {}),
            created_at=message.created_at,
        )

    @staticmethod
    def _insight_read(insight: ExecutionInsight) -> ExecutionInsightRead:
        return ExecutionInsightRead(
            id=insight.id,
            insight_type=insight.insight_type,
            content=insight.content,
            scope=insight.scope,
            confidence=insight.confidence,
            status=insight.status,
            source_message_id=insight.source_message_id,
            created_at=insight.created_at,
            decided_at=insight.decided_at,
            memory_key=insight.memory_key,
            source_type=insight.source_type,
            evidence_span=insight.evidence_span,
            confirmation_policy=insight.confirmation_policy,
            valid_from=insight.valid_from,
            valid_until=insight.valid_until,
        )

    @staticmethod
    def _review_read(review: ExecutionReview) -> ExecutionReviewRead:
        return ExecutionReviewRead(
            id=review.id,
            trigger_type=review.trigger_type,
            status=review.status,
            reason=review.reason,
            evidence=dict(review.evidence_json or {}),
            summary=review.summary,
            decision=review.decision,
            created_at=review.created_at,
            updated_at=review.updated_at,
            not_before=review.not_before,
        )
