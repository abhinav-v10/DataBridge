from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ColumnInfo:
    name: str
    data_type: str
    nullable: bool
    is_primary_key: bool = False
    max_length: int | None = None
    precision: int | None = None
    scale: int | None = None
    # INTERNAL ONLY: the reflected SQLAlchemy type object. Never serialize this (API/JSON/persistence);
    # it is excluded from repr/eq and the API builds responses from explicit fields.
    sa_type: Any = field(default=None, repr=False, compare=False)


@dataclass
class TableSchema:
    name: str
    columns: list[ColumnInfo] = field(default_factory=list)

    @property
    def primary_key(self) -> list[str]:
        return [c.name for c in self.columns if c.is_primary_key]


class ConnectorError(Exception):
    """Raised for any connector failure with a user-presentable message."""


class BaseConnector(ABC):
    """Contract every database connector must satisfy.

    Adding a database (Oracle, MySQL, ...) means implementing this class and registering it
    in connectors/registry.py. Nothing else in the platform changes.
    """

    db_type: str

    @abstractmethod
    def test_connection(self) -> None:
        """Return normally if reachable; raise ConnectorError otherwise."""

    @abstractmethod
    def list_tables(self) -> list[str]: ...

    @abstractmethod
    def get_table_schema(self, table: str) -> TableSchema: ...

    @abstractmethod
    def engine(self):
        """SQLAlchemy engine for query execution by profiling/ETL/reconciliation."""

    def dispose(self) -> None:
        pass
