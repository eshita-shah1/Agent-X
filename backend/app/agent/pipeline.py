"""The per-instrument decision pipeline: replayed signals -> agent decisions -> OrderService.

Every order the agent places goes through `OrderService.submit`, so the Phase 4 risk engine sizes and may reject it and
the Mock Exchange re-validates it; the agent never writes orders, fills, positions or ledger entries, and computes no
risk size of its own. Approved mapping (decisions D2-D7):

  ENTER_LONG (only from the session's locked selection, only on the latest eligible bar, flat, nothing open)
      -> MARKET BUY. Stop: the strategy's absolute stop, or the signal bar's close minus its stop distance; target:
         the close plus its target distance (D3). Quantity: an upper bound (the user's max position quantity, else
         the shares available cash covers at the reference price, in whole lots); risk resizes it (D4).
  EXIT_LONG (strategy exit or the 15:15 square-off)
      -> an owed exit (D6), submitted as a MARKET SELL of the sellable quantity on each new eligible bar until the
         position is flat. It expires at the end of its session: no carry-over exit policy exists (D2).

Idempotency: client order ids are derived from the instrument, the configuration version and the signal bar
(`AG:E:...`), or the position episode and the eligible bar (`AG:X:...`), and the decision table holds one row per
client order id, so a repeated evaluation can never create a second order.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import ROUND_FLOOR, ROUND_HALF_EVEN, Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agent.positions import open_orders, open_quantity
from app.agent.replay import ArchivedBar, BarOutcome
from app.db.enums import (
    AgentDecisionKind,
    AgentDecisionOutcome,
    AgentInstrumentStatus,
    AgentMode,
    AgentStatus,
    OrderSide,
    OrderSource,
    OrderType,
)
from app.db.models import AgentConfigVersion, AgentDecision, AgentInstance, AgentInstrumentState, Instrument
from app.exchange.types import OrderIntent
from app.orders.service import InvalidOrder, OrderService, TradingContext
from app.strategies.base import BAR, Action, Signal

PRICE_QUANTUM = Decimal("0.000001")  # orders store prices at 6 dp (NUMERIC(18, 6))
O = AgentDecisionOutcome


def entry_key(instrument_id: int, version_id: int, bar_start: datetime) -> str:
    return f"AG:E:{instrument_id}:{version_id}:{int(bar_start.timestamp())}"


def exit_key(instrument_id: int, opened_at: datetime, bar_start: datetime) -> str:
    return f"AG:X:{instrument_id}:{int(opened_at.timestamp())}:{int(bar_start.timestamp())}"


def _price(value: float | Decimal) -> Decimal:
    d = value if isinstance(value, Decimal) else Decimal(repr(value))
    return d.quantize(PRICE_QUANTUM, rounding=ROUND_HALF_EVEN)


def protective_prices(signal: Signal, close: Decimal) -> tuple[Decimal | None, Decimal | None]:
    """(stop, target) anchored to the signal bar's archived close (approved decision D3)."""
    p = signal.protective
    if p is None:
        return None, None
    if p.stop_price is not None:
        stop = _price(p.stop_price)
    elif p.stop_distance is not None:
        stop = _price(close - Decimal(repr(p.stop_distance)))
    else:
        stop = None
    target = _price(close + Decimal(repr(p.target_distance))) if p.target_distance is not None else None
    return stop, target


def upper_bound_quantity(max_position_quantity: int | None, available_cash: Decimal, price: Decimal, lot_size: int | None) -> int:
    """The proposal handed to the risk engine (approved decision D4): an upper bound, not a risk size."""
    if max_position_quantity is not None:
        qty = max_position_quantity
    else:
        qty = int((available_cash / price).to_integral_value(rounding=ROUND_FLOOR)) if price > 0 and available_cash > 0 else 0
    if lot_size:
        qty -= qty % lot_size
    return max(qty, 0)


@dataclass
class InstrumentReport:
    instrument_id: int
    bars: int = 0
    signals: int = 0
    submitted: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


class DecisionPipeline:
    def __init__(self, db: Session, ctx: TradingContext, agent: AgentInstance, version: AgentConfigVersion,
                 cycle_id: int, account, session_date: date, now: datetime, kill_switch: bool):
        self.db, self.ctx, self.agent, self.version = db, ctx, agent, version
        self.cycle_id, self.account, self.session_date, self.now = cycle_id, account, session_date, now
        self.kill_switch = kill_switch
        self.orders = OrderService(ctx)

    # ---- journal --------------------------------------------------------------------------------------------------

    def _decision(self, instrument_id: int, kind: AgentDecisionKind, outcome: AgentDecisionOutcome, *,
                  ab: ArchivedBar | None = None, bar_start: datetime | None = None, config_id: str | None = None,
                  signal: Signal | None = None, detail: str | None = None, explanation: dict | None = None,
                  result=None) -> AgentDecision:
        row = AgentDecision(
            user_id=self.agent.user_id, agent_id=self.agent.id, cycle_id=self.cycle_id,
            config_version_id=self.version.id, instrument_id=instrument_id, session_date=self.session_date,
            kind=kind, outcome=outcome, bar_start=ab.start if ab else bar_start,
            candle_id=ab.candle_id if ab else None, observation_version=ab.observation_version if ab else None,
            provider_code=ab.provider if ab else None, price_basis=ab.price_basis if ab else None,
            config_id=config_id, strategy_id=signal.strategy_id if signal else None,
            strategy_version=signal.strategy_version if signal else None,
            action=signal.action.value if signal else None, reason=signal.reason.value if signal else None,
            values=dict(signal.values) if signal else {}, explanation=explanation or {}, detail=detail,
            decided_at=self.now,
        )
        if result is not None:
            row.client_order_id = result.order.client_order_id
            row.order_id = result.order.id
            row.risk_decision_id = result.decision.id if result.decision is not None else None
            row.order_status = result.order.status.value
        self.db.add(row)
        self.db.flush()
        return row

    def record_selection(self, instrument_id: int, decision) -> None:
        exists = self.db.scalar(select(AgentDecision.id).where(
            AgentDecision.agent_id == self.agent.id, AgentDecision.instrument_id == instrument_id,
            AgentDecision.kind == AgentDecisionKind.SELECTION, AgentDecision.session_date == self.session_date,
        ))
        if exists is None:
            self._decision(
                instrument_id, AgentDecisionKind.SELECTION,
                O.SELECTED if decision.selected_config_id else O.NO_SELECTION,
                bar_start=decision.decided_on_bar_start, config_id=decision.selected_config_id,
                detail=decision.reason.value, explanation=decision.as_dict(),
            )

    # ---- evaluation -----------------------------------------------------------------------------------------------

    def process_bar(self, state: AgentInstrumentState, outcome: BarOutcome, check, report: InstrumentReport) -> None:
        """`check`: this cycle's execution-price check for the instrument (None in SIGNALS_ONLY mode)."""
        ab, signal, iid = outcome.archived, outcome.signal, state.instrument_id
        report.bars += 1
        if outcome.acting_config_id is None or signal is None:
            self._decision(iid, AgentDecisionKind.EVALUATION, O.NO_SELECTION, ab=ab,
                           detail="no locked selection for this bar (before the checkpoint, or NO_SELECTION)")
            return
        cid = outcome.acting_config_id
        if signal.action is Action.NONE:
            self._decision(iid, AgentDecisionKind.EVALUATION, O.NO_SIGNAL, ab=ab, config_id=cid, signal=signal)
            return
        report.signals += 1
        if self.agent.mode is AgentMode.SIGNALS_ONLY:
            self._decision(iid, AgentDecisionKind.EVALUATION, O.SIGNALS_ONLY_NOT_SUBMITTED, ab=ab, config_id=cid,
                           signal=signal, detail="SIGNALS_ONLY: no order is ever submitted")
            return
        if signal.action is Action.EXIT_LONG:
            self._exit_signal(state, ab, cid, signal)
        else:
            self._entry_signal(state, ab, cid, signal, check, report)

    def _exit_signal(self, state: AgentInstrumentState, ab: ArchivedBar, cid: str, signal: Signal) -> None:
        iid = state.instrument_id
        if state.status is not AgentInstrumentStatus.OK:
            self._decision(iid, AgentDecisionKind.EVALUATION, O.SUPPRESSED_INSTRUMENT_BLOCKED, ab=ab, config_id=cid,
                           signal=signal, detail=f"{state.status.value}: {state.status_detail}")
            return
        if state.owed_exit_since is not None:
            self._decision(iid, AgentDecisionKind.EVALUATION, O.EXIT_ALREADY_OWED, ab=ab, config_id=cid, signal=signal)
            return
        _pos, qty = open_quantity(self.db, self.agent.user_id, iid)
        if qty <= 0:
            self._decision(iid, AgentDecisionKind.EVALUATION, O.SUPPRESSED_NOT_HELD, ab=ab, config_id=cid,
                           signal=signal, detail="the position was already closed")
            return
        state.owed_exit_since, state.owed_exit_session, state.owed_exit_reason = ab.start, self.session_date, signal.reason.value
        self._decision(iid, AgentDecisionKind.EVALUATION, O.EXIT_OWED, ab=ab, config_id=cid, signal=signal)

    def _entry_signal(self, state: AgentInstrumentState, ab: ArchivedBar, cid: str, signal: Signal, check,
                      report: InstrumentReport) -> None:
        iid = state.instrument_id

        def suppress(outcome: AgentDecisionOutcome, detail: str, explanation: dict | None = None) -> None:
            self._decision(iid, AgentDecisionKind.EVALUATION, outcome, ab=ab, config_id=cid, signal=signal,
                           detail=detail, explanation=explanation)

        if self.agent.status is not AgentStatus.RUNNING:
            return suppress(O.SUPPRESSED_PAUSED, f"the agent is {self.agent.status.value}: entries are suppressed")
        if self.kill_switch:
            return suppress(O.SUPPRESSED_KILL_SWITCH, "the Phase 4 kill switch is engaged")
        if state.status is not AgentInstrumentStatus.OK:
            return suppress(O.SUPPRESSED_INSTRUMENT_BLOCKED, f"{state.status.value}: {state.status_detail}")
        _pos, qty = open_quantity(self.db, self.agent.user_id, iid)
        pending = open_orders(self.db, self.agent.user_id, iid)
        if qty > 0 or pending:
            return suppress(O.SUPPRESSED_POSITION_OR_ORDER, f"position {qty}, open orders {len(pending)}")
        if check is None or not check.ok or check.quote is None:
            detail = check.detail if check is not None else "no execution-price check"
            return suppress(O.SUPPRESSED_PRICE_NOT_ELIGIBLE, detail, {"price_check": check.as_dict() if check else None})
        if check.quote.bar_start != ab.start:
            return suppress(O.SUPPRESSED_LATE_SIGNAL, "not the latest eligible bar: late entries are never acted on (D5)",
                            {"latest_eligible_bar": check.quote.bar_start.isoformat()})
        stop, target = protective_prices(signal, ab.close)
        if stop is None or stop <= 0 or stop >= ab.close:
            return suppress(O.SUPPRESSED_INVALID_LEVELS, f"stop {stop} is not below the signal close {ab.close}")
        limits = self.ctx.risk.limits(self.agent.user_id)
        instrument = self.db.get(Instrument, iid)
        quantity = upper_bound_quantity(
            limits.max_position_quantity if limits else None, self.ctx.portfolio.available_cash(self.account),
            check.quote.price, instrument.lot_size if instrument else None,
        )
        if quantity <= 0:
            return suppress(O.SUPPRESSED_NO_QUANTITY, "no cash is available for an entry")
        intent = OrderIntent(
            client_order_id=entry_key(iid, self.version.id, ab.start), instrument_id=iid, side=OrderSide.BUY,
            order_type=OrderType.MARKET, quantity=quantity, source=OrderSource.AGENT, stop_loss_price=stop,
            take_profit_price=target, strategy_id=f"{signal.strategy_id}/{cid}"[:64], signal_at=ab.start + BAR,
        )
        explanation = {"stop_loss_price": str(stop), "take_profit_price": str(target) if target else None,
                       "proposed_quantity": quantity, "reference_price": str(check.quote.price),
                       "anchor_close": str(ab.close)}
        try:
            result = self.orders.submit(self.agent.user_id, intent)
        except InvalidOrder as exc:
            return suppress(O.SUPPRESSED_PRICE_NOT_ELIGIBLE if "PRICE" in str(exc) else O.SUPPRESSED_INVALID_LEVELS,
                            f"order refused before risk: {exc}", explanation)
        self._decision(iid, AgentDecisionKind.EVALUATION, O.ENTRY_SUBMITTED, ab=ab, config_id=cid, signal=signal,
                       explanation=explanation, result=result,
                       detail=result.order.reject_reason or f"order {result.order.status.value}")
        report.submitted.append(intent.client_order_id)

    # ---- owed exits -----------------------------------------------------------------------------------------------

    def settle_owed_exit(self, state: AgentInstrumentState, report: InstrumentReport, actor_event) -> None:
        """Submit, wait for or clear an owed exit (approved decisions D2, D6)."""
        if state.owed_exit_since is None:
            return
        iid = state.instrument_id
        position, qty = open_quantity(self.db, self.agent.user_id, iid)
        if qty <= 0:
            actor_event("agent.owed_exit_satisfied", iid, {"owed_exit_since": state.owed_exit_since.isoformat()})
            state.owed_exit_since = state.owed_exit_session = state.owed_exit_reason = None
            return
        if state.owed_exit_session < self.session_date:
            actor_event("agent.owed_exit_expired_session_end", iid, {
                "owed_exit_since": state.owed_exit_since.isoformat(), "reason": state.owed_exit_reason,
                "note": "no carry-over exit policy (D2): the position is ORPHANED_OVERNIGHT",
            })
            state.owed_exit_since = state.owed_exit_session = state.owed_exit_reason = None
            return
        if self.agent.mode is not AgentMode.EXECUTION or self.agent.status not in (AgentStatus.RUNNING, AgentStatus.PAUSED):
            return
        if state.status is not AgentInstrumentStatus.OK:
            report.notes.append(f"owed exit waiting: instrument {state.status.value}")
            return
        if open_orders(self.db, self.agent.user_id, iid, OrderSide.SELL):
            report.notes.append("owed exit waiting: an exit order is open")
            return
        check = self.ctx.gate.check(iid)
        if not check.ok or check.quote is None:
            report.notes.append(f"owed exit waiting for an eligible price: {check.detail}")
            return
        key = exit_key(iid, position.opened_at, check.quote.bar_start)
        if self.db.scalar(select(AgentDecision.id).where(AgentDecision.agent_id == self.agent.id,
                                                          AgentDecision.client_order_id == key)) is not None:
            return  # already submitted on this bar
        sellable = self.ctx.risk.snapshot(self.account, iid).sellable_quantity
        if sellable <= 0:
            return
        intent = OrderIntent(
            client_order_id=key, instrument_id=iid, side=OrderSide.SELL, order_type=OrderType.MARKET,
            quantity=sellable, source=OrderSource.AGENT, strategy_id=None, signal_at=check.quote.as_of,
        )
        try:
            result = self.orders.submit(self.agent.user_id, intent)
        except InvalidOrder as exc:
            report.notes.append(f"owed exit refused before risk: {exc}")
            return
        self._decision(
            iid, AgentDecisionKind.OWED_EXIT, O.EXIT_SUBMITTED, bar_start=check.quote.bar_start,
            detail=result.order.reject_reason or f"order {result.order.status.value}", result=result,
            explanation={"owed_exit_since": state.owed_exit_since.isoformat(), "reason": state.owed_exit_reason,
                         "quantity": sellable, "price_check": check.as_dict()},
        )
        report.submitted.append(key)
