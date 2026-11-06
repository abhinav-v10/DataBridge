from sqlalchemy.engine import URL

from app.connectors.sqlalchemy_connector import SQLAlchemyConnector


class PostgresConnector(SQLAlchemyConnector):
    db_type = "postgresql"

    def _build_url(self) -> URL:
        p = self.params
        return URL.create(
            "postgresql+psycopg2",
            username=p.get("username") or None,
            password=p.get("password") or None,
            host=p.get("host"),
            port=p.get("port") or 5432,
            database=p["database"],
            # Fail fast on unreachable hosts instead of hanging on the OS TCP default.
            query={"connect_timeout": "10"},
        )
