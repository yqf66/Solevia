from datetime import date as Date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class RecommendationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    date: Date | None = None
    max_items: int = Field(default=3, ge=1, le=3)
    use_ai: bool = False
    instruction: str = Field(default="", max_length=1000)


class RecommendationModelSelection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    task_id: str
    rationale: str = Field(min_length=1, max_length=500)

    @field_validator("rationale")
    @classmethod
    def nonempty_reason(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("推荐理由不能为空")
        return value.strip()


class RecommendationModelResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    selections: list[RecommendationModelSelection] = Field(default_factory=list, max_length=3)
    summary: str = Field(default="", max_length=500)


class RecommendationItem(BaseModel):
    task_id: str
    title: str
    scheduling_policy: str
    scheduled_date: Date
    estimate_minutes: int
    group_key: str | None = None
    eligible_from: Date | None = None
    eligible_to: Date | None = None
    remaining_value: int | None = None
    target_unit: str | None = None
    reason: str
    allowed_actions: list[Literal["ARRANGE_TODAY", "RESCHEDULE", "VIEW_DATE"]]


class RecommendationBlocked(BaseModel):
    task_id: str
    title: str
    scheduled_date: Date
    reason_code: str
    reason: str


class RecommendationCapacity(BaseModel):
    available_minutes: int
    current_task_minutes: int
    recommended_minutes: int
    spare_minutes: int
    remaining_minutes: int


class RecommendationPreview(BaseModel):
    id: str
    fingerprint: str
    date: Date
    strategy: Literal["RULES", "AI", "AI_FALLBACK"] = "RULES"
    summary: str = ""
    capacity: RecommendationCapacity
    items: list[RecommendationItem] = Field(default_factory=list)
    blocked_summary: list[RecommendationBlocked] = Field(default_factory=list)
    blocked_count: int = 0
    warnings: list[str] = Field(default_factory=list)
