"""Step 4: persistence of profile runs and schema mappings (service level, real SQLite metadata DB)."""
import json
import sqlite3

import pytest
from sqlalchemy import select, text

from app.core import db as dbmod
from app.schemas.connection import ConnectionCreate
from app.schemas.mapping import ColumnPair, MappingCreate
from app.schemas.profiling import ProfileRunRequest
from app.services import connection_service as conn_svc
from app.services import mapping_service as map_svc
from app.services import profiling_service as prof_svc
from app.models.mapping import SchemaMapping
from app.models.profile import ProfileColumnResult, ProfileRun

SECRET = "TOPSECRET-VALUE-9f3a"


@pytest.fixture
def session():
    gen = dbmod.get_session()
    s = next(gen)
    yield s
    gen.close()


@pytest.fixture
def two_dbs(tmp_path):
    def mk(name, ddl, rows=(), ins=None):
        p = tmp_path / name
        c = sqlite3.connect(p)
        c.execute(ddl)
        if rows:
            c.executemany(ins, rows)
        c.commit()
        c.close()
        return str(p)

    src = mk("src.db", "CREATE TABLE customers (customer_id INTEGER PRIMARY KEY, name VARCHAR(50), password VARCHAR(50), note TEXT)",
             [(1, "Asha", "pw-" + SECRET, SECRET), (2, "Ravi", "pw2", "x"), (3, "Zed", None, "x")],
             "INSERT INTO customers VALUES (?,?,?,?)")
    tgt = mk("tgt.db", "CREATE TABLE customers (customer_id INTEGER PRIMARY KEY, name VARCHAR(80), note TEXT)")
    return src, tgt


def add_conn(session, name, role, path):
    return conn_svc.create_connection(session, ConnectionCreate(name=name, role=role, db_type="sqlite", database=path))


@pytest.fixture
def conns(session, two_dbs):
    return add_conn(session, "src", "source", two_dbs[0]), add_conn(session, "tgt", "target", two_dbs[1])


# ------------------------------------------------------------------ profile runs
def test_run_is_persisted_with_columns_in_order(session, conns):
    run, live = prof_svc.run_profile(session, conns[0], ProfileRunRequest(connection_id=conns[0].id, table="customers"), False)
    assert run.id and run.status == "completed" and run.row_count == 3 and run.column_count == 4
    session.expire_all()
    stored = session.get(ProfileRun, run.id)
    assert [c.column_name for c in stored.columns] == ["customer_id", "name", "password", "note"]
    assert stored.primary_key == ["customer_id"]
    assert stored.table_metrics["pk_duplicate_key_count"]["value"] == 0
    assert stored.options["include_values"] is False and stored.options["sample_size"] == 100_000


def test_persisted_json_round_trips_and_has_no_internal_objects(session, conns):
    run, _ = prof_svc.run_profile(session, conns[0], ProfileRunRequest(connection_id=conns[0].id, table="customers"), False)
    session.expire_all()
    stored = session.get(ProfileRun, run.id)
    blob = json.dumps(prof_svc.run_to_out(stored), default=str)
    assert "sa_type" not in blob and "object at 0x" not in blob
    name = next(c for c in stored.columns if c.column_name == "name")
    assert name.canonical["kind"] == "string" and name.canonical["length"] == 50
    assert name.metrics["distinct_count"]["value"] == 3


def test_values_never_persisted_even_when_enabled(session, conns):
    req = ProfileRunRequest(connection_id=conns[0].id, table="customers", include_values=True)
    run, live = prof_svc.run_profile(session, conns[0], req, allow_values=True)
    # the live (response) columns carry values ...
    assert any("top_values" in c["metrics"] for c in live) and SECRET in json.dumps(live, default=str)
    # ... the stored rows do not
    session.expire_all()
    rows = list(session.scalars(select(ProfileColumnResult)))
    dump = json.dumps([[r.metrics, r.redacted, r.skipped, r.errors, r.canonical] for r in rows], default=str)
    assert SECRET not in dump
    assert all(not ({"min", "max", "mean", "sum", "stddev", "top_values"} & set(r.metrics)) for r in rows)
    note = next(r for r in rows if r.column_name == "note")
    assert "top_values" in note.redacted and "min" not in note.metrics
    # raw SQL view of the metadata DB: the secret is not anywhere in profile tables
    raw = session.execute(text("SELECT group_concat(metrics) FROM profile_column_results")).scalar()
    assert SECRET not in (raw or "")


def test_include_values_blocked_unless_server_allows(session, conns):
    req = ProfileRunRequest(connection_id=conns[0].id, table="customers", include_values=True)
    with pytest.raises(prof_svc.ProfilingNotAllowed):
        prof_svc.run_profile(session, conns[0], req, allow_values=False)
    assert session.scalar(select(ProfileRun)) is None  # nothing recorded for a rejected request


def test_sensitive_column_persisted_counts_only(session, conns):
    run, _ = prof_svc.run_profile(session, conns[0], ProfileRunRequest(connection_id=conns[0].id, table="customers"), False)
    pw = next(c for c in run.columns if c.column_name == "password")
    assert pw.sensitive and set(pw.metrics) == {"null_count", "non_null_count", "null_pct"}


def test_failed_run_is_recorded_missing_table_is_not(session, conns, monkeypatch):
    from app.connectors.base import ConnectorError

    with pytest.raises(ConnectorError):
        prof_svc.run_profile(session, conns[0], ProfileRunRequest(connection_id=conns[0].id, table="nope"), False)
    assert session.scalar(select(ProfileRun)) is None  # client error: not recorded

    def boom(connector, table, opts):
        raise ConnectorError("Cannot profile 'customers': query timed out")

    monkeypatch.setattr(prof_svc, "profile_table", boom)
    with pytest.raises(ConnectorError):
        prof_svc.run_profile(session, conns[0], ProfileRunRequest(connection_id=conns[0].id, table="customers"), False)
    failed = session.scalar(select(ProfileRun))
    assert failed.status == "failed" and "timed out" in failed.error and failed.columns == []


def test_list_filter_paginate_get_delete(session, conns):
    for _ in range(3):
        prof_svc.run_profile(session, conns[0], ProfileRunRequest(connection_id=conns[0].id, table="customers"), False)
    prof_svc.run_profile(session, conns[1], ProfileRunRequest(connection_id=conns[1].id, table="customers"), False)
    items, total = prof_svc.list_runs(session, conns[0].id, None, 2, 0)
    assert total == 3 and len(items) == 2 and items[0].id > items[1].id  # newest first
    assert prof_svc.list_runs(session, conns[0].id, None, 2, 2)[0][0].id == 1
    assert prof_svc.list_runs(session, None, "customers", 50, 0)[1] == 4
    assert prof_svc.list_runs(session, None, "other", 50, 0)[1] == 0
    run = prof_svc.get_run(session, items[0].id)
    prof_svc.delete_run(session, run.id)
    assert session.scalar(select(ProfileColumnResult).where(ProfileColumnResult.run_id == run.id)) is None  # cascade
    with pytest.raises(prof_svc.ProfileRunNotFound):
        prof_svc.get_run(session, run.id)


def test_credentials_never_in_persisted_profile_data(session, two_dbs):
    c = conn_svc.create_connection(session, ConnectionCreate(
        name="withpw", role="source", db_type="sqlite", database=two_dbs[0], username="svc_user", password="Sup3rS3cretPw"))
    prof_svc.run_profile(session, c, ProfileRunRequest(connection_id=c.id, table="customers"), False)
    for tbl in ("profile_runs", "profile_column_results"):
        rows = session.execute(text(f"SELECT * FROM {tbl}")).all()
        assert "Sup3rS3cretPw" not in json.dumps([list(map(str, r)) for r in rows])


# ------------------------------------------------------------------ mappings
def mk_mapping(session, conns, **kw):
    base = dict(name="m1", source_connection_id=conns[0].id, source_table="customers",
                target_connection_id=conns[1].id, target_table="customers")
    base.update(kw)
    return map_svc.create_mapping(session, MappingCreate(**base))


def test_auto_mapping_persists_recommendation(session, conns):
    m = mk_mapping(session, conns, key_columns=["customer_id"])
    session.expire_all()
    stored = session.get(SchemaMapping, m.id)
    cols = {c["source_column"]: c for c in stored.config["columns"]}
    assert set(cols) == {"customer_id", "name", "note"} and "password" not in cols
    assert cols["name"]["origin"] == "auto" and 0.6 <= cols["name"]["confidence"] <= 1.0
    assert stored.key_columns == ["customer_id"] and stored.config["min_confidence"] == 0.6


def test_manual_mapping_and_duplicate_name(session, conns):
    m = mk_mapping(session, conns, columns=[ColumnPair(source_column="name", target_column="name")])
    assert m.config["columns"][0]["origin"] == "manual" and m.config["columns"][0]["confidence"] is None
    with pytest.raises(map_svc.DuplicateMapping):
        mk_mapping(session, conns)


@pytest.mark.parametrize("columns,key,code", [
    ([("ghost", "name")], [], "source_column_missing"),
    ([("name", "ghost")], [], "target_column_missing"),
    ([("name", "name"), ("name", "note")], [], "duplicate_source"),
    ([("name", "name"), ("note", "name")], [], "duplicate_target"),
    ([("name", "name")], ["customer_id"], "key_column_unmapped"),
])
def test_invalid_manual_mapping_rejected_and_not_saved(session, conns, columns, key, code):
    cols = [ColumnPair(source_column=s, target_column=t) for s, t in columns]
    with pytest.raises(map_svc.MappingInvalid) as ei:
        mk_mapping(session, conns, columns=cols, key_columns=key)
    assert code in {e.code for e in ei.value.report.errors}
    assert session.scalar(select(SchemaMapping)) is None


def test_empty_columns_and_no_auto_matches_rejected(session, conns, tmp_path):
    with pytest.raises(map_svc.MappingInvalid):
        mk_mapping(session, conns, columns=[])
    other = tmp_path / "other.db"
    c = sqlite3.connect(other)
    c.execute("CREATE TABLE unrelated (zzz INTEGER, qqq TEXT)")
    c.commit()
    c.close()
    oc = add_conn(session, "other", "target", str(other))
    with pytest.raises(map_svc.MappingInvalid, match="No columns could be mapped"):
        map_svc.create_mapping(session, MappingCreate(
            name="none", source_connection_id=conns[0].id, source_table="customers",
            target_connection_id=oc.id, target_table="unrelated"))
    assert session.scalar(select(SchemaMapping)) is None


def test_validate_detects_schema_drift(session, conns, two_dbs):
    m = mk_mapping(session, conns)
    assert map_svc.validate_mapping(session, m.id).valid
    c = sqlite3.connect(two_dbs[1])  # drop the target 'note' column by rebuilding the table
    c.executescript("DROP TABLE customers; CREATE TABLE customers (customer_id INTEGER PRIMARY KEY, name VARCHAR(80));")
    c.commit()
    c.close()
    r = map_svc.validate_mapping(session, m.id)
    assert not r.valid and "target_column_missing" in {e.code for e in r.errors}
    c = sqlite3.connect(two_dbs[1])
    c.execute("DROP TABLE customers")
    c.commit()
    c.close()
    r = map_svc.validate_mapping(session, m.id)
    assert not r.valid and r.errors[0].code == "target_table_missing"


def test_deleting_connection_removes_dependents(session, conns):
    prof_svc.run_profile(session, conns[0], ProfileRunRequest(connection_id=conns[0].id, table="customers"), False)
    mk_mapping(session, conns)
    conn_svc.delete_connection(session, conns[1].id)  # target of the mapping
    assert session.scalar(select(SchemaMapping)) is None
    assert session.scalar(select(ProfileRun)) is not None  # run belongs to the source connection
    conn_svc.delete_connection(session, conns[0].id)
    assert session.scalar(select(ProfileRun)) is None and session.scalar(select(ProfileColumnResult)) is None


def test_mapping_not_found(session):
    with pytest.raises(map_svc.MappingNotFound):
        map_svc.get_mapping(session, 999)
