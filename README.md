# DataBridge — Intelligent Data Migration & Reconciliation Platform

Work in progress. See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the design and checklist, and
[docs/DEVLOG.md](docs/DEVLOG.md) for what has actually been built and verified.

## Status
| Module | Status |
|---|---|
| Connection management (API) | Implemented; unit tests use SQLite, API manually verified against real PostgreSQL 16 (Docker) |
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
