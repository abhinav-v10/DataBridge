from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class IssueOut(BaseModel):
    severity: str
    code: str
    message: str


class ColumnMappingOut(BaseModel):
    source_column: str
    target_column: str
    confidence: float
    name_score: float
    type_score: float
    structure_score: float
    type_compatibility: str
    status: str
    source_type: dict[str, Any]
    target_type: dict[str, Any]
    issues: list[IssueOut]


class UnmappedColumnOut(BaseModel):
    column: str
    type: dict[str, Any]
    nullable: bool
    is_primary_key: bool
    reason: str
    suggestion: dict[str, Any] | None = None
    issues: list[IssueOut]


class ComparisonSummary(BaseModel):
    source_columns: int
    target_columns: int
    mapped: int
    auto: int
    review: int
    conflict: int
    unmapped_source: int
    unmapped_target: int
    overall_confidence: float
    blocking_issues: int
    ready: bool


class ComparisonRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_connection_id: int
    source_table: str = Field(min_length=1, max_length=255)
    target_connection_id: int
    target_table: str = Field(min_length=1, max_length=255)
    min_confidence: float = Field(default=0.6, ge=0.0, le=1.0)


class ComparisonOut(BaseModel):
    source_table: str
    target_table: str
    min_confidence: float
    summary: ComparisonSummary
    mappings: list[ColumnMappingOut]
    unmapped_source: list[UnmappedColumnOut]
    unmapped_target: list[UnmappedColumnOut]
    issues: list[IssueOut]


class ColumnPair(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_column: str = Field(min_length=1, max_length=255)
    target_column: str = Field(min_length=1, max_length=255)


class MappingCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=100)
    source_connection_id: int
    source_table: str = Field(min_length=1, max_length=255)
    target_connection_id: int
    target_table: str = Field(min_length=1, max_length=255)
    # None = accept the automatic recommendation (excluding conflicts) at save time.
    columns: list[ColumnPair] | None = Field(default=None, max_length=2000)
    key_columns: list[str] = Field(default_factory=list, max_length=64)
    min_confidence: float = Field(default=0.6, ge=0.0, le=1.0)


class SavedColumn(BaseModel):
    source_column: str
    target_column: str
    origin: str  # "auto" | "manual"
    confidence: float | None = None


class MappingOut(BaseModel):
    id: int
    name: str
    source_connection_id: int
    target_connection_id: int
    source_table: str
    target_table: str
    key_columns: list[str]
    columns: list[SavedColumn]
    created_at: datetime


class ValidationOut(BaseModel):
    valid: bool
    errors: list[IssueOut]
    warnings: list[IssueOut]
    mappings: list[ColumnMappingOut]
