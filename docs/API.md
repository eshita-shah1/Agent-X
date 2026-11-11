# Agent-X Backend — API (Phases 1-5)

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

## Backtests (Phase 3)

Prefix `/api/v1/backtests`. **Every endpoint requires a bearer token.** Runs are owned by the user who started them:
another user's run (or an instrument that is not part of the run) is `404`, never `403`. There is no update or delete
endpoint (`PUT`/`PATCH`/`DELETE` are `405`), and stored results are immutable in the database. Backtests are
advisory: no endpoint ranks configurations by results or activates a strategy. Design and limitations:
[BACKTESTING.md](BACKTESTING.md).

| Method | Path | Notes |
|---|---|---|
| GET | `/strategies` | The strategy catalogue: id, version, family, rule description, approved initial defaults (times as `HH:MM`) and parameter names; `defaults_status`, the regime configuration (ADX 14, TREND ≥ 25, RANGE ≤ 20, checkpoint 09:30), the engine models and the disclosures. Static: contains no results. |
| POST | `` (i.e. `/api/v1/backtests`) | Starts a run. `202` with the run (`QUEUED`); it runs in the background. |
| GET | `` | The caller's runs, newest first. `limit` 1-100 (default 20), `offset`. `{items, count, limit, offset}`. |
| GET | `/{run_id}` | One run: status, request snapshot (`request.submitted`, `request.config`), labels, fingerprints, disclosures, `summary`, `error_code`/`error_message`, timestamps. |
| GET | `/{run_id}/instruments` | Per-instrument data quality, benchmark (with validity), summary and final values. |
| GET | `/{run_id}/instruments/{instrument_id}/trades` | Ordered by `seq`. `limit` 1-500 (default 100), `offset`. |
| GET | `/{run_id}/instruments/{instrument_id}/equity` | One point per evaluation bar, ordered by time. `limit` 1-5000 (default 1000), `offset`. |
| GET | `/{run_id}/instruments/{instrument_id}/selections` | One explained selection decision per session. `limit` 1-500, `offset`. |
| GET | `/{run_id}/instruments/{instrument_id}/events` | Signals, expiries, skips, deferrals, missing bars. `limit` 1-500, `offset`. |

Paginated responses are `{items, count, limit, offset}` (`count` is the total). In result rows, decimals are exact
strings (stored without rounding) and timestamps are UTC ISO-8601 with `Z`; selection explanations also use UTC
(the 09:30 IST checkpoint is `...T04:00:00Z`).

### `POST /api/v1/backtests` body

```json
{
  "instrument_ids": [1, 2],
  "start": "2026-10-01",
  "end": "2026-11-10",
  "mode": "REGIME_PRIORITY",
  "candidates": [
    {"config_id": "orb-default", "strategy_id": "ORB", "priority": 1},
    {"config_id": "rsi-25", "strategy_id": "RSI_BOLLINGER", "priority": 2, "params": {"rsi_period": 25}}
  ],
  "capital": "250000.00",
  "cost_model": {"fee_per_order": "20", "fee_bps_per_side": "0", "sell_side_bps": "0", "slippage_bps": "5"},
  "warmup_sessions": 2
}
```

| Field | Rules |
|---|---|
| `instrument_ids` | 1-20 unique ids of **enabled equities** (futures are refused). |
| `start`, `end` | Session dates, inclusive. `end` not after today (IST); at most 366 days. |
| `mode` | `REGIME_PRIORITY` (every candidate needs a unique `priority` ≥ 1) or `PINNED` (exactly one candidate, no priority). |
| `candidates` | 1-10. `params` override the approved defaults; unknown or mistyped parameters are refused (times as `"HH:MM"`). There is no implicit candidate list or default priority order. |
| `capital` | Optional, > 0, at most 2 decimal places. Default: the caller's virtual-account `initial_capital` (read only). Each instrument receives the full amount. |
| `cost_model` | Optional; every field defaults to `0` (the run is then `GROSS_OF_COSTS`). Send decimals as strings. `slippage_bps` < 10000. |
| `warmup_sessions` | 0-20, default 2. |

Unknown fields are rejected (`422`).

### Run states and errors

`status`: `QUEUED` → `RUNNING` → `SUCCEEDED` | `FAILED` | `INTERRUPTED`. Fingerprints exist only for `SUCCEEDED`.
A `FAILED`/`INTERRUPTED` run has no results; its `error_code` is a refusal code below, `CONFIG_FINGERPRINT_MISMATCH`,
`INTERNAL_ERROR` or `INTERRUPTED`.

| Status | `detail.code` | When |
|---|---|---|
| 422 | `INVALID_CONFIGURATION` | Unknown/disabled/non-equity instrument, unknown strategy or parameter, invalid configuration, mode or priorities |
| 422 | `INVALID_PERIOD` | `end` before `start`, `end` in the future, period too long |
| 422 | `INVALID_DATA` | Archived bars off the calendar grid, out of order, or with an unexpected price basis |
| 422 | `NO_DATA` | No archived bars in the period |
| 422 | `INSUFFICIENT_DATA` | All sessions with data are needed for warm-up |
| 422 | `CORPORATE_ACTION_IN_PERIOD` | Split or bonus ex-date inside the evaluation period |
| 409 | `BACKTEST_ALREADY_RUNNING` | The caller already has a `QUEUED`/`RUNNING` run (`active_run_id` given). Other users are unaffected. |

Refusals are `{"detail": {"code", "message", "instrument_id"}}`; request-shape errors use FastAPI's standard `422`
validation format. Nothing is queued when a request is refused.

## Phase 4: orders, portfolio, risk, exchange

All endpoints below require authentication and act only on the caller's own data. Monetary
and price values are decimal strings. See [PHASE4.md](PHASE4.md) for semantics.

| Method | Path | Notes |
|---|---|---|
| POST | `/api/v1/orders` | Submit. Orders **never fill at submission**: an accepted order is `PENDING` until a later bar is processed. `201` new order (it may be `REJECTED`, with a reason), `200` idempotent replay, `409` client_order_id reused for a different order, `422` invalid request or instrument |
| GET | `/api/v1/orders?status=&limit=` | Own orders, newest first |
| GET | `/api/v1/orders/{id}` | Order with its fills and risk decision; `404` if not own |
| POST | `/api/v1/orders/{id}/cancel` | Body `{"reason"?: str}`; `409` if not open; `404` if not own |
| GET | `/api/v1/portfolio` | Cash, reserved, available, positions, realized/unrealized P&L, equity (`null` when any position cannot be marked with an eligible price). Each position carries `protective_exit_unresolved` (true while its stop/target could not be evaluated on fresh data: it is **not** protected by an executed exit) |
| GET | `/api/v1/portfolio/fills?limit=` | Executions with price provenance |
| GET | `/api/v1/portfolio/pnl/daily?session_date=YYYY-MM-DD` | Realized P&L, fees and net for a simulated (IST) session date: fills by their simulated fill time, cash-in-lieu by ex-date; plus `dividend_income` by payment date (not trading P&L) |
| GET | `/api/v1/portfolio/corporate-actions/pending` | Positions frozen by a split/bonus/other action, with the action each is waiting for and the last attempt |
| GET | `/api/v1/portfolio/corporate-actions/adjustments` | The caller's adjustment history (append-only, read-only) |
| GET | `/api/v1/portfolio/dividends` | The caller's dividend credits and recorded refusals (read-only) |

### Operator endpoints (`users.is_operator`; set only by `python -m app.ops.cli set-operator`)
Non-operators get `403`, and every denied attempt is audited (`operator.access_denied`). Operators act on
corporate-action records; each affected user's result is computed and recorded separately.

| Method | Path | Notes |
|---|---|---|
| GET | `/api/v1/operator/corporate-actions/pending` | Actions freezing positions, with the number of affected positions |
| POST | `/api/v1/operator/corporate-actions/{id}/adjustments` | `{evidence_reference, confirm_ratio_numerator, confirm_ratio_denominator, cash_in_lieu_price?, cash_in_lieu_source?, cash_in_lieu_observed_at?}`. Applies to every affected position. Returns `[{user_id, adjustment, replayed}]` (APPLIED / REJECTED / BLOCKED_FRACTIONAL). `404` for an unknown action |
| GET | `/api/v1/operator/protective-exits/unresolved` | Alert view: every user's OPEN protective-exit requirements, oldest first |
| GET | `/api/v1/operator/dividends/pending` | Dividends past their ex-date with entitled users who have no credit (and users blocked by an unadjusted split) |
| POST | `/api/v1/operator/dividends/{id}/credits` | `{evidence_reference, confirmed_amount_per_share, payment_date, accept_provider_difference?}`. Credits gross amounts to every entitled user (holding at the close before the ex-date). Returns `[{credit, replayed}]`. `422` if not a dividend or the amount is not positive |
| GET | `/api/v1/risk/settings` | `404` until configured (no defaults) |
| PUT | `/api/v1/risk/settings` | All fields required (see below) |
| GET | `/api/v1/risk/state` | **Refreshes** the equity references first, then returns kill switch, peak/day-start/last equity, current `equity`, `drawdown_pct`, `daily_loss_pct`, `breaches` and `unresolved_protective_exits` (count of OPEN protective-exit requirements) |
| GET | `/api/v1/risk/protective-exits?status=OPEN\|RESOLVED&limit=` | The caller's protective-exit requirements (migration `0011`): reason (e.g. `STALE`), the stale bar, `stale_trigger`, checks, resolution (`EXIT_SUBMITTED`, `EXIT_ALREADY_PENDING`, `NOT_TRIGGERED`, `POSITION_CLOSED`) and the exit order |
| POST | `/api/v1/risk/kill-switch` | `{"engaged": bool, "reason": str}`; engaging cancels all open orders |
| GET | `/api/v1/risk/decisions?limit=` | Risk decision log |
| POST | `/api/v1/exchange/process` | Executes the caller's resting orders against bars finalized since they were accepted (next-bar-open model), expires DAY orders, refreshes risk. Returns `{changed_orders, risk}`. Explicitly triggered; nothing is scheduled |
| POST | `/api/v1/exchange/protective-exits` | Evaluates the caller's stop-losses/take-profits on the latest eligible prices and submits MARKET exits where triggered. Without a fresh eligible price no exit is triggered and no price is fabricated: an OPEN requirement and an audit alert (`risk.protective_exit_unresolved`) are recorded instead, and resolved by the first run on fresh data. Returns `{submitted_exits, unresolved, risk}`. Explicitly triggered; nothing is scheduled |
| GET | `/api/v1/exchange/status` | `execution_mode`, `execution_policy`, `execution_approval_reference`, execution model, whether execution is enabled and why not (`blocking_reasons`), and `disclosures` (in simulation mode: simulation-only fills on unverified Yahoo data; no five-year intraday backtesting at launch) |

### `POST /api/v1/orders`
```json
{
  "client_order_id": "ord-2026-11-11-001",
  "instrument_id": 1,
  "side": "BUY",
  "order_type": "LIMIT",
  "quantity": 10,
  "limit_price": "1450.50",
  "stop_loss_price": "1420.00",
  "take_profit_price": "1500.00",
  "strategy_id": "optional",
  "strategy_max_risk_pct": "0.5"
}
```
- `client_order_id`: 1–64 chars `[A-Za-z0-9._:-]`, unique per user (idempotency key).
- `product` (optional, default `DELIVERY`; `INTRADAY` is rejected `PRODUCT_NOT_SUPPORTED`).
- `signal_at` (optional, timezone-aware): when the decision was made; never in the future; defaults to acceptance time.
- `limit_price` is required for `LIMIT` and forbidden for `MARKET`. Prices are > 0 with at most 6 dp.
- Entries (`BUY`) need `stop_loss_price` (otherwise risk rejects with `STOP_LOSS_REQUIRED`). `SELL` may not carry stop/take-profit.
- Response: `{ "order": {...}, "risk_decision": {...}, "replayed": bool }`. Orders carry `signal_at`, `decision_price_ref` (the reference bar the decision used), `execution_policy` (policy in force at acceptance), `execution_approval_reference` (approval in force at acceptance; `null` while execution is disabled; immutable), `accepted_at` (only bars starting at or after it can fill them) and `risk_budget` (the user's hard per-trade risk limit in money; fills are capped by it if the next open gaps). Fills carry `realized_pnl` (attributed to the simulated fill time), `execution_policy` (`VERIFIED_NEXT_BAR_OPEN` or `SIMULATION_PROVIDER_NATIVE`), `execution_approval_reference` (copied from the order), `price_source_kind` (`NEXT_BAR_OPEN` or `BAR_LIMIT_CROSS`) and `fee_breakdown` (brokerage, stt, txn_charge, sebi_fee, stamp_duty, gst, total). `order.quantity` is the risk-approved size; `requested_quantity` is what was asked.

### `PUT /api/v1/risk/settings`
```json
{
  "max_risk_per_trade_pct": "1", "max_position_allocation_pct": "20",
  "max_portfolio_exposure_pct": "100", "max_daily_loss_pct": "5", "max_drawdown_pct": "10",
  "max_position_quantity": null, "resize_allowed": true
}
```
Percentages are in (0, 100]. `max_position_quantity` must be sent explicitly (`null` = no share-count cap).
Returns **409 `AGENT_RUNNING`** while the account's agent is RUNNING (Phase 5): pause or stop it first.

## Phase 5: agent (`/api/v1/agent`; design in [PHASE5.md](PHASE5.md))

Paper trading only. The agent places orders only through the worker (`python -m app.agent.cli cycle`) and
`OrderService`; no endpoint here places an order. Another user's agent is never visible. Transitions run under the
account lock, are recorded as agent events and return the **persisted** status read back after commit.

| Method | Path | Notes |
|---|---|---|
| GET | `/agent/status` | `configured`, `status` (STOPPED/RUNNING/PAUSED/ERROR), `mode` (EXECUTION/SIGNALS_ONLY), `entries_allowed`, `blockers` (e.g. `EXECUTION_DISABLED`, `KILL_SWITCH_ENGAGED`, `WORKER_NOT_SEEN`, `INSTRUMENT_MANUAL_INTERVENTION`), `disclosures`, `effective_config`, `pending_config`, `instruments[]` (watermark, owed exit, block), heartbeat, `unresolved_protective_exits` |
| GET | `/agent/config` | Latest configuration version; `404` until configured |
| PUT | `/agent/config` | Creates the agent (STOPPED) on first use and a new immutable version: `201`; identical to the latest: `200`, `created: false`. `409` while RUNNING. `422` for an invalid strategy, parameter, policy or instrument (only enabled equities). A new version is effective from the next session (immediately before the first start) |
| POST | `/agent/start` | STOPPED → RUNNING after validation and reconciliation. The mode is EXECUTION when `/exchange/status` reports execution enabled, otherwise SIGNALS_ONLY (no order is ever submitted). `409 AGENT_REFUSED: ...` lists every problem |
| POST | `/agent/pause` | RUNNING → PAUSED (monitor mode: no entries; exits continue) |
| POST | `/agent/resume` | PAUSED/ERROR → RUNNING after revalidation (same mode) and reconciliation; refused while the kill switch is engaged |
| POST | `/agent/stop` | → STOPPED. Nothing is liquidated; Phase 4 protective exits keep running |
| POST | `/agent/kill-switch` | Body `{"reason": "..."}`. Engages the **Phase 4** kill switch (cancels every open order) and pauses the agent. Release with `POST /risk/kill-switch`; the agent stays PAUSED until `/agent/resume` |
| GET | `/agent/decisions` | Newest first; filters `instrument_id`, `kind` (SELECTION/EVALUATION/OWED_EXIT), `outcome`, `before_id`, `limit` ≤ 500. Each carries the candle id, observation version, provider, price basis, strategy, signal, outcome, and the client order id / order / risk decision it produced |
| GET | `/agent/events` | Lifecycle, kill switch, reconciliation, instrument blocks, owed exits, cycle failures |
| GET | `/agent/cycles` | Worker cycles with status (RUNNING/SUCCEEDED/FAILED/INTERRUPTED) and summary |

`PUT /agent/config` body (the backtest candidate shape):
```json
{"instrument_ids": [1, 2], "mode": "REGIME_PRIORITY", "warmup_sessions": 2,
 "candidates": [{"config_id": "orb", "strategy_id": "ORB", "params": {}, "priority": 1},
                {"config_id": "rsi", "strategy_id": "RSI_BOLLINGER", "params": {}, "priority": 2}]}
```

## Errors
Errors use FastAPI's default `{"detail": ...}` shape; some errors carry a structured detail `{"code": ..., "message":
...}`: market data (`INSTRUMENT_DISABLED`, `INGESTION_ALREADY_RUNNING`, `NO_INGESTABLE_INSTRUMENTS`,
`PROVIDER_NOT_CONFIGURED`) and backtests (see above). `401` responses carry `WWW-Authenticate: Bearer`.

## Not yet available
The LLM journal, explanations and reports arrive in Phase 7.
