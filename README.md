# Agent-X

**Autonomous intraday paper-trading agent for the Indian NSE market.**

Agent-X is a deterministic quantitative trading and risk-management system with AI-assisted explanations. It collects market data, evaluates strategies, backtests them, compares them with Buy-and-Hold, and paper-trades through a custom Mock Exchange under strict risk controls.

> **Paper trading only.** There is no real-money execution, no broker order placement and no real capital movement anywhere in this codebase, and none will be added.

## Core principles

- **Deterministic trading.** Strategy, selection, risk and execution logic is deterministic and independently testable. An LLM may explain decisions but can never generate signals, authorize orders, or change risk settings or strategy parameters.
- **Data integrity first.** No fabricated candles, no silent provider switching, no stale data authorizing entries. Raw provider data and adjusted data are stored separately and raw data is never overwritten.
- **Three distinct components** sharing common infrastructure without being tightly coupled:
  1. Strategy Backtesting Engine
  2. Buy-and-Hold Benchmark Engine
  3. Delayed Live Market Paper-Trading Engine (its execution-delay model is a separate design decision; the observed Yahoo data itself is near-real-time)
- **Backtests are advisory.** Results never automatically enable strategies or modify strategy parameters, selection or risk settings.
- **Risk enforced at the execution boundary**, inside the Mock Exchange.
- **User isolation.** Every user has independent accounts, positions, history and configuration.

## Status

| Phase | Scope | Status |
|---|---|---|
| 0 | Requirement validation and data research | Done (decisions recorded with the project owner) |
| 1 | Backend foundation: FastAPI, PostgreSQL, Alembic, JWT auth, user isolation, base schema, Docker, tests | Approved, committed and pushed |
| 2 | Market-data foundation: instrument registry, Yahoo/`yfinance` adapter, validation, archive with provenance, ingestion jobs, coverage/freshness, authenticated APIs | **Implemented; the eight Phase 2 decisions are applied. Awaiting owner acceptance (not committed or pushed)** |
| 3 | Indicators, regimes, strategies, selector | Not started |
| 4 | Risk engine and Mock Exchange | **Implemented, pending owner approval.** Simulated fills blocked until decision D4 ([docs/PHASE4.md](docs/PHASE4.md)) |
| 5 | Agent orchestration | Not started |
| 6 | Backtesting and Buy-and-Hold benchmark | Not started |
| 7 | LLM layer and trading journal | Not started |
| 8 | Hardening and deployment | Not started |

Each phase is reviewed and approved by the project owner before it is committed, tagged (`phase-N-...`) and followed by the next. Futures support stays disabled until genuine contract-specific data exists; equities come first.

**What exists today:** user registration and JWT login, a per-user virtual account with an immutable initial-capital baseline, health endpoints, and a market-data foundation: 10 NSE equities registered (NIFTY/BANKNIFTY futures are disabled placeholders), a Yahoo Finance adapter, a validated PostgreSQL archive of 5-minute candles with provenance, restatement tracking, raw/adjusted separation, coverage and freshness reporting, ingestion jobs and authenticated market-data APIs. The first 60-day backfill was collected and its actual coverage is documented in [docs/MARKET_DATA.md](docs/MARKET_DATA.md). Phase 4 adds a deterministic risk engine and a Mock Exchange (orders, fills at the next bar's open, an Indian-equity component cost model, append-only cash ledger, long-only positions, risk-state refresh); no order can fill until an execution-price source is approved (decision D4, [docs/PHASE4.md](docs/PHASE4.md)). There is no strategy, agent, backtesting or LLM functionality yet.

**Market data in brief** (details and evidence in [docs/MARKET_DATA.md](docs/MARKET_DATA.md)):

- Yahoo Finance is the approved MVP provider. Its data is accepted as delivered, with its limitations detected and documented, never relabelled.
- In testing, Yahoo's 5-minute data was **near-real-time**: the latest row is the bar still forming. No fixed delay is assumed or guaranteed. Only *finalised* bars (ended at least 60 s ago, provisional) are archived.
- Prices are provider-native and labelled `UNVERIFIED_PROVIDER_NATIVE`: Yahoo's `Close` is split-adjusted even unadjusted. Which prices may feed simulated fills is decided in Phase 4.
- The first observation of a bar stays canonical; later differing observations are kept as separate versions.
- Holidays are not authoritatively known: closures are *inferred* and labelled so; other absences stay unexplained.
- Ingestion is operator-initiated (CLI or API); nothing runs on start-up and there is no scheduler. Any authenticated user may trigger it locally; this must be restricted to an operator before hosted deployment.
- Provider precision is kept; prices are not normalised to tick sizes. Futures are disabled.

## Tech stack

Python 3.14 · FastAPI · PostgreSQL 18 · SQLAlchemy 2 · Alembic · psycopg 3 · argon2id · PyJWT · pandas · NumPy · `yfinance` · pytest · Docker Compose.
Planned: LangGraph. Scheduling (e.g. APScheduler) is not used; it may be reconsidered in a later phase.

## Quick start

Requires Python 3.14, a running native PostgreSQL with two databases (`agentx`, `agentx_test`) and a non-superuser role, and (optionally) Docker Desktop.

```powershell
copy .env.example .env      # then fill in credentials and a JWT secret; never commit .env
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r backend\requirements-dev.txt
cd backend
..\.venv\Scripts\python.exe -m alembic upgrade head
..\.venv\Scripts\python.exe -m pytest -q
..\.venv\Scripts\python.exe -m uvicorn --factory app.main:build_app --port 8000
```

Interactive API docs: `http://localhost:8000/docs`. Full instructions, including Docker Compose, are in [docs/SETUP.md](docs/SETUP.md).

## Documentation

- [docs/SETUP.md](docs/SETUP.md): environment, configuration, running, testing, migrations
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md): structure, authentication, isolation, raw/adjusted price separation
- [docs/MARKET_DATA.md](docs/MARKET_DATA.md): Phase 2 market-data design, verified Yahoo behaviour, archive contents, limitations, operations
- [docs/API.md](docs/API.md): endpoints
- [docs/PHASE4.md](docs/PHASE4.md): risk engine, Mock Exchange, decision D4, shared contracts, limitations

## Repository layout

```
backend/    FastAPI application, Alembic migrations, tests
docs/       setup, architecture and API documentation
docker-compose.yml, .env.example, .gitignore, .gitattributes
```

## Security note

Credentials live only in a git-ignored `.env`. This project is private during development. The current security posture is suitable for local use; items to address before any hosted deployment (refresh tokens, revocation, rate limiting, row-level security, secrets management) are listed in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).
