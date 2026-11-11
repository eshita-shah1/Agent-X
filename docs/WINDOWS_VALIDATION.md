# Windows validation (B8): commands for the integration machine

**Status: partly validated on the Windows integration machine (2026-11-11/12); staging steps pending.** Results are
recorded in [§ Results](#results-2026-1111-12-windows-11-integration-machine) below. The commands follow.

Run the steps below in PowerShell on the Windows integration machine, from the repository root. Record the output of
each step. Any failure is a defect to fix before merging.

Prerequisites (from [SETUP.md](SETUP.md)):
- Python 3.14;
- native PostgreSQL 18 with `agentx` and `agentx_test`, and the non-superuser role `agentx_app`;
- Docker Desktop;
- a `.env` copied from `.env.example`.

```powershell
# 0. Branch and clean state
git fetch origin
git switch phase-4-simulation-execution          # once pushed for review
git status                                       # expect: clean
git config core.autocrlf                         # expect: false or input (.gitattributes enforces LF)

# 1. Environment and dependencies
py -3.14 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r backend\requirements-dev.txt
.\.venv\Scripts\python.exe -c "import zoneinfo; print(zoneinfo.ZoneInfo('Asia/Kolkata'))"   # tzdata on Windows
.\.venv\Scripts\python.exe -c "import app.main" ; # run from backend\ ; expect no output

# 2. Configuration loading (.env, JSON list values, no execution enabled)
cd backend
..\.venv\Scripts\python.exe -c "from app.core.config import get_settings as g; s=g(); print(s.execution_mode, s.db_port, s.exchange_cost_schedule_path)"
#    expect: disabled <your port> None

# 3. Migrations from an empty database (use a disposable database)
psql -U postgres -c "CREATE DATABASE agentx_wincheck OWNER agentx_app;"
$env:DB_NAME="agentx_wincheck"; ..\.venv\Scripts\python.exe -m alembic upgrade head
..\.venv\Scripts\python.exe -m alembic downgrade base
..\.venv\Scripts\python.exe -m alembic upgrade head
..\.venv\Scripts\python.exe -m alembic current     # expect: 0010 (head)
..\.venv\Scripts\python.exe -m alembic check       # expect: No new upgrade operations detected.
Remove-Item Env:DB_NAME
psql -U postgres -c "DROP DATABASE agentx_wincheck;"

# 4. Upgrade the real development database (keeps existing data; forward-only)
..\.venv\Scripts\python.exe -m alembic upgrade head

# 5. Full test suite (uses agentx_test only)
..\.venv\Scripts\python.exe -m pytest -q          # expect: all passed (865 on macOS at the time of writing)

# 6. API start-up and health
..\.venv\Scripts\python.exe -m uvicorn --factory app.main:build_app --port 8010
#    in another window:
Invoke-RestMethod http://localhost:8010/health          # {"status":"ok"}
Invoke-RestMethod http://localhost:8010/health/ready    # {"status":"ready"}

# 7. Docker Compose
cd ..
docker compose build api
docker compose run --rm migrate
docker compose run --rm migrate alembic current          # expect: 0010 (head)
docker compose up -d api
Invoke-RestMethod "http://localhost:$((Get-Content .env | Select-String '^API_PORT=').ToString().Split('=')[1])/health/ready"
docker compose down

# 8. Ingestion command against the real archive (ingestion only; no execution)
cd backend
..\.venv\Scripts\python.exe -m app.ops.cli daily --coverage-days 10 --report-dir ..\data\ingestion_reports
..\.venv\Scripts\python.exe -m app.ops.cli latency --days 10
```

Report back:
- the output of steps 2, 3, 5, 6, 7 and 8;
- `python --version`, `psql --version` and `docker version`;
- any path, encoding or line-ending error, with its traceback.

## Results (2026-11-11/12, Windows 11 integration machine)

Environment: Windows 11 Home 10.0.26200, timezone India Standard Time; Python 3.14.3; PostgreSQL 18.6 (native);
Docker Engine 29.2.0; `git core.autocrlf` per `.gitattributes` (LF). No path, encoding or line-ending errors occurred.

| Area | Result | Evidence |
|---|---|---|
| Configuration loading | PASS: `.env` loads; `execution_mode=disabled`, no cost schedule; the synthetic staging schedule is refused by development settings | `test_staging_readiness.py` guard tests |
| Database connection | PASS: `/health/ready` = ready against native PostgreSQL | API checks below |
| Migration state (development `agentx`) | PASS: backed up, `0009 → 0010 → 0011`, `alembic check` clean, history unchanged | [evidence/2026-11-12_dev_migration_0011.txt](evidence/2026-11-12_dev_migration_0011.txt) |
| Migration round trip on a disposable database (step 3) | NOT RUN: `agentx_app` has no CREATEDB and no administrator login was used. Covered on `agentx_test` by `test_migrations_round_trip_and_match_the_models` (PASS) | — |
| Full test suite (step 5) | PASS: 1455 passed, 0 failed, 0 skipped (see "Test suite" below) | — |
| Backend start-up, health and readiness (step 6) | PASS: `uvicorn --factory app.main:build_app --port 8010`; `/health` ok, `/health/ready` ready; `/exchange/status` disabled with `EXECUTION_DISABLED` and `COSTS_NOT_CONFIGURED`; new endpoints respond; operator view 403 for a non-operator | session log |
| Shutdown and recovery | PASS: server stopped (connection refused), restarted, ready again on the migrated database | session log |
| Docker Compose (step 7) | PASS: `docker compose build api`; `run --rm migrate` → `alembic current` = `0011 (head)`; `up -d api` → ready, healthy, unauthenticated `/exchange/status` = 401; `down` clean | session log |
| External ingestion (step 8) | PASS: `scripts\windows\ingest_intraday.ps1` skips outside session hours (logged `SKIPPED`); a manual run (`-IgnoreMarketHours`) → `SUCCEEDED`, 149 real bars archived with their real recording time (none back-dated) | `data\ingestion.log`, `data\ingestion_reports\` (local) |
| Ingestion schedule registration | PENDING (owner): `register_ingestion_task.ps1` was not run by the assistant (it creates a persistent scheduled task) | STAGING.md §1 |
| Mock Exchange staging configuration | PENDING: needs `agentx_staging` (administrator) | STAGING.md §2 |
| Staging validator, both runs | PENDING: needs fresh intraday data and `agentx_staging` | STAGING.md §5 |
| Order lifecycle on staging | PENDING (same prerequisites). The same runner passes on `agentx_test` through the HTTP API with realistic lags over one and two sessions | `test_lifecycle_*` |

### Test suite
`python -m pytest -q` from `backend\` against `agentx_test` (2026-11-12): **1455 passed, 0 failed, 0 skipped, 1 warning**
(Starlette's `httpx` deprecation notice, third-party), 654.56 s.
