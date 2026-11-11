# Agent-X Backend — Architecture (Phase 1)

Modular monolith: FastAPI + SQLAlchemy 2 (sync, psycopg 3) + Alembic + PostgreSQL. Phase 1 contains only the foundation. Market-data ingestion, Yahoo integration, strategies, regime detection, the Risk Engine, the Mock Exchange, the agent, backtesting and LLM features are **not implemented**.

## Layout

```
backend/
  app/
    main.py            app factory (create_app) and health routes
    core/              config (pydantic-settings), security (argon2id, JWT), logging
    db/
      base.py          declarative Base, naming convention, UserOwnedMixin
      enums.py         InstrumentType, Timeframe, PriceBasis, AdjustmentMethod, CorporateActionType
      session.py       engine / sessionmaker / per-request session
      scoping.py       owned_select(): user-scoped queries
      models/          user.py (users, virtual_accounts, audit_events), market.py (market data)
    api/v1/            auth, account routers; deps (get_current_user, get_db); schemas
  alembic/             migrations (0001_foundation)
  tests/               unit/ and integration/ (real PostgreSQL)
```

Later phases add packages beside these (`marketdata/`, `kernel/`, `risk/`, `exchange/`, ...). Empty placeholders were not created.

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

Not in Phase 1: PostgreSQL row-level security (possible later hardening), per-user rate limiting.

## Raw / adjusted price separation (schema level)

- `candles` stores provider-native OHLCV exactly as ingested. A database trigger rejects any `UPDATE` of prices, volume, timestamps, finality or identity columns. Only `price_basis` and `basis_verified_by` can change after insert.
- `price_basis` defaults to `UNVERIFIED_PROVIDER_NATIVE`. A check constraint forbids a verified label (`AS_TRADED_VERIFIED`, `SPLIT_ADJUSTED_VERIFIED`) without a recorded `basis_verified_by`. Nothing is called "raw" or "as traded" until verified.
- `adjusted_candles` is a separate table keyed to a candle, with method, source, factor and corporate-action-set version. It cannot modify `candles`.
- `corporate_actions` holds splits, bonuses and dividends, with required ratio/cash fields enforced by check constraints.
- Unique keys on `(instrument, provider, timeframe, bar_start_ts)` prevent duplicate candles and keep different providers' bars as separate rows (no silent provider switching).
- Timestamps are `timestamptz` (UTC instants); `session_date` records the exchange-local trading date. `observed_at` records when Agent-X received a bar, which later phases need for the no-lookahead eligibility rules.

Execution code (Phase 4) must read only `candles`, and only rows whose `price_basis` the owner has approved for execution (open decision D4).

## Account baseline and immutability

- `virtual_accounts.initial_capital` (`NUMERIC(18,2) NOT NULL`, no default, `> 0`) is the original account baseline, supplied at registration and used for historical performance calculations.
- A trigger (migration `0002`) rejects any `UPDATE` of `initial_capital`, `currency`, `user_id`, `id` or `created_at`. The baseline therefore cannot drift, whatever code runs.
- Cash, available balance, margin and equity are **not** modelled yet (Phase 4). They are intended to be derived from an append-only ledger, separate from this baseline.
- A future account reset must be a separate, explicitly authorised and audited operation. It is not an `UPDATE` of these columns and is not implemented.
- Registration creates the user, the virtual account and the audit event in a single transaction; failure injection tests confirm nothing is left behind.

## What is deliberately not here

- `risk_configuration` tables: no approved defaults and edit rules depend on agent state (Phase 4).
- `futures_contract`: futures stay disabled until genuine contract-specific data exists. `InstrumentType.FUTURE` exists only so the instrument model need not change later.
- Seeded instruments, providers or tick/lot sizes: none are invented; they arrive with Phase 2 sourcing.
- Virtual-account balances and margin: Phase 4. Phase 1 stores `initial_capital` (required at registration, no default) and currency `INR`.

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
