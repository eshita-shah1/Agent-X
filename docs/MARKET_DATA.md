# Agent-X Market Data Foundation (Phase 2)

Provider-neutral market-data infrastructure: instrument registry, provider adapters, validation, PostgreSQL archive with
provenance, ingestion jobs, coverage and freshness reporting. It is independent of strategies, risk, the Mock Exchange,
backtesting and any LLM. Paper trading only; no broker or real-money path exists.

## 1. What was built (and what was not)

| Built | Not built (later phases) |
|---|---|
| Instrument registry (10 NSE equities; NIFTY/BANKNIFTY futures as **disabled** placeholders) | Indicators, regimes, strategies, selector |
| `HistoricalDataProvider`, `IntradayDataProvider`, `QuoteDataProvider`, `InstrumentMetadataProvider` | Backtesting and Buy-and-Hold engines |
| Yahoo Finance adapter (`yfinance` 1.8.0, explicit parameters) | Mock Exchange, risk engine, agent |
| Validation, de-duplication, finalisation, quarantine | A scheduler (see §7: ingestion is operator-initiated) |
| Raw archive + separate split-adjusted series + corporate actions | Futures data (no verified contract-specific source) |
| Restatement versioning, ingestion jobs/batches, quality-issue log | Any paid or second data source |
| Coverage, gap, freshness and provider-health reporting; authenticated APIs; CLI; opt-in probe | Frontend |

## 1a. Data acceptance policy and approved Phase 2 decisions

Yahoo Finance (through the configured `yfinance` integration) is the **approved initial provider** for the MVP. Whatever it
returns for the ten approved equities is accepted, with its limitations detected, classified and documented rather than
used as a reason to reject the dataset. Accepting the data does not mean relabelling it: provenance and the
`UNVERIFIED_PROVIDER_NATIVE` price basis are kept, nothing is called as-traded or exchange-verified, and no second
provider is sought. The provider architecture stays extensible for later improvements.

The project owner approved these eight decisions at Phase 2 finalization:

| # | Decision | What it means in the code today | Status |
|---|---|---|---|
| 1 | Execution-price source **deferred to Phase 4** | Every candle stays `UNVERIFIED_PROVIDER_NATIVE`; nothing relabels data; no execution-price selection, Mock Exchange or fill logic exists. Yahoo prices *may* later be approved as an explicit MVP simulation input, on a documented price basis, never presented as verified exchange execution. | Implemented and tested; the selection itself is deferred |
| 2 | **First observation stays canonical** | Later differing observations are stored as separate non-canonical versions with their own observation time and batch; the original is never overwritten (trigger-enforced). Deterministic and idempotent. | Implemented and tested. No restatement has occurred in the real archive yet |
| 3 | **Provisional thresholds** retained: finalisation margin 60 s, stale after 15 min, provider unavailable after 3 consecutive failures | Configurable (`MARKETDATA_*`), labelled `PROVISIONAL` in the API, descriptive only. Not guaranteed Yahoo behaviour, not trading-risk rules; data age is reported separately. Trading-engine use needs a separate review. | Implemented and tested |
| 4 | **Inferred closures stay inferred** | No holiday calendar is invented. Verified sessions, inferred closures and unexplained absences are reported separately; an unexplained absence is never called a holiday. | Implemented and tested. No authoritative NSE calendar is approved |
| 5 | **Operator-initiated ingestion** | No scheduler, nothing on start-up; backfill and incremental are explicit CLI/API calls. Job locking, heartbeat recovery and idempotency unchanged. | Implemented and tested. Scheduling may be reconsidered later |
| 6 | **Any authenticated user may trigger ingestion, locally only** | Authentication on every endpoint; jobs visible only to their requester. | Implemented and tested. **Operator/admin-only ingestion is a prerequisite for hosted deployment** |
| 7 | **Near-real-time as observed**, no assumed fixed delay | Documentation describes measured behaviour; forming and finalised candles are distinguished; market time (`bar_start_ts`/`bar_end_ts`), observation time (`observed_at`) and ingestion time (`ingested_at`) are recorded separately. | Documented and implemented. The future delayed paper-trading execution model is a separate decision |
| 8 | **Tick-size normalisation deferred** | Provider precision is kept (6 dp storage, float32 noise preserved); no tick table, no guessed mapping, no snapping. | Implemented and tested. Needs an authoritative instrument-specific source |

## 2. Architecture

```
 API (/api/v1/market/*)  CLI (python -m app.marketdata.cli)     <- the only entry points
            \                   /
             MarketDataService  <- the single downstream-facing service
   ┌─────────────┼───────────────────────────────┐
 providers/   pipeline.py      archive.py      gaps.py  freshness.py  calendar.py
 (adapters;   normalise,       insert/idempo-   coverage  status from   NSE sessions,
  only yahoo   validate,       tent/restate-    & gaps    facts         overrides table
  imports      finalise        ment versions
  yfinance)                    adjustment.py (separate adjusted series)
            jobs.py: single-active-job lock, heartbeat recovery, per-instrument isolation
```

Rules enforced by tests: only `providers/yahoo.py` (and the opt-in probe) import `yfinance`; pandas never leaves the
adapter; the package imports nothing from trading logic; nothing imports `apscheduler`/`langgraph`; no synthetic-data code.

## 3. Yahoo adapter

Every request sets the adjustment-related parameters explicitly (yfinance's own defaults changed across versions):
`auto_adjust=False, back_adjust=False, actions=True, repair=False, prepost=False, rounding=False`, plus
`raise_errors=True` (failures raise instead of returning an empty frame) and `keepna=True` (NaN rows reach validation
instead of vanishing). `yfinance==1.8.0` is pinned.

Provider symbols are an explicit mapping (`instrument_provider_symbols`), never assumed equal to the trading symbol:
`RELIANCE` -> `RELIANCE.NS`, and so on for all ten. A probe marks each mapping `VERIFIED`/`FAILED`; futures have no
mapping.

### Capability status (stored in `provider_capabilities`, exposed at `/market/providers/capabilities`)

`DOCUMENTED` (stated, untested) · `VERIFIED` (demonstrated by a probe, evidence stored) · `UNSUPPORTED` (probe showed it
is not offered) · `UNKNOWN`. Code never declares `VERIFIED`; only a stored probe result can.

| Capability | Status | Evidence / note (probe 2026-11-11) |
|---|---|---|
| `HISTORICAL_INTRADAY_CANDLES` | VERIFIED | 5m bars for all 10 symbols |
| `FIVE_MINUTE_INTERVAL` | VERIFIED | A start of 60 days minus 10 minutes is accepted; **exactly 60 days is rejected** |
| `HISTORICAL_DAILY_CANDLES` | VERIFIED | ~1,235 daily bars over 5 years per symbol |
| `LATEST_CANDLE` | VERIFIED | The latest bar is returned **including the in-progress, partial bar** |
| `CORPORATE_ACTIONS` | VERIFIED | Dividends and splits, daily granularity; completeness not verified |
| `QUOTE_RETRIEVAL` | VERIFIED | Price and currency only; **no market timestamp** |
| `INSTRUMENT_METADATA` | VERIFIED | Exchange code (`NSI`), currency, timezone; no instrument name |
| `ADJUSTED_INTRADAY_PRICES` | UNSUPPORTED | `Adj Close` is present on intraday rows but identical to `Close` on every row |
| `FUTURES_CONTRACT_DATA` | UNKNOWN | Not probed: futures are disabled; no futures symbol exists to test |

## 4. Experimentally observed Yahoo behaviour

These findings drive the pipeline rules. All were measured, not assumed.

1. **The 60-day target is reachable.** Exactly 60 days fails only because time elapses before the server evaluates the
   request; a start 1 minute inside succeeds. The adapter requests 60 days minus a 10-minute safety margin.
2. **The data was near real-time in the tested environment.** The latest bar returned was the *currently forming* bar
   (in 125 of 129 polls over 22 minutes). No fixed 15-minute delay was observed, and none is assumed; equally, Yahoo
   gives no guarantee of real-time delivery or of any fixed latency. Agent-X therefore never relies on a delay value: it
   archives a bar only once it is final (§6) and records market time, observation time and ingestion time separately.
3. **Bars settle within ~31 s after their end** (5 bars x 3 symbols, last observed change 5-31 s after the bar closed).
   Small sample, liquid stocks, one midday window.
4. **The first bar of a request is truncated if the request starts inside a session** - even exactly on a bar boundary
   (e.g. volume 76,930 vs the true 156,911, and a different open). A start at or before the 09:15 open is clean. This
   would corrupt archived data and cause false restatements, so it is handled (§6).
5. **Missing bars are NaN rows**, almost all at 15:15-15:25 (about 38 sessions at 15:20 and 18 at 15:25 for every
   symbol), with volume 0. They are provider gaps, not data. One such bar was NaN on one fetch and valid on a later one.
6. **`Close` is split-adjusted even with `auto_adjust=False`.** At RELIANCE's 2024-10-28 bonus and HDFCBANK's 2025-08-26
   split the unadjusted daily close is continuous across the ex-date (ratio ~1.0, not ~2.0). It is **not as-traded**.
   (Dividend-adjusted values appear only in `Adj Close`, on daily data.) Whether *intraday* history is restated after a
   split is untested until a split falls inside the archive.
7. **Prices carry float32 noise** (about 7,000-8,700 values per symbol are off the 2-decimal grid, up to ~2e-4; in the
   archive 20,856 of 28,428 closes are off the 2-decimal grid, e.g. `1338.599976` for 1338.60). Values are stored as
   delivered at the archive's 6-decimal storage precision (`NUMERIC(18,6)`, the float's shortest repr, half-even at the
   7th place), which keeps the noise visible. They are **not** tick-normalised or rounded to 2 decimals: tick-size
   normalisation is deferred until an authoritative instrument-specific source is approved (decision 8).
8. **5-minute volume understates daily volume by 13-24%** (largest daily gap), consistent with the missing close bars.
9. Index timezone is `Asia/Kolkata` (aware); the bar timestamp is the bar **start**; the session's first bar is 09:15 and
   the last is 15:25. No duplicates, misaligned or out-of-session bars were returned.
10. **Zero-volume bars are common at the open.** 401 archived bars have volume 0, 370 of them the 09:15 bar (31 at 15:15).
   Zero volume is valid per the pipeline and accepted as provider data; it is reported here, not corrected. It adds to the
   5-minute volume shortfall (finding 8).

## 5. Archive contents (agentx database, evaluated 2026-11-11 ~14:22 IST)

Requested coverage: the latest 60 calendar days of 5-minute candles (2026-09-12 -> 2026-11-11). Provider symbols
verified: 10 of 10. Price basis of every stored candle: `UNVERIFIED_PROVIDER_NATIVE` (28,428 candles).

| Symbol | Candles | First bar (IST) | Last bar end (IST) | Expected | Missing | of which provider gap markers | Partial days | Absent weekdays |
|---|---|---|---|---|---|---|---|---|
| RELIANCE | 2,849 | 2026-09-15 09:15 | 2026-11-11 14:10 | 3,211 | 362 | 60 | 39 | 4 |
| HDFCBANK | 2,845 | 2026-09-15 09:15 | 2026-11-11 14:10 | 3,211 | 366 | 64 | 39 | 4 |
| ICICIBANK | 2,836 | 2026-09-15 09:15 | 2026-11-11 14:10 | 3,211 | 375 | 73 | 39 | 4 |
| SBIN | 2,844 | 2026-09-15 09:15 | 2026-11-11 14:10 | 3,211 | 367 | 65 | 39 | 4 |
| INFY | 2,841 | 2026-09-15 09:15 | 2026-11-11 14:10 | 3,211 | 370 | 68 | 39 | 4 |
| TCS | 2,843 | 2026-09-15 09:15 | 2026-11-11 14:10 | 3,211 | 368 | 66 | 39 | 4 |
| LT | 2,837 | 2026-09-15 09:15 | 2026-11-11 14:10 | 3,211 | 374 | 72 | 39 | 4 |
| BHARTIARTL | 2,840 | 2026-09-15 09:15 | 2026-11-11 14:10 | 3,211 | 371 | 69 | 39 | 4 |
| ITC | 2,848 | 2026-09-15 09:15 | 2026-11-11 14:10 | 3,211 | 363 | 61 | 39 | 4 |
| AXISBANK | 2,845 | 2026-09-15 09:15 | 2026-11-11 14:10 | 3,211 | 366 | 64 | 39 | 4 |

How to read it: **missing = 4 absent weekdays x 75 + provider gap markers (+ a few not-yet-final slots)**. The four absent
weekdays - 2026-09-14, 2026-10-02, 2026-10-20, 2026-11-10 - had no data for *any* of the ten symbols, so they are listed as
**inferred market closures** (an inference, not a calendar). Zero are unexplained. No instrument has
`full_coverage_of_requested_range = true`, because of the provider gap markers near the close. Restatement events
observed so far: **0** (the window between runs was short; this is not evidence that none occur). 410 corporate actions
(dividends and splits) are recorded from Yahoo. Coverage figures drift as the archive grows; query `/market/coverage`.

## 6. Pipeline rules

`pipeline.py` turns provider rows into candles. Nothing is corrected or filled; every rejection is a `data_quality_issues`
row that keeps the raw row.

| Rule | Outcome |
|---|---|
| Timestamp missing / **naive** (no verified rule to assume a zone) | quarantined (`INVALID_TIMESTAMP` / `NAIVE_TIMESTAMP`) |
| Off the timeframe grid | `MISALIGNED_TIMESTAMP` |
| Outside a known session (weekend, pre-open, after close) | `OUT_OF_SESSION` (a holiday/special session not in the calendar looks the same) |
| All prices NaN (provider gap marker) | `NON_NUMERIC_VALUE`, flagged `provider_gap_marker` |
| Some prices NaN / infinite / non-positive / OHLC inconsistent | `NON_NUMERIC_VALUE` / `NON_FINITE_VALUE` / `NON_POSITIVE_PRICE` / `INVALID_OHLC` |
| Volume missing, negative or fractional (zero is valid) | `INVALID_VOLUME` |
| Identical duplicate timestamps | collapsed to one, `DUPLICATE_IDENTICAL` (info) |
| Conflicting duplicates | all quarantined, `DUPLICATE_CONFLICTING` |
| **First bar of a request that started inside a session** (provider truncates it) | quarantined, `WINDOW_EDGE_TRUNCATED` |
| Bar not final: `observed_at < bar_end + margin`, **or** the bar ends after the request's own end | not archived, `INCOMPLETE_CANDLE` |

Finalisation: a bar is archived only when `observed_at >= bar_end + margin` (margin **60 s, provisional**, decision 3),
and never when it ends after the request's own end. The 60 s value has headroom over the ~31 s settling time measured in
one small sample; it is not a guarantee of Yahoo behaviour. Boundary tests: observed 59 s after the end is not final;
60 s is.

**Forming vs finalised candles.** Yahoo returns the currently forming bar as its latest row. That bar is dropped with an
`INCOMPLETE_CANDLE` info issue and is picked up by a later run once final. A database check constraint
(`only_finalized_archived`) makes `is_final = false` impossible in `candles`, and every candle the API serves carries
`is_final: true`. There is no "latest forming candle" endpoint: the newest candle the API returns is the newest
*finalised* one, and `/providers/health` reports its end as `latest_finalized_bar_end`.

Timestamps: `bar_start_ts`/`bar_end_ts` are UTC instants (market time); `session_date` is the exchange-local (IST)
trading date; `observed_at` is when Agent-X received the bar; `ingested_at` is the database insert time. Session and
bar-grid calculations use `Asia/Kolkata`; storage and the API use UTC. Every API timestamp is serialised as UTC with a
`Z` suffix (e.g. `2026-11-02T03:45:00Z` = 09:15 IST), including those inside a job `summary`.

Request windows: backfill/incremental starts are placed at a **local midnight** where the provider's lookback allows
(clean first bar). A 60-day request must start at the lookback floor (midnight would exceed the limit), so its first bar is
discarded only if the start falls inside a session. Incremental runs overlap the previous 24 h to compare against what is
archived.

## 7. Archive, restatements and jobs

* **Idempotent:** re-ingesting unchanged candles inserts nothing. Unique keys: `(instrument, provider, timeframe,
  bar_start_ts, observation_version)` plus a partial unique index allowing one *canonical* version per bar.
* **Restatements:** if the provider returns different OHLCV for an archived bar, the new observation is stored as a
  **non-canonical** version, a `data_restatement_events` row records the changed fields, and a quality issue is logged. The
  archived row is never modified (a database trigger forbids it) and never deleted. The first observation stays
  canonical (**approved policy**, decision 2); later versions are never promoted. Seeing a value revert does not create
  further versions, and replaying the same observations creates nothing (idempotent). Each version keeps its own
  `observed_at`, ingestion batch and `observation_version`; every `data_restatement_events` row points at the canonical
  (first) observation. The API serves canonical rows only and shows `is_canonical` and `observation_version`.
* **Provenance** on every candle: provider, provider symbol, ingestion batch, source metadata (library, version,
  request parameters), observation time, price basis.
* **Raw vs adjusted:** `candles` holds provider-native values only. `adjusted_candles` is a separate table (method,
  factor, source, corporate-action-set version). `compute_split_adjusted` applies a split to a bar *only if the bar was
  captured before the ex-date*; otherwise the provider may already have adjusted it and no row is written. Dividends are not
  applied. No adjusted value can enter `candles`. Which prices may feed future Mock Exchange fills is decided in Phase 4
  (decision 1); until then no price is classified as execution-ready. The function is not exposed through the API or CLI;
  `GET /candles?series=adjusted` only reads rows that already exist (none in the real archive).
* **Real-archive validation of `compute_split_adjusted`** (2026-11-11, on a data-only copy of the archive restored into
  the disposable test database; the real archive was only read and its row counts were unchanged afterwards): 31 splits
  are on record (e.g. RELIANCE 2024-10-28 and HDFCBANK 2025-08-26, both bonus issues recorded by Yahoo as 2:1 splits); all
  predate the archive (2026-09-15..2026-11-11). Result: 28,428 bars examined, factor 1 for all, 0 adjusted, 0 skipped,
  0 values differing from raw, so **no bar was adjusted a second time**. Re-running wrote nothing (idempotent). A
  hypothetical 2:1 split dated after the archive (rolled back) halved all 2,848 ITC bars and doubled volume exactly once
  (half-even at the 6th decimal, e.g. `255.449997 -> 127.724998`); a hypothetical split dated on the capture day
  adjusted only the bars it applied to and skipped 2,789 bars as ambiguous, as designed. Limitations: the real archive
  contains no split inside or after its window, so a real-world adjustment has not occurred; whether Yahoo restates
  *intraday* history after a split remains untested; dividends are not applied.
* **Jobs:** `data_ingestion_jobs` allows at most one `QUEUED`/`RUNNING` job (partial unique index). A job with no heartbeat
  for 300 s is marked `INTERRUPTED` by the next request. Each instrument runs in its own session: one failure cannot break
  the others; the job ends `SUCCEEDED`, `PARTIAL` or `FAILED`. Every batch is recorded, including failures.
* **No scheduler (approved, decision 5).** Nothing runs on start-up and no APScheduler dependency exists. Ingestion is
  started by an operator (CLI or API). Scheduling may be reconsidered in a later phase; the trading agent is a separate
  lifecycle and is not started by anything here.

## 8. Calendar and gaps

Known: the regular NSE equity session is 09:15-15:30 IST, Monday-Friday (75 five-minute bars). **Not integrated:** an
authoritative holiday / special-session source (O13; none has been approved). The calendar therefore ships with **no
holidays** (`exchange_calendar_days` is empty) and claims no holiday coverage. `/coverage` keeps three things apart:

* **Trading-session information** - the regular weekday session above, plus any explicit entry in
  `exchange_calendar_days` (none today). This is the only calendar input.
* **Inferred market closures** (`probable_market_closures_inferred`) - weekdays absent for *every* enabled instrument
  while other days have data. An inference from the archive, **not a verified NSE holiday**; it does not change the
  calendar. One instrument alone cannot corroborate a closure. The 4 in the archive (2026-09-14, 10-02, 10-20, 11-10)
  are inferred only.
* **Unexplained absences** (`unexplained_absences`) - any other absent weekday. Never classified as a holiday.

The response's `calendar` block states `authoritative_holiday_source: null` and
`inferred_closures_are_verified_holidays: false`. Missing intervals inside a day are reported separately again.
* Special sessions (e.g. a Saturday or evening session) appear as quarantined `OUT_OF_SESSION` rows until an approved
  entry is added to `exchange_calendar_days`.
* Slots whose bar has not yet ended are never counted as missing.

## 9. Freshness and provider health

Statuses: `HEALTHY`, `DEGRADED`, `STALE`, `UNAVAILABLE`, `INCOMPLETE`, plus `UNKNOWN` (nothing observed; never shown as
healthy). Instrument freshness compares the newest archived bar with the newest bar that *should* be final by now (closed
markets are not "stale"). Provider health is derived from `provider_health_state` (last attempt/success/failure, error
kind and message, consecutive failures, last observed market timestamp, last successful ingestion). A provider that answers
"no data" is reachable, not failing.

**Thresholds are provisional Phase 2 settings (decision 3), configurable and descriptive only:** finalisation margin
60 s (`MARKETDATA_FINALIZATION_MARGIN_SECONDS`); stale after 15 minutes of lag (`MARKETDATA_STALE_AFTER_MINUTES`);
provider unavailable after 3 consecutive failures (`MARKETDATA_PROVIDER_UNAVAILABLE_AFTER_FAILURES`). The API labels each
`PROVISIONAL`. They are not guaranteed Yahoo behaviour and not trading-risk rules: a `STALE` or `UNAVAILABLE` status
blocks no read and no ingestion (tested). Alongside the status, each instrument reports the observed `data_age_seconds`
(now minus the newest finalised bar end) and `lag_seconds` (behind the newest bar that should be final). Any future
trading-engine use of these thresholds needs a separate review.

## 10. Known limitations

These are accepted under the data acceptance policy (§1a); they are reported, not hidden.

1. **Yahoo is unofficial** (no SLA; behaviour can change). Intraday depth is limited to ~60 days; the archive keeps older
   data once collected, but nothing beyond 60 days can be back-filled from Yahoo. The 60-calendar-day target is reached
   in request terms; no instrument has `full_coverage_of_requested_range = true` (§5).
2. **Gap markers near the close are common** (15:15-15:25); close-of-day bars are therefore often absent, and 5-minute
   volume understates daily volume. The 09:15 bar often has volume 0.
3. **`Close` is split-adjusted, not as-traded.** Candles stay `UNVERIFIED_PROVIDER_NATIVE`; none is labelled
   `AS_TRADED_VERIFIED`. The execution-price source is decided in Phase 4 (decision 1).
4. **Restatements**: the first observation stays canonical (decision 2). None has occurred in the real archive yet;
   intraday restatement after a split is untested.
5. **Float32 noise** is preserved; tick-size normalisation is deferred (decision 8).
6. **No authoritative holiday source** (O13). Closures are inferred, never asserted (decision 4).
7. **Ingestion can be triggered by any authenticated user** - a local-development policy only (decision 6). Restricting
   it to an authorised administrator/operator is a **prerequisite for hosted deployment**.
8. **Thresholds** are provisional and descriptive (decision 3).
9. **Futures** stay disabled: no contract-specific data source exists. `FUTURES_CONTRACT_DATA` is `UNKNOWN`.
10. The corporate-action ratio is "new:old" as reported; Yahoo does not distinguish a bonus from a split. Corporate-action
    completeness is not verified; dividends are recorded but not applied to any series.
11. **Split adjustment** has been validated on the real archive only in the trivial case (every split predates the
    archive); a real in-window split has not yet happened.
12. Job summaries written before the UTC-format fix (the two real jobs from the initial backfill) store `window_start` /
    `window_end` as `+00:00` strings; they were not rewritten. New summaries use `Z`.

### Decisions for future phases

* Execution-price source and price basis for Mock Exchange fills (Phase 4).
* Whether and how the provisional thresholds may gate trading (trading-engine review).
* An authoritative NSE holiday / special-session source (O13).
* Automatic scheduling of ingestion.
* Operator/administrator authorisation for ingestion (before hosted deployment).
* Tick-size source and normalisation.
* The delayed paper-trading execution model (separate from the observed data latency).

## 11. Operations

```powershell
cd backend
..\.venv\Scripts\python.exe -m app.marketdata.cli seed                  # register instruments + provider (idempotent)
..\.venv\Scripts\python.exe -m app.marketdata.cli probe --persist       # opt-in, uses the network; stores capability evidence
..\.venv\Scripts\python.exe -m app.marketdata.cli backfill --days 60    # [--symbols RELIANCE,TCS]
..\.venv\Scripts\python.exe -m app.marketdata.cli incremental
..\.venv\Scripts\python.exe -m app.marketdata.cli coverage --days 60
..\.venv\Scripts\python.exe -m app.marketdata.cli status
..\.venv\Scripts\python.exe -m app.marketdata.probe --symbols RELIANCE  # probe only, no database; writes data/probe_reports/
```

Inside Docker: `docker compose run --rm api python -m app.marketdata.cli <command>`. Probe reports are written to the
git-ignored `data/probe_reports/`. The automated test suite never touches the network or the real `yfinance`.
