"""Deterministic, rule-based explanations: fixed templates filled only from the facts (pure; no database, no LLM).

This is the explanation of record and the fallback whenever no LLM narrative exists. Every value it prints is copied
from the facts as stored (no rounding, no arithmetic). Output is a list of (layer, sentence) pairs so the strategy
signal, agent decision, risk decision, order, execution and outcome stay separate.
"""

from __future__ import annotations

from typing import Any

GENERATOR_VERSION = "EXPLAIN_V1"

Sections = list[tuple[str, str]]


def _v(values: dict, key: str) -> str:
    value = values.get(key)
    return "n/a" if value is None else str(value)


def _p(params: dict, key: str) -> str:
    value = params.get(key)
    return "n/a" if value is None else str(value)


# Strategy reason -> sentence built from the decision's stored indicator values and the configured parameters.
def signal_sentence(reason: str | None, values: dict, params: dict) -> str:
    v, p = (lambda k: _v(values, k)), (lambda k: _p(params, k))
    templates = {
        "ORB_BREAKOUT": lambda: (f"the finalized bar closed at {v('close')}, above the opening-range high {v('or_high')} "
                                 f"(range low {v('or_low')}, range {p('range_minutes')} minutes, status {v('or_status')})"),
        "VWAP_CROSS_UP": lambda: (f"the close {v('close')} crossed above VWAP {v('vwap')} (previous close "
                                  f"{v('prev_close')} was at or below the previous VWAP {v('prev_vwap')})"),
        "VWAP_CROSS_DOWN": lambda: (f"the close {v('close')} crossed below VWAP {v('vwap')} (previous close "
                                    f"{v('prev_close')} was at or above the previous VWAP {v('prev_vwap')})"),
        "EMA_CROSS_UP": lambda: (f"the fast EMA ({p('fast_period')}) {v('ema_fast')} crossed above the slow EMA "
                                 f"({p('slow_period')}) {v('ema_slow')} (previously {v('prev_ema_fast')} vs "
                                 f"{v('prev_ema_slow')})"),
        "EMA_CROSS_DOWN": lambda: (f"the fast EMA ({p('fast_period')}) {v('ema_fast')} crossed below the slow EMA "
                                   f"({p('slow_period')}) {v('ema_slow')}"),
        "OVERSOLD_BELOW_LOWER_BAND": lambda: (f"the close {v('close')} was below the lower Bollinger band {v('bb_lower')} "
                                              f"and RSI {v('rsi')} was below the oversold threshold {p('oversold')}"),
        "REVERTED_TO_MIDDLE_BAND": lambda: (f"the close {v('close')} reached the middle Bollinger band {v('bb_mid')}"),
        "SUPERTREND_FLIP_UP": lambda: (f"Supertrend flipped to direction {v('direction')} with the line at "
                                       f"{v('supertrend_line')} (close {v('close')})"),
        "SUPERTREND_FLIP_DOWN": lambda: (f"Supertrend flipped to direction {v('direction')} with the line at "
                                         f"{v('supertrend_line')} (close {v('close')})"),
        "SESSION_SQUARE_OFF": lambda: (f"the bar ended at or after the configured square-off time {p('square_off')}, "
                                       "so the open position is exited for the session"),
    }
    template = templates.get(reason or "")
    if template is not None:
        return template()
    shown = ", ".join(f"{k} {val}" for k, val in sorted(values.items()) if val is not None) or "none recorded"
    return f"reason {reason or 'n/a'}; indicator values: {shown}"


OUTCOME_SENTENCES = {
    "SELECTED": "the session's strategy selection was locked to this configuration",
    "NO_SELECTION": "no strategy configuration was selected for this bar, so nothing could be traded",
    "NO_SIGNAL": "the acting strategy gave no signal on this bar",
    "ENTRY_SUBMITTED": "the agent submitted an entry order through the order service (its status is below)",
    "EXIT_SUBMITTED": "the agent submitted an exit order through the order service (its status is below)",
    "EXIT_OWED": "the exit signal was recorded as an owed exit, submitted as soon as an eligible price exists (D6)",
    "EXIT_ALREADY_OWED": "an exit was already owed for this position, so nothing new was recorded",
    "SIGNALS_ONLY_NOT_SUBMITTED": "the agent runs SIGNALS_ONLY (execution is disabled), so the signal was recorded and "
                                  "no order was submitted",
    "SUPPRESSED_PAUSED": "the agent was paused, so the entry was suppressed (exits continue while paused)",
    "SUPPRESSED_KILL_SWITCH": "the Phase 4 kill switch was engaged, so the entry was suppressed",
    "SUPPRESSED_INSTRUMENT_BLOCKED": "the instrument was blocked for the agent (manual intervention, overnight orphan "
                                     "or data error), so the agent placed nothing there",
    "SUPPRESSED_POSITION_OR_ORDER": "a position or an open order already existed in the instrument, so no new entry was made",
    "SUPPRESSED_LATE_SIGNAL": "the signal bar was not the latest eligible bar; late entry signals are never acted on (D5)",
    "SUPPRESSED_PRICE_NOT_ELIGIBLE": "no eligible execution price passed the price gate, so the entry was suppressed",
    "SUPPRESSED_INVALID_LEVELS": "the protective levels were invalid (the stop was not below the signal close), so the "
                                 "entry was suppressed",
    "SUPPRESSED_NO_QUANTITY": "no cash was available for an entry, so no order was proposed",
    "SUPPRESSED_NOT_HELD": "there was no agent position to exit, so the exit signal had nothing to do",
}

EVENT_SENTENCES = {
    "agent.started": "the user started the agent",
    "agent.stopped": "the user stopped the agent; it evaluates nothing but Phase 4 protective exits continue",
    "agent.paused": "the user paused the agent; it keeps evaluating and exiting but opens nothing new",
    "agent.resumed": "the user resumed the agent",
    "agent.error": "the agent entered ERROR and places no orders until it is resumed or stopped",
    "agent.paused_by_kill_switch": "the agent was paused because the Phase 4 kill switch was engaged",
    "agent.paused_execution_unavailable": "the agent was paused because execution became unavailable",
    "agent.kill_switch_engaged": "the kill switch was engaged: entries are blocked and open orders cancelled; positions "
                                 "are not liquidated",
    "agent.reconciliation_failed": "reconciliation found a discrepancy between agent decisions and orders",
    "agent.instrument_status_changed": "the agent's status for this instrument changed",
    "agent.instrument_data_error": "the archive could not be replayed for this instrument (contained to it)",
    "agent.owed_exit_expired_session_end": "an owed exit expired at the end of its session; the position is "
                                           "ORPHANED_OVERNIGHT and is not carried over (D2)",
    "agent.owed_exit_abandoned_on_stop": "an owed exit was abandoned because the agent was stopped",
    "agent.owed_exit_satisfied": "an owed exit was satisfied: the position is flat",
    "agent.cycle_failed": "an agent cycle failed and was rolled back",
    "agent.cycle_interrupted": "an agent cycle was interrupted (its worker stopped before finishing)",
    "agent.reconciled": "the agent reconciled its records with the orders before starting or resuming",
    "agent.config_saved": "a new agent configuration version was saved",
    "agent.created": "the agent was created",
}

INSTRUMENT_STATUS = {
    "MANUAL_INTERVENTION": "a manual order, a manual fill or a corporate-action adjustment touched the instrument, so "
                           "the agent places no further order there for the rest of the session (D7)",
    "ORPHANED_OVERNIGHT": "an agent position outlived its session; it is never carried over by the agent and is "
                          "managed only by its Phase 4 stop/target and the user (D2)",
    "DATA_ERROR": "the archive could not be replayed for the instrument",
    "OK": "the instrument is available to the agent again",
}


def _sym(facts: dict) -> str:
    inst = facts.get("instrument") or {}
    return inst.get("symbol") or (f"instrument {inst.get('id')}" if inst.get("id") else "the account")


def _risk_sections(risk: dict | None) -> Sections:
    if not risk:
        return []
    text = (f"risk decision {risk['id']} was {risk['outcome']}: proposed {risk['proposed_quantity']}, approved "
            f"{risk['approved_quantity']}")
    if risk.get("reasons"):
        text += "; reasons: " + "; ".join(str(r) for r in risk["reasons"])
    return [("Risk decision", text + ".")]


def _order_sections(order: dict | None, execution: dict | None) -> Sections:
    if not order:
        return []
    out: Sections = []
    text = (f"{order['source']} {order['order_type']} {order['side']} order {order['client_order_id']}: requested "
            f"{order['requested_quantity']}, quantity after risk sizing {order['quantity']}, filled "
            f"{order['filled_quantity']}, status {order['status']}")
    if order.get("reject_reason"):
        text += f"; rejected: {order['reject_reason']}"
    if order.get("cancel_reason"):
        text += f"; cancelled: {order['cancel_reason']}"
    if order.get("stop_loss_price") or order.get("take_profit_price"):
        text += f"; stop {order.get('stop_loss_price') or 'none'}, target {order.get('take_profit_price') or 'none'}"
    out.append(("Order", text + "."))
    fills = (execution or {}).get("fills") or []
    if not fills:
        if order["status"] in ("PENDING", "PARTIALLY_FILLED"):
            out.append(("Execution", "no fill yet; Mock Exchange fills happen at the open of a later finalized bar."))
        elif order["status"] != "FILLED":
            out.append(("Execution", "the order was never filled."))
        return out
    for f in fills:
        text = (f"fill {f['id']} (#{f['seq']}): {f['side']} {f['quantity']} at {f['executed_price']} (reference price "
                f"{f['reference_price']}, {f['price_basis']}, {f['execution_policy']}); fee {f['fee']}")
        if f.get("realized_pnl") is not None and f["side"] == "SELL":
            text += f"; realized P&L of this fill (gross of fees) {f['realized_pnl']}"
        out.append(("Execution", text + "."))
    if order["status"] == "PARTIALLY_FILLED" or (order["status"] == "CANCELLED" and order["filled_quantity"]):
        out.append(("Execution", f"partial fill: {order['filled_quantity']} of {order['quantity']} filled."))
    if order.get("execution_policy") == "SIMULATION_PROVIDER_NATIVE":
        out.append(("Execution", "these are simulated fills on unverified provider data, not verified executions."))
    return out


def render_decision(facts: dict) -> Sections:
    out: Sections = []
    sym, signal = _sym(facts), facts.get("strategy_signal")
    decision = facts["agent_decision"]
    params = ((facts.get("configuration") or {}).get("candidate") or {}).get("parameters") or {}
    sel = facts.get("selection")
    if sel:
        regime = f" with the regime {sel['regime']}" if sel.get("regime") else ""
        out.append(("Selection", f"{sym}: the session selection was {sel['outcome']}{regime} "
                                 f"({sel.get('reason') or 'no reason recorded'}); selected configuration "
                                 f"{sel.get('selected_config_id') or 'none'}."))
    if signal:
        verdict = {"ENTER_LONG": "generated an entry signal", "EXIT_LONG": "generated an exit signal"}.get(
            signal["action"], "gave no signal")
        cfg = facts.get("configuration") or {}
        text = (f"{sym} {signal['strategy_id']} (version {signal['strategy_version']}, configuration "
                f"{signal['config_id']}, agent configuration v{cfg.get('version')}) {verdict} "
                f"({signal['reason']}) on the 5-minute bar starting {signal['signal_bar_start_ist']}")
        if signal["action"] in ("ENTER_LONG", "EXIT_LONG"):
            text += f" because {signal_sentence(signal['reason'], signal['indicator_values'] or {}, params)}"
        text += "."
        out.append(("Strategy signal", text))
    candle = facts.get("candle")
    if candle:
        out.append(("Market data", f"candle {candle['id']} (observation version {candle['observation_version']}, "
                                   f"{candle['price_basis']}): open {candle['open']}, high {candle['high']}, low "
                                   f"{candle['low']}, close {candle['close']}."))
    text = OUTCOME_SENTENCES.get(decision["outcome"], decision["outcome"])
    detail = f" Detail: {decision['detail']}." if decision.get("detail") else ""
    out.append(("Agent decision", f"{decision['kind']} {decision['outcome']}: {text}.{detail}"))
    out += _risk_sections(facts.get("risk_decision"))
    out += _order_sections(facts.get("order"), facts.get("execution"))
    if not facts.get("order") and decision["outcome"] not in ("ENTRY_SUBMITTED", "EXIT_SUBMITTED"):
        out.append(("Outcome", "no order exists for this decision."))
    return out


def render_order(facts: dict) -> Sections:
    out: Sections = []
    sym = _sym(facts)
    if facts.get("agent_decision"):
        d = facts["agent_decision"]
        out.append(("Agent decision", f"{sym}: created by agent decision {d['id']} ({d['outcome']}) on a "
                                      f"{d.get('strategy_id') or 'strategy'} {d.get('reason') or ''} signal at "
                                      f"{d.get('bar_start_ist') or 'n/a'}.".replace("  ", " ")))
    if facts.get("protective_exit"):
        out.append(("Protective exit", f"{sym}: a Phase 4 protective (stop-loss / take-profit) exit order."))
    out += _risk_sections(facts.get("risk_decision"))
    out += _order_sections(facts.get("order"), facts.get("execution"))
    trade = facts.get("trade")
    if trade:
        if trade["status"] == "CLOSED":
            out.append(("Outcome", f"the trade closed at {trade['closed_at']} as {trade['outcome']}: stored realized "
                                   f"P&L {trade['realized_pnl']} (gross), fees {trade['fees']}, net "
                                   f"{trade['net_realized_pnl']}."))
        else:
            out.append(("Outcome", "the trade is still open."))
    pos = facts.get("position") or {}
    if pos.get("quantity_now"):
        if pos.get("unrealized_pnl_now") is not None:
            out.append(("Position now", f"open {pos['quantity_now']} at average {pos['avg_entry_price']}; unrealized "
                                        f"P&L {pos['unrealized_pnl_now']} at mark {pos['mark_price_now']} (current "
                                        "mark, not realized)."))
        else:
            reason = pos.get("unrealized_unavailable_reason") or "no eligible mark price"
            out.append(("Position now", f"open {pos['quantity_now']} at average {pos['avg_entry_price']}; unrealized "
                                        f"P&L unavailable: {reason}."))
    return out


def render_event(facts: dict) -> Sections:
    e = facts["event"]
    text = EVENT_SENTENCES.get(e["event_type"], e["event_type"])
    out: Sections = [("Agent event", f"{e['occurred_at_ist']}: {text}"
                      + (f" ({e['from_status']} -> {e['to_status']})" if e.get("to_status") else "") + ".")]
    details = e.get("details") or {}
    if e["event_type"] == "agent.instrument_status_changed":
        to = details.get("to")
        out.append(("Instrument", f"{_sym(facts)} became {to}: {INSTRUMENT_STATUS.get(to, to)}."
                    + (f" Detail: {details['detail']}." if details.get("detail") else "")))
    elif details:
        shown = "; ".join(f"{k}: {v}" for k, v in sorted(details.items()) if not isinstance(v, (dict, list)))
        if shown:
            out.append(("Details", shown + "."))
    if facts.get("agent_now"):
        out.append(("Agent now", f"the agent is {facts['agent_now']['status']}."))
    return out


def render_daily(facts: dict) -> Sections:
    s, o, c, r = facts["signals"], facts["orders"], facts["closed_trades"], facts["realized"]
    out: Sections = [
        ("Signals", f"{s['total']} strategy signals ({s['entry']} entry, {s['exit']} exit); "
                    f"{facts['agent_decisions']['total']} agent decisions in total."),
        ("Orders", f"{o['total']} orders ({o['rejected']} rejected, {o['cancelled']} cancelled); "
                   f"{facts['risk']['rejections']} risk rejections. Orders are not trades."),
        ("Trades", f"{c['total']} closed trades: {c['winning']} winning, {c['losing']} losing, {c['breakeven']} "
                   f"breakeven, {c['unknown']} unknown; {facts['fills']} fills."),
        ("Realized P&L", f"realized P&L {r['realized_pnl_gross']} gross, fees {r['fees']}, net {r['net_realized_pnl']} "
                         "(daily_pnl, by simulated fill time)."),
        ("Unrealized", f"not part of the session summary; {facts['unrealized']['open_positions_now']} positions are "
                       "open now (see the portfolio for their current marks)."),
    ]
    if facts["agent_events"]:
        shown = "; ".join(f"{e['at_ist']} {e['event_type']}" + (f" ({e['instrument']})" if e.get("instrument") else "")
                          for e in facts["agent_events"][:8])
        out.append(("Agent events", shown + "."))
    if facts.get("journal_reflections"):
        out.append(("Journal", f"{facts['journal_reflections']} trade reflections written for this session."))
    return out


def render_insights(facts: dict) -> Sections:
    ct = facts["closed_trades"]
    out: Sections = [("Sample", f"{ct['total']} closed trades between {facts['period']['from']} and "
                                f"{facts['period']['to']}; {ct['winning']} winning, {ct['losing']} losing.")]
    out += [("Caveat", c) for c in facts.get("caveats", [])]
    return out


RENDERERS = {
    "AGENT_DECISION": render_decision, "ORDER": render_order, "AGENT_EVENT": render_event, "DAILY_SUMMARY": render_daily,
    "JOURNAL_INSIGHTS": render_insights,
}


def render(kind: str, facts: dict[str, Any]) -> Sections:
    return RENDERERS[kind](facts)


def as_text(sections: Sections) -> str:
    return "\n".join(f"{layer}: {text}" for layer, text in sections)
