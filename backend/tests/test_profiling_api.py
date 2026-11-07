"""Step 5: profiling API."""
import json

import pytest

from tests.phase1_fixtures import DB_PASSWORD, SECRET, allow_values, api_conns, two_dbs  # noqa: F401

RUN_KEYS = {"id", "connection_id", "table_name", "status", "row_count", "column_count", "duration_ms", "created_at",
            "error", "include_values_requested", "values_in_response", "values_persisted", "options", "primary_key",
            "pk_duplicate_key_count", "pk_check_skipped", "pk_check_error", "columns"}
COL_KEYS = {"name", "type", "status", "sensitive", "metrics", "redacted", "skipped", "errors"}


def run(client, conn_id, table="customers", **kw):
    return client.post("/api/profiling/run", json={"connection_id": conn_id, "table": table, **kw})


def cols(body):
    return {c["name"]: c for c in body["columns"]}


def test_run_response_shape(client, api_conns):
    r = run(client, api_conns[0])
    assert r.status_code == 201, r.text
    b = r.json()
    assert set(b) == RUN_KEYS and all(set(c) == COL_KEYS for c in b["columns"])
    assert (b["status"], b["row_count"], b["column_count"]) == ("completed", 3, 4)
    assert b["values_in_response"] is False and b["values_persisted"] is False and b["include_values_requested"] is False
    assert b["primary_key"] == ["customer_id"] and b["pk_duplicate_key_count"]["value"] == 0
    name = cols(b)["name"]
    assert set(name["metrics"]["distinct_count"]) == {"value", "method", "sample_size"}
    assert name["type"]["kind"] == "string" and name["redacted"] == ["top_values"]
    assert "min" in cols(b)["customer_id"]["redacted"]


def test_get_matches_post_when_no_values(client, api_conns):
    posted = run(client, api_conns[0]).json()
    got = client.get(f"/api/profiling/runs/{posted['id']}").json()
    for k in ("columns", "row_count", "options", "primary_key", "status"):
        assert got[k] == posted[k]


def test_sensitive_columns_counts_only(client, api_conns):
    b = run(client, api_conns[0]).json()
    pw = cols(b)["password"]
    assert pw["sensitive"] and set(pw["metrics"]) == {"null_count", "non_null_count", "null_pct"}


def test_sampling_options_flow_through(client, api_conns):
    b = run(client, api_conns[0], sample_size=2, exact_distinct_max_rows=1).json()
    d = cols(b)["customer_id"]["metrics"]["distinct_count"]
    assert d["method"] == "sampled" and 0 < d["sample_size"] <= 2
    assert b["options"]["sample_size"] == 2


def test_list_filter_paginate(client, api_conns):
    ids = [run(client, api_conns[0]).json()["id"] for _ in range(3)]
    run(client, api_conns[1])
    r = client.get("/api/profiling/runs", params={"connection_id": api_conns[0], "limit": 2}).json()
    assert r["total"] == 3 and [i["id"] for i in r["items"]] == [ids[2], ids[1]]
    assert set(r["items"][0]) == {"id", "connection_id", "table_name", "status", "row_count", "column_count",
                                  "duration_ms", "created_at", "error"}
    assert client.get("/api/profiling/runs", params={"table": "customers", "offset": 3}).json()["total"] == 4
    assert client.get("/api/profiling/runs", params={"table": "nope"}).json() == {"total": 0, "items": []}
    assert client.get("/api/profiling/runs", params={"limit": 0}).status_code == 422
    assert client.get("/api/profiling/runs", params={"limit": 500}).status_code == 422


def test_get_and_delete_run(client, api_conns):
    rid = run(client, api_conns[0]).json()["id"]
    assert client.delete(f"/api/profiling/runs/{rid}").status_code == 204
    assert client.get(f"/api/profiling/runs/{rid}").status_code == 404
    assert client.delete(f"/api/profiling/runs/{rid}").status_code == 404
    assert client.get("/api/profiling/runs/99999").status_code == 404


def test_unknown_connection_and_table(client, api_conns):
    assert run(client, 9999).status_code == 404
    r = run(client, api_conns[0], table="no_such_table")
    assert r.status_code == 404 and "does not exist" in r.json()["detail"]


@pytest.mark.parametrize("body", [
    {"connection_id": 1}, {"connection_id": 1, "table": ""}, {"connection_id": 1, "table": "t", "bogus": 1},
    {"connection_id": "x", "table": "t"}, {"connection_id": 1, "table": "t", "sample_size": 0},
    {"connection_id": 1, "table": "t", "top_n": 1000}, {"connection_id": 1, "table": "t", "statement_timeout_ms": 5},
    {"connection_id": 1, "table": "x" * 300},
])
def test_request_validation(client, body):
    assert client.post("/api/profiling/run", json=body).status_code == 422


def test_unreachable_database_is_502_and_recorded(client, tmp_path):
    cid = client.post("/api/connections", json={
        "name": "bad", "role": "source", "db_type": "sqlite", "database": str(tmp_path / "no_dir" / "x.db")}).json()["id"]
    r = run(client, cid)
    assert r.status_code == 502
    runs = client.get("/api/profiling/runs", params={"connection_id": cid}).json()
    assert runs["total"] == 1 and runs["items"][0]["status"] == "failed" and runs["items"][0]["error"]


def test_include_values_forbidden_by_default(client, api_conns):
    r = run(client, api_conns[0], include_values=True)
    assert r.status_code == 403 and "DATABRIDGE_PROFILING_ALLOW_VALUES" in r.json()["detail"]
    assert client.get("/api/profiling/runs").json()["total"] == 0


def test_values_only_in_post_response_never_after(client, api_conns, allow_values):  # noqa: F811
    posted = run(client, api_conns[0], include_values=True)
    assert posted.status_code == 201
    b = posted.json()
    assert b["values_in_response"] is True and b["values_persisted"] is False
    assert cols(b)["note"]["metrics"]["top_values"]["value"][0]["value"] == "x"
    assert SECRET in posted.text
    got = client.get(f"/api/profiling/runs/{b['id']}")
    assert SECRET not in got.text and got.json()["values_in_response"] is False
    assert "top_values" not in cols(got.json())["note"]["metrics"] and "top_values" in cols(got.json())["note"]["redacted"]
    assert "min" not in cols(got.json())["customer_id"]["metrics"]
    # sensitive column stays counts-only even with values enabled
    assert "pw2" not in posted.text and "pw-" not in posted.text


def test_no_credentials_or_values_in_any_response(client, api_conns):
    texts = [run(client, api_conns[0]).text, client.get("/api/profiling/runs").text,
             client.get("/api/profiling/runs/1").text]
    blob = "\n".join(texts)
    assert DB_PASSWORD not in blob and "svc_user" not in blob and SECRET not in blob
    assert "password_enc" not in blob and "sa_type" not in blob


def test_logs_never_contain_secrets(client, api_conns, caplog):
    import logging

    with caplog.at_level(logging.DEBUG):
        run(client, api_conns[0])
        run(client, api_conns[0], table="no_such_table")
    text = caplog.text
    assert DB_PASSWORD not in text and SECRET not in text


def test_phase0_connection_endpoints_unchanged(client, api_conns):
    body = client.get(f"/api/connections/{api_conns[0]}/schema/customers").json()
    assert set(body) == {"name", "columns", "primary_key"}
    assert set(body["columns"][0]) == {"name", "data_type", "nullable", "is_primary_key", "max_length", "precision", "scale"}
    assert "customers" in client.get(f"/api/connections/{api_conns[0]}/tables").json()
    assert client.delete(f"/api/connections/{api_conns[0]}").status_code == 204
    assert client.get("/api/profiling/runs").json()["total"] == 0


def test_openapi_lists_phase1_endpoints(client):
    paths = client.get("/openapi.json").json()["paths"]
    for p in ("/api/profiling/run", "/api/profiling/runs", "/api/profiling/runs/{run_id}", "/api/schema-comparison",
              "/api/schema-mappings", "/api/schema-mappings/{mapping_id}", "/api/schema-mappings/{mapping_id}/validate"):
        assert p in paths
    json.dumps(paths)
