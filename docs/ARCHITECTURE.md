# Agent-X Backend — Architecture (Phases 1-6)

Modular monolith: FastAPI + SQLAlchemy 2 (sync, psycopg 3) + Alembic + PostgreSQL. Phase 1 is the foundation (auth,
isolation, base schema); Phase 2 adds the market-data foundation (see [MARKET_DATA.md](MARKET_DATA.md)); Phase 3 adds
indicators, strategies, regime-based selection, the backtesting engine and the Buy-and-Hold benchmark (see
[BACKTESTING.md](BACKTESTING.md)); Phase 4 adds the risk engine, the Mock Exchange, simulation-only execution, cost
schedules, corporate-action adjustments, dividends and the operator role (see [PHASE4.md](PHASE4.md)); Phase 5 adds the
autonomous paper-trading agent that orchestrates them (see [PHASE5.md](PHASE5.md)); Phase 6 adds explanations, an
optional LLM narrator and the trading journal, all read-only towards trading (see [PHASE6.md](PHASE6.md)). Live trading
and broker integration are **not implemented** and will not be added.

## Layout

```
backend/
  app/
    main.py            app factory (create_app) and health routes
    core/              config (pydantic-settings), security (argon2id, JWT), logging
    db/
      base.py          declarative Base, naming convention, UserOwnedMixin
      enums.py         InstrumentType, Timeframe, PriceBasis, AdjustmentMethod, CorporateActionType, job/batch/backtest enums
      session.py       engine (UTC session timezone) / sessionmaker / per-request session
      scoping.py       owned_select(): user-scoped queries
      models/          user.py, market.py (instruments, candles, adjusted candles, corporate actions), ingestion.py,
                       backtest.py (runs and results)
    marketdata/        providers/ (base interfaces, yahoo adapter, registry), pipeline, archive, adjustment, gaps,
                       freshness, calendar, jobs, service, seed, registry (approved universe), probe, cli
    indicators/        incremental float64 indicators (pure)
    strategies/        five long-only strategies, configs, registry; selection/ (regime, candidates, session lock)
    backtest/          pure backtesting engine: fills, costs, accounting, benchmark, fingerprints (no DB)
    backtest_runs/     read-only data loader, background runs, atomic persistence
    exchange/, risk/, orders/, ops/   Phase 4 (see below)
    agent/             Phase 5 (see below)
    explanations/      Phase 6: read-only facts, trade episodes, rule-based text, explanation service
    llm/               Phase 6: optional, provider-neutral narrator (no database, no services)
    journal/           Phase 6: journal service, automatic-entry sync, reflections, analytics
    api/v1/            auth, account, market, trading, operator, backtests, agent, explanations, journal routers; deps; schemas
  alembic/             migrations 0001-0013 (see "Migration chain")
  tests/               unit/, marketdata/, indicators/, strategies/, backtest/ (pure, no DB),
                       integration/ (real PostgreSQL); support.py (fake provider/clock)
```

Later phases add packages beside these. Empty placeholders were not created.

Phase 4 added (details in [PHASE4.md](PHASE4.md)):

```
  app/
    core/clock.py      injectable UTC clock
    db/models/trading.py   risk_settings, risk_state, orders, fills, cash_ledger, positions, risk_decisions
    exchange/          pricing (execution-price contract + gate, D4), costs, accounting (pure),
                       portfolio (valuation), service (MockExchange), types (OrderIntent)
    risk/              engine (pure risk evaluation), service (settings, state, snapshot, kill switch)
    orders/            service (OrderService: intent -> risk -> exchange, one transaction)
    api/v1/trading.py  orders, portfolio, risk and exchange-status endpoints
  alembic/versions/0004_risk_engine_and_mock_exchange.py, 0005_next_bar_execution_and_component_fees.py
```

## Authentication

- Passwords: argon2id (`argon2-cffi` defaults), hashes upgraded on login if parameters change. Minimum length 12 (a security floor, not a trading parameter).
- JWT (PyJWT): HS256 by default; `sub`, `iss`, `iat`, `exp` are all required. Algorithms are fixed server-side (never trusted from the token header), so `alg: none` and wrong-secret tokens are rejected. Access tokens only; there are no refresh tokens or logout yet.
- Login does a password verification even for unknown emails (against a dummy hash) so timing does not reveal registered emails; errors are identical for "unknown user" and "wrong password".
- Security events (`user.registered`, `auth.login_succeeded`, `auth.login_failed`) go to `audit_events`, which is append-only (trigger). Passwords are never stored in it.

## Multi-user isolation

1. Identity comes **only** from the verified JWT via `get_current_user`. No endpoint accepts a user id; client-supplied `user_id` fields, query parameters and headers are ignored (tested).
2. User-owned tables use `UserOwnedMixin` (non-null FK `user_id`), and queries go through `owned_select`, which refuses non-owned models.
3. `test_every_table_is_classified` forces every new table to be declared global, system or user-owned, so a later phase cannot add an unowned table by accident.
4. Candles, instruments, providers and corporate actions are **global** by design (shared market data) and carry no `user_id`.

Not in Phase 1: PostgreSQL row-level security (possible later hardening), per-user rate limiting. Phase 2 adds one more
item for hosted use: any authenticated user can start an ingestion job. This is an approved **local-development policy
only**; restricting ingestion to an authorised administrator/operator is a prerequisite for hosted deployment. No role
system is added in Phase 2.

## Raw / adjusted price separation (schema level)

- `candles` stores provider-native OHLCV exactly as ingested. A database trigger rejects any `UPDATE` of prices, volume, timestamps, finality or identity columns. Only `price_basis` and `basis_verified_by` can change after insert.
- `price_basis` defaults to `UNVERIFIED_PROVIDER_NATIVE`. A check constraint forbids a verified label (`AS_TRADED_VERIFIED`, `SPLIT_ADJUSTED_VERIFIED`) without a recorded `basis_verified_by`. Nothing is called "raw" or "as traded" until verified.
- `adjusted_candles` is a separate table keyed to a candle, with method, source, factor and corporate-action-set version. It cannot modify `candles`.
- `corporate_actions` holds splits, bonuses and dividends, with required ratio/cash fields enforced by check constraints.
- Unique keys on `(instrument, provider, timeframe, bar_start_ts, observation_version)`, plus a partial unique index allowing one canonical version per bar, prevent duplicate candles and keep different providers' bars as separate rows (no silent provider switching). The first observation stays canonical; restated observations are additional non-canonical versions (approved Phase 2 policy).
- Timestamps are `timestamptz` (UTC instants); `session_date` records the exchange-local trading date. `observed_at` records when Agent-X received a bar, which later phases need for the no-lookahead eligibility rules.

Execution code (Phase 4) must read only `candles`, and only rows whose `price_basis` the owner has approved for execution. The execution-price source is **deferred to Phase 4**, and Phase 4 leaves it unapproved (decision D4): every candle is still `UNVERIFIED_PROVIDER_NATIVE` and nothing relabels it.

Phase 4 enforces this through `ExecutionPriceGate`. The default configuration approves no source, basis or provider, so nothing can fill. A check constraint on `fills` also forbids storing an `UNVERIFIED_PROVIDER_NATIVE` execution price.

## Account baseline and immutability

- `virtual_accounts.initial_capital` (`NUMERIC(18,2) NOT NULL`, no default, `> 0`) is the original account baseline, supplied at registration and used for historical performance calculations.
- A trigger (migration `0002`) rejects any `UPDATE` of `initial_capital`, `currency`, `user_id`, `id` or `created_at`. The baseline therefore cannot drift, whatever code runs.
- Cash is derived as `initial_capital + SUM(cash_ledger.amount)` from the append-only ledger (Phase 4). Available cash subtracts reservations held by open BUY orders. Equity adds positions marked with gate-eligible prices only. Margin is not modelled: the account is a long-only cash account.
- A future account reset must be a separate, explicitly authorised and audited operation. It is not an `UPDATE` of these columns and is not implemented.
- Registration creates the user, the virtual account and the audit event in a single transaction; failure injection tests confirm nothing is left behind.

## Phase 2: market data

Full design, verified provider behaviour, archive contents and limitations are in [MARKET_DATA.md](MARKET_DATA.md).
Summary of what changed in the foundation:

- New package `app/marketdata/` (providers, pipeline, archive, adjustment, gaps, freshness, calendar, jobs, service, probe,
  CLI). `MarketDataService` is the only downstream-facing entry point. Only `providers/yahoo.py` imports `yfinance`;
  pandas never leaves the adapter; the package does not import any trading logic (tests enforce this).
- Migration `0003`: provenance columns on `candles` (provider symbol, ingestion batch, source metadata), observation
  versioning (`observation_version`, `is_canonical`, one canonical version per bar), `is_final` required, the candle
  immutability trigger extended to the new columns, instrument registry safety rails (a futures instrument **cannot be
  enabled**; a disabled instrument must state why; equities carry no contract fields), and new tables:
  `instrument_provider_symbols`, `provider_capabilities`, `provider_health_state`, `data_ingestion_jobs`,
  `data_ingestion_batches`, `data_quality_issues`, `data_restatement_events`, `exchange_calendar_days`.
  `0001` and `0002` are unchanged. Downgrading `0003` is refused while any market data it would discard exists (candles, corporate actions, jobs, batches, quality issues, restatements, calendar entries, symbol mappings, capabilities, provider health, or instruments carrying registry fields).
- Table classification (enforced by a test): the market-data tables are global; `data_ingestion_jobs` is a system table
  whose nullable `requested_by_user_id` is used to show each user only their own jobs.
- The database session timezone is pinned to UTC so every timestamp is a UTC instant in every environment. Visible
  change for Phase 1 endpoints: timestamps such as the account's `created_at` are now always serialised as UTC with a
  `Z` suffix (previously the offset followed the PostgreSQL server's session timezone, e.g. `+05:30`). The instant is
  unchanged. Exchange-session calculations still use `Asia/Kolkata`.
- Approved Phase 2 decisions (details in MARKET_DATA.md §1a): execution-price source deferred to Phase 4; first
  observation canonical; provisional, configurable, descriptive freshness thresholds (60 s / 15 min / 3 failures);
  inferred closures stay inferred; operator-initiated ingestion with no scheduler; local-only ingestion permission for
  any authenticated user; near-real-time described as observed; tick-size normalisation deferred.
- `create_app(settings, provider_registry, clock)` accepts a provider registry and clock as test hooks. Nothing
  market-related runs on application start-up.

## Migration chain

One linear chain with a single head (`alembic heads` → `0013`):

| Revision | Purpose | Phase |
|---|---|---|
| `0001` | Foundation | 1 |
| `0002` | Immutable account fields | 1 |
| `0003` | Market-data foundation | 2 |
| `0004` | Risk engine and Mock Exchange | 4 |
| `0005` | Next-bar execution, component fees, risk refresh | 4 |
| `0006` | Simulation-only execution policy | 4 |
| `0007` | Corporate-action adjustments, cost schedule fields, order product | 4 |
| `0008` | Operator role, dividend credits, cash-in-lieu provenance | 4 |
| `0009` | Backtest runs and results | 3 |
| `0010` | Execution approval reference on orders and fills | 4 |
| `0011` | Protective-exit requirements (stale prices) | 4 |
| `0012` | Agent orchestration: instances, configuration versions, cycles, decisions, events, instrument state | 5 |
| `0013` | Journal entries, tags, reflections, stored trade explanations | 6 |

Phases 3 and 4 were developed in parallel, and Phase 4 reached `main` first. The Phase 3 migration was written as `0004`
and renumbered to `0009` (parent `0008`) at integration. Its schema is unchanged, and `0001`-`0008` are unchanged.

A database stamped `0004` by the pre-integration Phase 3 branch holds the backtest schema, not Phase 4's. It must not be
upgraded in place: restore a pre-`0004` backup and migrate to head.

## Phase 3: strategies and backtesting

Full design and limitations: [BACKTESTING.md](BACKTESTING.md).

- **Dependency direction.** The packages depend only downward:
  `indicators` <- `strategies` (+ `selection`) <- `backtest` (pure) <- `backtest_runs` (database) <- `api/v1/backtests`.
  Boundary tests enforce three rules:
  - indicators, strategies and selection never import the backtest;
  - the engine imports no database, API, account, clock or randomness;
  - neither the engine nor the runner contains order, exchange, provider, scheduler or LLM code.
- **No feedback.** Selection takes no outcome metric as input, and nothing writes backtest results back into configuration or selection (D15).
- **Migration `0009` (additive).** Six user-owned tables, each carrying a `user_id` FK to `users`:

  | Table | Holds |
  |---|---|
  | `backtest_runs` | The run |
  | `backtest_instrument_results` | Per-instrument results |
  | `backtest_selection_decisions` | Explained selection decisions |
  | `backtest_trades` | Trades |
  | `backtest_events` | Events |
  | `backtest_equity_points` | Per-bar equity |

  Supporting objects:
  - Result tables reference their run by `run_id`.
  - A partial unique index allows one `QUEUED`/`RUNNING` run per user.
  - Money columns are unconstrained `NUMERIC` (exact).
  - Three trigger functions back 11 triggers, enforcing immutability. A run's request, configuration, labels, fingerprint and owner never change; its status only moves forward, a finished run is frozen, and runs are never deleted. Result rows are inserted only while their run is `RUNNING` and has the same owner, and are never updated or deleted. A check constraint ties the fingerprints to `SUCCEEDED`.
  - Downgrading `0009` is refused while any run exists.
- **Table classification.** The six tables are user-owned (`test_every_table_is_classified`). API queries go through `owned_select`, and another user's run is `404`.
- **Accounts are read only.** A backtest reads the account's `initial_capital` as its default capital and never writes to the account.
- **Time zones.** The engine normalises every bar timestamp to UTC and serialises explanations in UTC. Session logic stays in Asia/Kolkata. Results are therefore identical whatever zone the inputs, or the database session, used.
- **Separate from Phase 4 execution.** No Phase 3 module imports the risk engine, Mock Exchange, order pipeline or operations code, and no Phase 4 execution module imports the backtest. A backtest never writes orders, fills, ledger entries or positions, and runs with execution disabled. The two simulation models differ by design, and are kept separate by decision ([BACKTESTING.md](BACKTESTING.md) §9).

## Phase 5: agent orchestration

Full design: [PHASE5.md](PHASE5.md).

- **Package `app/agent`** (configuration, lifecycle, positions, replay, pipeline, cycle, cli, guards) plus
  `api/v1/agent.py` and `api/v1/schemas_agent.py`. The agent orchestrates existing services:
  `MarketDataService` → the approved selector and strategies (deterministic replay) → `OrderService.submit` → Phase 4
  risk engine → Mock Exchange. It writes only its own six tables (migration `0012`).
- **Dependency direction.** Nothing below the API imports `app.agent`. The one exception, by design, is
  `api/v1/trading.py`, which calls `app.agent.guards` for the risk-settings lock. The agent imports no backtest,
  provider, scheduler, thread, network, LLM or broker code, and none of the Phase 4 formula modules (risk engine, costs,
  accounting, pricing, portfolio). `tests/agent/test_boundaries.py` enforces this.
- **Shared, not copied.** `app/strategies/configs.py` (D13) holds the JSON → configuration/policy builder and the
  corporate-action warm-up rule used by both the backtest runner and the agent. `app/exchange/readiness.py` holds the
  execution-readiness check used by `GET /exchange/status` and by the agent's mode decision.
- **No in-app scheduling.** The worker is a one-shot CLI run by an external scheduler; the Phase 2 guard against threads
  and schedulers still applies to every module.

## Phase 6: explanations and trading journal

Full design: [PHASE6.md](PHASE6.md).

- **Read-only towards trading.** `app/explanations` builds layered facts from stored records:
  - strategy signal, agent decision, risk decision, order, execution and outcome, kept separate;
  - trade episodes from fills;
  - money from `daily_pnl` and `PortfolioReader`.

  `app/journal` derives automatic entries from those rows (idempotent sync; no new event pipeline) and stores the
  user's notes and reflections. They write only the five Phase 6 tables (migration `0013`), plus `audit_events` for
  unstored reflection drafts.
- **The LLM explains, it never decides.** `app/llm` sees only settings and enums: no session and no services. A
  narrative is a validated rewording of the facts (every number must occur in them; no instructions, URLs or labels).
  Any failure falls back to the rule-based text. `LLM_PROVIDER=disabled` by default, and no LLM package is a
  dependency (the Anthropic SDK is an optional install).
- **Dependency direction.**
  - No package below the API imports Phase 6.
  - Phase 6 imports no order pipeline, Mock Exchange service, risk engine or risk service, agent, backtest runner,
    provider, scheduler, thread or broker code.
  - The Phase 6 routers do not import the trading or agent routers.
  - `tests/phase6/test_boundaries.py` enforces all three.
- **Isolation.** All five tables are user-owned, and triggers require every linked record and tag to belong to the
  row's user.
  - Automatic entries are immutable apart from notes, and are never deleted.
  - Explanations are append-only.

## What is deliberately not here

- Risk configuration now exists as `risk_settings` (Phase 4) with **no defaults**: entries are rejected until the user saves every limit. Since Phase 5 the settings cannot change while the account's agent is RUNNING (`409 AGENT_RUNNING`).
- `futures_contract`: futures stay disabled until genuine contract-specific data exists. `InstrumentType.FUTURE` exists only so the instrument model need not change later.
- Tick/lot sizes: none are invented. Phase 2 seeds the 10 equities and the provider, but `tick_size`/`lot_size` stay `null` until an authoritative instrument-specific source is approved.
- A scheduler: ingestion is operator-initiated (approved for Phase 2), and backtests start only on an explicit request.
- Live trading and broker integration. The Phase 3 backtest's NEXT_BAR_OPEN_V1 fill model is backtest-only and never
  uses the Phase 4 Mock Exchange or its order pipeline.
- Margin and short selling: not modelled (long-only cash account). Phase 1 stores `initial_capital` (required at registration, no default) and currency `INR`.

## Security posture: local MVP versus hosted deployment

| Area | Current | Before hosted deployment |
|---|---|---|
| Refresh tokens | None; access token only (lifetime is a dev placeholder) | Refresh with rotation and revocation |
| Logout / revocation | None; deactivated users are rejected on every request | Server-side session or revocation list (`jti`) |
| Rate limiting | None | Per-IP and per-account throttling, backoff |
| Duplicate registration | `409` reveals that an email is registered; emails unverified | Uniform response plus email verification |
| User isolation | Application-level only (no PostgreSQL RLS); the app role owns the tables | Separate migration and runtime roles, then RLS with `FORCE` |
| Secrets | Plaintext git-ignored `.env`; no JWT key rotation | Secrets manager, rotated credentials, key IDs |
| Transport | No TLS, CORS or security headers; `/docs` exposed | TLS at a proxy, explicit CORS, restrict docs |
| Audit storage | Failed-login events store the attempted email; unbounded | Retention policy and PII review |

## Operations

- `/health` is liveness (no database). `/health/ready` checks the database, returns 503 on failure, and is bounded by `DB_CONNECT_TIMEOUT_SECONDS`.
- Logs are structured JSON; secrets are `SecretStr` and not printed.
- The container runs as an unprivileged user and connects to the native PostgreSQL via `host.docker.internal`.
