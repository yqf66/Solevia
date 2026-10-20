from datetime import timedelta
from dataclasses import asdict

from fastapi import HTTPException
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from planner_api.models import (
    Conversation, Goal, GoalMaterialLink, MaterialRevision, Message,
    PlanningBrief, RequirementVersion, ResearchResponseRequest, ResearchRun, utc_now,
)
from planner_api.config import get_settings

from .persistence import ResearchRunService
from .response_schemas import ResearchResponseCreate, ResearchResponseRead

ACTIVE = {"WAITING_RESEARCH", "QUEUED_ANSWER", "ANSWERING"}
RUN_ACTIVE = {"QUEUED", "RUNNING", "CANCEL_REQUESTED"}
USABLE = {"READY", "PARTIAL", "COMPLETED", "SKIPPED", "DISABLED"}


class ResearchResponseService:
    def __init__(self, session: Session) -> None:
        self.session = session

    def create(self, goal_id: str, payload: ResearchResponseCreate) -> ResearchResponseRead:
        goal = self.session.get(Goal, goal_id)
        if goal is None:
            raise HTTPException(404, "Goal not found")
        existing = self.session.scalar(select(ResearchResponseRequest).where(
            ResearchResponseRequest.idempotency_key == payload.idempotency_key,
        ))
        if existing:
            self._same_request(existing, goal_id, payload)
            return self.read(existing)
        pending = self.session.scalar(select(ResearchResponseRequest.id).where(
            ResearchResponseRequest.goal_id == goal_id,
            ResearchResponseRequest.status.in_(ACTIVE),
        ).limit(1))
        if pending:
            raise HTTPException(409, "该规划已有研究或回答正在进行，请先处理当前请求。")
        if payload.phase in {"PLAN", "ROUTE", "RECOVERY"} and payload.intent == "MESSAGE":
            raise HTTPException(422, "This phase requires EXPLAIN or REVISE")
        phase = "PLAN" if payload.phase == "RECOVERY" else payload.phase
        conversation = self.session.scalar(select(Conversation).where(
            Conversation.goal_id == goal_id, Conversation.phase == phase,
        ).order_by(Conversation.created_at.desc()).limit(1))
        if conversation is None:
            conversation = Conversation(goal_id=goal_id, phase=phase)
            self.session.add(conversation)
            self.session.flush()
        snapshot = self._snapshot(goal, conversation.id, payload.material_source_ids)
        requirement = self.session.get(RequirementVersion, goal.current_requirement_version_id) if goal.current_requirement_version_id else None
        facts = requirement.snapshot_json if requirement else {}
        snapshot["decision_context"] = {
            "phase": payload.phase, "goal": {"title": goal.title, "status": goal.status},
            "confirmed_requirements": {key: facts.get(key) for key in ("objective", "deadline", "available_capacity", "constraints")},
        }
        previous_runs = self.session.scalars(select(ResearchRun).where(
            ResearchRun.conversation_id == conversation.id,
            ResearchRun.question == payload.question,
            ResearchRun.status.in_({"READY", "PARTIAL", "COMPLETED"}),
        ).order_by(ResearchRun.created_at.desc()).limit(3)).all()
        if previous_runs:
            snapshot["decision_context"]["previous_read_urls"] = list(dict.fromkeys(
                source["url"] for previous in previous_runs
                for source in previous.result_json.get("sources", [])
                if source.get("read_status") == "SUCCEEDED" and source.get("url")
            ))[:6]
        if payload.phase == "RECOVERY":
            brief = self.session.scalar(select(PlanningBrief).where(
                PlanningBrief.goal_id == goal_id, PlanningBrief.brief_kind == "RECOVERY",
            ).order_by(PlanningBrief.version.desc()).limit(1))
            if brief:
                context = brief.brief_json.get("recovery_context", {})
                snapshot["decision_context"]["recovery"] = {
                    key: context.get(key) for key in (
                        "backlog_task_minutes", "local_capacity_minutes", "original_end_date", "recovery_scope", "locked_guarantees",
                    )
                }
        run = ResearchRun(
            goal_id=goal_id, question=payload.question, conversation_id=conversation.id,
            enabled=True, status="QUEUED", current_stage="QUEUED",
            idempotency_key=f"response:{payload.idempotency_key}",
            budget_json=asdict(ResearchRunService(self.session).orchestrator.limits),
        )
        self.session.add(run)
        try:
            self.session.flush()
            row = ResearchResponseRequest(
                goal_id=goal_id, run_id=run.id, conversation_id=conversation.id,
                phase=payload.phase, intent=payload.intent, question=payload.question,
                idempotency_key=payload.idempotency_key, snapshot_json=snapshot,
            )
            self.session.add(row)
            self.session.commit()
        except IntegrityError:
            self.session.rollback()
            row = self.session.scalar(select(ResearchResponseRequest).where(
                ResearchResponseRequest.idempotency_key == payload.idempotency_key,
            ))
            if row is None:
                raise HTTPException(409, "该规划已有研究或回答正在进行，请刷新查看。") from None
            self._same_request(row, goal_id, payload)
        return self.read(row)

    @staticmethod
    def _same_request(row, goal_id, payload) -> None:
        if (row.goal_id, row.phase, row.intent, row.question,
            sorted(row.snapshot_json.get("materials", {}))) != (
            goal_id, payload.phase, payload.intent, payload.question,
            sorted(set(payload.material_source_ids)),
        ):
            raise HTTPException(409, "Idempotency key belongs to a different request")

    def _snapshot(self, goal, conversation_id, sources) -> dict:
        materials = {}
        for source_id in dict.fromkeys(sources):
            link = self.session.scalar(select(GoalMaterialLink).where(
                GoalMaterialLink.goal_id == goal.id, GoalMaterialLink.source_id == source_id,
                GoalMaterialLink.status == "ACTIVE",
            ))
            if not link:
                raise HTTPException(404, "Material not linked to this goal")
            revision = self.session.scalar(select(MaterialRevision).where(
                MaterialRevision.source_id == source_id,
            ).order_by(MaterialRevision.revision.desc()).limit(1))
            if revision is None or revision.status != "READY":
                raise HTTPException(409, "Material is not ready")
            materials[source_id] = revision.id
        last_sequence = self.session.scalar(select(func.max(Message.sequence)).where(
            Message.conversation_id == conversation_id,
        )) or 0
        return {"requirement_id": goal.current_requirement_version_id,
                "route_id": goal.active_route_version_id,
                "plan_id": goal.active_plan_version_id,
                "goal_status": goal.status, "materials": materials,
                "message_sequence": last_sequence}

    def get(self, goal_id: str, request_id: str) -> ResearchResponseRead:
        return self.read(self._row(goal_id, request_id))

    def list(self, goal_id: str) -> list[ResearchResponseRead]:
        if self.session.get(Goal, goal_id) is None:
            raise HTTPException(404, "Goal not found")
        rows = self.session.scalars(select(ResearchResponseRequest).where(
            ResearchResponseRequest.goal_id == goal_id,
        ).order_by(ResearchResponseRequest.created_at.desc()).limit(30))
        return [self.read(row) for row in rows]

    def _row(self, goal_id, request_id):
        row = self.session.get(ResearchResponseRequest, request_id)
        if row is None or row.goal_id != goal_id:
            raise HTTPException(404, "Research response request not found")
        return row

    def read(self, row) -> ResearchResponseRead:
        return ResearchResponseRead(
            id=row.id, goal_id=row.goal_id, conversation_id=row.conversation_id,
            phase=row.phase, intent=row.intent, question=row.question, status=row.status,
            fallback=row.fallback, attempt_count=row.attempt_count,
            response_message_id=row.response_message_id, error_message=row.error_message,
            run=ResearchRunService(self.session).get(row.goal_id, row.run_id),
        )

    def action(self, goal_id: str, request_id: str, action: str) -> ResearchResponseRead:
        row = self._row(goal_id, request_id)
        if row.status in {"ANSWERING", "COMPLETED"}:
            raise HTTPException(409, "Response is already running or completed")
        if action == "retry-answer" and row.status not in {"ANSWER_FAILED", "INTERRUPTED"}:
            raise HTTPException(409, "There is no failed response to retry")
        if action == "retry-research" and row.status not in {"AWAITING_DECISION", "CANCELLED"}:
            raise HTTPException(409, "Research does not need a retry")
        if action == "retry-research":
            run = self.session.get(ResearchRun, row.run_id)
            effective_status = ResearchRunService(self.session).get(goal_id, row.run_id).status
            if effective_status not in {"FAILED", "PARTIAL", "INTERRUPTED", "CANCELLED"}:
                raise HTTPException(409, "Research is still stopping or does not need a retry")
            if run.recovery_count >= get_settings().research_max_recovery_attempts:
                raise HTTPException(409, "Research recovery limit reached")
        target = {"cancel": "CANCELLED", "ordinary": "QUEUED_ANSWER",
                  "retry-answer": "QUEUED_ANSWER", "retry-research": "WAITING_RESEARCH"}.get(action)
        if target is None:
            raise HTTPException(422, "Unknown research action")
        if target in ACTIVE:
            other = self.session.scalar(select(ResearchResponseRequest.id).where(
                ResearchResponseRequest.goal_id == goal_id, ResearchResponseRequest.id != row.id,
                ResearchResponseRequest.status.in_(ACTIVE),
            ).limit(1))
            if other:
                raise HTTPException(409, "该规划已有另一条研究或回答正在进行。")
        changed = self.session.execute(update(ResearchResponseRequest).where(
            ResearchResponseRequest.id == row.id, ResearchResponseRequest.status == row.status,
        ).values(status=target, fallback=True if action == "ordinary" else False if action == "retry-research" else row.fallback))
        if not changed.rowcount:
            self.session.rollback()
            raise HTTPException(409, "研究状态已经变化，请刷新后重试。")
        if action == "cancel":
            ResearchRunService(self.session).cancel(goal_id, row.run_id)
            row.status = "CANCELLED"
        elif action == "ordinary":
            ResearchRunService(self.session).cancel(goal_id, row.run_id)
            row.fallback = True
            row.status = "QUEUED_ANSWER"
        elif action == "retry-answer":
            row.status = "QUEUED_ANSWER"
        elif action == "retry-research":
            ResearchRunService(self.session).retry(goal_id, row.run_id)
            row.fallback = False
            row.status = "WAITING_RESEARCH"
        else:
            raise HTTPException(422, "Unknown research action")
        row.error_message = None
        self.session.commit()
        return self.read(row)

    def process_next(self) -> bool:
        now = utc_now()
        stale = list(self.session.scalars(select(ResearchResponseRequest).where(
            ResearchResponseRequest.status == "ANSWERING",
            ResearchResponseRequest.lease_expires_at < now,
        )))
        for row in stale:
            row.status = "INTERRUPTED"
            row.error_message = "回答生成中断，可复用本次研究重试。"
        if stale:
            self.session.commit()
        rows = list(self.session.scalars(select(ResearchResponseRequest).where(
            ResearchResponseRequest.status.in_({"WAITING_RESEARCH", "QUEUED_ANSWER"}),
        ).order_by(ResearchResponseRequest.created_at)))
        for row in rows:
            run = self.session.get(ResearchRun, row.run_id)
            if row.status == "WAITING_RESEARCH":
                if run.status in RUN_ACTIVE or run.status == "INTERRUPTED":
                    # The existing worker owns checkpoint recovery and its retry cap.
                    if run.status != "INTERRUPTED" or run.recovery_count < get_settings().research_max_recovery_attempts:
                        continue
                if run.status not in USABLE and run.status != "FAILED":
                    row.status = "CANCELLED" if run.status == "CANCELLED" else "AWAITING_DECISION"
                    row.error_message = "研究未完成，可重试研究或继续普通回答。"
                    self.session.commit()
                    return True
            claimed = self.session.execute(update(ResearchResponseRequest).where(
                ResearchResponseRequest.id == row.id,
                ResearchResponseRequest.status == row.status,
            ).values(status="ANSWERING", lease_expires_at=now + timedelta(minutes=20),
                     attempt_count=ResearchResponseRequest.attempt_count + 1))
            self.session.commit()
            if not claimed.rowcount:
                continue
            self.session.refresh(row)
            self._answer(row)
            return True
        return bool(stale)

    def _answer(self, row) -> None:
        try:
            goal = self.session.get(Goal, row.goal_id)
            expected = row.snapshot_json
            current = self._snapshot(goal, row.conversation_id, expected["materials"])
            # Only this exact in-flight exchange may follow the persisted baseline.
            tail = list(self.session.scalars(select(Message).where(
                Message.conversation_id == row.conversation_id,
                Message.sequence > expected["message_sequence"],
            ).order_by(Message.sequence)))
            matches = (not tail or (len(tail) <= 2 and tail[0].role == "USER"
                       and tail[0].content == row.question))
            completed = (matches and len(tail) == 2 and tail[-1].role == "ASSISTANT"
                         and tail[-1].generation_status == "SUCCEEDED"
                         and (tail[-1].metadata_json.get("research_run_id") == row.run_id
                              or tail[-1].metadata_json.get("research_response_request_id") == row.id))
            for key in ("requirement_id", "route_id", "plan_id", "materials", "goal_status"):
                if not completed and expected[key] != current[key] and not (
                    row.phase == "CLARIFICATION" and key in {"goal_status", "requirement_id"} and tail
                ):
                    raise HTTPException(409, "规划或附件版本已变化，请重新提交问题。")
            if not matches:
                raise HTTPException(409, "讨论已更新，请重新提交问题，避免覆盖后续回答。")
            if completed:
                result_message = tail[-1]
            else:
                self.session.commit()
                self.dispatch(row, regenerate=bool(tail))
                # Execution discussion releases its Session while waiting on the model.
                row = self.session.get(ResearchResponseRequest, row.id)
                result_message = self.session.scalar(select(Message).where(
                    Message.conversation_id == row.conversation_id, Message.role == "ASSISTANT",
                    Message.sequence > expected["message_sequence"],
                ).order_by(Message.sequence.desc()).limit(1))
                if result_message is None or result_message.generation_status != "SUCCEEDED":
                    raise RuntimeError("回答生成未完成；研究结果已保留，可直接重试回答。")
            row.response_message_id = result_message.id
            result_message.metadata_json = {**result_message.metadata_json,
                "research_response_request_id": row.id, "research_run_id": row.run_id,
                "research_fallback": row.fallback}
            row.status = "COMPLETED"
            row.error_message = None
        except Exception as error:
            self.session.rollback()
            row = self.session.get(ResearchResponseRequest, row.id)
            row.status = "ANSWER_FAILED"
            row.error_message = str(error.detail if isinstance(error, HTTPException) else error)[:1000]
        row.lease_expires_at = None
        self.session.commit()

    def dispatch(self, row, *, regenerate: bool):
        from planner_api.ai import build_clarification_provider
        from planner_api.config import get_settings
        from planner_api.execution_discussion_service import ExecutionDiscussionService
        from planner_api.plan_ai import build_plan_provider
        from planner_api.plan_discussion_ai import build_plan_discussion_provider
        from planner_api.plan_service import PlanService
        from planner_api.route_ai import build_route_provider
        from planner_api.route_explanation_ai import build_route_explanation_provider
        from planner_api.route_service import RouteService
        from planner_api.service import ClarificationService

        settings = get_settings()
        options = {"research_run_id": None if row.fallback else row.run_id,
                   "use_saved_research": not row.fallback}
        sources = list(row.snapshot_json["materials"])
        if row.phase == "CLARIFICATION":
            return ClarificationService(self.session, build_clarification_provider(settings), defer_analysis=True).add_user_message(
                row.goal_id, row.question, **options, reuse_latest_assistant=regenerate,
                material_source_ids=sources,
            )
        if row.phase == "EXECUTION":
            return ExecutionDiscussionService(self.session, build_plan_discussion_provider(settings)).discuss(
                row.goal_id, row.question, **options, regenerate=regenerate, material_source_ids=sources,
            )
        if row.phase == "ROUTE":
            service = RouteService(self.session, build_route_provider(settings), build_route_explanation_provider(settings))
            method = service.explain_route if row.intent == "EXPLAIN" else service.revise_route
        else:
            service = PlanService(self.session, build_plan_provider(settings), build_plan_discussion_provider(settings),
                                  enable_hybrid_work_items=settings.enable_hybrid_work_items,
                                  hybrid_rollout_mode=settings.hybrid_rollout_mode,
                                  hybrid_rollout_percentage=settings.hybrid_rollout_percentage)
            method = service.explain_plan if row.intent == "EXPLAIN" else service.revise_plan
        return method(row.goal_id, row.question, **options, regenerate=regenerate)
