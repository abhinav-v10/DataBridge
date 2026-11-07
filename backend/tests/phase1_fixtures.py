"""Shared fixtures for the Phase 1 API/integration tests (kept out of conftest.py to leave Phase 0 untouched)."""
import sqlite3

import pytest

SECRET = "TOPSECRET-VALUE-9f3a"
DB_PASSWORD = "Sup3rS3cretDbPw!"

SRC_DDL = "CREATE TABLE customers (customer_id INTEGER PRIMARY KEY, name VARCHAR(50), password VARCHAR(50), note TEXT)"
SRC_ROWS = [(1, "Asha", "pw-" + SECRET, SECRET), (2, "Ravi", "pw2", "x"), (3, "Zed", None, "x")]
TGT_DDL = ("CREATE TABLE customers (customer_id INTEGER PRIMARY KEY, name VARCHAR(80), note TEXT, "
           "extra_required TEXT NOT NULL DEFAULT 'd')")


def _mk(path, ddl, rows=(), ins=None):
    c = sqlite3.connect(path)
    c.execute(ddl)
    if rows:
        c.executemany(ins, rows)
    c.commit()
    c.close()
    return str(path)


@pytest.fixture
def two_dbs(tmp_path):
    src = _mk(tmp_path / "src.db", SRC_DDL, SRC_ROWS, "INSERT INTO customers VALUES (?,?,?,?)")
    tgt = _mk(tmp_path / "tgt.db", TGT_DDL)
    return src, tgt


def _post_conn(client, name, role, path, **extra):
    r = client.post("/api/connections", json={"name": name, "role": role, "db_type": "sqlite", "database": path, **extra})
    assert r.status_code == 201, r.text
    return r.json()["id"]


@pytest.fixture
def api_conns(client, two_dbs):
    """(source_id, target_id) of two saved SQLite connections. The source carries a username/password."""
    return (_post_conn(client, "src", "source", two_dbs[0], username="svc_user", password=DB_PASSWORD),
            _post_conn(client, "tgt", "target", two_dbs[1]))


@pytest.fixture
def allow_values(monkeypatch):
    from app.core import config

    monkeypatch.setenv("DATABRIDGE_PROFILING_ALLOW_VALUES", "true")
    config.get_settings.cache_clear()
    yield
    config.get_settings.cache_clear()
