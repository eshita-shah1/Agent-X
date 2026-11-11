# Phase 4 — Risk Engine and Mock Exchange

Status: **implemented and tested, NOT approved; held pending review.** Rebased onto Phase 2 (`main` @ 8cc2a8f)
on branch `phase-4-risk-exchange`, submitted as a pull request for review. Simulated fills are **blocked by default** until
decision D4 is made (below).

## 0. Review status (2026-11-11)

| Item | Outcome |
|---|---|
| D4: execution-price source | **Not approved.** Yahoo Finance remains unapproved. A verified provider with quotes/trades is preferred. |
| Fee and slippage assumptions | Remain mandatory configuration. A documented Indian-equity cost model (brokerage, exchange charges, taxes, slippage), shared by backtesting, benchmarking and paper trading, is to be agreed. |
| Approve and commit Phase 4 | **On hold** pending review and integration with Phase 2. |

Review findings accepted against this implementation:
1. **Same-bar fill risk.** `CandleCloseExecutionPriceSource` fills at the close of the latest
   finalized bar. That is the bar a strategy would use for its signal, so filling at its close
   is optimistic and is a same-bar lookahead risk. If bar-based execution is ever approved, the
   proposed model is: **fill at the open of the next finalized bar that starts after the order
   was accepted**, with documented slippage and gap handling. This is a simulation assumption,
   not evidence of a real fill. Not implemented, pending approval.
2. **Contract reconciliation: done** (section 5a). Phase 4 was first built against the Phase 1 `candles`
   table; it has since been rebased onto Phase 2 and adapted to its schema and decisions.
3. **Cost model structure.** The current generic model (fixed fee per order plus side-specific bps
   and slippage bps) cannot represent every Indian-equity charge exactly. GST, for example, is
   levied on brokerage + exchange + SEBI charges. A component model is to be agreed.
4. **Risk-state staleness.** Day-start and peak equity update only on order submission. This is
   acceptable for now, but it must be fixed before autonomous monitoring (Phase 5): risk metrics
   must refresh without a new order.
5. Synthetic fixture tests prove the execution logic under test conditions only, not the
   suitability of any real provider's data.
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
- Phase 2 research (recorded in the master PRD, section 7.1): Yahoo `Close` is split-adjusted
  even with `auto_adjust=False`; forming candles are returned; no guaranteed fixed delay;
  float32 noise; inconsistent timezones.
- Repository: every `candles` row defaults to `price_basis = UNVERIFIED_PROVIDER_NATIVE`. A
  check constraint forbids a verified label without `basis_verified_by` evidence. No code in
  this repository ingests or verifies provider data.
- Therefore **no verified execution-price source exists**.

### Decision implemented
- Execution prices come only through the `ExecutionPriceSource` contract and must pass the
  `ExecutionPriceGate` (`backend/app/exchange/pricing.py`).
- Default configuration enables **no source** and approves **no basis and no provider**, so
  every order is rejected with `NO_ELIGIBLE_PRICE: SOURCE_DISABLED` and nothing is filled.
- `UNVERIFIED_PROVIDER_NATIVE` can never be approved. This is enforced three ways: the
  configuration validator refuses it, the gate rejects it unconditionally, and a database
  check constraint (`ck_fills_price_basis_verified`) refuses to store such a fill.
- Yahoo data has **not** been relabelled. Historical closes are never used as live prices:
  the only source reads the latest *finalized*, *fresh* bar for *one* approved provider.

### Acceptance requirements for any execution price (gate)
1. A source is explicitly enabled.
2. The instrument matches.
3. Timestamps are timezone-aware.
4. The price is positive.
5. The observation is final (no forming candles).
6. It is not later than the decision time (`as_of` and `observed_at` ≤ now); no lookahead.
7. It is fresh: age ≤ `EXECUTION_MAX_PRICE_AGE_SECONDS` (900 s, **provisional**).
8. The basis is verified **and** listed in `EXECUTION_APPROVED_PRICE_BASES`.
9. The provider is listed in `EXECUTION_APPROVED_PROVIDERS` (exactly one for `candle_close`).

### What the owner must decide to enable fills
1. Which provider's data will be verified, and how (the verification procedure that justifies
   setting `price_basis = AS_TRADED_VERIFIED` with `basis_verified_by` evidence).
2. Whether "close of the latest finalized 5-minute bar" is an acceptable execution proxy, or
   whether a quote/LTP source is required (a new `ExecutionPriceSource` implementation).
3. Fee and slippage assumptions (the four `EXCHANGE_*` cost settings).

## 3. Architecture

```
Strategy / agent / API  ──OrderIntent──▶  OrderService (app/orders)
                                              │ 1. idempotency (client_order_id + fingerprint)
                                              │ 2. static validation (instrument, qty, lot, tick)
                                              │ 3. ExecutionPriceGate.check(instrument)
                                              │ 4. RiskService.snapshot  → risk.engine.evaluate (pure)
                                              │ 5. RiskDecision row (append-only)
                                              ▼
                                         MockExchange (app/exchange)
                                              │ independent re-validation: instrument, costs,
                                              │ kill switch, price gate, held qty, cash
                                              │ reserve cash → fill → ledger → position
                                              ▼
                       orders · fills · cash_ledger · positions   (one transaction)
```

- **Pure modules** (no DB, no clock): `risk/engine.py`, `exchange/costs.py`, `exchange/accounting.py`.
- **Single transaction** per operation under `SELECT … FOR UPDATE` on the user's
  `virtual_accounts` row; services never commit, the endpoint commits once.
- **Strategies never touch balances.** Phase 3/5 code must call `OrderService.submit`.

### Hybrid risk rules (`risk/engine.py`)
User limits are hard limits; a strategy may only tighten `max_risk_per_trade_pct`
(`OrderIntent.strategy_max_risk_pct`; effective = min). An entry must carry a stop-loss
below the entry price. Approved size = min(proposal, risk cap, allocation cap, exposure cap,
cash cap, quantity cap), rounded down to the lot size. If that is smaller than the proposal:
`RESIZED` when `resize_allowed`, else `REJECTED`. Drawdown and daily-loss breaches, the kill
switch, missing settings and missing prices reject entries. Exits (SELL of a held long) are
allowed under the kill switch and breached loss limits because they reduce risk.

### Execution semantics (documented assumptions)
- Cash account, **long-only**; short selling is rejected.
- MARKET = immediate-or-cancel against the current eligible observation.
- LIMIT fills when marketable, at the slipped price capped by the limit; otherwise it rests
  until `MockExchange.process_open_orders` (not scheduled; Phase 5 decides when to call it).
- Partial fills only when `EXCHANGE_MAX_VOLUME_PARTICIPATION` is set; one observation fills an
  order at most once (`uq_fills_order_price_source_ref`).
- Costs: fixed fee once per order (on its first fill) + side-specific bps of turnover; adverse
  slippage in bps. Prices rounded against the trader to 6 dp; fees rounded up to 6 dp.
- No execution delay is modelled.
- Protective exits: `OrderService.run_protective_exits` sells positions whose eligible price
  crossed the stop or take-profit, at the market observation (gaps are not filled at the stop).

### Accounting
- Cash = `initial_capital + SUM(cash_ledger.amount)`. The ledger is append-only, so there is
  no mutable balance. `initial_capital` is never modified (Phase 1 trigger).
- Average entry price is volume-weighted, excludes fees, and is kept at 10 dp. Realized P&L
  is gross of fees; fees are tracked per position.
- Unrealized P&L and equity are computed only from gate-eligible marks; otherwise they are
  reported as `null` with the reason, never estimated.

## 4. Database (migration `0004`, additive only; revises Phase 2's `0003`)

| Table | Purpose | Guards |
|---|---|---|
| `risk_settings` | user hard limits, no defaults | range checks, one row per user |
| `risk_state` | kill switch, peak equity, day-start equity | one row per user |
| `orders` | every order including rejected ones | unique `(user_id, client_order_id)`; status/fill consistency checks; trigger: no delete, terminal states frozen, identity immutable, fills never shrink |
| `fills` | executions with full price provenance + cost snapshot | append-only; unique `(order_id, seq)`, `(order_id, price_source_ref)`; basis ≠ UNVERIFIED; observation ≤ execution time |
| `cash_ledger` | cash movements per fill | append-only; sign matches type; unique `(fill_id, entry_type)` |
| `positions` | long-only holdings + realized P&L | `quantity ≥ 0`; avg price iff open |
| `risk_decisions` | every risk evaluation (PRD 4.3) | append-only |

All are user-owned (`UserOwnedMixin`) and registered in `test_every_table_is_classified`.
No existing table or migration was modified. Like `0003`, downgrading `0004` is **refused while any
trading record exists** (orders, fills, ledger, positions, risk settings/state/decisions).

## 5a. Integration with Phase 2 (reconciled)

| Phase 2 fact | Phase 4 adaptation |
|---|---|
| Migration `0003_market_data_foundation` | Phase 4 migration renumbered to `0004` (revises `0003`) |
| `candles` versioning: first observation canonical, restatements non-canonical | Execution source reads `is_canonical = true` only; a restated value is never an execution price (tested; mutation-checked) |
| Only final bars are archived (`ck_candles_only_finalized_archived`) | Kept the `is_final` filter as defence in depth; forming-candle cases now covered by the DB constraint + gate unit tests |
| Candles require provider symbol + ingestion batch | Test fixtures create a fixture job/batch |
| `instruments.trading_status` / `disabled_reason`; futures forced DISABLED | Orders for a DISABLED instrument are rejected `INSTRUMENT_DISABLED: <reason>` (pipeline and exchange) |
| `_enum` helper renamed `enum_type` | Phase 4 models use `enum_type` |
| `app.state.clock` set by `create_app` | Phase 4 endpoints use the same `app.state.clock` |
| `MARKETDATA_STALE_AFTER_MINUTES` is *descriptive only*; trading use needs separate review | Phase 4 keeps its own gate threshold `EXECUTION_MAX_PRICE_AGE_SECONDS` (provisional 900 s); it does not reuse the descriptive Phase 2 setting |
| Decision 1: execution-price source deferred to Phase 4 | Still unapproved; no candle is relabelled; no application code assigns a verified basis (Phase 2 policy test still passes) |

Phase 2-owned tests changed (minimal, intent-preserving):
- `tests/integration/test_market_schema.py`: the refused-downgrade test now asserts the revision is *unchanged*
  instead of hard-coding `"0003"` (head is now `0004`).
- `tests/marketdata/test_phase2_policies.py`: the "no execution code exists" guard is narrowed. Phase 4's
  `exchange/`, `orders/`, `risk/` packages are allowed; later-phase packages (`strategies`, `agent`, `backtest`,
  `execution`, `mock_exchange`) stay forbidden, and `app/marketdata` must still contain no order/execution code.

## 5. Shared contracts for the other Claude instance

**Phase 2 (market data)**: please review before merging.
- To feed execution, implement `ExecutionPriceSource` (`latest(instrument_id) -> ExecutionQuote | None`)
  or keep writing `candles` rows that `CandleCloseExecutionPriceSource` can read
  (`is_final`, UTC `bar_end_ts`, `observed_at`, `price_basis`, provider via `data_providers.code`).
- Phase 4 adds **no** files under `app/marketdata/` and no market-data config keys. Its keys
  are prefixed `EXECUTION_*` / `EXCHANGE_*` to avoid collisions with Phase 2's
  finalization/stale/failure settings.
- Phase 4 **reads** `candles` and `data_providers`; it never writes them.
- The source filters canonical rows (Phase 2 restatement policy); see section 5a.

**Phase 3/5 (strategies, agent)**
- Submit with `OrderService(TradingContext.build(db, settings, clock)).submit(user_id, OrderIntent(...))`
  using `source=OrderSource.AGENT`, a stable `strategy_id`, a deterministic `client_order_id`
  (idempotency), a `stop_loss_price` for entries, and optionally `strategy_max_risk_pct`.
- Call `MockExchange.process_open_orders` and `OrderService.run_protective_exits` from the
  orchestration loop. Phase 4 schedules nothing.

### Exact signatures (as implemented)

```python
# app/exchange/pricing.py — what Phase 2 provides (or satisfies via `candles` rows)
@dataclass(frozen=True)
class ExecutionQuote:
    instrument_id: int
    price: Decimal            # never float
    as_of: datetime           # tz-aware UTC data time (bar end for a candle)
    observed_at: datetime     # tz-aware UTC time Agent-X received it
    provider_code: str        # data_providers.code
    price_basis: PriceBasis   # must be verified + approved to execute
    is_final: bool
    kind: QuoteKind           # currently only CANDLE_CLOSE
    source_ref: str           # stable observation id, e.g. "candle:123" (dedupes fills)
    volume: int | None = None

class ExecutionPriceSource(Protocol):
    name: str
    enabled: bool
    def latest(self, instrument_id: int) -> ExecutionQuote | None: ...   # must not fabricate

# Columns CandleCloseExecutionPriceSource reads (read-only):
#   candles: instrument_id, provider_id, timeframe, bar_start_ts, bar_end_ts, close, volume,
#            is_final, is_canonical, observed_at, price_basis, id
#   data_providers: id, code
# Filter: one approved provider, timeframe = EXECUTION_CANDLE_TIMEFRAME, is_final, is_canonical,
#         bar_end_ts <= now, observed_at <= now; latest by bar_start_ts.

# app/exchange/types.py — what Phase 3/5 submit
@dataclass(frozen=True)
class OrderIntent:
    client_order_id: str              # 1-64 chars, deterministic per decision (idempotency key)
    instrument_id: int
    side: OrderSide                   # BUY | SELL (SELL only reduces a held long)
    order_type: OrderType             # MARKET | LIMIT
    quantity: int                     # > 0, proposed; risk may resize
    source: OrderSource               # AGENT for Phase 5
    limit_price: Decimal | None = None
    stop_loss_price: Decimal | None = None      # required for BUY
    take_profit_price: Decimal | None = None
    strategy_id: str | None = None
    strategy_max_risk_pct: Decimal | None = None  # strategy rule; can only tighten user limit

# app/orders/service.py — the only order entry point
ctx = TradingContext.build(db: Session, settings: Settings, clock: Callable[[], datetime])
OrderService(ctx).submit(user_id: UUID, intent: OrderIntent) -> SubmitResult(order, replayed, decision)
OrderService(ctx).run_protective_exits(user_id: UUID) -> list[SubmitResult]
ctx.exchange.process_open_orders(account: VirtualAccount) -> list[Order]
# Errors: IdempotencyConflict (same id, different request), InvalidOrder (static validation).
# Transactions: services flush, never commit; the caller commits once or rolls back.
```

**Shared-file edits made by Phase 4** (all additive): `app/core/config.py` (new settings),
`app/db/enums.py` (new enums), `app/db/models/__init__.py` (exports), `app/api/v1/router.py`
(one `include_router`), `tests/integration/test_isolation.py` (table classification).

## 6. Known limitations

- **Fills cannot happen in any real configuration yet** (D4). All fill behaviour is verified
  with synthetic fixture candles explicitly labelled as test fixtures.
- The candle-close proxy is unvalidated as an execution price; it is gated off by default.
- The day-start and peak equity references only update when equity is evaluated (at
  order submission). Without a scheduler they can understate moves between evaluations.
- Risk per trade excludes fees; exposure uses reserved cash (incl. fees) for open BUYs.
- Tick-size validation applies only when `instruments.tick_size` is known (it is never
  invented). Lot size is enforced only when known.
- No market-hours or holiday calendar check. Orders outside sessions are blocked indirectly,
  because data is stale.
- No short selling, margin, intraday square-off, or corporate-action handling of open positions.
- `/api/v1/orders` is user-triggered (manual). Operator-only restrictions for hosted
  deployment are not implemented (same posture as Phase 1).
- Docker was verified on the development Mac only (before the Phase 2 rebase; re-verified after, see the PR).
