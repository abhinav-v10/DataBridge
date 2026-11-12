"""Phase 1 PostgreSQL integration tests (API level). Skipped automatically if no server is reachable.

Uses the documented DataBridge test PostgreSQL (Docker, port 5434) or DATABRIDGE_TEST_PG_URL.
Never targets any other server. Each run uses a throwaway schema.
"""
import json
import os
import uuid

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import make_url

pytestmark = pytest.mark.integration

PG_URL = os.environ.get(
    "DATABRIDGE_TEST_PG_URL", "postgresql+psycopg2://databridge:databridge_dev@localhost:5434/databridge"
)
SECRET = "pg-secret-value-77"


@pytest.fixture(scope="module")
def pg_schema():
    url = make_url(PG_URL)
    try:
        eng = sa.create_engine(url, connect_args={"connect_timeout": 3})
        with eng.connect() as c:
            c.execute(sa.text("SELECT 1"))
    except Exception as e:
        pytest.skip(f"PostgreSQL not available ({type(e).__name__}); set DATABRIDGE_TEST_PG_URL to run")
    schema = "p1_" + uuid.uuid4().hex[:8]
    with eng.begin() as c:
        c.execute(sa.text(f'CREATE SCHEMA "{schema}"'))
        c.execute(sa.text(f"CREATE TYPE \"{schema}\".mood AS ENUM ('sad','ok')"))
        c.execute(sa.text(f"CREATE TYPE \"{schema}\".mood2 AS ENUM ('sad','ok','happy')"))
        c.execute(sa.text(f'CREATE DOMAIN "{schema}".positive_int AS integer CHECK (VALUE > 0)'))
        c.execute(sa.text(f"""CREATE TABLE "{schema}".src (
            id integer PRIMARY KEY, amount numeric(12,2), created timestamptz, tags text[],
            feeling "{schema}".mood, code varchar(20), qty "{schema}".positive_int, note text, password varchar(40),
            blob jsonb)"""))
        c.execute(sa.text(f"""CREATE TABLE "{schema}".tgt (
            id bigint PRIMARY KEY, amount numeric(10,2), created timestamp, tags text[],
            feeling "{schema}".mood2, code varchar(10), qty integer, note text, blob jsonb)"""))
        c.execute(sa.text(f"""INSERT INTO "{schema}".src
            SELECT g, g + 0.5, now(), ARRAY['a'], 'ok', 'c' || g, g, '{SECRET}', 'pw-' || g, jsonb_build_object('k', 1)
            FROM generate_series(1, 2000) g"""))
    yield schema
    with eng.begin() as c:
        c.execute(sa.text(f'DROP SCHEMA "{schema}" CASCADE'))
    eng.dispose()


@pytest.fixture
def pg_conn_id(client, pg_schema):
    u = make_url(PG_URL)
    r = client.post("/api/connections", json={
        "name": "pg", "role": "source", "db_type": "postgresql", "host": u.host, "port": u.port,
        "database": u.database, "username": u.username, "password": u.password, "db_schema": pg_schema})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def compare(client, cid):
    return client.post("/api/schema-comparison", json={
        "source_connection_id": cid, "source_table": "src", "target_connection_id": cid, "target_table": "tgt"})


def test_pg_types_through_comparison(client, pg_conn_id):
    r = compare(client, pg_conn_id)
    assert r.status_code == 200, r.text
    m = {x["source_column"]: x for x in r.json()["mappings"]}
    assert m["id"]["type_compatibility"] == "compatible"  # int32 -> int64
    assert m["amount"]["type_compatibility"] == "lossy"  # numeric(12,2) -> numeric(10,2)
    assert m["created"]["type_compatibility"] == "lossy"  # timestamptz -> timestamp
    assert m["tags"]["type_compatibility"] == "exact" and m["tags"]["source_type"]["kind"] == "array"
    assert m["feeling"]["type_compatibility"] == "compatible"  # enum gains a value
    assert m["code"]["type_compatibility"] == "lossy"  # varchar(20) -> varchar(10)
    assert m["qty"]["source_type"]["domain"] == "positive_int" and m["qty"]["type_compatibility"] == "exact"
    assert m["blob"]["type_compatibility"] == "exact"
    assert [u["column"] for u in r.json()["unmapped_source"]] == ["password"]


def test_pg_profiling_via_api_is_redacted_and_persisted(client, pg_conn_id):
    r = client.post("/api/profiling/run", json={"connection_id": pg_conn_id, "table": "src"})
    assert r.status_code == 201, r.text
    b = r.json()
    assert b["row_count"] == 2000 and all(c["status"] == "ok" for c in b["columns"])
    cols = {c["name"]: c for c in b["columns"]}
    assert cols["created"]["type"]["timezone"] is True and cols["tags"]["type"]["kind"] == "array"
    assert cols["id"]["metrics"]["is_unique"]["value"] is True
    assert set(cols["password"]["metrics"]) == {"null_count", "non_null_count", "null_pct"}
    assert SECRET not in r.text and "pw-" not in r.text
    again = client.get(f"/api/profiling/runs/{b['id']}")
    assert again.status_code == 200 and SECRET not in again.text


def test_pg_sampling_via_api(client, pg_conn_id):
    b = client.post("/api/profiling/run", json={
        "connection_id": pg_conn_id, "table": "src", "exact_distinct_max_rows": 500, "sample_size": 300}).json()
    d = {c["name"]: c for c in b["columns"]}["code"]["metrics"]["distinct_count"]
    assert d["method"] == "sampled" and d["sample_size"] > 0


def test_pg_mapping_lifecycle_and_validation(client, pg_conn_id):
    r = client.post("/api/schema-mappings", json={
        "name": "pgmap", "source_connection_id": pg_conn_id, "source_table": "src",
        "target_connection_id": pg_conn_id, "target_table": "tgt", "key_columns": ["id"]})
    assert r.status_code == 201, r.text
    v = client.post(f"/api/schema-mappings/{r.json()['id']}/validate").json()
    assert v["valid"] is True
    assert {w["code"] for w in v["warnings"]} >= {"type_lossy"}
    json.dumps(v)


def test_pg_missing_table_is_404(client, pg_conn_id):
    r = client.post("/api/profiling/run", json={"connection_id": pg_conn_id, "table": "nope"})
    assert r.status_code == 404
