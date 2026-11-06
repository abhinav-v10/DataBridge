from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class ConnectionCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    role: str = Field(pattern="^(source|target)$")
    db_type: str = "postgresql"
    host: str = ""
    port: int | None = Field(default=None, ge=1, le=65535)
    database: str = Field(min_length=1)
    username: str = ""
    password: str = ""
    db_schema: str | None = None


class ConnectionTestRequest(ConnectionCreate):
    """Test unsaved parameters; same shape as create."""


class ConnectionOut(BaseModel):
    """No password field exists here, so credentials can never leak through the API."""

    model_config = ConfigDict(from_attributes=True)
    id: int
    name: str
    role: str
    db_type: str
    host: str
    port: int | None
    database: str
    username: str
    db_schema: str | None
    created_at: datetime


class ConnectionStatus(BaseModel):
    ok: bool
    message: str


class ColumnOut(BaseModel):
    name: str
    data_type: str
    nullable: bool
    is_primary_key: bool
    max_length: int | None = None
    precision: int | None = None
    scale: int | None = None


class TableSchemaOut(BaseModel):
    name: str
    columns: list[ColumnOut]
    primary_key: list[str]
