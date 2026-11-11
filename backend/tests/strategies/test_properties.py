"""Properties of every strategy under its approved defaults, on a deterministic multi-session series with gaps.

The series is a fixed formula (no randomness); it is for exercising behaviour, not evidence of profitability."""

import dataclasses
import math
from datetime import timedelta

import pytest

from app.marketdata.calendar import EXCHANGE_TZ
from app.strategies import STRATEGIES, Action, Reason
from tests.strategies import DAY, NEXT_DAY, THIRD_DAY, pbar, simulate

SESSIONS = [DAY, NEXT_DAY, THIRD_DAY]
MISSING = {(0, 30), (0, 31), (1, 1), (1, 50), (2, 73), (2, 74)}  # mid-session gaps, an opening-range gap, closing gaps


def series():
    out = []
    for s, day in enumerate(SESSIONS):
        for slot in range(75):
            if (s, slot) in MISSING:
                continue
            i = s * 75 + slot
            close = 100 + 4 * math.sin(2 * math.pi * i / 40) + 1.5 * math.sin(2 * math.pi * i / 11) + 0.6 * math.sin(i / 2.3)
            out.append(pbar(day, slot, close, spread=0.3 + 0.2 * abs(math.sin(i)), volume=0 if slot == 0 else 500 + (i * 53) % 900))
    return out


BARS = series()
IDS = sorted(STRATEGIES)


def run_default(strategy_id, bars=BARS):
    return simulate(STRATEGIES[strategy_id].create(), bars)


def local_end(bar):
    return (bar.start_ts + timedelta(minutes=5)).astimezone(EXCHANGE_TZ).time()


@pytest.mark.parametrize("strategy_id", IDS)
def test_every_strategy_trades_on_the_series(strategy_id):
    """Makes the invariants below non-vacuous: each strategy enters and exits at least once."""
    actions = [s.action for s in run_default(strategy_id)]
    assert Action.ENTER_LONG in actions and Action.EXIT_LONG in actions


@pytest.mark.parametrize("strategy_id", IDS)
def test_truncation_invariance(strategy_id):
    """The signal for bar t is the same whether the data ends at t or continues: no lookahead, no repainting."""
    full = run_default(strategy_id)
    for t in range(len(BARS)):
        assert run_default(strategy_id, BARS[: t + 1]) == full[: t + 1], f"{strategy_id} differs at t={t}"


@pytest.mark.parametrize("strategy_id", IDS)
def test_changing_future_bars_never_changes_earlier_signals(strategy_id):
    cut = 120
    perturbed = BARS[:cut] + [dataclasses.replace(b, high=b.high + 5, close=b.close + 4.5, open=b.open + 4) for b in BARS[cut:]]
    assert run_default(strategy_id, perturbed)[:cut] == run_default(strategy_id)[:cut]


@pytest.mark.parametrize("strategy_id", IDS)
def test_repeatability_is_exact(strategy_id):
    assert run_default(strategy_id) == run_default(strategy_id)


@pytest.mark.parametrize("strategy_id", IDS)
def test_common_rules_hold_for_every_signal(strategy_id):
    config = STRATEGIES[strategy_id].default_config()
    out = run_default(strategy_id)
    prev = None
    is_open, pending = False, None
    for bar, sig in zip(BARS, out):
        same_session = prev is not None and prev.session_date == bar.session_date
        gap = (not same_session and bar.start_ts.astimezone(EXCHANGE_TZ).strftime("%H:%M") != "09:15") or (
            same_session and bar.start_ts - prev.start_ts != timedelta(minutes=5)
        )
        closed = pending is Action.EXIT_LONG
        if pending is Action.ENTER_LONG and same_session:
            is_open = True
        elif closed:
            is_open = False
        assert sig.action in (Action.ENTER_LONG, Action.EXIT_LONG, Action.NONE)
        if sig.action is Action.ENTER_LONG:
            assert not is_open and not gap and not closed, sig  # no duplicate entry, no entry after a gap or in cooldown
            assert local_end(bar) <= config.entry_cutoff, sig
            assert sig.reference_price == bar.close and sig.bar_start == bar.start_ts
        if sig.action is Action.EXIT_LONG:
            assert is_open, sig
        if is_open and local_end(bar) >= config.square_off:
            assert (sig.action, sig.reason) == (Action.EXIT_LONG, Reason.SESSION_SQUARE_OFF), sig
        pending = sig.action if sig.action is not Action.NONE else None
        prev = bar


@pytest.mark.parametrize("strategy_id", IDS)
def test_no_position_is_open_at_any_session_boundary(strategy_id):
    """Every position opened in a session is closed in it (square-off), including day 3 whose 15:20/15:25 bars are
    missing. (A position handed to the next session would also make the strategy raise.)"""
    out = run_default(strategy_id)
    is_open, pending = False, None
    for i, (bar, sig) in enumerate(zip(BARS, out)):
        if i and BARS[i - 1].session_date != bar.session_date:
            assert not is_open or pending is Action.EXIT_LONG, f"open position carried into {bar.session_date}"
            is_open, pending = False, None
        if pending is Action.ENTER_LONG:
            is_open = True
        elif pending is Action.EXIT_LONG:
            is_open = False
        pending = sig.action if sig.action is not Action.NONE else None
    assert not is_open or pending is Action.EXIT_LONG
