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
| 2 | Market-data foundation: instrument registry, Yahoo/`yfinance` adapter, validation, archive with provenance, ingestion jobs, coverage/freshness, authenticated APIs | Approved, committed and pushed |
| 3 | Indicators, five strategies, regime classification and session-locked selection, backtesting engine (NEXT_BAR_OPEN_V1), Buy-and-Hold benchmark, persisted user-owned backtest runs and APIs (approved decisions D1-D19) | **Implemented, tested and approved by the owner** |
| 4 | Risk engine and Mock Exchange | **Approved by the owner (2026-11-12).** Simulation-only staging approved; the staging run on real fresh data is pending the 2026-11-12 session. Simulated fills stay disabled outside staging until the real-environment items in [docs/D4_EXECUTION_READINESS.md](docs/D4_EXECUTION_READINESS.md) §7.3 |
| 5 | Agent orchestration | **Implemented, merged to `main` and tagged `phase-5-agent-orchestration`**. Runs SIGNALS_ONLY while execution is disabled; end-to-end paper execution awaits decision D15 ([docs/PHASE5.md](docs/PHASE5.md)) |
| 6 | LLM explanations and trading journal | **Implemented, tested and approved by the owner** (branch `phase-6-journal-explanations`, not yet pushed or merged; development database not yet migrated to `0013`) ([docs/PHASE6.md](docs/PHASE6.md)). Backtesting and Buy-and-Hold, the original Phase 6, were delivered in Phase 3 |
| 7 | (LLM layer and trading journal, moved into Phase 6) | Delivered in Phase 6 |
| 8 | Hardening and deployment | Not started |

Each phase is reviewed and approved by the project owner before it is committed, tagged (`phase-N-...`) and followed by the next. Futures support stays disabled until genuine contract-specific data exists; equities come first.

**What exists today:** user registration and JWT login, a per-user virtual account with an immutable initial-capital baseline, health endpoints, and a market-data foundation: 10 NSE equities registered (NIFTY/BANKNIFTY futures are disabled placeholders), a Yahoo Finance adapter, a validated PostgreSQL archive of 5-minute candles with provenance, restatement tracking, raw/adjusted separation, coverage and freshness reporting, ingestion jobs and authenticated market-data APIs. The first 60-day backfill was collected and its actual coverage is documented in [docs/MARKET_DATA.md](docs/MARKET_DATA.md).

Phase 3 adds the following:
- **Strategy layer:** deterministic indicators, and five long-only intraday strategies (ORB, VWAP cross, EMA crossover, RSI/Bollinger mean reversion, Supertrend) with approved initial defaults.
- **Selection:** regime classification (ADX at 09:30 IST), and per-instrument strategy selection locked for each session.
- **Backtesting engine:** next-bar-open fills, stop-first protective exits, mandatory 15:15 square-off, configurable costs (default zero, `GROSS_OF_COSTS`) and Decimal accounting.
- **Benchmark:** an independent Buy-and-Hold comparison.
- **Fingerprints:** SHA-256 fingerprints for reproducibility.
- **Runs:** user-owned, immutable backtest runs started through the API as background jobs.

Backtests are advisory and never select or activate strategies; about 60 days of data is not statistically sufficient (see [docs/BACKTESTING.md](docs/BACKTESTING.md)).

Phase 4 adds a deterministic risk engine and a Mock Exchange (orders, fills at the next bar's open, an Indian-equity component cost model, append-only cash ledger, long-only positions, risk-state refresh); execution is disabled by default; for the MVP, fills will be **simulation-only** on unverified Yahoo Finance data once approved ([docs/D4_EXECUTION_READINESS.md](docs/D4_EXECUTION_READINESS.md)). **Five-year intraday backtesting is not available at launch**: Yahoo provides about 60 days of 5-minute history and the archive grows only prospectively.

The Phase 3 backtest and the Phase 4 Mock Exchange are separate: backtests simulate their own NEXT_BAR_OPEN_V1 fills
and never place orders. Phase 5 adds the autonomous paper-trading agent ([docs/PHASE5.md](docs/PHASE5.md)).

Phase 6 adds understanding and review, not control:
- **Explanations:** layered explanations of signals, agent decisions, risk decisions, orders, fills and trading days,
  built only from stored records, with their evidence.
- **Optional LLM narrative:** disabled by default; a local OpenAI-compatible server or Claude can be used. Every number
  a narrative states must come from the facts, and any failure falls back to the rule-based text.
- **Trading journal:** automatic entries from trading records, manual notes, tags, full-text search, reflections and
  descriptive analytics.

The LLM never generates signals, sizes, submits or changes anything ([docs/PHASE6.md](docs/PHASE6.md)). There is no
live trading.

Agent execution approval (D15) and the Phase 4 staging run (B9) remain separate, outstanding milestones.

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
Optional (Phase 6): an OpenAI-compatible LLM server or the `anthropic` SDK for explanation narratives; none is required. Scheduling (e.g. APScheduler) is not used.

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

**Frontend** (React + Vite; the agent panel, Trade Journal and Daily Insights use the real API, other pages are still mock data; see [docs/FRONTEND.md](docs/FRONTEND.md)):
```bash
cd frontend && npm ci && npm run dev    # http://localhost:5173; /api is proxied to the backend on :8000
```
Open `Agent-X.code-workspace` in VS Code to work on the backend and frontend from one window; it includes tasks for both.

## Documentation

- [docs/SETUP.md](docs/SETUP.md): environment, configuration, running, testing, migrations
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md): structure, authentication, isolation, raw/adjusted price separation
- [docs/MARKET_DATA.md](docs/MARKET_DATA.md): Phase 2 market-data design, verified Yahoo behaviour, archive contents, limitations, operations
- [docs/BACKTESTING.md](docs/BACKTESTING.md): Phase 3 indicators, strategies, selection, fill model, costs, stops/targets, benchmark, fingerprints, persistence (migration 0009), relationship to Phase 4 execution and limitations
- [docs/API.md](docs/API.md): endpoints
- [docs/PHASE4.md](docs/PHASE4.md): risk engine, Mock Exchange, decision D4, shared contracts, limitations
- [docs/D4_EXECUTION_READINESS.md](docs/D4_EXECUTION_READINESS.md): simulation policy, cost model, corporate actions, dividends, blockers
- [docs/PHASE5.md](docs/PHASE5.md): the autonomous paper-trading agent: lifecycle, decisions, reconciliation, worker, migration 0012, limitations
- [docs/PHASE6.md](docs/PHASE6.md): explanations, the optional LLM narrator and its fallback, the trading journal, analytics, migration 0013, security, limitations
- [docs/OPERATIONS.md](docs/OPERATIONS.md): daily ingestion scheduling, latency and thresholds, operators, corporate actions, dividends, the agent worker
- [docs/STAGING.md](docs/STAGING.md) and [docs/WINDOWS_VALIDATION.md](docs/WINDOWS_VALIDATION.md): validation procedures
- [docs/FRONTEND.md](docs/FRONTEND.md): the web UI, how it connects to the API, and page-by-page integration gaps

## Architecture in brief

Modular monolith (FastAPI, SQLAlchemy, PostgreSQL). Dependencies point one way:

```
marketdata (archive) ──read──> backtest_runs (database runner) ──> backtest (pure engine)
                                                                    └─> strategies + selection ──> indicators
marketdata (archive) ──read──> orders ──> risk (engine) ──> exchange (Mock Exchange, costs, accounting)
api/v1 (auth, account, market, backtests, trading, operator, agent) ──> services above
agent ──> orders (OrderService) ──> risk ──> exchange                       (Phase 5)
api/v1 (explanations, journal) ──> explanations, journal ──read──> all of the records above   (Phase 6)
                                   └──> llm (optional narrator: settings in, validated text out)
```

The pure layers (indicators, strategies, selection, backtest engine) have no database, network, clock or LLM
dependencies. The Phase 3 backtest and the Phase 4 execution path never import each other. Nothing below the API imports
the Phase 6 packages, which write only journal and explanation tables. Tests enforce these boundaries. Details: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Roadmap

1. **Phase 3:** strategy and backtesting layer, implemented and approved; integrated with Phase 4 (migration `0009`).
2. **Phase 4:** risk engine and Mock Exchange, implemented. Fills stay simulation-only until decision D4 ([docs/PHASE4.md](docs/PHASE4.md)).
3. **Decided:** the backtest and the Mock Exchange keep separate simulation models (intraday research vs delivery paper execution; [docs/BACKTESTING.md](docs/BACKTESTING.md) §9).
4. **Phase 5:** agent orchestration, merged and tagged ([docs/PHASE5.md](docs/PHASE5.md)). Execution approval (D15) and the Phase 4 staging run (B9) are separate, outstanding milestones.
5. **Phase 6:** LLM explanations and trading journal, implemented and approved ([docs/PHASE6.md](docs/PHASE6.md)). The LLM explains decisions only and never decides them. (This was Phase 7; Phase 6 was absorbed into Phase 3.)
6. **Phase 8:** hardening and deployment. This includes operator-only ingestion, refresh tokens and row-level security.

## Repository layout

```
backend/    FastAPI application, Alembic migrations, tests
frontend/   React + TypeScript + Vite web UI (agent panel, journal and insights on the real API; other pages mock)
docs/       setup, architecture, market-data, backtesting, API and frontend documentation
docker-compose.yml, .env.example, .gitignore, .gitattributes, Agent-X.code-workspace
```

## Security note

Credentials live only in a git-ignored `.env`. This project is private during development. The current security posture is suitable for local use; items to address before any hosted deployment (refresh tokens, revocation, rate limiting, row-level security, secrets management) are listed in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).
