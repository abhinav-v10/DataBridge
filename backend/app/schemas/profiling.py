from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ProfileRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    connection_id: int
    table: str = Field(min_length=1, max_length=255)
    include_values: bool = False  # honoured only if the server enables DATABRIDGE_PROFILING_ALLOW_VALUES
    exact_distinct_max_rows: int | None = Field(default=None, ge=1, le=1_000_000_000)
    sample_size: int | None = Field(default=None, ge=1, le=10_000_000)
    top_n: int | None = Field(default=None, ge=1, le=100)
    statement_timeout_ms: int | None = Field(default=None, ge=100, le=600_000)


class MetricOut(BaseModel):
    value: Any = None
    method: str
    sample_size: int | None = None


class ColumnProfileOut(BaseModel):
    name: str
    type: dict[str, Any]
    status: str
    sensitive: bool
    metrics: dict[str, MetricOut]
    redacted: list[str]
    skipped: dict[str, str]
    errors: dict[str, str]


class ProfileRunSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    connection_id: int
    table_name: str
    status: str
    row_count: int | None
    column_count: int | None
    duration_ms: int
    created_at: datetime
    error: str | None = None


class ProfileRunOut(ProfileRunSummary):
    include_values_requested: bool
    values_in_response: bool  # True only on the POST that computed them; never on later GETs
    values_persisted: bool = False  # data values are never written to the metadata database
    options: dict[str, Any]
    primary_key: list[str]
    pk_duplicate_key_count: MetricOut | None = None
    pk_check_skipped: str | None = None
    pk_check_error: str | None = None
    columns: list[ColumnProfileOut]


class ProfileRunList(BaseModel):
    total: int
    items: list[ProfileRunSummary]
