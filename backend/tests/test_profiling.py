"""Tier-1 profiling engine tests (SQLite, real database files)."""
import json
import math
import sqlite3
import time

import pytest

from app.connectors.base import ConnectorError
from app.connectors.sqlite import SQLiteConnector
from app.profiling import ProfileOptions, profile_table
from app.profiling import profiler as prof_mod
from app.profiling.models import is_sensitive_column
from app.profiling.profiler import ProfilingQueryError, _Executor

SECRET = "TOPSECRET-VALUE-9f3a"

KITCHEN_DDL = """
CREATE TABLE kitchen (
  id INTEGER PRIMARY KEY, qty INTEGER, price NUMERIC(10,2), ratio REAL, name VARCHAR(20),
  flag BOOLEAN, born DATE, created DATETIME, blobby BLOB, payload JSON, untyped,
  note VARCHAR(100), password VARCHAR(50)
)"""
KITCHEN_ROWS = [
    (1, 10, 1.50, 0.5, "alice", 1, "2000-01-01", "2020-01-01 00:00:00", b"\x01", '{"a":1}', 1, SECRET, "hunter2-" + SECRET),
    (2, 0, 2.50, 1.5, "bob", 0, "2999-01-01", "2999-01-01 00:00:00", b"\x01\x02", "{}", 2, "n", "pw2"),
    (3, -5, None, None, "", None, None, None, None, None, None, None, None),
    (4, 10, None, None, " padded ", None, None, None, None, None, None, None, None),
    (5, None, None, None, "alice", None, None, None, None, None, None, None, None),
    (6, 20, None, None, None, None, None, None, None, None, None, None, None),
]


def make_db(tmp_path, ddl, rows=(), insert=None, name="p.db"):
    path = tmp_path / name
    con = sqlite3.connect(path)
    con.execute(ddl)
    if rows:
        con.executemany(insert, rows)
    con.commit()
    con.close()
    return SQLiteConnector({"database": str(path)})


@pytest.fixture
def kitchen(tmp_path):
    c = make_db(tmp_path, KITCHEN_DDL, KITCHEN_ROWS, "INSERT INTO kitchen VALUES (" + ",".join("?" * 13) + ")")
    yield c
    c.dispose()


def prof(conn, table="kitchen", **opts):
    tp = profile_table(conn, table, ProfileOptions(**opts))
    return tp, {c.name: c for c in tp.columns}


def val(col, metric):
    return col.metrics[metric].value


# ------------------------------------------------------------------ metrics
def test_table_level(kitchen):
    tp, _ = prof(kitchen)
    assert tp.row_count == 6 and tp.column_count == 13
    assert tp.primary_key == ["id"] and tp.pk_duplicate_key_count.value == 0


def test_null_metrics_all_columns(kitchen):
    _, c = prof(kitchen)
    assert (val(c["qty"], "null_count"), val(c["qty"], "non_null_count")) == (1, 5)
    assert val(c["qty"], "null_pct") == pytest.approx(100 / 6)
    assert val(c["price"], "null_count") == 4


def test_numeric_metrics_with_values(kitchen):
    _, c = prof(kitchen, include_values=True)
    q = c["qty"]
    assert (val(q, "min"), val(q, "max"), val(q, "sum")) == (-5, 20, 35)
    assert val(q, "mean") == pytest.approx(7.0)
    assert val(q, "stddev") == pytest.approx(math.sqrt(95))  # sample stddev of 10,0,-5,10,20
    assert (val(q, "zero_count"), val(q, "negative_count")) == (1, 1)
    assert val(q, "distinct_count") == 4 and val(q, "is_unique") is False
    assert val(q, "distinct_pct") == pytest.approx(80.0)
    assert val(c["id"], "is_unique") is True
    p = c["price"]
    assert (float(val(p, "min")), float(val(p, "max"))) == (1.5, 2.5)
    assert val(c["ratio"], "mean") == pytest.approx(1.0)


def test_string_metrics(kitchen):
    _, c = prof(kitchen)
    n = c["name"]
    assert (val(n, "min_length"), val(n, "max_length")) == (0, 8)
    assert val(n, "avg_length") == pytest.approx(4.2)  # (5+3+0+8+5)/5
    assert (val(n, "empty_count"), val(n, "whitespace_padded_count")) == (1, 1)
    assert val(n, "distinct_count") == 4


def test_top_values_ordering_and_counts(kitchen):
    _, c = prof(kitchen, include_values=True, top_n=3)
    tv = val(c["name"], "top_values")
    # count desc, then value asc
    assert tv == [{"value": "alice", "count": 2}, {"value": "", "count": 1}, {"value": " padded ", "count": 1}]


def test_top_values_truncates_long_strings(tmp_path):
    conn = make_db(tmp_path, "CREATE TABLE t (s TEXT)", [("x" * 500,)], "INSERT INTO t VALUES (?)")
    _, c = prof(conn, "t", include_values=True, max_value_chars=10)
    assert val(c["s"], "top_values")[0]["value"] == "x" * 10 + "…"


def test_top_values_skipped_for_high_cardinality(tmp_path):
    conn = make_db(tmp_path, "CREATE TABLE t (k INTEGER)", [(i,) for i in range(30)], "INSERT INTO t VALUES (?)")
    _, c = prof(conn, "t", include_values=True, top_values_max_distinct=10)
    assert "top_values" not in c["k"].metrics and "high cardinality" in c["k"].skipped["top_values"]


def test_temporal_metrics(kitchen):
    _, c = prof(kitchen, include_values=True)
    assert val(c["born"], "future_count") == 1
    assert str(val(c["born"], "min")) == "2000-01-01" and str(val(c["born"], "max")) == "2999-01-01"
    assert val(c["created"], "future_count") == 1
    assert str(val(c["created"], "min")).startswith("2020-01-01")


def test_boolean_metrics(kitchen):
    _, c = prof(kitchen)
    assert (val(c["flag"], "true_count"), val(c["flag"], "false_count"), val(c["flag"], "null_count")) == (1, 1, 4)


def test_binary_metrics(kitchen):
    _, c = prof(kitchen)
    b = c["blobby"]
    assert (val(b, "min_length"), val(b, "max_length")) == (1, 2)


def test_json_and_unknown_get_counts_only(kitchen):
    _, c = prof(kitchen, include_values=True)
    for name in ("payload", "untyped"):
        col = c[name]
        assert set(col.metrics) == {"null_count", "non_null_count", "null_pct"}
        assert "distinct_count" in col.skipped and col.status == "ok"
    assert val(c["payload"], "non_null_count") == 2
    assert c["untyped"].canonical.kind.value == "unknown"


def test_canonical_type_attached(kitchen):
    _, c = prof(kitchen)
    assert c["price"].canonical.kind.value == "decimal" and c["price"].canonical.scale == 2
    assert c["id"].canonical.bits == 64


# ------------------------------------------------------------------ empty / all-null
def test_empty_table(tmp_path):
    conn = make_db(tmp_path, "CREATE TABLE e (id INTEGER PRIMARY KEY, n INTEGER, s TEXT, d DATE)")
    tp, c = prof(conn, "e", include_values=True)
    assert tp.row_count == 0
    for col in c.values():
        assert val(col, "null_count") == 0 and val(col, "null_pct") is None
        assert col.status == "ok"
    assert val(c["n"], "min") is None and val(c["n"], "stddev") is None
    assert val(c["n"], "distinct_pct") is None and val(c["n"], "is_unique") is None
    assert val(c["s"], "top_values") == []
    json.dumps(tp.to_dict())


def test_all_null_column(tmp_path):
    conn = make_db(tmp_path, "CREATE TABLE a (id INTEGER PRIMARY KEY, n INTEGER, s TEXT)",
                   [(1, None, None), (2, None, None)], "INSERT INTO a VALUES (?,?,?)")
    _, c = prof(conn, "a", include_values=True)
    assert val(c["n"], "null_pct") == 100.0 and val(c["n"], "non_null_count") == 0
    assert val(c["n"], "min") is None and val(c["n"], "distinct_count") == 0
    assert val(c["n"], "distinct_pct") is None and val(c["s"], "avg_length") is None


def test_missing_table_raises_connector_error(kitchen):
    with pytest.raises(ConnectorError):
        profile_table(kitchen, "nope")


# ------------------------------------------------------------------ privacy
def test_redacted_by_default_values_never_computed(kitchen):
    tp, c = prof(kitchen)
    q = c["qty"]
    for m in ("min", "max", "mean", "sum", "stddev"):
        assert m not in q.metrics
    assert q.redacted == ["min", "max", "mean", "sum", "stddev", "top_values"]
    assert "top_values" not in c["name"].metrics and "top_values" in c["name"].redacted
    assert c["born"].redacted == ["min", "max"]
    assert "future_count" in c["born"].metrics  # a count, not a value
    blob = json.dumps(tp.to_dict()) + repr(tp)
    assert SECRET not in blob


def test_include_values_exposes_values(kitchen):
    tp, _ = prof(kitchen, include_values=True)
    assert SECRET in json.dumps(tp.to_dict())  # proves the redaction test above is meaningful


def test_sensitive_columns_counts_only_even_with_include_values(kitchen):
    tp, c = prof(kitchen, include_values=True)
    pw = c["password"]
    assert pw.sensitive and set(pw.metrics) == {"null_count", "non_null_count", "null_pct"}
    assert val(pw, "non_null_count") == 2
    assert "sensitive" in pw.skipped["*"]
    assert "hunter2" not in json.dumps(tp.to_dict())


def test_custom_sensitive_patterns(kitchen):
    _, c = prof(kitchen, sensitive_substrings=("note",), sensitive_tokens=())
    assert c["note"].sensitive and not c["password"].sensitive


@pytest.mark.parametrize("name,expected", [
    ("password", True), ("user_password", True), ("PASSWD", True), ("apiKey", True), ("api_key", True),
    ("ssn", True), ("SSN_number", True), ("pin", True), ("userPin", True), ("auth_token", True),
    ("credit_card_number", True), ("pwd", True),
    ("shipping", False), ("pincode", False), ("description", False), ("spinning", False), ("name", False),
])
def test_sensitive_name_detection(name, expected):
    assert is_sensitive_column(name, ProfileOptions()) is expected


def test_error_messages_never_contain_row_values(tmp_path):
    # A DATE column holding unparseable text makes SQLAlchemy raise ValueError quoting the value.
    conn = make_db(tmp_path, "CREATE TABLE b (id INTEGER PRIMARY KEY, d DATE, n INTEGER)",
                   [(1, "BADVALUE-" + SECRET, 7)], "INSERT INTO b VALUES (?,?,?)")
    tp, c = prof(conn, "b", include_values=True)
    assert c["d"].errors, "expected the bad date column to fail"
    assert SECRET not in json.dumps(tp.to_dict()) and SECRET not in repr(tp)
    assert val(c["n"], "max") == 7  # neighbour unaffected


# ------------------------------------------------------------------ failure isolation
def test_bad_column_in_batch_does_not_sink_others(tmp_path):
    conn = make_db(tmp_path, "CREATE TABLE b (a INTEGER, d DATE, z INTEGER)", [(1, "garbage", 3), (2, None, 4)],
                   "INSERT INTO b VALUES (?,?,?)")
    tp, c = prof(conn, "b", include_values=True)  # one batch containing the bad column -> per-column fallback
    assert c["d"].status in ("failed", "partial") and "basic" in c["d"].errors
    assert c["a"].status == "ok" and val(c["a"], "max") == 2
    assert c["z"].status == "ok" and val(c["z"], "sum") == 7
    assert tp.row_count == 2


def test_distribution_failure_is_partial(kitchen, monkeypatch):
    real = _Executor.run

    def flaky(self, stmt):
        if "DISTINCT kitchen.name" in str(stmt):
            raise ProfilingQueryError("boom")
        return real(self, stmt)

    monkeypatch.setattr(_Executor, "run", flaky)
    _, c = prof(kitchen)
    n = c["name"]
    assert n.status == "partial" and n.errors == {"distribution": "boom"} and "null_count" in n.metrics
    assert c["qty"].status == "ok"


def test_timeout_is_enforced_and_sanitised(kitchen):
    ex = _Executor(kitchen.engine(), timeout_ms=50)
    import sqlalchemy as sa

    endless = sa.text("WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM c) SELECT count(*) FROM c")
    t0 = time.monotonic()
    with pytest.raises(ProfilingQueryError, match="timed out"):
        ex.run(endless)
    assert time.monotonic() - t0 < 5


def test_executor_recovers_after_timeout(kitchen):
    import sqlalchemy as sa

    ex = _Executor(kitchen.engine(), timeout_ms=50)
    with pytest.raises(ProfilingQueryError):
        ex.run(sa.text("WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM c) SELECT count(*) FROM c"))
    assert ex.run(sa.text("SELECT 1 AS one"))[0]["one"] == 1  # handler was cleared


def test_row_count_failure_raises_connector_error(kitchen, monkeypatch):
    def boom(self, stmt):
        raise ProfilingQueryError("query timed out")

    monkeypatch.setattr(_Executor, "run", boom)
    with pytest.raises(ConnectorError, match="Cannot profile"):
        profile_table(kitchen, "kitchen")


def test_pk_check_error_is_not_fatal(kitchen, monkeypatch):
    def boom(ex, tbl, pk):
        raise ProfilingQueryError("query timed out")

    monkeypatch.setattr(prof_mod, "pk_duplicate_key_count", boom)
    tp, c = prof(kitchen)
    assert tp.pk_check_error == "query timed out" and tp.pk_duplicate_key_count is None
    assert c["qty"].status == "ok"


def test_pk_duplicate_counter_detects_duplicates(tmp_path):
    # A declared PK can't hold duplicates, so exercise the counter on an unconstrained table.
    import sqlalchemy as sa

    conn = make_db(tmp_path, "CREATE TABLE d (a INTEGER, b INTEGER)", [(1, 1), (1, 1), (1, 2), (2, 2), (2, 2), (2, 2)],
                   "INSERT INTO d VALUES (?,?)")
    tbl = sa.table("d", sa.column("a"), sa.column("b"))
    ex = _Executor(conn.engine(), 5000)
    assert prof_mod.pk_duplicate_key_count(ex, tbl, ["a", "b"]) == 2  # keys (1,1) and (2,2)
    assert prof_mod.pk_duplicate_key_count(ex, tbl, ["a"]) == 2


def test_pk_check_skipped_on_huge_table(tmp_path):
    conn = make_db(tmp_path, "CREATE TABLE h (id INTEGER PRIMARY KEY)", [(i,) for i in range(20)], "INSERT INTO h VALUES (?)")
    tp, _ = prof(conn, "h", exact_distinct_max_rows=10, sample_size=5)
    assert tp.pk_duplicate_key_count is None and "exact_distinct_max_rows" in tp.pk_check_skipped


# ------------------------------------------------------------------ sampling / large tables
@pytest.fixture
def big(tmp_path):
    rows = [(i, f"cat{i % 5}", i % 7 if i % 10 else None) for i in range(300)]
    c = make_db(tmp_path, "CREATE TABLE big (k INTEGER PRIMARY KEY, cat VARCHAR(10), m INTEGER)", rows,
                "INSERT INTO big VALUES (?,?,?)")
    yield c
    c.dispose()


def test_small_table_is_exact_with_no_sample_size(big):
    _, c = prof(big, "big")  # defaults: 300 rows << 1M threshold
    m = c["k"].metrics["distinct_count"]
    assert (m.method, m.sample_size, m.value) == ("exact", None, 300)
    assert val(c["k"], "is_unique") is True


def test_large_table_distribution_is_sampled_and_labelled(big):
    tp, c = prof(big, "big", exact_distinct_max_rows=50, sample_size=40)
    d = c["k"].metrics["distinct_count"]
    assert d.method == "sampled" and d.sample_size == 40
    assert 0 < d.value <= 40
    assert c["cat"].metrics["avg_length"].method == "sampled"
    # uniqueness is never asserted from a sample
    assert "is_unique" not in c["k"].metrics and "exact distinct" in c["k"].skipped["is_unique"]
    # cheap aggregates stay exact full-scan values
    nc = c["m"].metrics["null_count"]
    assert (nc.method, nc.sample_size, nc.value) == ("exact", None, 30)
    assert tp.row_count == 300
    out = tp.to_dict()["columns"][0]["metrics"]["distinct_count"]
    assert out["method"] == "sampled" and out["sample_size"] == 40


def test_sampled_top_values_report_rows_examined(big):
    _, c = prof(big, "big", include_values=True, exact_distinct_max_rows=50, sample_size=40)
    tv = c["cat"].metrics["top_values"]
    assert tv.method == "sampled" and tv.sample_size == 40
    assert sum(x["count"] for x in tv.value) <= 40


def test_sample_not_used_when_sample_covers_table(big):
    _, c = prof(big, "big", exact_distinct_max_rows=50, sample_size=1000)
    assert c["k"].metrics["distinct_count"].method == "exact"


def test_defaults_on_a_larger_table_are_exact_and_fast(tmp_path):
    rows = [(i, i % 100) for i in range(30_000)]
    conn = make_db(tmp_path, "CREATE TABLE l (a INTEGER PRIMARY KEY, b INTEGER)", rows, "INSERT INTO l VALUES (?,?)")
    t0 = time.monotonic()
    tp, c = prof(conn, "l", include_values=True)
    assert time.monotonic() - t0 < 20
    assert tp.row_count == 30_000 and val(c["b"], "distinct_count") == 100 and val(c["a"], "is_unique") is True
    assert val(c["a"], "sum") == sum(range(30_000))


def test_wide_table_is_batched_and_fully_profiled(tmp_path):
    n = 120
    cols = ", ".join(f"c{i} INTEGER" for i in range(n))
    conn = make_db(tmp_path, f"CREATE TABLE w ({cols})", [tuple(range(n)), tuple(range(n))],
                   "INSERT INTO w VALUES (" + ",".join("?" * n) + ")")
    from sqlalchemy import event

    def count_statements(batch):
        seen = []
        eng = conn.engine()
        fn = lambda c, cur, stmt, *a: seen.append(stmt)  # noqa: E731
        event.listen(eng, "before_cursor_execute", fn)
        try:
            tp = profile_table(conn, "w", ProfileOptions(column_batch_size=batch))
        finally:
            event.remove(eng, "before_cursor_execute", fn)
        return tp, len(seen)

    tp50, n50 = count_statements(50)
    _, n1 = count_statements(1)
    assert tp50.column_count == n and all(c.status == "ok" for c in tp50.columns)
    assert n50 < n1


def test_options_validation():
    with pytest.raises(ValueError):
        ProfileOptions(sample_size=0)
    with pytest.raises(ValueError):
        ProfileOptions(column_batch_size=0)


# ------------------------------------------------------------------ serialisation
def test_to_dict_is_json_serialisable_and_has_no_sa_types(kitchen):
    tp, _ = prof(kitchen, include_values=True)
    s = json.dumps(tp.to_dict())
    assert "sa_type" not in s and "object at 0x" not in s
    assert json.loads(s)["columns"][0]["type"]["kind"] == "integer"


def test_sqlite_stddev_workaround_documented_noise(tmp_path):
    # Values near 1e9 with spread ~1: the one-pass SQLite formula loses precision. It must still be
    # non-negative and finite (clamped), and the limitation is documented in profiling/metrics.py.
    conn = make_db(tmp_path, "CREATE TABLE s (v REAL)", [(1e9 + x,) for x in (0.0, 1.0, 2.0)], "INSERT INTO s VALUES (?)")
    _, c = prof(conn, "s", include_values=True)
    sd = val(c["v"], "stddev")
    assert sd >= 0 and math.isfinite(sd)
    assert math.isclose(sd, 1.0, abs_tol=1.0)  # true value is 1.0; accuracy is NOT guaranteed here
