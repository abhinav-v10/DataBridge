from app.connectors.base import BaseConnector, ConnectorError
from app.connectors.postgres import PostgresConnector
from app.connectors.sqlite import SQLiteConnector

# Only connectors that are actually implemented are registered.
# Oracle / MySQL / SQL Server / Snowflake are NOT implemented yet.
_REGISTRY: dict[str, type[BaseConnector]] = {
    "postgresql": PostgresConnector,
    "sqlite": SQLiteConnector,
}


def supported_types() -> list[str]:
    return sorted(_REGISTRY)


def build_connector(db_type: str, params: dict) -> BaseConnector:
    try:
        cls = _REGISTRY[db_type]
    except KeyError:
        raise ConnectorError(f"Unsupported db_type '{db_type}'. Supported: {', '.join(supported_types())}") from None
    return cls(params)
