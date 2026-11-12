"""Automatic journal entries, derived from the existing authoritative records. No new event pipeline.

Agent-X has no event bus: what happened is already recorded as rows (agent decisions and events, orders, risk
decisions, fills). Synchronisation scans a bounded window of those rows and inserts one AUTO entry per meaningful record,
keyed by a deterministic `source_key` with `INSERT ... ON CONFLICT DO NOTHING` on the unique (user_id, source_key)
index, so running it any number of times, concurrently or not, never duplicates an entry. It is run explicitly
(POST /api/v1/journal/sync); nothing hooks into, slows down or can fail the execution pipeline.

Entries store identifiers and labels only (`system_facts`, `outcome`); amounts are read from the authoritative rows when
an entry is shown, so the journal never becomes a second source of truth.
"""

from __future__ import annotations

import uuid
from collections import Counter
from datetime import date, datetime, time, timedelta

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.db.enums import (
    AgentDecisionKind,
    JournalEntryType,
    JournalSourceEvent as S,
    OrderSource,
    OrderStatus,
    RiskOutcome,
    TradeOutcome,
)
from app.db.models import AgentDecision, AgentEvent, Fill, Instrument, JournalEntry, Order, Position, RiskDecision
from app.db.scoping import owned_select
from app.explanations.facts import IST, SIGNAL_ACTIONS, plain, session_window
from app.explanations.trades import episodes, strategy_of
from app.strategies.base import BAR

SESSION_END = time(15, 30)
INTERVENTION_EVENTS = {
    "agent.error", "agent.paused_by_kill_switch", "agent.paused_execution_unavailable", "agent.kill_switch_engaged",
    "agent.reconciliation_failed", "agent.instrument_status_changed", "agent.instrument_data_error",
    "agent.owed_exit_expired_session_end", "agent.owed_exit_abandoned_on_stop",
}
LINKS = ("instrument_id", "strategy_id", "order_id", "fill_id", "position_id", "agent_decision_id", "agent_event_id",
         "config_version_id")


def _session(ts: datetime) -> date:
    return ts.astimezone(IST).date()



def collect(db: Session, user_id: uuid.UUID, first: date, last: date, now: datetime) -> list[dict]:
    """The AUTO entries the window's records call for (not yet deduplicated against stored entries)."""
    start, end = session_window(first)[0], session_window(last)[1]
    symbols = {i.id: i.symbol for i in db.scalars(select(Instrument))}
    positions = {p.instrument_id: p.id for p in db.scalars(owned_select(Position, user_id))}
    rows: list[dict] = []
    active_sessions: set[date] = set()

    def add(event: S, key: str, title: str, occurred_at: datetime, *, outcome=TradeOutcome.NONE, facts=None, **links):
        rows.append({"source_event": event, "source_key": key[:128], "title": title[:200], "occurred_at": occurred_at,
                     "session_date": _session(occurred_at), "outcome": outcome, "system_facts": plain(facts or {}),
                     **{k: links.get(k) for k in LINKS}})

    def sym(iid):
        return symbols.get(iid, f"#{iid}")

    # strategy signals (agent decisions on ENTER_LONG / EXIT_LONG)
    for d in db.scalars(owned_select(AgentDecision, user_id).where(
            AgentDecision.session_date >= first, AgentDecision.session_date <= last,
            AgentDecision.kind == AgentDecisionKind.EVALUATION, AgentDecision.action.in_(SIGNAL_ACTIONS))):
        entry = d.action == "ENTER_LONG"
        signal_at = d.bar_start + BAR
        active_sessions.add(d.session_date)
        add(S.ENTRY_SIGNAL if entry else S.EXIT_SIGNAL, f"SIGNAL:decision:{d.id}",
            f"{sym(d.instrument_id)} · {d.strategy_id} {'entry' if entry else 'exit'} signal ({d.reason}) · "
            f"{d.outcome.value}", signal_at,
            facts={"strategy_id": d.strategy_id, "config_id": d.config_id, "action": d.action, "reason": d.reason,
                   "decision_outcome": d.outcome, "order_status": d.order_status, "signal_bar_start": d.bar_start},
            instrument_id=d.instrument_id, strategy_id=d.strategy_id, agent_decision_id=d.id,
            config_version_id=d.config_version_id, order_id=d.order_id)

    # orders: rejections, cancellations, protective exits, manual trades
    orders = list(db.scalars(owned_select(Order, user_id).where(Order.accepted_at >= start, Order.accepted_at < end)))
    risk = {r.order_id: r for r in db.scalars(owned_select(RiskDecision, user_id).where(
        RiskDecision.order_id.in_([o.id for o in orders])))} if orders else {}
    for o in orders:
        active_sessions.add(_session(o.accepted_at))
        common = dict(instrument_id=o.instrument_id, strategy_id=strategy_of(o.strategy_id), order_id=o.id,
                      position_id=positions.get(o.instrument_id))
        label = f"{sym(o.instrument_id)} · {o.source.value} {o.side.value} {o.requested_quantity}"
        facts = {"source": o.source, "side": o.side, "order_type": o.order_type, "status": o.status,
                 "client_order_id": o.client_order_id, "reject_reason": (o.reject_reason or "")[:500] or None,
                 "cancel_reason": (o.cancel_reason or "")[:500] or None}
        if o.status is OrderStatus.REJECTED:
            rd = risk.get(o.id)
            by_risk = rd is not None and rd.outcome is RiskOutcome.REJECTED
            add(S.RISK_REJECTION if by_risk else S.ORDER_REJECTED, f"REJECTED:order:{o.id}",
                f"{label} · rejected{' by risk' if by_risk else ''}", o.accepted_at, outcome=TradeOutcome.REJECTED,
                facts={**facts, "risk_decision_id": rd.id if rd else None,
                       "risk_reasons": (rd.reasons if rd else None)}, **common)
        elif o.status is OrderStatus.CANCELLED:
            # dated by acceptance (trading clock): updated_at is wall-clock processing time
            add(S.ORDER_CANCELLED, f"CANCELLED:order:{o.id}", f"{label} · cancelled", o.accepted_at,
                outcome=TradeOutcome.CANCELLED, facts=facts, **common)
        if o.source is OrderSource.RISK_EXIT:
            add(S.PROTECTIVE_EXIT, f"PROTECTIVE_EXIT:order:{o.id}", f"{label} · protective exit ({o.status.value})",
                o.accepted_at, facts=facts, **common)
        if o.source is OrderSource.MANUAL and o.filled_quantity > 0:
            add(S.MANUAL_TRADE, f"MANUAL_TRADE:order:{o.id}", f"{label} · manual trade ({o.status.value})",
                o.accepted_at, facts=facts, **common)

    # partial fills: a fill after which its order was still incomplete
    fills = list(db.scalars(owned_select(Fill, user_id).where(Fill.price_as_of >= start, Fill.price_as_of < end)))
    if fills:
        order_rows = {o.id: o for o in db.scalars(owned_select(Order, user_id).where(
            Order.id.in_({f.order_id for f in fills})))}
        all_fills = list(db.scalars(owned_select(Fill, user_id).where(Fill.order_id.in_(order_rows)).order_by(
            Fill.order_id, Fill.seq)))
        running: Counter = Counter()
        in_window = {f.id for f in fills}
        for f in all_fills:
            running[f.order_id] += f.quantity
            o = order_rows[f.order_id]
            if f.id not in in_window:
                continue
            active_sessions.add(_session(f.price_as_of))
            if running[f.order_id] < o.quantity:
                add(S.PARTIAL_FILL, f"PARTIAL_FILL:fill:{f.id}",
                    f"{sym(f.instrument_id)} · partial fill {running[f.order_id]} of {o.quantity} ({o.side.value})",
                    f.price_as_of, facts={"order_status": o.status, "fill_seq": f.seq, "filled_after": running[f.order_id],
                                          "order_quantity": o.quantity},
                    instrument_id=f.instrument_id, order_id=o.id, fill_id=f.id, strategy_id=strategy_of(o.strategy_id),
                    position_id=positions.get(f.instrument_id))

    # closed trades (position episodes that returned to flat inside the window)
    for e in episodes(db, user_id):
        if not e.closed or not (start <= e.closed_at < end):
            continue
        key = (f"TRADE_CLOSED:fill:{e.closing_fill_id}" if e.closing_fill_id
               else f"TRADE_CLOSED:adjustment:{e.adjustment_ids[-1]}")
        exit_by = "/".join(sorted(set(e.exit_sources))) or "ADJUSTMENT"
        add(S.TRADE_CLOSED, key, f"{sym(e.instrument_id)} · trade closed · {e.outcome.value} "
            f"({e.strategy_id or e.entry_source}, exit by {exit_by})", e.closed_at, outcome=e.outcome,
            facts={"opened_at": e.opened_at, "closed_at": e.closed_at, "fill_ids": e.fill_ids,
                   "entry_order_ids": e.entry_order_ids, "exit_order_ids": e.exit_order_ids,
                   "entry_source": e.entry_source, "exit_sources": e.exit_sources, "adjustment_ids": e.adjustment_ids,
                   "outcome_basis": "sign of stored realized P&L minus stored fees of the trade's fills"},
            instrument_id=e.instrument_id, strategy_id=e.strategy_id, order_id=e.closing_order_id,
            fill_id=e.closing_fill_id, position_id=positions.get(e.instrument_id))

    # agent interventions
    for ev in db.scalars(owned_select(AgentEvent, user_id).where(
            AgentEvent.occurred_at >= start, AgentEvent.occurred_at < end, AgentEvent.event_type.in_(INTERVENTION_EVENTS))):
        details = ev.details or {}
        if ev.event_type == "agent.instrument_status_changed" and details.get("to") == "OK":
            continue
        active_sessions.add(_session(ev.occurred_at))
        what = details.get("to") or ev.to_status or ev.event_type.removeprefix("agent.")
        where = f"{sym(ev.instrument_id)} · " if ev.instrument_id else ""
        add(S.AGENT_INTERVENTION, f"AGENT_EVENT:event:{ev.id}", f"{where}agent {ev.event_type.removeprefix('agent.')} "
            f"({what})", ev.occurred_at,
            facts={"event_type": ev.event_type, "actor": ev.actor, "from_status": ev.from_status,
                   "to_status": ev.to_status, "to": details.get("to"), "detail": str(details.get("detail") or "")[:500] or None},
            instrument_id=ev.instrument_id, agent_event_id=ev.id)

    # one summary per finished session with activity
    for d in sorted(active_sessions):
        if first <= d <= last and now >= datetime.combine(d, SESSION_END, IST):
            add(S.SESSION_SUMMARY, f"SESSION_SUMMARY:{d.isoformat()}", f"Session summary · {d.isoformat()}",
                datetime.combine(d, SESSION_END, IST), facts={"session_date": d})
    return rows


def sync(db: Session, user_id: uuid.UUID, first: date, last: date, now: datetime) -> dict:
    """Insert the window's missing AUTO entries. Idempotent. Returns counts by source event (created only)."""
    rows = collect(db, user_id, first, last, now)
    created: Counter = Counter()
    for i in range(0, len(rows), 500):
        chunk = [{**r, "user_id": user_id, "entry_type": JournalEntryType.AUTO} for r in rows[i:i + 500]]
        stmt = (insert(JournalEntry).values(chunk)
                .on_conflict_do_nothing(index_elements=["user_id", "source_key"], index_where=JournalEntry.source_key.is_not(None))
                .returning(JournalEntry.source_event))
        created.update(e.value if hasattr(e, "value") else e for e in db.scalars(stmt))
    return {"from": first.isoformat(), "to": last.isoformat(), "candidates": len(rows),
            "created": sum(created.values()), "created_by_event": dict(created)}


def default_window(today: date, days: int = 30) -> tuple[date, date]:
    return today - timedelta(days=days - 1), today
