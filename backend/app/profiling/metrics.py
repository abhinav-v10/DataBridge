"""Which metrics apply to which canonical Kind, and the SQLAlchemy expressions that compute them.

All SQL is built from SQLAlchemy expression constructs over reflected column objects; no user
text is ever interpolated into a statement.

Standard deviation
------------------
PostgreSQL uses the server's `stddev_samp`. SQLite has no stddev, so it is derived from two
aggregates: sample variance = (AVG(x*x) - AVG(x)^2) * n/(n-1). This one-pass formula suffers
catastrophic cancellation when the mean is large relative to the spread (e.g. values near 1e9
with spread ~1): digits are lost and the result can be off or even negative before clamping to 0.
It is acceptable for SQLite because SQLite is a test/dev target only. x*x may also overflow to
float for very large integers. Do not rely on SQLite stddev for precision-sensitive work.
"""
import math
from datetime import date, datetime, timezone

import sqlalchemy as sa
from sqlalchemy import case, func

from app.connectors.types import CanonicalType, Kind

NUMERIC_KINDS = {Kind.INTEGER, Kind.DECIMAL, Kind.FLOAT}
TEMPORAL_KINDS = {Kind.DATE, Kind.TIMESTAMP}
# Kinds with no reliable equality/ordering: only null counts are computed.
NO_DISTINCT_KINDS = {Kind.JSON, Kind.ARRAY, Kind.UNKNOWN}
TOP_VALUES_KINDS = {Kind.STRING, Kind.ENUM, Kind.INTEGER, Kind.UUID}

NULL_METRICS = ("null_count", "non_null_count", "null_pct")


def value_metric_names(kind: Kind) -> list[str]:
    """Metrics that expose actual data values; computed only when include_values=True."""
    names: list[str] = []
    if kind in NUMERIC_KINDS:
        names += ["min", "max", "mean", "sum", "stddev"]
    elif kind in TEMPORAL_KINDS:
        names += ["min", "max"]
    if kind in TOP_VALUES_KINDS:
        names.append("top_values")
    return names


def _count_where(cond):
    return func.coalesce(func.sum(case((cond, 1), else_=0)), 0)


def basic_expressions(col, canon: CanonicalType, dialect: str, include_values: bool) -> dict:
    """Full-scan, cheap aggregates for one column. Keys are metric names."""
    kind = canon.kind
    ex: dict = {"non_null": func.count(col)}
    if kind in NUMERIC_KINDS:
        ex["zero_count"] = _count_where(col == 0)
        ex["negative_count"] = _count_where(col < 0)
        if include_values:
            ex.update(min=func.min(col), max=func.max(col), mean=func.avg(col), sum=func.sum(col))
            if dialect == "postgresql":
                ex["stddev"] = func.stddev_samp(col)
            else:
                ex["_avg_sq"] = func.avg(col * col)  # see module docstring
    elif kind in TEMPORAL_KINDS:
        if kind is Kind.DATE:
            now = date.today()
        elif canon.timezone:
            now = datetime.now(timezone.utc)
        else:
            now = datetime.now(timezone.utc).replace(tzinfo=None)
        ex["future_count"] = _count_where(col > sa.literal(now, type_=col.type))
        if include_values:
            ex.update(min=func.min(col), max=func.max(col))
    elif kind is Kind.BOOLEAN:
        ex["true_count"] = _count_where(col == sa.true())
        ex["false_count"] = _count_where(col == sa.false())
    return ex


def distribution_expressions(col, kind: Kind) -> dict:
    """Metrics computed over the full table or a sample. Keys are metric names."""
    ex: dict = {"n": func.count(), "nn": func.count(col), "distinct_count": func.count(sa.distinct(col))}
    if kind is Kind.STRING:
        ln = func.length(col)
        ex.update(
            min_length=func.min(ln),
            max_length=func.max(ln),
            avg_length=func.avg(ln),
            empty_count=_count_where(col == ""),
            whitespace_padded_count=_count_where(col != func.trim(col)),
        )
    elif kind is Kind.BINARY:
        ln = func.length(col)
        ex.update(min_length=func.min(ln), max_length=func.max(ln))
    return ex


def sample_source(tbl, col_name: str, dialect: str, sample_size: int, row_count: int, seed: int):
    """Return (selectable, column) yielding roughly `sample_size` random rows of one column.

    PostgreSQL: TABLESAMPLE SYSTEM (block-level, fast; actual size varies, so callers measure it).
    Others (SQLite): ORDER BY random() LIMIT n; exact size, not reproducible.
    """
    if dialect == "postgresql":
        pct = min(100.0, max(0.0001, sample_size / row_count * 100.0))
        ts = tbl.tablesample(func.system(pct), seed=sa.literal_column(str(int(seed))))
        return ts, ts.c[col_name]
    sub = sa.select(tbl.c[col_name]).order_by(func.random()).limit(sample_size).subquery()
    return sub, sub.c[col_name]


def sample_stddev(avg, avg_sq, n) -> float | None:
    """Sample standard deviation from AVG(x) and AVG(x*x); see module docstring for limits."""
    if avg is None or avg_sq is None or n is None or n < 2:
        return None
    var = (float(avg_sq) - float(avg) ** 2) * n / (n - 1)
    return math.sqrt(var) if var > 0 else 0.0
