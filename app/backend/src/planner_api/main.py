from contextlib import asynccontextmanager

from fastapi import FastAPI, Depends
from planner_api.model_connection import require_model_connection
from pydantic import BaseModel, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware
from planner_api.local_security import LocalAPISecurity

from planner_api.api import router
from planner_api.config import get_settings
from planner_api.database import SessionLocal
from planner_api.materials.api import router as materials_router
from planner_api.materials.worker import MaterialProcessingWorker
from planner_api.model_telemetry import trace_scope
from planner_api.research.worker import ResearchWorker
from planner_api.research.response_api import router as research_response_router
from planner_api.knowledge.api import router as knowledge_router
from planner_api.knowledge.worker import KnowledgeWorker


class ResearchConnection(BaseModel):
    client_id: str = Field(min_length=8, max_length=80)
    enabled: bool
    sequence: int = Field(ge=0, le=9007199254740991)


def create_app(
    *, start_material_worker: bool = False, start_research_worker: bool = False,
    health_details: dict[str, str] | None = None,
    switchable_kernel: bool = False,
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_application):
        settings = get_settings()
        from planner_api.clarification_worker import ClarificationAnalysisWorker
        analysis_worker = ClarificationAnalysisWorker(SessionLocal) if start_research_worker else None
        if analysis_worker is not None:
            analysis_worker.start()
        from planner_api.research.readiness import ResearchReadiness
        if settings.llm_provider == "codex":
            from planner_api.codex_runtime.readiness import CodexResearchReadiness
            ResearchReadiness = CodexResearchReadiness

        # Explicit UI research also works while automatic web research is OFF.
        readiness = (ResearchReadiness(settings) if start_research_worker
                     and (settings.llm_provider == "codex" or settings.web_search_provider == "open_websearch") else None)
        if start_research_worker and switchable_kernel:
            from planner_api.kernel_readiness import KernelReadiness
            readiness = KernelReadiness()
        _application.state.research_readiness = readiness
        if readiness is not None:
            readiness.start()
        knowledge_worker = KnowledgeWorker(SessionLocal, settings) if start_research_worker and settings.knowledge_enabled else None
        if knowledge_worker is not None:
            knowledge_worker.start()
        encoder_warmup = None
        bge_warmup = None
        if settings.llm_provider != "codex" and (settings.material_rerank_mode == "GUARDED" or settings.research_rerank_mode == "BGE"):
            from planner_api.materials.reranking import prewarm_bge

            bge_warmup = prewarm_bge(settings.model_copy(update={"material_rerank_mode": "GUARDED"}))
        if settings.llm_provider != "codex" and settings.material_semantic_enabled and settings.material_semantic_prewarm:
            from planner_api.materials.semantic import prewarm_encoder

            encoder_warmup = prewarm_encoder(settings)
        worker = (
            MaterialProcessingWorker(SessionLocal)
            if start_material_worker and get_settings().material_processing_worker_enabled
            else None
        )
        research_worker = (
            ResearchWorker(SessionLocal, readiness=readiness)
            if start_research_worker and get_settings().research_worker_enabled
            else None
        )
        if worker is not None:
            worker.start()
        if research_worker is not None:
            research_worker.start()
        try:
            yield
        finally:
            if analysis_worker is not None:
                analysis_worker.stop()
            if readiness is not None:
                readiness.stop()
            if knowledge_worker is not None:
                knowledge_worker.stop()
            if bge_warmup is not None:
                bge_warmup.join(timeout=0.1)
            if encoder_warmup is not None:
                encoder_warmup.join(timeout=0.1)
            if worker is not None:
                worker.stop()
            if research_worker is not None:
                research_worker.stop()

    application = FastAPI(
        title="Solevia API",
        dependencies=[Depends(require_model_connection)],
        version="0.2.0",
        description="Solevia · 足下 — 千里之行，始于足下。",
        lifespan=lifespan,
    )

    from planner_api.kernel import KernelManager, router as kernel_router
    application.state.kernel = KernelManager(application)
    application.include_router(kernel_router)
    from planner_api.model_catalog import router as model_catalog_router
    application.include_router(model_catalog_router)
    from planner_api.capabilities import router as capabilities_router
    application.include_router(capabilities_router)

    @application.middleware("http")
    async def protect_kernel_switch(request, call_next):
        from fastapi import HTTPException
        from fastapi.responses import JSONResponse
        business = (request.method not in {"GET", "HEAD", "OPTIONS"}
                    and request.url.path.startswith("/api/")
                    and not request.url.path.startswith(("/api/settings/", "/api/session", "/api/codex/preferences", "/api/research/connection")))
        if not business:
            return await call_next(request)
        try:
            with application.state.kernel.business_request():
                return await call_next(request)
        except HTTPException as exc:
            return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})

    @application.middleware("http")
    async def correlate_model_nodes(request, call_next):
        with trace_scope():
            return await call_next(request)

    @application.get("/api/health", tags=["system"])
    def health() -> dict[str, str]:
        return {"status": "ok", **(health_details or {})}

    @application.get("/api/research/readiness", tags=["system"])
    def research_readiness():
        from planner_api.research.readiness import readiness_snapshot

        return readiness_snapshot(application)

    @application.post("/api/research/connection", tags=["system"])
    def research_connection(payload: ResearchConnection):
        from planner_api.research.readiness import readiness_snapshot

        manager = getattr(application.state, "research_readiness", None)
        if manager is not None:
            return manager.connection(payload.client_id, payload.enabled, payload.sequence)
        return readiness_snapshot(application)

    application.include_router(router)
    application.include_router(materials_router)
    application.include_router(research_response_router)
    application.include_router(knowledge_router)

    application.add_middleware(LocalAPISecurity)
    # Starlette strips the port before matching: hostnames, not wildcard ports.
    application.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost"], www_redirect=False)

    return application


app = create_app(start_material_worker=True, start_research_worker=True)
