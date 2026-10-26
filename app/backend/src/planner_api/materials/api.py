from typing import Annotated

from fastapi import APIRouter, Body, Depends, Query, Response
from sqlalchemy.orm import Session

from planner_api.database import get_db_session
from planner_api.config import get_settings

from .processing import MaterialProcessingService
from .analysis_ai import build_material_analysis_provider
from .analysis_service import MaterialAnalysisService
from .schemas import (
    GoalMaterialRead,
    MaterialAnalysisRead,
    MaterialAnalyzeRequest,
    MaterialFindingConfirmRequest,
    LocalMaterialReferenceCreate,
    MaterialRootCreate,
    MaterialRootRead,
    MaterialSearchHitRead,
    MaterialSearchRequest,
    MaterialRootScanRead,
    MaterialBatchReferenceCreate,
    MaterialBatchReferenceRead,
    MaterialRevisionRead,
    MaterialReaderRead,
    MaterialDiffRead,
)
from .service import MaterialService
from .reading import MaterialReadingService

router = APIRouter(prefix="/api", tags=["materials"])
SessionDependency = Annotated[Session, Depends(get_db_session)]


def get_material_service(session: SessionDependency) -> MaterialService:
    return MaterialService(session)


def get_material_processing_service(session: SessionDependency) -> MaterialProcessingService:
    return MaterialProcessingService(session)


def get_material_analysis_service(session: SessionDependency) -> MaterialAnalysisService:
    return MaterialAnalysisService(session, build_material_analysis_provider(get_settings()))


MaterialServiceDependency = Annotated[MaterialService, Depends(get_material_service)]
MaterialProcessingDependency = Annotated[
    MaterialProcessingService, Depends(get_material_processing_service)
]
MaterialAnalysisDependency = Annotated[
    MaterialAnalysisService, Depends(get_material_analysis_service)
]


@router.get(
    "/goals/{goal_id}/materials/{source_id}/revisions", response_model=list[MaterialRevisionRead]
)
def material_revisions(goal_id: str, source_id: str, session: SessionDependency):
    return MaterialReadingService(session).revisions(goal_id, source_id)


@router.get("/goals/{goal_id}/materials/{source_id}/read", response_model=MaterialReaderRead)
def read_material(
    goal_id: str,
    source_id: str,
    session: SessionDependency,
    revision_id: str | None = None,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=12000, ge=100, le=30000),
):
    return MaterialReadingService(session).read(goal_id, source_id, revision_id, offset, limit)


@router.get("/goals/{goal_id}/materials/{source_id}/diff", response_model=MaterialDiffRead)
def diff_material(
    goal_id: str,
    source_id: str,
    from_revision_id: str,
    to_revision_id: str,
    session: SessionDependency,
):
    return MaterialReadingService(session).compare(
        goal_id, source_id, from_revision_id, to_revision_id
    )


@router.post("/goals/{goal_id}/materials/{source_id}/semantic-index")
def index_material(goal_id: str, source_id: str, session: SessionDependency):
    return MaterialReadingService(session).index(goal_id, source_id)


@router.get("/material-roots", response_model=list[MaterialRootRead])
def list_material_roots(service: MaterialServiceDependency):
    return service.list_roots()


@router.post("/material-roots", response_model=MaterialRootRead, status_code=201)
def create_material_root(payload: MaterialRootCreate, service: MaterialServiceDependency):
    return service.create_root(payload)


@router.delete("/material-roots/{root_id}", status_code=204)
def disable_material_root(root_id: str, service: MaterialServiceDependency):
    service.disable_root(root_id)
    return Response(status_code=204)


@router.get("/goals/{goal_id}/materials", response_model=list[GoalMaterialRead])
def list_goal_materials(goal_id: str, service: MaterialServiceDependency):
    return service.list_goal_materials(goal_id)


@router.get("/goals/{goal_id}/materials/roots/{root_id}/scan", response_model=MaterialRootScanRead)
def scan_material_root(goal_id: str, root_id: str, service: MaterialServiceDependency):
    return service.scan_root(goal_id, root_id)


@router.post("/goals/{goal_id}/materials/local/batch", response_model=MaterialBatchReferenceRead)
def reference_local_materials(
    goal_id: str,
    payload: MaterialBatchReferenceCreate,
    service: MaterialServiceDependency,
):
    return service.reference_local_files(goal_id, payload)


@router.post(
    "/goals/{goal_id}/materials/local",
    response_model=GoalMaterialRead,
    status_code=201,
)
def reference_local_material(
    goal_id: str,
    payload: LocalMaterialReferenceCreate,
    service: MaterialServiceDependency,
):
    return service.reference_local_file(
        goal_id,
        root_id=payload.root_id,
        relative_path=payload.relative_path,
        scope=payload.scope,
        usage_policy=payload.usage_policy,
        pinned=payload.pinned,
    )


@router.post(
    "/goals/{goal_id}/materials/upload",
    response_model=GoalMaterialRead,
    status_code=201,
)
def upload_material(
    goal_id: str,
    service: MaterialServiceDependency,
    filename: str = Query(min_length=1, max_length=2000),
    content: bytes = Body(..., media_type="application/octet-stream"),
):
    return service.upload_file(goal_id, filename, content)


@router.post(
    "/goals/{goal_id}/materials/{source_id}/refresh",
    response_model=GoalMaterialRead,
)
def refresh_local_material(
    goal_id: str,
    source_id: str,
    service: MaterialServiceDependency,
):
    return service.refresh_local_file(goal_id, source_id)


@router.post(
    "/goals/{goal_id}/materials/{source_id}/process",
    response_model=GoalMaterialRead,
)
def process_local_material(
    goal_id: str,
    source_id: str,
    service: MaterialProcessingDependency,
    material_service: MaterialServiceDependency,
    pdf_parser: str | None = Query(default=None, pattern="^(AUTO|BASIC|MINERU)$"),
):
    service.process_for_goal(goal_id, source_id, pdf_parser)
    materials = material_service.list_goal_materials(goal_id)
    return next(item for item in materials if item.source_id == source_id)


@router.post(
    "/goals/{goal_id}/materials/{source_id}/process/cancel",
    response_model=GoalMaterialRead,
)
def cancel_material_processing(
    goal_id: str,
    source_id: str,
    service: MaterialProcessingDependency,
    material_service: MaterialServiceDependency,
):
    service.cancel_for_goal(goal_id, source_id)
    materials = material_service.list_goal_materials(goal_id)
    return next(item for item in materials if item.source_id == source_id)


@router.post(
    "/goals/{goal_id}/materials/search",
    response_model=list[MaterialSearchHitRead],
)
def search_goal_materials(
    goal_id: str,
    payload: MaterialSearchRequest,
    service: MaterialProcessingDependency,
):
    return service.search_goal_materials(goal_id, payload)


@router.get("/goals/{goal_id}/materials/analyses", response_model=list[MaterialAnalysisRead])
def list_material_analyses(goal_id: str, service: MaterialAnalysisDependency):
    return service.list_analyses(goal_id)


@router.post(
    "/goals/{goal_id}/materials/analyze",
    response_model=MaterialAnalysisRead,
    status_code=201,
)
def analyze_goal_materials(
    goal_id: str,
    payload: MaterialAnalyzeRequest,
    service: MaterialAnalysisDependency,
):
    return service.analyze(goal_id, payload)


@router.post(
    "/goals/{goal_id}/materials/analyses/{analysis_id}/confirm",
    response_model=MaterialAnalysisRead,
)
def confirm_material_findings(
    goal_id: str,
    analysis_id: str,
    payload: MaterialFindingConfirmRequest,
    service: MaterialAnalysisDependency,
):
    return service.confirm_findings(goal_id, analysis_id, payload)


@router.delete("/goals/{goal_id}/materials/{source_id}", status_code=204)
def unlink_goal_material(
    goal_id: str,
    source_id: str,
    service: MaterialServiceDependency,
):
    service.unlink_goal_material(goal_id, source_id)
    return Response(status_code=204)
