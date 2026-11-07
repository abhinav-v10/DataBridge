"""Schema comparison, greedy one-to-one column matching, and mapping validation.

Confidence for a candidate pair (source column s, target column t), all components in [0, 1]:

    confidence = 0.65 * name_score + 0.25 * type_score + 0.10 * structure_score

  name_score       1.00 same name ignoring case
                   0.95 same after normalisation (camelCase/snake_case/punctuation: customerId == customer_id)
                   0.90 same once underscores are removed (cust_id == custid)
                   else fuzzy = min(0.85, 0.85 * max(SequenceMatcher ratio, token Jaccard))
  type_score       from compat.LEVEL_SCORE: exact 1.0, compatible 0.9, lossy 0.5, unverifiable 0.3, incompatible 0.0
  structure_score  1.0 if both or neither are primary-key columns, else 0.5

Algorithm: pairs with name_score < MIN_NAME_SCORE are never candidates (type alone must not pair
unrelated columns). Candidates are sorted by (-confidence, -name_score, source position, target
position) and assigned greedily; each source and target column is used at most once. An assigned pair
below `min_confidence` is rejected. Greedy is not globally optimal (it is not the Hungarian algorithm)
but is deterministic and explainable; every rejected/unmapped column reports its best suggestion.

Status of an accepted mapping: "auto" (confidence >= AUTO_CONFIDENCE and type exact/compatible),
"conflict" (types incompatible), otherwise "review".
"""
import re
from functools import lru_cache
from dataclasses import dataclass, field
from difflib import SequenceMatcher

from app.connectors.base import TableSchema
from app.connectors.types import CanonicalType, Kind, normalize_type
from app.mapping.compat import Level, TypeCompat, compare_types

W_NAME, W_TYPE, W_STRUCT = 0.65, 0.25, 0.10
MIN_NAME_SCORE = 0.5
AUTO_CONFIDENCE = 0.85
DEFAULT_MIN_CONFIDENCE = 0.6
_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")


@dataclass(frozen=True)
class SideColumn:
    name: str
    nullable: bool
    is_pk: bool
    canonical: CanonicalType


@dataclass(frozen=True)
class SchemaSide:
    table: str
    columns: tuple[SideColumn, ...]

    def get(self, name: str) -> SideColumn | None:
        return next((c for c in self.columns if c.name == name), None)


def side_from_schema(ts: TableSchema, dialect: str) -> SchemaSide:
    cols = []
    for c in ts.columns:
        canon = normalize_type(c.sa_type, dialect) if c.sa_type is not None else CanonicalType(Kind.UNKNOWN, c.data_type)
        cols.append(SideColumn(c.name, c.nullable, c.is_primary_key, canon))
    return SchemaSide(ts.name, tuple(cols))


# ---------------------------------------------------------------- names
@lru_cache(maxsize=None)
def normalize_name(name: str) -> str:
    s = _CAMEL.sub("_", name)
    s = re.sub(r"[^A-Za-z0-9]+", "_", s).strip("_").lower()
    return s


def _bigrams(s: str) -> set[str]:
    return {s[i : i + 2] for i in range(len(s) - 1)} or {s}


def name_score(a: str, b: str) -> float:
    if a.lower() == b.lower():
        return 1.0
    na, nb = normalize_name(a), normalize_name(b)
    if na == nb:
        return 0.95
    if na.replace("_", "") == nb.replace("_", ""):
        return 0.90
    ta, tb = set(na.split("_")), set(nb.split("_"))
    jac = len(ta & tb) / len(ta | tb) if ta | tb else 0.0
    ba, bb = _bigrams(na), _bigrams(nb)
    dice = 2 * len(ba & bb) / (len(ba) + len(bb))
    # Cheap pre-filter: SequenceMatcher is the expensive part and cannot reach a useful score
    # for strings that share almost no character bigrams.
    ratio = SequenceMatcher(None, na, nb, autojunk=False).ratio() if dice >= 0.3 else 0.0
    return round(min(0.85, 0.85 * max(ratio, jac)), 4)


# ---------------------------------------------------------------- results
@dataclass
class Issue:
    severity: str  # "info" | "warning" | "error"
    code: str
    message: str

    def to_dict(self):
        return {"severity": self.severity, "code": self.code, "message": self.message}


@dataclass
class ColumnMapping:
    source_column: str
    target_column: str
    confidence: float
    name_score: float
    type_score: float
    structure_score: float
    type_compatibility: str
    status: str  # auto | review | conflict
    source_type: dict
    target_type: dict
    issues: list[Issue] = field(default_factory=list)

    def to_dict(self):
        d = {k: getattr(self, k) for k in (
            "source_column", "target_column", "confidence", "name_score", "type_score", "structure_score",
            "type_compatibility", "status", "source_type", "target_type")}
        d["issues"] = [i.to_dict() for i in self.issues]
        return d


@dataclass
class UnmappedColumn:
    column: str
    type: dict
    nullable: bool
    is_primary_key: bool
    reason: str
    suggestion: dict | None = None  # {"column": ..., "confidence": ...}
    issues: list[Issue] = field(default_factory=list)

    def to_dict(self):
        return {"column": self.column, "type": self.type, "nullable": self.nullable,
                "is_primary_key": self.is_primary_key, "reason": self.reason, "suggestion": self.suggestion,
                "issues": [i.to_dict() for i in self.issues]}


@dataclass
class SchemaComparison:
    source_table: str
    target_table: str
    mappings: list[ColumnMapping]
    unmapped_source: list[UnmappedColumn]
    unmapped_target: list[UnmappedColumn]
    issues: list[Issue]
    min_confidence: float

    @property
    def summary(self) -> dict:
        n_src = len(self.mappings) + len(self.unmapped_source)
        n_tgt = len(self.mappings) + len(self.unmapped_target)
        by = lambda s: sum(1 for m in self.mappings if m.status == s)  # noqa: E731
        blocking = by("conflict")
        return {
            "source_columns": n_src,
            "target_columns": n_tgt,
            "mapped": len(self.mappings),
            "auto": by("auto"),
            "review": by("review"),
            "conflict": by("conflict"),
            "unmapped_source": len(self.unmapped_source),
            "unmapped_target": len(self.unmapped_target),
            # mean confidence over source columns; unmapped source columns count as 0
            "overall_confidence": round(sum(m.confidence for m in self.mappings) / n_src, 4) if n_src else 0.0,
            "blocking_issues": blocking,
            "ready": blocking == 0 and not self.unmapped_source,
        }

    def to_dict(self):
        return {
            "source_table": self.source_table,
            "target_table": self.target_table,
            "min_confidence": self.min_confidence,
            "summary": self.summary,
            "mappings": [m.to_dict() for m in self.mappings],
            "unmapped_source": [u.to_dict() for u in self.unmapped_source],
            "unmapped_target": [u.to_dict() for u in self.unmapped_target],
            "issues": [i.to_dict() for i in self.issues],
        }


# ---------------------------------------------------------------- pair evaluation
def _structure(s: SideColumn, t: SideColumn) -> float:
    return 1.0 if s.is_pk == t.is_pk else 0.5


def _confidence(ns: float, compat: TypeCompat, st: float) -> float:
    return round(W_NAME * ns + W_TYPE * compat.score + W_STRUCT * st, 4)


def _pair_issues(s: SideColumn, t: SideColumn, compat: TypeCompat) -> list[Issue]:
    out: list[Issue] = []
    sev = {Level.LOSSY: "warning", Level.UNVERIFIABLE: "warning", Level.INCOMPATIBLE: "error"}.get(compat.level)
    for r in compat.reasons:
        out.append(Issue(sev or "info", f"type_{compat.level.value}", r))
    if compat.level in (Level.INCOMPATIBLE,) and not compat.reasons:
        out.append(Issue("error", "type_incompatible", "types are incompatible"))
    if s.nullable and not t.nullable:
        out.append(Issue("warning", "nullability", "source allows NULL but target is NOT NULL: NULL rows will fail to load"))
    if s.is_pk != t.is_pk:
        out.append(Issue("warning", "primary_key", "primary-key membership differs between source and target"))
    return out


def build_mapping(s: SideColumn, t: SideColumn) -> ColumnMapping:
    compat = compare_types(s.canonical, t.canonical)
    ns, st = name_score(s.name, t.name), _structure(s, t)
    conf = _confidence(ns, compat, st)
    if compat.level is Level.INCOMPATIBLE:
        status = "conflict"
    elif conf >= AUTO_CONFIDENCE and compat.level in (Level.EXACT, Level.COMPATIBLE):
        status = "auto"
    else:
        status = "review"
    return ColumnMapping(
        s.name, t.name, conf, ns, compat.score, st, compat.level.value, status,
        s.canonical.to_dict(), t.canonical.to_dict(), _pair_issues(s, t, compat),
    )


# ---------------------------------------------------------------- comparison
def compare_schemas(source: SchemaSide, target: SchemaSide, min_confidence: float = DEFAULT_MIN_CONFIDENCE
                    ) -> SchemaComparison:
    if not 0.0 <= min_confidence <= 1.0:
        raise ValueError("min_confidence must be between 0 and 1")
    cands = []
    ns_matrix = [[name_score(s.name, t.name) for t in target.columns] for s in source.columns]
    for si, s in enumerate(source.columns):
        for ti, t in enumerate(target.columns):
            ns = ns_matrix[si][ti]
            if ns < MIN_NAME_SCORE:
                continue
            compat = compare_types(s.canonical, t.canonical)
            cands.append((-_confidence(ns, compat, _structure(s, t)), -ns, si, ti))
    cands.sort()

    used_s, used_t, chosen = set(), set(), {}
    for negc, _, si, ti in cands:
        if si in used_s or ti in used_t:
            continue
        used_s.add(si)
        used_t.add(ti)
        if -negc >= min_confidence:
            chosen[si] = ti
        else:  # best remaining candidate for both was too weak; leave both unmapped
            pass
    mapped_t = set(chosen.values())

    mappings = [build_mapping(source.columns[si], target.columns[ti]) for si, ti in sorted(chosen.items())]
    unmapped_s = [
        _unmapped(c, "no target column with a sufficiently similar name", _suggest(c, target.columns, {m.target_column for m in mappings}, True, ns_matrix[i]), False)
        for i, c in enumerate(source.columns) if i not in chosen
    ]
    unmapped_t = [
        _unmapped(c, "no source column maps to this target column", _suggest(c, source.columns, {m.source_column for m in mappings}, False, [row[i] for row in ns_matrix]), True)
        for i, c in enumerate(target.columns) if i not in mapped_t
    ]
    issues = _table_issues(source, target, mappings)
    return SchemaComparison(source.table, target.table, mappings, unmapped_s, unmapped_t, issues, min_confidence)


def _suggest(col: SideColumn, others: tuple[SideColumn, ...], taken: set[str], col_is_source: bool,
             name_scores: list[float]) -> dict | None:
    """Best name-similar column on the other side that is not already mapped (informational only)."""
    best = None
    for o, ns in zip(others, name_scores):
        if o.name in taken or ns < MIN_NAME_SCORE:
            continue
        s, t = (col, o) if col_is_source else (o, col)
        conf = _confidence(ns, compare_types(s.canonical, t.canonical), _structure(s, t))
        if best is None or conf > best["confidence"]:
            best = {"column": o.name, "confidence": conf}
    return best


def _unmapped(c: SideColumn, reason: str, suggestion, is_target: bool) -> UnmappedColumn:
    issues = []
    if is_target and not c.nullable:
        issues.append(Issue("warning", "required_target_unmapped",
                            "target column is NOT NULL and has no source mapping; ensure it has a default/identity"))
    if not is_target:
        issues.append(Issue("warning", "source_unmapped", "source column has no target and its data will not be migrated"))
    return UnmappedColumn(c.name, c.canonical.to_dict(), c.nullable, c.is_pk, reason, suggestion, issues)


def _table_issues(source: SchemaSide, target: SchemaSide, mappings: list[ColumnMapping]) -> list[Issue]:
    out: list[Issue] = []
    src_pk = {c.name for c in source.columns if c.is_pk}
    tgt_pk = {c.name for c in target.columns if c.is_pk}
    if src_pk:
        mapped_pk = {m.target_column for m in mappings if m.source_column in src_pk}
        if len({m.source_column for m in mappings} & src_pk) < len(src_pk):
            out.append(Issue("error", "pk_unmapped", "some source primary-key columns have no target mapping"))
        elif mapped_pk != tgt_pk:
            out.append(Issue("warning", "pk_mismatch", "source primary key maps to different target columns than the target's primary key"))
    elif not tgt_pk:
        out.append(Issue("warning", "no_primary_key", "neither table has a primary key; row-level reconciliation will need explicit key columns"))
    if not src_pk and tgt_pk:
        out.append(Issue("info", "target_only_pk", "only the target has a primary key"))
    return out


# ---------------------------------------------------------------- validation of explicit mappings
@dataclass
class ValidationReport:
    errors: list[Issue]
    warnings: list[Issue]
    mappings: list[ColumnMapping]

    @property
    def valid(self) -> bool:
        return not self.errors

    def to_dict(self):
        return {"valid": self.valid, "errors": [i.to_dict() for i in self.errors],
                "warnings": [i.to_dict() for i in self.warnings], "mappings": [m.to_dict() for m in self.mappings]}


def validate_pairs(source: SchemaSide, target: SchemaSide, pairs: list[tuple[str, str]],
                   key_columns: list[str] | None = None) -> ValidationReport:
    """Validate an explicit (source_column, target_column) list against the live schemas."""
    errors: list[Issue] = []
    warnings: list[Issue] = []
    mappings: list[ColumnMapping] = []
    seen_s: set[str] = set()
    seen_t: set[str] = set()
    for sname, tname in pairs:
        s, t = source.get(sname), target.get(tname)
        if s is None:
            errors.append(Issue("error", "source_column_missing", f"source column '{sname}' does not exist in '{source.table}'"))
        if t is None:
            errors.append(Issue("error", "target_column_missing", f"target column '{tname}' does not exist in '{target.table}'"))
        if sname in seen_s:
            errors.append(Issue("error", "duplicate_source", f"source column '{sname}' is mapped more than once"))
        if tname in seen_t:
            errors.append(Issue("error", "duplicate_target", f"target column '{tname}' is mapped more than once"))
        seen_s.add(sname)
        seen_t.add(tname)
        if s is None or t is None:
            continue
        m = build_mapping(s, t)
        mappings.append(m)
        for i in m.issues:
            if i.severity == "error":
                errors.append(Issue("error", i.code, f"{sname} -> {tname}: {i.message}"))
            elif i.severity == "warning":
                warnings.append(Issue("warning", i.code, f"{sname} -> {tname}: {i.message}"))
    for c in target.columns:
        if c.name not in seen_t and not c.nullable:
            warnings.append(Issue("warning", "required_target_unmapped",
                                  f"target column '{c.name}' is NOT NULL and unmapped; ensure it has a default/identity"))
    mapped_src = {s for s, _ in pairs}
    for k in key_columns or []:
        if source.get(k) is None:
            errors.append(Issue("error", "key_column_missing", f"key column '{k}' does not exist in the source table"))
        elif k not in mapped_src:
            errors.append(Issue("error", "key_column_unmapped", f"key column '{k}' is not part of the mapping"))
    return ValidationReport(errors, warnings, mappings)
