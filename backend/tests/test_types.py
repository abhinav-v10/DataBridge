"""Type normalizer tests. Types are real reflected SQLAlchemy objects:
- SQLite: reflected from a real SQLite file via inspect().
- PostgreSQL: produced by PGDialect._reflect_type (SQLAlchemy's own reflection code) from the
  format_type strings pg_catalog returns. No server needed; live-server verification is listed
  as a limitation in the devlog."""
import json
import sqlite3
import warnings

import pytest
from sqlalchemy import create_engine, inspect
from sqlalchemy.dialects.postgresql.base import PGDialect
from sqlalchemy.dialects.postgresql.base import ReflectedDomain, ReflectedEnum

from app.connectors.base import ColumnInfo, TableSchema
from app.connectors.sqlite import SQLiteConnector
from app.connectors.types import Kind, normalize_type
from app.schemas.connection import TableSchemaOut

PG = PGDialect()
ENUMS = {
    ("mood",): ReflectedEnum(name="mood", schema="public", visible=True, labels=["sad", "ok", "happy"])
}
DOMAINS = {
    ("positive_int",): ReflectedDomain(
        name="positive_int", schema="public", visible=True, type="integer", nullable=True,
        default=None, collation=None, constraints=[{"name": "pos", "check": "CHECK (VALUE > 0)"}],
    ),
    ("money_d",): ReflectedDomain(
        name="money_d", schema="public", visible=True, type="numeric(12,2)", nullable=False,
        default=None, collation=None, constraints=[],
    ),
}


def pg(format_type):
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")  # unknown types warn and become NullType
        return PG._reflect_type(format_type, DOMAINS, ENUMS, "test")


def c(fmt):
    return normalize_type(pg(fmt), "postgresql")


PG_CASES = [
    ("smallint", dict(kind=Kind.INTEGER, bits=16)),
    ("integer", dict(kind=Kind.INTEGER, bits=32)),
    ("bigint", dict(kind=Kind.INTEGER, bits=64)),
    ("numeric(12,2)", dict(kind=Kind.DECIMAL, precision=12, scale=2)),
    ("numeric(5,0)", dict(kind=Kind.DECIMAL, precision=5, scale=0)),
    ("numeric", dict(kind=Kind.DECIMAL, precision=None, scale=None)),
    ("real", dict(kind=Kind.FLOAT, bits=32)),
    ("double precision", dict(kind=Kind.FLOAT, bits=64)),
    ("boolean", dict(kind=Kind.BOOLEAN)),
    ("character varying(50)", dict(kind=Kind.STRING, length=50)),
    ("character(3)", dict(kind=Kind.STRING, length=3)),
    ("character varying", dict(kind=Kind.STRING, length=None)),
    ("text", dict(kind=Kind.STRING, length=None)),
    ("bytea", dict(kind=Kind.BINARY)),
    ("date", dict(kind=Kind.DATE)),
    ("timestamp without time zone", dict(kind=Kind.TIMESTAMP, timezone=False)),
    ("timestamp with time zone", dict(kind=Kind.TIMESTAMP, timezone=True)),
    ("timestamp(3) with time zone", dict(kind=Kind.TIMESTAMP, timezone=True, precision=3)),
    ("timestamp(6) without time zone", dict(kind=Kind.TIMESTAMP, timezone=False, precision=6)),
    ("time without time zone", dict(kind=Kind.TIME, timezone=False)),
    ("time with time zone", dict(kind=Kind.TIME, timezone=True)),
    ("interval", dict(kind=Kind.INTERVAL)),
    ("uuid", dict(kind=Kind.UUID)),
    ("json", dict(kind=Kind.JSON)),
    ("jsonb", dict(kind=Kind.JSON)),
    ("mood", dict(kind=Kind.ENUM, enum_values=("sad", "ok", "happy"))),
    ("positive_int", dict(kind=Kind.INTEGER, bits=32, domain="positive_int")),
    ("money_d", dict(kind=Kind.DECIMAL, precision=12, scale=2, domain="money_d")),
    ("geometry", dict(kind=Kind.UNKNOWN)),
    ("some_unknown_type", dict(kind=Kind.UNKNOWN)),
]


@pytest.mark.parametrize("fmt,expected", PG_CASES, ids=[x[0] for x in PG_CASES])
def test_postgres_types(fmt, expected):
    ct = c(fmt)
    for attr, val in expected.items():
        assert getattr(ct, attr) == val, f"{fmt}: {attr}={getattr(ct, attr)!r}, expected {val!r}"


def test_postgres_arrays():
    ct = c("integer[]")
    assert ct.kind is Kind.ARRAY and ct.element.kind is Kind.INTEGER and ct.element.bits == 32

    ct = c("timestamp with time zone[]")
    assert ct.kind is Kind.ARRAY and ct.element.timezone is True

    ct = c("character varying(20)[]")
    assert ct.element.kind is Kind.STRING and ct.element.length == 20

    ct = c("numeric(10,3)[]")
    assert (ct.element.precision, ct.element.scale) == (10, 3)

    # PostgreSQL does not preserve dimensionality; SQLAlchemy yields a single ARRAY level.
    assert c("integer[][]").kind is Kind.ARRAY

    ct = c("mood[]")
    assert ct.element.kind is Kind.ENUM and ct.element.enum_values == ("sad", "ok", "happy")


def test_unknown_is_flagged_not_guessed():
    ct = c("geometry")
    assert ct.kind is Kind.UNKNOWN and not ct.is_known
    assert normalize_type(pg("geometry")).kind is Kind.UNKNOWN  # no dialect given
    # unknown array element stays unknown; the array itself is still an array
    assert c("geometry[]").kind is Kind.UNKNOWN  # SQLAlchemy returns NullType (no ARRAY) for unknown base


def test_domain_is_unwrapped_and_known():
    ct = c("money_d")
    assert ct.kind is Kind.DECIMAL and ct.domain == "money_d" and ct.is_known


# ---- SQLite: reflected from a real file ----
SQLITE_DDL = """
CREATE TABLE t (
  id INTEGER PRIMARY KEY, small SMALLINT, big BIGINT,
  name VARCHAR(50), code CHAR(3), body TEXT,
  amt NUMERIC(12,2), dec DECIMAL(5,1), plain_num NUMERIC,
  r REAL, d DOUBLE, dp DOUBLE PRECISION,
  flag BOOLEAN, born DATE, ts TIMESTAMP, dt DATETIME, tm TIME,
  blobby BLOB, j JSON, weird GEOMETRYISH, untyped
)"""
SQLITE_CASES = {
    "id": dict(kind=Kind.INTEGER, bits=64),
    "small": dict(kind=Kind.INTEGER, bits=16),
    "big": dict(kind=Kind.INTEGER, bits=64),
    "name": dict(kind=Kind.STRING, length=50),
    "code": dict(kind=Kind.STRING, length=3),
    "body": dict(kind=Kind.STRING, length=None),
    "amt": dict(kind=Kind.DECIMAL, precision=12, scale=2),
    "dec": dict(kind=Kind.DECIMAL, precision=5, scale=1),
    "plain_num": dict(kind=Kind.DECIMAL, precision=None, scale=None),
    "r": dict(kind=Kind.FLOAT, bits=64),  # SQLite REAL is always 8 bytes
    "d": dict(kind=Kind.FLOAT, bits=64),
    "dp": dict(kind=Kind.FLOAT, bits=64),
    "flag": dict(kind=Kind.BOOLEAN),
    "born": dict(kind=Kind.DATE),
    "ts": dict(kind=Kind.TIMESTAMP, timezone=False),
    "dt": dict(kind=Kind.TIMESTAMP, timezone=False),
    "tm": dict(kind=Kind.TIME, timezone=False),
    "blobby": dict(kind=Kind.BINARY),
    "j": dict(kind=Kind.JSON),
    # SQLite affinity rules: an unrecognised declared type name resolves to NUMERIC.
    "weird": dict(kind=Kind.DECIMAL, precision=None, scale=None),
    # Only a column with no declared type reflects as NullType -> UNKNOWN.
    "untyped": dict(kind=Kind.UNKNOWN),
}


@pytest.fixture
def sqlite_reflected(tmp_path):
    path = tmp_path / "t.db"
    con = sqlite3.connect(path)
    con.execute(SQLITE_DDL)
    con.commit()
    con.close()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        cols = inspect(create_engine(f"sqlite:///{path}")).get_columns("t")
    return {col["name"]: col["type"] for col in cols}


@pytest.mark.parametrize("col", list(SQLITE_CASES))
def test_sqlite_types(sqlite_reflected, col):
    ct = normalize_type(sqlite_reflected[col], "sqlite")
    for attr, val in SQLITE_CASES[col].items():
        assert getattr(ct, attr) == val, f"{col}: {attr}={getattr(ct, attr)!r}, expected {val!r}"


def test_integer_width_is_dialect_aware(sqlite_reflected):
    t = sqlite_reflected["id"]
    assert normalize_type(t, "sqlite").bits == 64
    assert normalize_type(t, "postgresql").bits == 32


def test_to_dict_is_json_safe():
    ct = c("timestamp(3) with time zone[]")
    d = json.loads(json.dumps(ct.to_dict()))
    assert d["kind"] == "array" and d["element"]["timezone"] is True


# ---- Internal sa_type field must never leak ----
def _col(sa_type):
    return ColumnInfo(name="x", data_type="INTEGER", nullable=True, sa_type=sa_type)


def test_sa_type_excluded_from_repr_and_eq():
    t = pg("integer")
    assert "sa_type" not in repr(_col(t))
    assert _col(t) == _col(None)


def test_columninfo_backward_compatible_construction():
    ci = ColumnInfo(name="a", data_type="TEXT", nullable=False)  # Phase 0 call shape
    assert ci.sa_type is None and ci.is_primary_key is False


def test_pydantic_output_ignores_internal_type():
    ts = TableSchema(name="t", columns=[_col(pg("integer"))])
    # Feed the full __dict__ (as Phase 0's endpoint did) so sa_type is present in the input.
    payload = {"name": ts.name, "columns": [dict(x.__dict__) for x in ts.columns], "primary_key": []}
    assert "sa_type" in payload["columns"][0]
    body = TableSchemaOut.model_validate(payload).model_dump_json()
    assert "sa_type" not in body
    assert set(json.loads(body)["columns"][0]) == {
        "name", "data_type", "nullable", "is_primary_key", "max_length", "precision", "scale"
    }


def test_api_response_shape_unchanged_and_has_no_internal_field(client, sample_db):
    r = client.post("/api/connections", json={
        "name": "s", "role": "source", "db_type": "sqlite", "database": sample_db})
    cid = r.json()["id"]
    resp = client.get(f"/api/connections/{cid}/schema/customers")
    assert resp.status_code == 200
    assert "sa_type" not in resp.text
    assert all(set(col) == {"name", "data_type", "nullable", "is_primary_key",
                            "max_length", "precision", "scale"} for col in resp.json()["columns"])


def test_connector_populates_sa_type_for_normalizer(sample_db):
    conn = SQLiteConnector({"database": sample_db})
    by = {x.name: x for x in conn.get_table_schema("customers").columns}
    ct = normalize_type(by["balance"].sa_type, conn.db_type)
    assert (ct.kind, ct.precision, ct.scale) == (Kind.DECIMAL, 12, 2)
    assert normalize_type(by["name"].sa_type, conn.db_type).length == 50
    conn.dispose()
