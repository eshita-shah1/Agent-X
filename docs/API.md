# Agent-X Backend — API (Phase 1)

Base path `/api/v1`. Interactive docs at `/docs` (OpenAPI at `/openapi.json`).
Authenticated endpoints need `Authorization: Bearer <access_token>`.

## Health

| Method | Path | Auth | Response |
|---|---|---|---|
| GET | `/health` | none | `200 {"status":"ok"}` (process is up) |
| GET | `/health/ready` | none | `200 {"status":"ready"}` or `503 {"status":"unavailable"}` (database check) |

## Auth

### `POST /api/v1/auth/register` → 201
```json
{ "email": "user@example.com", "password": "at-least-12-characters", "initial_capital": "500000.00" }
```
- `initial_capital` is **required**, positive, at most 2 decimal places, and has no default.
- Email is trimmed and lower-cased; duplicates return `409`. Invalid input returns `422`.
- Creates the user and its single virtual account (currency `INR`) in one transaction.
- Response: `{ "user": {id,email,is_active,created_at}, "account": {id,currency,initial_capital,created_at} }`.

### `POST /api/v1/auth/login` → 200
```json
{ "email": "user@example.com", "password": "..." }
```
Response: `{ "access_token": "...", "token_type": "bearer", "expires_in": <seconds> }`.
Wrong password and unknown email both return the same `401`.

### `GET /api/v1/auth/me` → 200
The authenticated user. `401` for a missing, malformed, expired, tampered, or unknown-user token, or an inactive user.

## Account

### `GET /api/v1/account` → 200
The authenticated user's own virtual account `{id,currency,initial_capital,created_at}`. No user id is accepted; extra query parameters or headers are ignored.

## Market data (Phase 2)

Prefix `/api/v1/market`. **Every endpoint requires a bearer token.** Market data itself is global; no endpoint accepts a
user id or exposes account data. Candles, coverage and freshness are read from the configured active provider only (`MARKETDATA_PROVIDER`); series from different providers are never mixed, and an unregistered active provider is `503 PROVIDER_NOT_CONFIGURED`. Timestamps are always UTC with a `Z` suffix, including inside a job `summary`; this
also applies to Phase 1 endpoints since the UTC session change. Background and design details:
[MARKET_DATA.md](MARKET_DATA.md).

### Instruments
| Method | Path | Notes |
|---|---|---|
| GET | `/instruments` | Optional `type=EQUITY\|FUTURE`, `enabled=true\|false`. Returns the 10 equities and the 2 **disabled** futures placeholders, each with its explicit provider-symbol mapping and verification status. |
| GET | `/instruments/{id}` | `404` if unknown. `tick_size`/`lot_size` are `null` (never invented). |

### Candles and coverage
| Method | Path | Notes |
|---|---|---|
| GET | `/candles` | `instrument_id`, `start`, `end` (both **timezone-aware**; naive timestamps are `422`), `timeframe` (default `5m`), `limit` 1-2000 (default 500), `after` (cursor), `series=raw\|adjusted`. Range at most 366 days. Returns `{items, count, next_cursor, price_basis_note}` oldest first, canonical observations only. Each candle carries provider, provider symbol, `price_basis` (always `UNVERIFIED_PROVIDER_NATIVE` today; no price is classified as execution-ready, see the `price_basis_note`), `observation_version`, `is_canonical` (the first observation), `is_final` (always `true`: forming bars are never archived or served), `observed_at`, `ingested_at`, `ingestion_batch_id`. Prices keep the provider's precision (6 dp; not tick-normalised). `series=adjusted` returns only bars that have a computed adjustment (no silent fallback to raw). A disabled instrument (futures) is `409 INSTRUMENT_DISABLED`; an unknown one `404`. |
| GET | `/coverage` | `instrument_id`, `timeframe` (`1m`/`5m`/`15m`/`60m`; `1d` is `422`: coverage needs a fixed bar length), `days` 1-400 (default 60), optional `end_date`. Reports actuals: first/last candle, expected vs archived, missing intervals, partial/absent days, unexplained absences, *inferred* closures (not verified holidays), restated bars, quality-issue counts, `full_coverage_of_requested_range` (only `true` if earned), and the calendar source/limits (`authoritative_holiday_source: null`, `inferred_closures_are_verified_holidays: false`). |

### Ingestion (operator-initiated; nothing runs automatically)
| Method | Path | Notes |
|---|---|---|
| POST | `/ingestion/backfill` | Body `{days?: 1-60 (default 60), instrument_ids?: [int]}`. Default: all enabled equities with a provider mapping. Returns `202` with the job (`QUEUED`); it runs in the background. Explicit futures ids are `409 INSTRUMENT_DISABLED`; unknown ids `404`. |
| POST | `/ingestion/incremental` | Body `{instrument_ids?}`. Fetches from the last archived bar with a 24 h overlap that is compared against the archive (restatements are recorded, not applied). |
| GET | `/ingestion/jobs` | `limit` 1-100, `offset`. **Only the caller's own jobs** (jobs started from the CLI have no user and are not listed). |
| GET | `/ingestion/jobs/{job_id}` | `404` (not `403`) for another user's job. Includes per-instrument results in `summary`. |

Only one job may be active at a time: a second request returns `409 INGESTION_ALREADY_RUNNING` with the `active_job_id`.
A job whose heartbeat is older than the timeout is marked `INTERRUPTED` and does not block new work. Any authenticated
user can start ingestion: an approved **local-development policy only**. Restricting ingestion to an authorised
administrator/operator is a prerequisite for hosted deployment. No scheduler exists and nothing starts on application
start-up.

### Providers
| Method | Path | Notes |
|---|---|---|
| GET | `/providers` | Registered providers and the active one. There is no automatic fallback. |
| GET | `/providers/health` | Provider status and its facts (last attempt/success/failure, error kind and message, consecutive failures, last observed market timestamp, last ingestion), per-instrument freshness (`status`, observed `data_age_seconds`, `lag_seconds`, `latest_finalized_bar_end`), and the thresholds, each labelled `PROVISIONAL` (configurable, descriptive only; they gate nothing). A provider never used is `UNKNOWN`; an instrument with no data is `UNAVAILABLE`, never healthy. |
| GET | `/providers/capabilities` | Optional `provider`. Every capability with status `DOCUMENTED`, `VERIFIED`, `UNSUPPORTED` or `UNKNOWN`, plus the probe evidence. |

## Errors
Errors use FastAPI's default `{"detail": ...}` shape; some market-data errors carry a structured detail
`{"code": ..., "message": ...}` (`INSTRUMENT_DISABLED`, `INGESTION_ALREADY_RUNNING`, `NO_INGESTABLE_INSTRUMENTS`,
`PROVIDER_NOT_CONFIGURED`). `401` responses carry `WWW-Authenticate: Bearer`.

## Not yet available
Strategies, agent, risk, exchange, portfolio, backtests, benchmarks, journal and reports arrive in later phases.
