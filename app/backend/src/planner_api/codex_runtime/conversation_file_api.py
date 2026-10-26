from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

router = APIRouter(prefix="/api/codex/conversation-files", tags=["conversation-files"])


class FileDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decision: Literal["ALLOW", "DENY"]
    reason: str = Field(default="", max_length=1000)


def checked(action):
    try:
        return action()
    except (ValueError, OSError, HTTPException) as error:
        raise HTTPException(
            409,
            str(error)
            if isinstance(error, ValueError)
            else "文件或对应资料暂时不可访问，请重新选择文件。",
        ) from error


@router.get("/answers/{message_id}")
def answer_files(message_id: str, request: Request):
    return checked(lambda: request.app.state.conversation_files.for_answer(message_id))


@router.post("/{goal_id}/{task_id}/decision")
def decide(goal_id: str, task_id: str, body: FileDecision, request: Request):
    manager = getattr(request.app.state, "kernel", None)
    if body.decision == "ALLOW" and manager and manager.snapshot()["mode"] != "codex":
        raise HTTPException(409, "请先切换到 Codex 内核，再确认生成文件修改版。")
    return checked(
        lambda: request.app.state.conversation_files.decide(
            goal_id, task_id, body.decision, body.reason
        )
    )


@router.get("/{goal_id}/{task_id}/files/{index}")
def file_content(goal_id: str, task_id: str, index: int, request: Request):
    return checked(lambda: request.app.state.conversation_files.content(goal_id, task_id, index))
