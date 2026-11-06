import sqlite3

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from app.core import config, db


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.setenv("DATABRIDGE_SECRET_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("DATABRIDGE_METADATA_DB_URL", f"sqlite:///{tmp_path / 'meta.db'}")
    config.get_settings.cache_clear()
    db.init_engine()
    yield
    config.get_settings.cache_clear()


@pytest.fixture
def client():
    from app.main import app

    with TestClient(app) as c:
        yield c


@pytest.fixture
def sample_db(tmp_path):
    """A real SQLite file with two related tables, used as a connection target."""
    path = tmp_path / "sample.db"
    con = sqlite3.connect(path)
    con.executescript(
        """
        CREATE TABLE customers (customer_id INTEGER PRIMARY KEY, name VARCHAR(50) NOT NULL, balance NUMERIC(12,2));
        CREATE TABLE accounts (id INTEGER PRIMARY KEY, customer_id INTEGER REFERENCES customers(customer_id));
        INSERT INTO customers VALUES (1,'Asha',100.50),(2,'Ravi',NULL);
        """
    )
    con.commit()
    con.close()
    return str(path)
