"""Type compatibility (source -> target), table-driven."""
import pytest

from app.connectors.types import CanonicalType as T
from app.connectors.types import Kind as K
from app.mapping.compat import Level, compare_types

E, C, L, I, U = Level.EXACT, Level.COMPATIBLE, Level.LOSSY, Level.INCOMPATIBLE, Level.UNVERIFIABLE


def i(bits):
    return T(K.INTEGER, "int", bits=bits)


def f(bits):
    return T(K.FLOAT, "float", bits=bits)


def d(p=None, s=None):
    return T(K.DECIMAL, "numeric", precision=p, scale=s)


def st(n=None):
    return T(K.STRING, "varchar", length=n)


def by(n=None):
    return T(K.BINARY, "bytea", length=n)


def ts(tz=False, p=None):
    return T(K.TIMESTAMP, "ts", timezone=tz, precision=p)


def tm(tz=False):
    return T(K.TIME, "time", timezone=tz)


def en(*v):
    return T(K.ENUM, "enum", enum_values=tuple(v))


def arr(e):
    return T(K.ARRAY, "array", element=e)


def k(kind):
    return T(kind, kind.value)


CASES = [
    # integers
    (i(32), i(32), E), (i(16), i(32), C), (i(32), i(64), C), (i(64), i(32), L), (i(64), i(16), L),
    # floats
    (f(64), f(64), E), (f(32), f(64), C), (f(64), f(32), L),
    # decimals
    (d(12, 2), d(12, 2), E), (d(10, 2), d(12, 2), C), (d(10, 2), d(12, 4), C), (d(12, 2), d(10, 2), L),
    (d(12, 4), d(12, 2), L), (d(10, 2), d(10, 4), L),  # more scale, same precision -> fewer integer digits
    (d(), d(), E), (d(12, 2), d(), C), (d(), d(12, 2), L), (d(5, 0), d(5, 0), E),
    # strings / binary
    (st(50), st(50), E), (st(10), st(20), C), (st(20), st(10), L), (st(10), st(), C), (st(), st(10), L), (st(), st(), E),
    (by(10), by(5), L), (by(5), by(), C),
    # timestamps / time
    (ts(), ts(), E), (ts(True), ts(True), E), (ts(True), ts(False), L), (ts(False), ts(True), L),
    (ts(p=3), ts(p=6), C), (ts(p=6), ts(p=3), L), (ts(p=None), ts(p=6), E), (ts(True, 6), ts(False, 3), L),
    (tm(True), tm(False), L), (tm(), tm(), E),
    # enums
    (en("a", "b"), en("a", "b"), E), (en("a"), en("a", "b"), C), (en("a", "b"), en("a"), L),
    # arrays recurse
    (arr(i(32)), arr(i(32)), E), (arr(i(32)), arr(i(64)), C), (arr(i(64)), arr(i(32)), L),
    (arr(i(32)), arr(st()), L), (arr(k(K.UUID)), arr(i(32)), I),
    # same simple kinds
    (k(K.DATE), k(K.DATE), E), (k(K.BOOLEAN), k(K.BOOLEAN), E), (k(K.UUID), k(K.UUID), E), (k(K.JSON), k(K.JSON), E),
    # cross-kind
    (i(32), d(10, 0), C), (i(32), d(9, 0), L), (i(64), d(19, 0), C), (i(64), d(12, 2), L), (i(32), d(), C),
    (i(32), f(64), C), (i(64), f(64), L), (i(16), f(32), L),
    (d(10, 2), f(64), L), (f(64), d(10, 2), L), (d(10, 0), i(64), L), (f(64), i(64), L),
    (k(K.DATE), ts(), C), (ts(), k(K.DATE), L),
    (k(K.BOOLEAN), i(16), L), (i(16), k(K.BOOLEAN), L),
    (en("sad", "happy"), st(), C), (en("sad", "happy"), st(10), C), (en("sad", "happy"), st(4), L),
    (i(32), st(), L), (k(K.UUID), st(), L), (k(K.JSON), st(), L), (st(), i(32), L), (st(), k(K.UUID), L), (st(), ts(), L),
    (k(K.UUID), i(32), I), (k(K.BOOLEAN), k(K.DATE), I), (k(K.JSON), i(32), I), (k(K.DATE), k(K.UUID), I),
    (arr(i(32)), i(32), I), (k(K.INTERVAL), ts(), I),
    # unknown
    (k(K.UNKNOWN), i(32), U), (i(32), k(K.UNKNOWN), U), (k(K.UNKNOWN), k(K.UNKNOWN), U),
]


def _id(c):
    s, t, lvl = c
    return f"{s.kind.value}->{t.kind.value}:{lvl.value}"


@pytest.mark.parametrize("s,t,level", CASES, ids=[f"{n}-{_id(c)}" for n, c in enumerate(CASES)])
def test_compare(s, t, level):
    got = compare_types(s, t)
    assert got.level is level, f"{got.level} {got.reasons}"
    if level in (L, I, U):
        assert got.reasons, "non-lossless results must explain themselves"


def test_scores_are_ordered():
    order = [compare_types(i(32), i(32)).score, compare_types(i(32), i(64)).score, compare_types(i(64), i(32)).score,
             compare_types(k(K.UNKNOWN), i(32)).score, compare_types(k(K.UUID), i(32)).score]
    assert order == sorted(order, reverse=True) and order[0] == 1.0 and order[-1] == 0.0


def test_multiple_reasons_are_combined():
    got = compare_types(ts(True, 6), ts(False, 3))
    assert got.level is L and len(got.reasons) == 2


def test_decimal_reports_both_scale_and_overflow():
    got = compare_types(d(12, 4), d(8, 2))
    assert got.level is L and len(got.reasons) == 2
