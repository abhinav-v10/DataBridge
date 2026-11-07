"""Tier-1 profiling engine.

profile_table(connector, table, options) -> TableProfile

Flow: reflect columns via the connector (ColumnInfo.sa_type) -> normalize_type() per column ->
phase 1 (full-scan aggregates, batched, falling back to per-column on failure) -> phase 2
(distribution metrics per column, sampled on large tables) -> optional PK duplicate check.
Failures are isolated per column and recorded as sanitised messages; they never contain row values.
"""
import time
from dataclasses import dataclass, field

import sqlalchemy as sa
from sqlalchemy import func

from app.connectors.base import BaseConnector, ColumnInfo, ConnectorError
from app.connectors.types import CanonicalType, Kind, normalize_type
from app.profiling import metrics as M
from app.profiling.models import (
    ColumnProfile,
    MetricValue,
    ProfileOptions,
    TableProfile,
    is_sensitive_column,
)


class ProfilingQueryError(Exception):
    """A profiling query failed. The message is sanitised: no SQL parameters, no row values."""


def sanitize_error(e: BaseException) -> str:
    orig = getattr(e, "orig", None)
    if orig is not None:
        code = getattr(orig, "pgcode", None)
        if code == "57014" or "interrupted" in str(orig).lower():
            return "query timed out"
        return f"database error {type(orig).__name__}" + (f" (sqlstate {code})" if code else "")
    # Never include str(e): e.g. a ValueError from parsing a bad date quotes the offending value.
    return f"{type(e).__name__} while reading results"


class _Executor:
    """Runs one statement per connection, read-only where the database supports it, with a timeout."""

    def __init__(self, engine, timeout_ms: int):
        self.engine = engine
        self.dialect = engine.dialect.name
        self.timeout_ms = timeout_ms

    def run(self, stmt) -> list:
        raw = None
        try:
            with self.engine.connect() as conn:
                if self.dialect == "postgresql":
                    conn.execute(sa.text("SET TRANSACTION READ ONLY"))
                    conn.execute(
                        sa.text("SELECT set_config('statement_timeout', :ms, true)"), {"ms": str(self.timeout_ms)}
                    )
                elif self.dialect == "sqlite":
                    raw = conn.connection.dbapi_connection
                    deadline = time.monotonic() + self.timeout_ms / 1000
                    raw.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, 1000)
                try:
                    return list(conn.execute(stmt).mappings().all())
                finally:
                    if raw is not None:
                        raw.set_progress_handler(None, 0)
        except Exception as e:
            err = ProfilingQueryError(sanitize_error(e))
            err.__suppress_context__ = True
            raise err from None


@dataclass
class _Ctx:
    idx: int
    info: ColumnInfo
    col: sa.ColumnClause
    canon: CanonicalType
    profile: ColumnProfile
    rows: int = 0
    null_count: int = 0
    distinct: int | None = None
    distinct_method: tuple[str, int | None] = ("exact", None)
    extra: dict = field(default_factory=dict)


def profile_table(connector: BaseConnector, table: str, options: ProfileOptions | None = None) -> TableProfile:
    opts = options or ProfileOptions()
    schema = connector.get_table_schema(table)  # raises ConnectorError if the table is missing
    engine = connector.engine()
    ex = _Executor(engine, opts.statement_timeout_ms)
    dialect = ex.dialect

    ctxs: list[_Ctx] = []
    for i, c in enumerate(schema.columns):
        canon = (
            normalize_type(c.sa_type, connector.db_type)
            if c.sa_type is not None
            else CanonicalType(Kind.UNKNOWN, c.data_type)
        )
        col = sa.column(c.name, c.sa_type if c.sa_type is not None else sa.types.NullType())
        prof = ColumnProfile(name=c.name, canonical=canon, sensitive=is_sensitive_column(c.name, opts))
        ctxs.append(_Ctx(i, c, col, canon, prof))
    tbl = sa.table(table, *[x.col for x in ctxs], schema=getattr(connector, "schema", None))

    try:
        row_count = int(ex.run(sa.select(func.count().label("n")).select_from(tbl))[0]["n"])
    except ProfilingQueryError as e:
        raise ConnectorError(f"Cannot profile '{table}': {e}") from None

    _phase_basic(ex, tbl, ctxs, opts, dialect)
    sampled = row_count > opts.exact_distinct_max_rows and row_count > opts.sample_size
    for x in ctxs:
        _phase_distribution(ex, tbl, x, opts, dialect, row_count, sampled)

    tp = TableProfile(
        table=table,
        row_count=row_count,
        include_values=opts.include_values,
        primary_key=schema.primary_key,
        columns=[x.profile for x in ctxs],
    )
    _pk_check(ex, tbl, tp, opts)
    return tp


# ---------------------------------------------------------------- phase 1
def _phase_basic(ex, tbl, ctxs, opts, dialect):
    for start in range(0, len(ctxs), opts.column_batch_size):
        batch = ctxs[start : start + opts.column_batch_size]
        exprs = {x.idx: _basic_exprs(x, opts, dialect) for x in batch}
        try:
            row = _run_basic(ex, tbl, exprs)
            for x in batch:
                _apply_basic(x, row, exprs[x.idx], opts)
        except ProfilingQueryError:
            for x in batch:  # isolate: retry one column at a time
                try:
                    row = _run_basic(ex, tbl, {x.idx: exprs[x.idx]})
                    _apply_basic(x, row, exprs[x.idx], opts)
                except ProfilingQueryError as e:
                    x.profile.errors["basic"] = str(e)


def _basic_exprs(x: _Ctx, opts, dialect) -> dict:
    if x.profile.sensitive:
        return {"non_null": func.count(x.col)}  # counts only, whatever include_values says
    return M.basic_expressions(x.col, x.canon, dialect, opts.include_values)


def _run_basic(ex, tbl, exprs_by_idx: dict):
    cols = [func.count().label("rows")]
    for idx, d in exprs_by_idx.items():
        cols += [e.label(f"c{idx}_{k}") for k, e in d.items()]
    return ex.run(sa.select(*cols).select_from(tbl))[0]


def _apply_basic(x: _Ctx, row, exprs: dict, opts):
    p = x.profile
    raw = {k: row[f"c{x.idx}_{k}"] for k in exprs}
    x.rows = int(row["rows"])
    nn = int(raw.pop("non_null"))
    x.null_count = x.rows - nn
    p.metrics["null_count"] = MetricValue(x.null_count)
    p.metrics["non_null_count"] = MetricValue(nn)
    p.metrics["null_pct"] = MetricValue(x.null_count / x.rows * 100 if x.rows else None)

    if p.sensitive:
        p.skipped["*"] = "sensitive column name: only null counts are computed"
        return

    avg_sq = raw.pop("_avg_sq", "absent")
    if avg_sq != "absent":
        raw["stddev"] = M.sample_stddev(raw.get("mean"), avg_sq, nn)
    for k, v in raw.items():
        p.metrics[k] = MetricValue(int(v) if k.endswith("_count") else v)

    if not opts.include_values:
        p.redacted = M.value_metric_names(x.canon.kind)


# ---------------------------------------------------------------- phase 2
def _phase_distribution(ex, tbl, x: _Ctx, opts, dialect, row_count, sampled):
    p, kind = x.profile, x.canon.kind
    if p.sensitive:
        return
    if kind in M.NO_DISTINCT_KINDS:
        reason = f"not reliably comparable for kind '{kind.value}'"
        p.skipped.update(distinct_count=reason, distinct_pct=reason, is_unique=reason)
        return
    if "basic" in p.errors:
        return  # column already failed; do not hammer it further

    try:
        src, col = _source(tbl, x, sampled, opts, dialect, row_count)
        exprs = M.distribution_expressions(col, kind)
        row = ex.run(sa.select(*[e.label(k) for k, e in exprs.items()]).select_from(src))[0]
    except ProfilingQueryError as e:
        p.errors["distribution"] = str(e)
        return

    method, ss = ("sampled", int(row["n"])) if sampled else ("exact", None)
    nn = int(row["nn"])
    x.distinct, x.distinct_method = int(row["distinct_count"]), (method, ss)
    p.metrics["distinct_count"] = MetricValue(x.distinct, method, ss)
    p.metrics["distinct_pct"] = MetricValue(x.distinct / nn * 100 if nn else None, method, ss)
    if sampled:
        p.skipped["is_unique"] = "uniqueness requires an exact distinct count; table exceeds sampling threshold"
    else:
        p.metrics["is_unique"] = MetricValue(
            (x.distinct == nn and x.null_count == 0) if x.rows else None
        )
    for k, v in row.items():
        if k in ("n", "nn", "distinct_count"):
            continue
        p.metrics[k] = MetricValue(int(v) if k.endswith("_count") else v, method, ss)

    if kind in M.TOP_VALUES_KINDS:
        if not opts.include_values:
            return  # already listed in p.redacted by phase 1
        if x.distinct > opts.top_values_max_distinct:
            p.skipped["top_values"] = f"high cardinality (more than {opts.top_values_max_distinct} distinct)"
            return
        try:
            _top_values(ex, tbl, x, opts, dialect, row_count, sampled)
        except ProfilingQueryError as e:
            p.errors["top_values"] = str(e)


def _source(tbl, x: _Ctx, sampled, opts, dialect, row_count):
    if not sampled:
        return tbl, x.col
    return M.sample_source(tbl, x.info.name, dialect, opts.sample_size, row_count, opts.sample_seed)


def _top_values(ex, tbl, x: _Ctx, opts, dialect, row_count, sampled):
    src, col = _source(tbl, x, sampled, opts, dialect, row_count)
    cnt = func.count()
    stmt = (
        sa.select(col.label("v"), cnt.label("c"), func.sum(cnt).over().label("total"))
        .select_from(src)
        .where(col.is_not(None))
        .group_by(col)
        .order_by(cnt.desc(), col)
        .limit(opts.top_n)
    )
    rows = ex.run(stmt)
    out = []
    for r in rows:
        v = r["v"]
        if isinstance(v, str) and len(v) > opts.max_value_chars:
            v = v[: opts.max_value_chars] + "…"
        out.append({"value": v, "count": int(r["c"])})
    method, ss = ("sampled", int(rows[0]["total"]) if rows else 0) if sampled else ("exact", None)
    x.profile.metrics["top_values"] = MetricValue(out, method, ss)


# ---------------------------------------------------------------- PK check
def _pk_check(ex, tbl, tp: TableProfile, opts):
    if not tp.primary_key:
        return
    if tp.row_count > opts.exact_distinct_max_rows:
        tp.pk_check_skipped = "table exceeds exact_distinct_max_rows; duplicate check needs a full scan"
        return
    try:
        tp.pk_duplicate_key_count = MetricValue(pk_duplicate_key_count(ex, tbl, tp.primary_key))
    except ProfilingQueryError as e:
        tp.pk_check_error = str(e)


def pk_duplicate_key_count(ex, tbl, pk_names: list[str]) -> int:
    """Number of key values that occur more than once (0 when the PK is enforced)."""
    cols = [tbl.c[n] for n in pk_names]
    inner = sa.select(*cols).group_by(*cols).having(func.count() > 1).subquery()
    return int(ex.run(sa.select(func.count().label("n")).select_from(inner))[0]["n"])
