import pytest

from app.core import security


def _payload(path, name="src", role="source"):
    return {"name": name, "role": role, "db_type": "sqlite", "database": path}


def test_create_list_and_no_password_leak(client, sample_db):
    body = _payload(sample_db) | {"password": "hunter2"}
    r = client.post("/api/connections", json=body)
    assert r.status_code == 201
    assert "password" not in r.text and "hunter2" not in r.text
    listing = client.get("/api/connections")
    assert [c["name"] for c in listing.json()] == ["src"]
    assert "hunter2" not in listing.text


def test_password_is_encrypted_at_rest(client, sample_db):
    from app.core.db import get_session
    from app.models.connection import Connection

    client.post("/api/connections", json=_payload(sample_db) | {"password": "hunter2"})
    s = next(get_session())
    stored = s.query(Connection).one().password_enc
    assert stored and stored != "hunter2"
    assert security.decrypt_secret(stored) == "hunter2"


def test_duplicate_name_conflict(client, sample_db):
    assert client.post("/api/connections", json=_payload(sample_db)).status_code == 201
    assert client.post("/api/connections", json=_payload(sample_db)).status_code == 409


def test_unsupported_db_type_rejected(client):
    r = client.post("/api/connections", json={"name": "x", "role": "source", "db_type": "oracle", "database": "d"})
    assert r.status_code == 422
    assert "Unsupported" in r.json()["detail"]


def test_validation_error_on_bad_role(client, sample_db):
    assert client.post("/api/connections", json=_payload(sample_db, role="middle")).status_code == 422


def test_test_endpoint_success_and_failure(client, sample_db, tmp_path):
    assert client.post("/api/connections/test", json=_payload(sample_db)).json()["ok"] is True
    bad = client.post("/api/connections/test", json=_payload(str(tmp_path / "nope" / "x.db"))).json()
    assert bad["ok"] is False and bad["message"]


def test_tables_and_schema(client, sample_db):
    cid = client.post("/api/connections", json=_payload(sample_db)).json()["id"]
    assert client.get(f"/api/connections/{cid}/tables").json() == ["accounts", "customers"]
    s = client.get(f"/api/connections/{cid}/schema/customers").json()
    assert s["primary_key"] == ["customer_id"]
    cols = {c["name"]: c for c in s["columns"]}
    assert cols["name"]["nullable"] is False and cols["name"]["max_length"] == 50
    assert cols["balance"]["precision"] == 12 and cols["balance"]["scale"] == 2


def test_unknown_table_and_connection_404(client, sample_db):
    cid = client.post("/api/connections", json=_payload(sample_db)).json()["id"]
    assert client.get(f"/api/connections/{cid}/schema/ghost").status_code == 404
    assert client.get("/api/connections/999/tables").status_code == 404


def test_delete(client, sample_db):
    cid = client.post("/api/connections", json=_payload(sample_db)).json()["id"]
    assert client.delete(f"/api/connections/{cid}").status_code == 204
    assert client.get("/api/connections").json() == []


def _change_secret_key(monkeypatch, value):
    from app.core import config

    monkeypatch.setenv("DATABRIDGE_SECRET_KEY", value)
    config.get_settings.cache_clear()


@pytest.mark.parametrize("path", ["status", "tables", "schema/customers"])
def test_changed_secret_key_returns_safe_503(client, sample_db, monkeypatch, caplog, path):
    from cryptography.fernet import Fernet

    old_key = Fernet.generate_key().decode()
    _change_secret_key(monkeypatch, old_key)
    cid = client.post("/api/connections", json=_payload(sample_db) | {"password": "hunter2"}).json()["id"]

    new_key = Fernet.generate_key().decode()
    _change_secret_key(monkeypatch, new_key)
    with caplog.at_level("DEBUG"):
        r = client.get(f"/api/connections/{cid}/{path}")

    assert r.status_code == 503
    assert "encryption key" in r.json()["detail"]
    for leaked in ("hunter2", old_key, new_key, "gAAAA"):
        assert leaked not in r.text
        assert leaked not in caplog.text


def test_missing_secret_key_on_create_returns_safe_503(client, sample_db, monkeypatch):
    _change_secret_key(monkeypatch, "")
    r = client.post("/api/connections", json=_payload(sample_db) | {"password": "hunter2"})
    assert r.status_code == 503 and "hunter2" not in r.text


def test_invalid_secret_key_format_returns_safe_503(client, sample_db, monkeypatch):
    _change_secret_key(monkeypatch, "not-a-fernet-key")
    r = client.post("/api/connections", json=_payload(sample_db) | {"password": "hunter2"})
    assert r.status_code == 503
    assert "not-a-fernet-key" not in r.text and "hunter2" not in r.text


def test_passwordless_connection_unaffected_by_key_change(client, sample_db, monkeypatch):
    cid = client.post("/api/connections", json=_payload(sample_db)).json()["id"]
    from cryptography.fernet import Fernet

    _change_secret_key(monkeypatch, Fernet.generate_key().decode())
    assert client.get(f"/api/connections/{cid}/tables").status_code == 200


def test_missing_secret_key_is_loud(monkeypatch):
    monkeypatch.setenv("DATABRIDGE_SECRET_KEY", "")
    from app.core import config

    config.get_settings.cache_clear()
    with pytest.raises(security.SecretKeyError):
        security.encrypt_secret("x")
