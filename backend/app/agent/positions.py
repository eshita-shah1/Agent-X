"""Strategy position context and instrument blocks, derived from the authoritative Phase 4 records.

Nothing here is held in memory between cycles. A strategy's `PositionContext` for a bar is rebuilt from the account's
fills (by their simulated fill time, `fills.price_as_of`), so it can never disagree with what the Mock Exchange
booked. Phase 4 positions are one per (user, instrument), so the agent only acts on an instrument whose current
position episode it alone opened:

  * MANUAL_INTERVENTION (approved decision D7): a manual order is open, a manual fill was booked in the instrument this
    session, the current position contains a manual fill or no agent entry, or a corporate-action adjustment was
    applied to it. The agent places no further order there; Phase 4 protective exits continue.
  * ORPHANED_OVERNIGHT (approved decision D2): an agent position opened in an earlier session is still open (its
    square-off did not fill). It is never carried over by a new exit policy: only Phase 4 protective exits and the
    user manage it, and the agent does not trade the instrument while it is open.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, datetime

from sqlalchemy.orm import Session

from app.db.enums import (
    OPEN_ORDER_STATUSES,
    AdjustmentStatus,
    AgentInstrumentStatus,
    OrderSide,
    OrderSource,
)
from app.db.models import CorporateActionAdjustment, Fill, Order, Position
from app.db.scoping import owned_select
from app.marketdata.calendar import EXCHANGE_TZ
from app.strategies.base import BAR, FLAT, PositionContext

AGENT_ORDER_PREFIX = "AG:"


@dataclass(frozen=True)
class FillPoint:
    at: datetime  # simulated fill time (fills.price_as_of)
    side: OrderSide
    quantity: int


def session_of(ts: datetime) -> date:
    return ts.astimezone(EXCHANGE_TZ).date()


def contexts_for_bars(bar_starts: list[datetime], fills: list[FillPoint]) -> dict[datetime, PositionContext]:
    """The position context of each bar, from the fills of one session (the position is flat at the session start).

    A fill belongs to the bar in which it was simulated (`bar.start <= at < bar.end`): an entry filled at a bar's open
    is open when that bar is evaluated at its close; an exit filled during a bar closes it in that bar."""
    ordered = sorted(fills, key=lambda f: f.at)
    out: dict[datetime, PositionContext] = {}
    qty, i = 0, 0
    for start in sorted(bar_starts):
        before = qty
        touched = False
        while i < len(ordered) and ordered[i].at < start + BAR:
            f = ordered[i]
            qty += f.quantity if f.side is OrderSide.BUY else -f.quantity
            touched = touched or f.side is OrderSide.SELL
            i += 1
        if qty < 0:
            raise ValueError("fills imply a short position, which the exchange never books")
        if qty > 0:
            out[start] = PositionContext(is_open=True)
        else:
            out[start] = PositionContext(closed_this_bar=before > 0 or touched)
    return out


def session_fills(db: Session, user_id: uuid.UUID, instrument_id: int, session_date: date) -> list[tuple[Fill, Order]]:
    rows = db.execute(
        owned_select(Fill, user_id)
        .add_columns(Order)
        .join(Order, Order.id == Fill.order_id)
        .where(Fill.instrument_id == instrument_id)
        .order_by(Fill.price_as_of, Fill.id)
    ).all()
    return [(f, o) for f, o in rows if session_of(f.price_as_of) == session_date]


def is_agent_order(order: Order) -> bool:
    return order.source is OrderSource.AGENT and order.client_order_id.startswith(AGENT_ORDER_PREFIX)


def assess(db: Session, user_id: uuid.UUID, instrument_id: int, session_date: date) -> tuple[AgentInstrumentStatus, str | None]:
    """The instrument's block, if any, from orders, fills, positions and adjustments (authoritative records)."""
    manual_open = db.scalar(
        owned_select(Order, user_id).where(
            Order.instrument_id == instrument_id, Order.status.in_(OPEN_ORDER_STATUSES), Order.source == OrderSource.MANUAL
        ).limit(1)
    )
    if manual_open is not None:
        return AgentInstrumentStatus.MANUAL_INTERVENTION, f"manual order {manual_open.id} is open"
    today = session_fills(db, user_id, instrument_id, session_date)
    manual_today = next((o for _f, o in today if o.source is OrderSource.MANUAL), None)
    if manual_today is not None:
        return AgentInstrumentStatus.MANUAL_INTERVENTION, f"manual order {manual_today.id} filled this session"
    position = db.scalar(owned_select(Position, user_id).where(Position.instrument_id == instrument_id))
    if position is None or position.quantity == 0:
        return AgentInstrumentStatus.OK, None
    episode = [
        (f, o) for f, o in db.execute(
            owned_select(Fill, user_id).add_columns(Order).join(Order, Order.id == Fill.order_id)
            .where(Fill.instrument_id == instrument_id, Fill.price_as_of >= position.opened_at)
        ).all()
    ] if position.opened_at is not None else []
    if any(o.source is OrderSource.MANUAL for _f, o in episode):
        return AgentInstrumentStatus.MANUAL_INTERVENTION, f"position {position.id} contains a manual fill"
    if not any(is_agent_order(o) and o.side is OrderSide.BUY for _f, o in episode):
        return AgentInstrumentStatus.MANUAL_INTERVENTION, f"position {position.id} was not opened by the agent"
    adjusted = db.scalar(
        owned_select(CorporateActionAdjustment, user_id).where(
            CorporateActionAdjustment.position_id == position.id,
            CorporateActionAdjustment.status == AdjustmentStatus.APPLIED,
            CorporateActionAdjustment.adjusted_at >= position.opened_at,
        ).limit(1)
    )
    if adjusted is not None:
        return AgentInstrumentStatus.MANUAL_INTERVENTION, f"position {position.id} was adjusted for a corporate action"
    if session_of(position.opened_at) < session_date:
        return (
            AgentInstrumentStatus.ORPHANED_OVERNIGHT,
            f"agent position {position.id} opened {session_of(position.opened_at)} is still open (no carry-over exit)",
        )
    return AgentInstrumentStatus.OK, None


def open_quantity(db: Session, user_id: uuid.UUID, instrument_id: int) -> tuple[Position | None, int]:
    position = db.scalar(owned_select(Position, user_id).where(Position.instrument_id == instrument_id))
    return position, (position.quantity if position else 0)


def open_orders(db: Session, user_id: uuid.UUID, instrument_id: int, side: OrderSide | None = None) -> list[Order]:
    q = owned_select(Order, user_id).where(Order.instrument_id == instrument_id, Order.status.in_(OPEN_ORDER_STATUSES))
    if side is not None:
        q = q.where(Order.side == side)
    return list(db.scalars(q))


def flat_contexts(bar_starts: list[datetime]) -> dict[datetime, PositionContext]:
    return {s: FLAT for s in bar_starts}

