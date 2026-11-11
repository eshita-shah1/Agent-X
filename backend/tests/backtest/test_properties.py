"""Whole-engine properties with the real strategies and the regime-priority selector over several sessions:
determinism, no look-ahead, truncation invariance and the trading invariants (decisions D5, D6, D15)."""

import dataclasses
import math
from datetime import date, datetime, timedelta
from decimal import Decimal

from app.backtest import BacktestRequest, ExitReason, InstrumentData, run_backtest
from app.marketdata.calendar import EXCHANGE_TZ
from app.strategies import ORBConfig
from app.strategies.selection import Candidate, PinnedPolicy, RegimePriorityPolicy
from tests.backtest import D, NEXT_DAY, bar, local, session
from tests.strategies.selection import ALL

DAYS = [d for d in (date(2026, 10, 26) + timedelta(days=i) for i in range(17)) if d.weekday() < 5]  # 13 sessions
MISSING = {(5, 2), (6, 40), (6, 41), (7, 72), (9, 0)}  # a 09:25 checkpoint bar, a gap, the 15:15 bar, a late start
SHORT_SESSION = (10, 50)  # session 10 ends after 50 bars


def series():
    out, i = [], 0
    for s, day in enumerate(DAYS):
        for slot in range(75):
            i += 1
            if (s, slot) in MISSING or (s == SHORT_SESSION[0] and slot >= SHORT_SESSION[1]):
                continue
            close = 100 + 6 * math.sin(i / 25) + 0.8 * math.sin(i / 3) + 0.3 * math.sin(i / 1.7)
            o = close + 0.15 * math.sin(i * 1.3)
            c, o = D(f"{close:.2f}"), D(f"{o:.2f}")
            wick = D(f"{0.2 + 0.25 * abs(math.sin(i * 0.7)):.2f}")
            out.append(bar(day, f"{(555 + 5 * slot) // 60:02d}:{(555 + 5 * slot) % 60:02d}", o, max(o, c) + wick, min(o, c) - wick, c, 1000 + i % 300))
    return out


BARS = tuple(series())
REQUEST = BacktestRequest(("X",), DAYS[2], DAYS[-1], RegimePriorityPolicy(ALL), D(100000))


def full_run(bars=BARS, request=REQUEST):
    return run_backtest(request, {"X": InstrumentData("X", tuple(bars))})


RESULT = full_run()
INST = RESULT.instruments[0]


def test_the_scenario_exercises_the_engine():
    assert len(INST.trades) >= 5 and {t.config_id for t in INST.trades} >= {"orb-1"}
    assert len(INST.selection_decisions) == INST.data_quality.evaluation_sessions == 11
    assert INST.data_quality.missing_bars == 1 + 2 + 1 + 1 + 25  # MISSING, plus the short session's last 25 slots
    assert {t.exit_reason for t in INST.trades} >= {ExitReason.TARGET, ExitReason.STOP, ExitReason.STRATEGY_EXIT}


def test_identical_on_repeat():
    again = full_run()
    assert again == RESULT and again.result_fingerprint == RESULT.result_fingerprint


def before(inst, cut):
    return (
        [t for t in inst.trades if t.exit_bar_start < cut],
        [p for p in inst.equity if p.bar_start < cut],
        [e for e in inst.events if e.bar_start < cut],
        [d for d in inst.selection_decisions if datetime.fromisoformat(d["checkpoint"]) < cut],
    )


def test_no_lookahead_future_prices_cannot_change_the_past():
    for cut_index in (300, 420, 555, 700):
        cut = BARS[cut_index].start_ts
        scaled = BARS[:cut_index] + tuple(
            dataclasses.replace(b, open=b.open * D("1.37"), high=b.high * D("1.37"), low=b.low * D("1.37"), close=b.close * D("1.37"))
            for b in BARS[cut_index:]
        )
        other = full_run(scaled).instruments[0]
        assert before(other, cut) == before(INST, cut), cut_index


def test_truncation_at_session_boundaries_keeps_every_earlier_result():
    for keep in range(4, len(DAYS)):
        last_day = DAYS[keep - 1]
        cut_bars = tuple(b for b in BARS if b.session_date <= last_day)
        other = full_run(cut_bars).instruments[0]
        cut = min(b.start_ts for b in BARS if b.session_date > last_day) if keep < len(DAYS) else None
        if cut is not None:
            assert before(other, cut) == before(INST, cut), keep


def test_trading_invariants():
    decisions = {d["session_date"]: d for d in INST.selection_decisions}
    for t in INST.trades:
        assert t.entry_bar_start.astimezone(EXCHANGE_TZ).date() == t.exit_bar_start.astimezone(EXCHANGE_TZ).date() == t.session_date
        d = decisions[t.session_date.isoformat()]
        assert d["status"] == "SELECTED" and d["selected"]["config_id"] == t.config_id  # only the locked selection trades
        assert "09:25" <= local(t.entry_signal_bar_start) <= "14:40"  # signal bar ends 09:30..14:45
        assert t.quantity > 0 and isinstance(t.quantity, int)
        if t.stop_level is not None and t.exit_reason is ExitReason.STOP:
            assert t.exit_price == t.stop_level
    by_session = {}
    for p in INST.equity:
        assert p.cash >= 0 and p.equity == p.cash + p.quantity * p.close
        by_session[p.session_date] = p
    assert all(p.quantity == 0 for p in by_session.values())  # flat at every session's last bar: never overnight


def test_the_selector_alone_decides_which_configuration_may_enter():
    entry_events = [e for e in INST.events if e.event_type.value == "ENTRY_SIGNAL"]
    decisions = {d["session_date"]: d["selected"]["config_id"] if d["selected"] else None for d in INST.selection_decisions}
    assert all(decisions[e.session_date.isoformat()] == e.config_id for e in entry_events)
    assert INST.summary.entry_signals_ignored > 0  # non-selected candidates kept warm and their signals discarded


def test_the_real_orb_strategy_end_to_end():
    """Range 09:15-09:30 high 101 / low 99; 09:30 closes 101.5 -> entry at the 09:35 open 101.6; stop at the range
    low 99 (absolute); target 101.6 + 1.5 x 2 = 104.6, touched at 10:30."""
    overrides = {
        "09:15": (100, 101, 99, 100), "09:20": (100, 100.5, 99.5, 100), "09:25": (100, 100.8, 99.6, 100.5),
        "09:30": (100.5, 101.6, 100.4, 101.5), "09:35": (101.6, 101.8, 101.4, 101.7), "10:30": (102, 104.7, 101.9, 104),
    }
    request = BacktestRequest(("X",), NEXT_DAY, NEXT_DAY, PinnedPolicy(Candidate("orb", "ORB", ORBConfig())), D(100000), warmup_sessions=0)
    result = run_backtest(request, {"X": InstrumentData("X", tuple(session(NEXT_DAY, price="101.7", overrides=overrides)))})
    (t,) = result.instruments[0].trades
    assert (local(t.entry_signal_bar_start), local(t.entry_bar_start), t.entry_price, t.quantity) == ("09:30", "09:35", D("101.6"), 984)
    assert (t.stop_level, t.target_level) == (D(99), D("104.6"))
    assert (local(t.exit_bar_start), t.exit_reason, t.exit_price) == ("10:30", ExitReason.TARGET, D("104.6"))
    assert t.gross_pnl == 984 * D(3) and type(t.gross_pnl) is Decimal
