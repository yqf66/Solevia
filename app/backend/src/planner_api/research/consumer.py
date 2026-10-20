from __future__ import annotations

from typing import Literal

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from planner_api.config import get_settings
from planner_api.models import Conversation, ConversationSummary, Goal, ResearchRun

from .context import compile_research_context
from .persistence import ResearchRunService
from .schemas import ResearchPacket
from .strategy import refresh_research_coverage

ResearchPhase = Literal["PLAN", "ROUTE", "EXECUTION", "CLARIFICATION", "RECOVERY"]


class ResearchContextService:
    """Shared, bounded consumer for research results across agent phases."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def prepare(
        self,
        goal_id: str,
        question: str,
        *,
        phase: ResearchPhase,
        enabled: bool = False,
        run_id: str | None = None,
        conversation_id: str | None = None,
        idempotency_key: str | None = None,
        decision_context: dict[str, object] | None = None,
        reuse_latest: bool = True,
    ) -> tuple[dict[str, object] | None, str | None, str | None]:
        """Return bounded context, run id and status.

        A supplied run must already be terminal. For backwards-compatible API
        callers, enabled=True without a run executes a persisted synchronous
        run; the frontend should normally enqueue first and poll asynchronously.
        Research is optional: failures do not prevent the phase response.
        """
        run: ResearchRun | None = None
        if run_id:
            run = self.session.get(ResearchRun, run_id)
            if run is None or run.goal_id != goal_id:
                raise HTTPException(status_code=404, detail="Research run not found")
            if run.status in {"QUEUED", "RUNNING", "CANCEL_REQUESTED"}:
                raise HTTPException(status_code=409, detail="Research run is not complete")
        elif not enabled:
            if reuse_latest:
                # Research is goal-scoped memory. Reuse the latest usable
                # packet so follow-up questions can build on prior evidence.
                run = self.session.scalar(
                    select(ResearchRun)
                    .where(
                        ResearchRun.goal_id == goal_id,
                        ResearchRun.status.in_({"READY", "PARTIAL", "COMPLETED"}),
                    )
                    .order_by(ResearchRun.updated_at.desc())
                    .limit(1)
                )
        else:
            try:
                run_read = ResearchRunService(self.session).execute(
                    goal_id,
                    question,
                    enabled=True,
                    conversation_id=conversation_id,
                    idempotency_key=idempotency_key,
                    decision_context=decision_context,
                )
                run = self.session.get(ResearchRun, run_read.id)
            except Exception:
                run = None

        if run is None:
            if (phase == "CLARIFICATION" and not enabled and not run_id and reuse_latest
                    and get_settings().knowledge_enabled):
                from planner_api.knowledge.search import cached_context, reuse_allowed
                goal = self.session.get(Goal, goal_id)
                memory = self._conversation_memory(conversation_id)
                goal_text = goal.raw_input if goal else ""
                inherited = "\n".join(str(v) for v in memory.get("hard_constraints", []))
                if reuse_allowed(question + "\n" + goal_text + "\n" + inherited):
                    anchor = (str(memory.get("summary", ""))[:250] + "\n" + goal_text[:400]).strip()
                    cached = cached_context(self.session, get_settings(), goal_id, question, phase=phase,
                                            anchor=anchor)
                    if cached is not None:
                        return cached, None, "PARTIAL"
            return None, None, "FAILED"
        payload = run.result_json or run.checkpoint_json or {}
        if not isinstance(payload, dict):
            return None, run.id, run.status
        try:
            packet = ResearchPacket.model_validate(payload)
        except (TypeError, ValueError):
            return None, run.id, run.status
        memory = self._conversation_memory(conversation_id)
        context = compile_research_context(
            packet,
            max_chars=min(12000, get_settings().context_max_input_chars // 3),
            phase="PLAN" if phase == "CLARIFICATION" else phase,
            focus=question,
            conversation_memory=memory,
        )
        if packet.cache_context:
            from planner_api.knowledge.integration import merge_context, reauthorize_cache
            cached = reauthorize_cache(self.session, get_settings(), goal_id, packet.cache_context, phase=phase)
            context = merge_context(context, cached,
                max_chars=min(12000, get_settings().context_max_input_chars // 3))
        return context, run.id, str(context.get("research_status", run.status))

    def _conversation_memory(self, conversation_id: str | None) -> dict[str, object]:
        if not conversation_id:
            return {}
        conversation = self.session.get(Conversation, conversation_id)
        if conversation is None:
            return {}
        summary = self.session.scalar(
            select(ConversationSummary)
            .where(ConversationSummary.conversation_id == conversation.id)
            .order_by(ConversationSummary.through_message_sequence.desc())
            .limit(1)
        )
        if summary is None:
            return {}
        return {
            "summary": summary.summary[:1200],
            "hard_constraints": list(summary.confirmed_conclusions_json or [])[-12:],
            "open_questions": list(summary.open_questions_json or [])[-8:],
        }


def research_summary_from_run(run: ResearchRun | None) -> dict[str, object] | None:
    """Build the bounded evidence manifest shown in every phase workspace."""
    if run is None:
        return None
    result = run.result_json or run.checkpoint_json or {}
    if not isinstance(result, dict):
        return None
    try:
        packet = ResearchPacket.model_validate(result)
        refresh_research_coverage(packet)
        result = packet.model_dump(mode="json")
    except (TypeError, ValueError):
        pass
    sources = result.get("sources", [])
    coverage = result.get("coverage", {})
    claims = result.get("claims", [])
    bounded_sources: list[dict[str, object]] = []
    for source in sources[:20] if isinstance(sources, list) else []:
        if not isinstance(source, dict):
            continue
        url = str(source.get("url") or source.get("canonical_url") or "").strip()
        if not url:
            continue
        bounded_sources.append(
            {
                "title": str(source.get("title", ""))[:180],
                "url": url[:2000],
                "read_status": str(source.get("read_status", ""))[:24],
                "retrieval_method": str(source.get("retrieval_method", ""))[:48],
                "evidence_level": str(source.get("evidence_level", "SEARCH_SNIPPET"))[:32],
                "snippet": str(source.get("snippet", ""))[:500],
                "excerpt": str(source.get("content_excerpt", ""))[:1000],
                "ranking_score": source.get("ranking_score", 0) or 0,
            }
        )
    bounded_claims: list[dict[str, object]] = []
    cached_sources = {}
    for evidence in (result.get("cache_context") or {}).get("selected_evidence", []):
        url = evidence.get("source_url", "")
        if url and url not in {s["url"] for s in bounded_sources}:
            cached_sources[url] = {"title": evidence.get("title", ""), "url": url,
                "read_status": "CACHED", "retrieval_method": "knowledge_cache",
                "evidence_level": "FULL_TEXT", "cached_at": evidence.get("cached_at"),
                "excerpt": evidence.get("quote", ""), "snippet": "", "ranking_score": 0}
    bounded_sources.extend(cached_sources.values())
    for claim in claims[:12] if isinstance(claims, list) else []:
        if not isinstance(claim, dict):
            continue
        bounded_claims.append(
            {
                "claim": str(claim.get("claim", ""))[:500],
                "status": str(claim.get("status", "UNVERIFIED"))[:32],
                "evidence_level": str(claim.get("evidence_level", "UNVERIFIED"))[:32],
                "source_urls": [str(url)[:2000] for url in claim.get("source_urls", [])[:4]],
            }
        )
    return {
        "id": run.id,
        "status": result.get("status", run.status) if run.status == "READY" else run.status,
        "current_stage": run.current_stage,
        "question": run.question[:500],
        "stop_reason": run.stop_reason,
        "error_code": run.error_code,
        "error_message": run.error_message,
        "result_status": result.get("status"),
        "source_count": len(sources) if isinstance(sources, list) else 0,
        "read_pages": int(coverage.get("read_pages", 0) or 0)
        if isinstance(coverage, dict)
        else 0,
        "unresolved_points": [str(item)[:300] for item in result.get("uncertain_points", [])[:10]]
        if isinstance(result.get("uncertain_points", []), list)
        else [],
        "sources": bounded_sources,
        "claims": bounded_claims,
        "coverage": coverage if isinstance(coverage, dict) else {},
        "knowledge_cache": (result.get("cache_context") or {}).get("knowledge_cache", {}),
    }

