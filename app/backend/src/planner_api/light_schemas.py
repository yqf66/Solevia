import re
from datetime import date, datetime
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from planner_api.schemas import ManualTaskCreate


class LightDraftCreate(BaseModel):
    raw_input: str = Field(min_length=1, max_length=12000)
    timezone: str = "Asia/Shanghai"
    start_date: date
    horizon_days: int = Field(default=7, ge=1, le=14)

    @field_validator("raw_input")
    @classmethod
    def input_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Please enter at least one task")
        return value.strip()

    @field_validator("timezone")
    @classmethod
    def valid_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as error:
            raise ValueError("Timezone must be a valid IANA timezone") from error
        return value


class LightTaskDraft(ManualTaskCreate):
    model_config = ConfigDict(extra="forbid")
    description: str = Field(default="", max_length=4000)
    flexible_recurrence: bool = True

    @model_validator(mode="after")
    def fixed_task_needs_date(self):
        if not self.title.strip():
            raise ValueError("Task title must not be blank")
        if self.start_local and self.scheduled_date is None:
            raise ValueError("Fixed tasks require a date")
        return self


class LightUnavailableWindow(BaseModel):
    model_config = ConfigDict(extra="forbid")
    date: date
    start_local: str
    end_local: str
    reason: str = Field(min_length=1, max_length=180)

    @field_validator("start_local", "end_local")
    @classmethod
    def valid_time(cls, value: str) -> str:
        if re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value) is None:
            raise ValueError("Time must use HH:MM")
        return value

    @model_validator(mode="after")
    def end_after_start(self):
        if self.end_local <= self.start_local:
            raise ValueError("Unavailable window end must be later than start")
        return self


class LightDraftResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1, max_length=160)
    tasks: list[LightTaskDraft] = Field(default_factory=list, max_length=30)
    assumptions: list[str] = Field(default_factory=list, max_length=12)
    questions: list[str] = Field(default_factory=list, max_length=6)
    unavailable_windows: list[LightUnavailableWindow] = Field(default_factory=list, max_length=30)
    full_planning_recommended: bool = False

    @field_validator("title")
    @classmethod
    def title_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Arrangement title must not be blank")
        return value.strip()


class LightDraftEdit(BaseModel):
    revision: int = Field(ge=1)
    result: LightDraftResult


class LightRevisionRequest(BaseModel):
    revision: int = Field(ge=1)


class LightDraftRead(BaseModel):
    id: str
    raw_input: str
    timezone: str
    start_date: date
    horizon_days: int
    status: Literal["DRAFT", "GENERATING", "READY", "FAILED", "APPLIED"]
    revision: int
    result: LightDraftResult
    error_message: str | None
    quick_plan_id: str | None
    manual_plan_id: str | None = None
    created_at: datetime
    updated_at: datetime
