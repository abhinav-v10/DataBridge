# DataBridge — Intelligent Data Migration & Reconciliation Platform

Work in progress. See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the design and checklist, and
[docs/DEVLOG.md](docs/DEVLOG.md) for what has actually been built and verified.

## Status
| Module | Status |
|---|---|
| Connection management (API) | Implemented; unit tests use SQLite, API manually verified against real PostgreSQL 16 (Docker) |
| Type system (`CanonicalType`, `normalize_type`) | Implemented; unit-tested against real reflected SQLite types and PostgreSQL's own reflection code |
| Tier-1 data profiling engine + API | Implemented; privacy-by-default (values redacted, never stored); SQLite-verified, PostgreSQL integration tests written but see below |
| Schema comparison + one-to-one mapping recommendations, saved mappings, validation | Implemented (SQLite-verified) |
| Everything else | Planned |

## Run the backend (Windows PowerShell)
```powershell
cd backend
python -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements.txt
$env:DATABRIDGE_SECRET_KEY = (.\.venv\Scripts\python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())")
.\.venv\Scripts\python -m uvicorn app.main:app --reload     # docs at http://localhost:8000/docs
.\.venv\Scripts\python -m pytest -q
```

## Local PostgreSQL (Docker)
Windows machines often already run native PostgreSQL on 5432/5433, which silently shadows Docker. Use 5434:
```powershell
docker run -d --name databridge-postgres -e POSTGRES_USER=databridge -e POSTGRES_PASSWORD=databridge_dev `
  -e POSTGRES_DB=databridge -p 5434:5432 -v databridge_pgdata:/var/lib/postgresql/data postgres:16
docker exec databridge-postgres psql -U databridge -d databridge -c "CREATE DATABASE databridge_meta;"
```
Copy `.env.example` to `backend/.env` and fill in the secret key.

## Phase 1 API (prefix `/api`)
| Endpoint | Purpose |
|---|---|
| `POST /profiling/run` | Profile a table (`{connection_id, table, include_values?, sample_size?, ...}`); stores the redacted result |
| `GET /profiling/runs`, `GET/DELETE /profiling/runs/{id}` | List (filter by `connection_id`, `table`; paginated), fetch, delete stored runs |
| `POST /schema-comparison` | Compare two tables and recommend a one-to-one column mapping (nothing saved) |
| `POST /schema-mappings`, `GET`, `GET /{id}`, `DELETE /{id}` | Save (auto-recommended or explicit), list, fetch, delete mappings |
| `POST /schema-mappings/{id}/validate` | Re-validate a saved mapping against the current live schemas |

Privacy: profiling never returns data values unless the server sets `DATABRIDGE_PROFILING_ALLOW_VALUES=true` *and* the
request sets `include_values`; even then values appear only in that POST response and are never stored. Columns named like
`password`/`token`/`ssn` get null counts only, always.

## Tests
```powershell
cd backend
.\.venv\Scripts\python -m pytest -q          # PostgreSQL integration tests skip automatically if no server
```
PostgreSQL integration tests (`-m integration`) use the Docker setup above (port 5434) or `DATABRIDGE_TEST_PG_URL`.
They never touch any other server. If no server is reachable they are skipped, and the PostgreSQL-specific behaviour
(TABLESAMPLE, read-only transactions, statement_timeout, stddev_samp, live enum/domain/array reflection) is unverified.
