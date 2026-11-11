# Agent-X Backend — Setup (Phases 1-2)

Paper trading only. No real-money execution exists in this codebase.

## Prerequisites

| Tool | Verified version | Notes |
|---|---|---|
| Python | 3.14.3 (Windows), 3.14.10 (Docker image) | Phase 1 and Phase 2 dependencies (including `pandas` 3.1.1, `numpy` 2.5.4, `yfinance` 1.8.0, `curl_cffi` 0.17.0) install from prebuilt wheels and run on both. LangGraph and APScheduler are **not** installed yet (a dry run resolved them on 3.14, untested at runtime). |
| PostgreSQL | 18.6 (native Windows) | Not run through Docker. Two databases: `agentx` and `agentx_test`. |
| Docker Desktop | Engine 29.2.0, Compose v5.0.2 | Used for the API container only. |

### Database role

The application connects as `agentx_app`, a **non-superuser** (no `CREATEROLE`, `CREATEDB`, `BYPASSRLS`). It owns the tables in both databases. Phase 1 needs **no PostgreSQL extensions** beyond the built-in `plpgsql`, and a test asserts this, so no administrator step is required. A later phase that needs an extension must say so explicitly.

## Configuration

Configuration is read from environment variables and a git-ignored `.env` in the project root. Copy `.env.example` to `.env` and fill it in. **Never commit `.env`.**

| Variable | Purpose |
|---|---|
| `DB_HOST`, `DB_PORT`, `DB_USER`, `DB_PASSWORD` | Database connection. Use `DB_HOST=127.0.0.1` on the host (not `localhost`, see below); the Compose service overrides it to `host.docker.internal`. |
| `DB_NAME`, `DB_TEST_NAME` | Main and test databases. The test name must end in `_test` (tests truncate tables and refuse to run otherwise). |
| `JWT_SECRET_KEY` | At least 32 characters. Generate with `python -c "import secrets;print(secrets.token_urlsafe(48))"`. |
| `JWT_ALGORITHM`, `JWT_ISSUER` | HS256/384/512 only. |
| `ACCESS_TOKEN_EXPIRE_MINUTES` | Token lifetime. The value in `.env.example` is a development placeholder, not a product decision. |
| `DB_CONNECT_TIMEOUT_SECONDS` | Optional, default 5. Bounds how long `/health/ready` waits for an unreachable database. |
| `API_PORT` | Host port for the container (Compose). Default 8000; this machine's `.env` uses 8010 because other local projects hold 8000. |
| `MARKETDATA_PROVIDER` | Active provider (default `yahoo`). There is no automatic fallback. |
| `MARKETDATA_FINALIZATION_MARGIN_SECONDS` | **Provisional** (default 60, retained by the owner for Phase 2). A bar is archived once `observed_at >= bar_end + margin`. Not a guarantee of Yahoo behaviour. |
| `MARKETDATA_STALE_AFTER_MINUTES`, `MARKETDATA_PROVIDER_UNAVAILABLE_AFTER_FAILURES` | **Provisional** (defaults 15 and 3). Descriptive only: they label data and gate nothing. Trading-engine use needs a separate review. |
| `MARKETDATA_INCREMENTAL_OVERLAP_HOURS`, `MARKETDATA_JOB_HEARTBEAT_TIMEOUT_SECONDS`, `MARKETDATA_REQUEST_PAUSE_SECONDS` | Operational (defaults 24, 300, 1.5). |

## Run on the host

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r backend\requirements-dev.txt

cd backend
..\.venv\Scripts\python.exe -m alembic upgrade head                      # main database
..\.venv\Scripts\python.exe -m alembic -x db=test upgrade head           # test database (tests also do this)
..\.venv\Scripts\python.exe -m uvicorn --factory app.main:build_app --port 8000
```

Interactive API docs: `http://localhost:8000/docs`.

## Run tests

```powershell
cd backend
..\.venv\Scripts\python.exe -m pytest -q
```

Integration tests run against the real `agentx_test` PostgreSQL (the immutability triggers, check constraints and `timestamptz` behaviour cannot be exercised on SQLite); each starts from truncated tables. Pure unit tests (`tests/unit`, `tests/marketdata`) never touch the database. **The suite never touches the network or the real `yfinance`**: Yahoo is replaced by a deterministic fake provider. Do not run the opt-in probe or the ingestion CLI against `agentx_test`.

## Market data operations (Phase 2)

Nothing market-related runs on application start-up and there is no scheduler; ingestion is always started explicitly
by an operator (approved for Phase 2). Locally, any authenticated user may also start it through the API; restrict this
to an operator before any hosted deployment. Full details and findings:
[MARKET_DATA.md](MARKET_DATA.md).

```powershell
cd backend
..\.venv\Scripts\python.exe -m alembic upgrade head                      # includes 0003_market_data_foundation
..\.venv\Scripts\python.exe -m app.marketdata.cli seed                  # register the approved instruments (idempotent)
..\.venv\Scripts\python.exe -m app.marketdata.cli probe --persist       # opt-in; uses the network; records capability evidence
..\.venv\Scripts\python.exe -m app.marketdata.cli backfill --days 60
..\.venv\Scripts\python.exe -m app.marketdata.cli incremental
..\.venv\Scripts\python.exe -m app.marketdata.cli coverage --days 60
..\.venv\Scripts\python.exe -m app.marketdata.cli status
```

In Docker: `docker compose run --rm api python -m app.marketdata.cli <command>`. Probe reports are written to the git-ignored `data/probe_reports/`.

## Run with Docker Compose

Docker Desktop must be running.

```powershell
docker compose build api
docker compose run --rm migrate        # alembic upgrade head, from inside a container
docker compose up -d api               # http://localhost:%API_PORT%/health/ready
docker compose logs -f api
docker compose stop api
```

PostgreSQL is deliberately **not** a Compose service: the native instance is used.

### Port and listener caveats on this machine

- **Use `127.0.0.1`, not `localhost`.** On the development machine `localhost` resolves to `::1` first, and Docker Desktop's backend can hold the IPv6 `5432` listener for an unrelated container's PostgreSQL. `127.0.0.1` always reaches the native PostgreSQL 18.6 instance (verified from the host, and from containers via `host.docker.internal`).
- Other local projects' containers may occupy common ports such as 8000. Change `API_PORT` in `.env` if the API host port clashes.

## Migrations

Alembic is configured in `backend/alembic`. The URL is built from `.env`, never stored in `alembic.ini`. `-x db=test` targets the test database.

```powershell
..\.venv\Scripts\python.exe -m alembic revision --autogenerate -m "description"
..\.venv\Scripts\python.exe -m alembic upgrade head
..\.venv\Scripts\python.exe -m alembic check        # fails if models and migrations drift
```

Review every autogenerated migration. Triggers and some constraints are hand-written (see `0001_foundation.py`).
