# Development Log

## 2026-11-05 — Phase 1 + Phase 2 (foundation)
- Repo was empty. Environment: Python 3.12, Node 22; **Docker and PostgreSQL not installed** → compose files unverified until Docker is installed.
- Wrote `docs/ARCHITECTURE.md` (components, schema, hashing rules, reconciliation algorithm, scoring gates, roadmap).
- Backend foundation: settings, Fernet secret encryption, metadata DB, connector ABC + registry (postgresql real, sqlite test-only), connection API.
- Tests: 10 passing (create/list, no password leak, encrypted at rest, duplicates, unsupported db_type, test endpoint, tables/schema, 404s, delete, missing key).
- Not yet verified: real PostgreSQL connectivity (needs a Postgres instance; the connector path is covered only via the shared SQLAlchemy code with SQLite).

## Real PostgreSQL verification
- Verified against PostgreSQL 16 in Docker (localhost:5434): test/create/list/status/tables/schema/delete endpoints, 409 duplicate, 404 unknown table, wrong password / closed port / missing DB all return clean errors, password encrypted at rest, metadata DB on Postgres works.
- Found: native Windows PostgreSQL 17/18 occupy 5432/5433 and shadow the container -> container moved to 5434.
- Fixed: added `connect_timeout=10` to the Postgres connector (unreachable hosts previously hung for the OS TCP default).

## 2026-11-07 — Phase 1: type system, profiling, schema comparison/mapping, persistence, APIs
Branch `phase-1` (uncommitted). Built in six internal checkpoints.

- **Step 1 Type system** (`connectors/types.py`): `CanonicalType` + `normalize_type()` read the *reflected SQLAlchemy type objects*
  (precision/scale/timezone/array/enum/domain/integer width); unknown types become `Kind.UNKNOWN`. `ColumnInfo.sa_type` is internal
  only (excluded from repr/eq; the schema endpoint builds `ColumnOut` field by field). Found: SQLite reflects `DOUBLE PRECISION`
  as `REAL` and resolves unknown declared types to NUMERIC by affinity.
- **Step 2 Profiling engine** (`app/profiling/`): per-column metrics by canonical kind, batched aggregates, sampling on large
  tables (every metric carries `method` + `sample_size`), per-column failure isolation, sanitised errors (never row values),
  statement timeouts, read-only PostgreSQL transactions. Values are redacted by construction (never queried) unless
  `include_values`; sensitive column names are counts-only always. SQLite stddev uses a one-pass variance workaround (documented
  precision limits).
- **Step 3 Mapping** (`app/mapping/`): type compatibility levels (exact/compatible/lossy/incompatible/unverifiable) and a greedy
  one-to-one matcher; `confidence = 0.65*name + 0.25*type + 0.10*structure`. Perf: first version took 29s for 300x300 columns
  (recomputed fuzzy scores for suggestions); now a precomputed score matrix + bigram pre-filter = 0.4s.
- **Step 4 Persistence**: tables `profile_runs`, `profile_column_results`, `schema_mappings` (created by `create_all`; no existing
  table changed, so **no migration** is needed and none was added — the project has no Alembic). Only redacted profiles are stored.
  Bug found by tests: `passive_deletes=True` orphaned child rows on SQLite (FKs not enforced) -> removed; connection deletion now
  removes dependent runs/mappings explicitly.
- **Step 5 APIs**: `/api/profiling/*`, `/api/schema-comparison`, `/api/schema-mappings/*`. `include_values` is gated by
  `DATABRIDGE_PROFILING_ALLOW_VALUES` (default off -> 403).
- **Step 6 Integration**: end-to-end workflow, SQL-injection-style identifiers (quoted, never executed), unicode/mixed-case names,
  empty/no-PK/composite-PK/all-null/unknown-type/wide/sampled tables, persistence across engine restart, no secrets in responses
  or logs, safe 503 on missing encryption key.
- **Decisions** (ambiguities resolved conservatively): the "mapping preview" is `POST /schema-comparison`; mappings are persisted
  in Step 4 alongside profiles; failed profile runs are recorded, missing-table (client error) runs are not; auto-saved mappings
  exclude type conflicts; unmapped NOT NULL target columns are warnings (defaults are not reflected), not blockers.
- **Verification status**: 316 passed, 11 skipped. **PostgreSQL integration tests (11) were skipped: no server was reachable
  (Docker not running).** Unverified on a live server: TABLESAMPLE SYSTEM ... REPEATABLE, `SET TRANSACTION READ ONLY`,
  `set_config('statement_timeout')`, `stddev_samp`, and end-to-end enum/domain/array/timestamptz behaviour through the APIs.
