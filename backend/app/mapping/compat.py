"""Type compatibility between a source and a target CanonicalType (direction: source -> target).

Levels, strongest to weakest:
  EXACT        identical semantics.
  COMPATIBLE   lossless widening or representation-preserving (e.g. int32 -> int64, varchar(10) -> varchar(20)).
  LOSSY        values can be truncated, rounded, overflow, or change meaning (e.g. numeric(12,2) -> numeric(10,2),
               timestamptz -> timestamp, anything <-> text).
  INCOMPATIBLE no sensible conversion (e.g. uuid -> integer).
  UNVERIFIABLE one side is an unknown type; nothing can be asserted.
"""
from dataclasses import dataclass, field
from enum import Enum

from app.connectors.types import CanonicalType, Kind


class Level(str, Enum):
    EXACT = "exact"
    COMPATIBLE = "compatible"
    LOSSY = "lossy"
    INCOMPATIBLE = "incompatible"
    UNVERIFIABLE = "unverifiable"


# Score used in mapping confidence.
LEVEL_SCORE = {
    Level.EXACT: 1.0,
    Level.COMPATIBLE: 0.9,
    Level.LOSSY: 0.5,
    Level.UNVERIFIABLE: 0.3,
    Level.INCOMPATIBLE: 0.0,
}
_RANK = {Level.EXACT: 0, Level.COMPATIBLE: 1, Level.LOSSY: 2, Level.UNVERIFIABLE: 3, Level.INCOMPATIBLE: 4}


@dataclass(frozen=True)
class TypeCompat:
    level: Level
    reasons: tuple[str, ...] = field(default_factory=tuple)

    @property
    def score(self) -> float:
        return LEVEL_SCORE[self.level]


def _c(level: Level, *reasons: str) -> TypeCompat:
    return TypeCompat(level, tuple(reasons))


def _worst(*items: TypeCompat) -> TypeCompat:
    level = max((i.level for i in items), key=_RANK.get)
    reasons = tuple(r for i in items for r in i.reasons)
    return TypeCompat(level, reasons)


_INT_DIGITS = {16: 5, 32: 10, 64: 19}


def compare_types(s: CanonicalType, t: CanonicalType) -> TypeCompat:
    if s.kind is Kind.UNKNOWN or t.kind is Kind.UNKNOWN:
        return _c(Level.UNVERIFIABLE, "unknown type on one side; compatibility cannot be verified")
    if s.kind is t.kind:
        return _same_kind(s, t)
    return _cross_kind(s, t)


# ---------------------------------------------------------------- same kind
def _same_kind(s: CanonicalType, t: CanonicalType) -> TypeCompat:
    k = s.kind
    if k in (Kind.INTEGER, Kind.FLOAT):
        return _bits(s, t, k.value)
    if k is Kind.DECIMAL:
        return _decimal(s, t)
    if k in (Kind.STRING, Kind.BINARY):
        return _length(s, t)
    if k in (Kind.TIMESTAMP, Kind.TIME):
        return _temporal(s, t)
    if k is Kind.ENUM:
        return _enum(s, t)
    if k is Kind.ARRAY:
        inner = compare_types(s.element, t.element)
        return TypeCompat(inner.level, tuple(f"array element: {r}" for r in inner.reasons))
    return _c(Level.EXACT)


def _bits(s, t, label) -> TypeCompat:
    if s.bits == t.bits:
        return _c(Level.EXACT)
    if t.bits > s.bits:
        return _c(Level.COMPATIBLE, f"{label} widened from {s.bits} to {t.bits} bits")
    return _c(Level.LOSSY, f"{label} narrowed from {s.bits} to {t.bits} bits: values may overflow")


def _decimal(s, t) -> TypeCompat:
    if s.precision is None and t.precision is None:
        return _c(Level.EXACT)
    if t.precision is None:
        return _c(Level.COMPATIBLE, "target numeric is unbounded")
    if s.precision is None:
        return _c(Level.LOSSY, f"source numeric is unbounded; target is NUMERIC({t.precision},{t.scale or 0})")
    ss, ts = s.scale or 0, t.scale or 0
    if (s.precision, ss) == (t.precision, ts):
        return _c(Level.EXACT)
    si, ti = s.precision - ss, t.precision - ts
    reasons = []
    if ts < ss:
        reasons.append(f"scale reduced from {ss} to {ts}: values will be rounded")
    if ti < si:
        reasons.append(f"integer digits reduced from {si} to {ti}: values may overflow")
    if reasons:
        return _c(Level.LOSSY, *reasons)
    return _c(Level.COMPATIBLE, f"numeric widened to NUMERIC({t.precision},{ts})")


def _length(s, t) -> TypeCompat:
    if s.length == t.length:
        return _c(Level.EXACT)
    if t.length is None:
        return _c(Level.COMPATIBLE, "target length is unbounded")
    if s.length is None:
        return _c(Level.LOSSY, f"source is unbounded; target length is {t.length}: values may be truncated")
    if t.length > s.length:
        return _c(Level.COMPATIBLE, f"length widened from {s.length} to {t.length}")
    return _c(Level.LOSSY, f"length reduced from {s.length} to {t.length}: values may be truncated")


def _temporal(s, t) -> TypeCompat:
    parts = []
    if bool(s.timezone) != bool(t.timezone):
        parts.append(
            _c(Level.LOSSY, "time zone semantics differ (with/without time zone): instants may shift or lose offset")
        )
    sp = 6 if s.precision is None else s.precision
    tp = 6 if t.precision is None else t.precision
    if tp < sp:
        parts.append(_c(Level.LOSSY, f"fractional-second precision reduced from {sp} to {tp}"))
    elif tp > sp:
        parts.append(_c(Level.COMPATIBLE, f"fractional-second precision widened from {sp} to {tp}"))
    return _worst(*parts) if parts else _c(Level.EXACT)


def _enum(s, t) -> TypeCompat:
    sv, tv = set(s.enum_values or ()), set(t.enum_values or ())
    if sv == tv:
        return _c(Level.EXACT)
    if sv <= tv:
        return _c(Level.COMPATIBLE, "target enum has additional values")
    missing = sorted(sv - tv)
    return _c(Level.LOSSY, f"target enum lacks values present in source: {', '.join(missing)}")


# ---------------------------------------------------------------- cross kind
def _cross_kind(s: CanonicalType, t: CanonicalType) -> TypeCompat:
    sk, tk = s.kind, t.kind
    if sk is Kind.INTEGER and tk is Kind.DECIMAL:
        need = _INT_DIGITS.get(s.bits, 19)
        if t.precision is None:
            return _c(Level.COMPATIBLE, "integer stored in unbounded numeric")
        room = t.precision - (t.scale or 0)
        if room >= need:
            return _c(Level.COMPATIBLE, "integer stored as numeric")
        return _c(Level.LOSSY, f"target numeric has {room} integer digits; source integers need up to {need}")
    if sk is Kind.INTEGER and tk is Kind.FLOAT:
        if s.bits <= 32 and t.bits == 64:
            return _c(Level.COMPATIBLE, "integer stored as double precision (exact)")
        return _c(Level.LOSSY, "large integers lose precision as floating point")
    if (sk, tk) in {
        (Kind.DECIMAL, Kind.FLOAT), (Kind.FLOAT, Kind.DECIMAL), (Kind.DECIMAL, Kind.INTEGER),
        (Kind.FLOAT, Kind.INTEGER), (Kind.BOOLEAN, Kind.INTEGER), (Kind.INTEGER, Kind.BOOLEAN),
    }:
        return _c(Level.LOSSY, f"{sk.value} -> {tk.value} changes representation and may lose precision or meaning")
    if sk is Kind.DATE and tk is Kind.TIMESTAMP:
        return _c(Level.COMPATIBLE, "date stored as timestamp (midnight)")
    if sk is Kind.TIMESTAMP and tk is Kind.DATE:
        return _c(Level.LOSSY, "time-of-day component is dropped")
    if sk is Kind.ENUM and tk is Kind.STRING:
        longest = max((len(v) for v in s.enum_values or ()), default=0)
        if t.length is None or t.length >= longest:
            return _c(Level.COMPATIBLE, "enum stored as text")
        return _c(Level.LOSSY, f"target length {t.length} is shorter than the longest enum label ({longest})")
    if tk is Kind.STRING:
        return _c(Level.LOSSY, f"{sk.value} stored as text: native type semantics are lost")
    if sk is Kind.STRING:
        return _c(Level.LOSSY, f"text parsed as {tk.value}: values must be valid for the target type")
    return _c(Level.INCOMPATIBLE, f"no conversion from {sk.value} to {tk.value}")
