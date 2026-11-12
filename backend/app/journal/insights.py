"""Journal analytics: deterministic counts over persisted records, with sample sizes and caveats (Feature C).

Every number here is a count, a sum of stored values, or a share computed in code from those counts; an optional LLM
narrative may only reword them (see app.llm.narrator). Nothing here feeds back into strategies, selection, risk
settings or the agent: the analytics are read-only and advisory, like backtests.
"""

from __future__ import annotations

import re
import uuid
from collections import Counter, defaultdict
from datetime import date
from decimal import ROUND_HALF_EVEN, Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.enums import AgentDecisionKind, OrderStatus, RiskOutcome
from app.db.models import AgentDecision, Instrument, JournalEntry, JournalEntryTag, JournalReflection, JournalTag, Order, RiskDecision
from app.db.scoping import owned_select
from app.explanations.facts import IST, SIGNAL_ACTIONS, plain, session_window
from app.explanations.trades import episodes

SMALL_SAMPLE = 30
STOPWORDS = set("""about above after again against also because been before being below between both could didn does
doing down during each from further have having here into itself just more most much must once only other over same
should some such than that their them then there these they this those through under until very were what when where
which while with would your yours trade trades traded trading market price entry exit stop really""".split())


def _share(part: int, whole: int) -> str | None:
    if whole == 0:
        return None
    return str((Decimal(part) * 100 / Decimal(whole)).quantize(Decimal("0.01"), rounding=ROUND_HALF_EVEN))


def _outcomes(items) -> dict:
    c = Counter(items)
    n = sum(c.values())
    return {"n": n, "winning": c.get("WIN", 0), "losing": c.get("LOSS", 0), "breakeven": c.get("BREAKEVEN", 0),
            "unknown": c.get("UNKNOWN", 0), "win_share_pct": _share(c.get("WIN", 0), n)}


def analytics(db: Session, user_id: uuid.UUID, first: date, last: date) -> tuple[dict, list[str]]:
    """(analytics facts, the user's reflection texts: untrusted, sent to an LLM only if enabled)."""
    start, end = session_window(first)[0], session_window(last)[1]
    symbols = {i.id: i.symbol for i in db.scalars(select(Instrument))}

    # closed trades and the regime of the session each was opened in (from the agent's locked selection, if any)
    closed = [e for e in episodes(db, user_id) if e.closed and start <= e.closed_at < end]
    selections = {
        (d.instrument_id, d.session_date): ((d.explanation or {}).get("regime") or {}).get("regime")
        for d in db.scalars(owned_select(AgentDecision, user_id).where(
            AgentDecision.kind == AgentDecisionKind.SELECTION, AgentDecision.session_date <= last))
    }
    by_strategy, by_regime, by_instrument = defaultdict(list), defaultdict(list), defaultdict(list)
    exits: Counter = Counter()
    net_by_strategy: dict[str, Decimal] = defaultdict(Decimal)
    for e in closed:
        strategy = e.strategy_id or f"({e.entry_source})"
        regime = selections.get((e.instrument_id, e.opened_at.astimezone(IST).date())) or "UNKNOWN"
        by_strategy[strategy].append(e.outcome.value)
        by_regime[f"{strategy} | {regime}"].append(e.outcome.value)
        by_instrument[symbols.get(e.instrument_id, str(e.instrument_id))].append(e.outcome.value)
        exits["/".join(sorted(set(e.exit_sources))) or "ADJUSTMENT"] += 1
        if e.net_realized_pnl is not None:
            net_by_strategy[strategy] += e.net_realized_pnl

    # signals versus what was executed
    decisions = list(db.scalars(owned_select(AgentDecision, user_id).where(
        AgentDecision.session_date >= first, AgentDecision.session_date <= last,
        AgentDecision.kind == AgentDecisionKind.EVALUATION, AgentDecision.action.in_(SIGNAL_ACTIONS))))
    suppressed_by_instrument: Counter = Counter(
        symbols.get(d.instrument_id, str(d.instrument_id)) for d in decisions if d.outcome.value.startswith("SUPPRESSED"))
    orders = list(db.scalars(owned_select(Order, user_id).where(Order.accepted_at >= start, Order.accepted_at < end)))
    risk_rejections: Counter = Counter(
        symbols.get(r.instrument_id, str(r.instrument_id)) for r in db.scalars(owned_select(RiskDecision, user_id).where(
            RiskDecision.evaluated_at >= start, RiskDecision.evaluated_at < end, RiskDecision.outcome == RiskOutcome.REJECTED)))

    # the user's own journal
    entries = list(db.scalars(owned_select(JournalEntry, user_id).where(
        JournalEntry.session_date >= first, JournalEntry.session_date <= last)))
    entry_ids = [e.id for e in entries]
    tags: Counter = Counter()
    reflections: list[JournalReflection] = []
    if entry_ids:
        tags.update(name for (name,) in db.execute(
            select(JournalTag.name).join(JournalEntryTag, JournalEntryTag.tag_id == JournalTag.id)
            .where(JournalEntryTag.user_id == user_id, JournalEntryTag.entry_id.in_(entry_ids))))
        reflections = list(db.scalars(owned_select(JournalReflection, user_id).where(JournalReflection.entry_id.in_(entry_ids))))
    words: Counter = Counter()
    for text in [r.mistakes for r in reflections] + [e.went_wrong for e in entries]:
        words.update(w for w in re.findall(r"[a-z]{4,}", (text or "").lower()) if w not in STOPWORDS)

    def yes_no(attr: str) -> dict:
        values = [getattr(r, attr) for r in reflections]
        return {"yes": values.count(True), "no": values.count(False), "not_answered": values.count(None)}

    n = len(closed)
    caveats = [f"Based on {n} closed paper trades between {first.isoformat()} and {last.isoformat()}."]
    if n < SMALL_SAMPLE:
        caveats.append(f"Fewer than {SMALL_SAMPLE} closed trades: too few to draw conclusions; shares can change "
                       "sharply with one more trade.")
    caveats += [
        "No statistical significance test was performed.",
        "Associations between regimes, strategies, instruments and outcomes are correlations, not causes.",
        "Paper trading on unverified provider data; past results do not predict future results.",
        "These analytics never change strategies, risk settings or the agent.",
    ]
    overall = _outcomes(e.outcome.value for e in closed)
    return plain({
        "kind": "JOURNAL_INSIGHTS",
        "paper_trading_only": True,
        "period": {"from": first, "to": last},
        "closed_trades": {"total": n, **{k: v for k, v in overall.items() if k != "n"}},
        "by_strategy": {k: {**_outcomes(v), "net_realized_pnl_sum": net_by_strategy.get(k)} for k, v in sorted(by_strategy.items())},
        "by_strategy_and_regime": {k: _outcomes(v) for k, v in sorted(by_regime.items())},
        "by_instrument": {k: _outcomes(v) for k, v in sorted(by_instrument.items())},
        "exit_sources": dict(exits),
        "signals_vs_execution": {
            "signals": len(decisions), "by_decision_outcome": dict(Counter(d.outcome.value for d in decisions)),
            "orders": len(orders), "orders_filled": sum(o.status is OrderStatus.FILLED for o in orders),
            "orders_rejected": sum(o.status is OrderStatus.REJECTED for o in orders),
            "orders_cancelled": sum(o.status is OrderStatus.CANCELLED for o in orders),
        },
        "suppressed_signals_by_instrument": dict(suppressed_by_instrument.most_common()),
        "risk_rejections_by_instrument": dict(risk_rejections.most_common()),
        "journal": {
            "entries": len(entries), "manual": sum(e.entry_type.value == "MANUAL" for e in entries),
            "reflections": len(reflections), "tags": dict(tags.most_common(20)),
            "reflection_answers": {"followed_strategy_rules": yes_no("followed_strategy_rules"),
                                   "risk_limits_respected": yes_no("risk_limits_respected"),
                                   "exit_followed_strategy": yes_no("exit_followed_strategy")},
            "frequent_words_in_mistakes": dict(words.most_common(10)),
            "note": "Word counts from your own notes, not a diagnosis.",
        },
        "caveats": caveats,
    }), [r.mistakes or "" for r in reflections] + [r.lessons or "" for r in reflections]
