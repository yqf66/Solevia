from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator

type MaterialScope = Literal["CLARIFICATION", "ROUTE", "PLAN", "EXECUTION", "ALL"]
type MaterialUsagePolicy = Literal["AUTO", "MANUAL_ONLY"]


class MaterialRootCreate(BaseModel):
    display_name: str = Field(min_length=1, max_length=160)
    path: str = Field(min_length=1, max_length=2000)

    @field_validator("display_name", "path")
    @classmethod
    def strip_text(cls, value: str) -> str:
        return value.strip()


class MaterialRootRead(BaseModel):
    id: str
    display_name: str
    path: str
    status: str
    created_at: datetime
    updated_at: datetime


class MaterialScanFileRead(BaseModel):
    relative_path: str
    title: str
    extension: str
    file_size: int
    modified_at: datetime
    supported: bool
    reason: str | None = None
    linked: bool = False
    source_id: str | None = None
    revision_status: str | None = None


class MaterialRootScanRead(BaseModel):
    root_id: str
    scanned_at: datetime
    truncated: bool = False
    files: list[MaterialScanFileRead] = Field(default_factory=list)


class MaterialBatchReferenceCreate(BaseModel):
    root_id: str = Field(min_length=1, max_length=36)
    relative_paths: list[str] = Field(min_length=1, max_length=500)
    scope: MaterialScope = "ALL"
    usage_policy: MaterialUsagePolicy = "MANUAL_ONLY"
    pinned: bool = False

    @field_validator("relative_paths")
    @classmethod
    def normalize_relative_paths(cls, values: list[str]) -> list[str]:
        normalized = list(dict.fromkeys(value.strip() for value in values if value.strip()))
        if not normalized:
            raise ValueError("relative_paths cannot be empty")
        return normalized


class MaterialBatchReferenceError(BaseModel):
    relative_path: str
    message: str


class MaterialBatchReferenceRead(BaseModel):
    imported: list["GoalMaterialRead"] = Field(default_factory=list)
    errors: list[MaterialBatchReferenceError] = Field(default_factory=list)


class LocalMaterialReferenceCreate(BaseModel):
    root_id: str = Field(min_length=1, max_length=36)
    relative_path: str = Field(min_length=1, max_length=2000)
    scope: MaterialScope = "ALL"
    usage_policy: MaterialUsagePolicy = "AUTO"
    pinned: bool = False

    @field_validator("relative_path")
    @classmethod
    def normalize_relative_path(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("Relative path cannot be blank")
        return normalized


class MaterialProcessingStatusRead(BaseModel):
    status: str
    attempts: int
    queued_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None
    queue_wait_ms: int | None = None
    processing_duration_ms: int | None = None
    cancellation_requested: bool = False


class MaterialRevisionRead(BaseModel):
    id: str
    revision: int
    content_hash: str
    file_size: int
    source_modified_at: datetime | None = None
    parser_name: str
    parser_version: str | None = None
    status: str
    metadata: dict = Field(default_factory=dict)
    processing: MaterialProcessingStatusRead | None = None
    error_code: str | None = None
    error_message: str | None = None
    created_at: datetime
    completed_at: datetime | None = None


class GoalMaterialRead(BaseModel):
    link_id: str
    source_id: str
    kind: str
    title: str
    mime_type: str | None = None
    root_id: str | None = None
    relative_path: str | None = None
    canonical_uri: str
    source_status: str
    scope: MaterialScope
    usage_policy: MaterialUsagePolicy
    pinned: bool
    link_status: str
    latest_revision: MaterialRevisionRead | None = None


class MaterialSearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=1000)
    max_chunks: int = Field(default=8, ge=1, le=30)
    max_chars: int = Field(default=1800, ge=200, le=8000)
    max_total_chars: int = Field(default=12000, ge=500, le=30000)
    source_ids: list[str] | None = Field(default=None, max_length=500)
    diversify_sources: bool = False

    @field_validator("query")
    @classmethod
    def strip_query(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("query cannot be blank")
        return value


class MaterialSearchHitRead(BaseModel):
    source_id: str
    revision_id: str
    revision: int
    sequence: int
    section_title: str = ""
    text: str
    score: float
    locator: str
    locator_details: list[dict[str, object]] = Field(default_factory=list)
    retrieval_method: str = "KEYWORD"


class MaterialChunkRead(BaseModel):
    revision_id: str
    sequence: int
    section_title: str = ""
    text: str
    char_start: int
    char_end: int


class MaterialAnalyzeRequest(BaseModel):
    query: str = Field(min_length=2, max_length=2000)
    max_chunks: int = Field(default=8, ge=1, le=12)
    max_total_chars: int = Field(default=12000, ge=1000, le=30000)
    source_ids: list[str] | None = Field(default=None, max_length=500)

    @field_validator("query")
    @classmethod
    def strip_query(cls, value: str) -> str:
        value = value.strip()
        if len(value) < 2:
            raise ValueError("analysis query cannot be blank")
        return value


class MaterialCitationRead(BaseModel):
    source_id: str
    revision_id: str
    sequence: int
    locator: str
    section_title: str = ""
    source_title: str = ""
    relative_path: str = ""


class MaterialKeyPointRead(BaseModel):
    title: str
    detail: str
    citations: list[MaterialCitationRead] = Field(default_factory=list)


class MaterialFindingRead(BaseModel):
    text: str
    category: str = "OTHER"
    confidence: float = Field(default=0.5, ge=0, le=1)
    status: str = "PROPOSED"
    citations: list[MaterialCitationRead] = Field(default_factory=list)


class MaterialConflictRead(BaseModel):
    description: str
    citations: list[MaterialCitationRead] = Field(default_factory=list)


class MaterialQueryCoverageRead(BaseModel):
    query: str
    retrieved_chunks: int = 0


class MaterialAnalysisRead(BaseModel):
    id: str
    goal_id: str
    query: str
    status: str
    summary: str
    key_points: list[MaterialKeyPointRead] = Field(default_factory=list)
    findings: list[MaterialFindingRead] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)
    source_count: int = 0
    file_count: int = 0
    conflicts: list[MaterialConflictRead] = Field(default_factory=list)
    retrieval_coverage: list[MaterialQueryCoverageRead] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime


class MaterialFindingConfirmRequest(BaseModel):
    finding_indexes: list[int] = Field(min_length=1, max_length=30)


class MaterialReaderRead(BaseModel):
    source_id: str
    title: str
    revision: MaterialRevisionRead
    offset: int
    next_offset: int | None
    total_chars: int
    text: str
    outline: list[dict] = Field(default_factory=list)
    outline_truncated: bool = False
    warnings: list[str] = Field(default_factory=list)


class MaterialDiffRead(BaseModel):
    source_id: str
    from_revision_id: str
    to_revision_id: str
    lines: list[dict] = Field(default_factory=list)
    added_lines: int
    removed_lines: int
    truncated: bool
    compared_chars: list[int]
    warning: str = "变化不会自动修改已确认事实，请核实后再调整规划。"
