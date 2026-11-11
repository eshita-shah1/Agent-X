"""The Mock Exchange: order acceptance, independent validation, simulated fills and accounting.

The exchange is deliberately independent of the risk engine. It re-validates everything that
matters for execution (instrument, quantity, kill switch, cash, held quantity, price
eligibility) and does not trust that an upstream component already did. It never decides
*what* to trade and never creates a price: every fill uses an observation that passed the
`ExecutionPriceGate` and records its provenance.

Execution semantics (documented assumptions):
  * Cash account, long-only. SELL only reduces an existing long position; short selling is
    rejected.
  * MARKET orders are immediate-or-cancel: they fill against the current eligible
    observation, and any quantity not filled (liquidity cap) is cancelled.
  * LIMIT orders fill when the observation is marketable (BUY: price <= limit, SELL:
    price >= limit), at the slipped price capped by the limit. Otherwise they rest until
    `process_open_orders` sees a marketable observation.
  * Partial fills occur only when EXCHANGE_MAX_VOLUME_PARTICIPATION is configured.
  * One observation can fill a given order at most once.
  * No execution delay is modelled.

Concurrency: every mutating operation first locks the user's `virtual_accounts` row
(SELECT ... FOR UPDATE), so a user's orders, fills and ledger entries are serialized.
Callers own the transaction: nothing here commits, so any failure rolls everything back.
"""

import logging
import uuid
from decimal import ROUND_FLOOR, ROUND_HALF_EVEN, Decimal

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
from app.exchange.pricing import ExecutionPriceGate, PriceCheck
from app.exchange.types import OrderIntent

log = logging.getLogger("agentx.exchange")


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


class MockExchange:
    def __init__(self, db: Session, gate: ExecutionPriceGate, costs: CostModel | None, clock: Clock):
        self.db = db
        self.gate = gate
        self.costs = costs
        self.clock = clock
        self.portfolio = PortfolioReader(db, gate)

    # ---- order acceptance -------------------------------------------------------------

    def record_rejected(self, account: VirtualAccount, intent: OrderIntent, fingerprint: str, reason: str) -> Order:
        """Persist an order that was rejected upstream (e.g. by risk), for a complete history."""
        lock_account(self.db, account.user_id)
        order = self._new_order(account, intent, fingerprint, quantity=0)
        order.status = OrderStatus.REJECTED
        order.reject_reason = reason
        self.db.add(order)
        self.db.flush()
        log.info("order rejected upstream", extra={"fields": self._log_fields(order)})
        return order

    def submit(self, account: VirtualAccount, intent: OrderIntent, fingerprint: str, quantity: int) -> Order:
        """Accept an order for `quantity` (already sized by risk), validate it independently,
        reserve cash, and attempt immediate execution."""
        lock_account(self.db, account.user_id)
        order = self._new_order(account, intent, fingerprint, quantity=quantity)
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
        self._try_fill(account, order, check)
        self.db.flush()
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
        """Try to execute resting orders against the current eligible observations. Orders
        whose instrument has no eligible observation stay open; nothing is filled from stale
        or unapproved data."""
        lock_account(self.db, account.user_id)
        touched = []
        orders = self.db.scalars(
            owned_select(Order, account.user_id)
            .where(Order.status.in_(OPEN_ORDER_STATUSES))
            .order_by(Order.created_at, Order.id)
            .with_for_update()
        ).all()
        for order in orders:
            if self.costs is None:
                break
            check = self.gate.check(order.instrument_id)
            if check.ok and self._try_fill(account, order, check):
                touched.append(order)
        self.db.flush()
        return touched

    # ---- execution ------------------------------------------------------------------------

    def _try_fill(self, account: VirtualAccount, order: Order, check: PriceCheck) -> bool:
        assert self.costs is not None and check.quote is not None
        quote = check.quote
        if quote.instrument_id != order.instrument_id:
            raise ExchangeInvariantError("price observation instrument does not match the order")
        remaining = order.quantity - order.filled_quantity
        if remaining <= 0:
            return False

        slipped = self.costs.slipped_price(order.side, quote.price)
        if order.order_type is OrderType.LIMIT:
            assert order.limit_price is not None
            if order.side is OrderSide.BUY:
                if quote.price > order.limit_price:
                    return False
                price = min(slipped, order.limit_price)
            else:
                if quote.price < order.limit_price:
                    return False
                price = max(slipped, order.limit_price)
        else:
            price = slipped

        fill_qty = remaining
        if self.costs.max_volume_participation is not None:
            liquidity = (
                0
                if quote.volume is None
                else int((Decimal(quote.volume) * self.costs.max_volume_participation).to_integral_value(ROUND_FLOOR))
            )
            fill_qty = min(fill_qty, liquidity)
        instrument = self.db.get(Instrument, order.instrument_id)
        if instrument is not None and instrument.lot_size:
            fill_qty -= fill_qty % instrument.lot_size

        already_used = self.db.scalar(
            select(func.count()).select_from(Fill).where(
                Fill.order_id == order.id, Fill.price_source_ref == quote.source_ref
            )
        )
        if already_used:
            return False  # this observation already filled this order once

        if fill_qty <= 0:
            if order.order_type is OrderType.MARKET:
                self._close_open_order(order, "IOC: no liquidity available in the eligible observation")
            return False

        self._execute(account, order, check, fill_qty, price)
        if order.order_type is OrderType.MARKET and order.filled_quantity < order.quantity:
            self._close_open_order(order, "IOC: unfilled remainder cancelled (liquidity cap)")
        return True

    def _execute(self, account: VirtualAccount, order: Order, check: PriceCheck, qty: int, price: Decimal) -> None:
        assert self.costs is not None and check.quote is not None
        quote = check.quote
        now = self.clock()
        first_fill = order.filled_quantity == 0
        turnover = (price * qty).quantize(MONEY_QUANTUM)
        fee = self.costs.fee(order.side, turnover, include_fixed=first_fill)

        position = self.portfolio.position(account.user_id, order.instrument_id)
        if order.side is OrderSide.BUY:
            available_to_order = self.portfolio.available_cash(account, exclude_order_id=order.id)
            if turnover + fee > available_to_order:
                raise ExchangeInvariantError("buy fill exceeds available cash including its own reservation")
        elif position is None or position.quantity < qty:
            raise ExchangeInvariantError("sell fill exceeds held quantity")

        seq = (
            self.db.scalar(select(func.count()).select_from(Fill).where(Fill.order_id == order.id)) or 0
        ) + 1
        fill = Fill(
            user_id=account.user_id,
            order_id=order.id,
            instrument_id=order.instrument_id,
            seq=seq,
            side=order.side,
            quantity=qty,
            reference_price=quote.price,
            price=price,
            turnover=turnover,
            fee=fee,
            price_source_kind=quote.kind.value,
            price_source_ref=quote.source_ref,
            provider_code=quote.provider_code,
            price_basis=quote.price_basis,
            price_as_of=quote.as_of,
            price_observed_at=quote.observed_at,
            cost_model=self.costs.snapshot(),
            executed_at=now,
        )
        self.db.add(fill)
        self.db.flush()

        if order.side is OrderSide.BUY:
            self._ledger(account, fill, LedgerEntryType.BUY_SETTLEMENT, -turnover)
        else:
            self._ledger(account, fill, LedgerEntryType.SELL_PROCEEDS, turnover)
        self._ledger(account, fill, LedgerEntryType.FEE, -fee)

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
            state = apply_buy(state, qty, price)
            # Protective levels follow the most recent entry that specified them.
            if order.stop_loss_price is not None:
                position.stop_loss_price = order.stop_loss_price
            if order.take_profit_price is not None:
                position.take_profit_price = order.take_profit_price
        else:
            state, _ = apply_sell(state, qty, price)
            if state.quantity == 0:
                position.stop_loss_price = position.take_profit_price = None
                position.opened_at = None
        position.quantity = state.quantity
        position.avg_entry_price = state.avg_entry_price
        position.realized_pnl = state.realized_pnl
        position.fees_paid = (position.fees_paid or Decimal(0)) + fee

        prev_value = (order.avg_fill_price or Decimal(0)) * order.filled_quantity
        order.filled_quantity += qty
        order.avg_fill_price = ((prev_value + price * qty) / order.filled_quantity).quantize(
            AVG_QUANTUM, rounding=ROUND_HALF_EVEN
        )
        if order.filled_quantity == order.quantity:
            order.status = OrderStatus.FILLED
            order.reserved_cash = Decimal(0)
        else:
            order.status = OrderStatus.PARTIALLY_FILLED
            if order.side is OrderSide.BUY:
                order.reserved_cash = self._reservation(order, quote.price)
        self.db.flush()
        log.info(
            "order filled",
            extra={
                "fields": {
                    **self._log_fields(order),
                    "fill_qty": qty,
                    "fill_price": str(price),
                    "fee": str(fee),
                    "price_source_ref": quote.source_ref,
                    "provider": quote.provider_code,
                    "price_basis": quote.price_basis.value,
                }
            },
        )

    # ---- helpers --------------------------------------------------------------------------

    def _new_order(self, account: VirtualAccount, intent: OrderIntent, fingerprint: str, quantity: int) -> Order:
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
        """Worst-case cash for the unfilled BUY quantity: price cap (limit, or slipped market
        price) times quantity, plus fees (including the fixed fee if nothing has filled yet)."""
        assert self.costs is not None
        remaining = order.quantity - order.filled_quantity
        cap = order.limit_price if order.order_type is OrderType.LIMIT else self.costs.slipped_price(
            OrderSide.BUY, reference_price
        )
        assert cap is not None
        turnover = (cap * remaining).quantize(MONEY_QUANTUM)
        return turnover + self.costs.fee(OrderSide.BUY, turnover, include_fixed=order.filled_quantity == 0)

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
