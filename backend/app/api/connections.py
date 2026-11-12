from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.connectors.base import ConnectorError
from app.core.db import get_session
from app.schemas.connection import (
    ConnectionCreate,
    ConnectionOut,
    ConnectionStatus,
    ConnectionTestRequest,
    ColumnOut,
    TableSchemaOut,
)
from app.services import connection_service as svc

router = APIRouter(tags=["connections"])


def _resolve(db: Session, conn_id: int):
    try:
        return svc.get_connection(db, conn_id)
    except svc.ConnectionNotFound as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(e)) from e


@router.post("/connections", response_model=ConnectionOut, status_code=status.HTTP_201_CREATED)
def create_connection(data: ConnectionCreate, db: Session = Depends(get_session)):
    # Validate db_type early so we never store an unusable connection.
    try:
        svc.connector_from_request(data)
    except ConnectorError as e:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(e)) from e
    try:
        return svc.create_connection(db, data)
    except svc.DuplicateConnection as e:
        raise HTTPException(status.HTTP_409_CONFLICT, str(e)) from e


@router.get("/connections", response_model=list[ConnectionOut])
def list_connections(role: str | None = Query(None, pattern="^(source|target)$"), db: Session = Depends(get_session)):
    return svc.list_connections(db, role)


@router.delete("/connections/{conn_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_connection(conn_id: int, db: Session = Depends(get_session)):
    _resolve(db, conn_id)
    svc.delete_connection(db, conn_id)


@router.post("/connections/test", response_model=ConnectionStatus)
def test_unsaved_connection(data: ConnectionTestRequest):
    try:
        connector = svc.connector_from_request(data)
        try:
            connector.test_connection()
        finally:
            connector.dispose()
    except ConnectorError as e:
        return ConnectionStatus(ok=False, message=str(e))
    return ConnectionStatus(ok=True, message="Connection successful")


@router.get("/connections/{conn_id}/status", response_model=ConnectionStatus)
def connection_status(conn_id: int, db: Session = Depends(get_session)):
    connector = svc.connector_for(_resolve(db, conn_id))
    try:
        connector.test_connection()
        return ConnectionStatus(ok=True, message="Connected")
    except ConnectorError as e:
        return ConnectionStatus(ok=False, message=str(e))
    finally:
        connector.dispose()


@router.get("/connections/{conn_id}/tables", response_model=list[str])
def list_tables(conn_id: int, db: Session = Depends(get_session)):
    connector = svc.connector_for(_resolve(db, conn_id))
    try:
        return connector.list_tables()
    except ConnectorError as e:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(e)) from e
    finally:
        connector.dispose()


@router.get("/connections/{conn_id}/schema/{table_name}", response_model=TableSchemaOut)
def table_schema(conn_id: int, table_name: str, db: Session = Depends(get_session)):
    connector = svc.connector_for(_resolve(db, conn_id))
    try:
        ts = connector.get_table_schema(table_name)
    except ConnectorError as e:
        code = status.HTTP_404_NOT_FOUND if "does not exist" in str(e) else status.HTTP_502_BAD_GATEWAY
        raise HTTPException(code, str(e)) from e
    finally:
        connector.dispose()
    return TableSchemaOut(name=ts.name, columns=[
            ColumnOut(
                name=c.name,
                data_type=c.data_type,
                nullable=c.nullable,
                is_primary_key=c.is_primary_key,
                max_length=c.max_length,
                precision=c.precision,
                scale=c.scale,
            )
            for c in ts.columns
        ], primary_key=ts.primary_key)
