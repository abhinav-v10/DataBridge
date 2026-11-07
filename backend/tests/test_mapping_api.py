"""Step 5: schema comparison and mapping API."""
import sqlite3

import pytest

from tests.phase1_fixtures import DB_PASSWORD, api_conns, two_dbs  # noqa: F401


def compare(client, s, t, **kw):
    return client.post("/api/schema-comparison", json={
        "source_connection_id": s, "source_table": "customers", "target_connection_id": t,
        "target_table": "customers", **kw})


def create(client, s, t, **kw):
    return client.post("/api/schema-mappings", json={
        "name": "m", "source_connection_id": s, "source_table": "customers", "target_connection_id": t,
        "target_table": "customers", **kw})


def test_comparison_response(client, api_conns):
    r = compare(client, *api_conns)
    assert r.status_code == 200, r.text
    b = r.json()
    assert set(b) == {"source_table", "target_table", "min_confidence", "summary", "mappings", "unmapped_source",
                      "unmapped_target", "issues"}
    s = b["summary"]
    assert (s["mapped"], s["auto"], s["unmapped_source"], s["unmapped_target"]) == (3, 3, 1, 1)
    assert s["source_columns"] == 4 and s["target_columns"] == 4 and s["blocking_issues"] == 0
    assert s["ready"] is False  # 'password' has no target
    assert s["overall_confidence"] == pytest.approx((1.0 + 0.975 + 1.0) / 4, abs=0.001)
    by = {m["source_column"]: m for m in b["mappings"]}
    assert set(by["name"]) >= {"confidence", "name_score", "type_score", "structure_score", "type_compatibility",
                               "status", "source_type", "target_type", "issues"}
    assert by["name"]["type_compatibility"] == "compatible" and by["name"]["confidence"] == pytest.approx(0.975)
    assert by["customer_id"]["confidence"] == 1.0 and by["customer_id"]["status"] == "auto"
    assert b["unmapped_source"][0]["column"] == "password"
    tgt = b["unmapped_target"][0]
    assert tgt["column"] == "extra_required" and tgt["nullable"] is False
    assert tgt["issues"][0]["code"] == "required_target_unmapped"


def test_comparison_does_not_persist_anything(client, api_conns):
    compare(client, *api_conns)
    assert client.get("/api/schema-mappings").json() == []


def test_comparison_min_confidence_param(client, api_conns):
    b = compare(client, *api_conns, min_confidence=0.99).json()
    assert {m["source_column"] for m in b["mappings"]} == {"customer_id", "note"}  # name is 0.975
    assert any(u["column"] == "name" and u["suggestion"]["column"] == "name" for u in b["unmapped_source"])


def test_comparison_errors(client, api_conns):
    assert compare(client, 9999, api_conns[1]).status_code == 404
    r = client.post("/api/schema-comparison", json={
        "source_connection_id": api_conns[0], "source_table": "ghost", "target_connection_id": api_conns[1],
        "target_table": "customers"})
    assert r.status_code == 404 and "does not exist" in r.json()["detail"]
    assert compare(client, *api_conns, min_confidence=1.5).status_code == 422
    assert compare(client, *api_conns, bogus=1).status_code == 422
    assert client.post("/api/schema-comparison", json={}).status_code == 422


def test_mapping_lifecycle(client, api_conns):
    r = create(client, *api_conns, key_columns=["customer_id"])
    assert r.status_code == 201, r.text
    m = r.json()
    assert set(m) == {"id", "name", "source_connection_id", "target_connection_id", "source_table", "target_table",
                      "key_columns", "columns", "created_at"}
    assert {c["source_column"] for c in m["columns"]} == {"customer_id", "name", "note"}
    assert all(c["origin"] == "auto" and c["confidence"] >= 0.6 for c in m["columns"])
    assert client.get("/api/schema-mappings").json()[0]["id"] == m["id"]
    assert client.get(f"/api/schema-mappings/{m['id']}").json()["name"] == "m"
    v = client.post(f"/api/schema-mappings/{m['id']}/validate").json()
    assert v["valid"] is True and v["errors"] == [] and len(v["mappings"]) == 3
    assert any(w["code"] == "required_target_unmapped" for w in v["warnings"])
    assert client.delete(f"/api/schema-mappings/{m['id']}").status_code == 204
    assert client.get(f"/api/schema-mappings/{m['id']}").status_code == 404
    assert client.delete(f"/api/schema-mappings/{m['id']}").status_code == 404
    assert client.post(f"/api/schema-mappings/{m['id']}/validate").status_code == 404


def test_manual_mapping(client, api_conns):
    r = create(client, *api_conns, columns=[{"source_column": "name", "target_column": "note"}])
    assert r.status_code == 201
    c = r.json()["columns"][0]
    assert c["origin"] == "manual" and c["confidence"] is None
    v = client.post(f"/api/schema-mappings/{r.json()['id']}/validate").json()
    assert v["valid"] and v["mappings"][0]["type_compatibility"] == "compatible"  # varchar(50) -> text


def test_duplicate_name_409(client, api_conns):
    assert create(client, *api_conns).status_code == 201
    assert create(client, *api_conns).status_code == 409


def test_invalid_mapping_422_with_details(client, api_conns):
    r = create(client, *api_conns, columns=[{"source_column": "ghost", "target_column": "name"},
                                            {"source_column": "name", "target_column": "name"},
                                            {"source_column": "note", "target_column": "name"}])
    assert r.status_code == 422
    d = r.json()["detail"]
    codes = {e["code"] for e in d["errors"]}
    assert {"source_column_missing", "duplicate_target"} <= codes and d["message"]
    assert client.get("/api/schema-mappings").json() == []


@pytest.mark.parametrize("body", [
    {"columns": []}, {"columns": [{"source_column": "a"}]}, {"name": ""}, {"min_confidence": 2},
    {"key_columns": ["k"] * 100}, {"bogus": True},
])
def test_mapping_request_validation(client, api_conns, body):
    r = create(client, *api_conns, **body)
    assert r.status_code == 422


def test_mapping_unknown_connection_404(client, api_conns):
    assert create(client, 9999, api_conns[1]).status_code == 404


def test_validate_reports_drift_via_api(client, api_conns, two_dbs):
    mid = create(client, *api_conns).json()["id"]
    c = sqlite3.connect(two_dbs[1])
    c.executescript("DROP TABLE customers; CREATE TABLE customers (customer_id INTEGER PRIMARY KEY);")
    c.commit()
    c.close()
    v = client.post(f"/api/schema-mappings/{mid}/validate").json()
    assert v["valid"] is False and "target_column_missing" in {e["code"] for e in v["errors"]}


def test_deleting_connection_deletes_its_mappings(client, api_conns):
    mid = create(client, *api_conns).json()["id"]
    assert client.delete(f"/api/connections/{api_conns[1]}").status_code == 204
    assert client.get(f"/api/schema-mappings/{mid}").status_code == 404


def test_no_credentials_in_mapping_responses(client, api_conns):
    mid = create(client, *api_conns).json()["id"]
    blob = "\n".join([compare(client, *api_conns).text, client.get("/api/schema-mappings").text,
                      client.get(f"/api/schema-mappings/{mid}").text,
                      client.post(f"/api/schema-mappings/{mid}/validate").text])
    assert DB_PASSWORD not in blob and "svc_user" not in blob and "password_enc" not in blob and "sa_type" not in blob
