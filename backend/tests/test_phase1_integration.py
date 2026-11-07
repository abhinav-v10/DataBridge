"""Step 6: end-to-end workflow and edge cases across profiling, comparison, mapping and persistence."""
import sqlite3

import pytest

from app.core import db as dbmod
from tests.phase1_fixtures import SECRET, api_conns, two_dbs  # noqa: F401


def post_conn(client, name, role, path):
    r = client.post("/api/connections", json={"name": name, "role": role, "db_type": "sqlite", "database": str(path)})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def make_db(tmp_path, name, *statements):
    p = tmp_path / name
    c = sqlite3.connect(p)
    for s in statements:
        c.execute(s)
    c.commit()
    c.close()
    return p


def profile(client, cid, table):
    return client.post("/api/profiling/run", json={"connection_id": cid, "table": table})


def compare(client, s, st, t, tt, **kw):
    return client.post("/api/schema-comparison", json={
        "source_connection_id": s, "source_table": st, "target_connection_id": t, "target_table": tt, **kw})


# ------------------------------------------------------------------ the full workflow
def test_end_to_end_workflow(client, api_conns):
    src, tgt = api_conns
    assert "customers" in client.get(f"/api/connections/{src}/tables").json()

    ps, pt = profile(client, src, "customers"), profile(client, tgt, "customers")
    assert ps.status_code == pt.status_code == 201
    assert ps.json()["row_count"] == 3 and pt.json()["row_count"] == 0

    cmp = compare(client, src, "customers", tgt, "customers").json()
    assert cmp["summary"]["mapped"] == 3 and cmp["summary"]["blocking_issues"] == 0

    m = client.post("/api/schema-mappings", json={
        "name": "customers_map", "source_connection_id": src, "source_table": "customers",
        "target_connection_id": tgt, "target_table": "customers", "key_columns": ["customer_id"]})
    assert m.status_code == 201
    mid = m.json()["id"]
    assert client.post(f"/api/schema-mappings/{mid}/validate").json()["valid"] is True

    listed = client.get("/api/profiling/runs", params={"table": "customers"}).json()
    assert listed["total"] == 2
    assert {i["connection_id"] for i in listed["items"]} == {src, tgt}

    # cleanup chain: deleting both connections leaves nothing behind
    for cid in (src, tgt):
        assert client.delete(f"/api/connections/{cid}").status_code == 204
    assert client.get("/api/profiling/runs").json()["total"] == 0
    assert client.get("/api/schema-mappings").json() == []


def test_results_survive_engine_restart(client, api_conns):
    rid = profile(client, api_conns[0], "customers").json()["id"]
    mid = client.post("/api/schema-mappings", json={
        "name": "m", "source_connection_id": api_conns[0], "source_table": "customers",
        "target_connection_id": api_conns[1], "target_table": "customers"}).json()["id"]
    dbmod.init_engine()  # new engine on the same metadata file = a server restart
    got = client.get(f"/api/profiling/runs/{rid}").json()
    assert got["row_count"] == 3 and len(got["columns"]) == 4
    assert client.get(f"/api/schema-mappings/{mid}").status_code == 200


# ------------------------------------------------------------------ SQL safety
HOSTILE_TABLE = 'we"ird; DROP TABLE customers; --'


def test_hostile_identifiers_are_quoted_not_executed(client, tmp_path):
    path = make_db(
        tmp_path, "h.db",
        "CREATE TABLE customers (id INTEGER PRIMARY KEY)",
        "INSERT INTO customers VALUES (1)",
        'CREATE TABLE "we""ird; DROP TABLE customers; --" (id INTEGER PRIMARY KEY, "a b" TEXT, "c\'d" INTEGER, '
        '"order" INTEGER, "x""; DELETE FROM customers; --" TEXT)',
        'INSERT INTO "we""ird; DROP TABLE customers; --" VALUES (1, \'v\', 2, 3, \'z\')',
    )
    cid = post_conn(client, "h", "source", path)
    r = profile(client, cid, HOSTILE_TABLE)
    assert r.status_code == 201, r.text
    names = [c["name"] for c in r.json()["columns"]]
    assert {"a b", "c'd", "order", 'x"; DELETE FROM customers; --'} <= set(names)
    assert all(c["status"] == "ok" for c in r.json()["columns"])
    # nothing was dropped or deleted
    con = sqlite3.connect(path)
    assert con.execute("SELECT count(*) FROM customers").fetchone()[0] == 1
    con.close()


def test_injection_in_table_parameter_is_just_a_missing_table(client, api_conns, two_dbs):
    r = profile(client, api_conns[0], "customers; DROP TABLE customers")
    assert r.status_code == 404
    r = compare(client, api_conns[0], "customers' OR '1'='1", api_conns[1], "customers")
    assert r.status_code == 404
    con = sqlite3.connect(two_dbs[0])
    assert con.execute("SELECT count(*) FROM customers").fetchone()[0] == 3
    con.close()


def test_unicode_and_mixed_case_names(client, tmp_path):
    a = make_db(tmp_path, "a.db", 'CREATE TABLE "Kunden" ("KundenId" INTEGER PRIMARY KEY, "Größe" TEXT, "名前" TEXT)',
                "INSERT INTO Kunden VALUES (1, 'x', 'y')")
    b = make_db(tmp_path, "b.db", 'CREATE TABLE "kunden" (kunden_id INTEGER PRIMARY KEY, "größe" TEXT, "名前" TEXT)')
    ca, cb = post_conn(client, "a", "source", a), post_conn(client, "b", "target", b)
    assert profile(client, ca, "Kunden").status_code == 201
    cmp = compare(client, ca, "Kunden", cb, "kunden").json()
    pairs = {(m["source_column"], m["target_column"]) for m in cmp["mappings"]}
    assert ("KundenId", "kunden_id") in pairs and ("Größe", "größe") in pairs and ("名前", "名前") in pairs


# ------------------------------------------------------------------ data edge cases
def test_empty_tables_end_to_end(client, tmp_path):
    p = make_db(tmp_path, "e.db", "CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
    cid = post_conn(client, "e", "source", p)
    b = profile(client, cid, "t").json()
    assert b["row_count"] == 0 and all(c["status"] == "ok" for c in b["columns"])
    assert b["columns"][1]["metrics"]["null_pct"]["value"] is None
    assert compare(client, cid, "t", cid, "t").json()["summary"]["overall_confidence"] == 1.0


def test_table_without_primary_key_and_composite_pk(client, tmp_path):
    p = make_db(tmp_path, "pk.db", "CREATE TABLE nopk (a INTEGER, b TEXT)",
                "CREATE TABLE comp (a INTEGER, b INTEGER, c TEXT, PRIMARY KEY (a, b))",
                "INSERT INTO comp VALUES (1, 1, 'x'), (1, 2, 'y')")
    cid = post_conn(client, "pk", "source", p)
    nopk = profile(client, cid, "nopk").json()
    assert nopk["primary_key"] == [] and nopk["pk_duplicate_key_count"] is None
    comp = profile(client, cid, "comp").json()
    assert comp["primary_key"] == ["a", "b"] and comp["pk_duplicate_key_count"]["value"] == 0
    cmp = compare(client, cid, "nopk", cid, "nopk").json()
    assert "no_primary_key" in {i["code"] for i in cmp["issues"]}


def test_all_null_and_unknown_type_columns(client, tmp_path):
    p = make_db(tmp_path, "n.db", "CREATE TABLE t (id INTEGER PRIMARY KEY, n INTEGER, untyped)",
                "INSERT INTO t VALUES (1, NULL, 5), (2, NULL, 6)")
    cid = post_conn(client, "n", "source", p)
    cols = {c["name"]: c for c in profile(client, cid, "t").json()["columns"]}
    assert cols["n"]["metrics"]["null_pct"]["value"] == 100.0
    assert cols["untyped"]["type"]["kind"] == "unknown" and "distinct_count" in cols["untyped"]["skipped"]
    m = {x["source_column"]: x for x in compare(client, cid, "t", cid, "t").json()["mappings"]}
    assert m["untyped"]["status"] == "review" and m["untyped"]["type_compatibility"] == "unverifiable"


def test_incompatible_types_block_readiness_and_mapping(client, tmp_path):
    a = make_db(tmp_path, "a.db", "CREATE TABLE t (id INTEGER PRIMARY KEY, flag BOOLEAN, d DATE)")
    b = make_db(tmp_path, "b.db", "CREATE TABLE t (id INTEGER PRIMARY KEY, flag DATE, d BOOLEAN)")
    ca, cb = post_conn(client, "a", "source", a), post_conn(client, "b", "target", b)
    cmp = compare(client, ca, "t", cb, "t").json()
    assert cmp["summary"]["conflict"] == 2 and cmp["summary"]["ready"] is False
    # auto-mapping skips conflicts; an explicit conflicting mapping is rejected with details
    ok = client.post("/api/schema-mappings", json={
        "name": "auto", "source_connection_id": ca, "source_table": "t", "target_connection_id": cb, "target_table": "t"})
    assert ok.status_code == 201 and [c["source_column"] for c in ok.json()["columns"]] == ["id"]
    bad = client.post("/api/schema-mappings", json={
        "name": "bad", "source_connection_id": ca, "source_table": "t", "target_connection_id": cb, "target_table": "t",
        "columns": [{"source_column": "flag", "target_column": "flag"}]})
    assert bad.status_code == 422 and any(e["code"] == "type_incompatible" for e in bad.json()["detail"]["errors"])


def test_wide_table_via_api(client, tmp_path):
    n = 150
    cols = ", ".join(f"col_{i} INTEGER" for i in range(n))
    p = make_db(tmp_path, "w.db", f"CREATE TABLE w ({cols})",
                "INSERT INTO w VALUES (" + ",".join(str(i) for i in range(n)) + ")")
    cid = post_conn(client, "w", "source", p)
    b = profile(client, cid, "w").json()
    assert b["column_count"] == n and all(c["status"] == "ok" for c in b["columns"])
    s = compare(client, cid, "w", cid, "w").json()["summary"]
    assert s["mapped"] == n and s["auto"] == n


def test_profile_of_larger_table_uses_sampling_when_configured(client, tmp_path):
    p = make_db(tmp_path, "big.db", "CREATE TABLE big (id INTEGER PRIMARY KEY, g INTEGER)")
    con = sqlite3.connect(p)
    con.executemany("INSERT INTO big VALUES (?,?)", [(i, i % 10) for i in range(5000)])
    con.commit()
    con.close()
    cid = post_conn(client, "big", "source", p)
    b = client.post("/api/profiling/run", json={
        "connection_id": cid, "table": "big", "exact_distinct_max_rows": 1000, "sample_size": 500}).json()
    c = {x["name"]: x for x in b["columns"]}
    d = c["g"]["metrics"]["distinct_count"]
    assert d["method"] == "sampled" and d["sample_size"] == 500 and d["value"] == 10
    assert c["g"]["metrics"]["null_count"]["method"] == "exact" and b["row_count"] == 5000
    assert "is_unique" in c["id"]["skipped"]


def test_secret_values_never_leave_through_any_phase1_endpoint(client, api_conns):
    mid = client.post("/api/schema-mappings", json={
        "name": "m", "source_connection_id": api_conns[0], "source_table": "customers",
        "target_connection_id": api_conns[1], "target_table": "customers"}).json()["id"]
    texts = [profile(client, api_conns[0], "customers").text, client.get("/api/profiling/runs/1").text,
             client.get("/api/profiling/runs").text, compare(client, api_conns[0], "customers", api_conns[1], "customers").text,
             client.get(f"/api/schema-mappings/{mid}").text, client.post(f"/api/schema-mappings/{mid}/validate").text]
    assert all(SECRET not in t and "pw2" not in t for t in texts)


def test_missing_encryption_key_gives_safe_503(client, api_conns, monkeypatch):
    from app.core import config

    monkeypatch.setenv("DATABRIDGE_SECRET_KEY", "")
    config.get_settings.cache_clear()
    # connection saved with a password needs decryption -> SecretKeyError -> fixed safe message
    r = profile(client, api_conns[0], "customers")
    assert r.status_code == 503 and "DATABRIDGE_SECRET_KEY" in r.json()["detail"]
    assert "svc_user" not in r.text
