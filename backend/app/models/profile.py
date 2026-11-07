from datetime import datetime, timezone

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base


def _now() -> datetime:
    return datetime.now(timezone.utc)


class ProfileRun(Base):
    """One profiling execution for one table. Only redacted results are persisted (see profiling_service)."""

    __tablename__ = "profile_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    connection_id: Mapped[int] = mapped_column(ForeignKey("connections.id", ondelete="CASCADE"), index=True)
    table_name: Mapped[str] = mapped_column(String(255), index=True)
    status: Mapped[str] = mapped_column(String(10))  # "completed" | "failed"
    include_values_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    options: Mapped[dict] = mapped_column(JSON, default=dict)
    row_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    column_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    primary_key: Mapped[list] = mapped_column(JSON, default=list)
    table_metrics: Mapped[dict] = mapped_column(JSON, default=dict)  # pk duplicate check result/skip/error
    error: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    columns: Mapped[list["ProfileColumnResult"]] = relationship(
        order_by="ProfileColumnResult.position", cascade="all, delete-orphan"
    )


class ProfileColumnResult(Base):
    __tablename__ = "profile_column_results"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("profile_runs.id", ondelete="CASCADE"), index=True)
    position: Mapped[int] = mapped_column(Integer)
    column_name: Mapped[str] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(10))
    sensitive: Mapped[bool] = mapped_column(Boolean, default=False)
    canonical: Mapped[dict] = mapped_column(JSON)
    metrics: Mapped[dict] = mapped_column(JSON, default=dict)
    redacted: Mapped[list] = mapped_column(JSON, default=list)
    skipped: Mapped[dict] = mapped_column(JSON, default=dict)
    errors: Mapped[dict] = mapped_column(JSON, default=dict)
