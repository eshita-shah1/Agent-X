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
  3. Delayed Live Market Paper-Trading Engine
- **Backtests are advisory.** Results never automatically enable strategies or modify strategy parameters, selection or risk settings.
- **Risk enforced at the execution boundary**, inside the Mock Exchange.
- **User isolation.** Every user has independent accounts, positions, history and configuration.

## Status

| Phase | Scope | Status |
|---|---|---|
| 0 | Requirement validation and data research | Done (decisions recorded with the project owner) |
| 1 | Backend foundation: FastAPI, PostgreSQL, Alembic, JWT auth, user isolation, base schema, Docker, tests | **Implemented, pending owner approval** |
| 2 | Market-data infrastructure (Yahoo/`yfinance` adapter, archiver, validation) | Not started |
| 3 | Indicators, regimes, strategies, selector | Not started |
| 4 | Risk engine and Mock Exchange | Not started |
| 5 | Agent orchestration | Not started |
| 6 | Backtesting and Buy-and-Hold benchmark | Not started |
| 7 | LLM layer and trading journal | Not started |
| 8 | Hardening and deployment | Not started |

Each phase is reviewed and approved by the project owner before it is committed, tagged (`phase-N-...`) and followed by the next. Futures support stays disabled until genuine contract-specific data exists; equities come first.

**What exists today:** user registration and JWT login, a per-user virtual account with an immutable initial-capital baseline, health endpoints, and the database foundation (users, accounts, audit events, and market-data tables with raw/adjusted price separation). There is no market-data ingestion, strategy, risk, exchange, agent, backtesting or LLM functionality yet.

## Tech stack

Python 3.14 · FastAPI · PostgreSQL 18 · SQLAlchemy 2 · Alembic · psycopg 3 · argon2id · PyJWT · pytest · Docker Compose.
Planned: `yfinance` (initial delayed data), pandas/NumPy, LangGraph, APScheduler.

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
- [docs/API.md](docs/API.md): endpoints

## Repository layout

```
backend/    FastAPI application, Alembic migrations, tests
docs/       setup, architecture and API documentation
docker-compose.yml, .env.example, .gitignore, .gitattributes
```

## Security note

Credentials live only in a git-ignored `.env`. This project is private during development. The current security posture is suitable for local use; items to address before any hosted deployment (refresh tokens, revocation, rate limiting, row-level security, secrets management) are listed in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).
