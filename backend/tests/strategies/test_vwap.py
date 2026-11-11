"""VWAP cross strategy. Hand-computed sequences use ATR(2) so the numbers stay small; defaults are tested separately."""

from datetime import time

import pytest

from app.indicators import ATR, SessionVWAP, run
from app.strategies import FLAT, Action, PositionContext, Reason, VWAPConfig, VWAPCross
from tests.strategies import DAY, NEXT_DAY, at, events, hhmm, pbar, simulate

R = Reason

# t   bar                         TP        session VWAP          ATR(2)
# 0   100/101/99/100   v=0        100       None (volume 0)       -
# 1   100/101/99/100   v=100      100       100                   -
# 2   100/100.5/99/99.5 v=100     299/3     99.8333 (19966.67/200) 1.75
# 3   99.5/101/99.5/100.8 v=100   301.3/3   100.0333 (30010/300)   1.625  <- close crosses above VWAP: ENTER
# 4   100.8/101/100.5/100.9 v=100 100.8     100.225                1.5625
# 5   100.9/100.9/99/99.2 v=200   99.7      100.05                 -      <- close crosses below VWAP: EXIT
ROWS = [
    ("09:15", 100, 101, 99, 100, 0),
    ("09:20", 100, 101, 99, 100, 100),
    ("09:25", 100, 100.5, 99, 99.5, 100),
    ("09:30", 99.5, 101, 99.5, 100.8, 100),
    ("09:35", 100.8, 101, 100.5, 100.9, 100),
    ("09:40", 100.9, 100.9, 99, 99.2, 200),
]


def sequence(day=DAY):
    return [at(day, t, o, h, low, c, v) for t, o, h, low, c, v in ROWS]


def test_cross_up_enters_and_cross_down_exits():
    out = simulate(VWAPCross(VWAPConfig(atr_period=2)), sequence())
    assert [s.reason for s in out] == [
        R.BEFORE_ELIGIBLE_TIME, R.BEFORE_ELIGIBLE_TIME, R.NO_ENTRY_CONDITION, R.VWAP_CROSS_UP, R.POSITION_OPEN_NO_EXIT, R.VWAP_CROSS_DOWN,
    ]
    entry = out[3]
    assert entry.action is Action.ENTER_LONG and entry.reference_price == 100.8
    assert entry.values["vwap"] == pytest.approx(30010 / 300) and entry.values["prev_vwap"] == pytest.approx(19966.666666666668 / 200)
    assert entry.protective.stop_distance == pytest.approx(1.5 * 1.625) and entry.protective.stop_price is None
    assert out[5].action is Action.EXIT_LONG and out[5].values["vwap"] == pytest.approx(100.05)


def test_no_entry_before_09_30_even_on_a_cross():
    bars = [at(DAY, "09:15", 99.5, 100, 98.5, 99, 100), at(DAY, "09:20", 99, 101, 99, 100.5, 100)]
    assert simulate(VWAPCross(VWAPConfig(atr_period=1)), bars)[-1].reason is R.BEFORE_ELIGIBLE_TIME
    earlier = VWAPCross(VWAPConfig(atr_period=1, eligible_from=time(9, 25)))
    assert simulate(earlier, bars)[-1].reason is R.VWAP_CROSS_UP  # the same cross, once eligible


def test_zero_volume_leaves_the_vwap_and_signals_unavailable():
    bars = [pbar(DAY, s, 100.0 + (s % 3), volume=0) for s in range(12)]
    out = simulate(VWAPCross(VWAPConfig(atr_period=2)), bars)
    assert {s.reason for s in out[3:]} == {R.INDICATOR_NOT_READY} and all(s.values["vwap"] is None for s in out)


def test_no_cross_is_measured_against_the_previous_session():
    s = VWAPCross(VWAPConfig(atr_period=1, eligible_from=time(9, 15)))
    for b in sequence():
        s.evaluate(b, FLAT)
    first = s.evaluate(at(NEXT_DAY, "09:15", 110, 111, 109, 110.5, 100), FLAT)  # far above yesterday's VWAP
    assert first.reason is R.INDICATOR_NOT_READY and first.values["prev_vwap"] is None


def test_values_come_from_the_approved_indicators():
    bars = sequence()
    out = simulate(VWAPCross(VWAPConfig(atr_period=2)), bars)
    assert [s.values["vwap"] for s in out] == run(SessionVWAP(), bars)
    assert [s.values["atr"] for s in out] == run(ATR(2), bars)


def test_default_atr14_must_warm_up_before_any_entry():
    # a cross on every other bar from 09:30; ATR(14) is first defined on the 15th bar (09:25 + 10 bars = 10:25)
    bars = [pbar(DAY, s, 100.0 + (1 if s % 2 else -1), volume=100) for s in range(20)]
    out = simulate(VWAPCross(), bars)
    first_entry = next(i for i, s in enumerate(out) if s.action is Action.ENTER_LONG)
    assert out[14].values["atr"] is not None and out[13].values["atr"] is None
    assert first_entry >= 14 and all(s.reason in (R.BEFORE_ELIGIBLE_TIME, R.INDICATOR_NOT_READY, R.NO_ENTRY_CONDITION) for s in out[:14])


def test_exit_on_cross_down_is_not_blocked_by_the_entry_cutoff():
    bars = [at(DAY, "09:15", 100, 101, 99, 100, 100)] + [pbar(DAY, s, 100.0, volume=100) for s in range(1, 66)]
    bars += [pbar(DAY, 66, 90.0, volume=100)]  # 14:45 bar: sharp drop below the VWAP
    s = VWAPCross(VWAPConfig(atr_period=2))
    out = [s.evaluate(b, FLAT if i == 0 else PositionContext(is_open=True)) for i, b in enumerate(bars)]
    assert hhmm(out[-1]) == "14:45" and (out[-1].action, out[-1].reason) == (Action.EXIT_LONG, R.VWAP_CROSS_DOWN)


@pytest.mark.parametrize(
    "kwargs",
    [{"eligible_from": time(15, 0)}, {"eligible_from": time(9, 31)}, {"atr_period": 0}, {"stop_atr_multiple": 0}, {"stop_atr_multiple": float("inf")}],
)
def test_invalid_vwap_configuration(kwargs):
    with pytest.raises(ValueError):
        VWAPConfig(**kwargs)


def test_only_long_events():
    assert {a for _, a, _ in events(simulate(VWAPCross(VWAPConfig(atr_period=2)), sequence()))} <= {Action.ENTER_LONG, Action.EXIT_LONG}
