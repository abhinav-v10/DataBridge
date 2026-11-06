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
