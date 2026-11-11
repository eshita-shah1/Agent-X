# Agent-X Backend — Setup (Phases 1-4)

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
| `BACKTEST_JOB_HEARTBEAT_TIMEOUT_SECONDS` | Operational (default 600). A `QUEUED`/`RUNNING` backtest whose heartbeat is older than this is marked `INTERRUPTED` when its owner next starts one. |

Backtest models (fill model, sizing, cost defaults of zero, warm-up default of 2 sessions) are fixed in code and recorded
in every run; they are not environment settings. See [BACKTESTING.md](BACKTESTING.md).

| `EXECUTION_MODE` | Phase 4. `disabled` (default), `verified_next_bar_open`, or `simulation_provider_native` (Yahoo data, simulation only; see [D4_EXECUTION_READINESS.md](D4_EXECUTION_READINESS.md)). Enabled modes refuse to start unless every safeguard holds. |
| `EXECUTION_APPROVAL_REFERENCE` | Phase 4. Owner approval id/date; required for any enabled mode. |
| `EXECUTION_APPROVED_PRICE_BASES` | Phase 4. JSON list, default `[]`. Verified mode only; must be empty in simulation mode. `UNVERIFIED_PROVIDER_NATIVE` can never be listed. |
| `EXECUTION_APPROVED_PROVIDERS` | Phase 4. JSON list of provider codes, default `[]`; exactly one is required. |
| `EXECUTION_CANDLE_TIMEFRAME` | Phase 4. Default `5m`; must be `1m`, `5m` or `15m`. |
| `EXECUTION_MAX_PRICE_AGE_SECONDS` | Phase 4. Default `900`, the **provisional** 15-minute stale threshold. |
| `EXCHANGE_COST_SCHEDULE_PATH` | Phase 4. Path to the effective-dated cost schedule (JSON; format in [cost_schedule.template.json](cost_schedule.template.json)). **No rates ship with the code.** Required to enable execution. |
| `EXCHANGE_PRODUCT` | Phase 4. `DELIVERY` (default; the only executable product). |
| `EXCHANGE_MAX_VOLUME_PARTICIPATION` | Phase 4. Optional, in (0, 1]. Enables partial fills (max share of a bar's volume per fill). |

Phase 4 needs no new dependencies and no PostgreSQL extensions.

### macOS note (Phase 4 development machine)
Phase 4 was developed and tested on macOS with Homebrew `python@3.14` (3.14.10) and `postgresql@18` (18.x). The cluster ran on port 5433 because another PostgreSQL already used 5432, so `DB_PORT=5433` in that machine's `.env`. Use `.venv/bin/python` instead of `.\.venv\Scripts\python.exe`.

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

Test layout: `tests/indicators`, `tests/strategies` (including `selection/`) and `tests/backtest` are pure and fast
(`pytest tests/backtest -q`); `tests/integration/test_backtest_*.py` cover migration 0009, the runner, the API, user
isolation and reproducibility against the test database.

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

## Backtests (Phase 3)

Backtests run only when an authenticated user requests one (`POST /api/v1/backtests`). They read the archive and never
contact a provider, so run a backfill first. One run per user may be active at a time. Endpoints:
[API.md](API.md); behaviour and limitations: [BACKTESTING.md](BACKTESTING.md).

```powershell
cd backend
..\.venv\Scripts\python.exe -m alembic upgrade head                      # through 0009_backtest_runs_and_results (single head)
```

Back up a database that holds data before migrating it, for example
`pg_dump -h 127.0.0.1 -U agentx_app -d agentx -Fc -f <backup-file>.dump` (supply the password through `PGPASSWORD`,
never on the command line or in a committed file).

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


## Operations (Phase 4)

See [OPERATIONS.md](OPERATIONS.md). Summary:
- **Daily ingestion for an external scheduler:** `python -m app.ops.cli daily`. It is one-shot and idempotent; exit
  codes are documented in OPERATIONS.md.
- **Latency of archived bars (B7):** `python -m app.ops.cli latency --days 5`.
- **Operators:** `python -m app.ops.cli set-operator --email ... --on|--off` (server-side only).
- **Contract-note check (B2):** `python -m app.ops.cli check-contract-note note.json`.
- **Staging (B9):** [STAGING.md](STAGING.md). **Windows (B8):** [WINDOWS_VALIDATION.md](WINDOWS_VALIDATION.md).
