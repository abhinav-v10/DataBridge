from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import URL, Engine
from sqlalchemy.exc import NoSuchTableError, SQLAlchemyError

from app.connectors.base import BaseConnector, ColumnInfo, ConnectorError, TableSchema


class SQLAlchemyConnector(BaseConnector):
    """Shared implementation using SQLAlchemy inspection. Subclasses only build the URL."""

    def __init__(self, params: dict):
        self.params = params
        self.schema = params.get("db_schema") or None
        self._engine: Engine | None = None

    def _build_url(self) -> URL | str:
        raise NotImplementedError

    def engine(self) -> Engine:
        if self._engine is None:
            try:
                self._engine = create_engine(self._build_url(), pool_pre_ping=True)
            except Exception as e:  # bad driver/URL
                raise ConnectorError(f"Cannot create engine: {e}") from e
        return self._engine

    def test_connection(self) -> None:
        try:
            with self.engine().connect() as conn:
                conn.execute(text("SELECT 1"))
        except SQLAlchemyError as e:
            raise ConnectorError(f"Connection failed: {getattr(e, 'orig', e)}") from e

    def list_tables(self) -> list[str]:
        try:
            return sorted(inspect(self.engine()).get_table_names(schema=self.schema))
        except SQLAlchemyError as e:
            raise ConnectorError(f"Cannot list tables: {getattr(e, 'orig', e)}") from e

    def get_table_schema(self, table: str) -> TableSchema:
        try:
            insp = inspect(self.engine())
            cols = insp.get_columns(table, schema=self.schema)
            pk = set(insp.get_pk_constraint(table, schema=self.schema).get("constrained_columns") or [])
        except NoSuchTableError as e:
            raise ConnectorError(f"Table '{table}' does not exist") from e
        except SQLAlchemyError as e:
            raise ConnectorError(f"Cannot read schema: {getattr(e, 'orig', e)}") from e
        if not cols:
            raise ConnectorError(f"Table '{table}' does not exist")
        out = []
        for c in cols:
            t = c["type"]
            out.append(
                ColumnInfo(
                    name=c["name"],
                    data_type=str(t),
                    nullable=bool(c.get("nullable", True)),
                    is_primary_key=c["name"] in pk,
                    max_length=getattr(t, "length", None),
                    precision=getattr(t, "precision", None),
                    scale=getattr(t, "scale", None),
                    sa_type=t,
                )
            )
        return TableSchema(name=table, columns=out)

    def dispose(self) -> None:
        if self._engine is not None:
            self._engine.dispose()
            self._engine = None
