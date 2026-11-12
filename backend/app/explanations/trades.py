"""Closed trades (position episodes) reconstructed read-only from the authoritative fills.

A position episode starts when an instrument's holding goes from 0 to > 0 and ends when it returns to 0. The holding is
replayed from the immutable fills (by simulated fill time, `price_as_of`) and APPLIED corporate-action adjustments,
which are the only other events that change a quantity. Nothing is recomputed: an episode's realized P&L is the sum of
the realized P&L the Mock Exchange stored on its fills (plus any cash-in-lieu realized on an adjustment), and its fees
are the sum of the stored fill fees, exactly as `app.exchange.portfolio.daily_pnl` aggregates them.

Outcome label (a classification, not an amount): WIN / LOSS / BREAKEVEN by the sign of (realized P&L - fees) over the
episode's fills; UNKNOWN when a fill predates migration 0007 and carries no realized P&L; OPEN while it is not flat.
An order is not a trade: only a completed episode counts as a closed trade.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.enums import AdjustmentStatus, OrderSide, TradeOutcome
from app.db.models import CorporateActionAdjustment, Fill, Order


@dataclass
class Episode:
    instrument_id: int
    opened_at: datetime
    fill_ids: list[int] = field(default_factory=list)
    entry_order_ids: list[uuid.UUID] = field(default_factory=list)
    exit_order_ids: list[uuid.UUID] = field(default_factory=list)
    exit_sources: list[str] = field(default_factory=list)
    strategy_id: str | None = None
    entry_source: str | None = None
    closed_at: datetime | None = None
    closing_fill_id: int | None = None
    closing_order_id: uuid.UUID | None = None
    realized_pnl: Decimal | None = Decimal(0)  # None if any SELL fill has no stored realized P&L
    fees: Decimal = Decimal(0)
    adjustment_ids: list[int] = field(default_factory=list)

    @property
    def closed(self) -> bool:
        return self.closed_at is not None

    @property
    def net_realized_pnl(self) -> Decimal | None:
        return None if self.realized_pnl is None else self.realized_pnl - self.fees

    @property
    def outcome(self) -> TradeOutcome:
        if not self.closed:
            return TradeOutcome.OPEN
        net = self.net_realized_pnl
        if net is None:
            return TradeOutcome.UNKNOWN
        return TradeOutcome.WIN if net > 0 else TradeOutcome.LOSS if net < 0 else TradeOutcome.BREAKEVEN


def strategy_of(order_strategy_id: str | None) -> str | None:
    """Agent orders carry "<STRATEGY>/<config_id>"; manual orders carry free text or nothing."""
    return order_strategy_id.split("/")[0][:32] if order_strategy_id else None


def episodes(db: Session, user_id: uuid.UUID, instrument_id: int | None = None) -> list[Episode]:
    """Every episode of the user's fills (closed and open), oldest first."""
    q = (select(Fill, Order).join(Order, Order.id == Fill.order_id)
         .where(Fill.user_id == user_id, Order.user_id == user_id))
    if instrument_id is not None:
        q = q.where(Fill.instrument_id == instrument_id)
    fills = db.execute(q.order_by(Fill.instrument_id, Fill.price_as_of, Fill.id)).all()
    aq = select(CorporateActionAdjustment).where(
        CorporateActionAdjustment.user_id == user_id, CorporateActionAdjustment.status == AdjustmentStatus.APPLIED)
    if instrument_id is not None:
        aq = aq.where(CorporateActionAdjustment.instrument_id == instrument_id)
    adjustments = list(db.scalars(aq))

    timeline: dict[int, list[tuple[datetime, int, object, object]]] = {}
    for f, o in fills:
        timeline.setdefault(f.instrument_id, []).append((f.price_as_of, 0, f, o))
    for a in adjustments:
        timeline.setdefault(a.instrument_id, []).append((a.adjusted_at, 1, a, None))

    out: list[Episode] = []
    for iid in sorted(timeline):
        qty, current = 0, None
        for _at, kind, row, order in sorted(timeline[iid], key=lambda t: (t[0], t[1], getattr(t[2], "id", 0))):
            if kind == 1:  # corporate-action adjustment: quantity rescaled, cash-in-lieu realized
                if current is not None:
                    qty = row.adjusted_quantity
                    current.adjustment_ids.append(row.id)
                    if current.realized_pnl is not None:
                        current.realized_pnl += row.realized_pnl
                    if qty == 0:
                        current.closed_at = row.adjusted_at
                        out.append(current)
                        current = None
                continue
            f: Fill = row
            if current is None:
                current = Episode(iid, f.price_as_of, strategy_id=strategy_of(order.strategy_id),
                                  entry_source=order.source.value)
            current.fill_ids.append(f.id)
            current.fees += f.fee
            if f.side is OrderSide.BUY:
                qty += f.quantity
                if order.id not in current.entry_order_ids:
                    current.entry_order_ids.append(order.id)
            else:
                qty -= f.quantity
                if order.id not in current.exit_order_ids:
                    current.exit_order_ids.append(order.id)
                    current.exit_sources.append(order.source.value)
                if f.realized_pnl is None:
                    current.realized_pnl = None
                elif current.realized_pnl is not None:
                    current.realized_pnl += f.realized_pnl
            if qty <= 0:
                current.closed_at, current.closing_fill_id, current.closing_order_id = f.price_as_of, f.id, order.id
                out.append(current)
                current, qty = None, 0
        if current is not None:
            out.append(current)
    return sorted(out, key=lambda e: (e.closed_at or e.opened_at, e.instrument_id))
