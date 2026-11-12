from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.connectors.base import ConnectorError
from app.core.config import get_settings
from app.core.db import get_session
from app.schemas.profiling import ProfileRunList, ProfileRunOut, ProfileRunRequest
from app.services import connection_service as conn_svc
from app.services import profiling_service as svc

router = APIRouter(tags=["profiling"])


def connector_http_error(e: ConnectorError) -> HTTPException:
    code = status.HTTP_404_NOT_FOUND if "does not exist" in str(e) else status.HTTP_502_BAD_GATEWAY
    return HTTPException(code, str(e))


@router.post("/profiling/run", response_model=ProfileRunOut, status_code=status.HTTP_201_CREATED)
def run_profiling(req: ProfileRunRequest, db: Session = Depends(get_session)):
    """Profile one table. Only redacted results are stored; values (if enabled and requested) appear
    in this response only."""
    try:
        conn = conn_svc.get_connection(db, req.connection_id)
    except conn_svc.ConnectionNotFound as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(e)) from e
    try:
        run, live = svc.run_profile(db, conn, req, get_settings().profiling_allow_values)
    except svc.ProfilingNotAllowed as e:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(e)) from e
    except ConnectorError as e:
        raise connector_http_error(e) from e
    return svc.run_to_out(run, live)


@router.get("/profiling/runs", response_model=ProfileRunList)
def list_profile_runs(
    connection_id: int | None = Query(None),
    table: str | None = Query(None, max_length=255),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_session),
):
    items, total = svc.list_runs(db, connection_id, table, limit, offset)
    return {"total": total, "items": items}


@router.get("/profiling/runs/{run_id}", response_model=ProfileRunOut)
def get_profile_run(run_id: int, db: Session = Depends(get_session)):
    try:
        return svc.run_to_out(svc.get_run(db, run_id))
    except svc.ProfileRunNotFound as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(e)) from e


@router.delete("/profiling/runs/{run_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_profile_run(run_id: int, db: Session = Depends(get_session)):
    try:
        svc.delete_run(db, run_id)
    except svc.ProfileRunNotFound as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(e)) from e
