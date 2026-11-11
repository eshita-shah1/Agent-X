# Phase 4 — Risk Engine and Mock Exchange

Status: the base implementation was merged in PR #1 (with Phase 2). This follow-up adds three things:
the **next-bar-open execution model**, the **Indian-equity component cost model** and **risk-state refresh**.
Simulated fills stay **blocked by default** until decision D4 is made (below).

## 0. Review status

| Item | Outcome |
|---|---|
| D4: execution-price source | **Not approved.** Yahoo Finance remains unapproved. A verified provider with quotes/trades is preferred. |
| Fee and slippage assumptions | Mandatory configuration with **no defaults**. The component model is implemented (section 3.3); its rates remain an owner decision. |
| Phase 4 base | Merged (PR #1). |

Review findings and their resolution:
1. **Same-bar fill risk: resolved.** The candle-close source is removed (`EXECUTION_PRICE_SOURCE=candle_close` is
   now invalid configuration). Orders never fill at submission. They fill at the **open of the first finalized bar
   that starts at or after acceptance**, with documented slippage and gap handling (section 3.2). This is a
   simulation assumption, not evidence of a real fill.
2. **Contract reconciliation with Phase 2: done** (section 5a).
3. **Cost model structure: resolved.** Indian-equity components (brokerage with a per-order cap, side-specific
   STT, exchange transaction charges, SEBI fee, buy-side stamp duty, GST on brokerage + charges), in a single
   module that backtesting and the benchmark (Phase 6) must reuse. No rate is built in.
4. **Risk-state staleness: resolved for Phase 4.** `RiskService.refresh` re-values the account and updates
   peak and day-start equity without an order. It is called by `GET /risk/state`, `POST /exchange/process`, the
   kill switch and every order submission. `last_equity_at` makes staleness visible. Phase 5 must call it on each cycle.
5. Synthetic fixture tests prove the execution logic under test conditions only, not the suitability of any real
   provider's data.
6. Docker must be verified on the integration machine, not only on the development Mac.

## 1. Scope (from the repository phase plan)

| Phase | Scope |
|---|---|
| 4 | Risk engine and Mock Exchange |

Built on Phase 1 and Phase 2. Phase 3 (indicators, strategies, selector) does not exist yet, so
Phase 4 accepts generic **order intents** instead of strategy signals.

Not in Phase 4 (later phases): LangGraph orchestration and the autonomous loop, scheduling,
`monitor` / pause mode, `manual_modify` rules beyond manual orders and cancels, the lifetime
dashboard metrics (Sharpe, profit factor, equity curve), round-trip trade records.

## 2. Decision D4 — execution-price source

### Evidence
- Phase 2 research (master PRD section 7.1, docs/MARKET_DATA.md): Yahoo `Close` is split-adjusted even with
  `auto_adjust=False`; forming candles are returned; no guaranteed fixed delay; float32 noise.
- Every archived candle is `price_basis = UNVERIFIED_PROVIDER_NATIVE`. A check constraint forbids a verified
  label without `basis_verified_by` evidence. Nothing in the repository verifies provider data.
- Therefore **no verified execution-price source exists**.

### Decision implemented
- Market data reaches the exchange only through the `ExecutionPriceSource` contract and the `ExecutionPriceGate`
  (`backend/app/exchange/pricing.py`). There are two uses, kept separate:
  - **Reference price**: the close of the latest finalized bar. It is used for decisions, risk sizing, cash
    reservation, marks and protective-exit triggers. It must be fresh. It is **never a fill price**.
  - **Fill bars**: finalized bars starting at or after an order's `accepted_at`. Fills happen only on these bars.
- Default configuration enables **no source** and approves **no basis and no provider**. Every order is therefore
  rejected with `NO_ELIGIBLE_PRICE: SOURCE_DISABLED` and nothing fills.
- `UNVERIFIED_PROVIDER_NATIVE` can never be approved. Three layers enforce this: the configuration validator, the gate,
  and the check constraint `ck_fills_price_basis_verified`.
- Yahoo data has **not** been relabelled.

### Acceptance requirements (reference prices and fill bars)
1. A source is explicitly enabled (`EXECUTION_PRICE_SOURCE=next_bar_open`).
2. The instrument matches.
3. Timestamps are timezone-aware.
4. Prices are positive.
5. The bar is final and canonical (Phase 2: first observation).
6. The bar ended and was observed by the decision time; no lookahead. A fill bar must also have started at or
   after acceptance.
7. The basis is verified **and** listed in `EXECUTION_APPROVED_PRICE_BASES`.
8. The provider is the single entry in `EXECUTION_APPROVED_PROVIDERS`.
9. Reference prices only: age ≤ `EXECUTION_MAX_PRICE_AGE_SECONDS` (900 s, **provisional**).

If any fill bar fails a check, iteration stops at that bar. A later bar is never used to skip over an ineligible one.

### What the owner must decide to enable fills
1. Which provider's data will be verified, and how: the verification procedure that justifies
   `price_basis = AS_TRADED_VERIFIED` with `basis_verified_by` evidence. If a quote/LTP source is preferred over
   bars, it needs a new `ExecutionPriceSource` implementation.
2. Whether the next-bar-open model (section 3.2) is an acceptable simulation assumption.
3. The nine cost rates (section 3.3), for the product (intraday or delivery) being simulated.

## 3. Architecture

```
Strategy / agent / API  ──OrderIntent──▶  OrderService (app/orders)
                                              │ 1. idempotency (client_order_id + fingerprint)
                                              │ 2. static validation (instrument, qty, lot, tick)
                                              │ 3. ExecutionPriceGate.check (fresh reference price)
                                              │ 4. RiskService.snapshot → risk.engine.evaluate (pure)
                                              │ 5. RiskDecision row (append-only)
                                              ▼
                                         MockExchange.submit
                                              │ independent re-validation; reserve cash;
                                              │ order rests PENDING (accepted_at = trading clock)
                                              ▼
           POST /exchange/process ──▶   MockExchange.process_open_orders
           (Phase 5 loop later)               │ fill bars after accepted_at → fill → ledger → position
                                              │ DAY expiry; then RiskService.refresh
                                              ▼
                       orders · fills · cash_ledger · positions · risk_state   (one transaction)
```

- **Pure modules** (no DB, no clock): `risk/engine.py`, `exchange/costs.py`, `exchange/accounting.py`.
- **Single transaction** per operation, under `SELECT … FOR UPDATE` on the user's `virtual_accounts` row.
  Services never commit; the endpoint commits once.
- **Strategies never touch balances.** Phase 3/5 code must call `OrderService.submit`.

### 3.1 Hybrid risk rules (`risk/engine.py`)
- User limits are hard limits. A strategy may only tighten `max_risk_per_trade_pct`
  (`OrderIntent.strategy_max_risk_pct`; the effective limit is the minimum of the two).
- An entry must carry a stop-loss below the entry price.
- Approved size = min(proposal, risk cap, allocation cap, exposure cap, cash cap, quantity cap), rounded down to
  the lot size. The cash cap includes every fee (`CostModel.max_buy_quantity`).
- If the approved size is smaller than the proposal, the order is `RESIZED` when `resize_allowed`, else `REJECTED`.
- Entries are rejected on drawdown or daily-loss breaches, the kill switch, missing settings or missing prices.
- Exits (SELL of a held long) are allowed under the kill switch and breached loss limits, because they reduce risk.
- The order's **`risk_budget`** = equity × effective risk % (the hard limit in money). The exchange caps fills
  with it (section 3.2).

### 3.2 Execution model: next bar open (documented simulation assumptions)
- **No fill at submission.** An accepted order is `PENDING`. `accepted_at` comes from the trading clock. Only
  finalized bars with `bar_start >= accepted_at` can fill it, so the bar a decision was based on never can.
- **MARKET**
  - Fills at the open of the **immediately next** bar (`bar_start` in `[accepted_at, accepted_at + timeframe)`),
    with adverse slippage. Immediate-or-cancel: any unfilled remainder is cancelled.
  - If that bar is missing (data gap), or the next bar is in a later session (overnight gap), the order is
    **cancelled** (`NEXT_BAR_UNAVAILABLE`) rather than filled at a later price.
- **LIMIT** (DAY validity)
  - Evaluated bar by bar, in time order.
  - BUY fills at `min(slipped open, limit)` when the bar opens at or below the limit. Otherwise it fills at the
    limit when the bar's low trades **strictly** below it (`BAR_LIMIT_CROSS`). Touching the limit is not a fill.
  - SELL is symmetric.
  - The order expires (`DAY_ORDER_EXPIRED`) when its session ends. It never fills on a later session's bar.
- **Gaps on entries**
  - A BUY whose fill price is at or below its stop is cancelled (`GAP_THROUGH_STOP`).
  - BUY fills are capped so that (fill price − stop) × quantity stays within `risk_budget`. The remainder is
    cancelled (`RISK_BUDGET`).
  - BUY fills are also capped by available cash at the fill price (`INSUFFICIENT_CASH_AT_FILL`).
- **Partial fills** happen only when `EXCHANGE_MAX_VOLUME_PARTICIPATION` is set. A bar fills an order at most
  once (`uq_fills_order_price_source_ref`).
- **Protective exits** (`OrderService.run_protective_exits`): triggered when the latest finalized bar's low
  reached the stop, or its high reached the take-profit. The exit is a MARKET SELL, so it fills at the **next
  bar's open**, including any gap. Gaps are never filled at the stop price.
- Cash account, **long-only**; short selling is rejected.

### 3.3 Cost model (`exchange/costs.py`; shared with backtesting and the benchmark)

```
brokerage  = min(BROKERAGE_BPS × turnover, BROKERAGE_MAX_PER_ORDER − brokerage already charged on the order)
stt        = turnover × STT_{BUY|SELL}_BPS
txn_charge = turnover × TXN_CHARGE_BPS
sebi_fee   = turnover × SEBI_FEE_BPS
stamp_duty = turnover × STAMP_DUTY_BUY_BPS            (BUY only)
gst        = (brokerage + txn_charge + sebi_fee) × GST_PCT / 100
slippage   = adverse SLIPPAGE_BPS on the reference price of the fill
```

- No rate has a default, because rates depend on product, broker and date. While any rate is unset, orders are
  rejected with `COSTS_NOT_CONFIGURED`, and `/exchange/status` lists the missing settings.
- Each fill stores `fee_breakdown` and a snapshot of the cost model.
- Rounding goes against the trader: prices are quantized to 6 dp (BUY up, SELL down), and each fee component is
  rounded up to 6 dp.
- `max_buy_quantity` returns a quantity whose worst-case total cost provably fits the cash. It is property-tested.

### 3.4 Risk-state refresh (`RiskService.refresh`)
- Re-values the account using eligible marks only.
- Updates `peak_equity`, the session's `day_start_equity`, `last_equity` and `last_equity_at`.
- Reports `equity`, `drawdown_pct`, `daily_loss_pct` and `breaches`.
- No order is needed. It runs on `GET /risk/state`, `POST /exchange/process`, kill-switch changes and every
  submission.

### 3.5 Accounting
- Cash = `initial_capital + SUM(cash_ledger.amount)`. The ledger is append-only, so there is no mutable balance.
  `initial_capital` is never modified (Phase 1 trigger).
- Average entry price is volume-weighted, excludes fees and is kept at 10 dp. Realized P&L is gross of fees;
  fees are tracked per position.
- Unrealized P&L and equity are computed only from gate-eligible marks. Otherwise they are reported as `null`
  with the reason, never estimated.

## 4. Database

**Migration `0004`** (additive; revises Phase 2's `0003`):

| Table | Purpose | Guards |
|---|---|---|
| `risk_settings` | user hard limits, no defaults | range checks, one row per user |
| `risk_state` | kill switch, peak / day-start / last equity | one row per user |
| `orders` | every order, including rejected ones | unique `(user_id, client_order_id)`; status/fill consistency checks; trigger: no delete, terminal states frozen, identity immutable, fills never shrink |
| `fills` | executions with full price provenance and cost snapshot | append-only; unique `(order_id, seq)`, `(order_id, price_source_ref)`; basis ≠ UNVERIFIED; observation ≤ execution time |
| `cash_ledger` | cash movements per fill | append-only; sign matches type; unique `(fill_id, entry_type)` |
| `positions` | long-only holdings and realized P&L | `quantity ≥ 0`; avg price iff open |
| `risk_decisions` | every risk evaluation (PRD 4.3) | append-only |

**Migration `0005`** (additive; revises `0004`):
- `orders.accepted_at`: NOT NULL. Existing rows are backfilled from `created_at`, with the guard trigger disabled
  for that single statement inside the migration transaction.
- `orders.risk_budget`: nullable, ≥ 0.
- `fills.fee_breakdown`: JSONB, default `{}`.
- `risk_state.last_equity` and `risk_state.last_equity_at`.
- The order guard trigger is replaced so that `accepted_at` and `risk_budget` are immutable too.

All Phase 4 tables are user-owned (`UserOwnedMixin`) and classified in `test_every_table_is_classified`.
Downgrades are **refused while data would be lost**: `0005` refuses while orders or fills exist; `0004` refuses
while any trading record exists. This is the same convention as `0003`.

## 5a. Integration with Phase 2 (reconciled)

| Phase 2 fact | Phase 4 adaptation |
|---|---|
| Migration `0003_market_data_foundation` | Phase 4 migrations are `0004` and `0005` |
| Restatements are non-canonical | Sources read `is_canonical = true` only (tested; mutation-checked) |
| Only final bars are archived | The `is_final` filter is kept as defence in depth |
| `instruments.trading_status`; futures forced DISABLED | Orders for a DISABLED instrument are rejected `INSTRUMENT_DISABLED` |
| `app.state.clock` | Phase 4 endpoints use the same clock |
| `MARKETDATA_STALE_AFTER_MINUTES` is descriptive only | Phase 4 keeps its own `EXECUTION_MAX_PRICE_AGE_SECONDS` |
| `timeframe_minutes` (calendar) | Reused for the next-bar window; the execution timeframe must be intraday |
| Decision 1: execution-price source deferred to Phase 4 | Still unapproved; no candle is relabelled |

Phase 2-owned tests changed in PR #1 (minimal, intent-preserving): `test_market_schema.py` (no hard-coded head
revision) and `test_phase2_policies.py` (the no-execution-code guard allows the Phase 4 packages; later-phase
packages and execution code in `app/marketdata` stay forbidden).

## 5. Shared contracts

**Market data (Phase 2 or a future provider)**
- Implement `ExecutionPriceSource`, or keep writing `candles` rows that `CandleBarSource` can read.
- Phase 4 **reads** `candles` and `data_providers`; it never writes them, and it adds no market-data config keys.

**Phase 3/5 (strategies, agent)**
- Submit with `OrderService(TradingContext.build(db, settings, clock)).submit(user_id, OrderIntent(...))`.
  Use `source=OrderSource.AGENT`, a stable `strategy_id`, a deterministic `client_order_id`, a `stop_loss_price`
  for entries and, optionally, `strategy_max_risk_pct`.
- On every cycle, call `ctx.exchange.process_open_orders(account)`, then `ctx.risk.refresh(account)` and
  `OrderService.run_protective_exits(user_id)`. Call `refresh` at session start. Phase 4 schedules nothing.
- Backtesting (Phase 6) must use `app.exchange.costs.CostModel` and the same next-bar-open semantics, so that
  paper trading and backtests agree.

### Exact signatures (as implemented)

```python
# app/exchange/pricing.py
@dataclass(frozen=True)
class ExecutionQuote:            # reference price (latest finalized bar close); never a fill price
    instrument_id: int
    price: Decimal
    as_of: datetime              # tz-aware UTC (bar end)
    observed_at: datetime
    provider_code: str
    price_basis: PriceBasis
    is_final: bool
    kind: QuoteKind              # LATEST_BAR_CLOSE
    source_ref: str              # e.g. "candle:123"
    volume: int | None = None
    low: Decimal | None = None   # bar range for protective-exit triggers
    high: Decimal | None = None

@dataclass(frozen=True)
class ExecutionBar:              # a bar an order may fill against
    instrument_id: int
    bar_start: datetime
    bar_end: datetime
    session_date: date
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int
    observed_at: datetime
    provider_code: str
    price_basis: PriceBasis
    is_final: bool
    source_ref: str

class ExecutionPriceSource(Protocol):
    name: str
    enabled: bool
    def latest(self, instrument_id: int) -> ExecutionQuote | None: ...
    def bars_from(self, instrument_id: int, start: datetime) -> list[ExecutionBar]: ...   # oldest first

# CandleBarSource reads (read-only): candles(instrument_id, provider_id, timeframe, bar_start_ts, bar_end_ts,
#   session_date, open, high, low, close, volume, is_final, is_canonical, observed_at, price_basis, id),
#   data_providers(id, code). Filter: one approved provider, EXECUTION_CANDLE_TIMEFRAME, is_final, is_canonical,
#   bar_end_ts <= now, observed_at <= now.

# app/exchange/types.py
@dataclass(frozen=True)
class OrderIntent:
    client_order_id: str              # 1-64 chars, deterministic per decision (idempotency key)
    instrument_id: int
    side: OrderSide                   # BUY | SELL (SELL only reduces a held long)
    order_type: OrderType             # MARKET | LIMIT (DAY)
    quantity: int                     # > 0, proposed; risk may resize
    source: OrderSource               # AGENT for Phase 5
    limit_price: Decimal | None = None
    stop_loss_price: Decimal | None = None      # required for BUY
    take_profit_price: Decimal | None = None
    strategy_id: str | None = None
    strategy_max_risk_pct: Decimal | None = None  # can only tighten the user limit

# app/orders/service.py and friends
ctx = TradingContext.build(db: Session, settings: Settings, clock: Callable[[], datetime])
OrderService(ctx).submit(user_id, intent) -> SubmitResult(order, replayed, decision)   # order is PENDING or REJECTED
OrderService(ctx).run_protective_exits(user_id) -> list[SubmitResult]
ctx.exchange.process_open_orders(account) -> list[Order]       # orders that changed
ctx.risk.refresh(account) -> RiskStatus(state, equity, drawdown_pct, daily_loss_pct, breaches)
CostModel.from_settings(settings) -> CostModel | None           # None while any rate is unset
# Errors: IdempotencyConflict, InvalidOrder. Services flush, never commit; the caller commits once or rolls back.
```

## 6. Known limitations

- **Fills cannot happen in any real configuration yet** (D4). All fill behaviour is verified with synthetic
  fixture bars and synthetic cost rates, both labelled as fixtures.
- The next-bar-open model is a simulation assumption. It does not model queue position, intrabar sequencing
  beyond open/low/high, or auctions.
- The day-start reference is the equity at the first refresh on a session date. Phase 5 must refresh at session
  start; until then, `last_equity_at` shows how current the references are.
- Per-trade risk excludes fees. Exposure uses reserved cash (fees included) for open BUYs.
- Tick-size validation applies only when `instruments.tick_size` is known (never invented). Lot size is enforced
  only when known.
- No holiday calendar check (Phase 2 decision 4). Session boundaries use the exchange-local date of bars and the clock.
- No short selling, margin, intraday square-off or corporate-action handling of open positions.
- `/api/v1/orders` and `/api/v1/exchange/process` are user-triggered. Operator-only restrictions for hosted
  deployment are not implemented (same posture as Phases 1 and 2).
