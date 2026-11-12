"""PostgreSQL integration tests for the profiler.

Skipped automatically when no server is reachable. Point at one with DATABRIDGE_TEST_PG_URL
(default: the project's Docker setup on port 5434). Each run uses a throwaway schema.
"""
import json
import os
import uuid

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import make_url

from app.connectors.postgres import PostgresConnector
from app.profiling import ProfileOptions, profile_table
from app.profiling.profiler import ProfilingQueryError, _Executor

pytestmark = pytest.mark.integration

PG_URL = os.environ.get(
    "DATABRIDGE_TEST_PG_URL", "postgresql+psycopg2://databridge:databridge_dev@localhost:5434/databridge"
)


@pytest.fixture(scope="module")
def pg():
    url = make_url(PG_URL)
    try:
        eng = sa.create_engine(url, connect_args={"connect_timeout": 3})
        with eng.connect() as c:
            c.execute(sa.text("SELECT 1"))
    except Exception as e:  # no server / bad credentials -> skip, never fail
        pytest.skip(f"PostgreSQL not available ({type(e).__name__}); set DATABRIDGE_TEST_PG_URL to run")
    schema = "prof_" + uuid.uuid4().hex[:8]
    with eng.begin() as c:
        c.execute(sa.text(f'CREATE SCHEMA "{schema}"'))
        c.execute(sa.text(f"CREATE TYPE \"{schema}\".mood AS ENUM ('sad','ok','happy')"))
        c.execute(sa.text(f"""CREATE TABLE "{schema}".t (
            id integer PRIMARY KEY, amount numeric(12,2), ts timestamptz, tags text[], feeling "{schema}".mood,
            doc jsonb, uid uuid, name varchar(30), password varchar(30), born date)"""))
        c.execute(sa.text(f"""INSERT INTO "{schema}".t
            SELECT g, (g % 50) + 0.25, now() - (g || ' days')::interval, ARRAY['a','b'],
                   (ARRAY['sad','ok','happy'])[1 + g % 3]::"{schema}".mood, jsonb_build_object('k', 1), gen_random_uuid(),
                   'name' || (g % 10), 'secret-pw-' || g, DATE '2000-01-01' + g
            FROM generate_series(1, 5000) g"""))
        c.execute(sa.text(f'INSERT INTO "{schema}".t (id) VALUES (9999)'))
    conn = PostgresConnector({
        "host": url.host, "port": url.port, "database": url.database,
        "username": url.username, "password": url.password, "db_schema": schema,
    })
    yield conn, eng, schema
    conn.dispose()
    with eng.begin() as c:
        c.execute(sa.text(f'DROP SCHEMA "{schema}" CASCADE'))
    eng.dispose()


def by_name(tp):
    return {c.name: c for c in tp.columns}


def test_types_and_exact_metrics(pg):
    conn, _, _ = pg
    tp = profile_table(conn, "t", ProfileOptions(include_values=True))
    c = by_name(tp)
    assert tp.row_count == 5001 and all(col.status == "ok" for col in tp.columns)
    assert c["amount"].canonical.scale == 2 and c["ts"].canonical.timezone is True
    assert c["tags"].canonical.kind.value == "array" and c["feeling"].canonical.kind.value == "enum"
    assert c["id"].metrics["distinct_count"].value == 5001 and c["id"].metrics["is_unique"].value is True
    assert c["amount"].metrics["null_count"].value == 1
    assert c["tags"].metrics["non_null_count"].value == 5000 and "distinct_count" in c["tags"].skipped
    assert c["doc"].metrics["non_null_count"].value == 5000
    assert c["feeling"].metrics["distinct_count"].value == 3
    assert {x["value"] for x in c["feeling"].metrics["top_values"].value} == {"sad", "ok", "happy"}
    assert c["born"].metrics["future_count"].value == 0


def test_stddev_matches_postgres_stddev_samp(pg):
    conn, eng, schema = pg
    tp = profile_table(conn, "t", ProfileOptions(include_values=True))
    with eng.connect() as cx:
        expected = cx.execute(sa.text(f'SELECT stddev_samp(id), stddev_samp(amount) FROM "{schema}".t')).one()
    c = by_name(tp)
    assert float(c["id"].metrics["stddev"].value) == pytest.approx(float(expected[0]))
    assert float(c["amount"].metrics["stddev"].value) == pytest.approx(float(expected[1]))


def test_redaction_and_sensitive_columns(pg):
    conn, _, _ = pg
    tp = profile_table(conn, "t")
    assert "secret-pw" not in json.dumps(tp.to_dict())
    assert "min" not in by_name(tp)["amount"].metrics
    tp = profile_table(conn, "t", ProfileOptions(include_values=True))
    pw = by_name(tp)["password"]
    assert set(pw.metrics) == {"null_count", "non_null_count", "null_pct"}
    assert "secret-pw" not in json.dumps(tp.to_dict())


def test_tablesample_sampling_reports_method_and_size(pg):
    conn, _, _ = pg
    tp = profile_table(conn, "t", ProfileOptions(exact_distinct_max_rows=1000, sample_size=500))
    d = by_name(tp)["name"].metrics["distinct_count"]
    assert d.method == "sampled" and d.sample_size > 0 and 1 <= d.value <= 10
    assert by_name(tp)["name"].metrics["null_count"].method == "exact"
    assert by_name(tp)["name"].status == "ok"


def test_statement_timeout_enforced(pg):
    _, eng, _ = pg
    with pytest.raises(ProfilingQueryError, match="timed out"):
        _Executor(eng, timeout_ms=200).run(sa.text("SELECT pg_sleep(5)"))


def test_executor_is_read_only(pg):
    _, eng, schema = pg
    with pytest.raises(ProfilingQueryError):
        _Executor(eng, timeout_ms=5000).run(sa.text(f'INSERT INTO "{schema}".t (id) VALUES (123456)'))
    with eng.connect() as cx:
        assert cx.execute(sa.text(f'SELECT count(*) FROM "{schema}".t WHERE id = 123456')).scalar() == 0
