from sqlalchemy import select
from sqlalchemy.orm import Session

from app.connectors.base import BaseConnector
from app.connectors.registry import build_connector
from app.core.security import decrypt_secret, encrypt_secret
from app.models.connection import Connection
from app.models.mapping import SchemaMapping
from app.models.profile import ProfileRun
from app.schemas.connection import ConnectionCreate


class ConnectionNotFound(Exception):
    pass


class DuplicateConnection(Exception):
    pass


def create_connection(db: Session, data: ConnectionCreate) -> Connection:
    if db.scalar(select(Connection).where(Connection.name == data.name)):
        raise DuplicateConnection(f"A connection named '{data.name}' already exists")
    conn = Connection(
        **data.model_dump(exclude={"password"}),
        password_enc=encrypt_secret(data.password) if data.password else "",
    )
    db.add(conn)
    db.commit()
    return conn


def list_connections(db: Session, role: str | None = None) -> list[Connection]:
    q = select(Connection).order_by(Connection.id)
    if role:
        q = q.where(Connection.role == role)
    return list(db.scalars(q))


def get_connection(db: Session, conn_id: int) -> Connection:
    conn = db.get(Connection, conn_id)
    if conn is None:
        raise ConnectionNotFound(f"Connection {conn_id} not found")
    return conn


def delete_connection(db: Session, conn_id: int) -> None:
    conn = get_connection(db, conn_id)
    # Remove dependents explicitly (via the ORM so child rows cascade); SQLite does not enforce FK cascades.
    for run in db.scalars(select(ProfileRun).where(ProfileRun.connection_id == conn_id)):
        db.delete(run)
    for m in db.scalars(
        select(SchemaMapping).where(
            (SchemaMapping.source_connection_id == conn_id) | (SchemaMapping.target_connection_id == conn_id)
        )
    ):
        db.delete(m)
    db.delete(conn)
    db.commit()


def connector_for(conn: Connection) -> BaseConnector:
    return build_connector(
        conn.db_type,
        {
            "host": conn.host,
            "port": conn.port,
            "database": conn.database,
            "username": conn.username,
            "password": decrypt_secret(conn.password_enc) if conn.password_enc else "",
            "db_schema": conn.db_schema,
        },
    )


def connector_from_request(data: ConnectionCreate) -> BaseConnector:
    return build_connector(data.db_type, data.model_dump())
