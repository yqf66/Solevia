from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy.exc import SQLAlchemyError

from planner_api.config import get_settings
from planner_api.database import SessionLocal
from planner_api.models import GenerationBatch, PlanGenerationJob, PlanningBrief, utc_now
from planner_api.plan_ai import build_plan_provider
from planner_api.plan_discussion_ai import build_plan_discussion_provider
from planner_api.plan_service import PlanService
from planner_api.ai import ClarificationProviderError
from planner_api.codex_runtime.failures import failure_for_code


def recover_interrupted_plan_generation(session_factory=SessionLocal) -> None:
    """Run once after migrations, before starting any workers in this process.

    A server restart must not leave old jobs 'running' forever or block kernel
    switching. Retain every result/checkpoint and let the user choose to resume.
    """
    message = "Solevia 服务已重新启动，上次生成已中断。已保存的输入和计划进度保留，可以继续补齐未完成部分。"
    with session_factory() as session:
        for job in session.query(PlanGenerationJob).filter(PlanGenerationJob.status.in_(['QUEUED', 'RUNNING'])):
            job.status = 'FAILED'
            job.error_code = 'GENERATION_INTERRUPTED'
            job.error_message = message
            job.completed_at = utc_now()
        for batch in session.query(GenerationBatch).filter(GenerationBatch.status.in_(['QUEUED', 'RUNNING'])):
            batch.status = 'FAILED'
            batch.failure_code = 'GENERATION_INTERRUPTED'
            batch.failure_message = message
        session.commit()


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
                except HTTPException as error:
                    session.rollback()
                    detail = error.detail if isinstance(error.detail, dict) else {}
                    failure = failure_for_code(detail.get('provider_code') or detail.get('code'))
                    if failure:
                        # Release the rest of this failed queue so users can
                        # switch kernels. Checkpoints and retry scopes survive.
                        for waiting in batches:
                            session.refresh(waiting)
                            if waiting.status in {'QUEUED', 'RUNNING'}:
                                waiting.status = 'FAILED'
                                waiting.failure_code = failure.code
                                waiting.failure_message = failure.message
                        session.commit()
                        break
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

        try:
            settings = get_settings()
            service = PlanService(
                session,
                build_plan_provider(settings),
                build_plan_discussion_provider(settings),
                enable_hybrid_work_items=settings.enable_hybrid_work_items,
                hybrid_rollout_mode=settings.hybrid_rollout_mode,
                hybrid_rollout_percentage=settings.hybrid_rollout_percentage,
            )
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
        except ClarificationProviderError as error:
            job.error_code = error.code
            job.error_message = str(error)
            job.diagnostic = error.diagnostic
        except Exception as error:  # pragma: no cover - final worker safety net
            job.error_code = "UNEXPECTED_ERROR"
            job.error_message = "后台生成意外中断，已完成内容仍然保留。"
            job.diagnostic = type(error).__name__
        job.status = "FAILED"
        job.completed_at = utc_now()
        session.commit()
