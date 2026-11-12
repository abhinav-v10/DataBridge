"""Canonical, database-agnostic type model + normalizer.

The normalizer consumes the *actual reflected SQLAlchemy type objects* (not their string
rendering), so precision/scale/timezone/array/enum/domain information is read from the
structured attributes and never re-parsed from text.

CanonicalType is deliberately lossless about what we could not classify: unknown types become
Kind.UNKNOWN with `native` preserved, so later phases can refuse to hash/compare them rather
than silently guess.
"""
from dataclasses import dataclass
from enum import Enum

from sqlalchemy import types as sa


class Kind(str, Enum):
    INTEGER = "integer"
    DECIMAL = "decimal"
    FLOAT = "float"
    BOOLEAN = "boolean"
    STRING = "string"
    BINARY = "binary"
    DATE = "date"
    TIME = "time"
    TIMESTAMP = "timestamp"
    INTERVAL = "interval"
    UUID = "uuid"
    JSON = "json"
    ENUM = "enum"
    ARRAY = "array"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class CanonicalType:
    kind: Kind
    native: str  # native type name as the database reports it (informational)
    bits: int | None = None  # INTEGER: 16/32/64; FLOAT: 32/64
    precision: int | None = None  # DECIMAL precision; TIME/TIMESTAMP fractional-second digits
    scale: int | None = None
    length: int | None = None  # STRING/BINARY max length; None = unbounded
    timezone: bool | None = None  # TIME/TIMESTAMP only; None = not applicable
    element: "CanonicalType | None" = None  # ARRAY only
    enum_values: tuple[str, ...] | None = None  # ENUM only
    domain: str | None = None  # name of the PostgreSQL domain this type came through, if any

    @property
    def is_known(self) -> bool:
        return self.kind is not Kind.UNKNOWN

    def to_dict(self) -> dict:
        d = {"kind": self.kind.value, "native": self.native}
        for k in ("bits", "precision", "scale", "length", "timezone", "domain"):
            v = getattr(self, k)
            if v is not None:
                d[k] = v
        if self.enum_values is not None:
            d["enum_values"] = list(self.enum_values)
        if self.element is not None:
            d["element"] = self.element.to_dict()
        return d


def _native_name(t: sa.TypeEngine, dialect_name: str | None) -> str:
    """Native type rendering, e.g. 'NUMERIC(12, 2)'. Informational only; never parsed."""
    try:
        if dialect_name:
            from sqlalchemy.engine.url import URL

            return t.compile(dialect=URL.create(dialect_name).get_dialect()())
        return str(t)
    except Exception:
        return type(t).__name__


def normalize_type(t: sa.TypeEngine, dialect_name: str | None = None) -> CanonicalType:
    """Map a reflected SQLAlchemy type object to a CanonicalType.

    `dialect_name` ("postgresql", "sqlite", ...) is needed only where the same SQLAlchemy type
    means different things per database (SQLite INTEGER is a 64-bit affinity, PostgreSQL's is 32).
    """
    native = _native_name(t, dialect_name)

    # Unwrap PostgreSQL DOMAIN to its base type, remembering the domain name.
    domain_cls = _pg("DOMAIN")
    if domain_cls is not None and isinstance(t, domain_cls):
        inner = normalize_type(t.data_type, dialect_name)
        return _replace(inner, native=native, domain=t.name)

    # Order matters: check subclasses before their parents.
    array_cls = _pg("ARRAY") or sa.ARRAY
    if isinstance(t, (array_cls, sa.ARRAY)):
        return CanonicalType(Kind.ARRAY, native, element=normalize_type(t.item_type, dialect_name))

    if isinstance(t, sa.Enum):  # subclass of String; includes postgresql.ENUM
        return CanonicalType(Kind.ENUM, native, enum_values=tuple(t.enums))

    if isinstance(t, sa.Boolean):
        return CanonicalType(Kind.BOOLEAN, native)

    if isinstance(t, sa.Integer):  # SmallInteger and BigInteger subclass Integer
        return CanonicalType(Kind.INTEGER, native, bits=_int_bits(t, dialect_name))

    if isinstance(t, sa.Float):  # Float/REAL/DOUBLE subclass Numeric: check before Numeric
        return CanonicalType(Kind.FLOAT, native, bits=_float_bits(t, dialect_name))

    if isinstance(t, sa.Numeric):
        return CanonicalType(Kind.DECIMAL, native, precision=t.precision, scale=t.scale)

    if isinstance(t, sa.Interval):
        return CanonicalType(Kind.INTERVAL, native)
    pg_interval = _pg("INTERVAL")
    if pg_interval is not None and isinstance(t, pg_interval):
        return CanonicalType(Kind.INTERVAL, native)

    if isinstance(t, sa.DateTime):  # includes TIMESTAMP
        return CanonicalType(
            Kind.TIMESTAMP, native, precision=getattr(t, "precision", None), timezone=bool(t.timezone)
        )
    if isinstance(t, sa.Time):
        return CanonicalType(
            Kind.TIME, native, precision=getattr(t, "precision", None), timezone=bool(t.timezone)
        )
    if isinstance(t, sa.Date):
        return CanonicalType(Kind.DATE, native)

    if isinstance(t, sa.Uuid):  # also covers postgresql.UUID in SQLAlchemy 2.0
        return CanonicalType(Kind.UUID, native)

    if isinstance(t, sa.JSON):  # includes postgresql.JSON / JSONB
        return CanonicalType(Kind.JSON, native)

    if isinstance(t, sa._Binary):  # LargeBinary, BLOB, BYTEA
        return CanonicalType(Kind.BINARY, native, length=t.length)

    if isinstance(t, sa.String):  # VARCHAR/CHAR/TEXT/CLOB; length None = unbounded
        return CanonicalType(Kind.STRING, native, length=t.length)

    return CanonicalType(Kind.UNKNOWN, native)  # NullType, geometry, hstore, ...


def _replace(t: CanonicalType, **kw) -> CanonicalType:
    from dataclasses import replace

    return replace(t, **kw)


def _pg(name: str):
    try:
        from sqlalchemy.dialects import postgresql

        return getattr(postgresql, name, None)
    except Exception:  # pragma: no cover
        return None


def _int_bits(t: sa.Integer, dialect_name: str | None) -> int:
    if isinstance(t, sa.BigInteger):
        return 64
    if isinstance(t, sa.SmallInteger):
        return 16
    # Plain INTEGER: 32-bit on PostgreSQL, but SQLite's INTEGER affinity stores up to 64 bits.
    return 64 if dialect_name == "sqlite" else 32


def _float_bits(t: sa.Float, dialect_name: str | None) -> int:
    # SQLite REAL is always an 8-byte IEEE float, whatever the declared name (SQLAlchemy reflects
    # 'DOUBLE PRECISION' as REAL there).
    if dialect_name == "sqlite":
        return 64
    # Float(53)/DOUBLE PRECISION → 64; REAL / Float(24) → 32; unspecified generic Float → 64.
    p = getattr(t, "precision", None)
    if isinstance(t, sa.REAL):
        return 32
    if p is not None and p <= 24:
        return 32
    return 64
