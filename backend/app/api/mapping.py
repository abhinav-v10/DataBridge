from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.api.profiling import connector_http_error
from app.connectors.base import ConnectorError
from app.core.db import get_session
from app.schemas.mapping import ComparisonOut, ComparisonRequest, MappingCreate, MappingOut, ValidationOut
from app.services import connection_service as conn_svc
from app.services import mapping_service as svc

router = APIRouter(tags=["mapping"])


def _mapping_out(m) -> dict:
    return {
        "id": m.id, "name": m.name, "source_connection_id": m.source_connection_id,
        "target_connection_id": m.target_connection_id, "source_table": m.source_table,
        "target_table": m.target_table, "key_columns": m.key_columns, "columns": m.config["columns"],
        "created_at": m.created_at,
    }


def _not_found(e: Exception) -> HTTPException:
    return HTTPException(status.HTTP_404_NOT_FOUND, str(e))


@router.post("/schema-comparison", response_model=ComparisonOut)
def compare_schemas(req: ComparisonRequest, db: Session = Depends(get_session)):
    """Compare two tables and recommend a one-to-one column mapping. Nothing is saved."""
    try:
        return svc.compare(db, req).to_dict()
    except conn_svc.ConnectionNotFound as e:
        raise _not_found(e) from e
    except ConnectorError as e:
        raise connector_http_error(e) from e


@router.post("/schema-mappings", response_model=MappingOut, status_code=status.HTTP_201_CREATED)
def create_mapping(data: MappingCreate, db: Session = Depends(get_session)):
    try:
        return _mapping_out(svc.create_mapping(db, data))
    except conn_svc.ConnectionNotFound as e:
        raise _not_found(e) from e
    except svc.DuplicateMapping as e:
        raise HTTPException(status.HTTP_409_CONFLICT, str(e)) from e
    except svc.MappingInvalid as e:
        detail = {"message": str(e)}
        if e.report is not None:
            detail["errors"] = [i.to_dict() for i in e.report.errors]
            detail["warnings"] = [i.to_dict() for i in e.report.warnings]
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail) from e
    except ConnectorError as e:
        raise connector_http_error(e) from e


@router.get("/schema-mappings", response_model=list[MappingOut])
def list_mappings(db: Session = Depends(get_session)):
    return [_mapping_out(m) for m in svc.list_mappings(db)]


@router.get("/schema-mappings/{mapping_id}", response_model=MappingOut)
def get_mapping(mapping_id: int, db: Session = Depends(get_session)):
    try:
        return _mapping_out(svc.get_mapping(db, mapping_id))
    except svc.MappingNotFound as e:
        raise _not_found(e) from e


@router.delete("/schema-mappings/{mapping_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_mapping(mapping_id: int, db: Session = Depends(get_session)):
    try:
        svc.delete_mapping(db, mapping_id)
    except svc.MappingNotFound as e:
        raise _not_found(e) from e


@router.post("/schema-mappings/{mapping_id}/validate", response_model=ValidationOut)
def validate_mapping(mapping_id: int, db: Session = Depends(get_session)):
    """Re-check a saved mapping against the current live schemas."""
    try:
        return svc.validate_mapping(db, mapping_id).to_dict()
    except svc.MappingNotFound as e:
        raise _not_found(e) from e
    except ConnectorError as e:
        raise connector_http_error(e) from e
