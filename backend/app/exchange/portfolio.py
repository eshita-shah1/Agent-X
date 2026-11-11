"""Read-side portfolio valuation: cash, reservations, positions and marks.

Positions are marked only with prices that pass the execution-price gate. When a position
cannot be marked, its unrealized P&L and the account equity are reported as unknown (None)
with the reason, never estimated from an unapproved price.
"""

import uuid
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.enums import OPEN_ORDER_STATUSES, OrderSide
from app.db.models import CashLedgerEntry, Order, Position, VirtualAccount
from app.db.scoping import owned_select
from app.exchange.accounting import PositionState, unrealized_pnl
from app.exchange.pricing import ExecutionPriceGate, PriceCheck


@dataclass(frozen=True)
class PositionValuation:
    position: Position
    price_check: PriceCheck
    mark: Decimal | None
    market_value: Decimal | None
    unrealized_pnl: Decimal | None


@dataclass(frozen=True)
class AccountValuation:
    cash_balance: Decimal
    reserved_cash: Decimal
    available_cash: Decimal
    positions: list[PositionValuation]
    positions_market_value: Decimal | None
    equity: Decimal | None  # None when any open position cannot be marked
    gross_exposure: Decimal | None  # marked positions + cash committed to open BUY orders
    unmarked_instrument_ids: list[int]


class PortfolioReader:
    def __init__(self, db: Session, gate: ExecutionPriceGate):
        self.db = db
        self.gate = gate

    def cash_balance(self, account: VirtualAccount) -> Decimal:
        total = self.db.scalar(
            select(func.coalesce(func.sum(CashLedgerEntry.amount), 0)).where(
                CashLedgerEntry.user_id == account.user_id, CashLedgerEntry.account_id == account.id
            )
        )
        return account.initial_capital + Decimal(total)

    def reserved_cash(self, user_id: uuid.UUID, exclude_order_id: uuid.UUID | None = None) -> Decimal:
        stmt = select(func.coalesce(func.sum(Order.reserved_cash), 0)).where(
            Order.user_id == user_id, Order.status.in_(OPEN_ORDER_STATUSES)
        )
        if exclude_order_id is not None:
            stmt = stmt.where(Order.id != exclude_order_id)
        return Decimal(self.db.scalar(stmt))

    def available_cash(self, account: VirtualAccount, exclude_order_id: uuid.UUID | None = None) -> Decimal:
        return self.cash_balance(account) - self.reserved_cash(account.user_id, exclude_order_id)

    def position(self, user_id: uuid.UUID, instrument_id: int) -> Position | None:
        return self.db.scalar(owned_select(Position, user_id).where(Position.instrument_id == instrument_id))

    def open_positions(self, user_id: uuid.UUID) -> list[Position]:
        return list(
            self.db.scalars(owned_select(Position, user_id).where(Position.quantity > 0).order_by(Position.instrument_id))
        )

    def open_orders(
        self, user_id: uuid.UUID, instrument_id: int | None = None, side: OrderSide | None = None
    ) -> list[Order]:
        stmt = owned_select(Order, user_id).where(Order.status.in_(OPEN_ORDER_STATUSES))
        if instrument_id is not None:
            stmt = stmt.where(Order.instrument_id == instrument_id)
        if side is not None:
            stmt = stmt.where(Order.side == side)
        return list(self.db.scalars(stmt.order_by(Order.created_at, Order.id)))

    def valuation(self, account: VirtualAccount) -> AccountValuation:
        cash = self.cash_balance(account)
        reserved = self.reserved_cash(account.user_id)
        valued: list[PositionValuation] = []
        unmarked: list[int] = []
        for pos in self.open_positions(account.user_id):
            check = self.gate.check(pos.instrument_id)
            if check.ok:
                assert check.quote is not None
                mark = check.quote.price
                state = PositionState(pos.quantity, pos.avg_entry_price, pos.realized_pnl)
                valued.append(PositionValuation(pos, check, mark, mark * pos.quantity, unrealized_pnl(state, mark)))
            else:
                unmarked.append(pos.instrument_id)
                valued.append(PositionValuation(pos, check, None, None, None))
        if unmarked:
            market_value = equity = gross = None
        else:
            market_value = sum((v.market_value for v in valued if v.market_value is not None), Decimal(0))
            equity = cash + market_value
            open_buy_commitment = sum(
                (o.reserved_cash for o in self.open_orders(account.user_id, side=OrderSide.BUY)), Decimal(0)
            )
            gross = market_value + open_buy_commitment
        return AccountValuation(
            cash_balance=cash,
            reserved_cash=reserved,
            available_cash=cash - reserved,
            positions=valued,
            positions_market_value=market_value,
            equity=equity,
            gross_exposure=gross,
            unmarked_instrument_ids=unmarked,
        )
