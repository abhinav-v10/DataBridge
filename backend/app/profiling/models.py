"""Result and option types for the profiling engine. Plain dataclasses, JSON-safe via to_dict()."""
import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Any

from app.connectors.types import CanonicalType

# Column-name rules for "counts only" treatment. Matched case-insensitively.
# SUBSTRINGS are matched against the name with all non-alphanumerics removed ("api_key" -> "apikey").
# TOKENS are matched only as whole words (split on punctuation and camelCase), to avoid
# flagging e.g. "shipping" for "pin".
DEFAULT_SENSITIVE_SUBSTRINGS = (
    "password", "passwd", "secret", "token", "apikey", "accesskey", "privatekey", "creditcard", "cardnumber",
)
DEFAULT_SENSITIVE_TOKENS = ("ssn", "pwd", "pin", "cvv", "cvc")


@dataclass(frozen=True)
class ProfileOptions:
    include_values: bool = False  # False = data-bearing metrics are never computed (redacted)
    exact_distinct_max_rows: int = 1_000_000  # above this, distribution metrics use a sample
    sample_size: int = 100_000
    sample_seed: int = 42  # used for PostgreSQL TABLESAMPLE ... REPEATABLE
    top_n: int = 10
    top_values_max_distinct: int = 1000  # top_values only for columns with at most this many distinct
    max_value_chars: int = 64  # string values in top_values are truncated to this length
    statement_timeout_ms: int = 30_000
    column_batch_size: int = 50  # columns per aggregate query
    sensitive_substrings: tuple[str, ...] = DEFAULT_SENSITIVE_SUBSTRINGS
    sensitive_tokens: tuple[str, ...] = DEFAULT_SENSITIVE_TOKENS

    def __post_init__(self):
        for name in ("exact_distinct_max_rows", "sample_size", "top_n", "top_values_max_distinct",
                     "max_value_chars", "statement_timeout_ms", "column_batch_size"):
            if getattr(self, name) < 1:
                raise ValueError(f"{name} must be >= 1")


def _jsonable(v: Any) -> Any:
    if v is None or isinstance(v, (bool, int, str)):
        return v
    if isinstance(v, float):
        return v if v == v and v not in (float("inf"), float("-inf")) else str(v)
    if isinstance(v, Decimal):
        return str(v)  # string keeps exact precision
    if isinstance(v, (datetime, date, time)):
        return v.isoformat()
    if isinstance(v, timedelta):
        return v.total_seconds()
    if isinstance(v, (list, tuple)):
        return [_jsonable(x) for x in v]
    if isinstance(v, dict):
        return {str(k): _jsonable(x) for k, x in v.items()}
    return str(v)


@dataclass(frozen=True)
class MetricValue:
    """One metric. `method` is "exact" (full scan) or "sampled". For sampled metrics `sample_size`
    is the number of rows the metric's query examined (for top_values: non-null rows)."""

    value: Any
    method: str = "exact"
    sample_size: int | None = None

    def to_dict(self) -> dict:
        return {"value": _jsonable(self.value), "method": self.method, "sample_size": self.sample_size}


@dataclass
class ColumnProfile:
    name: str
    canonical: CanonicalType
    sensitive: bool = False
    metrics: dict[str, MetricValue] = field(default_factory=dict)
    redacted: list[str] = field(default_factory=list)  # metrics deliberately NOT computed (privacy)
    skipped: dict[str, str] = field(default_factory=dict)  # metric -> reason (not applicable / too costly)
    errors: dict[str, str] = field(default_factory=dict)  # phase -> sanitised error message

    @property
    def status(self) -> str:
        if not self.errors:
            return "ok"
        return "partial" if self.metrics else "failed"

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "type": self.canonical.to_dict(),
            "status": self.status,
            "sensitive": self.sensitive,
            "metrics": {k: v.to_dict() for k, v in self.metrics.items()},
            "redacted": list(self.redacted),
            "skipped": dict(self.skipped),
            "errors": dict(self.errors),
        }


@dataclass
class TableProfile:
    table: str
    row_count: int
    include_values: bool
    primary_key: list[str] = field(default_factory=list)
    pk_duplicate_key_count: MetricValue | None = None
    pk_check_skipped: str | None = None
    pk_check_error: str | None = None
    columns: list[ColumnProfile] = field(default_factory=list)

    @property
    def column_count(self) -> int:
        return len(self.columns)

    def to_dict(self) -> dict:
        return {
            "table": self.table,
            "row_count": self.row_count,
            "column_count": self.column_count,
            "include_values": self.include_values,
            "primary_key": list(self.primary_key),
            "pk_duplicate_key_count": self.pk_duplicate_key_count.to_dict() if self.pk_duplicate_key_count else None,
            "pk_check_skipped": self.pk_check_skipped,
            "pk_check_error": self.pk_check_error,
            "columns": [c.to_dict() for c in self.columns],
        }


_CAMEL = re.compile(r"[^A-Za-z0-9]+|(?<=[a-z0-9])(?=[A-Z])")


def is_sensitive_column(name: str, opts: ProfileOptions) -> bool:
    squashed = re.sub(r"[^a-z0-9]", "", name.lower())
    if any(s in squashed for s in opts.sensitive_substrings):
        return True
    tokens = {t.lower() for t in _CAMEL.split(name) if t}
    return any(t in tokens for t in opts.sensitive_tokens)
