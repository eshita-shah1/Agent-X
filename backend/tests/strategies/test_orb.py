"""Opening Range Breakout: approved defaults (15-minute range, 11:30 cutoff, target 1.5 x range height)."""

import pytest

from app.indicators import OpeningRange, run
from app.strategies import FLAT, Action, OpeningRangeBreakout, ORBConfig, PositionContext, ProtectiveLevels, Reason
from tests.strategies import DAY, NEXT_DAY, at, events, hhmm, pbar, simulate, slot_of

R = Reason


def opening(day=DAY, skip=()):
    rows = {"09:15": (100, 101, 99, 100), "09:20": (101, 103, 100, 102), "09:25": (102, 102, 98, 99)}  # range 103 / 98
    return [at(day, t, *row) for t, row in rows.items() if t not in skip]


def quiet(start, end, day=DAY, close=102.0):
    """Bars that stay inside the range (close below 103)."""
    return [pbar(day, s, close, 0.4) for s in range(slot_of(start), slot_of(end) + 1)]


def by_time(signals):
    return {hhmm(s): s for s in signals}


def test_breakout_above_the_completed_range_enters_with_stop_and_target():
    bars = opening() + [at(DAY, "09:30", 102, 102.9, 101.5, 102.5), at(DAY, "09:35", 102.5, 104, 102.4, 103.5)]
    out = by_time(simulate(OpeningRangeBreakout(), bars))
    assert [out[t].reason for t in ("09:15", "09:20")] == [R.OPENING_RANGE_FORMING] * 2
    assert out["09:25"].reason is R.NO_ENTRY_CONDITION and out["09:25"].values["or_high"] == 103.0  # complete on this bar
    assert out["09:30"].reason is R.NO_ENTRY_CONDITION
    sig = out["09:35"]
    assert (sig.action, sig.reason, sig.reference_price) == (Action.ENTER_LONG, R.ORB_BREAKOUT, 103.5)
    assert sig.protective == ProtectiveLevels(stop_price=98.0, target_distance=7.5)  # 1.5 x (103 - 98)


def test_a_close_equal_to_the_range_high_is_not_a_breakout():
    bars = opening() + [at(DAY, "09:30", 102, 103.5, 101.5, 103)]
    assert simulate(OpeningRangeBreakout(), bars)[-1].reason is R.NO_ENTRY_CONDITION


def test_entry_cutoff_is_11_30():
    on_time = opening() + quiet("09:30", "11:20") + [at(DAY, "11:25", 102, 104, 101.8, 103.6)]  # ends 11:30
    late = opening() + quiet("09:30", "11:25") + [at(DAY, "11:30", 102, 104, 101.8, 103.6)]  # ends 11:35
    assert simulate(OpeningRangeBreakout(), on_time)[-1].action is Action.ENTER_LONG
    assert simulate(OpeningRangeBreakout(), late)[-1].reason is R.AFTER_ENTRY_CUTOFF


def test_at_most_one_entry_per_session_and_the_limit_resets_next_session():
    s = OpeningRangeBreakout()
    for b in opening():
        s.evaluate(b, FLAT)
    assert s.evaluate(at(DAY, "09:30", 102, 104, 101.8, 103.6), FLAT).action is Action.ENTER_LONG
    s.evaluate(pbar(DAY, slot_of("09:35"), 103.6), PositionContext(is_open=True))
    s.evaluate(pbar(DAY, slot_of("09:40"), 103.6), PositionContext(closed_this_bar=True))  # stopped out, say
    s.evaluate(pbar(DAY, slot_of("09:45"), 102.0), FLAT)
    assert s.evaluate(at(DAY, "09:50", 102, 105, 101.9, 104.5), FLAT).reason is R.SESSION_ENTRY_LIMIT
    for b in opening(NEXT_DAY):
        s.evaluate(b, FLAT)
    assert s.evaluate(at(NEXT_DAY, "09:30", 102, 104, 101.8, 103.6), FLAT).action is Action.ENTER_LONG


@pytest.mark.parametrize("missing", ["09:15", "09:20", "09:25"])
def test_an_incomplete_opening_range_never_trades(missing):
    bars = opening(skip=(missing,)) + quiet("09:30", "09:45") + [at(DAY, "09:50", 102, 106, 101.9, 105.5)]
    out = simulate(OpeningRangeBreakout(), bars)
    assert all(s.action is Action.NONE for s in out)
    assert out[-1].reason is R.OPENING_RANGE_INCOMPLETE and out[-1].values["or_high"] is None


def test_the_bar_after_a_missing_range_slot_reports_the_gap_first():
    out = by_time(simulate(OpeningRangeBreakout(), opening(skip=("09:20",))))
    assert out["09:25"].reason is R.GAP_SUPPRESSED


def test_the_range_is_taken_from_the_approved_indicator():
    bars = opening() + quiet("09:30", "09:40")
    direct = run(OpeningRange(15), bars)
    for sig, value in zip(simulate(OpeningRangeBreakout(), bars), direct):
        assert (sig.values["or_status"], sig.values["or_high"], sig.values["or_low"]) == (value.status.value, value.high, value.low)


def test_a_zero_height_range_gives_no_entry():
    flat = [pbar(DAY, s, 100.0, 0.0) for s in range(3)]
    assert simulate(OpeningRangeBreakout(), flat + [at(DAY, "09:30", 100, 101, 100, 100.8)])[-1].reason is R.DEGENERATE_PROTECTIVE_LEVEL


def test_the_strategy_exits_only_at_square_off():
    bars = opening() + [at(DAY, "09:30", 102, 104, 101.8, 103.6)] + quiet("09:35", "15:15", close=110.0)
    assert events(simulate(OpeningRangeBreakout(), bars)) == [
        ("09:30", Action.ENTER_LONG, R.ORB_BREAKOUT),
        ("15:10", Action.EXIT_LONG, R.SESSION_SQUARE_OFF),
    ]


@pytest.mark.parametrize(
    "kwargs", [{"range_minutes": 0}, {"range_minutes": 7}, {"range_minutes": 15.0}, {"target_multiple": 0}, {"target_multiple": -1.5}, {"target_multiple": True}]
)
def test_invalid_orb_configuration(kwargs):
    with pytest.raises(ValueError):
        ORBConfig(**kwargs)
