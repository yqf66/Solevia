from __future__ import annotations

import asyncio
import json
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from .store import TERMINAL
from .preferences import CodexPreferenceUpdate, read_preferences, save_preferences


class MessageInput(BaseModel):
    content: str = Field(min_length=1, max_length=20000)
    client_request_id: str = Field(min_length=8, max_length=120)


class RequestAnswer(BaseModel):
    decision: str | None = None
    answers: dict[str, dict[str, list[str]]] | None = None


router = APIRouter(prefix="/api/codex")


@router.get('/preferences')
def preferences(request: Request):
    return read_preferences(request.app.state.codex_business)


@router.put('/preferences')
def update_preferences(request: Request, body: CodexPreferenceUpdate):
    return save_preferences(request.app.state.codex_business, body)


@router.get('/generation-status')
def generation_status(request: Request, goal_id: str | None = None):
    result = request.app.state.codex_business.generation_status()
    if goal_id:
        from sqlalchemy import select
        from planner_api.database import SessionLocal
        from planner_api.models import GenerationBatch
        with SessionLocal() as session:
            batch = session.scalar(select(GenerationBatch).where(
                GenerationBatch.goal_id == goal_id,
                GenerationBatch.scope_type == 'HYBRID_ADJUSTMENT_PREVIEW',
            ).order_by(GenerationBatch.updated_at.desc()).limit(1))
            if batch:
                checkpoint = batch.staged_result_json or {}
                result['plan_checkpoint'] = {'status': batch.status,
                    'completed_weeks': sum(bool(row) for row in checkpoint.get('details', [])),
                    'total_weeks': (checkpoint.get('outline') or {}).get('total_weeks')}
    return result


@router.get("/status")
async def status(request: Request) -> dict[str, Any]:
    return request.app.state.codex.status()


@router.get("/sessions")
async def sessions(request: Request) -> dict[str, Any]:
    return {"sessions": request.app.state.codex.store.sessions()}


@router.post("/sessions")
async def create_session(request: Request) -> dict[str, Any]:
    return request.app.state.codex.store.create_session()


@router.get("/sessions/{session_id}")
async def session(request: Request, session_id: str) -> dict[str, Any]:
    return request.app.state.codex.store.snapshot(session_id)


@router.post("/sessions/{session_id}/messages")
async def message(request: Request, session_id: str, body: MessageInput) -> dict[str, Any]:
    return request.app.state.codex.send(session_id, body.content, body.client_request_id)


@router.get("/runs/{run_id}/events")
async def events(run_id: str, request: Request, after: int = 0) -> StreamingResponse:
    service = request.app.state.codex
    service.store.get_run(run_id)

    async def stream():
        cursor = max(after, 0)
        while not await request.is_disconnected():
            rows = service.store.events(run_id, cursor)
            for row in rows:
                cursor = row["id"]
                yield f"id: {cursor}\nevent: {row['type']}\ndata: {json.dumps(row['payload'], ensure_ascii=False)}\n\n"
            if any(row["type"] == "run_completed" for row in rows):
                break
            if not rows and service.store.get_run(run_id)["status"] in TERMINAL:
                break
            if not rows:
                yield ": keepalive\n\n"
            await asyncio.sleep(0.2)

    return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.post("/runs/{run_id}/cancel")
async def cancel(run_id: str, request: Request) -> dict[str, str]:
    await asyncio.to_thread(request.app.state.codex.cancel, run_id)
    return {"status": "cancel_requested"}


@router.post("/requests/{request_id}/respond")
async def respond(request_id: str, request: Request, body: RequestAnswer) -> dict[str, str]:
    await asyncio.to_thread(request.app.state.codex.respond, request_id, body.model_dump(exclude_none=True))
    return {"status": "answered"}
