from typing import Literal

from pydantic import BaseModel, Field, field_validator

from .schemas import ResearchRunRead

ResponsePhase = Literal["CLARIFICATION", "ROUTE", "PLAN", "EXECUTION", "RECOVERY"]


class ResearchResponseCreate(BaseModel):
    question: str = Field(min_length=1, max_length=12000)
    phase: ResponsePhase
    intent: Literal["EXPLAIN", "REVISE", "MESSAGE"] = "MESSAGE"
    idempotency_key: str = Field(min_length=1, max_length=160)
    material_source_ids: list[str] = Field(default_factory=list, max_length=40)

    @field_validator("material_source_ids")
    @classmethod
    def bounded_source_ids(cls, values: list[str]) -> list[str]:
        if any(not value or len(value) > 36 for value in values):
            raise ValueError("Invalid material source ID")
        return list(dict.fromkeys(values))

    @field_validator("question")
    @classmethod
    def nonempty_question(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Question cannot be blank")
        return value.strip()


class ResearchResponseRead(BaseModel):
    id: str
    goal_id: str
    conversation_id: str
    phase: ResponsePhase
    intent: str
    question: str
    status: str
    fallback: bool
    attempt_count: int
    response_message_id: str | None
    error_message: str | None
    run: ResearchRunRead
