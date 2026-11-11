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

Concurrency: every mutating operation first locks the user's `virtual_accounts` row
(SELECT ... FOR UPDATE), so a user's orders, fills and ledger entries are serialized.
Callers own the transaction: nothing here commits, so any failure rolls everything back.
"""

import logging
import uuid
from dataclasses import dataclass
from datetime import date, datetime
from decimal import ROUND_FLOOR, ROUND_HALF_EVEN, Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.clock import Clock
from app.db.enums import (
    OPEN_ORDER_STATUSES,
    InstrumentType,
    LedgerEntryType,
    OrderSide,
    OrderStatus,
    OrderType,
    TradingStatus,
)
from app.db.models import CashLedgerEntry, Fill, Instrument, Order, Position, RiskState, VirtualAccount
from app.db.scoping import owned_select
from app.exchange.accounting import AVG_QUANTUM, PositionState, apply_buy, apply_sell
from app.exchange.costs import MONEY_QUANTUM, CostModel
from app.exchange.portfolio import PortfolioReader
from app.exchange.pricing import ExecutionBar, ExecutionPriceGate, FillKind
from app.exchange.types import OrderIntent

log = logging.getLogger("agentx.exchange")

# NSE trading dates are Indian calendar dates (a fact about the exchange, not a parameter).
EXCHANGE_TZ = ZoneInfo("Asia/Kolkata")


def session_date_of(ts: datetime) -> date:
    return ts.astimezone(EXCHANGE_TZ).date()


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
    as_of: datetime
    kind: FillKind


class MockExchange:
    def __init__(
        self, db: Session, gate: ExecutionPriceGate, costs: CostModel | None, clock: Clock, timeframe_minutes: int = 5
    ):
        self.db = db
        self.gate = gate
        self.costs = costs
        self.clock = clock
        self.timeframe_minutes = timeframe_minutes
        self.portfolio = PortfolioReader(db, gate)

    # ---- order acceptance -------------------------------------------------------------

    def record_rejected(self, account: VirtualAccount, intent: OrderIntent, fingerprint: str, reason: str) -> Order:
        """Persist an order that was rejected upstream (e.g. by risk), for a complete history."""
        lock_account(self.db, account.user_id)
        order = self._new_order(account, intent, fingerprint, quantity=0, risk_budget=None)
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
    ) -> Order:
        """Accept an order for `quantity` (already sized by risk), validate it independently and reserve cash.
        The order rests as PENDING; it can only fill against later bars (see `process_open_orders`)."""
        lock_account(self.db, account.user_id)
        order = self._new_order(account, intent, fingerprint, quantity=quantity, risk_budget=risk_budget)
        order.status = OrderStatus.PENDING
        self.db.add(order)
        self.db.flush()

        problem = instrument_problem(self.db.get(Instrument, order.instrument_id), quantity, order.limit_price)
        if problem:
            return self._reject(order, problem)
        if self.costs is None:
            return self._reject(order, "COSTS_NOT_CONFIGURED: fee and slippage assumptions are not set")
        if order.side is OrderSide.BUY and self._kill_switch_engaged(account.user_id):
            return self._reject(order, "KILL_SWITCH_ENGAGED")
        if order.side is OrderSide.BUY and order.stop_loss_price is None:
            return self._reject(order, "STOP_LOSS_REQUIRED: entries must carry a stop-loss")

        # Stale or unapproved data blocks new orders (the reference price must be eligible and fresh).
        check = self.gate.check(order.instrument_id)
        if not check.ok:
            return self._reject(order, f"EXECUTION_PRICE_UNAVAILABLE: {check.rejection.value}: {check.detail}")

        if order.side is OrderSide.SELL:
            held = self._sellable_quantity(account.user_id, order.instrument_id, exclude_order_id=order.id)
            if quantity > held:
                return self._reject(
                    order, f"INSUFFICIENT_POSITION: requested {quantity}, sellable {held} (short selling not supported)"
                )
        else:
            assert check.quote is not None
            needed = self._reservation(order, check.quote.price)
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
        """Execute resting orders against the bars that have finalized since they were accepted, in time
        order, then expire DAY orders whose session has ended. Returns the orders that changed."""
        lock_account(self.db, account.user_id)
        touched = []
        orders = self.db.scalars(
            owned_select(Order, account.user_id)
            .where(Order.status.in_(OPEN_ORDER_STATUSES))
            .order_by(Order.accepted_at, Order.created_at, Order.id)
            .with_for_update()
        ).all()
        for order in orders:
            before = (order.status, order.filled_quantity)
            if self.costs is not None:
                self._process(account, order)
            if order.status in OPEN_ORDER_STATUSES and session_date_of(self.clock()) > session_date_of(order.accepted_at):
                reason = (
                    "DAY_ORDER_EXPIRED: session ended"
                    if order.order_type is OrderType.LIMIT
                    else "NEXT_BAR_UNAVAILABLE: no eligible bar in the order's session"
                )
                self._close_open_order(order, reason)
            if (order.status, order.filled_quantity) != before:
                touched.append(order)
        self.db.flush()
        return touched

    # ---- execution ------------------------------------------------------------------------

    def _process(self, account: VirtualAccount, order: Order) -> None:
        bar_check = self.gate.fill_bars(order.instrument_id, order.accepted_at)
        accepted_session = session_date_of(order.accepted_at)
        for bar in bar_check.bars:
            if order.status not in OPEN_ORDER_STATUSES:
                return
            if bar.session_date != accepted_session:
                reason = (
                    "DAY_ORDER_EXPIRED: session ended"
                    if order.order_type is OrderType.LIMIT
                    else f"NEXT_BAR_UNAVAILABLE: next bar is in session {bar.session_date} (overnight gap)"
                )
                self._close_open_order(order, reason)
                return
            if order.order_type is OrderType.MARKET:
                window_end = order.accepted_at.timestamp() + self.timeframe_minutes * 60
                if bar.bar_start.timestamp() >= window_end:
                    self._close_open_order(
                        order, f"NEXT_BAR_UNAVAILABLE: first bar after acceptance starts at {bar.bar_start.isoformat()}"
                    )
                    return
                self._fill_on_bar(account, order, bar)
                if order.status in OPEN_ORDER_STATUSES:
                    self._close_open_order(order, "IOC: unfilled remainder cancelled after the next bar")
                return
            self._fill_on_bar(account, order, bar)

    def _price_on_bar(self, order: Order, bar: ExecutionBar) -> _Fill | None:
        assert self.costs is not None
        side = order.side
        slipped_open = self.costs.slipped_price(side, bar.open)
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
        assert self.costs is not None
        already_used = self.db.scalar(
            select(func.count()).select_from(Fill).where(Fill.order_id == order.id, Fill.price_source_ref == bar.source_ref)
        )
        if already_used:
            return  # this bar already filled this order once
        fill = self._price_on_bar(order, bar)
        if fill is None:
            return
        remaining = order.quantity - order.filled_quantity
        qty = remaining
        cap_reason = None

        if self.costs.max_volume_participation is not None:
            qty = min(qty, int((Decimal(bar.volume) * self.costs.max_volume_participation).to_integral_value(ROUND_FLOOR)))

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
            by_cash = self.costs.max_buy_quantity(fill.price, available)
            if by_cash < qty:
                qty, cap_reason = by_cash, "INSUFFICIENT_CASH_AT_FILL"

        instrument = self.db.get(Instrument, order.instrument_id)
        if instrument is not None and instrument.lot_size:
            qty -= qty % instrument.lot_size

        if qty > 0:
            self._execute(account, order, bar, fill, qty)
        if cap_reason and order.status in OPEN_ORDER_STATUSES:
            self._close_open_order(order, f"{cap_reason}; remainder cancelled")

    def _execute(self, account: VirtualAccount, order: Order, bar: ExecutionBar, fill: _Fill, qty: int) -> None:
        assert self.costs is not None
        now = self.clock()
        turnover = (fill.price * qty).quantize(MONEY_QUANTUM)
        prior_brokerage = sum(
            (Decimal(b["brokerage"]) for b in self.db.scalars(select(Fill.fee_breakdown).where(Fill.order_id == order.id))
             if b and "brokerage" in b),
            Decimal(0),
        )
        fees = self.costs.fees(order.side, turnover, prior_brokerage)
        fee = fees.total

        position = self.portfolio.position(account.user_id, order.instrument_id)
        if order.side is OrderSide.BUY:
            if turnover + fee > self.portfolio.available_cash(account, exclude_order_id=order.id):
                raise ExchangeInvariantError("buy fill exceeds available cash including its own reservation")
        elif position is None or position.quantity < qty:
            raise ExchangeInvariantError("sell fill exceeds held quantity")

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
            price_as_of=fill.as_of,
            price_observed_at=bar.observed_at,
            cost_model=self.costs.snapshot(),
            fee_breakdown=fees.as_dict(),
            executed_at=now,
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
        state = PositionState(position.quantity, position.avg_entry_price, position.realized_pnl)
        if order.side is OrderSide.BUY:
            if state.quantity == 0:
                position.opened_at = now
            state = apply_buy(state, qty, fill.price)
            # Protective levels follow the most recent entry that specified them.
            if order.stop_loss_price is not None:
                position.stop_loss_price = order.stop_loss_price
            if order.take_profit_price is not None:
                position.take_profit_price = order.take_profit_price
        else:
            state, _ = apply_sell(state, qty, fill.price)
            if state.quantity == 0:
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
                order.reserved_cash = self._reservation(order, fill.reference_price)
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
        self, account: VirtualAccount, intent: OrderIntent, fingerprint: str, quantity: int, risk_budget: Decimal | None
    ) -> Order:
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
            accepted_at=self.clock(),
            risk_budget=risk_budget,
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

    def _reservation(self, order: Order, reference_price: Decimal) -> Decimal:
        """Cash held for the unfilled BUY quantity: price cap (limit, or slipped reference price) times
        quantity, plus every fee at that turnover. A market order's next-bar open can be higher than the
        reference; the fill is then capped by available cash at the fill price (see `_fill_on_bar`)."""
        assert self.costs is not None
        remaining = order.quantity - order.filled_quantity
        cap = order.limit_price if order.order_type is OrderType.LIMIT else self.costs.slipped_price(
            OrderSide.BUY, reference_price
        )
        assert cap is not None
        turnover = (cap * remaining).quantize(MONEY_QUANTUM)
        return turnover + self.costs.fee(OrderSide.BUY, turnover)

    def _sellable_quantity(self, user_id: uuid.UUID, instrument_id: int, exclude_order_id: uuid.UUID) -> int:
        position = self.portfolio.position(user_id, instrument_id)
        held = position.quantity if position else 0
        committed = sum(
            o.quantity - o.filled_quantity
            for o in self.portfolio.open_orders(user_id, instrument_id, OrderSide.SELL)
            if o.id != exclude_order_id
        )
        return held - committed

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
