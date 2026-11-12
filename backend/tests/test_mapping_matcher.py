"""Name scoring, greedy one-to-one matching, confidence, and explicit-mapping validation."""
import sqlite3

import pytest

from app.connectors.sqlite import SQLiteConnector
from app.connectors.types import CanonicalType as T
from app.connectors.types import Kind as K
from app.mapping import compare_schemas, side_from_schema, validate_pairs
from app.mapping.matcher import SchemaSide, SideColumn, name_score, normalize_name


def col(name, canon, nullable=True, pk=False):
    return SideColumn(name, nullable, pk, canon)


INT = T(K.INTEGER, "int", bits=32)
BIG = T(K.INTEGER, "bigint", bits=64)
SMALL = T(K.INTEGER, "smallint", bits=16)
TXT = T(K.STRING, "text")
V50 = T(K.STRING, "varchar(50)", length=50)
UID = T(K.UUID, "uuid")
DEC = T(K.DECIMAL, "numeric", precision=12, scale=2)


def side(table, *cols):
    return SchemaSide(table, tuple(cols))


@pytest.mark.parametrize("a,b,expected", [
    ("name", "NAME", 1.0), ("customerId", "customer_id", 0.95), ("Customer-ID", "customer_id", 0.95),
    ("cust_id", "custid", 0.90), ("CustomerName", "customer_name", 0.95),
])
def test_name_score_exact_tiers(a, b, expected):
    assert name_score(a, b) == expected


def test_name_score_fuzzy_is_capped_and_ordered():
    close = name_score("cust_name", "customer_name")
    far = name_score("cust_name", "zip_code")
    assert 0.5 < close <= 0.85 and far < 0.5 and close > far
    assert name_score("a", "zzzz") < 0.5


def test_normalize_name():
    assert normalize_name("customerID") == "customer_id" or normalize_name("customerID") == "customerid"
    assert normalize_name(" Foo__Bar ") == "foo_bar"


def test_perfect_match_is_high_confidence_auto():
    src = side("s", col("id", INT, False, True), col("name", V50))
    tgt = side("t", col("id", INT, False, True), col("name", V50))
    r = compare_schemas(src, tgt)
    assert [m.status for m in r.mappings] == ["auto", "auto"]
    assert all(m.confidence == 1.0 for m in r.mappings)
    s = r.summary
    assert (s["mapped"], s["unmapped_source"], s["overall_confidence"], s["ready"]) == (2, 0, 1.0, True)


def test_confidence_formula_components():
    src = side("s", col("amount", DEC))
    tgt = side("t", col("amount", T(K.DECIMAL, "n", precision=10, scale=2)))  # lossy, both non-pk
    m = compare_schemas(src, tgt).mappings[0]
    assert m.name_score == 1.0 and m.type_score == 0.5 and m.structure_score == 1.0
    assert m.confidence == pytest.approx(0.65 * 1.0 + 0.25 * 0.5 + 0.10 * 1.0)
    assert m.status == "review" and m.type_compatibility == "lossy"
    assert any(i.code == "type_lossy" for i in m.issues)


def test_renamed_columns_are_matched_with_review():
    src = side("s", col("cust_name", V50), col("zip", V50))
    tgt = side("t", col("customer_name", V50), col("postal", V50))
    r = compare_schemas(src, tgt)
    assert [(m.source_column, m.target_column) for m in r.mappings] == [("cust_name", "customer_name")]
    assert r.mappings[0].status == "review"
    assert [u.column for u in r.unmapped_source] == ["zip"]
    assert [u.column for u in r.unmapped_target] == ["postal"]
    assert not r.summary["ready"]


def test_greedy_is_one_to_one_and_prefers_best_pair():
    src = side("s", col("name", TXT), col("name2", TXT))
    tgt = side("t", col("name", TXT))
    r = compare_schemas(src, tgt)
    assert len(r.mappings) == 1 and r.mappings[0].source_column == "name"  # exact beats fuzzy for the single target
    assert [u.column for u in r.unmapped_source] == ["name2"]


def test_each_target_used_once_many_to_one_is_never_produced():
    src = side("s", *[col(f"col_{i}", TXT) for i in range(4)])
    tgt = side("t", col("col_1", TXT), col("col_2", TXT))
    r = compare_schemas(src, tgt)
    targets = [m.target_column for m in r.mappings]
    sources = [m.source_column for m in r.mappings]
    assert len(set(targets)) == len(targets) and len(set(sources)) == len(sources)
    assert set(zip(sources, targets)) == {("col_1", "col_1"), ("col_2", "col_2")}


def test_type_alone_never_pairs_unrelated_columns():
    src = side("s", col("alpha", INT))
    tgt = side("t", col("zzzzzz", INT))
    r = compare_schemas(src, tgt)
    assert r.mappings == [] and r.unmapped_source[0].suggestion is None


def test_min_confidence_threshold_rejects_weak_pairs():
    src = side("s", col("cust_name", V50))
    tgt = side("t", col("customer_name", V50))
    assert len(compare_schemas(src, tgt, min_confidence=0.6).mappings) == 1
    strict = compare_schemas(src, tgt, min_confidence=0.95)
    assert strict.mappings == []
    assert strict.unmapped_source[0].suggestion["column"] == "customer_name"  # suggestion still offered
    with pytest.raises(ValueError):
        compare_schemas(src, tgt, min_confidence=1.5)


def test_incompatible_same_name_is_conflict_and_blocks():
    src = side("s", col("id", UID, False, True))
    tgt = side("t", col("id", INT, False, True))
    r = compare_schemas(src, tgt)
    m = r.mappings[0]
    assert m.status == "conflict" and m.type_compatibility == "incompatible"
    assert any(i.severity == "error" for i in m.issues)
    assert r.summary["blocking_issues"] == 1 and r.summary["ready"] is False


def test_unknown_type_is_review_never_auto():
    src = side("s", col("geo", T(K.UNKNOWN, "geometry")))
    tgt = side("t", col("geo", T(K.UNKNOWN, "geometry")))
    m = compare_schemas(src, tgt).mappings[0]
    assert m.status == "review" and m.type_compatibility == "unverifiable"


def test_nullability_and_pk_warnings():
    src = side("s", col("id", INT, True, False))
    tgt = side("t", col("id", INT, False, True))
    codes = {i.code for i in compare_schemas(src, tgt).mappings[0].issues}
    assert {"nullability", "primary_key"} <= codes


def test_unmapped_required_target_column_warns():
    src = side("s", col("a", TXT))
    tgt = side("t", col("a", TXT), col("created_by", TXT, nullable=False))
    r = compare_schemas(src, tgt)
    assert r.unmapped_target[0].issues[0].code == "required_target_unmapped"
    assert r.summary["ready"] is True  # a warning, not a blocker: it may have a default


def test_table_level_pk_issues():
    src = side("s", col("id", INT, False, True), col("x", TXT))
    no_pk_target = side("t", col("x", TXT))
    assert "pk_unmapped" in {i.code for i in compare_schemas(src, no_pk_target).issues}
    none = compare_schemas(side("s", col("x", TXT)), side("t", col("x", TXT)))
    assert "no_primary_key" in {i.code for i in none.issues}
    moved = compare_schemas(side("s", col("id", INT, False, True), col("k", INT)),
                            side("t", col("id", INT), col("k", INT, False, True)))
    assert "pk_mismatch" in {i.code for i in moved.issues}


def test_deterministic_ordering():
    src = side("s", *[col(f"c{i}", TXT) for i in range(6)])
    tgt = side("t", *reversed([col(f"c{i}", TXT) for i in range(6)]))
    a = compare_schemas(src, tgt).to_dict()
    b = compare_schemas(src, tgt).to_dict()
    assert a == b and [m["source_column"] for m in a["mappings"]] == [f"c{i}" for i in range(6)]


def test_empty_sides():
    r = compare_schemas(side("s"), side("t"))
    assert r.mappings == [] and r.summary["overall_confidence"] == 0.0


def test_large_schema_is_fast_enough():
    import time

    src = side("s", *[col(f"source_column_{i}", TXT) for i in range(300)])
    tgt = side("t", *[col(f"target_field_{i}", TXT) for i in range(300)])
    t0 = time.monotonic()
    compare_schemas(src, tgt)
    assert time.monotonic() - t0 < 5


def test_to_dict_has_no_non_json_objects():
    import json

    src = side("s", col("id", INT, False, True))
    json.dumps(compare_schemas(src, src).to_dict())


# ---------------------------------------------------------------- validate_pairs
@pytest.fixture
def sides():
    src = side("s", col("id", INT, False, True), col("name", V50), col("uid", UID))
    tgt = side("t", col("id", BIG, False, True), col("full_name", V50), col("n", INT), col("req", TXT, nullable=False))
    return src, tgt


def test_validate_ok_with_warnings(sides):
    src, tgt = sides
    r = validate_pairs(src, tgt, [("id", "id"), ("name", "full_name")], key_columns=["id"])
    assert r.valid and not r.errors
    assert any(w.code == "required_target_unmapped" for w in r.warnings)
    assert [m.source_column for m in r.mappings] == ["id", "name"]


def test_validate_reports_missing_duplicates_and_incompatible(sides):
    src, tgt = sides
    r = validate_pairs(src, tgt, [("ghost", "id"), ("id", "nope"), ("name", "full_name"), ("name", "n"),
                                  ("uid", "n")])
    codes = {e.code for e in r.errors}
    assert {"source_column_missing", "target_column_missing", "duplicate_source", "duplicate_target",
            "type_incompatible"} <= codes
    assert not r.valid


def test_validate_key_columns(sides):
    src, tgt = sides
    r = validate_pairs(src, tgt, [("name", "full_name")], key_columns=["id", "missing"])
    assert {e.code for e in r.errors} == {"key_column_unmapped", "key_column_missing"}


def test_validate_lossy_is_warning_not_error(sides):
    src, tgt = sides
    r = validate_pairs(src, tgt, [("name", "n")])  # text -> int: lossy
    assert r.valid and any(w.code == "type_lossy" for w in r.warnings)


# ---------------------------------------------------------------- from real reflected schemas
def test_side_from_real_sqlite_schemas(tmp_path):
    def mk(name, ddl):
        p = tmp_path / name
        c = sqlite3.connect(p)
        c.execute(ddl)
        c.commit()
        c.close()
        return SQLiteConnector({"database": str(p)})

    a = mk("a.db", "CREATE TABLE customers (customer_id INTEGER PRIMARY KEY, name VARCHAR(50) NOT NULL, balance NUMERIC(12,2))")
    b = mk("b.db", "CREATE TABLE client (clientId INTEGER PRIMARY KEY, name VARCHAR(30) NOT NULL, balance NUMERIC(12,2), note TEXT)")
    try:
        s = side_from_schema(a.get_table_schema("customers"), "sqlite")
        t = side_from_schema(b.get_table_schema("client"), "sqlite")
        r = compare_schemas(s, t)
        by_s = {m.source_column: m for m in r.mappings}
        assert by_s["name"].type_compatibility == "lossy"  # varchar(50) -> varchar(30)
        assert by_s["balance"].status == "auto"
        # customer_id vs clientId is too dissimilar by name to pair; the matcher declines rather than guesses,
        # and reports both sides as unmapped (a primary-key issue is raised for the user to resolve).
        assert "customer_id" not in by_s
        assert sorted(u.column for u in r.unmapped_target) == ["clientId", "note"]
        assert [u.column for u in r.unmapped_source] == ["customer_id"]
        assert "pk_unmapped" in {i.code for i in r.issues}
    finally:
        a.dispose()
        b.dispose()
