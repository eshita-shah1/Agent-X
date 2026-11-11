# Agent-X Backend — Architecture (Phases 1-2)

Modular monolith: FastAPI + SQLAlchemy 2 (sync, psycopg 3) + Alembic + PostgreSQL. Phase 1 is the foundation (auth,
isolation, base schema); Phase 2 adds the market-data foundation (see [MARKET_DATA.md](MARKET_DATA.md)). Strategies, regime
detection, the Risk Engine, the Mock Exchange, the agent, backtesting and LLM features are **not implemented**.

## Layout

```
backend/
  app/
    main.py            app factory (create_app) and health routes
    core/              config (pydantic-settings), security (argon2id, JWT), logging
    db/
      base.py          declarative Base, naming convention, UserOwnedMixin
      enums.py         InstrumentType, Timeframe, PriceBasis, AdjustmentMethod, CorporateActionType, job/batch enums
      session.py       engine (UTC session timezone) / sessionmaker / per-request session
      scoping.py       owned_select(): user-scoped queries
      models/          user.py, market.py (instruments, candles, adjusted candles, corporate actions), ingestion.py
    marketdata/        providers/ (base interfaces, yahoo adapter, registry), pipeline, archive, adjustment, gaps,
                       freshness, calendar, jobs, service, seed, registry (approved universe), probe, cli
    api/v1/            auth, account, market routers; deps (get_current_user, get_db); schemas
  alembic/             migrations (0001_foundation, 0002_immutable_account_fields, 0003_market_data_foundation)
  tests/               unit/, marketdata/ (pure, no DB), integration/ (real PostgreSQL); support.py (fake provider/clock)
```

Later phases add packages beside these (`kernel/`, `risk/`, `exchange/`, ...). Empty placeholders were not created.

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

## What is deliberately not here

- Risk configuration now exists as `risk_settings` (Phase 4) with **no defaults**: entries are rejected until the user saves every limit. Edit rules that depend on agent state (for example, locking settings while the agent runs) remain for Phase 5.
- `futures_contract`: futures stay disabled until genuine contract-specific data exists. `InstrumentType.FUTURE` exists only so the instrument model need not change later.
- Tick/lot sizes: none are invented. Phase 2 seeds the 10 equities and the provider, but `tick_size`/`lot_size` stay `null` until an authoritative instrument-specific source is approved.
- A scheduler: ingestion is operator-initiated (approved for Phase 2).
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
