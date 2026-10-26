from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy.exc import SQLAlchemyError

from planner_api.config import get_settings
from planner_api.database import SessionLocal
from planner_api.models import GenerationBatch, PlanGenerationJob, PlanningBrief, utc_now
from planner_api.plan_ai import build_plan_provider
from planner_api.plan_discussion_ai import build_plan_discussion_provider
from planner_api.plan_service import PlanService


def run_recovery_generation(goal_id: str) -> None:
    """Generate recovery weeks in the background so the request stays responsive."""
    try:
        with SessionLocal() as session:
            settings = get_settings()
            service = PlanService(
                session,
                build_plan_provider(settings),
                build_plan_discussion_provider(settings),
                enable_hybrid_work_items=settings.enable_hybrid_work_items,
                hybrid_rollout_mode=settings.hybrid_rollout_mode,
                hybrid_rollout_percentage=settings.hybrid_rollout_percentage,
            )
            latest = (
                session.query(PlanningBrief)
                .filter(
                    PlanningBrief.goal_id == goal_id,
                    PlanningBrief.brief_kind == "RECOVERY",
                )
                .order_by(PlanningBrief.version.desc())
                .first()
            )
            if latest is None:
                return
            batches = list(
                session.query(GenerationBatch)
                .filter(
                    GenerationBatch.goal_id == goal_id,
                    GenerationBatch.brief_id == latest.id,
                    GenerationBatch.scope_type == "RECOVERY_WEEK",
                    GenerationBatch.status == "QUEUED",
                )
                .order_by(GenerationBatch.created_at)
            )
            for batch in batches:
                try:
                    service.retry_recovery_batch(goal_id, batch.id)
                except Exception:
                    session.rollback()
    except SQLAlchemyError:
        return


def run_plan_generation(job_id: str) -> None:
    with SessionLocal() as session:
        job = session.get(PlanGenerationJob, job_id)
        if job is None or job.status != "QUEUED":
            return
        job.status = "RUNNING"
        job.started_at = utc_now()
        session.commit()

        settings = get_settings()
        service = PlanService(
            session,
            build_plan_provider(settings),
            build_plan_discussion_provider(settings),
            enable_hybrid_work_items=settings.enable_hybrid_work_items,
            hybrid_rollout_mode=settings.hybrid_rollout_mode,
            hybrid_rollout_percentage=settings.hybrid_rollout_percentage,
        )
        try:
            while True:
                session.refresh(job)
                workspace = service.get_workspace(job.goal_id)
                job.phase = workspace.generation_phase
                job.generated_weeks = workspace.generated_weeks
                job.total_weeks = workspace.total_weeks
                session.commit()

                if workspace.generation_phase == "COMPLETE":
                    job.status = "COMPLETED"
                    job.phase = "COMPLETE"
                    job.completed_at = utc_now()
                    session.commit()
                    return
                if workspace.generation_phase == "OUTLINE":
                    service.draft_plan(job.goal_id, job.mode)
                else:
                    service.draft_next_details(job.goal_id, job.mode)
        except HTTPException as error:
            detail = error.detail
            if isinstance(detail, dict):
                job.error_code = str(detail.get("provider_code") or detail.get("code") or "FAILED")
                job.error_message = str(detail.get("message") or "计划生成暂时中断。")
                job.diagnostic = str(detail.get("diagnostic") or "") or None
            else:
                job.error_code = "FAILED"
                job.error_message = str(detail)
        except Exception as error:  # pragma: no cover - final worker safety net
            job.error_code = "UNEXPECTED_ERROR"
            job.error_message = "后台生成意外中断，已完成内容仍然保留。"
            job.diagnostic = type(error).__name__
        job.status = "FAILED"
        job.completed_at = utc_now()
        session.commit()
