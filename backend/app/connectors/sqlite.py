from app.connectors.sqlalchemy_connector import SQLAlchemyConnector


class SQLiteConnector(SQLAlchemyConnector):
    """TEST/DEV ONLY. Lets the test-suite run without a database server. Not a migration target."""

    db_type = "sqlite"

    def _build_url(self) -> str:
        return f"sqlite:///{self.params['database']}"
