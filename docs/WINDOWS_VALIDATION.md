# Windows validation (B8): commands for the integration machine

**Status:** not yet run. The development machine is macOS, and no Windows environment was available, so Windows
compatibility is **not claimed**. Run the steps below in PowerShell on the Windows integration machine, from the
repository root. Record the output of each step. Any failure is a defect to fix before merging.

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
..\.venv\Scripts\python.exe -m alembic current     # expect: 0008 (head)
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
docker compose run --rm migrate alembic current          # expect: 0008 (head)
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
