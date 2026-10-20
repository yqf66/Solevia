from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from planner_api.database import get_db_session

from .response_schemas import ResearchResponseCreate, ResearchResponseRead
from .responses import ResearchResponseService

router = APIRouter(prefix="/api/goals/{goal_id}/research/requests", tags=["research"])
SessionDependency = Annotated[Session, Depends(get_db_session)]


@router.post("", response_model=ResearchResponseRead, status_code=202)
def create(goal_id: str, payload: ResearchResponseCreate, session: SessionDependency, request: Request):
    from .readiness import readiness_snapshot

    manager = getattr(request.app.state, "research_readiness", None)
    if manager is not None and hasattr(manager, "request_use"):
        manager.request_use()
    if not readiness_snapshot(request.app)["ready"]:
        raise HTTPException(503, "联网组件正在准备，请稍候重试。", headers={"Retry-After": "2"})
    return ResearchResponseService(session).create(goal_id, payload)


@router.get("", response_model=list[ResearchResponseRead])
def list_requests(goal_id: str, session: SessionDependency):
    return ResearchResponseService(session).list(goal_id)


@router.get("/{request_id}", response_model=ResearchResponseRead)
def get(goal_id: str, request_id: str, session: SessionDependency):
    return ResearchResponseService(session).get(goal_id, request_id)


@router.post("/{request_id}/{action}", response_model=ResearchResponseRead)
def act(goal_id: str, request_id: str,
        action: Literal["cancel", "ordinary", "retry-answer", "retry-research"], session: SessionDependency):
    return ResearchResponseService(session).action(goal_id, request_id, action)
