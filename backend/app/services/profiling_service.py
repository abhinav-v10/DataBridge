import dataclasses
import logging
import time

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.connectors.base import ConnectorError
from app.models.connection import Connection
from app.models.profile import ProfileColumnResult, ProfileRun
from app.profiling import ProfileOptions, TableProfile, profile_table
from app.schemas.profiling import ProfileRunRequest
from app.services import connection_service as conn_svc

logger = logging.getLogger("databridge.profiling")

# Metric names that carry data values. Never written to the metadata DB.
VALUE_METRICS = frozenset({"min", "max", "mean", "sum", "stddev", "top_values"})


class ProfilingNotAllowed(Exception):
    pass


class ProfileRunNotFound(Exception):
    pass


def build_options(req: ProfileRunRequest, allow_values: bool) -> ProfileOptions:
    if req.include_values and not allow_values:
        raise ProfilingNotAllowed(
            "include_values is disabled on this server. Set DATABRIDGE_PROFILING_ALLOW_VALUES=true to enable it."
        )
    overrides = {
        "exact_distinct_max_rows": req.exact_distinct_max_rows,
        "sample_size": req.sample_size,
        "top_n": req.top_n,
        "statement_timeout_ms": req.statement_timeout_ms,
    }
    kw = {k: v for k, v in overrides.items() if v is not None}
    return ProfileOptions(include_values=req.include_values, **kw)


def strip_values(column: dict) -> dict:
    """Remove data-bearing metrics from a ColumnProfile.to_dict() so it is safe to persist."""
    col = dict(column)
    col["metrics"] = {k: v for k, v in column["metrics"].items() if k not in VALUE_METRICS}
    removed = [k for k in column["metrics"] if k in VALUE_METRICS]
    col["redacted"] = list(dict.fromkeys([*column["redacted"], *removed]))
    return col


def _options_json(opts: ProfileOptions) -> dict:
    d = dataclasses.asdict(opts)
    return {k: (list(v) if isinstance(v, tuple) else v) for k, v in d.items()}


def run_profile(db: Session, conn: Connection, req: ProfileRunRequest, allow_values: bool):
    """Profile a table and persist the redacted result.

    Returns (ProfileRun, live_columns). live_columns is the full (possibly value-bearing) per-column dict
    list from this execution, for the POST response only. ConnectorError propagates; failures worth
    recording are saved first, but a missing table is a client error and is not recorded.
    """
    opts = build_options(req, allow_values)
    connector = conn_svc.connector_for(conn)
    t0 = time.monotonic()
    try:
        tp: TableProfile = profile_table(connector, req.table, opts)
    except ConnectorError as e:
        if "does not exist" not in str(e):
            _save_failure(db, conn.id, req, opts, str(e), t0)
        raise
    finally:
        connector.dispose()
    d = tp.to_dict()
    run = ProfileRun(
        connection_id=conn.id,
        table_name=req.table,
        status="completed",
        include_values_requested=req.include_values,
        options=_options_json(opts),
        row_count=tp.row_count,
        column_count=tp.column_count,
        primary_key=d["primary_key"],
        table_metrics={k: d[k] for k in ("pk_duplicate_key_count", "pk_check_skipped", "pk_check_error")},
        duration_ms=int((time.monotonic() - t0) * 1000),
    )
    for pos, c in enumerate(d["columns"]):
        safe = strip_values(c)
        run.columns.append(ProfileColumnResult(
            position=pos, column_name=safe["name"], status=safe["status"], sensitive=safe["sensitive"],
            canonical=safe["type"], metrics=safe["metrics"], redacted=safe["redacted"],
            skipped=safe["skipped"], errors=safe["errors"],
        ))
    db.add(run)
    db.commit()
    logger.info("profile run %s: connection=%s table=%s rows=%s", run.id, conn.id, req.table, tp.row_count)
    return run, d["columns"]


def _save_failure(db: Session, conn_id: int, req: ProfileRunRequest, opts: ProfileOptions, message: str, t0: float):
    db.add(ProfileRun(
        connection_id=conn_id, table_name=req.table, status="failed",
        include_values_requested=req.include_values, options=_options_json(opts),
        error=message[:1000], duration_ms=int((time.monotonic() - t0) * 1000),
    ))
    db.commit()


def run_to_out(run: ProfileRun, live_columns: list[dict] | None = None) -> dict:
    """Shape a ProfileRun (plus optional live, value-bearing columns) for ProfileRunOut."""
    if live_columns is not None:
        columns = live_columns
    else:
        columns = [{
            "name": c.column_name, "type": c.canonical, "status": c.status, "sensitive": c.sensitive,
            "metrics": c.metrics, "redacted": c.redacted, "skipped": c.skipped, "errors": c.errors,
        } for c in run.columns]
    tm = run.table_metrics or {}
    return {
        "id": run.id, "connection_id": run.connection_id, "table_name": run.table_name, "status": run.status,
        "row_count": run.row_count, "column_count": run.column_count, "duration_ms": run.duration_ms,
        "created_at": run.created_at, "error": run.error,
        "include_values_requested": run.include_values_requested,
        "values_in_response": live_columns is not None and run.include_values_requested,
        "values_persisted": False,
        "options": run.options, "primary_key": run.primary_key,
        "pk_duplicate_key_count": tm.get("pk_duplicate_key_count"),
        "pk_check_skipped": tm.get("pk_check_skipped"), "pk_check_error": tm.get("pk_check_error"),
        "columns": columns,
    }


def list_runs(db: Session, connection_id: int | None, table: str | None, limit: int, offset: int):
    q = select(ProfileRun)
    cq = select(func.count()).select_from(ProfileRun)
    if connection_id is not None:
        q = q.where(ProfileRun.connection_id == connection_id)
        cq = cq.where(ProfileRun.connection_id == connection_id)
    if table:
        q = q.where(ProfileRun.table_name == table)
        cq = cq.where(ProfileRun.table_name == table)
    q = q.order_by(ProfileRun.id.desc()).limit(limit).offset(offset)
    return list(db.scalars(q)), db.scalar(cq)


def get_run(db: Session, run_id: int) -> ProfileRun:
    run = db.get(ProfileRun, run_id)
    if run is None:
        raise ProfileRunNotFound(f"Profile run {run_id} not found")
    return run


def delete_run(db: Session, run_id: int) -> None:
    db.delete(get_run(db, run_id))
    db.commit()
