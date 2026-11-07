from datetime import datetime, timezone

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class SchemaMapping(Base):
    """A saved source-table -> target-table column mapping.

    config = {"columns": [{"source_column", "target_column", "origin": "auto"|"manual", "confidence": float|None}],
              "min_confidence": float|None}
    """

    __tablename__ = "schema_mappings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True)
    source_connection_id: Mapped[int] = mapped_column(ForeignKey("connections.id", ondelete="CASCADE"), index=True)
    target_connection_id: Mapped[int] = mapped_column(ForeignKey("connections.id", ondelete="CASCADE"), index=True)
    source_table: Mapped[str] = mapped_column(String(255))
    target_table: Mapped[str] = mapped_column(String(255))
    key_columns: Mapped[list] = mapped_column(JSON, default=list)
    config: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
