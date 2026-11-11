"""The Mock Exchange: order acceptance, independent validation, simulated fills and accounting.

The exchange is deliberately independent of the risk engine. It re-validates everything that
matters for execution (instrument, quantity, kill switch, cash, held quantity, price
eligibility) and does not trust that an upstream component already did. It never decides
*what* to trade and never creates a price: every fill uses a bar that passed the
`ExecutionPriceGate` and records its provenance.

Execution model: **next bar open** (documented simulation assumption, not evidence of a real fill)
  * An order is accepted at `accepted_at` (trading clock). It never fills at submission. It can only
    fill against finalized bars that *start at or after* `accepted_at`; the bar a decision was based on
    can never fill it (no same-bar lookahead).
  * MARKET: fills at the open of the **immediately next** bar (the first bar starting in
    [accepted_at, accepted_at + timeframe)), with adverse slippage. Immediate-or-cancel: any unfilled
    remainder is cancelled. If the next bar is missing (data gap) or belongs to a later session
    (overnight gap), the order is cancelled rather than filled at a later price.
  * LIMIT (DAY validity): evaluated bar by bar in time order. BUY fills at min(slipped open, limit) when
    the bar opens at or below the limit; otherwise at the limit when the bar's low trades strictly
    below it. SELL is symmetric (open >= limit, or high strictly above). It expires at the end of the
    session in which it was accepted.
  * Gaps on entries: a BUY whose fill price would be at or below its stop-loss is cancelled
    (GAP_THROUGH_STOP). BUY fills are capped so the order's realized risk (fill price - stop) never
    exceeds the risk budget approved by the risk engine, and by available cash at the fill price.
  * Partial fills occur only when EXCHANGE_MAX_VOLUME_PARTICIPATION is configured. One bar fills a
    given order at most once.
  * Cash account, long-only: SELL only reduces an existing long position; short selling is rejected.
  * Execution policy: an order records the policy in force when it was accepted (`verified` or `simulation`); it is
    only ever filled under that same policy (otherwise cancelled EXECUTION_MODE_CHANGED). Simulation fills carry
    `SIMULATION_PROVIDER_NATIVE` and the bar's UNVERIFIED_PROVIDER_NATIVE basis.
  * Missing data: bars an order is evaluated against must be contiguous, starting with the bar immediately after
    acceptance; a missing bar cancels the order (DATA_GAP) instead of skipping to a later price.
  * Corporate actions: a position held across a split/bonus/other ex-date is on an unadjusted basis; orders and fills
    in that instrument are refused (CORPORATE_ACTION_UNADJUSTED_POSITION) until an APPLIED corporate-action
    adjustment exists for it (app/exchange/corporate_actions.py).
  * Time: three timestamps are kept apart on every fill: `price_as_of` (the simulated fill time), `price_observed_at`
    (when the finalized candle became available to Agent-X) and `executed_at` (when the simulator booked it).
    Position open dates and realized-P&L attribution use the simulated fill time; processing is ordered by simulated
    time across all of an account's orders, so a late run books fills in the order they would have happened.
  * Costs: the rates in force on the fill's simulated session date, for the order's product, from the effective-dated
    cost schedule (app/exchange/costs.py). Only DELIVERY orders are executed.

Concurrency: every mutating operation first locks the user's `virtual_accounts` row
(SELECT ... FOR UPDATE), so a user's orders, fills and ledger entries are serialized.
Callers own the transaction: nothing here commits, so any failure rolls everything back.
"""

import logging
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_EVEN, Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.clock import Clock
from app.db.enums import (
    OPEN_ORDER_STATUSES,
    ExecutionPolicy,
    InstrumentType,
    LedgerEntryType,
    OrderSide,
    OrderStatus,
    OrderType,
    Product,
    TradingStatus,
)
from app.db.models import (
    CashLedgerEntry,
    Fill,
    Instrument,
    Order,
    Position,
    RiskState,
    VirtualAccount,
)
from app.db.scoping import owned_select
from app.exchange.accounting import AVG_QUANTUM, PositionState, apply_buy, apply_sell
from app.exchange.costs import (
    MONEY_QUANTUM,
    ChargeType,
    CostModel,
    CostSchedule,
    CostScheduleError,
    FeeContext,
    RoundingStage,
    breakdown_component,
)
from app.exchange.portfolio import PortfolioReader, unadjusted_action
from app.exchange.pricing import ExecutionBar, ExecutionPriceGate, FillKind, scale_changing_actions_between
from app.exchange.types import OrderIntent

log = logging.getLogger("agentx.exchange")

# NSE trading dates are Indian calendar dates (a fact about the exchange, not a parameter).
EXCHANGE_TZ = ZoneInfo("Asia/Kolkata")


def session_date_of(ts: datetime) -> date:
    return ts.astimezone(EXCHANGE_TZ).date()


def session_bounds(d: date) -> tuple[datetime, datetime]:
    """UTC instants bounding an exchange-local calendar date."""
    start = datetime.combine(d, time(0), EXCHANGE_TZ)
    return start.astimezone(UTC), (start + timedelta(days=1)).astimezone(UTC)


class ExchangeInvariantError(RuntimeError):
    """An accounting invariant would be violated. Raised (never swallowed) so the whole
    transaction rolls back instead of persisting inconsistent state."""


class OrderNotCancellable(ValueError):
    pass


class OrderNotFound(LookupError):
    pass


def lock_account(db: Session, user_id: uuid.UUID) -> VirtualAccount:
    account = db.scalar(owned_select(VirtualAccount, user_id).with_for_update())
    if account is None:
        raise LookupError("virtual account not found")
    return account


def instrument_problem(instrument: Instrument | None, quantity: int, limit_price: Decimal | None) -> str | None:
    """Static order validation shared by the pipeline and the exchange. None means valid."""
    if instrument is None:
        return "UNKNOWN_INSTRUMENT"
    if not instrument.is_active:
        return "INSTRUMENT_INACTIVE"
    if instrument.trading_status is not TradingStatus.ENABLED:
        return f"INSTRUMENT_DISABLED: {instrument.disabled_reason}"
    if instrument.instrument_type is not InstrumentType.EQUITY:
        return "INSTRUMENT_TYPE_NOT_SUPPORTED: only equities are enabled"
    if quantity <= 0:
        return "INVALID_QUANTITY"
    if instrument.lot_size and quantity % instrument.lot_size:
        return f"INVALID_QUANTITY: not a multiple of lot size {instrument.lot_size}"
    if limit_price is not None and instrument.tick_size and limit_price % instrument.tick_size:
        return f"INVALID_PRICE: limit price not a multiple of tick size {instrument.tick_size}"
    return None


@dataclass(frozen=True)
class _Fill:
    price: Decimal
    reference_price: Decimal
    as_of: datetime  # simulated fill time
    kind: FillKind


@dataclass
class _Stream:
    """Bars still to evaluate for one order during a processing run."""

    order: Order
    bars: deque
    blocked_by: object
    detail: str
    expected_start: int  # epoch seconds of the next bar the order may be evaluated on
    accepted_session: date = field(default=None)  # type: ignore[assignment]


class MockExchange:
    def __init__(
        self, db: Session, gate: ExecutionPriceGate, cost_schedule: CostSchedule | None, clock: Clock,
        timeframe_minutes: int = 5, execution_policy: ExecutionPolicy | None = None,
        max_volume_participation: Decimal | None = None,
    ):
        self.db = db
        self.gate = gate
        self.cost_schedule = cost_schedule
        self.clock = clock
        self.timeframe_minutes = timeframe_minutes
        self.execution_policy = execution_policy  # None while execution is disabled
        self.max_volume_participation = max_volume_participation
        self.portfolio = PortfolioReader(db, gate)
        self._cost_cache: dict[tuple[Product, date], CostModel] = {}

    def costs_for(self, product: Product, on_date: date) -> CostModel:
        """Rates in force on `on_date` for `product`. Raises CostScheduleError with the missing rates."""
        if self.cost_schedule is None:
            raise CostScheduleError("no cost schedule is configured (EXCHANGE_COST_SCHEDULE_PATH)")
        key = (product, on_date)
        if key not in self._cost_cache:
            self._cost_cache[key] = self.cost_schedule.resolve(product, on_date, self.max_volume_participation)
        return self._cost_cache[key]

    # ---- order acceptance -------------------------------------------------------------

    def record_rejected(
        self, account: VirtualAccount, intent: OrderIntent, fingerprint: str, reason: str,
        decision_price_ref: str | None = None,
    ) -> Order:
        """Persist an order that was rejected upstream (e.g. by risk), for a complete history."""
        lock_account(self.db, account.user_id)
        order = self._new_order(
            account, intent, fingerprint, quantity=0, risk_budget=None, decision_price_ref=decision_price_ref
        )
        order.status = OrderStatus.REJECTED
        order.reject_reason = reason
        self.db.add(order)
        self.db.flush()
        log.info("order rejected upstream", extra={"fields": self._log_fields(order)})
        return order

    def submit(
        self,
        account: VirtualAccount,
        intent: OrderIntent,
        fingerprint: str,
        quantity: int,
        risk_budget: Decimal | None = None,
        decision_price_ref: str | None = None,
    ) -> Order:
        """Accept an order for `quantity` (already sized by risk), validate it independently and reserve cash.
        The order rests as PENDING; it can only fill against later bars (see `process_open_orders`)."""
        lock_account(self.db, account.user_id)
        order = self._new_order(
            account, intent, fingerprint, quantity=quantity, risk_budget=risk_budget,
            decision_price_ref=decision_price_ref,
        )
        order.status = OrderStatus.PENDING
        self.db.add(order)
        self.db.flush()

        problem = instrument_problem(self.db.get(Instrument, order.instrument_id), quantity, order.limit_price)
        if problem:
            return self._reject(order, problem)
        if order.product is not Product.DELIVERY:
            return self._reject(order, f"PRODUCT_NOT_SUPPORTED: {order.product.value} (intraday square-off is not modelled)")
        try:
            costs = self.costs_for(order.product, session_date_of(self.clock()))
        except CostScheduleError as e:
            return self._reject(order, f"COSTS_NOT_CONFIGURED: {e}")
        if order.side is OrderSide.BUY and self._kill_switch_engaged(account.user_id):
            return self._reject(order, "KILL_SWITCH_ENGAGED")
        if order.side is OrderSide.BUY and order.stop_loss_price is None:
            return self._reject(order, "STOP_LOSS_REQUIRED: entries must carry a stop-loss")

        # Stale or unapproved data blocks new orders (the reference price must be eligible and fresh).
        check = self.gate.check(order.instrument_id)
        if not check.ok:
            return self._reject(order, f"EXECUTION_PRICE_UNAVAILABLE: {check.rejection.value}: {check.detail}")
        if self.execution_policy is None:
            return self._reject(order, "EXECUTION_DISABLED: no execution policy is active")
        ca = self.position_corporate_action_problem(account.user_id, order.instrument_id, session_date_of(self.clock()))
        if ca:
            return self._reject(order, ca)

        if order.side is OrderSide.SELL:
            held = self._sellable_quantity(account.user_id, order.instrument_id, exclude_order_id=order.id)
            if quantity > held:
                return self._reject(
                    order, f"INSUFFICIENT_POSITION: requested {quantity}, sellable {held} (short selling not supported)"
                )
        else:
            assert check.quote is not None
            needed = self._reservation(order, check.quote.price, costs)
            available = self.portfolio.available_cash(account, exclude_order_id=order.id)
            if needed > available:
                return self._reject(
                    order, f"INSUFFICIENT_CASH: requires up to {self._money(needed)}, available {self._money(available)}"
                )
            order.reserved_cash = needed
        self.db.flush()
        log.info("order accepted", extra={"fields": self._log_fields(order)})
        return order

    # ---- lifecycle ----------------------------------------------------------------------

    def cancel(self, account: VirtualAccount, order_id: uuid.UUID, reason: str) -> Order:
        lock_account(self.db, account.user_id)
        order = self.db.scalar(owned_select(Order, account.user_id).where(Order.id == order_id).with_for_update())
        if order is None:
            raise OrderNotFound(str(order_id))
        if order.status not in OPEN_ORDER_STATUSES:
            raise OrderNotCancellable(f"order is {order.status.value}")
        self._close_open_order(order, reason)
        self.db.flush()
        log.info("order cancelled", extra={"fields": self._log_fields(order)})
        return order

    def cancel_all_open(self, account: VirtualAccount, reason: str) -> list[Order]:
        lock_account(self.db, account.user_id)
        orders = list(
            self.db.scalars(
                owned_select(Order, account.user_id).where(Order.status.in_(OPEN_ORDER_STATUSES)).with_for_update()
            )
        )
        for order in orders:
            self._close_open_order(order, reason)
        self.db.flush()
        return orders

    def process_open_orders(self, account: VirtualAccount) -> list[Order]:
        """Execute resting orders against the bars that have finalized since they were accepted, then expire DAY
        orders whose session has ended. Returns the orders that changed.

        Bars are applied in **simulated time order across all of the account's orders** (ties: acceptance time,
        creation time, id), so a late or catch-up run books fills in the order they would have happened and average
        prices and realized P&L do not depend on when processing ran."""
        lock_account(self.db, account.user_id)
        orders = self.db.scalars(
            owned_select(Order, account.user_id)
            .where(Order.status.in_(OPEN_ORDER_STATUSES))
            .order_by(Order.accepted_at, Order.created_at, Order.id)
            .with_for_update()
        ).all()
        before = {o.id: (o.status, o.filled_quantity) for o in orders}
        step = self.timeframe_minutes * 60
        streams: list[_Stream] = []
        for order in orders:
            if order.execution_policy is None or order.execution_policy is not self.execution_policy:
                # Checked before anything else: disabling or switching the execution mode must never leave an order
                # to be filled later under a policy it was not accepted with.
                self._close_open_order(
                    order,
                    "EXECUTION_MODE_CHANGED: accepted under "
                    f"{order.execution_policy.value if order.execution_policy else None}, active policy is "
                    f"{self.execution_policy.value if self.execution_policy else None}",
                )
                continue
            if self.cost_schedule is None:
                continue
            bc = self.gate.fill_bars(order.instrument_id, order.accepted_at)
            streams.append(
                _Stream(
                    order=order, bars=deque(bc.bars), blocked_by=bc.blocked_by, detail=bc.detail,
                    # first bar boundary at or after acceptance (bars are aligned to the 09:15 IST session grid)
                    expected_start=-(-int(order.accepted_at.timestamp()) // step) * step,
                    accepted_session=session_date_of(order.accepted_at),
                )
            )
        while True:
            live = [st for st in streams if st.order.status in OPEN_ORDER_STATUSES and st.bars]
            if not live:
                break
            st = min(
                live,
                key=lambda x: (x.bars[0].bar_start, x.order.accepted_at, x.order.created_at, str(x.order.id)),
            )
            self._step(account, st, st.bars.popleft())
        for st in streams:
            if st.order.status in OPEN_ORDER_STATUSES and not st.bars and st.blocked_by is not None:
                # The next bar exists but failed validation (e.g. zero volume, data-quality issue, ambiguous basis).
                # Later bars are never used to skip over it, so the order cannot be evaluated: cancel with the reason.
                self._close_open_order(st.order, f"FILL_BAR_INELIGIBLE: {st.blocked_by.value}: {st.detail}")
        for order in orders:
            if order.status in OPEN_ORDER_STATUSES and session_date_of(self.clock()) > session_date_of(order.accepted_at):
                reason = (
                    "DAY_ORDER_EXPIRED: session ended"
                    if order.order_type is OrderType.LIMIT
                    else "NEXT_BAR_UNAVAILABLE: no eligible bar in the order's session"
                )
                self._close_open_order(order, reason)
        self.db.flush()
        return [o for o in orders if (o.status, o.filled_quantity) != before[o.id]]

    # ---- execution ------------------------------------------------------------------------

    def _step(self, account: VirtualAccount, st: _Stream, bar: ExecutionBar) -> None:
        """Evaluate one order on one bar."""
        order = st.order
        if bar.session_date == st.accepted_session and int(bar.bar_start.timestamp()) != st.expected_start:
            self._close_open_order(
                order,
                f"DATA_GAP: expected the bar starting {datetime.fromtimestamp(st.expected_start, UTC).isoformat()}, "
                f"next archived bar starts {bar.bar_start.isoformat()}",
            )
            return
        st.expected_start = int(bar.bar_end.timestamp())
        ca = self.position_corporate_action_problem(account.user_id, order.instrument_id, bar.session_date)
        if ca:
            self._close_open_order(order, ca)
            return
        if bar.session_date != st.accepted_session:
            reason = (
                "DAY_ORDER_EXPIRED: session ended"
                if order.order_type is OrderType.LIMIT
                else f"NEXT_BAR_UNAVAILABLE: next bar is in session {bar.session_date} (overnight gap)"
            )
            self._close_open_order(order, reason)
            return
        self._fill_on_bar(account, order, bar)
        if order.order_type is OrderType.MARKET and order.status in OPEN_ORDER_STATUSES:
            self._close_open_order(order, "IOC: unfilled remainder cancelled after the next bar")

    def _price_on_bar(self, order: Order, bar: ExecutionBar, costs: CostModel) -> _Fill | None:
        side = order.side
        slipped_open = costs.slipped_price(side, bar.open)
        if order.order_type is OrderType.MARKET:
            return _Fill(slipped_open, bar.open, bar.bar_start, FillKind.NEXT_BAR_OPEN)
        limit = order.limit_price
        assert limit is not None
        if side is OrderSide.BUY:
            if bar.open <= limit:
                return _Fill(min(slipped_open, limit), bar.open, bar.bar_start, FillKind.NEXT_BAR_OPEN)
            if bar.low < limit:
                return _Fill(limit, limit, bar.bar_end, FillKind.BAR_LIMIT_CROSS)
            return None
        if bar.open >= limit:
            return _Fill(max(slipped_open, limit), bar.open, bar.bar_start, FillKind.NEXT_BAR_OPEN)
        if bar.high > limit:
            return _Fill(limit, limit, bar.bar_end, FillKind.BAR_LIMIT_CROSS)
        return None

    def _fill_on_bar(self, account: VirtualAccount, order: Order, bar: ExecutionBar) -> None:
        try:
            # rates in force on the simulated fill date (effective-dated schedule), for the order's product
            costs = self.costs_for(order.product, bar.session_date)
        except CostScheduleError as e:
            self._close_open_order(order, f"COSTS_NOT_CONFIGURED: {e}")
            return
        already_used = self.db.scalar(
            select(func.count()).select_from(Fill).where(Fill.order_id == order.id, Fill.price_source_ref == bar.source_ref)
        )
        if already_used:
            return  # this bar already filled this order once
        fill = self._price_on_bar(order, bar, costs)
        if fill is None:
            return
        remaining = order.quantity - order.filled_quantity
        qty = remaining
        cap_reason = None

        if costs.max_volume_participation is not None:
            qty = min(qty, int((Decimal(bar.volume) * costs.max_volume_participation).to_integral_value(ROUND_FLOOR)))

        if order.side is OrderSide.BUY:
            assert order.stop_loss_price is not None
            per_share_risk = fill.price - order.stop_loss_price
            if per_share_risk <= 0:
                self._close_open_order(
                    order, f"GAP_THROUGH_STOP: fill price {fill.price} is at or below stop {order.stop_loss_price}"
                )
                return
            if order.risk_budget is not None:
                used = (
                    (order.avg_fill_price - order.stop_loss_price) * order.filled_quantity if order.filled_quantity else 0
                )
                by_risk = int(((order.risk_budget - used) / per_share_risk).to_integral_value(ROUND_FLOOR))
                if by_risk < qty:
                    qty, cap_reason = max(by_risk, 0), "RISK_BUDGET: gap at the fill price would exceed approved risk"
            available = self.portfolio.available_cash(account, exclude_order_id=order.id)
            by_cash = costs.max_buy_quantity(fill.price, available)
            if by_cash < qty:
                qty, cap_reason = by_cash, "INSUFFICIENT_CASH_AT_FILL"

        instrument = self.db.get(Instrument, order.instrument_id)
        if instrument is not None and instrument.lot_size:
            qty -= qty % instrument.lot_size

        if qty > 0:
            self._execute(account, order, bar, fill, qty, costs)
        if cap_reason and order.status in OPEN_ORDER_STATUSES:
            self._close_open_order(order, f"{cap_reason}; remainder cancelled")

    def _execute(
        self, account: VirtualAccount, order: Order, bar: ExecutionBar, fill: _Fill, qty: int, costs: CostModel
    ) -> None:
        now = self.clock()  # booking time; never used as the simulated fill time
        turnover = (fill.price * qty).quantize(MONEY_QUANTUM)
        fees = costs.fees(order.side, turnover, self._fee_context(account, order, fill, costs))
        fee = fees.total

        position = self.portfolio.position(account.user_id, order.instrument_id)
        if order.side is OrderSide.BUY:
            if turnover + fee > self.portfolio.available_cash(account, exclude_order_id=order.id):
                raise ExchangeInvariantError("buy fill exceeds available cash including its own reservation")
        elif position is None or position.quantity < qty:
            raise ExchangeInvariantError("sell fill exceeds held quantity")

        # Position arithmetic first, so the fill is inserted once, complete (fills are append-only).
        state = PositionState(
            position.quantity if position else 0,
            position.avg_entry_price if position else None,
            position.realized_pnl if position else Decimal(0),
        )
        opening = order.side is OrderSide.BUY and state.quantity == 0
        if order.side is OrderSide.BUY:
            state = apply_buy(state, qty, fill.price)
            realized = Decimal(0)
        else:
            state, realized = apply_sell(state, qty, fill.price)

        seq = (self.db.scalar(select(func.count()).select_from(Fill).where(Fill.order_id == order.id)) or 0) + 1
        row = Fill(
            user_id=account.user_id,
            order_id=order.id,
            instrument_id=order.instrument_id,
            seq=seq,
            side=order.side,
            quantity=qty,
            reference_price=fill.reference_price,
            price=fill.price,
            turnover=turnover,
            fee=fee,
            price_source_kind=fill.kind.value,
            price_source_ref=bar.source_ref,
            provider_code=bar.provider_code,
            price_basis=bar.price_basis,
            execution_policy=order.execution_policy,
            price_as_of=fill.as_of,  # simulated fill time
            price_observed_at=bar.observed_at,  # when the finalized candle became available to Agent-X
            cost_model=costs.snapshot(),
            fee_breakdown=fees.as_dict(),
            realized_pnl=realized,  # attributed to price_as_of (B10)
            executed_at=now,  # booking time
        )
        self.db.add(row)
        self.db.flush()

        if order.side is OrderSide.BUY:
            self._ledger(account, row, LedgerEntryType.BUY_SETTLEMENT, -turnover)
        else:
            self._ledger(account, row, LedgerEntryType.SELL_PROCEEDS, turnover)
        self._ledger(account, row, LedgerEntryType.FEE, -fee)

        if position is None:
            position = Position(
                user_id=account.user_id,
                account_id=account.id,
                instrument_id=order.instrument_id,
                quantity=0,
                avg_entry_price=None,
                realized_pnl=Decimal(0),
                fees_paid=Decimal(0),
            )
            self.db.add(position)
        if opening:
            # B10: the position opened at the *simulated* fill time, not when the simulator processed it.
            position.opened_at = fill.as_of
        if order.side is OrderSide.BUY:
            # Protective levels follow the most recent entry that specified them.
            if order.stop_loss_price is not None:
                position.stop_loss_price = order.stop_loss_price
            if order.take_profit_price is not None:
                position.take_profit_price = order.take_profit_price
        elif state.quantity == 0:
            position.stop_loss_price = position.take_profit_price = None
            position.opened_at = None
        position.quantity = state.quantity
        position.avg_entry_price = state.avg_entry_price
        position.realized_pnl = state.realized_pnl
        position.fees_paid = (position.fees_paid or Decimal(0)) + fee

        prev_value = (order.avg_fill_price or Decimal(0)) * order.filled_quantity
        order.filled_quantity += qty
        order.avg_fill_price = ((prev_value + fill.price * qty) / order.filled_quantity).quantize(
            AVG_QUANTUM, rounding=ROUND_HALF_EVEN
        )
        if order.filled_quantity == order.quantity:
            order.status = OrderStatus.FILLED
            order.reserved_cash = Decimal(0)
        else:
            order.status = OrderStatus.PARTIALLY_FILLED
            if order.side is OrderSide.BUY:
                order.reserved_cash = self._reservation(order, fill.reference_price, costs)
        self.db.flush()
        log.info(
            "order filled",
            extra={
                "fields": {
                    **self._log_fields(order),
                    "fill_qty": qty,
                    "fill_price": str(fill.price),
                    "fill_kind": fill.kind.value,
                    "fee": str(fee),
                    "price_source_ref": bar.source_ref,
                    "provider": bar.provider_code,
                    "price_basis": bar.price_basis.value,
                }
            },
        )

    # ---- helpers --------------------------------------------------------------------------

    def _new_order(
        self, account: VirtualAccount, intent: OrderIntent, fingerprint: str, quantity: int, risk_budget: Decimal | None,
        decision_price_ref: str | None = None,
    ) -> Order:
        now = self.clock()
        return Order(
            user_id=account.user_id,
            account_id=account.id,
            instrument_id=intent.instrument_id,
            client_order_id=intent.client_order_id,
            request_fingerprint=fingerprint,
            source=intent.source,
            strategy_id=intent.strategy_id,
            side=intent.side,
            order_type=intent.order_type,
            requested_quantity=intent.quantity,
            quantity=quantity,
            filled_quantity=0,
            limit_price=intent.limit_price,
            stop_loss_price=intent.stop_loss_price,
            take_profit_price=intent.take_profit_price,
            reserved_cash=Decimal(0),
            accepted_at=now,
            risk_budget=risk_budget,
            signal_at=intent.signal_at or now,
            decision_price_ref=decision_price_ref,
            execution_policy=self.execution_policy,
            product=intent.product,
        )

    def _reject(self, order: Order, reason: str) -> Order:
        order.status = OrderStatus.REJECTED
        order.reject_reason = reason
        order.reserved_cash = Decimal(0)
        self.db.flush()
        log.info("order rejected by exchange", extra={"fields": self._log_fields(order)})
        return order

    def _close_open_order(self, order: Order, reason: str) -> None:
        order.status = OrderStatus.CANCELLED
        order.cancel_reason = reason
        order.reserved_cash = Decimal(0)

    def _reservation(self, order: Order, reference_price: Decimal, costs: CostModel) -> Decimal:
        """Cash held for the unfilled BUY quantity: worst-case cost (every fee and rounding) at the price cap (limit,
        or slipped reference price). A market order's next-bar open can be higher than the reference; the fill is then
        capped by available cash at the fill price (see `_fill_on_bar`)."""
        remaining = order.quantity - order.filled_quantity
        cap = order.limit_price if order.order_type is OrderType.LIMIT else costs.slipped_price(OrderSide.BUY, reference_price)
        assert cap is not None
        return costs.buy_cost_upper(cap, remaining).quantize(MONEY_QUANTUM, rounding=ROUND_CEILING)

    def _fee_context(self, account: VirtualAccount, order: Order, fill: _Fill, costs: CostModel) -> FeeContext:
        """Facts about earlier fills that the pure cost model needs: per-order brokerage, whether today's DP charge
        was already booked, and the (raw, charged) sums of each charge's rounding group. Groups use the *simulated*
        session date of the fills."""
        order_rows = self.db.scalars(select(Fill.fee_breakdown).where(Fill.order_id == order.id)).all()
        session = session_date_of(fill.as_of)
        start, end = session_bounds(session)
        session_rows = self.db.execute(
            select(Fill.instrument_id, Fill.side, Fill.fee_breakdown).where(
                Fill.user_id == account.user_id, Fill.price_as_of >= start, Fill.price_as_of < end
            )
        ).all()
        dp_already = any(i == order.instrument_id and side is OrderSide.SELL for i, side, _ in session_rows)
        prior: dict[ChargeType, tuple[Decimal, Decimal]] = {}
        for charge, rule in costs.rounding.items():
            if rule.stage is RoundingStage.FILL:
                continue
            if rule.stage is RoundingStage.ORDER:
                rows = order_rows
            elif rule.stage is RoundingStage.SESSION:
                rows = [b for _, _, b in session_rows]
            else:
                rows = [b for i, _, b in session_rows if i == order.instrument_id]
            parts = [breakdown_component(b, charge) for b in rows]
            prior[charge] = (sum((p[0] for p in parts), Decimal(0)), sum((p[1] for p in parts), Decimal(0)))
        return FeeContext(
            first_fill_of_order=order.filled_quantity == 0,
            brokerage_raw_on_order=sum((breakdown_component(b, ChargeType.BROKERAGE)[0] for b in order_rows), Decimal(0)),
            dp_already_charged=dp_already,
            prior=prior,
        )

    def _sellable_quantity(self, user_id: uuid.UUID, instrument_id: int, exclude_order_id: uuid.UUID) -> int:
        position = self.portfolio.position(user_id, instrument_id)
        held = position.quantity if position else 0
        committed = sum(
            o.quantity - o.filled_quantity
            for o in self.portfolio.open_orders(user_id, instrument_id, OrderSide.SELL)
            if o.id != exclude_order_id
        )
        return held - committed

    def position_corporate_action_problem(self, user_id: uuid.UUID, instrument_id: int, upto: date) -> str | None:
        """A position opened before a split/bonus/other ex-date (on or before `upto`) is on an unadjusted basis."""
        position = self.portfolio.position(user_id, instrument_id)
        if position is None or position.quantity == 0 or position.opened_at is None:
            return None
        a = unadjusted_action(self.db, position, upto)
        if a is None:
            return None
        return (
            f"CORPORATE_ACTION_UNADJUSTED_POSITION: {a.action_type.value} ex-date {a.ex_date} (corporate action "
            f"{a.id}) after the position was opened; apply a corporate-action adjustment to resume"
        )

    def _kill_switch_engaged(self, user_id: uuid.UUID) -> bool:
        state = self.db.scalar(owned_select(RiskState, user_id))
        return bool(state and state.kill_switch_engaged)

    def _ledger(self, account: VirtualAccount, fill: Fill, entry_type: LedgerEntryType, amount: Decimal) -> None:
        self.db.add(
            CashLedgerEntry(
                user_id=account.user_id, account_id=account.id, fill_id=fill.id, entry_type=entry_type, amount=amount
            )
        )

    @staticmethod
    def _money(v: Decimal) -> str:
        return str(v.quantize(Decimal("0.01")))

    @staticmethod
    def _log_fields(order: Order) -> dict:
        return {
            "order_id": str(order.id),
            "user_id": str(order.user_id),
            "instrument_id": order.instrument_id,
            "side": order.side.value,
            "type": order.order_type.value,
            "quantity": order.quantity,
            "filled": order.filled_quantity,
            "status": order.status.value,
            "reason": order.reject_reason or order.cancel_reason,
        }
