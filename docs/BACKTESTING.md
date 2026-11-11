# Agent-X Backtesting (Phase 3)

Paper simulation only. A backtest never places an order, never touches a virtual account and never contacts a
provider. Its results are **advisory**: they never change strategy selection, parameters or risk settings, and nothing
"activates" a strategy because it backtested well (decision D15).

This document describes the implementation in `backend/app/indicators`, `backend/app/strategies`,
`backend/app/backtest` (pure engine) and `backend/app/backtest_runs` (database runner). Endpoints are in
[API.md](API.md); data provenance and Yahoo limitations are in [MARKET_DATA.md](MARKET_DATA.md).

## 1. Building blocks

| Layer | Package | Notes |
|---|---|---|
| Indicators | `app/indicators` | Incremental, float64, pure Python (`math.fsum`, no numpy/pandas). `update()` returns a value or `None` during warm-up, never NaN. RSI, ATR, Bollinger, ADX (+DI/−DI), Supertrend, EMA/SMA, session VWAP and the opening range (both reset each session). Indicators run continuously across sessions (D13). |
| Strategies | `app/strategies` | Five deterministic long-only strategies with frozen, validated configurations and **approved initial defaults** (not optimised; `DEFAULTS_STATUS = "APPROVED_INITIAL_DEFAULTS"`): `ORB` (opening-range breakout), `VWAP` (VWAP cross), `EMA_CROSSOVER`, `RSI_BOLLINGER` (mean reversion) and `SUPERTREND`. A strategy turns finalized 5-minute bars into one `Signal` per bar; it never fills, sizes or executes. |
| Selection | `app/strategies/selection` | Regime from ADX(14) at the **09:30 IST checkpoint**: `TREND` if ADX ≥ 25, `RANGE` if ADX ≤ 20, otherwise `UNDETERMINED`. TREND allows the TREND and OPENING_BREAKOUT families, RANGE allows MEAN_REVERSION, UNDETERMINED allows nothing. Modes: `REGIME_PRIORITY` (highest-priority eligible and warmed-up candidate) and `PINNED` (one configuration, regime and selector warm-up recorded but not enforced, no fallback). One decision per instrument per session, locked for the session, fully explained. No outcome metric is ever an input. |
| Engine | `app/backtest` | Pure: no database, API, account, clock or randomness (boundary tests enforce this). |
| Runner | `app/backtest_runs` | Loads archived data read-only, queues runs as background jobs and persists results atomically. |

### Common strategy rules

Decided at the close of each bar (times are bar **end** times, IST):

- **Position open:** square-off (bar end ≥ 15:15) → the strategy's own exit → hold.
- **Flat:** cooldown (1 bar after a close) → before the strategy's eligible time → after the entry cut-off (14:45; ORB 11:30) → gap after one or more missing expected bars → the strategy's own entry rules.

## 2. Event-driven processing (NEXT_BAR_OPEN_V1)

Each instrument is simulated **independently with the full capital**; instruments are never combined into a
portfolio. Every bar, warm-up and evaluation, in time order, passes through these phases exactly once:

1. **Open.** A pending entry from the previous bar is entered at this bar's open. With a position open:
   a. **Protective stop gap-through.** If open ≤ stop, exit at the open (`STOP_GAP`).
   b. Otherwise, a **pending strategy exit** exits at the open (`STRATEGY_EXIT`, or `SESSION_SQUARE_OFF`).
2. **Intrabar.** c. Otherwise, if low ≤ stop, exit at the stop (`STOP`). Else, if high ≥ target, exit at the target (`TARGET`), **including when the bar opened above the target** (conservative). The stop is checked first, so it wins when both are touched. These checks start on the entry bar itself, after the entry.
3. **Selection.** The bar goes to the session selector. The decision is locked at the checkpoint, before any strategy sees a bar ending after 09:30.
4. **Strategies.** Every candidate evaluates the bar once. The configuration that holds the position receives the real position state; every other candidate receives `FLAT` and only keeps its indicators warm.
5. **Act.** An entry signal becomes a pending entry only if the configuration is the session's locked selection (`entry_permitted`), the position is flat and nothing is pending. Exit signals come only from the position's owner. Other signals are counted as ignored.
6. **Session rules.** These use only the **next bar's timestamp**, never its prices:
   - A position is open on the session's last available bar → exit at that bar's close (`SQUARE_OFF_FALLBACK_LAST_BAR`). Never overnight.
   - The square-off signal fired (bar ending 15:15) but the intended fill bar (15:15) is missing → exit at the signal bar's close (`SQUARE_OFF_FALLBACK_LAST_BAR`, D5). Later bars are not used.
   - A pending entry whose next expected bar is missing, or whose session ends → it expires (`ENTRY_EXPIRED_MISSING_BAR` / `ENTRY_EXPIRED_SESSION_END`).
   - A pending ordinary strategy exit whose next bar is missing → deferred to the next available bar's open in the session (`EXIT_DEFERRED_MISSING_BAR`).
7. **Mark.** Evaluation bars record `cash + quantity × close` beside the benchmark's value.

No trading happens in warm-up sessions. Missing bars are never synthesised or interpolated; they are reported
(`MISSING_BARS` events and data-quality counts).

### Protective levels

- An absolute `stop_price` (ORB: the opening-range low) is used as given. If it is at or above the entry open, the position exits immediately at that open (`STOP_GAP`).
- `stop_distance` and `target_distance` are measured from the **simulated entry price after slippage**.
- Strategy levels are float64. They are converted to `Decimal(repr(float))` exactly, with no rounding and no tick-size normalisation.

### Signal prices and simulated prices

Every trade stores the signal bars' closes (`entry_reference_price`, `exit_reference_price`) separately from the
simulated prices (`entry_price`, `exit_price`), together with the signal and fill bar timestamps, the exit timing
(`AT_OPEN`, `INTRABAR`, `AT_CLOSE`) and the exit reason.

## 3. Sizing, costs and accounting

- **Sizing (`WHOLE_SHARES_FULL_ALLOCATION_V1`).** Long only, no leverage, one position per instrument. The largest whole number of shares whose cost plus buy fees fits in the cash. If that is zero, the entry is skipped (`ENTRY_SKIPPED_INSUFFICIENT_CAPITAL`).
- **Costs (`CostModel`), all zero by default:**
  - `fee_per_order`: once per order with a non-zero quantity.
  - `fee_bps_per_side`: on every order's notional.
  - `sell_side_bps`: on sell orders only.
  - `slippage_bps`: moves every simulated price against the trade (buys higher, sells lower).

  Fees are rounded to the paisa (half-up) per order. **No statutory Indian rates are built in.**
- **Labels.** With every cost setting at zero, results are labelled **`GROSS_OF_COSTS`**; otherwise `NET_OF_MODELLED_COSTS`.
- **Decimal.** All money is `Decimal`; only indicators use float64. `gross_pnl = quantity × (exit price − entry price)`, with slippage inside the prices. `net_pnl` deducts both orders' fees.
- **Capital.** Default: the user's virtual-account `initial_capital`, **read only**. A request may override it (`REQUEST_OVERRIDE`). Every instrument receives the full capital.

## 4. Data preparation and refusals

The runner loads canonical, finalized 5-minute candles of the active provider (`yahoo`), labelled
`UNVERIFIED_PROVIDER_NATIVE`, from `start − (7 + 3 × warmup_sessions)` calendar days to `end`, plus the instrument's
corporate actions. Nothing is written and no provider is contacted.

- **Warm-up.** The last `warmup_sessions` (default 2) sessions with data before `start` feed indicators and the regime classifier without trading. If fewer exist, the shortfall is taken from the first sessions of the period and the effective start moves forward (`warmup_source = SHIFTED_INTO_PERIOD` or `PRIOR_AND_SHIFTED`).
- **Splits and bonuses.**
  - An ex-date inside the requested evaluation period **refuses** the backtest (D11).
  - An ex-date before the period drops the older warm-up history, because unadjusted prices from both sides would corrupt continuous indicators. This is disclosed as `warmup_truncated_by_corporate_action`.
- **Dividends and other actions** in the period are disclosed only; prices are not adjusted.
- **Data quality**, per instrument:
  - requested and effective periods, and warm-up sessions and bars;
  - evaluation bars, expected bars (calendar sessions up to the requested end), missing bars and their first 500 timestamps;
  - coverage %, sessions without data, and bars ignored outside the window.

Refusal codes (`BacktestRefused`, HTTP 422 at submission):

| Code | Meaning |
|---|---|
| `INVALID_CONFIGURATION` | Unknown, disabled or non-equity instrument; unknown strategy, unknown or mistyped parameter, invalid configuration; invalid mode, priorities or capital; no virtual account and no capital override |
| `INVALID_PERIOD` | `end` before `start`, `end` in the future, or a period longer than 366 days |
| `INVALID_DATA` | A bar off the 5-minute calendar grid or out of order, or an unexpected price basis |
| `NO_DATA` | No bars in the requested period |
| `INSUFFICIENT_DATA` | Every session with data is needed for warm-up |
| `CORPORATE_ACTION_IN_PERIOD` | A split or bonus ex-date inside the evaluation period |

If any requested instrument is refused, the whole run is refused.

## 5. Buy-and-Hold benchmark

Independent of the strategy. It uses the same capital, cost model and whole-share sizing:

- **Entry:** buys at the open of the first evaluation bar (never during warm-up).
- **Holding:** holds through every session, overnight included.
- **Marks:** recorded at every evaluation bar's close.
- **Final value:** deducts a hypothetical exit at the last close, with slippage and fees.

Its status is `VALID` or `LIMITED`. Limitations: `DIVIDENDS_EXCLUDED`, `OTHER_CORPORATE_ACTION_NOT_ADJUSTED`,
`MISSING_BARS`, `SESSIONS_WITHOUT_DATA`, `INSUFFICIENT_CAPITAL_FOR_ONE_SHARE`. Notes on every benchmark:
`UNVERIFIED_PRICE_BASIS`, `PER_INSTRUMENT_NOT_PORTFOLIO`, and `HOLDS_OVERNIGHT_UNLIKE_THE_INTRADAY_STRATEGY` for more
than one session.

## 6. Fingerprints and reproducibility

All three fingerprints are SHA-256 over a canonical JSON form:

- sorted keys;
- Decimals in plain notation without trailing zeros;
- datetimes in UTC with `Z`;
- enums as values;
- deterministic list order.

| Fingerprint | Covers |
|---|---|
| `config_fingerprint` | Engine, fill and sizing model versions, price basis, timeframe, instruments, period, capital, cost model, warm-up, and the policy (mode, regime configuration, each candidate's id, strategy, version, parameters and priority) |
| `data_fingerprint` | Every bar used, with its `(candle id, observation version)`, the corporate actions supplied, and the calendar sessions spanned |
| `result_fingerprint` | Every output: data quality, selection decisions, trades, events, equity points, the benchmark with its validity, and the summary |

**Time zones.** A bar timestamp must be timezone-aware; naive timestamps are rejected. Every bar timestamp is
normalised to UTC on input, and selection explanations are serialised in UTC (`...Z`, e.g. the 09:30 IST checkpoint is
`T04:00:00Z`). Equal instants given in UTC, IST or any other zone therefore produce identical outcomes and
fingerprints. Session dates and all session times are still computed in exchange time (Asia/Kolkata). The database
loader also normalises to UTC, so the connection's session time zone cannot change a result.

Tests check the following:

- repeated runs are identical, including across Python hash seeds;
- prices after a cut-off cannot change earlier results (no look-ahead);
- truncating the data at a session boundary leaves earlier sessions unchanged;
- a run persisted through the API reproduces an independent engine run exactly.

## 7. Runs, jobs and persistence (migration 0009)

- **Starting a run.** `POST /api/v1/backtests` performs every refusal check synchronously, stores the run as `QUEUED` and hands it to FastAPI `BackgroundTasks`. There is no scheduler, and nothing runs on start-up.
- **One active run per user** (`QUEUED`/`RUNNING`), enforced by a partial unique index; other users are unaffected. A run whose heartbeat is older than `BACKTEST_JOB_HEARTBEAT_TIMEOUT_SECONDS` (default 600) is marked `INTERRUPTED` when its owner next starts a run.
- **The job:**
  - rebuilds the request from the stored JSON and checks its `config_fingerprint`;
  - reloads the data and runs the engine;
  - writes every result row and the `SUCCEEDED` status in **one transaction**.

  A refusal at run time (e.g. a split recorded after acceptance), a crash or an interruption leaves `FAILED` or `INTERRUPTED` with **no** result rows. `error_code` is a refusal code, `CONFIG_FINGERPRINT_MISMATCH`, `INTERNAL_ERROR` or `INTERRUPTED`.
- **Tables (all user-owned, each carrying `user_id`):**

  | Table | Holds |
  |---|---|
  | `backtest_runs` | Request snapshot, labels, fingerprints, disclosures, summary, status and timestamps |
  | `backtest_instrument_results` | Data quality, benchmark, summary, final values |
  | `backtest_selection_decisions` | One explained decision per instrument per evaluation session |
  | `backtest_trades` | Trades |
  | `backtest_events` | Signals, expiries, skips, deferrals, missing bars, sessions without a decision |
  | `backtest_equity_points` | Per-bar equity and benchmark equity |

  Money columns are unconstrained `NUMERIC`, so engine Decimals are stored exactly.
- **Immutability triggers:**
  - A run is never deleted.
  - Its request, configuration, labels, fingerprint and owner never change.
  - Its status only moves `QUEUED → RUNNING → SUCCEEDED/FAILED/INTERRUPTED` (or `QUEUED → FAILED/INTERRUPTED`), and a finished run never changes.
  - Fingerprints and summary are written only by the transition to `SUCCEEDED`; a check constraint requires the fingerprints exactly when `SUCCEEDED`.
  - Result rows can be inserted only while their run is `RUNNING` and has the same owner, and are never updated or deleted.
- **Migration number.** The migration was written as `0004` and renumbered to **`0009`** (parent `0008`) when Phase 3 was integrated with Phase 4, whose migrations `0004`-`0008` were already on `main`. The schema is unchanged.
- **Downgrade.** Downgrading `0009` is refused while any run exists.

## 8. Limitations (read before interpreting any result)

- **About 60 days of 5-minute Yahoo data is not statistically sufficient.** Results are not evidence of future performance or profitability.
- Prices are Yahoo provider-native (`UNVERIFIED_PROVIDER_NATIVE`), not verified as-traded. Yahoo's 5-minute bars may be missing or later restated. No authoritative NSE holiday calendar is integrated.
- NEXT_BAR_OPEN_V1 is an approximation:
  - entries and exits at the next bar's open;
  - stops and targets from OHLC with the stop assumed first;
  - no partial fills, queueing, market impact or circuit limits;
  - no tick-size normalisation.

  It is backtest-only and is **not** the Phase 4 Mock Exchange (see section 9).
- Costs default to zero (`GROSS_OF_COSTS`). Modelled costs are user assumptions.
- Results are per instrument, not a portfolio; do not sum them.
- Corporate-action records come from the provider and may be incomplete.

## 9. Relationship to Phase 4 execution (open decision)

The Phase 3 backtest and the Phase 4 risk engine / Mock Exchange were developed in parallel and integrated afterwards.
They are deliberately **separate**:
- The backtest never imports or calls the risk engine, the Mock Exchange, the order pipeline or the operations code.
- It never places orders and never writes orders, fills, ledger entries or positions.
- Phase 4 never reads backtest results.

Tests enforce this (`tests/backtest/test_phase_separation.py`, and an integration test asserting that a complete run
leaves every Phase 4 table empty).

Their simulation assumptions currently **differ**. Backtest results are therefore not expected to match Phase 4
simulated fills for the same signals:

| Aspect | Phase 3 backtest (approved Phase 3 decisions) | Phase 4 Mock Exchange ([PHASE4.md](PHASE4.md), [D4_EXECUTION_READINESS.md](D4_EXECUTION_READINESS.md)) |
|---|---|---|
| Entries and strategy exits | Next bar's open (`NEXT_BAR_OPEN_V1`) | Open of the first finalized bar at or after acceptance |
| Protective stop / target | Intrabar at the stop or target level (stop first; gap-through at the open) | Detected on a bar, exited by a market order at the next bar's open |
| Costs | Generic `CostModel`, all zero by default (`GROSS_OF_COSTS`) | Operator-maintained, effective-dated Indian-equity component schedule |
| Session | Intraday: mandatory 15:15 square-off, no overnight positions | `DELIVERY` product only; intraday square-off not modelled |

Phase 4's documentation states that backtesting "must use `app.exchange.costs.CostModel` and the same next-bar-open
semantics". That has **not** been applied, because it would change approved Phase 3 decisions. Whether, and how,
to align the two models is an open owner decision.
