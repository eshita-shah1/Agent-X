"""Verified facts for explanations, read from the authoritative records only (no writes, no recalculation).

Each builder returns `Facts(facts, evidence)`:
  * `facts`: the JSON-ready content an explanation may state, kept in separate layers (strategy signal, agent decision,
    risk decision, order, execution, outcome) so they are never collapsed into one story;
  * `evidence`: the identifiers of every row used (instrument, candle and observation version, configuration version,
    decision, order, fills, risk decision, events, provider and price basis), stored with the explanation.

Figures are copied from stored columns (orders, fills, risk decisions, agent decisions) or from the existing
calculation service (`daily_pnl`, `PortfolioReader.valuation`). Indicator values and thresholds come from the decision's
stored `values` and the configuration version's stored parameters (merged with the strategy's code defaults, which is
how `build_config` constructs the strategy). Nothing personal (email, account id) is included.
"""

from __future__ import annotations

import enum
import uuid
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.enums import AgentDecisionKind, OrderSource, OrderStatus, RiskOutcome
from app.db.models import (
    AgentConfigVersion,
    AgentCycle,
    AgentDecision,
    AgentEvent,
    AgentInstance,
    Candle,
    Fill,
    Instrument,
    JournalEntry,
    JournalReflection,
    Order,
    Position,
    ProtectiveExitRequirement,
    RiskDecision,
)
from app.db.scoping import owned_select
from app.exchange.portfolio import daily_pnl
from app.explanations.trades import episodes, strategy_of
from app.strategies.registry import STRATEGIES

IST = ZoneInfo("Asia/Kolkata")
SIGNAL_ACTIONS = ("ENTER_LONG", "EXIT_LONG")
# Agent events worth surfacing in summaries and the journal (the rest are routine bookkeeping).
NOTABLE_EVENTS = {
    "agent.started", "agent.stopped", "agent.paused", "agent.resumed", "agent.error", "agent.paused_by_kill_switch",
    "agent.paused_execution_unavailable", "agent.kill_switch_engaged", "agent.reconciliation_failed",
    "agent.instrument_status_changed", "agent.instrument_data_error", "agent.owed_exit_expired_session_end",
    "agent.owed_exit_abandoned_on_stop", "agent.cycle_failed", "agent.cycle_interrupted",
}


@dataclass(frozen=True)
class Facts:
    facts: dict
    evidence: dict


def plain(value: Any) -> Any:
    """JSON-ready copy: Decimals as exact strings, instants as ISO-8601 UTC, enums as values."""
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, (date, time)):
        return value.isoformat()
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, enum.Enum):
        return value.value
    if isinstance(value, dict):
        return {str(k): plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [plain(v) for v in value]
    return value


def ist(ts: datetime | None) -> str | None:
    return ts.astimezone(IST).strftime("%Y-%m-%d %H:%M IST") if ts else None


def session_window(d: date) -> tuple[datetime, datetime]:
    start = datetime.combine(d, time(0), IST)
    return start, start + timedelta(days=1)


def _symbols(db: Session, ids) -> dict[int, str]:
    ids = {i for i in ids if i is not None}
    if not ids:
        return {}
    return {i.id: i.symbol for i in db.scalars(select(Instrument).where(Instrument.id.in_(ids)))}


def _instrument(db: Session, instrument_id: int | None) -> dict | None:
    if instrument_id is None:
        return None
    inst = db.get(Instrument, instrument_id)
    return {"id": instrument_id, "symbol": inst.symbol if inst else None, "exchange": inst.exchange if inst else None}


def configured_parameters(version: AgentConfigVersion | None, config_id: str | None) -> dict | None:
    """The candidate's stored parameters merged over the strategy's code defaults (as `build_config` does)."""
    if version is None or config_id is None:
        return None
    candidate = next((c for c in version.candidates if c.get("config_id") == config_id), None)
    if candidate is None:
        return None
    definition = STRATEGIES.get(candidate.get("strategy_id"))
    params = dict(plain(definition.defaults())) if definition else {}
    params.update(plain(candidate.get("params") or {}))
    return {"config_id": config_id, "strategy_id": candidate.get("strategy_id"), "priority": candidate.get("priority"),
            "parameters": params, "parameters_from_user": sorted((candidate.get("params") or {}).keys())}


def _risk(rd: RiskDecision | None) -> dict | None:
    if rd is None:
        return None
    failed = [c for c in (rd.checks or []) if isinstance(c, dict) and c.get("passed") is False]
    return plain({
        "id": rd.id, "outcome": rd.outcome, "side": rd.side, "proposed_quantity": rd.proposed_quantity,
        "approved_quantity": rd.approved_quantity, "sizing_price": rd.sizing_price, "stop_loss_price": rd.stop_loss_price,
        "risk_amount": rd.risk_amount, "equity": rd.equity, "reasons": rd.reasons, "failed_checks": failed,
        "evaluated_at": rd.evaluated_at,
    })


def _order(o: Order | None) -> dict | None:
    if o is None:
        return None
    return plain({
        "id": o.id, "client_order_id": o.client_order_id, "source": o.source, "strategy": o.strategy_id, "side": o.side,
        "order_type": o.order_type, "product": o.product, "requested_quantity": o.requested_quantity,
        "quantity": o.quantity, "filled_quantity": o.filled_quantity, "status": o.status, "limit_price": o.limit_price,
        "stop_loss_price": o.stop_loss_price, "take_profit_price": o.take_profit_price, "avg_fill_price": o.avg_fill_price,
        "reject_reason": o.reject_reason, "cancel_reason": o.cancel_reason, "signal_at": o.signal_at,
        "accepted_at": o.accepted_at, "decision_price_ref": o.decision_price_ref, "execution_policy": o.execution_policy,
        "execution_approval_reference": o.execution_approval_reference,
    })


def _slippage_rate(f: Fill) -> Any:
    rates = (f.cost_model or {}).get("rates") or {}
    return next((v for k, v in rates.items() if k.startswith("SLIPPAGE") and f.side.value in k), None)


def _fills(fs: list[Fill]) -> list[dict]:
    return [plain({
        "id": f.id, "seq": f.seq, "side": f.side, "quantity": f.quantity, "reference_price": f.reference_price,
        "executed_price": f.price, "slippage_rate": _slippage_rate(f), "turnover": f.turnover, "fee": f.fee,
        "fee_breakdown": f.fee_breakdown, "realized_pnl": f.realized_pnl, "price_source_ref": f.price_source_ref,
        "provider_code": f.provider_code, "price_basis": f.price_basis, "execution_policy": f.execution_policy,
        "price_as_of": f.price_as_of, "executed_at": f.executed_at,
    }) for f in fs]


def _order_children(db: Session, user_id: uuid.UUID, order: Order) -> tuple[RiskDecision | None, list[Fill]]:
    rd = db.scalar(owned_select(RiskDecision, user_id).where(RiskDecision.order_id == order.id))
    fs = list(db.scalars(owned_select(Fill, user_id).where(Fill.order_id == order.id).order_by(Fill.seq)))
    return rd, fs


# ---- agent decision ---------------------------------------------------------------------------------------------


def decision_facts(db: Session, user_id: uuid.UUID, decision_id: int) -> Facts | None:
    d = db.scalar(owned_select(AgentDecision, user_id).where(AgentDecision.id == decision_id))
    if d is None:
        return None
    cycle = db.scalar(owned_select(AgentCycle, user_id).where(AgentCycle.id == d.cycle_id))
    version = db.scalar(owned_select(AgentConfigVersion, user_id).where(AgentConfigVersion.id == d.config_version_id))
    candle = db.get(Candle, d.candle_id) if d.candle_id else None
    selection = db.scalar(owned_select(AgentDecision, user_id).where(
        AgentDecision.agent_id == d.agent_id, AgentDecision.instrument_id == d.instrument_id,
        AgentDecision.session_date == d.session_date, AgentDecision.kind == AgentDecisionKind.SELECTION))
    order = db.scalar(owned_select(Order, user_id).where(Order.id == d.order_id)) if d.order_id else None
    rd, fs = _order_children(db, user_id, order) if order else (None, [])

    signal = None
    if d.action:
        signal = plain({
            "strategy_id": d.strategy_id, "strategy_version": d.strategy_version, "config_id": d.config_id,
            "action": d.action, "reason": d.reason, "indicator_values": d.values, "timeframe": "5m",
            "signal_bar_start": d.bar_start, "signal_bar_start_ist": ist(d.bar_start),
        })
    regime = None
    if selection is not None:
        ex = selection.explanation or {}
        regime = plain({
            "decision_id": selection.id, "outcome": selection.outcome, "reason": selection.detail,
            "selected_config_id": selection.config_id, "regime": (ex.get("regime") or {}).get("regime"),
            "regime_detail": ex.get("regime"), "mode": ex.get("mode"),
            "candidates": [{k: c.get(k) for k in ("config_id", "strategy_id", "priority", "outcome", "detail")}
                           for c in ex.get("candidates") or []],
        })
    facts = {
        "kind": "AGENT_DECISION",
        "paper_trading_only": True,
        "instrument": _instrument(db, d.instrument_id),
        "session_date": d.session_date.isoformat(),
        "strategy_signal": signal,
        "selection": regime,
        "configuration": {
            "version": version.version if version else None, "selection_mode": version.selection_mode if version else None,
            "candidate": configured_parameters(version, d.config_id),
        },
        "agent_decision": plain({
            "id": d.id, "kind": d.kind, "outcome": d.outcome, "detail": d.detail, "explanation": d.explanation,
            "decided_at": d.decided_at, "decided_at_ist": ist(d.decided_at),
            "agent_mode_at_cycle": cycle.mode if cycle else None, "agent_status_at_cycle": cycle.agent_status if cycle else None,
        }),
        "candle": plain({
            "id": candle.id, "bar_start": candle.bar_start_ts, "open": candle.open, "high": candle.high, "low": candle.low,
            "close": candle.close, "volume": candle.volume, "observed_at": candle.observed_at,
            "observation_version": candle.observation_version, "is_canonical": candle.is_canonical,
            "price_basis": candle.price_basis,
        }) if candle else None,
        "risk_decision": _risk(rd),
        "order": _order(order),
        "execution": {"fills": _fills(fs)} if order else None,
    }
    evidence = plain({
        "instrument_id": d.instrument_id, "symbol": (facts["instrument"] or {}).get("symbol"), "agent_decision_id": d.id,
        "cycle_id": d.cycle_id, "config_version_id": d.config_version_id,
        "config_version": version.version if version else None, "strategy_id": d.strategy_id, "config_id": d.config_id,
        "candle_id": d.candle_id, "observation_version": d.observation_version, "provider_code": d.provider_code,
        "price_basis": d.price_basis, "selection_decision_id": selection.id if selection else None,
        "order_id": d.order_id, "risk_decision_id": d.risk_decision_id, "fill_ids": [f.id for f in fs],
        "timestamp": d.decided_at,
    })
    return Facts(facts, evidence)


# ---- order -------------------------------------------------------------------------------------------------------


def order_facts(db: Session, user_id: uuid.UUID, order_id: uuid.UUID, marks=None) -> Facts | None:
    """`marks(position)` -> (unrealized_pnl, mark, reason) from PortfolioReader; None skips unrealized P&L."""
    order = db.scalar(owned_select(Order, user_id).where(Order.id == order_id))
    if order is None:
        return None
    rd, fs = _order_children(db, user_id, order)
    decision = db.scalar(owned_select(AgentDecision, user_id).where(AgentDecision.order_id == order.id))
    requirement = db.scalar(owned_select(ProtectiveExitRequirement, user_id).where(
        ProtectiveExitRequirement.exit_order_id == order.id))
    position = db.scalar(owned_select(Position, user_id).where(Position.instrument_id == order.instrument_id))
    position_facts = None
    if position is not None:
        position_facts = plain({
            "id": position.id, "quantity_now": position.quantity, "avg_entry_price": position.avg_entry_price,
            "realized_pnl_to_date": position.realized_pnl, "fees_paid_to_date": position.fees_paid,
            "stop_loss_price": position.stop_loss_price, "take_profit_price": position.take_profit_price,
        })
        if position.quantity > 0 and marks is not None:
            unrealized, mark, reason = marks(position)
            position_facts.update(plain({"unrealized_pnl_now": unrealized, "mark_price_now": mark,
                                         "unrealized_unavailable_reason": reason}))
    trade = next((e for e in episodes(db, user_id, order.instrument_id)
                  if order.id in e.entry_order_ids or order.id in e.exit_order_ids), None)
    facts = {
        "kind": "ORDER",
        "paper_trading_only": True,
        "instrument": _instrument(db, order.instrument_id),
        "agent_decision": plain({"id": decision.id, "outcome": decision.outcome, "strategy_id": decision.strategy_id,
                                 "reason": decision.reason, "bar_start_ist": ist(decision.bar_start)}) if decision else None,
        "protective_exit": plain({"requirement_id": requirement.id, "reason_code": requirement.reason_code,
                                  "detail": requirement.detail}) if requirement else (
            {"source": "RISK_EXIT"} if order.source is OrderSource.RISK_EXIT else None),
        "risk_decision": _risk(rd),
        "order": _order(order),
        "execution": {"fills": _fills(fs), "fill_count": len(fs),
                      "fees_total_stored": str(sum((f.fee for f in fs), Decimal(0))) if fs else None},
        "position": position_facts,
        "trade": plain({
            "status": "CLOSED" if trade.closed else "OPEN", "outcome": trade.outcome, "opened_at": trade.opened_at,
            "closed_at": trade.closed_at, "realized_pnl": trade.realized_pnl, "fees": trade.fees,
            "net_realized_pnl": trade.net_realized_pnl, "fill_ids": trade.fill_ids,
        }) if trade else None,
    }
    evidence = plain({
        "instrument_id": order.instrument_id, "symbol": (facts["instrument"] or {}).get("symbol"), "order_id": order.id,
        "client_order_id": order.client_order_id, "risk_decision_id": rd.id if rd else None,
        "fill_ids": [f.id for f in fs], "agent_decision_id": decision.id if decision else None,
        "protective_exit_requirement_id": requirement.id if requirement else None,
        "position_id": position.id if position else None,
        "price_sources": sorted({f"{f.provider_code}:{f.price_basis.value}:{f.price_source_ref}" for f in fs}),
        "timestamp": order.accepted_at,
    })
    return Facts(facts, evidence)


# ---- agent event -------------------------------------------------------------------------------------------------


def event_facts(db: Session, user_id: uuid.UUID, event_id: int) -> Facts | None:
    e = db.scalar(owned_select(AgentEvent, user_id).where(AgentEvent.id == event_id))
    if e is None:
        return None
    agent = db.scalar(owned_select(AgentInstance, user_id).where(AgentInstance.id == e.agent_id))
    facts = {
        "kind": "AGENT_EVENT",
        "paper_trading_only": True,
        "instrument": _instrument(db, e.instrument_id),
        "event": plain({"id": e.id, "event_type": e.event_type, "actor": e.actor, "from_status": e.from_status,
                        "to_status": e.to_status, "details": e.details, "occurred_at": e.occurred_at,
                        "occurred_at_ist": ist(e.occurred_at), "cycle_id": e.cycle_id}),
        "agent_now": plain({"status": agent.status, "mode": agent.mode, "status_reason": agent.status_reason}) if agent else None,
    }
    evidence = plain({"agent_event_id": e.id, "cycle_id": e.cycle_id, "instrument_id": e.instrument_id,
                      "symbol": (facts["instrument"] or {}).get("symbol"), "timestamp": e.occurred_at})
    return Facts(facts, evidence)


# ---- daily summary -----------------------------------------------------------------------------------------------


def daily_facts(db: Session, user_id: uuid.UUID, session: date) -> Facts:
    start, end = session_window(session)
    decisions = list(db.scalars(owned_select(AgentDecision, user_id).where(AgentDecision.session_date == session)))
    signals = [d for d in decisions if d.kind is AgentDecisionKind.EVALUATION and d.action in SIGNAL_ACTIONS]
    orders = list(db.scalars(owned_select(Order, user_id).where(Order.accepted_at >= start, Order.accepted_at < end)))
    risk_rows = list(db.scalars(owned_select(RiskDecision, user_id).where(
        RiskDecision.evaluated_at >= start, RiskDecision.evaluated_at < end)))
    fills = list(db.scalars(owned_select(Fill, user_id).where(Fill.price_as_of >= start, Fill.price_as_of < end)))
    closed = [e for e in episodes(db, user_id) if e.closed and start <= e.closed_at < end]
    events = list(db.scalars(owned_select(AgentEvent, user_id).where(
        AgentEvent.occurred_at >= start, AgentEvent.occurred_at < end).order_by(AgentEvent.occurred_at, AgentEvent.id)))
    pnl = daily_pnl(db, user_id, session)
    reflections = db.scalar(
        select(func.count()).select_from(JournalReflection).join(JournalEntry, JournalEntry.id == JournalReflection.entry_id)
        .where(JournalReflection.user_id == user_id, JournalEntry.session_date == session))
    open_positions = db.scalar(select(func.count()).select_from(Position).where(
        Position.user_id == user_id, Position.quantity > 0))
    symbols = _symbols(db, [d.instrument_id for d in decisions] + [o.instrument_id for o in orders]
                       + [f.instrument_id for f in fills] + [e.instrument_id for e in events])

    per_strategy: dict[str, Counter] = defaultdict(Counter)
    per_instrument: dict[int, dict] = {}

    def inst(iid: int) -> dict:
        return per_instrument.setdefault(iid, {"symbol": symbols.get(iid), "signals": 0, "orders": 0, "fills": 0,
                                               "risk_rejections": 0, "closed_trades": 0, "realized_pnl": Decimal(0),
                                               "fees": Decimal(0)})

    for d in signals:
        per_strategy[d.strategy_id or "UNKNOWN"]["signals"] += 1
        per_strategy[d.strategy_id or "UNKNOWN"][f"signal_outcome:{d.outcome.value}"] += 1
        inst(d.instrument_id)["signals"] += 1
    for o in orders:
        per_strategy[strategy_of(o.strategy_id) or f"({o.source.value} orders)"]["orders"] += 1
        inst(o.instrument_id)["orders"] += 1
    for f in fills:
        row = inst(f.instrument_id)
        row["fills"] += 1
        row["fees"] += f.fee
        row["realized_pnl"] += f.realized_pnl or Decimal(0)
    rejected_by_risk = [r for r in risk_rows if r.outcome is RiskOutcome.REJECTED]
    for r in rejected_by_risk:
        inst(r.instrument_id)["risk_rejections"] += 1
    for e in closed:
        key = e.strategy_id or f"({e.entry_source} trades)"
        per_strategy[key]["closed_trades"] += 1
        per_strategy[key][f"closed:{e.outcome.value}"] += 1
        inst(e.instrument_id)["closed_trades"] += 1
    status_counts = Counter(o.status.value for o in orders)
    reasons = Counter(str(reason) for r in rejected_by_risk for reason in (r.reasons or []))
    notable = [e for e in events if e.event_type in NOTABLE_EVENTS]
    outcomes = Counter(e.outcome.value for e in closed)

    facts = plain({
        "kind": "DAILY_SUMMARY",
        "paper_trading_only": True,
        "session_date": session.isoformat(),
        "signals": {"total": len(signals), "entry": sum(d.action == "ENTER_LONG" for d in signals),
                    "exit": sum(d.action == "EXIT_LONG" for d in signals),
                    "by_outcome": dict(Counter(d.outcome.value for d in signals))},
        "agent_decisions": {"total": len(decisions), "by_kind": dict(Counter(d.kind.value for d in decisions)),
                            "by_outcome": dict(Counter(d.outcome.value for d in decisions))},
        "orders": {"total": len(orders), "by_status": dict(status_counts),
                   "by_source": dict(Counter(o.source.value for o in orders)),
                   "rejected": status_counts.get(OrderStatus.REJECTED.value, 0),
                   "cancelled": status_counts.get(OrderStatus.CANCELLED.value, 0),
                   "note": "Orders are not trades: a closed trade is a position that went from flat to open to flat."},
        "risk": {"evaluations": len(risk_rows), "rejections": len(rejected_by_risk), "rejection_reasons": dict(reasons)},
        "fills": len(fills),
        "closed_trades": {"total": len(closed), "winning": outcomes.get("WIN", 0), "losing": outcomes.get("LOSS", 0),
                          "breakeven": outcomes.get("BREAKEVEN", 0), "unknown": outcomes.get("UNKNOWN", 0),
                          "classification": "by realized P&L net of the trade's stored fill fees"},
        "realized": {"source": "daily_pnl: fills by simulated fill time (price_as_of), adjustments by ex-date",
                     "realized_pnl_gross": pnl.realized_pnl, "fees": pnl.fees, "net_realized_pnl": pnl.net_realized_pnl,
                     "fills": pnl.fills, "adjustments": pnl.adjustments,
                     "unattributed_legacy_fills": pnl.unattributed_legacy_fills,
                     "dividend_income": pnl.dividend_income, "dividends": pnl.dividends},
        "unrealized": {"included": False, "open_positions_now": open_positions,
                       "note": "Unrealized P&L is a current mark of open positions (GET /api/v1/portfolio), not a "
                               "result of this session."},
        "by_strategy": {k: dict(v) for k, v in sorted(per_strategy.items())},
        "by_instrument": {str(k): v for k, v in sorted(per_instrument.items())},
        "agent_events": [{"id": e.id, "event_type": e.event_type, "at_ist": ist(e.occurred_at),
                          "instrument": symbols.get(e.instrument_id), "to_status": e.to_status,
                          "reason": (e.details or {}).get("detail") or (e.details or {}).get("code")
                          or (e.details or {}).get("to")} for e in notable[:25]],
        "journal_reflections": reflections,
    })
    evidence = plain({
        "session_date": session, "agent_decision_ids": [d.id for d in signals][:200],
        "order_ids": [o.id for o in orders][:200], "risk_decision_ids": [r.id for r in rejected_by_risk][:200],
        "fill_ids": [f.id for f in fills][:500], "agent_event_ids": [e.id for e in notable][:200],
        "closed_trade_fill_ids": [e.closing_fill_id for e in closed], "pnl_service": "app.exchange.portfolio.daily_pnl",
    })
    return Facts(facts, evidence)
