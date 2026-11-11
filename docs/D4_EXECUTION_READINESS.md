# D4 — Execution Readiness (Yahoo Finance, simulation-only)

**Status (2026-11-12): B3, B4, B5, B10, the operator/ingestion/staging tooling and the staging-readiness fixes
(validator timing, approval references, freshness policy, synthetic-schedule guard, stale protective exits) are
implemented and tested; the development database is at `0011`. Staging is approved and fully scripted but has not run:
it waits for fresh intraday data (next NSE session) and an administrator to provision `agentx_staging`. B2, B7
(measurement), B8 (staging part) and the real-environment B1 approval remain open (§8). Execution remains DISABLED in
development**
(`EXECUTION_MODE` defaults to `disabled`; no local override). Paper trading only: real-money execution is out of scope
and does not exist anywhere in the codebase.

## 0. Three environments (do not confuse them)

| | Development (`agentx`) | Simulation-only staging (`agentx_staging`) | Real-environment execution |
|---|---|---|---|
| `EXECUTION_MODE` | `disabled` | `simulation_provider_native`, **inside the validator process only** | `simulation_provider_native` in configuration |
| Cost schedule | none | `SYNTHETIC_STAGING_ONLY` file outside Git (refused anywhere else) | verified Zerodha delivery schedule (B2) |
| Approval reference | none | `STAGING-ONLY: <ref>` (prefixed by the validator) | owner approval id for the environment |
| Data | live archive | `pg_dump` copy of the archive | live archive |
| Database role | `agentx_app` | dedicated least-privilege role (STAGING.md §1) | `agentx_app` |
| Status | in use | ready to provision; **not yet run** | **blocked** (§7.3, §8) |

Staging proves the simulation mechanics on real Yahoo bars; it does **not** prove the cost rates (they are synthetic)
and does not enable anything outside its own process.

**Approved staging decisions (owner, 2026-11-12):** Yahoo Finance; `UNVERIFIED_PROVIDER_NATIVE`; simulation only;
DELIVERY only; 5-minute ingestion during market hours; 15-minute maximum price age; synthetic staging-only schedule;
`agentx_staging` owned by the least-privilege role `agentx_staging_app`; approval reference
`STAGING-ONLY:AGENTX-MOCK-EXCHANGE-VALIDATION-001` (authorises only the isolated staging exercise, not real trading);
approval references immutable on accepted orders and fills; pending orders cancelled `EXECUTION_APPROVAL_CHANGED` when
the approval changes; real-money execution prohibited. Procedure and status: [STAGING.md](STAGING.md).

## 1. Decision recorded (owner, 2026-11-11)

- **Yahoo Finance is Agent-X's initial market-data provider for the MVP.** The ~60-day intraday history limit is
  accepted. TrueData is not evaluated further (findings kept in §11).
- Yahoo data keeps the `UNVERIFIED_PROVIDER_NATIVE` label. **It is never relabelled as verified.**
- Paper execution on Yahoo data uses an explicit **simulation-only** mode, separate from any verified mode.
- **Delivery** trading is the MVP product.
- Execution stays disabled until the simulation price policy, the cost schedule and every configuration safeguard
  are approved (§7).

## 2. Disclosures (served by `GET /api/v1/exchange/status` in simulation mode)

1. **Simulation only.** Fills are retrospective simulations at the open of the next finalized 5-minute bar, priced from
   `UNVERIFIED_PROVIDER_NATIVE` Yahoo Finance data. They are not verified executions and do not represent prices that
   were or could have been traded.
2. **No five-year intraday backtesting at launch.** Yahoo provides about 60 days of 5-minute history. The local
   archive grows only prospectively; bars that were never collected cannot be recovered (`UNRECOVERABLE_GAP`).
3. **Adjustment basis.** Yahoo's daily OHLCV is split-adjusted (§4). Bars with an ambiguous basis are rejected.
   Positions affected by a split/bonus are frozen until an evidence-backed adjustment is applied (§4.4).
4. **Dividends** are credited gross (no TDS deducted), only after an operator confirms the amount and payment date with evidence (§4.5).

## 3. Data collection (Phase 2, unchanged)

| Requirement | How it is met |
|---|---|
| Rolling ~60-day 5-minute history | `python -m app.marketdata.cli backfill --days 60` (or `POST /api/v1/market/ingestion/backfill`) |
| Ongoing collection | `cli incremental` / `POST /api/v1/market/ingestion/incremental`, operator-run (no scheduler; Phase 2 decision 5) |
| Persisted, growing archive | Each finalized bar stored once in `candles`; the first observation stays canonical; restatements are kept as non-canonical versions |
| Provenance | Provider, provider symbol, ingestion batch, `observed_at`, `ingested_at`, `price_basis = UNVERIFIED_PROVIDER_NATIVE` |

## 4. Adjustment basis and corporate actions

### 4.1 Evidence
Probe on 2026-11-11, using Phase 2's exact parameters, on daily bars around two 2:1 actions
([raw output](evidence/2026-11-11_yahoo_daily_ohlcv_adjustment_probe.txt)):

| Ex-date / ratio | Open post/pre | High | Low | Close | Volume |
|---|---|---|---|---|---|
| RELIANCE 2024-10-28, 2:1 | 0.9952 | 1.0064 | 1.0001 | 1.0049 | 0.5820 |
| HDFCBANK 2025-08-26, 2:1 | 0.9994 | 0.9970 | 0.9893 | 0.9912 | 0.9657 |

All four daily price fields are back-adjusted for splits; volume is not as-traded either. Whether intraday bars are
restated after a split is untested: no split has fallen inside the archive window yet.

### 4.2 Bar-level policy (implemented)
1. Within a session every bar shares one basis (an ex-date is a session boundary), and orders are DAY orders.
2. A bar first observed after a later split/bonus/other ex-date is rejected (`CORPORATE_ACTION_AFTER_BAR`).
3. A bar whose ingestion batch could not fetch corporate actions is rejected (`CORPORATE_ACTIONS_UNAVAILABLE`).
4. Cash dividends do not block execution; dividend cash is not credited.

### 4.3 Freeze (implemented)
A position is frozen when a split/bonus/other action has an ex-date after the position's **simulated** open date
(B10, §5.3a) and no APPLIED adjustment exists for it.
- Every order in that instrument is refused, including exits and protective exits
  (`CORPORATE_ACTION_UNADJUSTED_POSITION`).
- Its mark is unavailable, so account equity is undetermined. New entries in **every** instrument are therefore
  rejected (`EQUITY_UNDETERMINED`). This is deliberate: equity-based limits cannot be computed reliably while one
  position's value is unknown.
- Unaffected positions keep their marks and their exits.

### 4.4 Recovery: corporate-action adjustment (B4, implemented; operator-only)
Workflow (operator, `/api/v1/operator/...`; see [OPERATIONS.md](OPERATIONS.md) §4):
1. **Detect:** `GET /operator/corporate-actions/pending`.
2. **Validate the evidence:** an evidence reference, plus the ratio **re-entered** by the operator, which must match
   the stored record exactly.
3. **Create the adjustment event** for every affected position, recorded per user with the operator's id.
4. **Apply it atomically.**
5. **Verify:** cost basis is preserved; the action no longer freezes the position.
6. **Resume** valuation and exits.

Users can no longer adjust their own portfolio. They see their own records read-only. The operator flag is set
only on the server (`python -m app.ops.cli set-operator`), and denied attempts are audited.

| Rule | Implementation |
|---|---|
| Arithmetic (ratio new:old = n:d, f = n/d) | quantity × f; average ÷ f (10 dp, half-even); stop/take-profit ÷ f (6 dp); cost basis preserved |
| Cash | unchanged, except a separately recorded `CASH_IN_LIEU` ledger entry |
| Fractional entitlements | never rounded silently. Without a cash-in-lieu price **and its source and observation time**, the attempt is `BLOCKED_FRACTIONAL` and the position stays frozen. The price is operator-supplied with evidence and **never estimated**. Source, timestamp and operator are recorded (`cash_in_lieu_source`, `cash_in_lieu_observed_at`, `requested_by`) |
| Idempotency | one APPLIED row per position and action (partial unique index); a re-run finds nothing left to adjust |
| Order / scope | ex-date order; SPLIT and BONUS only (OTHER is never auto-adjusted) |
| Immutability | fills and earlier ledger entries are never modified; adjustment rows are append-only |
| Atomicity | one transaction; any failure rolls back everything |

### 4.5 Dividends (B5, implemented; operator-only)
Owner decision: operator-confirmed, gross, credited on the payment date (`app/exchange/dividends.py`).
- **Entitlement:** shares held at the **close of the last session before the ex-date**. This is replayed from
  immutable fills (by simulated fill time) and APPLIED split adjustments; mutable position state is never used.
  Trades on or after the ex-date do not change it.
- **Amount and payment date:** confirmed by an operator with an evidence reference. Yahoo supplies only an ex-date
  and an amount, possibly adjusted later. A confirmed amount that differs from Yahoo's record is refused unless
  `accept_provider_difference` is set; both values are recorded.
- **Credit:** the gross amount is booked as a separate `DIVIDEND` ledger entry, linked to an append-only
  `dividend_credits` row. Fills are never touched. Equity includes it through cash. Daily P&L reports it as
  `dividend_income` on the payment date, separately from trading P&L.
- **Refused and recorded, nothing credited:**
  - a payment date in the future or before the ex-date;
  - missing evidence;
  - an unaccepted amount difference;
  - a holding across an unadjusted split/bonus (`UNADJUSTED_CORPORATE_ACTION`).
- **Duplicates:** one APPLIED credit per user and dividend (partial unique index). A re-run, restart or late
  ingestion returns the existing credit.
- **Not implemented:** TDS / tax treatment; record-date data (Yahoo has none); dividend reinvestment.

## 5. Simulation-only execution mode

### 5.1 Modes (`EXECUTION_MODE`)
| Mode | Accepts | Fill policy recorded |
|---|---|---|
| `disabled` (default) | nothing | — |
| `verified_next_bar_open` | verified bases listed in `EXECUTION_APPROVED_PRICE_BASES` | `VERIFIED_NEXT_BAR_OPEN` |
| `simulation_provider_native` | **only** `UNVERIFIED_PROVIDER_NATIVE`; the basis list must be empty | `SIMULATION_PROVIDER_NATIVE` |

The modes never mix:
- Each mode rejects the other's basis.
- An order is only ever filled under the policy it was accepted with (`EXECUTION_MODE_CHANGED`).
- The database enforces the pairing between fill policy and price basis (`ck_fills_price_basis_matches_policy`).

### 5.2 Fill price
Fills use the open of the next eligible 5-minute bar, plus slippage. An order never fills at submission. See
[PHASE4.md](PHASE4.md) §3.2.

### 5.3 Recorded for every order and fill
Recorded on each order and fill:
- signal time (`orders.signal_at`);
- decision bar (`orders.decision_price_ref`);
- acceptance time (`orders.accepted_at`);
- execution policy;
- execution candle (`fills.price_source_ref`);
- reference and fill price;
- provider and price basis;
- product (`orders.product`);
- fees (`fills.fee`, `fills.fee_breakdown`);
- the cost schedule version, SHA-256 and rate ids used (`fills.cost_model`);
- realized P&L (`fills.realized_pnl`);
- the **approval reference** in force at acceptance (`orders.execution_approval_reference`,
  `fills.execution_approval_reference`; migration `0010`).

### 5.3b Approval reference (migration 0010)
- An order accepted under an execution policy records `EXECUTION_APPROVAL_REFERENCE`; while execution is disabled it
  is `NULL`. A NOT VALID check requires it for every new order that carries a policy.
- The value is immutable: it is one of the order guard trigger's identity columns.
- Every fill copies its order's value. A NOT VALID check requires it on new fills, an insert trigger
  (`trg_fills_approval_matches_order`) refuses any other value, and fills are append-only.
- An order is never filled under a different approval: if the active reference differs from the order's, processing
  cancels it (`EXECUTION_APPROVAL_CHANGED`), exactly as a mode change does (`EXECUTION_MODE_CHANGED`).
- Rows created before `0010` keep `NULL` (history is not rewritten). Downgrading `0010` is refused while any reference
  exists. The API returns the field on orders and fills.

### 5.3a Time (B10, fixed)
| Column | Meaning | Used for |
|---|---|---|
| `fills.price_as_of` | **Simulated fill time** (bar start for an open fill; bar end for a limit crossed inside the bar) | position open date, P&L attribution, cost-rate date, rounding groups, DP-per-day |
| `fills.price_observed_at` | When the finalized candle became available to Agent-X | audit (≤ booking time, DB check) |
| `fills.executed_at` | When the simulator booked the fill | audit only |

- `positions.opened_at` is now set from the simulated fill time (`app/exchange/service.py`, `_execute`).
- Each fill stores its own `realized_pnl`. Daily P&L (`GET /portfolio/pnl/daily`) groups fills by simulated date, and
  cash-in-lieu by ex-date.
- Processing applies bars in **simulated time order across all of an account's orders**, so a catch-up run books
  fills in the order they would have happened. Average prices and realized P&L no longer depend on when processing ran.
- Regression: a 5 Jan fill processed on 6 Jan (a split ex-date) now opens on 5 Jan and is frozen. The sell on the
  post-split scale is refused, and no false loss is booked
  (`test_regression_fill_processed_on_a_split_ex_date_cannot_book_a_false_loss`).
- Remaining limit: `observed_at` is when Agent-X fetched the candle, not when Yahoo first published it.

### 5.3c Freshness policy (B7, development decision 2026-11-11)
- **Target ingestion cadence:** every 5 minutes during market hours, run by an external scheduler
  ([OPERATIONS.md](OPERATIONS.md) §1). Agent-X still never schedules itself.
- **Maximum price age:** 15 minutes (`EXECUTION_MAX_PRICE_AGE_SECONDS=900`, the default), measured from the reference
  bar's close to the decision time.
- **Stale prices block new orders** (entries and exits alike): `EXECUTION_PRICE_UNAVAILABLE: STALE`, recorded on the
  rejected order and in `risk_decisions`. Stale data is never treated as current.
- **Existing positions** stay under the established risk policy: marks need an eligible price, so equity is reported
  `null` with the reason rather than estimated. Protective exits are never triggered from stale data and no price is
  fabricated; instead (migration `0011`, `protective_exit_requirements`):
  1. the first run that cannot evaluate a position's stop/target records one OPEN requirement (reason, the stale bar,
     and `stale_trigger` = SL/TP if that bar had already crossed the level) and an audit alert
     `risk.protective_exit_unresolved`;
  2. the position is reported `protective_exit_unresolved: true` (`GET /portfolio`), the risk state counts it
     (`unresolved_protective_exits`) and operators see every user's OPEN requirements
     (`GET /operator/protective-exits/unresolved`);
  3. later stale runs update the same row (`checks`, `last_checked_at`); there is at most one OPEN row per position;
  4. the first run on a fresh eligible price resolves it under the same rule: an exit is submitted if the fresh bar
     crosses **or** a crossing was already observed on the stale bar (`EXIT_SUBMITTED`); otherwise `NOT_TRIGGERED`;
     `EXIT_ALREADY_PENDING` if an exit already rests; `POSITION_CLOSED` if the position was closed meanwhile; an exit
     refused by the exchange keeps it OPEN. Each resolution raises `risk.protective_exit_resolved`;
  5. exits are deduplicated by the existing rules (client order id per position and observation; sellable quantity
     excludes resting exits); rows are never deleted, resolved rows are frozen and a recorded `stale_trigger` cannot be
     cleared (trigger).
  Runs are explicit: `POST /api/v1/exchange/protective-exits` (Phase 5 will call the same service each cycle).
  Orders already resting fill on their next bar once it is recorded (fill bars have no age limit, only the
  no-lookahead and integrity checks).
- **Ingestion failures** are recorded by Phase 2 (`data_ingestion_jobs`, `provider_health_state`, per-run reports).
- Still to confirm before the real environment: the latency measurement on the integration machine (OPERATIONS.md §2).

### 5.4 Rejected candles
- **Stale:** a reference bar older than `EXECUTION_MAX_PRICE_AGE_SECONDS`.
- **Incomplete:**
  - not final or not canonical;
  - a WARNING/ERROR data-quality issue recorded for the bar;
  - corporate actions unavailable for its batch.
- **Missing:** bars must be contiguous (`DATA_GAP`); there are no cross-session fills.
- **Invalid:** bad OHLC, non-positive prices, negative or zero fill volume, naive or future timestamps, or an
  unapproved provider.
- An ineligible next bar cancels the order (`FILL_BAR_INELIGIBLE`).

### 5.5 Configuration safeguards
- An enabled mode refuses to start without all of:
  - `EXECUTION_APPROVAL_REFERENCE`;
  - exactly one provider;
  - a basis list that matches the mode;
  - `EXCHANGE_PRODUCT=DELIVERY`;
  - a readable, valid `EXCHANGE_COST_SCHEDULE_PATH`.
- At runtime the gate also requires the schedule to cover today's date for every required charge and side, with
  rates that are not all zero. Otherwise every order is refused with `CONFIG_INVALID` and the reason.

## 6. Cost model (B3, implemented)

`app/exchange/costs.py` is the single implementation. It is pure and deterministic (Decimal arithmetic), and
backtesting and the benchmark (Phase 6) must import it.

### 6.1 Rate schedule
- **Format:** an operator-maintained JSON file (`EXCHANGE_COST_SCHEDULE_PATH`); the format is in
  [cost_schedule.template.json](cost_schedule.template.json). **No rate ships with the code.**
- **Each entry has:**
  - `rate_id`, `product` (DELIVERY / INTRADAY), `charge_type`, `side` (BUY / SELL / BOTH);
  - `basis` (calculation base), `value`, `cap` (capped brokerage only), `applies_to` (GST only);
  - `effective_from` (inclusive), `effective_to` (exclusive, optional), `source`.
- **Schedule-level fields:** `schedule_version`; the file's SHA-256 is recorded on every fill.
- **Validation:**
  - allowed bases per charge;
  - stamp duty is BUY-only;
  - DP is SELL- and DELIVERY-only;
  - GST `applies_to` must be non-empty, base charges only;
  - a capped brokerage needs a cap;
  - unique rate ids;
  - **no overlapping effective ranges** for the same product, charge and side;
  - a rounding rule for every money charge.
- **Selection:** the rate in force on the fill's **simulated session date**, for the order's product. A missing rate
  is an explicit error naming the charge, product, side and date. An all-zero rate set is refused; individual
  explicit zeros are allowed.

| Charge | Bases | Notes |
|---|---|---|
| BROKERAGE | `TURNOVER_BPS`, `TURNOVER_BPS_CAPPED_PER_ORDER` (cap across partial fills), `FLAT_PER_ORDER` (first fill only) | configurable mode |
| STT | `TURNOVER_BPS`, per side | delivery: both sides; intraday: sell (as configured) |
| TXN_CHARGE, SEBI_FEE, IPFT | `TURNOVER_BPS` | SEBI is published per crore: convert explicitly |
| STAMP_DUTY | `TURNOVER_BPS`, BUY only | |
| DP_CHARGE | `FLAT_PER_SCRIP_SELL_DAY`, DELIVERY SELL only | once per instrument per simulated session date |
| GST | `PCT_OF_CHARGES` on `applies_to` | whether DP is in the base is configurable |
| SLIPPAGE | `PRICE_BPS` | adverse; prices rounded against the trader to 6 dp |

### 6.2 Calculation order and rounding
1. turnover = quantity × executed price.
2. Raw base components.
3. Each base component is charged under its own rounding rule.
4. GST raw = GST% × the *charged* components in `applies_to`; it is then charged under its own rule.
5. Total = the sum of the charged amounts.

- **Rounding is per charge type**, with a stage, a quantum (e.g. 0.01 or 1) and a mode (UP / DOWN / HALF_UP /
  HALF_EVEN).
- **Stages:**
  - `FILL`;
  - `ORDER` (cumulative over the order's fills);
  - `SESSION` (cumulative over the account's fills that simulated day: contract-note level);
  - `SESSION_INSTRUMENT`.
- Cumulative stages charge `round(prior_raw + raw) − prior_charged`. The group total therefore equals the rounded
  total, and is never negative (property-tested).
- **No rounding scheme is assumed to match any broker's contract notes**; that needs external validation (§8, B2).

### 6.3 Sizing and reservations
- Risk sizing and cash reservations use conservative upper bounds, covering every fee plus one rounding quantum per
  component.
- For capped brokerage, the smaller of the rate bound and the cap bound is used.
- This is property-tested: the computed size never exceeds the available cash.

### 6.4 Products
DELIVERY and INTRADAY rates are both supported by the engine, ready for backtests. **Only DELIVERY orders are
executed**: intraday orders are refused (`PRODUCT_NOT_SUPPORTED`), because intraday square-off and margin are not
modelled.

## 7. Enabling checklist

### 7.1 Exact configuration (only after the blockers in §8)
```
MARKETDATA_PROVIDER=yahoo
EXECUTION_MODE=simulation_provider_native
EXECUTION_APPROVAL_REFERENCE=<owner approval id and date>
EXECUTION_APPROVED_PROVIDERS=["yahoo"]
EXECUTION_APPROVED_PRICE_BASES=[]
EXECUTION_CANDLE_TIMEFRAME=5m
EXECUTION_MAX_PRICE_AGE_SECONDS=<approved>
EXCHANGE_PRODUCT=DELIVERY
EXCHANGE_COST_SCHEDULE_PATH=<path to the approved schedule file>
EXCHANGE_MAX_VOLUME_PARTICIPATION=<approved or unset>
```
- **Verify after the change:** `GET /api/v1/exchange/status` shows `execution_enabled: true`, no blocking reasons, and
  the simulation disclosures.
- **Rollback:** set `EXECUTION_MODE=disabled`. Pending orders are then cancelled with `EXECUTION_MODE_CHANGED`.

### 7.2 Tests done (this branch)
See §10 for the results. Coverage:
- **B10**
  - same-day fill;
  - delayed processing by 1 and 3 days;
  - processing on a split ex-date (regression);
  - daily P&L by simulated date;
  - cross-order chronological booking.
- **B4**
  - 2:1, 5:1 and reverse splits;
  - fractional cash-in-lieu (blocked, then applied);
  - cash-in-lieu refused when there is no fraction;
  - duplicate application, and a DB refusal of a second APPLIED row or any edit;
  - ratio mismatch, missing evidence, OTHER-type actions, out-of-order actions;
  - rollback on an injected failure;
  - fills and ledger untouched;
  - valuation and close after adjustment;
  - trading refused before adjustment;
  - old-basis orders cancelled;
  - unaffected instruments' exits;
  - API, including isolation.
- **B3**
  - every component;
  - DP once per day, and GST-on-DP on/off;
  - IPFT;
  - each brokerage mode;
  - effective-date selection and boundaries;
  - missing-rate errors;
  - product selection;
  - every validation rule;
  - every rounding mode and stage (property-tested);
  - legacy breakdown reading;
  - the sizing bounds (property-tested);
  - fill-date rates in the exchange;
  - intraday refusal.
- **Migration `0007`:** clean upgrade, downgrade/upgrade round trip, no model drift, downgrade refused with data.

### 7.3 Outstanding requirements for real-environment enablement
- [ ] **Verified delivery cost rates** (B2): Zerodha's official tariff and every statutory rate, with sources and
      effective dates, in a schedule that is not `SYNTHETIC_STAGING_ONLY`.
- [ ] Cost **golden tests against real delivery contract notes** from the owner's broker (a buy, a sell with a DP
      charge, a multi-trade day). Every value in the current tests is a formula check on synthetic rates (§9).
- [ ] **Fresh intraday data**: at least one session ingested on the 5-minute cadence, so the archive holds a run of 8+
      bars recorded within 15 minutes of their close (2026-11-12 00:00 IST: best run 3; `ops freshness`).
- [ ] Staging runs on a copy of the real Yahoo archive (B9): both validator runs exit 0, and the lifecycle exercise
      exits 0 (STAGING.md §5).
- [ ] Latency measurement confirming the 15-minute threshold under the 5-minute cadence (B7).
- [ ] Full suite and Docker on the Windows integration machine, recorded (B8).
- [ ] **Final owner approvals**: the simulation price policy for the real environment (B1) and the
      `EXECUTION_APPROVAL_REFERENCE` to record on every order and fill.

## 8. Blockers

| # | Blocker | Classification | What remains |
|---|---|---|---|
| B1 | Simulation price policy | **APPROVED FOR STAGING; REAL ENVIRONMENT WAITING FOR USER APPROVAL** | Yahoo prices, `UNVERIFIED_PROVIDER_NATIVE`, simulation-only, approved for simulation-only staging (2026-11-11). Approval references are now recorded immutably on orders and fills (§5.3b). The real-environment approval and its reference are still to be issued |
| B2 | Delivery cost schedule (Zerodha) | **WAITING FOR EXTERNAL DATA** | The cost engine and the contract-note golden harness are ready. Needed: Zerodha's official delivery tariff (with effective dates) and redacted real delivery contract notes (a buy, a sell with a DP charge, a multi-trade day) |
| B3 | Cost model gaps | **RESOLVED** | Formula-tested; real-note validation is B2 |
| B4 | Corporate-action recovery | **RESOLVED** | Now operator-only, with cash-in-lieu provenance |
| B5 | Dividend crediting | **IMPLEMENTED BUT NOT VALIDATED** | Tested on synthetic data; no real dividend has been credited end to end on archive data. TDS is not handled (documented) |
| B6 | Daily ingestion | **IMPLEMENTED; SCHEDULE TO REGISTER** | `scripts\windows\ingest_intraday.ps1` run-tested on the Windows integration machine (2026-11-11: SUCCEEDED, real bars archived, nothing back-dated). The 5-minute Task Scheduler task (`register_ingestion_task.ps1`) must be registered by the owner |
| B7 | Slippage and staleness thresholds | **DECIDED (development); MEASUREMENT PENDING** | 5-minute ingestion target, 15-minute maximum price age, stale prices block new orders (§5.3c). Confirm with `ops latency` on the integration machine during market hours. The slippage value remains part of the B2 schedule |
| B8 | Windows | **PARTLY DONE** | Recorded in [WINDOWS_VALIDATION.md](WINDOWS_VALIDATION.md) (2026-11-12): environment, configuration, dev migration, suite, API start/stop/restart, ingestion, Docker. Staging steps wait for B9 prerequisites; the disposable-database migration round trip needs an administrator |
| B9 | Staging validation | **APPROVED AND SCRIPTED; NOT RUN** | Approval reference issued. Validator, lifecycle runner, copy and provisioning scripts ready and tested on synthetic archives with realistic lags. Waiting for: fresh intraday data (next session) and an administrator running `scripts/staging/provision_staging.sql`. Then `staging_copy.ps1` and `staging_validate.ps1` (STAGING.md) |
| B10 | Simulated fill time | **RESOLVED** | — |

Architectural decisions:
1. **Cash-in-lieu price source:** operator-supplied with an evidence reference, source and observation time,
   recorded with the operator id; never estimated. **Implemented.**
2. **New buys while a position is frozen:** the restriction is **kept**. No mathematically safe alternative exists,
   because a frozen position's current value has no upper bound, so gross exposure and equity-based limits cannot be
   bounded. Exits and marks of unaffected positions continue.
3. **Adjustment permissions:** **operator-only**, via a CLI-set flag. Users keep isolated read-only views. Every
   attempt, including denied ones, is recorded. **Implemented.**

## 9. Status by category

| Item | Implemented and tested | Implemented, not externally validated | Blocked by missing information | Not implemented |
|---|---|---|---|---|
| Simulated fill time / attribution (B10) | ✓ | | | |
| Corporate-action adjustment (B4) | ✓ (operator-only) | Real-world split on Yahoo intraday data not yet observed | Cash-in-lieu prices per event (operator) | OTHER-type actions (manual procedure) |
| Dividends (B5) | ✓ (synthetic data) | Real dividend end to end on archive data | Confirmed amount and payment date per event (operator) | TDS / tax treatment |
| Daily ingestion (B6) | ✓ (and run against live Yahoo) | Scheduled operation on the integration machine | — | In-app scheduling (by decision) |
| Cost engine (B3) | ✓ (formulas, synthetic rates) | Broker contract-note compatibility | Real rates and effective dates (B2) | Intraday execution (engine supports rates only) |
| Simulation mode and safeguards | ✓ | Behaviour on the real archive (B9) | Owner approval (B1) | |

## 10. Test results (2026-11-11, macOS, PostgreSQL 18, Python 3.14)

- **Full suite: 839 passed.** This covers Phases 1, 2 and 4: regression, unit, integration, API, authentication and
  isolation, risk engine, Mock Exchange, simulation safeguards, and B10/B4/B3.
- **Migrations:**
  - from an empty database `0001`→`0007`; down to base; back up; `alembic check` reports no drift;
  - `0007` round trip on the dev database;
  - downgrade refused while trading records exist (tested).
- **Mutation checks:** 25 B10/B4/B3 safeguards were each disabled in turn.
  - Two initially survived: DP-once-per-day inside the exchange, and the exclusive `effective_to`.
  - A targeted test was added for each, and both are now caught.
  - Result: 25 of 25 caught. The earlier 17 simulation-mode and 14 execution-model mutations remain covered.
- **Docker (macOS):**
  - the image builds and migrations report `0007`;
  - the API is healthy;
  - `/exchange/status` reports `disabled` with `EXECUTION_DISABLED` and `COSTS_NOT_CONFIGURED`;
  - the new corporate-action and daily-P&L endpoints respond.
- **Not yet run:** Windows integration machine (B8); the staging validator on the real archive (B9, needs an approved
  staging configuration); golden tests against real Zerodha contract notes (B2, needs the notes).

### 10.1 Finalisation round (B5, B6, B7, B9 tooling, operator role)
- **Full suite:** 865 passed (macOS, PostgreSQL 18, Python 3.14).
- **Mutation checks:** 13 new safeguards (operator gate, dividend idempotency / entitlement cut-off / unadjusted-split
  block / provider-difference / future payment / evidence, split replay, cash-in-lieu provenance, operator
  attribution, dividend-income date, staging DB guard, daily-run lock clock). 13 of 13 caught.
- **Migrations:** clean `0001`→`0008`, downgrade to base and back up; `alembic check` reports no drift.
- **Real-provider run (B6):** `ops daily` against live Yahoo into the Mac dev database (ingestion only).
  - SUCCEEDED: 28,577 bars for 10 instruments; corporate actions recorded for every batch (379 dividends, 31 splits,
    none inside the archive window); 684 Yahoo gap markers quarantined; every bar `UNVERIFIED_PROVIDER_NATIVE`.
  - Coverage reported **incomplete** (about 83 missing intervals per instrument over 10 days), as it should be.
  - An immediate re-run inserted 0 bars with 0 restatements (idempotent).
- **Latency (B7):** this archive is backfilled, so the minimum lag is 16,639 s. Under the 900 s threshold, once-a-day
  ingestion can never produce a fresh reference price; see the decision in §8.
- **Staging copy procedure (B9):** verified on the Mac's real archive: 28,577 = 28,577 candles, source unchanged,
  no superuser needed. The validator itself was **not** run on it (approval required).

## 11. Superseded: TrueData evaluation (for the record)

Not pursued for the MVP. Findings as of 2026-11-11:
- 5-minute bars for the last 6 months only;
- the licence excludes "Gaming / Virtual trading / Simulation usage";
- API adjustment basis and timezone are not stated;
- pricing is quote-only.

Sources:
[product](https://www.truedata.in/products/marketdataapi),
[licensing](https://www.truedata.in/market-data-apis),
[data availability](https://feedback.truedata.in/knowledge-base/article/historical-real-time-data-availability-through-market-data-api).
