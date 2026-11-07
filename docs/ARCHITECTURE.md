# DataBridge — Architecture

## 1. Goal
Prove (or disprove) that a migrated target database faithfully reproduces the source. Every number shown
anywhere in the product is computed from real queries against real databases.

## 2. Components & data flow

```
                        ┌─────────────────────────── React + TS + Tailwind + Recharts ─────────────┐
                        │ Overview · Connections · Explorer · Profiling · Mapping · Migration ·     │
                        │ Reconciliation · Data Quality · Exceptions · Audit · Settings            │
                        └───────────────────────────────┬──────────────────────────────────────────┘
                                                        │ REST/JSON
┌───────────────────────────────────────────────────────▼──────────────────────────────────────────┐
│ FastAPI  (api/ → services/ → engines)       Pydantic schemas · pagination · no secrets in output │
├──────────────┬───────────┬──────────┬──────────┬────────────────┬────────────┬──────────────────┤
│ connectors   │ profiling │ mapping  │ etl      │ reconciliation │ data_quality│ anomaly / scoring│
│ (registry)   │           │ +transform│ extract→ │ count/key/hash/│ rule engine │ IsolationForest, │
│              │           │          │ transform│ column/aggregate│            │ weighted score   │
│              │           │          │ →load    │ /RI/tolerance  │            │ + critical gates │
├──────────────┴───────────┴──────────┴──────────┴────────────────┴────────────┴──────────────────┤
│ exceptions (status workflow, notes, history)  ·  reporting (CSV / PDF)  ·  audit log             │
└──────────────┬───────────────────────────────────────────────────────────────┬───────────────────┘
               │ SQLAlchemy                                                    │ SQLAlchemy (read/write)
     ┌─────────▼─────────┐                                         ┌───────────▼───────────┐
     │ Metadata DB       │   connections(encrypted), mappings,     │ Source DB  │ Target DB│
     │ (Postgres)        │   runs, stage logs, results, exceptions │ (Postgres) │(Postgres)│
     └───────────────────┘                                         └───────────────────────┘
Airflow DAG → calls the same backend service layer (no duplicated logic).  dbt → optional post-load models on target.
```

Migration run lifecycle: `CREATED → EXTRACTING → PROFILING → TRANSFORMING → VALIDATING → LOADING → RECONCILING → SCORED`
(or `FAILED`). Every stage writes a `stage_log` row (started, finished, rows_in, rows_out, error).

## 3. Key design decisions
| Decision | Rationale |
|---|---|
| Connector ABC + registry; only `postgresql` is real (plus `sqlite`, flagged test-only) | Honest extensibility; SQLite lets CI run without a DB server. Oracle/MySQL etc. are *not* registered. |
| Credentials Fernet-encrypted at rest, key from `DATABRIDGE_SECRET_KEY`; API never returns them | No hardcoded passwords; no secret leakage. |
| Reconciliation pushes work into SQL where possible (counts, aggregates, anti-joins); row compare done in keyset-paginated chunks | Never loads a full table in memory. |
| Hash = SHA-256 over canonical string (see §5) computed in Python on both sides with identical normalizer | Deterministic, DB-agnostic; same function used by ETL and reconciliation. |
| Scoring = weighted components **plus critical-failure gates** | A high weighted score can't hide a financial discrepancy. |
| Isolation Forest is advisory, separate from the verdict | Anomalies are "suspicious", never "errors". |
| PySpark/Airflow/dbt are optional extras (heavy) in separate requirement files | Local core stays light; chunked pandas/SQL streaming is the default engine. |

## 4. Metadata database schema (SQLAlchemy models)
- `connections` id, name, role(source/target), db_type, host, port, database, username, `password_enc`, schema, created_at
- `schema_mappings` id, name(unique), source_connection_id, target_connection_id, source_table, target_table, key_columns(JSON), config(JSON: `columns[{source_column,target_column,origin,confidence}]`, `min_confidence`), created_at  **(implemented; casts/transforms still planned)**
- `migration_runs` id(uuid), source_conn_id, target_conn_id, mapping_ids, config(JSON), status, started_at, finished_at, score, verdict
- `stage_logs` id, run_id, stage, status, started_at, finished_at, rows_in, rows_out, error
- `profile_runs` id, connection_id, table_name, status, include_values_requested, options(JSON), row_count, column_count, primary_key(JSON), table_metrics(JSON), error, duration_ms, created_at  **(implemented)**
- `profile_column_results` id, run_id, position, column_name, status, sensitive, canonical(JSON), metrics(JSON), redacted(JSON), skipped(JSON), errors(JSON)  **(implemented; replaces the planned `profile_results`; value-bearing metrics are never stored)**
- `recon_results` id, run_id, table, check_type, status, summary(JSON), duration_ms
- `dq_results` id, run_id, rule_id, rule_name, table, records_tested, failed, pass_pct, severity, details(JSON)
- `exceptions` id, run_id, table, column, pk, severity, reason, source_value, target_value, status, created_at
- `exception_events` id, exception_id, from_status, to_status, note, at  (full change history)
- `audit_log` id, at, actor, action, detail(JSON)

## 5. Canonical hashing rules
Row string = `col=value` joined by `\x1f` in sorted column order; NULL → `\x00NULL`; Decimal/float/int → quantized
`Decimal` with configured scale, normalized (no exponent, `-0`→`0`); datetime → UTC ISO-8601 seconds; date → ISO;
bool → `true/false`; str → NFC-normalized, optional strip/casefold per config; bytes → hex.

## 6. Reconciliation algorithm (per table pair)
1. **Count**: `SELECT count(*)` both sides.
2. **Keys**: stream `ORDER BY pk` from both sides in chunks, merge-join on key → *missing*, *extra*, *duplicate* keys.
3. **Record/column/hash**: for keys present on both, compare normalized column values (with per-column tolerance) and row hash → *mismatches* with exact column diffs.
4. **Aggregates**: `SUM/COUNT/AVG/MIN/MAX` in SQL per configured column, compared with tolerance.
5. **Referential integrity**: anti-join child FK → parent on target (and on source for baseline).
6. Each finding becomes an `exception` row; each check a `recon_results` row.

## 7. Scoring
`score = Σ wᵢ·componentᵢ / Σ wᵢ`, components in [0,100]: record_match, completeness, dq_pass, referential_integrity,
aggregate_match, schema_compat. Weights come from run config. **Gates**: any critical failure (aggregate on a
financial column outside tolerance, missing PK rows above threshold, broken RI) forces verdict `REJECTED`/`NEEDS_REVIEW`
regardless of score. Output includes per-component values, weights, and the reason list.

## 8. API surface (prefix `/api`)
connections (POST, GET, DELETE, POST /test) · tables · schema/{table} · profiling/run, profiling/runs, profiling/runs/{id} · schema-comparison (the mapping preview) · schema-mappings (POST, GET, GET/{id}, DELETE, POST /{id}/validate) ·
migrations (POST, GET, GET/{id}, logs) · reconciliation/run, /{run_id} · quality/run, /{run_id} · anomalies/run ·
exceptions (GET filters, PATCH) · reports/{run_id}?format=csv|pdf · dashboard/summary · testdata (seed, corrupt).

## 9. Hard problems
1. Memory-safe reconciliation (keyset pagination + streaming merge-join).
2. Cross-DB type normalization so equal values hash equally (decimals, timestamps/timezones).
3. Idempotent loads (`INSERT … ON CONFLICT (pk) DO UPDATE`, batch checkpointing).
4. Correct scoring gates; avoiding false confidence.
5. Reproducible seed + corruption (fixed RNG seed, corruption manifest = ground truth the E2E test asserts against).
6. Verifying Docker on a machine without Docker today (compose is written but unverified until Docker is installed).

## 10. Roadmap / checklist
- [x] Phase 1 architecture (this doc)
- [ ] Phase 2 backend foundation: config, crypto, metadata models, connector registry, connection API, tests
- [~] Phase 3 profiling, mapping, transforms — profiling + schema comparison/mapping + their APIs done (see DEVLOG, "Phase 1"); transforms pending
- [ ] Phase 4 ETL (batch, retry, idempotent, stage logs)
- [ ] Phase 5 reconciliation
- [ ] Phase 6 DQ engine, anomaly detection, scoring
- [ ] Phase 7 exceptions, reporting
- [ ] Phase 8 React frontend
- [ ] Phase 9 Docker/Airflow/dbt/AWS docs
- [ ] Phase 10 E2E tests, docs, resume + interview prep
